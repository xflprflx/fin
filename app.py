"""
Servidor Backend de Controle Financeiro Pessoal (FastAPI + SQLite).
Fornece API RESTful para Lançamentos, Dashboard Anual, Orçamento vs Realizado,
Investimentos, Dívidas, Metas e Reserva de Emergência, com mecanismos de Poka-Yoke.
"""
import os
import sqlite3
import json
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "financas.db")

app = FastAPI(title="Controle Financeiro Pessoal API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db_connection():
    """Retorna uma conexão SQLite configurada com Row factory para retorno em formato dict."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ==========================================
# Modelos Pydantic (Validação Poka-Yoke)
# ==========================================
class LancamentoInput(BaseModel):
    data: str = Field(..., description="Data no formato YYYY-MM-DD")
    tipo: str = Field(..., pattern="^(Receita|Despesa)$")
    categoria: str
    descricao: Optional[str] = ""
    valor: float = Field(..., gt=0, description="O valor deve ser estritamente positivo")
    forma_pgto: str = "PIX"
    observacao: Optional[str] = ""


class OrcamentoInput(BaseModel):
    macro_categoria: str
    ano: int
    mes: int = Field(..., ge=1, le=12)
    valor_orcado: float = Field(..., ge=0)


class InvestimentoUpdate(BaseModel):
    id: int
    saldo_inicial: float
    saldo_atual: float
    rentabilidade_pct: float


class DividaUpdate(BaseModel):
    id: int
    saldo_devedor: float
    taxa_juros_mes: float
    parcela_min: float
    parcelas_rest: int


class MetaUpdate(BaseModel):
    id: int
    valor_alvo: float
    prazo: str
    valor_atual: float


class ReservaUpdate(BaseModel):
    despesa_media_manual: float
    usar_media_automatica: int
    meses_seguranca: int
    saldo_atual_reserva: float


# ==========================================
# Endpoints Principais
# ==========================================
@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    """Serve a interface Single-Page Application (SPA) HTML."""
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
    if not os.path.exists(html_path):
        raise HTTPException(status_code=404, detail="Arquivo index.html não encontrado.")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.get("/api/categorias")
def listar_categorias():
    """Retorna todas as categorias separadas por tipo e macro-categoria para alimentar dropdowns."""
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM categorias ORDER BY tipo DESC, nome ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/lancamentos")
def listar_lancamentos(ano: int = 2026, mes: Optional[int] = None, tipo: Optional[str] = None, busca: Optional[str] = None):
    """Lista os lançamentos com suporte a filtros combinados de ano, mês, tipo e termo de busca."""
    conn = get_db_connection()
    query = "SELECT * FROM lancamentos WHERE ano = ?"
    params = [ano]

    if mes:
        query += " AND mes = ?"
        params.append(mes)
    if tipo and tipo in ("Receita", "Despesa"):
        query += " AND tipo = ?"
        params.append(tipo)
    if busca:
        query += " AND (descricao LIKE ? OR categoria LIKE ? OR observacao LIKE ?)"
        termo = f"%{busca}%"
        params.extend([termo, termo, termo])

    query += " ORDER BY data DESC, id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/lancamentos")
def criar_lancamento(payload: LancamentoInput):
    """
    Insere um novo lançamento validando coerência de data e derivando automaticamente
    os campos de mês, ano e competência (Poka-Yoke contra erro de digitação de competência).
    """
    try:
        dt = datetime.strptime(payload.data, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida. Use YYYY-MM-DD.")

    mes = dt.month
    ano = dt.year
    competencia = f"{mes:02d}/{ano}"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO lancamentos (data, mes, ano, tipo, categoria, descricao, valor, forma_pgto, observacao, competencia)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload.data,
            mes,
            ano,
            payload.tipo,
            payload.categoria,
            payload.descricao.strip(),
            round(payload.valor, 2),
            payload.forma_pgto,
            payload.observacao.strip(),
            competencia,
        ),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"status": "ok", "id": new_id}


@app.delete("/api/lancamentos/{lanc_id}")
def excluir_lancamento(lanc_id: int):
    """Remove um lançamento pelo ID."""
    conn = get_db_connection()
    conn.execute("DELETE FROM lancamentos WHERE id = ?", (lanc_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": lanc_id}


@app.get("/api/dashboard")
def obter_dashboard(ano: int = 2026):
    """
    Calcula toda a matriz anual do Dashboard (Receita, Despesa, Saldo do Mês,
    Saldo Acumulado, % Poupado, Orçado vs Realizado, e Resumo por Categoria).
    """
    conn = get_db_connection()

    # 1. Receitas e Despesas por mês
    rows_mes = conn.execute(
        """
        SELECT mes, tipo, SUM(valor) as total
        FROM lancamentos
        WHERE ano = ?
        GROUP BY mes, tipo
        """,
        (ano,),
    ).fetchall()

    receitas_mes = {m: 0.0 for m in range(1, 13)}
    despesas_mes = {m: 0.0 for m in range(1, 13)}
    for r in rows_mes:
        if r["tipo"] == "Receita":
            receitas_mes[r["mes"]] = round(r["total"], 2)
        else:
            despesas_mes[r["mes"]] = round(r["total"], 2)

    # 2. Orçamento total por mês
    rows_orc = conn.execute(
        """
        SELECT mes, SUM(valor_orcado) as total_orcado
        FROM orcamento
        WHERE ano = ?
        GROUP BY mes
        """,
        (ano,),
    ).fetchall()
    orcado_mes = {m: 0.0 for m in range(1, 13)}
    for r in rows_orc:
        orcado_mes[r["mes"]] = round(r["total_orcado"] or 0.0, 2)

    # 3. Cálculos mensais derivados
    saldo_mes = {}
    saldo_acumulado = {}
    pct_poupado = {}
    orcado_vs_real = {}
    acum = 0.0

    for m in range(1, 13):
        rec = receitas_mes[m]
        desp = despesas_mes[m]
        s_mes = round(rec - desp, 2)
        saldo_mes[m] = s_mes
        acum = round(acum + s_mes, 2)
        saldo_acumulado[m] = acum
        pct_poupado[m] = round((s_mes / rec) * 100, 2) if rec > 0 else 0.0
        orcado_vs_real[m] = round(desp - orcado_mes[m], 2) if orcado_mes[m] > 0 or desp > 0 else 0.0

    # 4. Resumo por Categoria (aba ⚙ Resumo / 📥 Receitas)
    rows_cat = conn.execute(
        """
        SELECT l.categoria, l.tipo, COALESCE(c.macro_categoria, 'Outros') as macro_categoria,
               l.mes, SUM(l.valor) as total
        FROM lancamentos l
        LEFT JOIN categorias c ON l.categoria = c.nome
        WHERE l.ano = ?
        GROUP BY l.categoria, l.tipo, l.mes
        """,
        (ano,),
    ).fetchall()

    todas_cats = conn.execute("SELECT nome, tipo, macro_categoria FROM categorias ORDER BY id ASC").fetchall()
    resumo_categorias = []
    mapa_cat = {}
    for c in todas_cats:
        item = {
            "categoria": c["nome"],
            "tipo": c["tipo"],
            "macro_categoria": c["macro_categoria"],
            "meses": {m: 0.0 for m in range(1, 13)},
            "total_ano": 0.0,
        }
        resumo_categorias.append(item)
        mapa_cat[c["nome"]] = item

    for r in rows_cat:
        nome_c = r["categoria"]
        if nome_c not in mapa_cat:
            item = {
                "categoria": nome_c,
                "tipo": r["tipo"],
                "macro_categoria": r["macro_categoria"],
                "meses": {m: 0.0 for m in range(1, 13)},
                "total_ano": 0.0,
            }
            resumo_categorias.append(item)
            mapa_cat[nome_c] = item
        mapa_cat[nome_c]["meses"][r["mes"]] = round(r["total"], 2)
        mapa_cat[nome_c]["total_ano"] = round(sum(mapa_cat[nome_c]["meses"].values()), 2)

    # 5. Totais de Carteira, Dívidas e Reserva
    tot_inv = conn.execute("SELECT COALESCE(SUM(saldo_atual), 0) FROM investimentos").fetchone()[0]
    tot_div = conn.execute("SELECT COALESCE(SUM(saldo_devedor), 0) FROM dividas").fetchone()[0]
    res_cfg = conn.execute("SELECT * FROM reserva_config WHERE id = 1").fetchone()

    conn.close()

    total_receita_ano = round(sum(receitas_mes.values()), 2)
    total_despesa_ano = round(sum(despesas_mes.values()), 2)
    saldo_ano = round(total_receita_ano - total_despesa_ano, 2)

    return {
        "ano": ano,
        "kpis": {
            "total_receita": total_receita_ano,
            "total_despesa": total_despesa_ano,
            "saldo_anual": saldo_ano,
            "pct_poupado_anual": round((saldo_ano / total_receita_ano) * 100, 2) if total_receita_ano > 0 else 0.0,
            "total_investido": round(tot_inv, 2),
            "total_dividas": round(tot_div, 2),
            "saldo_reserva": round(res_cfg["saldo_atual_reserva"], 2) if res_cfg else 0.0,
        },
        "series_mensais": {
            "receitas": receitas_mes,
            "despesas": despesas_mes,
            "saldo_mes": saldo_mes,
            "saldo_acumulado": saldo_acumulado,
            "pct_poupado": pct_poupado,
            "orcado": orcado_mes,
            "orcado_vs_real": orcado_vs_real,
        },
        "resumo_categorias": resumo_categorias,
    }


@app.get("/api/orcamento")
def obter_orcamento(ano: int = 2026):
    """Retorna a matriz de Orçamento vs Realizado por Macro-Categoria e Mês."""
    conn = get_db_connection()

    macros = [
        "Moradia", "Alimentação", "Transporte", "Saúde", "Educação",
        "Lazer", "Vestuário", "Pessoal", "Financeiro", "Poupança/Investimentos", "Outros"
    ]

    rows_orc = conn.execute("SELECT macro_categoria, mes, valor_orcado FROM orcamento WHERE ano = ?", (ano,)).fetchall()
    orc_map = {(r["macro_categoria"], r["mes"]): round(r["valor_orcado"], 2) for r in rows_orc}

    rows_real = conn.execute(
        """
        SELECT COALESCE(c.macro_categoria, 'Outros') as macro, l.mes, SUM(l.valor) as total
        FROM lancamentos l
        LEFT JOIN categorias c ON l.categoria = c.nome
        WHERE l.ano = ? AND l.tipo = 'Despesa'
        GROUP BY macro, l.mes
        """,
        (ano,),
    ).fetchall()
    real_map = {(r["macro"], r["mes"]): round(r["total"], 2) for r in rows_real}

    conn.close()

    resultado = []
    for mac in macros:
        orc_meses = {m: orc_map.get((mac, m), 0.0) for m in range(1, 13)}
        real_meses = {m: real_map.get((mac, m), 0.0) for m in range(1, 13)}
        dif_meses = {m: round(real_meses[m] - orc_meses[m], 2) for m in range(1, 13)}
        tot_orc = round(sum(orc_meses.values()), 2)
        tot_real = round(sum(real_meses.values()), 2)
        resultado.append({
            "macro_categoria": mac,
            "orcado": orc_meses,
            "realizado": real_meses,
            "diferenca": dif_meses,
            "total_orcado": tot_orc,
            "total_realizado": tot_real,
            "variacao_anual": round(tot_real - tot_orc, 2),
        })

    return resultado


@app.post("/api/orcamento")
def atualizar_orcamento(payload: OrcamentoInput):
    """Atualiza o valor orçado de uma macro-categoria em um mês específico."""
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO orcamento (macro_categoria, ano, mes, valor_orcado)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(macro_categoria, ano, mes) DO UPDATE SET valor_orcado = excluded.valor_orcado
        """,
        (payload.macro_categoria, payload.ano, payload.mes, round(payload.valor_orcado, 2)),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/planejamento")
def obter_planejamento(ano: int = 2026):
    """Retorna os dados consolidados de Investimentos, Dívidas, Metas e Reserva de Emergência."""
    conn = get_db_connection()
    inv = [dict(r) for r in conn.execute("SELECT * FROM investimentos ORDER BY id ASC").fetchall()]
    div = [dict(r) for r in conn.execute("SELECT * FROM dividas ORDER BY taxa_juros_mes DESC, id ASC").fetchall()]
    metas = [dict(r) for r in conn.execute("SELECT * FROM metas ORDER BY id ASC").fetchall()]
    res_cfg = dict(conn.execute("SELECT * FROM reserva_config WHERE id = 1").fetchone())

    # Calcula despesa mensal média automática dos meses com movimentação
    row_media = conn.execute(
        """
        SELECT AVG(total_mes) as media FROM (
            SELECT mes, SUM(valor) as total_mes
            FROM lancamentos
            WHERE ano = ? AND tipo = 'Despesa'
            GROUP BY mes
        )
        """,
        (ano,),
    ).fetchone()
    despesa_media_auto = round(row_media["media"] or 0.0, 2)
    despesa_base = despesa_media_auto if res_cfg["usar_media_automatica"] else res_cfg["despesa_media_manual"]
    meta_reserva = round(despesa_base * res_cfg["meses_seguranca"], 2)

    conn.close()
    return {
        "investimentos": inv,
        "dividas": div,
        "metas": metas,
        "reserva": {
            **res_cfg,
            "despesa_media_calculada": despesa_media_auto,
            "meta_total_reserva": meta_reserva,
            "progresso_pct": round((res_cfg["saldo_atual_reserva"] / meta_reserva) * 100, 1) if meta_reserva > 0 else 0.0,
        },
    }


@app.post("/api/investimentos")
def salvar_investimento(payload: InvestimentoUpdate):
    conn = get_db_connection()
    conn.execute(
        "UPDATE investimentos SET saldo_inicial = ?, saldo_atual = ?, rentabilidade_pct = ? WHERE id = ?",
        (payload.saldo_inicial, payload.saldo_atual, payload.rentabilidade_pct, payload.id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/dividas")
def salvar_divida(payload: DividaUpdate):
    conn = get_db_connection()
    conn.execute(
        "UPDATE dividas SET saldo_devedor = ?, taxa_juros_mes = ?, parcela_min = ?, parcelas_rest = ? WHERE id = ?",
        (payload.saldo_devedor, payload.taxa_juros_mes, payload.parcela_min, payload.parcelas_rest, payload.id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/metas")
def salvar_meta(payload: MetaUpdate):
    conn = get_db_connection()
    conn.execute(
        "UPDATE metas SET valor_alvo = ?, prazo = ?, valor_atual = ? WHERE id = ?",
        (payload.valor_alvo, payload.prazo, payload.valor_atual, payload.id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/reserva")
def salvar_reserva(payload: ReservaUpdate):
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE reserva_config
        SET despesa_media_manual = ?, usar_media_automatica = ?, meses_seguranca = ?, saldo_atual_reserva = ?
        WHERE id = 1
        """,
        (payload.despesa_media_manual, payload.usar_media_automatica, payload.meses_seguranca, payload.saldo_atual_reserva),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}
