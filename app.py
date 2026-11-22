"""
Servidor Backend de Controle Financeiro Pessoal (FastAPI + SQLite).
Fornece API RESTful para Lançamentos, Dashboard Anual, Orçamento vs Realizado,
Investimentos, Dívidas, Metas e Reserva de Emergência, com mecanismos de Poka-Yoke.
"""
import os
import sqlite3
import json
import calendar
import math
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


MESES_NOMES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
               "setembro", "outubro", "novembro", "dezembro"]


def normalizar_prazo(txt: Optional[str]) -> Optional[str]:
    """Aceita 'AAAA-MM', 'MM/AAAA' ou 'Mês/AAAA' e devolve 'AAAA-MM' (ou None se não reconhecer)."""
    if not txt:
        return None
    t = txt.strip().lower().replace(" de ", "/").replace(" ", "/")
    try:
        if len(t) == 7 and t[4] == "-":
            a, m = int(t[:4]), int(t[5:])
        else:
            parte_mes, parte_ano = t.split("/")[0], t.split("/")[-1]
            a = int(parte_ano)
            if parte_mes.isdigit():
                m = int(parte_mes)
            else:
                m = next(i + 1 for i, n in enumerate(MESES_NOMES)
                         if n.startswith(parte_mes[:3]) or n.replace("ç", "c").startswith(parte_mes[:3]))
        if 1 <= m <= 12 and 1900 <= a <= 2200:
            return f"{a:04d}-{m:02d}"
    except (ValueError, StopIteration, IndexError):
        pass
    return None


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

        CREATE TABLE IF NOT EXISTS financiamentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            sistema TEXT NOT NULL DEFAULT 'SAC',
            indexador TEXT NOT NULL DEFAULT 'TR',
            valor_financiado REAL NOT NULL,
            taxa_juros_anual REAL NOT NULL,
            tipo_taxa TEXT NOT NULL DEFAULT 'efetiva',
            prazo_meses INTEGER NOT NULL,
            data_primeira_parcela TEXT NOT NULL,
            tr_projetada REAL NOT NULL DEFAULT 0.0,
            seguro_mensal REAL NOT NULL DEFAULT 0.0,
            taxa_adm_mensal REAL NOT NULL DEFAULT 0.0,
            categoria TEXT NOT NULL,
            forma_pgto TEXT NOT NULL DEFAULT 'Boleto',
            lancar_a_partir_de TEXT,
            recorrencia_id TEXT,
            criado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS financiamento_amortizacoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            financiamento_id INTEGER NOT NULL,
            data TEXT NOT NULL,
            valor REAL NOT NULL,
            modo TEXT NOT NULL,
            criado_em TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS financiamento_tr (
            financiamento_id INTEGER NOT NULL,
            ano INTEGER NOT NULL,
            mes INTEGER NOT NULL,
            tr_pct REAL NOT NULL,
            PRIMARY KEY (financiamento_id, ano, mes)
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

        CREATE TABLE IF NOT EXISTS reserva_movimentos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data TEXT NOT NULL,
            tipo TEXT NOT NULL,
            valor REAL NOT NULL,
            descricao TEXT
        );

        CREATE TABLE IF NOT EXISTS app_meta (
            chave TEXT PRIMARY KEY,
            valor TEXT
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
        ("financiamento_id", "INTEGER"),
        ("amortizacao_id", "INTEGER"),
        ("valor_previsto", "REAL"),
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

    # Dados de exemplo antigos (eram inseridos automaticamente): remove uma única vez
    # apenas as linhas que continuam idênticas ao exemplo, preservando o que o usuário alterou.
    if not cur.execute("SELECT 1 FROM app_meta WHERE chave = 'exemplos_removidos'").fetchone():
        exemplos_inv = [
            ("Tesouro Selic 2029", "Renda Fixa", 15000.0, 15650.0, 4.33),
            ("CDB 110% CDI Liquidez Diária", "Renda Fixa", 10000.0, 10420.0, 4.20),
            ("Fundo Imobiliário KNCR11", "FIIs", 8000.0, 8350.0, 4.38),
        ]
        for e in exemplos_inv:
            cur.execute("""DELETE FROM investimentos WHERE ativo = ? AND classe = ? AND saldo_inicial = ?
                           AND saldo_atual = ? AND rentabilidade_pct = ?""", e)
        exemplos_div = [
            ("Cartão de Crédito", "Rotativo", 1200.0, 12.5, 600.0, 2),
            ("Financiamento Veicular", "CDC", 18500.0, 1.89, 780.0, 28),
        ]
        for e in exemplos_div:
            cur.execute("""DELETE FROM dividas WHERE credor = ? AND tipo = ? AND saldo_devedor = ? AND taxa_juros_mes = ?
                           AND parcela_min = ? AND parcelas_rest = ?""", e)
        exemplos_metas = [
            ("Viagem de Férias", "Dezembro/2026", 8000.0, 2500.0),
            ("Troca de Carro", "Julho/2027", 35000.0, 10000.0),
            ("Pós-Graduação", "Janeiro/2027", 12000.0, 4000.0),
        ]
        for e in exemplos_metas:
            cur.execute("DELETE FROM metas WHERE nome = ? AND prazo = ? AND valor_alvo = ? AND valor_atual = ?", e)
        cur.execute("INSERT INTO app_meta (chave, valor) VALUES ('exemplos_removidos', ?)", (datetime.now().isoformat(),))

    # Prazo das metas no formato AAAA-MM (converte textos antigos como "Dezembro/2026")
    for row in cur.execute("SELECT id, prazo FROM metas").fetchall():
        convertido = normalizar_prazo(row[1])
        if convertido and convertido != row[1]:
            cur.execute("UPDATE metas SET prazo = ? WHERE id = ?", (convertido, row[0]))

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


TR_ANUAL_MIN, TR_ANUAL_MAX = -5.0, 20.0


class FinanciamentoInput(BaseModel):
    nome: str = Field(..., min_length=1, description="Ex.: Apartamento - Caixa")
    valor_financiado: float = Field(..., gt=0)
    taxa_juros_anual: float = Field(..., ge=0, le=100, description="Taxa de juros anual em %")
    tipo_taxa: str = Field(default="efetiva", pattern="^(efetiva|nominal)$")
    prazo_meses: int = Field(..., ge=1, le=600)
    data_primeira_parcela: str = Field(..., description="YYYY-MM-DD")
    tr_projetada_anual: float = Field(
        default=0.0, ge=TR_ANUAL_MIN, le=TR_ANUAL_MAX,
        description="TR projetada em % AO ANO (ex.: 1.8); convertida para a taxa mensal equivalente",
    )
    seguro_mensal: float = Field(default=0.0, ge=0, description="MIP + DFI em R$/mês")
    taxa_adm_mensal: float = Field(default=0.0, ge=0, description="Taxa de administração em R$/mês")
    categoria: str = Field(default="Juros e Empréstimos")
    forma_pgto: str = "Boleto"
    lancar_a_partir_de: Optional[str] = Field(
        default=None, description="AAAA-MM: só gera lançamentos a partir deste mês (financiamento já em andamento)"
    )


class TRMensalInput(BaseModel):
    ano: int
    mes: int = Field(..., ge=1, le=12)
    tr_pct: Optional[float] = Field(
        default=None, ge=-1, le=1, description="TR real do MÊS em % (ex.: 0.17); null remove"
    )


class TRProjetadaInput(BaseModel):
    tr_anual_pct: float = Field(..., ge=TR_ANUAL_MIN, le=TR_ANUAL_MAX, description="TR projetada em % ao ano")


class LancamentoUpdateInput(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    tipo: str = Field(..., pattern="^(Receita|Despesa)$")
    categoria: str = Field(..., min_length=1)
    descricao: Optional[str] = ""
    valor: float = Field(..., gt=0)
    forma_pgto: str = "PIX"
    observacao: Optional[str] = ""
    cartao_id: Optional[int] = None
    escopo: str = Field(default="unico", pattern="^(unico|futuros|todos)$")
    restaurar_previsto: bool = Field(default=False, description="Volta o valor ao previsto (descarta o ajuste)")


class AmortizacaoInput(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD do pagamento extra")
    valor: float = Field(..., gt=0)
    modo: str = Field(..., pattern="^(prazo|parcela)$", description="prazo = reduz prazo; parcela = reduz prestação")


class PagamentoFaturaInput(BaseModel):
    data_pagamento: Optional[str] = None


class OrcamentoInput(BaseModel):
    macro_categoria: str
    ano: int
    mes: int = Field(..., ge=1, le=12)
    valor_orcado: float = Field(..., ge=0)


class OrcamentoLoteInput(BaseModel):
    macros: List[str] = Field(..., min_length=1)
    de: str = Field(..., description="AAAA-MM (inclusive)")
    ate: str = Field(..., description="AAAA-MM (inclusive)")
    valor_orcado: float = Field(..., ge=0)
    sobrescrever: bool = Field(default=True, description="False = só preenche meses ainda sem orçamento")


class OrcamentoCopiaInput(BaseModel):
    origem: str = Field(..., description="AAAA-MM de onde copiar todas as macro-categorias")
    de: str
    ate: str
    sobrescrever: bool = True


class InvestimentoInput(BaseModel):
    ativo: str = Field(..., min_length=1)
    classe: str = Field(default="Renda Fixa")
    saldo_inicial: float = Field(default=0.0, ge=0, description="Valor aplicado")
    saldo_atual: float = Field(default=0.0, ge=0)


class DividaInput(BaseModel):
    credor: str = Field(..., min_length=1)
    tipo: str = Field(default="Outros")
    saldo_devedor: float = Field(default=0.0, ge=0)
    taxa_juros_mes: float = Field(default=0.0, ge=0, le=100, description="% ao mês")
    parcela_min: float = Field(default=0.0, ge=0)
    parcelas_rest: int = Field(default=0, ge=0, le=1000)


class MetaInput(BaseModel):
    nome: str = Field(..., min_length=1)
    prazo: str = Field(..., description="AAAA-MM")
    valor_alvo: float = Field(..., gt=0)
    valor_atual: float = Field(default=0.0, ge=0)


class AporteMetaInput(BaseModel):
    valor: float = Field(..., description="Positivo para aportar, negativo para retirar")


class ReservaMovimentoInput(BaseModel):
    data: str = Field(..., description="YYYY-MM-DD")
    tipo: str = Field(..., pattern="^(aporte|resgate)$")
    valor: float = Field(..., gt=0)
    descricao: Optional[str] = ""


class ReservaUpdate(BaseModel):
    despesa_media_manual: float = Field(default=0.0, ge=0)
    usar_media_automatica: int = Field(default=1, ge=0, le=1)
    meses_seguranca: int = Field(default=6, ge=1, le=60)
    saldo_atual_reserva: float = Field(default=0.0, ge=0)


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
# Financiamento: SAC (Sistema de Amortização Constante) com indexador TR
# ==========================================
def taxa_mensal(taxa_anual_pct: float, tipo: str) -> float:
    """Converte a taxa anual em mensal: efetiva -> (1+i)^(1/12)-1 ; nominal -> i/12."""
    i = taxa_anual_pct / 100
    return (1 + i) ** (1 / 12) - 1 if tipo == "efetiva" else i / 12


def tr_anual_para_mensal(tr_anual_pct: float) -> float:
    """TR anual (%) -> TR mensal equivalente (%)."""
    return ((1 + tr_anual_pct / 100) ** (1 / 12) - 1) * 100


def tr_mensal_para_anual(tr_mensal_pct: float) -> float:
    return ((1 + tr_mensal_pct / 100) ** 12 - 1) * 100


def calcular_sac_tr(fin: dict, tr_reais: Optional[dict] = None, amortizacoes: Optional[List[dict]] = None) -> List[dict]:
    """
    Gera o cronograma SAC corrigido pela TR. A cada mês k:
      saldo corrigido = saldo anterior × (1 + TR mensal)
      amortização     = saldo corrigido ÷ prestações restantes
      juros           = saldo corrigido × taxa mensal
      prestação       = amortização + juros + seguros + taxa de administração
      novo saldo      = saldo corrigido − amortização
    TR real informada para o mês substitui a TR projetada (fin["tr_projetada"], mensal em %).

    Amortizações extraordinárias são abatidas do saldo logo após a prestação vencida na data do
    pagamento extra (ou antes da 1ª, se anteriores a ela):
      modo "parcela": mantém o nº de prestações restantes → as próximas prestações diminuem;
      modo "prazo":   mantém a amortização mensal → o nº de prestações restantes diminui.
    """
    tr_reais = tr_reais or {}
    i_m = taxa_mensal(fin["taxa_juros_anual"], fin["tipo_taxa"])
    n = int(fin["prazo_meses"])
    inicio = datetime.strptime(fin["data_primeira_parcela"], "%Y-%m-%d").date()
    saldo = float(fin["valor_financiado"])
    seguro = float(fin["seguro_mensal"] or 0)
    adm = float(fin["taxa_adm_mensal"] or 0)
    extras = sorted(amortizacoes or [], key=lambda a: (a["data"], a.get("id") or 0))
    restantes = n
    amort_ref = saldo / n

    def aplicar_extras(ate_data: Optional[str], desde_data: Optional[str]):
        """Abate extras com desde_data <= data < ate_data. Retorna o total abatido."""
        nonlocal saldo, restantes
        total = 0.0
        for a in extras:
            if (desde_data is None or a["data"] >= desde_data) and (ate_data is None or a["data"] < ate_data):
                if saldo <= 0.005:
                    break
                valor = min(float(a["valor"]), saldo)
                saldo -= valor
                total += valor
                if a["modo"] == "prazo" and saldo > 0.005 and amort_ref > 0:
                    restantes = max(1, min(restantes, math.ceil(saldo / amort_ref - 1e-9)))
        return total

    linhas = []
    # Extras pagos antes da 1ª prestação
    extra_inicial = aplicar_extras(inicio.isoformat(), None)

    k = 0
    while saldo > 0.005 and restantes > 0 and k < n:
        k += 1
        d = somar_meses(inicio, k - 1)
        d_prox = somar_meses(inicio, k)
        real = (d.year, d.month) in tr_reais
        tr = tr_reais[(d.year, d.month)] if real else float(fin["tr_projetada"] or 0)
        saldo_ini = saldo
        correcao = saldo * tr / 100
        saldo_corr = saldo + correcao
        amort = saldo_corr / restantes
        juros = saldo_corr * i_m
        prestacao = amort + juros + seguro + adm
        saldo = saldo_corr - amort
        restantes -= 1
        amort_ref = amort
        extra = aplicar_extras(d_prox.isoformat(), d.isoformat())
        linhas.append({
            "n": k,
            "data": d.isoformat(),
            "ano": d.year,
            "mes": d.month,
            "tr_pct": round(tr, 6),
            "tr_real": real,
            "saldo_inicial": round(saldo_ini, 2),
            "correcao_tr": round(correcao, 2),
            "amortizacao": round(amort, 2),
            "juros": round(juros, 2),
            "seguro": round(seguro, 2),
            "taxa_adm": round(adm, 2),
            "prestacao": round(prestacao, 2),
            "amortizacao_extra": round(extra, 2),
            "amortizacao_extra_antes": round(extra_inicial, 2) if k == 1 else 0.0,
            "saldo_devedor": round(max(saldo, 0.0), 2),
            "restantes": restantes if saldo > 0.005 else 0,
        })
    return linhas


def resumo_financiamento(fin: dict, cronograma: List[dict], hoje: Optional[date] = None) -> dict:
    hoje = hoje or date.today()
    if not cronograma:
        return {"taxa_mensal_pct": round(taxa_mensal(fin["taxa_juros_anual"], fin["tipo_taxa"]) * 100, 6),
                "quitado": True, "parcelas_total": 0, "parcelas_pagas": 0, "parcelas_restantes": 0,
                "saldo_devedor_hoje": 0.0, "proxima_parcela": None, "total_juros": 0.0, "total_pago": 0.0,
                "total_amortizado": 0.0, "total_correcao_tr": 0.0, "total_seguros_taxas": 0.0,
                "total_amortizacao_extra": 0.0, "primeira_prestacao": 0.0, "ultima_prestacao": 0.0,
                "ultima_data": None, "prestacao_sobe": None, "tr_limite_inicial_pct": 0.0}
    pagas = [l for l in cronograma if l["data"] <= hoje.isoformat()]
    futuras = [l for l in cronograma if l["data"] > hoje.isoformat()]
    i_m = taxa_mensal(fin["taxa_juros_anual"], fin["tipo_taxa"])

    # Diagnóstico: no SAC/TR a prestação só sobe se a TR mensal superar i ÷ (1 + i × restantes)
    sobe = None
    for a, b in zip(cronograma, cronograma[1:]):
        if a["amortizacao_extra"] == 0 and b["prestacao"] > a["prestacao"] + 0.005:
            sobe = {"de_n": a["n"], "para_n": b["n"], "de": a["prestacao"], "para": b["prestacao"],
                    "tr_pct": b["tr_pct"]}
            break
    limite = i_m / (1 + i_m * int(fin["prazo_meses"])) * 100

    total_extra = sum(l["amortizacao_extra"] + l["amortizacao_extra_antes"] for l in cronograma)
    return {
        "taxa_mensal_pct": round(i_m * 100, 6),
        "tr_projetada_mensal_pct": round(float(fin["tr_projetada"] or 0), 6),
        "tr_projetada_anual_pct": round(tr_mensal_para_anual(float(fin["tr_projetada"] or 0)), 4),
        "tr_limite_inicial_pct": round(limite, 4),
        "tr_limite_inicial_anual_pct": round(tr_mensal_para_anual(limite), 4),
        "prestacao_sobe": sobe,
        "quitado": not futuras,
        "parcelas_total": len(cronograma),
        "primeira_prestacao": cronograma[0]["prestacao"],
        "ultima_prestacao": cronograma[-1]["prestacao"],
        "ultima_data": cronograma[-1]["data"],
        "total_amortizado": round(sum(l["amortizacao"] for l in cronograma), 2),
        "total_amortizacao_extra": round(total_extra, 2),
        "total_juros": round(sum(l["juros"] for l in cronograma), 2),
        "total_correcao_tr": round(sum(l["correcao_tr"] for l in cronograma), 2),
        "total_seguros_taxas": round(sum(l["seguro"] + l["taxa_adm"] for l in cronograma), 2),
        "total_pago": round(sum(l["prestacao"] for l in cronograma) + total_extra, 2),
        "parcelas_pagas": len(pagas),
        "parcelas_restantes": len(futuras),
        "saldo_devedor_hoje": pagas[-1]["saldo_devedor"] if pagas else cronograma[0]["saldo_inicial"],
        "proxima_parcela": futuras[0] if futuras else None,
    }


def _obs_parcela(l: dict) -> str:
    return (f"Amort. R$ {l['amortizacao']:.2f} | Juros R$ {l['juros']:.2f} | "
            f"Seg+Adm R$ {l['seguro'] + l['taxa_adm']:.2f} | TR {l['tr_pct']:.4f}% a.m."
            + ("" if l["tr_real"] else " (proj.)"))


def _validar_financiamento(payload: FinanciamentoInput) -> dict:
    try:
        datetime.strptime(payload.data_primeira_parcela, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Data da 1ª prestação inválida. Use YYYY-MM-DD.")
    if payload.lancar_a_partir_de:
        try:
            datetime.strptime(payload.lancar_a_partir_de + "-01", "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="'Lançar a partir de' deve estar no formato AAAA-MM.")
    nome = payload.nome.strip()
    if not nome:
        raise HTTPException(status_code=400, detail="Informe um nome para o financiamento.")
    dados = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    dados["nome"] = nome
    dados["tr_projetada"] = tr_anual_para_mensal(payload.tr_projetada_anual)
    return dados


def _tr_reais(conn, fin_id: int) -> dict:
    return {(r["ano"], r["mes"]): r["tr_pct"] for r in conn.execute(
        "SELECT ano, mes, tr_pct FROM financiamento_tr WHERE financiamento_id = ?", (fin_id,)).fetchall()}


def _amortizacoes(conn, fin_id: int) -> List[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM financiamento_amortizacoes WHERE financiamento_id = ? ORDER BY data, id", (fin_id,)).fetchall()]


def _cronograma(conn, fin: dict, amortizacoes: Optional[List[dict]] = None) -> List[dict]:
    amort = _amortizacoes(conn, fin["id"]) if amortizacoes is None else amortizacoes
    return calcular_sac_tr(fin, _tr_reais(conn, fin["id"]), amort)


def _sincronizar_lancamentos_financiamento(conn, fin: dict, atualizar_contrato: bool = False) -> List[dict]:
    """
    Recalcula o cronograma e alinha as prestações lançadas:
      - atualiza valor, observação e total de parcelas das existentes;
        prestações ajustadas manualmente (valor_previsto preenchido) mantêm o valor pago
        e só têm o valor previsto atualizado;
      - remove as que deixaram de existir (prazo reduzido / quitação);
      - cria as que voltaram a existir além da última lançada (ex.: amortização desfeita).
    Com atualizar_contrato=True (edição do contrato), também atualiza data, categoria,
    descrição e forma de pagamento.
    Prestações excluídas manualmente no meio da série não são recriadas.
    """
    cron = _cronograma(conn, fin)
    por_n = {l["n"]: l for l in cron}
    total = len(cron)
    existentes = conn.execute(
        "SELECT id, parcela_num, valor_previsto FROM lancamentos WHERE financiamento_id = ? AND amortizacao_id IS NULL",
        (fin["id"],)).fetchall()
    max_n = 0
    for row in existentes:
        l = por_n.get(row["parcela_num"])
        if not l:
            conn.execute("DELETE FROM lancamentos WHERE id = ?", (row["id"],))
            continue
        max_n = max(max_n, row["parcela_num"])
        if row["valor_previsto"] is not None:
            conn.execute("UPDATE lancamentos SET valor_previsto = ?, parcela_total = ? WHERE id = ?",
                         (l["prestacao"], total, row["id"]))
        else:
            conn.execute("UPDATE lancamentos SET valor = ?, observacao = ?, parcela_total = ? WHERE id = ?",
                         (l["prestacao"], _obs_parcela(l), total, row["id"]))
        if atualizar_contrato:
            conn.execute(
                """UPDATE lancamentos SET data = ?, mes = ?, ano = ?, competencia = ?, categoria = ?,
                   descricao = ?, forma_pgto = ? WHERE id = ?""",
                (l["data"], l["mes"], l["ano"], f"{l['mes']:02d}/{l['ano']}", fin["categoria"],
                 f"Financiamento {fin['nome']}", fin["forma_pgto"], row["id"]),
            )
    if atualizar_contrato:
        conn.execute("UPDATE lancamentos SET categoria = ?, forma_pgto = ? WHERE amortizacao_id IS NOT NULL AND financiamento_id = ?",
                     (fin["categoria"], fin["forma_pgto"], fin["id"]))
    a_partir = fin.get("lancar_a_partir_de") or (cron[0]["data"][:7] if cron else "9999-12")
    if existentes:
        for l in cron:
            if l["n"] > max_n and l["data"][:7] >= a_partir:
                _inserir_prestacao(conn, fin, l, total)
    return cron


def _inserir_prestacao(conn, fin: dict, l: dict, total: int):
    conn.execute(
        """
        INSERT INTO lancamentos (data, mes, ano, tipo, categoria, descricao, valor, forma_pgto, observacao,
            competencia, recorrencia_id, parcela_num, parcela_total, financiamento_id)
        VALUES (?, ?, ?, 'Despesa', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (l["data"], l["mes"], l["ano"], fin["categoria"], f"Financiamento {fin['nome']}", l["prestacao"],
         fin["forma_pgto"], _obs_parcela(l), f"{l['mes']:02d}/{l['ano']}", fin["recorrencia_id"], l["n"],
         total, fin["id"]),
    )


def _obter_financiamento(conn, fin_id: int) -> dict:
    row = conn.execute("SELECT * FROM financiamentos WHERE id = ?", (fin_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Financiamento não encontrado.")
    return dict(row)


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
    ate: Optional[str] = None,
    mes: Optional[int] = None,
    tipo: Optional[str] = None,
    busca: Optional[str] = None,
    recorrencia: Optional[str] = None,
    cartao_id: Optional[int] = None,
    com_cartao: bool = False,
    categoria: Optional[str] = None,
    macro: Optional[str] = None,
):
    """
    Lista os lançamentos com filtros combinados.

    Período (um dos três):
      - padrão: janela de 12 meses iniciada em mes_inicio/ano;
      - `periodo=AAAA-MM`: um único mês;
      - `ate=AAAA-MM`: do início da janela até esse mês (ex.: saldo acumulado);
      - `mes` (legado): um mês do `ano`.
    Outros filtros: tipo, busca (texto), recorrencia (unico | recorrente), cartao_id,
    com_cartao (só compras em cartão), categoria (nome exato) e macro (macro-categoria).
    """
    def ano_mes(txt: str, nome: str) -> tuple:
        try:
            a, m = (int(x) for x in txt.split("-"))
            if not 1 <= m <= 12:
                raise ValueError
            return a, m
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{nome} deve estar no formato AAAA-MM.")

    if periodo:
        p_ano, p_mes = ano_mes(periodo, "periodo")
        where, params = "l.ano = ? AND l.mes = ?", [p_ano, p_mes]
    elif ate:
        a_ano, a_mes = ano_mes(ate, "ate")
        ini = janela_meses(ano, mes_inicio)[0]
        where = "(l.ano * 100 + l.mes) BETWEEN ? AND ?"
        params = [chave_periodo(ini["ano"], ini["mes"]), chave_periodo(a_ano, a_mes)]
    elif mes:
        where, params = "l.ano = ? AND l.mes = ?", [ano, mes]
    else:
        where, params = filtro_janela(janela_meses(ano, mes_inicio), "l.")

    query = f"""
        SELECT l.*, k.nome AS cartao_nome, COALESCE(c.macro_categoria, 'Outros') AS macro_categoria
        FROM lancamentos l
        LEFT JOIN cartoes k ON k.id = l.cartao_id
        LEFT JOIN categorias c ON c.nome = l.categoria
        WHERE {where}
    """
    if tipo and tipo in ("Receita", "Despesa"):
        query += " AND l.tipo = ?"
        params.append(tipo)
    if busca:
        query += " AND (l.descricao LIKE ? OR l.categoria LIKE ? OR l.observacao LIKE ? OR k.nome LIKE ?)"
        termo = f"%{busca}%"
        params.extend([termo, termo, termo, termo])
    if recorrencia == "unico":
        query += " AND l.recorrencia_id IS NULL"
    elif recorrencia == "recorrente":
        query += " AND l.recorrencia_id IS NOT NULL"
    if cartao_id:
        query += " AND l.cartao_id = ?"
        params.append(cartao_id)
    if com_cartao:
        query += " AND l.cartao_id IS NOT NULL"
    if categoria is not None:
        query += " AND l.categoria = ?"
        params.append(categoria)
    if macro is not None:
        query += " AND COALESCE(c.macro_categoria, 'Outros') = ?"
        params.append(macro)

    query += " ORDER BY l.data DESC, l.id DESC"
    conn = get_db_connection()
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
    elif rec is not None and FREQUENCIAS[rec.frequencia][0] == "meses":
        # Cobrança recorrente mensal/bimestral/... no cartão: uma ocorrência por fatura no intervalo,
        # ancorada na fatura da 1ª cobrança (evita pular/duplicar fatura em dias 29–31)
        base = fatura_da_compra(cartao, dt)
        passo = FREQUENCIAS[rec.frequencia][1]
        periodos_contabeis = [mover_mes(base[0], base[1], i * passo) for i in range(len(datas))]
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


@app.get("/api/lancamentos/{lanc_id}")
def obter_lancamento(lanc_id: int):
    """Retorna um lançamento (com nome do cartão e modo da série) para o formulário de edição."""
    conn = get_db_connection()
    row = conn.execute(
        """
        SELECT l.*, k.nome AS cartao_nome, r.modo AS modo_serie
        FROM lancamentos l
        LEFT JOIN cartoes k ON k.id = l.cartao_id
        LEFT JOIN recorrencias r ON r.id = l.recorrencia_id
        WHERE l.id = ?
        """,
        (lanc_id,),
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado.")
    return dict(row)


@app.put("/api/lancamentos/{lanc_id}")
def atualizar_lancamento(lanc_id: int, payload: LancamentoUpdateInput):
    """
    Edita um lançamento. Em séries (recorrências e parcelamentos), `escopo` define o alcance:
    unico (só este), futuros (este e os próximos) ou todos. Ao mudar a data numa série, todas as
    ocorrências do escopo são deslocadas pela mesma diferença de dias.

    O primeiro ajuste de valor guarda o valor original em `valor_previsto`, para comparar previsto x real
    (descontos, juros, reajustes). `restaurar_previsto` desfaz o ajuste.

    Casos especiais:
      - prestação de financiamento: só o próprio lançamento; o valor ajustado não é sobrescrito por recálculos;
      - pagamento de amortização extra: altera a amortização (data/valor) e recalcula o financiamento;
      - compra no cartão: mudar data ou cartão recalcula a fatura.
    """
    try:
        nova_data = datetime.strptime(payload.data, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida. Use YYYY-MM-DD.")

    conn = get_db_connection()
    lanc = conn.execute("SELECT * FROM lancamentos WHERE id = ?", (lanc_id,)).fetchone()
    if not lanc:
        conn.close()
        raise HTTPException(status_code=404, detail="Lançamento não encontrado.")
    lanc = dict(lanc)
    descricao = (payload.descricao or "").strip()
    observacao = (payload.observacao or "").strip()
    valor = round(payload.valor, 2)

    # --- Pagamento de amortização extraordinária ---
    if lanc["amortizacao_id"] and lanc["financiamento_id"]:
        fin = _obter_financiamento(conn, lanc["financiamento_id"])
        amort = conn.execute("SELECT * FROM financiamento_amortizacoes WHERE id = ?", (lanc["amortizacao_id"],)).fetchone()
        outras = [a for a in _amortizacoes(conn, fin["id"]) if a["id"] != lanc["amortizacao_id"]]
        base = _cronograma(conn, fin, outras)
        entrada = AmortizacaoInput(data=payload.data, valor=valor, modo=amort["modo"])
        _validar_amortizacao(conn, fin, entrada, base)
        _gravar_amortizacao(conn, fin, amort["id"], payload.data, valor, amort["modo"])
        conn.execute("UPDATE lancamentos SET categoria = ?, descricao = ?, forma_pgto = ? WHERE id = ?",
                     (payload.categoria, descricao, payload.forma_pgto, lanc_id))
        _sincronizar_lancamentos_financiamento(conn, fin)
        conn.commit()
        conn.close()
        return {"status": "ok", "atualizados": 1}

    # --- Cartão ---
    cartao = None
    forma_pgto = payload.forma_pgto
    if payload.cartao_id is not None and payload.forma_pgto == "Crédito":
        cartao = conn.execute("SELECT * FROM cartoes WHERE id = ?", (payload.cartao_id,)).fetchone()
        if not cartao:
            conn.close()
            raise HTTPException(status_code=404, detail="Cartão não encontrado.")
        if payload.tipo != "Despesa":
            conn.close()
            raise HTTPException(status_code=400, detail="Compras no cartão devem ser do tipo Despesa.")
    cartao_id = cartao["id"] if cartao else None

    # --- Prestação de financiamento: só este lançamento ---
    eh_prestacao = bool(lanc["financiamento_id"])
    if eh_prestacao:
        if payload.tipo != "Despesa" or cartao_id:
            conn.close()
            raise HTTPException(status_code=400, detail="Prestação de financiamento deve ser Despesa e sem cartão.")
        escopo = "unico"
    else:
        escopo = payload.escopo if lanc["recorrencia_id"] else "unico"

    # Linhas afetadas
    if escopo == "unico":
        linhas = [lanc]
    elif escopo == "futuros":
        linhas = [dict(r) for r in conn.execute(
            "SELECT * FROM lancamentos WHERE recorrencia_id = ? AND parcela_num >= ? ORDER BY parcela_num",
            (lanc["recorrencia_id"], lanc["parcela_num"])).fetchall()]
    else:
        linhas = [dict(r) for r in conn.execute(
            "SELECT * FROM lancamentos WHERE recorrencia_id = ? ORDER BY parcela_num",
            (lanc["recorrencia_id"],)).fetchall()]

    modo_serie = None
    if lanc["recorrencia_id"]:
        rec = conn.execute("SELECT modo FROM recorrencias WHERE id = ?", (lanc["recorrencia_id"],)).fetchone()
        modo_serie = rec["modo"] if rec else None

    delta = nova_data - datetime.strptime(lanc["data"], "%Y-%m-%d").date()
    for row in linhas:
        d_antiga = datetime.strptime(row["data"], "%Y-%m-%d").date()
        d_nova = nova_data if row["id"] == lanc_id else d_antiga + delta
        mudou_data = d_nova != d_antiga
        mudou_cartao = cartao_id != row["cartao_id"]
        if cartao is not None and (mudou_data or mudou_cartao):
            if modo_serie == "parcelar" and row["parcela_num"]:
                base = fatura_da_compra(cartao, d_nova)
                ano_c, mes_c = mover_mes(base[0], base[1], row["parcela_num"] - 1)
            else:
                ano_c, mes_c = fatura_da_compra(cartao, d_nova)
        elif cartao is None and (mudou_data or row["cartao_id"] is not None):
            ano_c, mes_c = d_nova.year, d_nova.month
        else:
            ano_c, mes_c = row["ano"], row["mes"]

        if payload.restaurar_previsto and row["valor_previsto"] is not None:
            novo_valor, previsto = row["valor_previsto"], None
        elif payload.restaurar_previsto:
            novo_valor, previsto = row["valor"], None
        else:
            novo_valor = valor
            previsto = row["valor_previsto"]
            if previsto is None and abs(novo_valor - row["valor"]) >= 0.005:
                previsto = row["valor"]
            if previsto is not None and abs(novo_valor - previsto) < 0.005:
                previsto = None  # voltou ao previsto

        conn.execute(
            """
            UPDATE lancamentos SET data = ?, mes = ?, ano = ?, competencia = ?, tipo = ?, categoria = ?,
                descricao = ?, valor = ?, valor_previsto = ?, forma_pgto = ?, observacao = ?, cartao_id = ?
            WHERE id = ?
            """,
            (d_nova.isoformat(), mes_c, ano_c, f"{mes_c:02d}/{ano_c}", payload.tipo, payload.categoria, descricao,
             novo_valor, previsto, forma_pgto, observacao, cartao_id, row["id"]),
        )

    # Prestação restaurada volta a seguir o cronograma
    if eh_prestacao and payload.restaurar_previsto:
        fin = _obter_financiamento(conn, lanc["financiamento_id"])
        _sincronizar_lancamentos_financiamento(conn, fin)

    conn.commit()
    conn.close()
    return {"status": "ok", "atualizados": len(linhas)}


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

    # Lançamento de amortização extra: desfaz a amortização inteira (mantém o cronograma coerente)
    if lanc["amortizacao_id"] and lanc["financiamento_id"]:
        fin = conn.execute("SELECT * FROM financiamentos WHERE id = ?", (lanc["financiamento_id"],)).fetchone()
        if fin:
            _remover_amortizacao(conn, dict(fin), lanc["amortizacao_id"])
            conn.commit()
            conn.close()
            return {"status": "deleted", "id": lanc_id, "escopo": "amortizacao", "removidos": 1}

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
        """SELECT l.*, r.modo AS modo_serie FROM lancamentos l LEFT JOIN recorrencias r ON r.id = l.recorrencia_id
           WHERE l.cartao_id = ? AND l.ano = ? AND l.mes = ? ORDER BY l.data ASC, l.id ASC""",
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


# ==========================================
# Financiamentos (SAC / TR)
# ==========================================
@app.post("/api/financiamentos/simular")
def simular_financiamento(payload: FinanciamentoInput):
    """Calcula o cronograma SAC/TR sem gravar nada (prévia do formulário)."""
    fin = _validar_financiamento(payload)
    cron = calcular_sac_tr(fin)
    return {"resumo": resumo_financiamento(fin, cron), "cronograma": cron}


@app.post("/api/financiamentos")
def criar_financiamento(payload: FinanciamentoInput):
    """
    Cadastra o financiamento e gera uma despesa mensal para cada prestação
    (a partir de `lancar_a_partir_de`, se informado), vinculadas como uma série.
    """
    fin = _validar_financiamento(payload)
    cron = calcular_sac_tr(fin)
    a_partir = fin.get("lancar_a_partir_de") or cron[0]["data"][:7]
    parcelas = [l for l in cron if l["data"][:7] >= a_partir]
    if not parcelas:
        raise HTTPException(status_code=400, detail="Nenhuma prestação a lançar a partir do mês informado.")

    fin["recorrencia_id"] = uuid.uuid4().hex
    fin["lancar_a_partir_de"] = a_partir
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO financiamentos (nome, sistema, indexador, valor_financiado, taxa_juros_anual, tipo_taxa,
                prazo_meses, data_primeira_parcela, tr_projetada, seguro_mensal, taxa_adm_mensal, categoria,
                forma_pgto, lancar_a_partir_de, recorrencia_id, criado_em)
            VALUES (?, 'SAC', 'TR', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (fin["nome"], fin["valor_financiado"], fin["taxa_juros_anual"], fin["tipo_taxa"], fin["prazo_meses"],
             fin["data_primeira_parcela"], fin["tr_projetada"], fin["seguro_mensal"], fin["taxa_adm_mensal"],
             fin["categoria"], fin["forma_pgto"], a_partir, fin["recorrencia_id"],
             datetime.now().isoformat(timespec="seconds")),
        )
        fin["id"] = cur.lastrowid
        cur.execute(
            """
            INSERT INTO recorrencias (id, frequencia, modo, ocorrencias, data_inicio, valor_informado, criado_em)
            VALUES (?, 'mensal', 'financiamento', ?, ?, ?, ?)
            """,
            (fin["recorrencia_id"], fin["prazo_meses"], fin["data_primeira_parcela"], fin["valor_financiado"],
             datetime.now().isoformat(timespec="seconds")),
        )
        for l in parcelas:
            _inserir_prestacao(conn, fin, l, len(cron))
        conn.commit()
    finally:
        conn.close()
    return {"status": "ok", "id": fin["id"], "lancamentos_gerados": len(parcelas),
            "primeira_competencia": parcelas[0]["data"][:7], "ultima_competencia": parcelas[-1]["data"][:7]}


def _com_tr_anual(fin: dict) -> dict:
    fin["tr_projetada_anual"] = round(tr_mensal_para_anual(float(fin["tr_projetada"] or 0)), 4)
    return fin


@app.get("/api/financiamentos")
def listar_financiamentos():
    conn = get_db_connection()
    resultado = []
    for row in conn.execute("SELECT * FROM financiamentos ORDER BY id").fetchall():
        fin = _com_tr_anual(dict(row))
        cron = _cronograma(conn, fin)
        resultado.append({**fin, "resumo": resumo_financiamento(fin, cron)})
    conn.close()
    return resultado


@app.get("/api/financiamentos/{fin_id}")
def obter_financiamento(fin_id: int):
    """
    Contrato + cronograma (com TR real/projetada e amortizações extras) + quais prestações estão lançadas
    + comparação com o cenário sem amortizações extraordinárias.
    """
    conn = get_db_connection()
    fin = _com_tr_anual(_obter_financiamento(conn, fin_id))
    amortizacoes = _amortizacoes(conn, fin_id)
    cron = _cronograma(conn, fin, amortizacoes)
    sem_extra = _cronograma(conn, fin, [])
    lancadas = {r["parcela_num"]: dict(r) for r in conn.execute(
        "SELECT id, parcela_num, valor, valor_previsto FROM lancamentos WHERE financiamento_id = ? AND amortizacao_id IS NULL",
        (fin_id,)).fetchall()}
    conn.close()
    for l in cron:
        lanc = lancadas.get(l["n"])
        l["lancamento_id"] = lanc["id"] if lanc else None
        l["valor_lancado"] = lanc["valor"] if lanc else None
        l["ajustado"] = bool(lanc and lanc["valor_previsto"] is not None)
    resumo = resumo_financiamento(fin, cron)
    resumo_sem = resumo_financiamento(fin, sem_extra)
    return {
        **fin,
        "resumo": resumo,
        "cronograma": cron,
        "amortizacoes": amortizacoes,
        "efeito_amortizacoes": {
            "economia_juros": round(resumo_sem["total_juros"] - resumo["total_juros"], 2),
            "meses_a_menos": resumo_sem["parcelas_total"] - resumo["parcelas_total"],
            "ultima_data_original": resumo_sem["ultima_data"],
        },
    }


def _comparar_amortizacao(fin: dict, base: List[dict], com: List[dict], data_extra: str) -> dict:
    """Compara cronogramas antes/depois de uma amortização: juros, prazo e próxima prestação."""
    juros_base = sum(l["juros"] for l in base)
    juros_com = sum(l["juros"] for l in com)
    prox_base = next((l for l in base if l["data"] > data_extra), None)
    prox_com = next((l for l in com if l["data"] > data_extra), None)
    return {
        "economia_juros": round(juros_base - juros_com, 2),
        "parcelas_antes": len(base),
        "parcelas_depois": len(com),
        "meses_a_menos": len(base) - len(com),
        "ultima_data_antes": base[-1]["data"] if base else None,
        "ultima_data_depois": com[-1]["data"] if com else None,
        "proxima_prestacao_antes": prox_base["prestacao"] if prox_base else None,
        "proxima_prestacao_depois": prox_com["prestacao"] if prox_com else None,
        "proxima_data": prox_com["data"] if prox_com else None,
        "quita": not com or com[-1]["data"] <= data_extra,
    }


def fmt_brl(v: float) -> str:
    return f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _validar_amortizacao(conn, fin: dict, payload: AmortizacaoInput, base: List[dict]):
    try:
        datetime.strptime(payload.data, "%Y-%m-%d")
    except ValueError:
        conn.close()
        raise HTTPException(status_code=400, detail="Data inválida. Use YYYY-MM-DD.")
    if not base or payload.data >= base[-1]["data"]:
        conn.close()
        raise HTTPException(status_code=400, detail="A data da amortização deve ser anterior à última prestação.")
    # saldo devedor no momento do pagamento extra
    anteriores = [l for l in base if l["data"] <= payload.data]
    saldo = anteriores[-1]["saldo_devedor"] if anteriores else float(fin["valor_financiado"])
    if payload.valor > saldo + 0.01:
        conn.close()
        raise HTTPException(status_code=400,
                            detail=f"O valor ultrapassa o saldo devedor nessa data (R$ {fmt_brl(saldo)}).")


@app.post("/api/financiamentos/{fin_id}/amortizacoes/simular")
def simular_amortizacao(fin_id: int, payload: AmortizacaoInput):
    """Mostra o efeito de uma amortização extraordinária sem gravar."""
    conn = get_db_connection()
    fin = _obter_financiamento(conn, fin_id)
    amortizacoes = _amortizacoes(conn, fin_id)
    base = _cronograma(conn, fin, amortizacoes)
    _validar_amortizacao(conn, fin, payload, base)
    nova = {"id": 10 ** 9, "data": payload.data, "valor": payload.valor, "modo": payload.modo}
    com = _cronograma(conn, fin, amortizacoes + [nova])
    conn.close()
    return _comparar_amortizacao(fin, base, com, payload.data)


@app.post("/api/financiamentos/{fin_id}/amortizacoes")
def registrar_amortizacao(fin_id: int, payload: AmortizacaoInput):
    """
    Registra uma amortização extraordinária: lança o pagamento como despesa na data informada
    e recalcula as prestações (reduzindo prazo ou parcela).
    """
    conn = get_db_connection()
    fin = _obter_financiamento(conn, fin_id)
    amortizacoes = _amortizacoes(conn, fin_id)
    base = _cronograma(conn, fin, amortizacoes)
    _validar_amortizacao(conn, fin, payload, base)
    cur = conn.execute(
        "INSERT INTO financiamento_amortizacoes (financiamento_id, data, valor, modo, criado_em) VALUES (?, ?, ?, ?, ?)",
        (fin_id, payload.data, round(payload.valor, 2), payload.modo, datetime.now().isoformat(timespec="seconds")),
    )
    amort_id = cur.lastrowid
    d = datetime.strptime(payload.data, "%Y-%m-%d").date()
    conn.execute(
        """
        INSERT INTO lancamentos (data, mes, ano, tipo, categoria, descricao, valor, forma_pgto, observacao,
            competencia, financiamento_id, amortizacao_id)
        VALUES (?, ?, ?, 'Despesa', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (payload.data, d.month, d.year, fin["categoria"], f"Amortização extra {fin['nome']}", round(payload.valor, 2),
         fin["forma_pgto"], "Reduz prazo" if payload.modo == "prazo" else "Reduz parcela",
         f"{d.month:02d}/{d.year}", fin_id, amort_id),
    )
    cron = _sincronizar_lancamentos_financiamento(conn, fin)
    efeito = _comparar_amortizacao(fin, base, cron, payload.data)
    conn.commit()
    conn.close()
    return {"status": "ok", "id": amort_id, **efeito}


def _remover_amortizacao(conn, fin: dict, amort_id: int):
    conn.execute("DELETE FROM lancamentos WHERE amortizacao_id = ?", (amort_id,))
    conn.execute("DELETE FROM financiamento_amortizacoes WHERE id = ? AND financiamento_id = ?", (amort_id, fin["id"]))
    _sincronizar_lancamentos_financiamento(conn, fin)


@app.delete("/api/financiamentos/{fin_id}/amortizacoes/{amort_id}")
def excluir_amortizacao(fin_id: int, amort_id: int):
    """Desfaz uma amortização extraordinária (remove o lançamento e recalcula as prestações)."""
    conn = get_db_connection()
    fin = _obter_financiamento(conn, fin_id)
    _remover_amortizacao(conn, fin, amort_id)
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": amort_id}


@app.put("/api/financiamentos/{fin_id}")
def atualizar_financiamento(fin_id: int, payload: FinanciamentoInput):
    """
    Edita o contrato (nome, valores, taxa, prazo, datas, seguros, taxas, TR projetada, categoria, forma de pgto)
    e recalcula todas as prestações lançadas. Prestações com valor ajustado manualmente mantêm o valor pago.
    O mês a partir do qual as prestações são lançadas não muda.
    """
    dados = _validar_financiamento(payload)
    conn = get_db_connection()
    fin = _obter_financiamento(conn, fin_id)
    campos = ["nome", "valor_financiado", "taxa_juros_anual", "tipo_taxa", "prazo_meses", "data_primeira_parcela",
              "tr_projetada", "seguro_mensal", "taxa_adm_mensal", "categoria", "forma_pgto"]
    for c in campos:
        fin[c] = dados[c]
    conn.execute(
        f"UPDATE financiamentos SET {', '.join(f'{c} = ?' for c in campos)} WHERE id = ?",
        [fin[c] for c in campos] + [fin_id],
    )
    conn.execute("UPDATE lancamentos SET descricao = ? WHERE amortizacao_id IS NOT NULL AND financiamento_id = ?",
                 (f"Amortização extra {fin['nome']}", fin_id))
    cron = _sincronizar_lancamentos_financiamento(conn, fin, atualizar_contrato=True)
    conn.commit()
    conn.close()
    return {"status": "ok", "id": fin_id, "parcelas_total": len(cron)}


@app.put("/api/financiamentos/{fin_id}/amortizacoes/{amort_id}")
def atualizar_amortizacao(fin_id: int, amort_id: int, payload: AmortizacaoInput):
    """Edita data, valor ou modo de uma amortização extraordinária e recalcula as prestações."""
    conn = get_db_connection()
    fin = _obter_financiamento(conn, fin_id)
    if not conn.execute("SELECT id FROM financiamento_amortizacoes WHERE id = ? AND financiamento_id = ?",
                        (amort_id, fin_id)).fetchone():
        conn.close()
        raise HTTPException(status_code=404, detail="Amortização não encontrada.")
    outras = [a for a in _amortizacoes(conn, fin_id) if a["id"] != amort_id]
    base = _cronograma(conn, fin, outras)
    _validar_amortizacao(conn, fin, payload, base)
    _gravar_amortizacao(conn, fin, amort_id, payload.data, payload.valor, payload.modo)
    cron = _sincronizar_lancamentos_financiamento(conn, fin)
    efeito = _comparar_amortizacao(fin, base, cron, payload.data)
    conn.commit()
    conn.close()
    return {"status": "ok", "id": amort_id, **efeito}


def _gravar_amortizacao(conn, fin: dict, amort_id: int, data_str: str, valor: float, modo: str):
    """Atualiza a amortização e o lançamento do pagamento extra correspondente."""
    d = datetime.strptime(data_str, "%Y-%m-%d").date()
    conn.execute("UPDATE financiamento_amortizacoes SET data = ?, valor = ?, modo = ? WHERE id = ?",
                 (data_str, round(valor, 2), modo, amort_id))
    conn.execute(
        """UPDATE lancamentos SET data = ?, mes = ?, ano = ?, competencia = ?, valor = ?, observacao = ?
           WHERE amortizacao_id = ?""",
        (data_str, d.month, d.year, f"{d.month:02d}/{d.year}", round(valor, 2),
         "Reduz prazo" if modo == "prazo" else "Reduz parcela", amort_id),
    )


@app.put("/api/financiamentos/{fin_id}/tr")
def definir_tr_mensal(fin_id: int, payload: TRMensalInput):
    """Registra (ou remove, com tr_pct = null) a TR real de um mês e recalcula as prestações."""
    conn = get_db_connection()
    fin = _obter_financiamento(conn, fin_id)
    if payload.tr_pct is None:
        conn.execute("DELETE FROM financiamento_tr WHERE financiamento_id = ? AND ano = ? AND mes = ?",
                     (fin_id, payload.ano, payload.mes))
    else:
        conn.execute(
            """
            INSERT INTO financiamento_tr (financiamento_id, ano, mes, tr_pct) VALUES (?, ?, ?, ?)
            ON CONFLICT(financiamento_id, ano, mes) DO UPDATE SET tr_pct = excluded.tr_pct
            """,
            (fin_id, payload.ano, payload.mes, payload.tr_pct),
        )
    _sincronizar_lancamentos_financiamento(conn, fin)
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.put("/api/financiamentos/{fin_id}/tr-projetada")
def definir_tr_projetada(fin_id: int, payload: TRProjetadaInput):
    """Altera a TR projetada (informada AO ANO, aplicada como taxa mensal equivalente) e recalcula."""
    conn = get_db_connection()
    fin = _obter_financiamento(conn, fin_id)
    fin["tr_projetada"] = tr_anual_para_mensal(payload.tr_anual_pct)
    conn.execute("UPDATE financiamentos SET tr_projetada = ? WHERE id = ?", (fin["tr_projetada"], fin_id))
    _sincronizar_lancamentos_financiamento(conn, fin)
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.delete("/api/financiamentos/{fin_id}")
def excluir_financiamento(fin_id: int):
    """Exclui o financiamento, TRs, amortizações extras e todos os lançamentos vinculados."""
    conn = get_db_connection()
    fin = _obter_financiamento(conn, fin_id)
    removidos = conn.execute("DELETE FROM lancamentos WHERE financiamento_id = ?", (fin_id,)).rowcount
    conn.execute("DELETE FROM financiamento_tr WHERE financiamento_id = ?", (fin_id,))
    conn.execute("DELETE FROM financiamento_amortizacoes WHERE financiamento_id = ?", (fin_id,))
    if fin["recorrencia_id"]:
        conn.execute("DELETE FROM recorrencias WHERE id = ?", (fin["recorrencia_id"],))
    conn.execute("DELETE FROM financiamentos WHERE id = ?", (fin_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": fin_id, "lancamentos_removidos": removidos}


# ==========================================
# Fluxo de caixa diário
# ==========================================
DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


@app.get("/api/fluxo-diario")
def fluxo_diario(inicio: Optional[str] = None, fim: Optional[str] = None):
    """
    Entradas, saídas, saldo do dia e saldo acumulado, dia a dia, entre `inicio` e `fim` (YYYY-MM-DD;
    padrão: mês atual).

    Regra de caixa: lançamentos comuns contam na própria data; compras no cartão contam no
    VENCIMENTO da fatura, agrupadas por cartão/fatura (é quando o dinheiro sai da conta).
    O saldo inicial é o acumulado de tudo que ocorreu antes de `inicio`.
    """
    hoje = date.today()
    try:
        d_ini = datetime.strptime(inicio, "%Y-%m-%d").date() if inicio else hoje.replace(day=1)
        d_fim = datetime.strptime(fim, "%Y-%m-%d").date() if fim else dia_no_mes(d_ini.year, d_ini.month, 31)
    except ValueError:
        raise HTTPException(status_code=400, detail="Datas inválidas. Use YYYY-MM-DD.")
    if d_fim < d_ini:
        raise HTTPException(status_code=400, detail="A data final deve ser posterior à inicial.")
    if (d_fim - d_ini).days > 400:
        raise HTTPException(status_code=400, detail="Período máximo de 400 dias.")
    ini_s, fim_s = d_ini.isoformat(), d_fim.isoformat()

    conn = get_db_connection()
    cartoes = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM cartoes").fetchall()}

    # Saldo anterior: lançamentos comuns antes do início
    abertura = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN tipo = 'Receita' THEN valor ELSE -valor END), 0)
           FROM lancamentos WHERE cartao_id IS NULL AND data < ?""", (ini_s,)).fetchone()[0]

    # Faturas (agrupadas): data de saída = vencimento
    faturas = []
    for r in conn.execute(
        """SELECT cartao_id, ano, mes, SUM(CASE WHEN tipo = 'Receita' THEN -valor ELSE valor END) AS total, COUNT(*) AS qtd
           FROM lancamentos WHERE cartao_id IS NOT NULL GROUP BY cartao_id, ano, mes""").fetchall():
        c = cartoes.get(r["cartao_id"])
        if not c:
            continue
        _, venc = datas_da_fatura(c, r["ano"], r["mes"])
        if venc < d_ini:
            abertura -= r["total"]
        elif venc <= d_fim:
            faturas.append({"cartao_id": c["id"], "cartao": c["nome"], "ano": r["ano"], "mes": r["mes"],
                            "label": f"{MESES_ABREV[r['mes'] - 1]}/{str(r['ano'])[2:]}",
                            "vencimento": venc.isoformat(), "total": round(r["total"], 2), "qtd": r["qtd"]})

    comuns = [dict(r) for r in conn.execute(
        """SELECT l.*, COALESCE(c.macro_categoria, 'Outros') AS macro_categoria FROM lancamentos l
           LEFT JOIN categorias c ON c.nome = l.categoria
           WHERE l.cartao_id IS NULL AND l.data BETWEEN ? AND ? ORDER BY l.data, l.id""", (ini_s, fim_s)).fetchall()]

    # Itens das faturas do período (para detalhar o dia do vencimento)
    itens_fatura = {}
    for f in faturas:
        itens_fatura[(f["cartao_id"], f["ano"], f["mes"])] = [dict(r, cartao_nome=f["cartao"], vencimento_fatura=f["vencimento"]) for r in conn.execute(
            """SELECT l.*, COALESCE(c.macro_categoria, 'Outros') AS macro_categoria FROM lancamentos l
               LEFT JOIN categorias c ON c.nome = l.categoria
               WHERE l.cartao_id = ? AND l.ano = ? AND l.mes = ? ORDER BY l.data, l.id""",
            (f["cartao_id"], f["ano"], f["mes"])).fetchall()]
    conn.close()

    dias = {}
    d = d_ini
    while d <= d_fim:
        dias[d.isoformat()] = {"data": d.isoformat(), "dia_semana": DIAS_SEMANA[d.weekday()], "entradas": 0.0,
                               "saidas": 0.0, "itens": [], "faturas": []}
        d += timedelta(days=1)
    for l in comuns:
        dia = dias[l["data"]]
        if l["tipo"] == "Receita":
            dia["entradas"] += l["valor"]
        else:
            dia["saidas"] += l["valor"]
        dia["itens"].append(l)
    for f in faturas:
        dia = dias[f["vencimento"]]
        dia["saidas"] += f["total"]
        dia["faturas"].append(f)
        dia["itens"].extend(itens_fatura[(f["cartao_id"], f["ano"], f["mes"])])

    saldo = abertura
    lista = []
    menor = None
    for k in sorted(dias):
        dia = dias[k]
        dia["entradas"] = round(dia["entradas"], 2)
        dia["saidas"] = round(dia["saidas"], 2)
        dia["saldo_dia"] = round(dia["entradas"] - dia["saidas"], 2)
        saldo += dia["saldo_dia"]
        dia["saldo_acumulado"] = round(saldo, 2)
        dia["qtd"] = len([i for i in dia["itens"] if not i.get("cartao_id")]) + len(dia["faturas"])
        if menor is None or dia["saldo_acumulado"] < menor["saldo"]:
            menor = {"data": k, "saldo": dia["saldo_acumulado"]}
        lista.append(dia)

    entradas = round(sum(x["entradas"] for x in lista), 2)
    saidas = round(sum(x["saidas"] for x in lista), 2)
    return {
        "inicio": ini_s,
        "fim": fim_s,
        "saldo_inicial": round(abertura, 2),
        "saldo_final": round(saldo, 2),
        "entradas": entradas,
        "saidas": saidas,
        "saldo_periodo": round(entradas - saidas, 2),
        "menor_saldo": menor,
        "dias_negativos": sum(1 for x in lista if x["saldo_acumulado"] < 0),
        "dias": lista,
    }


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

    macros = _macros_despesa(conn)

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


MACROS_DESPESA_PADRAO = [
    "Moradia", "Alimentação", "Transporte", "Saúde", "Educação",
    "Lazer", "Vestuário", "Pessoal", "Financeiro", "Poupança/Investimentos", "Outros"
]


def _macros_despesa(conn) -> List[str]:
    """Macros padrão + as criadas pelo usuário em categorias de despesa + as que já têm orçamento."""
    extras = [r[0] for r in conn.execute(
        """SELECT DISTINCT macro_categoria FROM categorias WHERE tipo = 'Despesa'
           UNION SELECT DISTINCT macro_categoria FROM orcamento""").fetchall() if r[0]]
    macros = list(MACROS_DESPESA_PADRAO)
    for m in sorted(set(extras) - set(macros), key=str.lower):
        macros.insert(len(macros) - 1, m)  # antes de "Outros"
    return macros


def _intervalo_meses(de: str, ate: str) -> List[tuple]:
    try:
        a1, m1 = (int(x) for x in de.split("-"))
        a2, m2 = (int(x) for x in ate.split("-"))
        if not (1 <= m1 <= 12 and 1 <= m2 <= 12):
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="Meses devem estar no formato AAAA-MM.")
    ini, fim = a1 * 12 + m1 - 1, a2 * 12 + m2 - 1
    if fim < ini:
        raise HTTPException(status_code=400, detail="O mês final deve ser igual ou posterior ao inicial.")
    if fim - ini >= 120:
        raise HTTPException(status_code=400, detail="Intervalo máximo de 120 meses.")
    return [(t // 12, t % 12 + 1) for t in range(ini, fim + 1)]


def _gravar_orcamento(conn, macro: str, ano: int, mes: int, valor: float, sobrescrever: bool) -> bool:
    if not sobrescrever:
        existe = conn.execute("SELECT valor_orcado FROM orcamento WHERE macro_categoria = ? AND ano = ? AND mes = ?",
                              (macro, ano, mes)).fetchone()
        if existe and (existe[0] or 0) > 0:
            return False
    conn.execute(
        """INSERT INTO orcamento (macro_categoria, ano, mes, valor_orcado) VALUES (?, ?, ?, ?)
           ON CONFLICT(macro_categoria, ano, mes) DO UPDATE SET valor_orcado = excluded.valor_orcado""",
        (macro, ano, mes, round(valor, 2)),
    )
    return True


@app.get("/api/orcamento/macros")
def listar_macros_orcamento():
    conn = get_db_connection()
    macros = _macros_despesa(conn)
    conn.close()
    return macros


@app.get("/api/orcamento/serie")
def serie_orcamento(de: str, ate: str, macro: Optional[str] = None):
    """
    Orçado x realizado mês a mês entre `de` e `ate` (AAAA-MM), para uma macro-categoria ou,
    sem `macro`, para o total (orçado de todas as macros x todas as despesas).
    """
    meses = _intervalo_meses(de, ate)
    k_ini, k_fim = chave_periodo(*meses[0]), chave_periodo(*meses[-1])
    conn = get_db_connection()
    if macro:
        orc_rows = conn.execute(
            """SELECT ano, mes, SUM(valor_orcado) AS v FROM orcamento
               WHERE macro_categoria = ? AND (ano * 100 + mes) BETWEEN ? AND ? GROUP BY ano, mes""",
            (macro, k_ini, k_fim)).fetchall()
        real_rows = conn.execute(
            """SELECT l.ano, l.mes, SUM(l.valor) AS v FROM lancamentos l
               LEFT JOIN categorias c ON c.nome = l.categoria
               WHERE l.tipo = 'Despesa' AND COALESCE(c.macro_categoria, 'Outros') = ?
                 AND (l.ano * 100 + l.mes) BETWEEN ? AND ? GROUP BY l.ano, l.mes""",
            (macro, k_ini, k_fim)).fetchall()
    else:
        orc_rows = conn.execute(
            """SELECT ano, mes, SUM(valor_orcado) AS v FROM orcamento
               WHERE (ano * 100 + mes) BETWEEN ? AND ? GROUP BY ano, mes""", (k_ini, k_fim)).fetchall()
        real_rows = conn.execute(
            """SELECT ano, mes, SUM(valor) AS v FROM lancamentos
               WHERE tipo = 'Despesa' AND (ano * 100 + mes) BETWEEN ? AND ? GROUP BY ano, mes""",
            (k_ini, k_fim)).fetchall()
    conn.close()
    orc = {(r["ano"], r["mes"]): r["v"] or 0 for r in orc_rows}
    real = {(r["ano"], r["mes"]): r["v"] or 0 for r in real_rows}

    serie = []
    acum_o = acum_r = 0.0
    for a, m in meses:
        o, r = round(orc.get((a, m), 0.0), 2), round(real.get((a, m), 0.0), 2)
        acum_o += o
        acum_r += r
        serie.append({
            "chave": f"{a:04d}-{m:02d}", "ano": a, "mes": m, "label": f"{MESES_ABREV[m - 1]}/{str(a)[2:]}",
            "orcado": o, "realizado": r, "diferenca": round(r - o, 2),
            "orcado_acumulado": round(acum_o, 2), "realizado_acumulado": round(acum_r, 2),
        })
    tot_o, tot_r = round(acum_o, 2), round(acum_r, 2)
    return {
        "macro": macro,
        "meses": serie,
        "total_orcado": tot_o,
        "total_realizado": tot_r,
        "diferenca": round(tot_r - tot_o, 2),
        "execucao_pct": round(tot_r / tot_o * 100, 1) if tot_o > 0 else None,
        "meses_acima": sum(1 for x in serie if x["orcado"] > 0 and x["realizado"] > x["orcado"] + 0.005),
        "meses_com_orcamento": sum(1 for x in serie if x["orcado"] > 0),
        "meses_sem_orcamento_com_gasto": sum(1 for x in serie if x["orcado"] == 0 and x["realizado"] > 0),
    }


@app.post("/api/orcamento/lote")
def orcamento_em_lote(payload: OrcamentoLoteInput):
    """Define o mesmo valor orçado para uma ou várias macro-categorias em todos os meses de um intervalo."""
    meses = _intervalo_meses(payload.de, payload.ate)
    macros = [m.strip() for m in payload.macros if m.strip()]
    if not macros:
        raise HTTPException(status_code=400, detail="Escolha ao menos uma macro-categoria.")
    conn = get_db_connection()
    gravados = sum(_gravar_orcamento(conn, mac, a, m, payload.valor_orcado, payload.sobrescrever)
                   for mac in macros for a, m in meses)
    conn.commit()
    conn.close()
    return {"status": "ok", "gravados": gravados, "ignorados": len(macros) * len(meses) - gravados,
            "meses": len(meses), "macros": len(macros)}


@app.post("/api/orcamento/copiar")
def copiar_orcamento(payload: OrcamentoCopiaInput):
    """Copia o orçamento de todas as macro-categorias de um mês para cada mês de um intervalo."""
    try:
        oa, om = (int(x) for x in payload.origem.split("-"))
    except ValueError:
        raise HTTPException(status_code=400, detail="Mês de origem deve estar no formato AAAA-MM.")
    meses = [(a, m) for a, m in _intervalo_meses(payload.de, payload.ate) if (a, m) != (oa, om)]
    conn = get_db_connection()
    origem = conn.execute("SELECT macro_categoria, valor_orcado FROM orcamento WHERE ano = ? AND mes = ? AND valor_orcado > 0",
                          (oa, om)).fetchall()
    if not origem:
        conn.close()
        raise HTTPException(status_code=400, detail="O mês de origem não tem orçamento para copiar.")
    gravados = sum(_gravar_orcamento(conn, r["macro_categoria"], a, m, r["valor_orcado"], payload.sobrescrever)
                   for r in origem for a, m in meses)
    conn.commit()
    conn.close()
    return {"status": "ok", "gravados": gravados, "ignorados": len(origem) * len(meses) - gravados,
            "meses": len(meses), "macros": len(origem)}


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


def _meses_ate(prazo: Optional[str], hoje: Optional[date] = None) -> Optional[int]:
    if not prazo or len(prazo) != 7:
        return None
    hoje = hoje or date.today()
    a, m = int(prazo[:4]), int(prazo[5:])
    return (a - hoje.year) * 12 + (m - hoje.month)


def _enriquecer_meta(m: dict) -> dict:
    falta = max(0.0, m["valor_alvo"] - m["valor_atual"])
    meses = _meses_ate(m["prazo"])
    m["progresso_pct"] = round(min(100.0, m["valor_atual"] / m["valor_alvo"] * 100), 1) if m["valor_alvo"] > 0 else 0.0
    m["falta"] = round(falta, 2)
    m["meses_restantes"] = meses
    m["aporte_mensal_necessario"] = round(falta / meses, 2) if meses and meses > 0 and falta > 0 else None
    m["atingida"] = falta <= 0.005
    m["atrasada"] = meses is not None and meses < 0 and falta > 0.005
    return m


def _enriquecer_investimento(i: dict) -> dict:
    ini = i["saldo_inicial"] or 0
    i["rendimento"] = round((i["saldo_atual"] or 0) - ini, 2)
    i["rentabilidade_pct"] = round(((i["saldo_atual"] or 0) / ini - 1) * 100, 2) if ini > 0 else 0.0
    return i


@app.get("/api/planejamento")
def obter_planejamento(ano: int = 2026, mes_inicio: int = 1):
    """
    Dados de Investimentos, Dívidas (incluindo financiamentos cadastrados, só leitura),
    Metas e Reserva de Emergência (configuração, meta calculada e movimentações).
    """
    conn = get_db_connection()
    inv = [_enriquecer_investimento(dict(r)) for r in conn.execute("SELECT * FROM investimentos ORDER BY id ASC").fetchall()]
    div = [dict(r, origem="manual") for r in conn.execute("SELECT * FROM dividas ORDER BY taxa_juros_mes DESC, id ASC").fetchall()]
    metas = [_enriquecer_meta(dict(r)) for r in conn.execute("SELECT * FROM metas ORDER BY prazo ASC, id ASC").fetchall()]
    res_cfg = dict(conn.execute("SELECT * FROM reserva_config WHERE id = 1").fetchone())
    movimentos = [dict(r) for r in conn.execute(
        "SELECT * FROM reserva_movimentos ORDER BY data DESC, id DESC LIMIT 50").fetchall()]

    # Financiamentos entram na lista de dívidas (somente leitura; editados na aba Financiamentos)
    for row in conn.execute("SELECT * FROM financiamentos ORDER BY id").fetchall():
        fin = dict(row)
        r = resumo_financiamento(fin, _cronograma(conn, fin))
        if r["quitado"]:
            continue
        prox = r["proxima_parcela"]
        div.append({
            "id": f"fin-{fin['id']}",
            "financiamento_id": fin["id"],
            "origem": "financiamento",
            "credor": fin["nome"],
            "tipo": f"Financiamento {fin['sistema']}/{fin['indexador']}",
            "saldo_devedor": r["saldo_devedor_hoje"],
            "taxa_juros_mes": round(r["taxa_mensal_pct"], 4),
            "parcela_min": prox["prestacao"] if prox else 0.0,
            "parcelas_rest": r["parcelas_restantes"],
        })
    div.sort(key=lambda d: (-d["taxa_juros_mes"], str(d["id"])))

    # Despesa mensal média automática dos meses com movimentação dentro da janela
    sql_j, params_j = filtro_janela(janela_meses(ano, mes_inicio))
    row_media = conn.execute(
        f"""
        SELECT AVG(total_mes) as media, COUNT(*) as meses FROM (
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

    total_inv = sum(i["saldo_atual"] for i in inv)
    total_aplicado = sum(i["saldo_inicial"] for i in inv)
    return {
        "investimentos": inv,
        "totais_investimentos": {
            "aplicado": round(total_aplicado, 2),
            "atual": round(total_inv, 2),
            "rendimento": round(total_inv - total_aplicado, 2),
            "rentabilidade_pct": round((total_inv / total_aplicado - 1) * 100, 2) if total_aplicado > 0 else 0.0,
        },
        "dividas": div,
        "totais_dividas": {
            "saldo": round(sum(d["saldo_devedor"] for d in div), 2),
            "parcelas_mes": round(sum(d["parcela_min"] for d in div), 2),
        },
        "metas": metas,
        "reserva": {
            **res_cfg,
            "despesa_media_calculada": despesa_media_auto,
            "meses_com_despesa": row_media["meses"] or 0,
            "despesa_base": round(despesa_base, 2),
            "meta_total_reserva": meta_reserva,
            "falta": round(max(0.0, meta_reserva - res_cfg["saldo_atual_reserva"]), 2),
            "meses_cobertos": round(res_cfg["saldo_atual_reserva"] / despesa_base, 1) if despesa_base > 0 else None,
            "progresso_pct": round((res_cfg["saldo_atual_reserva"] / meta_reserva) * 100, 1) if meta_reserva > 0 else 0.0,
            "movimentos": movimentos,
        },
    }


# ---------- Investimentos ----------
def _nome_obrigatorio(txt: str, oque: str) -> str:
    t = (txt or "").strip()
    if not t:
        raise HTTPException(status_code=400, detail=f"Informe o {oque}.")
    return t


@app.post("/api/investimentos")
def criar_investimento(payload: InvestimentoInput):
    ativo = _nome_obrigatorio(payload.ativo, "nome do ativo")
    conn = get_db_connection()
    cur = conn.execute(
        "INSERT INTO investimentos (ativo, classe, saldo_inicial, saldo_atual, rentabilidade_pct) VALUES (?, ?, ?, ?, ?)",
        (ativo, payload.classe.strip() or "Outros", round(payload.saldo_inicial, 2), round(payload.saldo_atual, 2),
         _enriquecer_investimento(dict(saldo_inicial=payload.saldo_inicial, saldo_atual=payload.saldo_atual))["rentabilidade_pct"]),
    )
    conn.commit()
    novo = cur.lastrowid
    conn.close()
    return {"status": "ok", "id": novo}


@app.put("/api/investimentos/{inv_id}")
def atualizar_investimento(inv_id: int, payload: InvestimentoInput):
    ativo = _nome_obrigatorio(payload.ativo, "nome do ativo")
    conn = get_db_connection()
    cur = conn.execute(
        "UPDATE investimentos SET ativo = ?, classe = ?, saldo_inicial = ?, saldo_atual = ?, rentabilidade_pct = ? WHERE id = ?",
        (ativo, payload.classe.strip() or "Outros", round(payload.saldo_inicial, 2), round(payload.saldo_atual, 2),
         _enriquecer_investimento(dict(saldo_inicial=payload.saldo_inicial, saldo_atual=payload.saldo_atual))["rentabilidade_pct"],
         inv_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Investimento não encontrado.")
    return {"status": "ok", "id": inv_id}


@app.delete("/api/investimentos/{inv_id}")
def excluir_investimento(inv_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM investimentos WHERE id = ?", (inv_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": inv_id}


# ---------- Dívidas ----------
@app.post("/api/dividas")
def criar_divida(payload: DividaInput):
    credor = _nome_obrigatorio(payload.credor, "credor / nome da dívida")
    conn = get_db_connection()
    cur = conn.execute(
        "INSERT INTO dividas (credor, tipo, saldo_devedor, taxa_juros_mes, parcela_min, parcelas_rest) VALUES (?, ?, ?, ?, ?, ?)",
        (credor, payload.tipo.strip() or "Outros", round(payload.saldo_devedor, 2), payload.taxa_juros_mes,
         round(payload.parcela_min, 2), payload.parcelas_rest),
    )
    conn.commit()
    novo = cur.lastrowid
    conn.close()
    return {"status": "ok", "id": novo}


@app.put("/api/dividas/{div_id}")
def atualizar_divida(div_id: int, payload: DividaInput):
    credor = _nome_obrigatorio(payload.credor, "credor / nome da dívida")
    conn = get_db_connection()
    cur = conn.execute(
        """UPDATE dividas SET credor = ?, tipo = ?, saldo_devedor = ?, taxa_juros_mes = ?, parcela_min = ?,
           parcelas_rest = ? WHERE id = ?""",
        (credor, payload.tipo.strip() or "Outros", round(payload.saldo_devedor, 2), payload.taxa_juros_mes,
         round(payload.parcela_min, 2), payload.parcelas_rest, div_id),
    )
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Dívida não encontrada.")
    return {"status": "ok", "id": div_id}


@app.delete("/api/dividas/{div_id}")
def excluir_divida(div_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM dividas WHERE id = ?", (div_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": div_id}


# ---------- Metas ----------
def _validar_meta(payload: MetaInput) -> tuple:
    nome = _nome_obrigatorio(payload.nome, "nome da meta")
    prazo = normalizar_prazo(payload.prazo)
    if not prazo:
        raise HTTPException(status_code=400, detail="Prazo inválido. Use o formato AAAA-MM.")
    return nome, prazo


@app.post("/api/metas")
def criar_meta(payload: MetaInput):
    nome, prazo = _validar_meta(payload)
    conn = get_db_connection()
    cur = conn.execute("INSERT INTO metas (nome, prazo, valor_alvo, valor_atual) VALUES (?, ?, ?, ?)",
                       (nome, prazo, round(payload.valor_alvo, 2), round(payload.valor_atual, 2)))
    conn.commit()
    novo = cur.lastrowid
    conn.close()
    return {"status": "ok", "id": novo}


@app.put("/api/metas/{meta_id}")
def atualizar_meta(meta_id: int, payload: MetaInput):
    nome, prazo = _validar_meta(payload)
    conn = get_db_connection()
    cur = conn.execute("UPDATE metas SET nome = ?, prazo = ?, valor_alvo = ?, valor_atual = ? WHERE id = ?",
                       (nome, prazo, round(payload.valor_alvo, 2), round(payload.valor_atual, 2), meta_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Meta não encontrada.")
    return {"status": "ok", "id": meta_id}


@app.post("/api/metas/{meta_id}/aporte")
def aportar_meta(meta_id: int, payload: AporteMetaInput):
    """Soma (ou retira, se negativo) um valor ao acumulado da meta."""
    conn = get_db_connection()
    row = conn.execute("SELECT valor_atual FROM metas WHERE id = ?", (meta_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Meta não encontrada.")
    novo = round(row["valor_atual"] + payload.valor, 2)
    if novo < 0:
        conn.close()
        raise HTTPException(status_code=400, detail="A retirada é maior que o valor acumulado na meta.")
    conn.execute("UPDATE metas SET valor_atual = ? WHERE id = ?", (novo, meta_id))
    conn.commit()
    conn.close()
    return {"status": "ok", "valor_atual": novo}


@app.delete("/api/metas/{meta_id}")
def excluir_meta(meta_id: int):
    conn = get_db_connection()
    conn.execute("DELETE FROM metas WHERE id = ?", (meta_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": meta_id}


# ---------- Reserva de Emergência ----------
@app.put("/api/reserva")
@app.post("/api/reserva")
def salvar_reserva(payload: ReservaUpdate):
    """Configuração da reserva: meses de segurança, base (média automática ou valor manual) e saldo atual."""
    conn = get_db_connection()
    conn.execute(
        """
        UPDATE reserva_config
        SET despesa_media_manual = ?, usar_media_automatica = ?, meses_seguranca = ?, saldo_atual_reserva = ?
        WHERE id = 1
        """,
        (round(payload.despesa_media_manual, 2), payload.usar_media_automatica, payload.meses_seguranca,
         round(payload.saldo_atual_reserva, 2)),
    )
    conn.commit()
    conn.close()
    return {"status": "ok"}


@app.post("/api/reserva/movimentos")
def lancar_movimento_reserva(payload: ReservaMovimentoInput):
    """Registra um aporte ou resgate da reserva e atualiza o saldo."""
    try:
        datetime.strptime(payload.data, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Data inválida. Use YYYY-MM-DD.")
    conn = get_db_connection()
    saldo = conn.execute("SELECT saldo_atual_reserva FROM reserva_config WHERE id = 1").fetchone()[0]
    delta = payload.valor if payload.tipo == "aporte" else -payload.valor
    if saldo + delta < -0.005:
        conn.close()
        raise HTTPException(status_code=400, detail=f"Resgate maior que o saldo da reserva (R$ {fmt_brl(saldo)}).")
    cur = conn.execute("INSERT INTO reserva_movimentos (data, tipo, valor, descricao) VALUES (?, ?, ?, ?)",
                       (payload.data, payload.tipo, round(payload.valor, 2), (payload.descricao or "").strip()))
    conn.execute("UPDATE reserva_config SET saldo_atual_reserva = ? WHERE id = 1", (round(saldo + delta, 2),))
    conn.commit()
    novo = cur.lastrowid
    conn.close()
    return {"status": "ok", "id": novo, "saldo": round(saldo + delta, 2)}


@app.delete("/api/reserva/movimentos/{mov_id}")
def excluir_movimento_reserva(mov_id: int):
    """Desfaz um aporte/resgate (estorna o efeito no saldo)."""
    conn = get_db_connection()
    mov = conn.execute("SELECT * FROM reserva_movimentos WHERE id = ?", (mov_id,)).fetchone()
    if not mov:
        conn.close()
        raise HTTPException(status_code=404, detail="Movimentação não encontrada.")
    saldo = conn.execute("SELECT saldo_atual_reserva FROM reserva_config WHERE id = 1").fetchone()[0]
    delta = -mov["valor"] if mov["tipo"] == "aporte" else mov["valor"]
    conn.execute("UPDATE reserva_config SET saldo_atual_reserva = ? WHERE id = 1", (round(max(0.0, saldo + delta), 2),))
    conn.execute("DELETE FROM reserva_movimentos WHERE id = ?", (mov_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": mov_id}
