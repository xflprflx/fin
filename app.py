"""
Servidor Backend de Controle Financeiro Pessoal (FastAPI + SQLite).
Fornece API RESTful para Lançamentos, Dashboard Anual, Orçamento vs Realizado,
Investimentos, Dívidas, Metas e Reserva de Emergência, com mecanismos de Poka-Yoke.
"""
import os
import sqlite3
import json
import calendar
import uuid
from datetime import datetime, date, timedelta
from typing import Optional, List
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


def init_db():
    """Inicializa as tabelas do banco de dados e popula categorias/configurações padrão caso estejam vazias."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS categorias (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE,
            tipo TEXT NOT NULL,
            macro_categoria TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lancamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            mes INTEGER NOT NULL,
            ano INTEGER NOT NULL,
            tipo TEXT NOT NULL,
            categoria TEXT NOT NULL,
            descricao TEXT,
            valor REAL NOT NULL,
            forma_pgto TEXT,
            observacao TEXT,
            competencia TEXT
        );

        CREATE TABLE IF NOT EXISTS recorrencias (
            id TEXT PRIMARY KEY,
            frequencia TEXT NOT NULL,
            modo TEXT NOT NULL,
            ocorrencias INTEGER NOT NULL,
            data_inicio TEXT NOT NULL,
            valor_informado REAL NOT NULL,
            criado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS cartoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL UNIQUE,
            dia_fechamento INTEGER NOT NULL,
            dia_vencimento INTEGER NOT NULL,
            limite REAL DEFAULT 0.0
        );

        CREATE TABLE IF NOT EXISTS faturas_pagas (
            cartao_id INTEGER NOT NULL,
            ano INTEGER NOT NULL,
            mes INTEGER NOT NULL,
            data_pagamento TEXT NOT NULL,
            PRIMARY KEY (cartao_id, ano, mes)
        );

        CREATE TABLE IF NOT EXISTS orcamento (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            macro_categoria TEXT NOT NULL,
            ano INTEGER NOT NULL,
            mes INTEGER NOT NULL,
            valor_orcado REAL DEFAULT 0.0,
            UNIQUE(macro_categoria, ano, mes)
        );

        CREATE TABLE IF NOT EXISTS investimentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ativo TEXT NOT NULL,
            classe TEXT NOT NULL,
            saldo_inicial REAL DEFAULT 0.0,
            saldo_atual REAL DEFAULT 0.0,
            rentabilidade_pct REAL DEFAULT 0.0
        );

        CREATE TABLE IF NOT EXISTS dividas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            credor TEXT NOT NULL,
            tipo TEXT NOT NULL,
            saldo_devedor REAL DEFAULT 0.0,
            taxa_juros_mes REAL DEFAULT 0.0,
            parcela_min REAL DEFAULT 0.0,
            parcelas_rest INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS metas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            prazo TEXT NOT NULL,
            valor_alvo REAL DEFAULT 0.0,
            valor_atual REAL DEFAULT 0.0
        );

        CREATE TABLE IF NOT EXISTS reserva_config (
            id INTEGER PRIMARY KEY,
            despesa_media_manual REAL DEFAULT 0.0,
            usar_media_automatica INTEGER DEFAULT 1,
            meses_seguranca INTEGER DEFAULT 6,
            saldo_atual_reserva REAL DEFAULT 0.0
        );
    """)

    # Migração: colunas de recorrência em lançamentos (bancos antigos)
    colunas_lanc = {r[1] for r in cur.execute("PRAGMA table_info(lancamentos)").fetchall()}
    for col, ddl in (
        ("recorrencia_id", "TEXT"),
        ("parcela_num", "INTEGER"),
        ("parcela_total", "INTEGER"),
        ("cartao_id", "INTEGER"),
    ):
        if col not in colunas_lanc:
            cur.execute(f"ALTER TABLE lancamentos ADD COLUMN {col} {ddl}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lanc_recorrencia ON lancamentos(recorrencia_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_lanc_cartao ON lancamentos(cartao_id, ano, mes)")

    # Garante configuração inicial da reserva de emergência
    row_res = cur.execute("SELECT id FROM reserva_config WHERE id = 1").fetchone()
    if not row_res:
        cur.execute(
            "INSERT INTO reserva_config (id, despesa_media_manual, usar_media_automatica, meses_seguranca, saldo_atual_reserva) VALUES (1, 0.0, 1, 6, 0.0)"
        )

    # Popula categorias padrão se tabela vazia
    qtd_cat = cur.execute("SELECT COUNT(*) FROM categorias").fetchone()[0]
    if qtd_cat == 0:
        categorias_padrao = [
            # Despesas
            ("Aluguel / Condomínio", "Despesa", "Moradia"),
            ("Energia Elétrica", "Despesa", "Moradia"),
            ("Água e Saneamento", "Despesa", "Moradia"),
            ("Internet / TV", "Despesa", "Moradia"),
            ("Manutenção da Casa", "Despesa", "Moradia"),
            ("Supermercado", "Despesa", "Alimentação"),
            ("Feira / Hortifrúti", "Despesa", "Alimentação"),
            ("Restaurantes / Bares", "Despesa", "Alimentação"),
            ("Delivery / Lanches", "Despesa", "Alimentação"),
            ("Combustível", "Despesa", "Transporte"),
            ("Transporte Público / Uber", "Despesa", "Transporte"),
            ("Manutenção Veículo", "Despesa", "Transporte"),
            ("Seguro / IPVA", "Despesa", "Transporte"),
            ("Plano de Saúde", "Despesa", "Saúde"),
            ("Farmácia / Medicamentos", "Despesa", "Saúde"),
            ("Consultas e Exames", "Despesa", "Saúde"),
            ("Escola / Faculdade", "Despesa", "Educação"),
            ("Cursos / Treinamentos", "Despesa", "Educação"),
            ("Livros e Material", "Despesa", "Educação"),
            ("Viagens / Turismo", "Despesa", "Lazer"),
            ("Streaming / Assinaturas", "Despesa", "Lazer"),
            ("Cinema / Eventos", "Despesa", "Lazer"),
            ("Roupas e Calçados", "Despesa", "Vestuário"),
            ("Acessórios e Beleza", "Despesa", "Vestuário"),
            ("Cabeleireiro / Barbeiro", "Despesa", "Pessoal"),
            ("Academia / Esportes", "Despesa", "Pessoal"),
            ("Tarifas e Anuidade", "Despesa", "Financeiro"),
            ("Juros e Empréstimos", "Despesa", "Financeiro"),
            ("Aportes e Investimentos", "Despesa", "Poupança/Investimentos"),
            ("Doações e Presentes", "Despesa", "Outros"),
            ("Outros Gastos", "Despesa", "Outros"),
            # Receitas
            ("Salário Líquido", "Receita", "Renda Principal"),
            ("Pró-labore", "Receita", "Renda Principal"),
            ("13º Salário", "Receita", "Renda Principal"),
            ("Férias", "Receita", "Renda Principal"),
            ("Freelance / Bicos", "Receita", "Renda Extra"),
            ("Vendas / Comissões", "Receita", "Renda Extra"),
            ("Rendimentos Investimentos", "Receita", "Investimentos"),
            ("Aluguéis Recebidos", "Receita", "Investimentos"),
            ("Restituição IR / Reembolso", "Receita", "Outras Receitas"),
            ("Outras Entradas", "Receita", "Outras Receitas"),
        ]
        cur.executemany("INSERT INTO categorias (nome, tipo, macro_categoria) VALUES (?, ?, ?)", categorias_padrao)

    # Popula investimentos padrão se vazio
    qtd_inv = cur.execute("SELECT COUNT(*) FROM investimentos").fetchone()[0]
    if qtd_inv == 0:
        inv_padrao = [
            ("Tesouro Selic 2029", "Renda Fixa", 15000.0, 15650.0, 4.33),
            ("CDB 110% CDI Liquidez Diária", "Renda Fixa", 10000.0, 10420.0, 4.20),
            ("Fundo Imobiliário KNCR11", "FIIs", 8000.0, 8350.0, 4.38),
        ]
        cur.executemany("INSERT INTO investimentos (ativo, classe, saldo_inicial, saldo_atual, rentabilidade_pct) VALUES (?, ?, ?, ?, ?)", inv_padrao)

    # Popula dividas padrão se vazio
    qtd_div = cur.execute("SELECT COUNT(*) FROM dividas").fetchone()[0]
    if qtd_div == 0:
        div_padrao = [
            ("Cartão de Crédito", "Rotativo", 1200.0, 12.5, 600.0, 2),
            ("Financiamento Veicular", "CDC", 18500.0, 1.89, 780.0, 28),
        ]
        cur.executemany("INSERT INTO dividas (credor, tipo, saldo_devedor, taxa_juros_mes, parcela_min, parcelas_rest) VALUES (?, ?, ?, ?, ?, ?)", div_padrao)

    # Popula metas padrão se vazio
    qtd_metas = cur.execute("SELECT COUNT(*) FROM metas").fetchone()[0]
    if qtd_metas == 0:
        metas_padrao = [
            ("Viagem de Férias", "Dezembro/2026", 8000.0, 2500.0),
            ("Troca de Carro", "Julho/2027", 35000.0, 10000.0),
            ("Pós-Graduação", "Janeiro/2027", 12000.0, 4000.0),
        ]
        cur.executemany("INSERT INTO metas (nome, prazo, valor_alvo, valor_atual) VALUES (?, ?, ?, ?)", metas_padrao)

    conn.commit()
    conn.close()


# Inicializa o banco de dados na inicialização
init_db()


# ==========================================
# Modelos Pydantic (Validação Poka-Yoke)
# ==========================================
class CategoriaInput(BaseModel):
    nome: str = Field(..., min_length=1, description="Nome da categoria")
    tipo: str = Field(..., pattern="^(Receita|Despesa)$")
    macro_categoria: str = Field(default="Outros")


class CategoriaUpdateInput(BaseModel):
    nome: str = Field(..., min_length=1, description="Nome da categoria")
    tipo: str = Field(..., pattern="^(Receita|Despesa)$")
    macro_categoria: str = Field(default="Outros")


FREQUENCIAS = {
    # nome: (tipo de passo, quantidade)
    "semanal": ("dias", 7),
    "quinzenal": ("dias", 14),
    "mensal": ("meses", 1),
    "bimestral": ("meses", 2),
    "trimestral": ("meses", 3),
    "semestral": ("meses", 6),
    "anual": ("meses", 12),
}
MAX_OCORRENCIAS = 120


class RecorrenciaInput(BaseModel):
    frequencia: str = Field(..., pattern="^(semanal|quinzenal|mensal|bimestral|trimestral|semestral|anual)$")
    ocorrencias: int = Field(..., ge=2, le=MAX_OCORRENCIAS, description="Quantidade total de lançamentos gerados")
    modo: str = Field(
        default="repetir",
        pattern="^(repetir|parcelar)$",
        description="repetir = mesmo valor em cada ocorrência; parcelar = valor total dividido entre as parcelas",
    )


class LancamentoInput(BaseModel):
    data: str = Field(..., description="Data no formato YYYY-MM-DD")
    tipo: str = Field(..., pattern="^(Receita|Despesa)$")
    categoria: str
    descricao: Optional[str] = ""
    valor: float = Field(..., gt=0, description="O valor deve ser estritamente positivo")
    forma_pgto: str = "PIX"
    observacao: Optional[str] = ""
    recorrencia: Optional[RecorrenciaInput] = None
    cartao_id: Optional[int] = Field(
        default=None,
        description="Compra no cartão de crédito: o lançamento entra na fatura calculada pelo fechamento do cartão",
    )


class CartaoInput(BaseModel):
    nome: str = Field(..., min_length=1)
    dia_fechamento: int = Field(..., ge=1, le=31)
    dia_vencimento: int = Field(..., ge=1, le=31)
    limite: float = Field(default=0.0, ge=0)


class PagamentoFaturaInput(BaseModel):
    data_pagamento: Optional[str] = None


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
# Utilitários de Recorrência
# ==========================================
def somar_meses(dt: date, meses: int) -> date:
    """Soma meses preservando o dia; se o mês destino for mais curto, usa o último dia (ex.: 31/01 -> 28/02)."""
    total = dt.month - 1 + meses
    ano = dt.year + total // 12
    mes = total % 12 + 1
    dia = min(dt.day, calendar.monthrange(ano, mes)[1])
    return date(ano, mes, dia)


def gerar_datas(inicio: date, frequencia: str, ocorrencias: int) -> List[date]:
    """Gera as datas de cada ocorrência sempre a partir da data inicial (evita 'deriva' de dias)."""
    passo, qtd = FREQUENCIAS[frequencia]
    if passo == "dias":
        return [inicio + timedelta(days=qtd * i) for i in range(ocorrencias)]
    return [somar_meses(inicio, qtd * i) for i in range(ocorrencias)]


MESES_ABREV = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def janela_meses(ano: int, mes_inicio: int = 1, qtd: int = 12) -> List[dict]:
    """
    Monta uma linha do tempo de `qtd` meses começando em mes_inicio/ano, podendo atravessar a virada
    de ano (ex.: Nov/2026 → Out/2027). Cada item traz a posição (1..qtd), ano, mês e rótulo.
    """
    if not 1 <= mes_inicio <= 12:
        raise HTTPException(status_code=400, detail="mes_inicio deve estar entre 1 e 12.")
    periodos = []
    for i in range(qtd):
        total = mes_inicio - 1 + i
        a, m = ano + total // 12, total % 12 + 1
        periodos.append({"pos": i + 1, "ano": a, "mes": m, "label": f"{MESES_ABREV[m - 1]}/{str(a)[2:]}"})
    return periodos


def chave_periodo(ano: int, mes: int) -> int:
    """Chave numérica comparável AAAAMM."""
    return ano * 100 + mes


def filtro_janela(periodos: List[dict], prefixo: str = "") -> tuple:
    """Retorna (sql, params) que restringe a consulta ao intervalo da janela."""
    ini, fim = periodos[0], periodos[-1]
    return (
        f"({prefixo}ano * 100 + {prefixo}mes) BETWEEN ? AND ?",
        [chave_periodo(ini["ano"], ini["mes"]), chave_periodo(fim["ano"], fim["mes"])],
    )


def mapa_posicoes(periodos: List[dict]) -> dict:
    """(ano, mes) -> posição na janela."""
    return {(p["ano"], p["mes"]): p["pos"] for p in periodos}


def dividir_valor(total: float, partes: int) -> List[float]:
    """Divide um valor em centavos exatos; a diferença de arredondamento vai para a primeira parcela."""
    centavos = int(round(total * 100))
    base, resto = divmod(centavos, partes)
    valores = [base] * partes
    valores[0] += resto
    return [v / 100 for v in valores]


# ==========================================
# Utilitários de Cartão de Crédito / Fatura
# ==========================================
def dia_no_mes(ano: int, mes: int, dia: int) -> date:
    """Data com o dia limitado ao tamanho do mês (dia 31 em fevereiro vira 28/29)."""
    return date(ano, mes, min(dia, calendar.monthrange(ano, mes)[1]))


def mover_mes(ano: int, mes: int, delta: int) -> tuple:
    t = ano * 12 + (mes - 1) + delta
    return t // 12, t % 12 + 1


def datas_da_fatura(cartao, ano: int, mes: int) -> tuple:
    """
    Fatura identificada pelo mês de VENCIMENTO (ano/mes).
    Se o fechamento é antes do vencimento no calendário (ex.: fecha 3, vence 10), fecha no mesmo mês;
    caso contrário (ex.: fecha 28, vence 5), fecha no mês anterior.
    """
    venc = dia_no_mes(ano, mes, cartao["dia_vencimento"])
    fa, fm = (ano, mes) if cartao["dia_fechamento"] < cartao["dia_vencimento"] else mover_mes(ano, mes, -1)
    fech = dia_no_mes(fa, fm, cartao["dia_fechamento"])
    return fech, venc


def fatura_da_compra(cartao, d: date) -> tuple:
    """Primeira fatura cujo fechamento é DEPOIS da compra (compra no dia do fechamento vai para a próxima)."""
    for delta in range(-1, 3):
        a, m = mover_mes(d.year, d.month, delta)
        fech, _ = datas_da_fatura(cartao, a, m)
        if d < fech:
            return a, m
    return mover_mes(d.year, d.month, 3)  # inalcançável na prática


def status_da_fatura(cartao, ano: int, mes: int, paga: bool, hoje: Optional[date] = None) -> str:
    hoje = hoje or date.today()
    fech, venc = datas_da_fatura(cartao, ano, mes)
    if paga:
        return "paga"
    if hoje < fech:
        return "aberta"
    if hoje <= venc:
        return "fechada"
    return "vencida"


def info_fatura(cartao, ano: int, mes: int, total: float, paga_em: Optional[str]) -> dict:
    fech, venc = datas_da_fatura(cartao, ano, mes)
    return {
        "cartao_id": cartao["id"],
        "cartao": cartao["nome"],
        "ano": ano,
        "mes": mes,
        "label": f"{MESES_ABREV[mes - 1]}/{str(ano)[2:]}",
        "fechamento": fech.isoformat(),
        "vencimento": venc.isoformat(),
        "total": round(total, 2),
        "paga": paga_em is not None,
        "data_pagamento": paga_em,
        "status": status_da_fatura(cartao, ano, mes, paga_em is not None),
    }


def obter_cartao(conn, cartao_id: int):
    cartao = conn.execute("SELECT * FROM cartoes WHERE id = ?", (cartao_id,)).fetchone()
    if not cartao:
        conn.close()
        raise HTTPException(status_code=404, detail="Cartão não encontrado.")
    return cartao


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


@app.get("/api/macro-categorias")
def listar_macro_categorias():
    """Retorna as macro-categorias padrão organizadas por tipo para filtros e formulários."""
    return {
        "Despesa": [
            "Moradia",
            "Alimentação",
            "Transporte",
            "Saúde",
            "Educação",
            "Lazer",
            "Vestuário",
            "Pessoal",
            "Financeiro",
            "Poupança/Investimentos",
            "Outros",
        ],
        "Receita": [
            "Renda Principal",
            "Renda Extra",
            "Investimentos",
            "Benefícios",
            "Outras Receitas",
            "Outros",
        ],
    }


@app.get("/api/categorias")
def listar_categorias():
    """Retorna todas as categorias com contagem de lançamentos associados (Poka-Yoke de integridade)."""
    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT c.id, c.nome, c.tipo, c.macro_categoria,
               COUNT(l.id) as total_lancamentos
        FROM categorias c
        LEFT JOIN lancamentos l ON c.nome = l.categoria
        GROUP BY c.id, c.nome, c.tipo, c.macro_categoria
        ORDER BY c.tipo DESC, c.macro_categoria ASC, c.nome ASC
        """
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/categorias")
def criar_categoria(payload: CategoriaInput):
    """
    Cadastra uma nova categoria no sistema.
    Aplica validação Poka-Yoke para impedir nomes vazios e duplicados (case-insensitive).
    """
    nome_limpo = payload.nome.strip()
    if not nome_limpo:
        raise HTTPException(status_code=400, detail="O nome da categoria não pode estar em branco.")

    macro_limpa = payload.macro_categoria.strip() or "Outros"

    conn = get_db_connection()
    cur = conn.cursor()
    existe = cur.execute("SELECT id FROM categorias WHERE LOWER(nome) = LOWER(?)", (nome_limpo,)).fetchone()
    if existe:
        conn.close()
        raise HTTPException(status_code=400, detail=f"A categoria '{nome_limpo}' já está cadastrada.")

    cur.execute(
        "INSERT INTO categorias (nome, tipo, macro_categoria) VALUES (?, ?, ?)",
        (nome_limpo, payload.tipo, macro_limpa),
    )
    conn.commit()
    novo_id = cur.lastrowid
    conn.close()
    return {"status": "ok", "id": novo_id, "nome": nome_limpo, "tipo": payload.tipo, "macro_categoria": macro_limpa}


@app.put("/api/categorias/{cat_id}")
def atualizar_categoria(cat_id: int, payload: CategoriaUpdateInput):
    """
    Atualiza uma categoria existente.
    Se o nome for alterado, sincroniza automaticamente todos os lançamentos históricos associados (Poka-Yoke).
    """
    nome_limpo = payload.nome.strip()
    if not nome_limpo:
        raise HTTPException(status_code=400, detail="O nome da categoria não pode estar em branco.")

    macro_limpa = payload.macro_categoria.strip() or "Outros"

    conn = get_db_connection()
    cur = conn.cursor()
    cat = cur.execute("SELECT * FROM categorias WHERE id = ?", (cat_id,)).fetchone()
    if not cat:
        conn.close()
        raise HTTPException(status_code=404, detail="Categoria não encontrada.")

    # Verifica se já existe outra categoria com esse mesmo nome
    existe = cur.execute(
        "SELECT id FROM categorias WHERE LOWER(nome) = LOWER(?) AND id != ?",
        (nome_limpo, cat_id),
    ).fetchone()
    if existe:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Já existe outra categoria chamada '{nome_limpo}'.")

    nome_antigo = cat["nome"]
    cur.execute(
        "UPDATE categorias SET nome = ?, tipo = ?, macro_categoria = ? WHERE id = ?",
        (nome_limpo, payload.tipo, macro_limpa, cat_id),
    )

    # Se o nome ou tipo mudou, atualiza lançamentos históricos
    if nome_antigo != nome_limpo or cat["tipo"] != payload.tipo:
        cur.execute(
            "UPDATE lancamentos SET categoria = ?, tipo = ? WHERE categoria = ?",
            (nome_limpo, payload.tipo, nome_antigo),
        )

    conn.commit()
    conn.close()
    return {"status": "ok", "id": cat_id, "nome": nome_limpo, "tipo": payload.tipo, "macro_categoria": macro_limpa}


@app.delete("/api/categorias/{cat_id}")
def excluir_categoria(cat_id: int, forcar: bool = False):
    """
    Exclui uma categoria.
    Caso existam lançamentos vinculados, exige parâmetro de confirmação explícita (forcar=true).
    """
    conn = get_db_connection()
    cur = conn.cursor()
    cat = cur.execute("SELECT * FROM categorias WHERE id = ?", (cat_id,)).fetchone()
    if not cat:
        conn.close()
        raise HTTPException(status_code=404, detail="Categoria não encontrada.")

    qtd_lanc = cur.execute("SELECT COUNT(*) FROM lancamentos WHERE categoria = ?", (cat["nome"],)).fetchone()[0]
    if qtd_lanc > 0 and not forcar:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Não é possível excluir: existem {qtd_lanc} lançamento(s) vinculados a esta categoria. Confirme para forçar a exclusão ou reatribua os lançamentos antes.",
        )

    cur.execute("DELETE FROM categorias WHERE id = ?", (cat_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": cat_id, "nome": cat["nome"]}


@app.get("/api/lancamentos")
def listar_lancamentos(
    ano: int = 2026,
    mes_inicio: int = 1,
    periodo: Optional[str] = None,
    mes: Optional[int] = None,
    tipo: Optional[str] = None,
    busca: Optional[str] = None,
    recorrencia: Optional[str] = None,
    cartao_id: Optional[int] = None,
):
    """
    Lista os lançamentos com suporte a filtros combinados de período, tipo, termo de busca
    e recorrência (recorrencia=unico | recorrente).

    Período: por padrão, a janela de 12 meses iniciada em mes_inicio/ano.
    `periodo=AAAA-MM` restringe a um único mês; `mes` (legado) filtra um mês do `ano`.
    """
    conn = get_db_connection()
    if periodo:
        try:
            p_ano, p_mes = (int(x) for x in periodo.split("-"))
        except ValueError:
            conn.close()
            raise HTTPException(status_code=400, detail="periodo deve estar no formato AAAA-MM.")
        query = "SELECT * FROM lancamentos WHERE ano = ? AND mes = ?"
        params = [p_ano, p_mes]
    elif mes:
        query = "SELECT * FROM lancamentos WHERE ano = ? AND mes = ?"
        params = [ano, mes]
    else:
        sql_j, params = filtro_janela(janela_meses(ano, mes_inicio))
        query = f"SELECT * FROM lancamentos WHERE {sql_j}"
    query = query.replace(
        "SELECT * FROM lancamentos WHERE",
        "SELECT l.*, k.nome AS cartao_nome FROM lancamentos l LEFT JOIN cartoes k ON k.id = l.cartao_id WHERE",
    )
    if tipo and tipo in ("Receita", "Despesa"):
        query += " AND tipo = ?"
        params.append(tipo)
    if busca:
        query += " AND (descricao LIKE ? OR categoria LIKE ? OR observacao LIKE ? OR k.nome LIKE ?)"
        termo = f"%{busca}%"
        params.extend([termo, termo, termo, termo])
    if recorrencia == "unico":
        query += " AND recorrencia_id IS NULL"
    elif recorrencia == "recorrente":
        query += " AND recorrencia_id IS NOT NULL"
    if cartao_id:
        query += " AND l.cartao_id = ?"
        params.append(cartao_id)

    query += " ORDER BY l.data DESC, l.id DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/lancamentos")
def criar_lancamento(payload: LancamentoInput):
    """
    Insere um novo lançamento validando coerência de data e derivando automaticamente
    os campos de mês, ano e competência (Poka-Yoke contra erro de digitação de competência).

    Se `recorrencia` for informada, gera todas as ocorrências da série de uma vez,
    vinculadas por `recorrencia_id` e numeradas (parcela_num / parcela_total).
    """
    try:
        dt = datetime.strptime(payload.data, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida. Use YYYY-MM-DD.")

    descricao = (payload.descricao or "").strip()
    observacao = (payload.observacao or "").strip()
    valor = round(payload.valor, 2)
    rec = payload.recorrencia
    forma_pgto = payload.forma_pgto

    cartao = None
    if payload.cartao_id is not None:
        conn_c = get_db_connection()
        cartao = obter_cartao(conn_c, payload.cartao_id)
        conn_c.close()
        if payload.tipo != "Despesa":
            raise HTTPException(status_code=400, detail="Compras no cartão devem ser do tipo Despesa.")
        forma_pgto = "Crédito"

    if rec is None:
        datas = [dt]
        valores = [valor]
        recorrencia_id = None
    else:
        datas = gerar_datas(dt, rec.frequencia, rec.ocorrencias)
        if rec.modo == "parcelar":
            if valor < 0.01 * rec.ocorrencias:
                raise HTTPException(status_code=400, detail="Valor total muito baixo para a quantidade de parcelas.")
            valores = dividir_valor(valor, rec.ocorrencias)
        else:
            valores = [valor] * rec.ocorrencias
        recorrencia_id = uuid.uuid4().hex

    # Mês contábil de cada ocorrência: data da compra, ou mês da fatura quando é compra no cartão.
    if cartao is None:
        periodos_contabeis = [(d.year, d.month) for d in datas]
    elif rec is not None and rec.modo == "parcelar":
        # Parcelas no cartão: mesma data de compra, uma parcela em cada fatura seguinte
        base = fatura_da_compra(cartao, dt)
        periodos_contabeis = [mover_mes(base[0], base[1], i) for i in range(len(datas))]
        datas = [dt] * len(datas)
    else:
        periodos_contabeis = [fatura_da_compra(cartao, d) for d in datas]

    conn = get_db_connection()
    cur = conn.cursor()
    ids = []
    try:
        if recorrencia_id:
            cur.execute(
                """
                INSERT INTO recorrencias (id, frequencia, modo, ocorrencias, data_inicio, valor_informado, criado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (recorrencia_id, rec.frequencia, rec.modo, rec.ocorrencias, payload.data, valor,
                 datetime.now().isoformat(timespec="seconds")),
            )
        total = len(datas)
        for i, (d, v, (p_ano, p_mes)) in enumerate(zip(datas, valores, periodos_contabeis), start=1):
            cur.execute(
                """
                INSERT INTO lancamentos (data, mes, ano, tipo, categoria, descricao, valor, forma_pgto, observacao,
                                         competencia, recorrencia_id, parcela_num, parcela_total, cartao_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    d.isoformat(),
                    p_mes,
                    p_ano,
                    payload.tipo,
                    payload.categoria,
                    descricao,
                    v,
                    forma_pgto,
                    observacao,
                    f"{p_mes:02d}/{p_ano}",
                    recorrencia_id,
                    i if recorrencia_id else None,
                    total if recorrencia_id else None,
                    payload.cartao_id,
                ),
            )
            ids.append(cur.lastrowid)
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "id": ids[0],
        "ids": ids,
        "recorrencia_id": recorrencia_id,
        "quantidade": len(ids),
        "primeira_data": datas[0].isoformat(),
        "ultima_data": datas[-1].isoformat(),
        "primeira_competencia": "%04d-%02d" % periodos_contabeis[0],
        "ultima_competencia": "%04d-%02d" % periodos_contabeis[-1],
    }


@app.delete("/api/lancamentos/{lanc_id}")
def excluir_lancamento(lanc_id: int, escopo: str = "unico"):
    """
    Remove um lançamento pelo ID.
    Para lançamentos recorrentes, `escopo` define o alcance:
      - unico: apenas este lançamento
      - futuros: este e os seguintes da mesma série
      - todos: a série inteira
    """
    if escopo not in ("unico", "futuros", "todos"):
        raise HTTPException(status_code=400, detail="Escopo inválido. Use unico, futuros ou todos.")

    conn = get_db_connection()
    cur = conn.cursor()
    lanc = cur.execute("SELECT * FROM lancamentos WHERE id = ?", (lanc_id,)).fetchone()
    if not lanc:
        conn.close()
        raise HTTPException(status_code=404, detail="Lançamento não encontrado.")

    rid = lanc["recorrencia_id"]
    if not rid or escopo == "unico":
        cur.execute("DELETE FROM lancamentos WHERE id = ?", (lanc_id,))
    elif escopo == "futuros":
        cur.execute(
            "DELETE FROM lancamentos WHERE recorrencia_id = ? AND parcela_num >= ?",
            (rid, lanc["parcela_num"]),
        )
    else:
        cur.execute("DELETE FROM lancamentos WHERE recorrencia_id = ?", (rid,))
    removidos = cur.rowcount

    # Remove metadados da série se não sobrou nenhum lançamento
    if rid:
        restantes = cur.execute("SELECT COUNT(*) FROM lancamentos WHERE recorrencia_id = ?", (rid,)).fetchone()[0]
        if restantes == 0:
            cur.execute("DELETE FROM recorrencias WHERE id = ?", (rid,))

    conn.commit()
    conn.close()
    return {"status": "deleted", "id": lanc_id, "escopo": escopo, "removidos": removidos}


# ==========================================
# Cartões de Crédito e Faturas
# ==========================================
def _validar_cartao(payload: CartaoInput) -> str:
    nome = payload.nome.strip()
    if not nome:
        raise HTTPException(status_code=400, detail="O nome do cartão não pode estar em branco.")
    if payload.dia_fechamento == payload.dia_vencimento:
        raise HTTPException(status_code=400, detail="O dia de fechamento deve ser diferente do dia de vencimento.")
    return nome


def _total_fatura(conn, cartao_id: int, ano: int, mes: int) -> float:
    return conn.execute(
        "SELECT COALESCE(SUM(valor), 0) FROM lancamentos WHERE cartao_id = ? AND ano = ? AND mes = ?",
        (cartao_id, ano, mes),
    ).fetchone()[0]


def _paga_em(conn, cartao_id: int, ano: int, mes: int) -> Optional[str]:
    row = conn.execute(
        "SELECT data_pagamento FROM faturas_pagas WHERE cartao_id = ? AND ano = ? AND mes = ?",
        (cartao_id, ano, mes),
    ).fetchone()
    return row["data_pagamento"] if row else None


@app.get("/api/cartoes")
def listar_cartoes():
    """
    Lista os cartões com a fatura atual (a que recebe compras de hoje), limite usado
    (soma de todas as faturas não pagas, incluindo parcelas futuras) e limite disponível.
    """
    conn = get_db_connection()
    cartoes = conn.execute("SELECT * FROM cartoes ORDER BY nome COLLATE NOCASE").fetchall()
    hoje = date.today()
    resultado = []
    for c in cartoes:
        usado = conn.execute(
            """
            SELECT COALESCE(SUM(l.valor), 0) FROM lancamentos l
            LEFT JOIN faturas_pagas f ON f.cartao_id = l.cartao_id AND f.ano = l.ano AND f.mes = l.mes
            WHERE l.cartao_id = ? AND f.cartao_id IS NULL
            """,
            (c["id"],),
        ).fetchone()[0]
        qtd = conn.execute("SELECT COUNT(*) FROM lancamentos WHERE cartao_id = ?", (c["id"],)).fetchone()[0]
        a, m = fatura_da_compra(c, hoje)
        atual = info_fatura(c, a, m, _total_fatura(conn, c["id"], a, m), _paga_em(conn, c["id"], a, m))
        resultado.append({
            **dict(c),
            "limite_usado": round(usado, 2),
            "limite_disponivel": round((c["limite"] or 0) - usado, 2),
            "total_lancamentos": qtd,
            "fatura_atual": atual,
        })
    conn.close()
    return resultado


@app.post("/api/cartoes")
def criar_cartao(payload: CartaoInput):
    nome = _validar_cartao(payload)
    conn = get_db_connection()
    if conn.execute("SELECT id FROM cartoes WHERE LOWER(nome) = LOWER(?)", (nome,)).fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"O cartão '{nome}' já está cadastrado.")
    cur = conn.execute(
        "INSERT INTO cartoes (nome, dia_fechamento, dia_vencimento, limite) VALUES (?, ?, ?, ?)",
        (nome, payload.dia_fechamento, payload.dia_vencimento, round(payload.limite, 2)),
    )
    conn.commit()
    novo_id = cur.lastrowid
    conn.close()
    return {"status": "ok", "id": novo_id}


@app.put("/api/cartoes/{cartao_id}")
def atualizar_cartao(cartao_id: int, payload: CartaoInput):
    """
    Atualiza o cartão. Mudar fechamento/vencimento vale para compras futuras;
    compras já lançadas permanecem nas faturas em que foram registradas.
    """
    nome = _validar_cartao(payload)
    conn = get_db_connection()
    obter_cartao(conn, cartao_id)
    if conn.execute("SELECT id FROM cartoes WHERE LOWER(nome) = LOWER(?) AND id != ?", (nome, cartao_id)).fetchone():
        conn.close()
        raise HTTPException(status_code=400, detail=f"Já existe outro cartão chamado '{nome}'.")
    conn.execute(
        "UPDATE cartoes SET nome = ?, dia_fechamento = ?, dia_vencimento = ?, limite = ? WHERE id = ?",
        (nome, payload.dia_fechamento, payload.dia_vencimento, round(payload.limite, 2), cartao_id),
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "id": cartao_id}


@app.delete("/api/cartoes/{cartao_id}")
def excluir_cartao(cartao_id: int):
    """Exclui um cartão. Bloqueado se houver compras lançadas nele (Poka-Yoke)."""
    conn = get_db_connection()
    cartao = obter_cartao(conn, cartao_id)
    qtd = conn.execute("SELECT COUNT(*) FROM lancamentos WHERE cartao_id = ?", (cartao_id,)).fetchone()[0]
    if qtd > 0:
        conn.close()
        raise HTTPException(
            status_code=400,
            detail=f"Não é possível excluir: o cartão '{cartao['nome']}' tem {qtd} compra(s) lançada(s). Exclua as compras antes.",
        )
    conn.execute("DELETE FROM faturas_pagas WHERE cartao_id = ?", (cartao_id,))
    conn.execute("DELETE FROM cartoes WHERE id = ?", (cartao_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": cartao_id}


@app.get("/api/cartoes/{cartao_id}/fatura-da-compra")
def consultar_fatura_da_compra(cartao_id: int, data: str):
    """Informa em qual fatura uma compra feita em `data` vai cair (usado na prévia do formulário)."""
    try:
        d = datetime.strptime(data, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida. Use YYYY-MM-DD.")
    conn = get_db_connection()
    cartao = obter_cartao(conn, cartao_id)
    a, m = fatura_da_compra(cartao, d)
    info = info_fatura(cartao, a, m, _total_fatura(conn, cartao_id, a, m), _paga_em(conn, cartao_id, a, m))
    conn.close()
    return info


@app.get("/api/cartoes/{cartao_id}/faturas/{ano}/{mes}")
def obter_fatura(cartao_id: int, ano: int, mes: int):
    """Detalha uma fatura (identificada pelo mês de vencimento): datas, status, total e itens."""
    if not 1 <= mes <= 12:
        raise HTTPException(status_code=400, detail="Mês inválido.")
    conn = get_db_connection()
    cartao = obter_cartao(conn, cartao_id)
    itens = [dict(r) for r in conn.execute(
        "SELECT * FROM lancamentos WHERE cartao_id = ? AND ano = ? AND mes = ? ORDER BY data ASC, id ASC",
        (cartao_id, ano, mes),
    ).fetchall()]
    total = sum(i["valor"] for i in itens)
    info = info_fatura(cartao, ano, mes, total, _paga_em(conn, cartao_id, ano, mes))
    conn.close()

    por_categoria = {}
    for i in itens:
        por_categoria[i["categoria"]] = round(por_categoria.get(i["categoria"], 0) + i["valor"], 2)
    return {
        **info,
        "itens": itens,
        "quantidade": len(itens),
        "por_categoria": sorted(
            ({"categoria": k, "total": v} for k, v in por_categoria.items()), key=lambda x: -x["total"]
        ),
    }


@app.post("/api/cartoes/{cartao_id}/faturas/{ano}/{mes}/pagar")
def pagar_fatura(cartao_id: int, ano: int, mes: int, payload: Optional[PagamentoFaturaInput] = None):
    """
    Marca a fatura como paga. Não cria lançamento de despesa: as compras já contam
    como despesa no mês da fatura (evita contar em dobro).
    """
    data_pag = (payload.data_pagamento if payload else None) or date.today().isoformat()
    try:
        datetime.strptime(data_pag, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Data de pagamento inválida. Use YYYY-MM-DD.")
    conn = get_db_connection()
    obter_cartao(conn, cartao_id)
    conn.execute(
        """
        INSERT INTO faturas_pagas (cartao_id, ano, mes, data_pagamento) VALUES (?, ?, ?, ?)
        ON CONFLICT(cartao_id, ano, mes) DO UPDATE SET data_pagamento = excluded.data_pagamento
        """,
        (cartao_id, ano, mes, data_pag),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/cartoes/{cartao_id}/faturas/{ano}/{mes}/pagar")
def reabrir_fatura(cartao_id: int, ano: int, mes: int):
    """Desfaz a marcação de pagamento da fatura."""
    conn = get_db_connection()
    conn.execute("DELETE FROM faturas_pagas WHERE cartao_id = ? AND ano = ? AND mes = ?", (cartao_id, ano, mes))
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/faturas")
def resumo_faturas(ano: int = 2026, mes_inicio: int = 1):
    """Linha do tempo das faturas: total e status de cada cartão em cada mês da janela de 12 meses."""
    periodos = janela_meses(ano, mes_inicio)
    sql_j, params_j = filtro_janela(periodos)
    conn = get_db_connection()
    cartoes = conn.execute("SELECT * FROM cartoes ORDER BY nome COLLATE NOCASE").fetchall()
    totais = {
        (r["cartao_id"], r["ano"], r["mes"]): r["total"]
        for r in conn.execute(
            f"""
            SELECT cartao_id, ano, mes, SUM(valor) AS total FROM lancamentos
            WHERE cartao_id IS NOT NULL AND {sql_j}
            GROUP BY cartao_id, ano, mes
            """,
            params_j,
        ).fetchall()
    }
    pagas = {
        (r["cartao_id"], r["ano"], r["mes"]): r["data_pagamento"]
        for r in conn.execute("SELECT * FROM faturas_pagas").fetchall()
    }
    conn.close()

    linhas = []
    for c in cartoes:
        faturas = [
            info_fatura(c, p["ano"], p["mes"], totais.get((c["id"], p["ano"], p["mes"]), 0.0),
                        pagas.get((c["id"], p["ano"], p["mes"])))
            for p in periodos
        ]
        linhas.append({"cartao_id": c["id"], "cartao": c["nome"], "faturas": faturas,
                       "total": round(sum(f["total"] for f in faturas), 2)})
    totais_mes = [round(sum(l["faturas"][i]["total"] for l in linhas), 2) for i in range(12)]
    return {"periodos": periodos, "linhas": linhas, "totais_mes": totais_mes}


@app.get("/api/dashboard")
def obter_dashboard(ano: int = 2026, mes_inicio: int = 1):
    """
    Calcula a matriz do Dashboard para uma janela de 12 meses (Receita, Despesa, Saldo do Mês,
    Saldo Acumulado, % Poupado, Orçado vs Realizado, e Resumo por Categoria).

    A janela começa em mes_inicio/ano e pode atravessar a virada do ano. As séries são indexadas
    pela posição na janela (1..12); `periodos` informa ano/mês/rótulo de cada posição.
    """
    periodos = janela_meses(ano, mes_inicio)
    pos_de = mapa_posicoes(periodos)
    posicoes = range(1, 13)
    sql_j, params_j = filtro_janela(periodos)
    conn = get_db_connection()

    # 1. Receitas e Despesas por mês
    rows_mes = conn.execute(
        f"""
        SELECT ano, mes, tipo, SUM(valor) as total
        FROM lancamentos
        WHERE {sql_j}
        GROUP BY ano, mes, tipo
        """,
        params_j,
    ).fetchall()

    receitas_mes = {p: 0.0 for p in posicoes}
    despesas_mes = {p: 0.0 for p in posicoes}
    for r in rows_mes:
        pos = pos_de[(r["ano"], r["mes"])]
        if r["tipo"] == "Receita":
            receitas_mes[pos] = round(r["total"], 2)
        else:
            despesas_mes[pos] = round(r["total"], 2)

    # 2. Orçamento total por mês
    rows_orc = conn.execute(
        f"""
        SELECT ano, mes, SUM(valor_orcado) as total_orcado
        FROM orcamento
        WHERE {sql_j}
        GROUP BY ano, mes
        """,
        params_j,
    ).fetchall()
    orcado_mes = {p: 0.0 for p in posicoes}
    for r in rows_orc:
        orcado_mes[pos_de[(r["ano"], r["mes"])]] = round(r["total_orcado"] or 0.0, 2)

    # 3. Cálculos mensais derivados (acumulado segue a ordem da janela)
    saldo_mes = {}
    saldo_acumulado = {}
    pct_poupado = {}
    orcado_vs_real = {}
    acum = 0.0

    for p in posicoes:
        rec = receitas_mes[p]
        desp = despesas_mes[p]
        s_mes = round(rec - desp, 2)
        saldo_mes[p] = s_mes
        acum = round(acum + s_mes, 2)
        saldo_acumulado[p] = acum
        pct_poupado[p] = round((s_mes / rec) * 100, 2) if rec > 0 else 0.0
        orcado_vs_real[p] = round(desp - orcado_mes[p], 2) if orcado_mes[p] > 0 or desp > 0 else 0.0

    # 4. Resumo por Categoria (aba 📥 Receitas & Despesas)
    sql_jl, params_jl = filtro_janela(periodos, "l.")
    rows_cat = conn.execute(
        f"""
        SELECT l.categoria, l.tipo, COALESCE(c.macro_categoria, 'Outros') as macro_categoria,
               l.ano, l.mes, SUM(l.valor) as total
        FROM lancamentos l
        LEFT JOIN categorias c ON l.categoria = c.nome
        WHERE {sql_jl}
        GROUP BY l.categoria, l.tipo, l.ano, l.mes
        """,
        params_jl,
    ).fetchall()

    todas_cats = conn.execute("SELECT nome, tipo, macro_categoria FROM categorias ORDER BY id ASC").fetchall()
    resumo_categorias = []
    mapa_cat = {}

    def novo_item(nome, tipo, macro):
        item = {
            "categoria": nome,
            "tipo": tipo,
            "macro_categoria": macro,
            "meses": {p: 0.0 for p in posicoes},
            "total_ano": 0.0,
        }
        resumo_categorias.append(item)
        mapa_cat[nome] = item
        return item

    for c in todas_cats:
        novo_item(c["nome"], c["tipo"], c["macro_categoria"])

    for r in rows_cat:
        item = mapa_cat.get(r["categoria"]) or novo_item(r["categoria"], r["tipo"], r["macro_categoria"])
        item["meses"][pos_de[(r["ano"], r["mes"])]] = round(r["total"], 2)
        item["total_ano"] = round(sum(item["meses"].values()), 2)

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
        "mes_inicio": mes_inicio,
        "periodos": periodos,
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
def obter_orcamento(ano: int = 2026, mes_inicio: int = 1):
    """
    Retorna a matriz de Orçamento vs Realizado por Macro-Categoria para uma janela de 12 meses.
    Valores indexados pela posição (1..12) na janela; `periodos` traz ano/mês de cada posição.
    """
    periodos = janela_meses(ano, mes_inicio)
    pos_de = mapa_posicoes(periodos)
    posicoes = range(1, 13)
    conn = get_db_connection()

    macros = [
        "Moradia", "Alimentação", "Transporte", "Saúde", "Educação",
        "Lazer", "Vestuário", "Pessoal", "Financeiro", "Poupança/Investimentos", "Outros"
    ]

    sql_j, params_j = filtro_janela(periodos)
    rows_orc = conn.execute(
        f"SELECT macro_categoria, ano, mes, valor_orcado FROM orcamento WHERE {sql_j}", params_j
    ).fetchall()
    orc_map = {(r["macro_categoria"], pos_de[(r["ano"], r["mes"])]): round(r["valor_orcado"], 2) for r in rows_orc}

    sql_jl, params_jl = filtro_janela(periodos, "l.")
    rows_real = conn.execute(
        f"""
        SELECT COALESCE(c.macro_categoria, 'Outros') as macro, l.ano, l.mes, SUM(l.valor) as total
        FROM lancamentos l
        LEFT JOIN categorias c ON l.categoria = c.nome
        WHERE {sql_jl} AND l.tipo = 'Despesa'
        GROUP BY macro, l.ano, l.mes
        """,
        params_jl,
    ).fetchall()
    real_map = {(r["macro"], pos_de[(r["ano"], r["mes"])]): round(r["total"], 2) for r in rows_real}

    conn.close()

    resultado = []
    for mac in macros:
        orc_meses = {p: orc_map.get((mac, p), 0.0) for p in posicoes}
        real_meses = {p: real_map.get((mac, p), 0.0) for p in posicoes}
        dif_meses = {p: round(real_meses[p] - orc_meses[p], 2) for p in posicoes}
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

    return {"periodos": periodos, "linhas": resultado}


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
def obter_planejamento(ano: int = 2026, mes_inicio: int = 1):
    """Retorna os dados consolidados de Investimentos, Dívidas, Metas e Reserva de Emergência."""
    conn = get_db_connection()
    inv = [dict(r) for r in conn.execute("SELECT * FROM investimentos ORDER BY id ASC").fetchall()]
    div = [dict(r) for r in conn.execute("SELECT * FROM dividas ORDER BY taxa_juros_mes DESC, id ASC").fetchall()]
    metas = [dict(r) for r in conn.execute("SELECT * FROM metas ORDER BY id ASC").fetchall()]
    res_cfg = dict(conn.execute("SELECT * FROM reserva_config WHERE id = 1").fetchone())

    # Calcula despesa mensal média automática dos meses com movimentação dentro da janela
    sql_j, params_j = filtro_janela(janela_meses(ano, mes_inicio))
    row_media = conn.execute(
        f"""
        SELECT AVG(total_mes) as media FROM (
            SELECT ano, mes, SUM(valor) as total_mes
            FROM lancamentos
            WHERE {sql_j} AND tipo = 'Despesa'
            GROUP BY ano, mes
        )
        """,
        params_j,
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
