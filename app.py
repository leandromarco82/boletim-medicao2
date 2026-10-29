import os
import smtplib
import sqlite3
import ssl
import unicodedata
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import streamlit as st

DB = os.environ.get("BOLETIM_DB", "boletim.db")
PLANILHA_PADRAO = Path(__file__).parent / "contratos.xlsx"

TIPOS = {
    "c": "Corretivas fora do SLA",
    "p": "Preventivas fora do prazo ou não executadas",
    "r": "Relatórios fora do prazo ou não entregues",
}
PCT_KEY = {"c": "pc", "p": "pp", "r": "pr"}
ROTULOS = {"c": ("Abertura", "Atendimento"), "p": ("Prazo", "Execução"), "r": ("Prazo", "Entrega")}
COLS = ["unidade", "sistema", "fornecedor", "contrato", "valor_anual"]


# ---------------------------------------------------------------- utilidades
def brl(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(v: float) -> str:
    return f"{v:.1f}".replace(".", ",") + "%"


def secret(nome, padrao=None):
    valor = os.environ.get(nome)
    if valor:
        return valor
    try:
        return st.secrets[nome]
    except Exception:
        return padrao


def _n(s):
    t = unicodedata.normalize("NFD", str(s))
    return "".join(c for c in t if unicodedata.category(c) != "Mn").strip().lower()


def normalizar(df):
    """Aceita variações de cabeçalho (acentos, maiúsculas, 'Fornacedor' etc.)."""
    ren = {}
    for c in df.columns:
        n = _n(c)
        if n == "unidade":
            ren[c] = "unidade"
        elif n == "sistema":
            ren[c] = "sistema"
        elif n.startswith("forn"):
            ren[c] = "fornecedor"
        elif n == "contrato":
            ren[c] = "contrato"
        elif n.startswith("valor"):
            ren[c] = "valor_anual"
    df = df.rename(columns=ren)
    faltam = [c for c in COLS if c not in df.columns]
    if faltam:
        raise ValueError("Colunas não encontradas: " + ", ".join(faltam))
    df = df[COLS].dropna(subset=["unidade", "sistema", "contrato"]).copy()
    df["fornecedor"] = df["fornecedor"].fillna("")
    for c in ["unidade", "sistema", "fornecedor", "contrato"]:
        df[c] = df[c].astype(str).str.strip()
    df["valor_anual"] = pd.to_numeric(df["valor_anual"], errors="coerce").fillna(0.0)
    return df.drop_duplicates(["unidade", "contrato"], keep="last").reset_index(drop=True)


# ------------------------------------------------------------------- banco
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS b_cadastro(
                unidade TEXT, sistema TEXT, fornecedor TEXT, contrato TEXT, valor_anual REAL,
                PRIMARY KEY(unidade, contrato));
            CREATE TABLE IF NOT EXISTS b_termos(
                unidade TEXT, contrato TEXT, email TEXT DEFAULT '',
                pc REAL DEFAULT 3, pp REAL DEFAULT 5, pr REAL DEFAULT 2,
                sla REAL DEFAULT 4, modo TEXT DEFAULT 'u', PRIMARY KEY(unidade, contrato));
            CREATE TABLE IF NOT EXISTS b_boletins(
                unidade TEXT, contrato TEXT, mes TEXT, nf_num TEXT DEFAULT '', nf REAL DEFAULT 0,
                PRIMARY KEY(unidade, contrato, mes));
            CREATE TABLE IF NOT EXISTS b_ocorr(
                id INTEGER PRIMARY KEY AUTOINCREMENT, unidade TEXT, contrato TEXT, mes TEXT,
                tipo TEXT, os TEXT, a TEXT, b TEXT, sla REAL);
            """
        )
        vazio_ = con.execute("SELECT COUNT(*) FROM b_cadastro").fetchone()[0] == 0
    if vazio_ and PLANILHA_PADRAO.exists():
        importar_cadastro(normalizar(pd.read_excel(PLANILHA_PADRAO)))


def importar_cadastro(df):
    with db() as con:
        con.execute("DELETE FROM b_cadastro")
        con.executemany(
            "INSERT INTO b_cadastro VALUES (?,?,?,?,?)",
            [tuple(r) for r in df[COLS].itertuples(index=False, name=None)],
        )


def cadastro():
    with db() as con:
        rows = con.execute("SELECT * FROM b_cadastro ORDER BY unidade, sistema, contrato").fetchall()
    return pd.DataFrame([dict(r) for r in rows], columns=COLS)


def get_termos(unidade, contrato, fornecedor):
    with db() as con:
        r = con.execute("SELECT * FROM b_termos WHERE unidade=? AND contrato=?", (unidade, contrato)).fetchone()
        if r is None:
            em = con.execute(
                "SELECT t.email FROM b_termos t JOIN b_cadastro c ON c.unidade=t.unidade AND c.contrato=t.contrato "
                "WHERE c.fornecedor=? AND t.email<>'' LIMIT 1",
                (fornecedor,),
            ).fetchone()
            con.execute("INSERT INTO b_termos(unidade, contrato, email) VALUES (?,?,?)", (unidade, contrato, em[0] if em else ""))
            r = con.execute("SELECT * FROM b_termos WHERE unidade=? AND contrato=?", (unidade, contrato)).fetchone()
    return dict(r)


def save_termos(t):
    with db() as con:
        con.execute(
            "UPDATE b_termos SET email=?, pc=?, pp=?, pr=?, sla=?, modo=? WHERE unidade=? AND contrato=?",
            (t["email"], t["pc"], t["pp"], t["pr"], t["sla"], t["modo"], t["unidade"], t["contrato"]),
        )


def get_boletim(unidade, contrato, mes):
    with db() as con:
        r = con.execute(
            "SELECT nf_num, nf FROM b_boletins WHERE unidade=? AND contrato=? AND mes=?", (unidade, contrato, mes)
        ).fetchone()
    return dict(r) if r else None


def save_boletim(unidade, contrato, mes, valor_base):
    """Marca o boletim do mês como lançado (a NF ainda não existe nesta etapa)."""
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO b_boletins(unidade, contrato, mes, nf_num, nf) VALUES (?,?,?,?,?)",
            (unidade, contrato, mes, "", valor_base),
        )


def vazio(tipo):
    d = {
        "os": pd.Series(dtype="str"),
        "a": pd.Series(dtype="datetime64[ns]"),
        "b": pd.Series(dtype="datetime64[ns]"),
    }
    if tipo == "c":
        d["sla"] = pd.Series(dtype="float")
    return pd.DataFrame(d)


def load_occ(unidade, contrato, mes, tipo):
    with db() as con:
        rows = con.execute(
            "SELECT os, a, b, sla FROM b_ocorr WHERE unidade=? AND contrato=? AND mes=? AND tipo=? ORDER BY id",
            (unidade, contrato, mes, tipo),
        ).fetchall()
    if not rows:
        return vazio(tipo)
    df = pd.DataFrame([dict(r) for r in rows])
    df["a"] = pd.to_datetime(df["a"], errors="coerce")
    df["b"] = pd.to_datetime(df["b"], errors="coerce")
    return df if tipo == "c" else df.drop(columns=["sla"])


def save_occ(unidade, contrato, mes, tipo, df):
    with db() as con:
        con.execute(
            "DELETE FROM b_ocorr WHERE unidade=? AND contrato=? AND mes=? AND tipo=?", (unidade, contrato, mes, tipo)
        )
        for _, r in df.iterrows():
            a, b = pd.to_datetime(r.get("a"), errors="coerce"), pd.to_datetime(r.get("b"), errors="coerce")
            os_ = r.get("os")
            if pd.isna(a) and pd.isna(b) and (os_ is None or pd.isna(os_) or not str(os_).strip()):
                continue
            sla = r.get("sla")
            con.execute(
                "INSERT INTO b_ocorr(unidade, contrato, mes, tipo, os, a, b, sla) VALUES (?,?,?,?,?,?,?,?)",
                (
                    unidade, contrato, mes, tipo,
                    None if os_ is None or pd.isna(os_) else str(os_),
                    None if pd.isna(a) else a.isoformat(),
                    None if pd.isna(b) else b.isoformat(),
                    None if sla is None or pd.isna(sla) else float(sla),
                ),
            )


# ------------------------------------------------------------------ regras
def atrasado(tipo, row, sla_padrao):
    """True = descumpriu, False = cumpriu, None = linha sem data inicial (ignorada)."""
    a = pd.to_datetime(row.get("a"), errors="coerce")
    b = pd.to_datetime(row.get("b"), errors="coerce")
    if pd.isna(a):
        return None
    if pd.isna(b):
        return True
    if tipo == "c":
        horas = (b - a).total_seconds() / 3600
        sla = row.get("sla")
        limite = sla if sla is not None and pd.notna(sla) and sla > 0 else sla_padrao
        return horas > limite
    return b.normalize() > a.normalize()


def calcular(termos, nf, dfs):
    partes, total = [], 0.0
    for tipo, nome in TIPOS.items():
        viol, n = 0, 0
        for _, row in dfs[tipo].iterrows():
            r = atrasado(tipo, row, termos["sla"])
            if r is None:
                continue
            n += 1
            viol += int(r)
        p = float(termos[PCT_KEY[tipo]])
        aplicado = 0.0 if viol == 0 else (p if termos["modo"] == "u" else min(100.0, p * viol))
        valor = nf * aplicado / 100
        total += valor
        partes.append({"tipo": tipo, "nome": nome, "viol": viol, "n": n, "pct": aplicado, "valor": valor})
    total = min(total, nf)
    return {"partes": partes, "total": total, "pagar": nf - total, "nf": nf}


def parecer(c, mes, mensal, res):
    linhas = [
        f"• {p['nome']}: {p['viol']} de {p['n']} ocorrência(s) com descumprimento → "
        + (f"glosa de {pct(p['pct'])} ({brl(p['valor'])})" if p["valor"] else "sem glosa")
        for p in res["partes"]
    ]
    fecho = (
        "Solicitamos a emissão da NF no valor a faturar acima, já considerando as glosas. "
        "Em caso de contestação, favor encaminhar as evidências."
        if res["total"]
        else "Não há glosas neste período. A NF pode ser emitida pelo valor mensal do contrato."
    )
    return (
        f"Prezados ({c['fornecedor']}),\n\n"
        f"Segue o resultado da medição referente a {mes}.\n"
        f"Unidade: {c['unidade']}\nSistema: {c['sistema']}\nContrato: {c['contrato']}\n\n"
        f"Valor mensal do contrato: {brl(mensal)}\n\n"
        + "\n".join(linhas)
        + f"\n\nTotal de glosas: {brl(res['total'])}\nValor a faturar (NF): {brl(res['pagar'])}\n\n{fecho}\n\n"
        "Atenciosamente,\nGestão de Manutenção"
    )


# ------------------------------------------------------------------ e-mail
def smtp_cfg():
    host = secret("SMTP_HOST")
    if not host:
        return None
    return {
        "host": host,
        "port": int(secret("SMTP_PORT", 587)),
        "user": secret("SMTP_USER"),
        "password": secret("SMTP_PASSWORD"),
        "from": secret("SMTP_FROM", secret("SMTP_USER")),
        "cc": secret("SMTP_CC", ""),
    }


def enviar(cfg, destino, assunto, corpo):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["from"], destino, assunto
    if cfg["cc"]:
        msg["Cc"] = cfg["cc"]
    msg.set_content(corpo)
    ctx = ssl.create_default_context()
    if cfg["port"] == 465:
        with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx) as s:
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
    else:
        with smtplib.SMTP(cfg["host"], cfg["port"]) as s:
            s.starttls(context=ctx)
            s.login(cfg["user"], cfg["password"])
            s.send_message(msg)


def resumo_unidade(cad_u, mes):
    linhas = []
    for _, c in cad_u.iterrows():
        t = get_termos(c["unidade"], c["contrato"], c["fornecedor"])
        b = get_boletim(c["unidade"], c["contrato"], mes)
        d = {tp: load_occ(c["unidade"], c["contrato"], mes, tp) for tp in TIPOS}
        mensal = round(float(c["valor_anual"]) / 12, 2)
        r = calcular(t, mensal, d)
        linhas.append(
            {
                "Unidade": c["unidade"], "Sistema": c["sistema"], "Contrato": c["contrato"],
                "Fornecedor": c["fornecedor"], "Status": "Lançado" if b else "Pendente",
                "Valor mensal": mensal,
                "Corretivas": r["partes"][0]["valor"], "Preventivas": r["partes"][1]["valor"],
                "Relatórios": r["partes"][2]["valor"], "Glosa total": r["total"], "A faturar": r["pagar"],
            }
        )
    return pd.DataFrame(linhas)


def tabela_resumo(df):
    cols = ["Valor mensal", "Corretivas", "Preventivas", "Relatórios", "Glosa total", "A faturar"]
    cfg = {c: st.column_config.NumberColumn(format="R$ %.2f") for c in cols}
    st.dataframe(df, hide_index=True, use_container_width=True, column_config=cfg)


# ---------------------------------------------------------------- interface
st.set_page_config(page_title="Boletim de Medição", page_icon="🧾", layout="centered")
init()

senha = secret("APP_PASSWORD")
if senha and not st.session_state.get("ok"):
    st.title("Boletim de Medição")
    digitada = st.text_input("Senha de acesso", type="password")
    if st.button("Entrar"):
        if digitada == senha:
            st.session_state["ok"] = True
            st.rerun()
        else:
            st.error("Senha incorreta.")
    st.stop()

st.title("🧾 Boletim de Medição")

cad = cadastro()

with st.sidebar:
    mes = st.text_input("Mês de referência (AAAA-MM)", value=date.today().strftime("%Y-%m"))
    with st.expander("Cadastro de contratos"):
        st.caption(f"{len(cad)} contratos em {cad['unidade'].nunique() if len(cad) else 0} unidade(s).")
        up = st.file_uploader("Atualizar com nova planilha (.xlsx)", type=["xlsx"])
        if up is not None and st.button("Substituir cadastro"):
            try:
                importar_cadastro(normalizar(pd.read_excel(up)))
                st.success("Cadastro atualizado.")
                st.rerun()
            except Exception as e:
                st.error(f"Não foi possível ler a planilha: {e}")
        st.caption("Colunas: Unidade, Sistema, Fornecedor, Contrato, Valor anual.")

if cad.empty:
    st.warning("Nenhum contrato cadastrado. Envie a planilha no menu lateral (Cadastro de contratos).")
    st.stop()

# ---- Passo 1: unidade, sistema e contrato
st.subheader("1 · Unidade, sistema e contrato")
c1, c2 = st.columns(2)
unidade = c1.selectbox("Unidade", sorted(cad["unidade"].unique()))
cad_u = cad[cad["unidade"] == unidade]
sistema = c2.selectbox("Sistema", sorted(cad_u["sistema"].unique()))
cad_s = cad_u[cad_u["sistema"] == sistema]
if len(cad_s) > 1:
    contrato = st.selectbox("Contrato", list(cad_s["contrato"]))
    linha = cad_s[cad_s["contrato"] == contrato].iloc[0]
else:
    linha = cad_s.iloc[0]
contrato = linha["contrato"]
ctr = {"unidade": unidade, "sistema": sistema, "fornecedor": linha["fornecedor"], "contrato": contrato}
anual = float(linha["valor_anual"])
mensal = round(anual / 12, 2)
ck = f"{unidade}|{contrato}"
k = f"{ck}|{mes}"

i1, i2, i3 = st.columns(3)
i1.metric("Fornecedor", linha["fornecedor"] or "—")
i2.metric("Contrato", contrato)
i3.metric("Valor mensal", brl(mensal), help=f"Valor anual {brl(anual)} ÷ 12")

termos = get_termos(unidade, contrato, linha["fornecedor"])
with st.expander("Termos do contrato (glosas, SLA e e-mail)", expanded=not termos["email"]):
    termos["email"] = st.text_input("E-mail do fornecedor", termos["email"], key=f"e_{ck}")
    g1, g2, g3, g4 = st.columns(4)
    termos["pc"] = g1.number_input("Corretivas %", 0.0, 100.0, float(termos["pc"]), 0.5, key=f"pc_{ck}")
    termos["pp"] = g2.number_input("Preventivas %", 0.0, 100.0, float(termos["pp"]), 0.5, key=f"pp_{ck}")
    termos["pr"] = g3.number_input("Relatórios %", 0.0, 100.0, float(termos["pr"]), 0.5, key=f"pr_{ck}")
    termos["sla"] = g4.number_input("SLA padrão (h)", 0.0, 720.0, float(termos["sla"]), 1.0, key=f"sla_{ck}")
    termos["modo"] = st.radio(
        "Como aplicar a glosa",
        ["u", "o"],
        index=0 if termos["modo"] == "u" else 1,
        format_func=lambda x: "Uma vez, se houver ao menos 1 descumprimento" if x == "u" else "Por ocorrência (limitado a 100%)",
        key=f"modo_{ck}",
    )

# ---- Passo 2: ocorrências
st.subheader("2 · Ocorrências do mês")
abas = st.tabs(["Corretivas", "Preventivas", "Relatórios"])
dfs = {}
for aba, tipo in zip(abas, TIPOS):
    with aba:
        la, lb = ROTULOS[tipo]
        st.caption(
            {
                "c": "SLA h em branco = usa o SLA padrão. Sem data de atendimento = fora do SLA.",
                "p": "Sem data de execução = não executada no mês.",
                "r": "Sem data de entrega = não entregue.",
            }[tipo]
        )
        col = st.column_config.DatetimeColumn if tipo == "c" else st.column_config.DateColumn
        fmt = "DD/MM/YYYY HH:mm" if tipo == "c" else "DD/MM/YYYY"
        cfg = {"os": st.column_config.TextColumn("OS"), "a": col(la, format=fmt), "b": col(lb, format=fmt)}
        if tipo == "c":
            cfg["sla"] = st.column_config.NumberColumn("SLA h", min_value=0.0)
        dfs[tipo] = st.data_editor(
            load_occ(unidade, contrato, mes, tipo), column_config=cfg, num_rows="dynamic",
            use_container_width=True, hide_index=True, key=f"ed_{tipo}_{k}",
        )

if st.button("💾 Salvar boletim", type="primary"):
    termos.update({"unidade": unidade, "contrato": contrato})
    save_termos(termos)
    save_boletim(unidade, contrato, mes, mensal)
    for tipo in TIPOS:
        save_occ(unidade, contrato, mes, tipo, dfs[tipo])
    st.success("Boletim salvo.")

# ---- Passo 3: resultado e e-mail
st.subheader("3 · Resultado e envio")
res = calcular(termos, mensal, dfs)
m1, m2, m3 = st.columns(3)
m1.metric("Valor mensal", brl(mensal))
m2.metric("Glosas", brl(res["total"]))
m3.metric("A faturar (NF)", brl(res["pagar"]), help="Valor que o fornecedor deve usar na emissão da NF.")
for p in res["partes"]:
    st.write(
        f"**{p['nome']}** — {p['viol']} de {p['n']} com descumprimento · "
        + (f":red[− {brl(p['valor'])}] ({pct(p['pct'])})" if p["valor"] else ":green[Sem glosa]")
    )

texto = st.text_area("Parecer ao fornecedor (edite se precisar)", parecer(ctr, mes, mensal, res), height=360)
assunto = f"Boletim de Medição - {unidade} - {sistema} ({contrato}) - {mes}"
cfg_mail = smtp_cfg()
if not termos["email"]:
    st.info("Informe o e-mail do fornecedor em 'Termos do contrato'.")
elif cfg_mail:
    if st.button("✉ Enviar e-mail ao fornecedor"):
        try:
            enviar(cfg_mail, termos["email"], assunto, texto)
            st.success(f"E-mail enviado para {termos['email']}.")
        except Exception as e:
            st.error(f"Falha no envio: {e}")
else:
    st.warning("SMTP não configurado no servidor. Use o botão abaixo para abrir no seu app de e-mail.")
    st.link_button("✉ Abrir no app de e-mail", f"mailto:{termos['email']}?subject={quote(assunto)}&body={quote(texto)}")

# ---- Passo 4: resumo
st.subheader(f"4 · Resumo de {mes} · {unidade}")
res_u = resumo_unidade(cad_u, mes)
tabela_resumo(res_u)
pend = int((res_u["Status"] == "Pendente").sum())
st.caption(f"{pend} contrato(s) ainda pendente(s) nesta unidade." if pend else "Todos os contratos da unidade foram lançados.")
st.metric("Glosa total da unidade (dados salvos)", brl(float(res_u["Glosa total"].sum())))
st.download_button("Baixar resumo da unidade (CSV)", res_u.to_csv(index=False, sep=";", decimal=","), f"resumo_{unidade}_{mes}.csv", "text/csv")

with st.expander("Todas as unidades"):
    res_t = resumo_unidade(cad, mes)
    tabela_resumo(res_t)
    st.metric("Glosa total geral (dados salvos)", brl(float(res_t["Glosa total"].sum())))
    st.download_button("Baixar resumo geral (CSV)", res_t.to_csv(index=False, sep=";", decimal=","), f"resumo_geral_{mes}.csv", "text/csv", key="dl_geral")
