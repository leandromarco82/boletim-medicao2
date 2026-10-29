import os
import smtplib
import sqlite3
import ssl
from datetime import date
from email.message import EmailMessage
from urllib.parse import quote

import pandas as pd
import streamlit as st

DB = os.environ.get("BOLETIM_DB", "boletim.db")

SISTEMAS = [
    "Gerador",
    "Sistema Elétrico",
    "SDAI",
    "Elevadores",
    "Nobreaks",
    "Combate Convencional de Incêndio",
]
TIPOS = {
    "c": "Corretivas fora do SLA",
    "p": "Preventivas fora do prazo ou não executadas",
    "r": "Relatórios fora do prazo ou não entregues",
}
PCT_KEY = {"c": "pc", "p": "pp", "r": "pr"}
ROTULOS = {"c": ("Abertura", "Atendimento"), "p": ("Prazo", "Execução"), "r": ("Prazo", "Entrega")}


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


# ------------------------------------------------------------------- banco
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS contratos(
                sistema TEXT PRIMARY KEY, fornecedor TEXT DEFAULT '', email TEXT DEFAULT '',
                pc REAL DEFAULT 3, pp REAL DEFAULT 5, pr REAL DEFAULT 2,
                sla REAL DEFAULT 4, modo TEXT DEFAULT 'u');
            CREATE TABLE IF NOT EXISTS boletins(
                sistema TEXT, mes TEXT, nf REAL DEFAULT 0, PRIMARY KEY(sistema, mes));
            CREATE TABLE IF NOT EXISTS ocorrencias(
                id INTEGER PRIMARY KEY AUTOINCREMENT, sistema TEXT, mes TEXT,
                tipo TEXT, os TEXT, a TEXT, b TEXT, sla REAL);
            """
        )


def get_contrato(sistema):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO contratos(sistema) VALUES (?)", (sistema,))
        return dict(con.execute("SELECT * FROM contratos WHERE sistema=?", (sistema,)).fetchone())


def save_contrato(c):
    with db() as con:
        con.execute(
            "UPDATE contratos SET fornecedor=?, email=?, pc=?, pp=?, pr=?, sla=?, modo=? WHERE sistema=?",
            (c["fornecedor"], c["email"], c["pc"], c["pp"], c["pr"], c["sla"], c["modo"], c["sistema"]),
        )


def get_nf(sistema, mes):
    with db() as con:
        r = con.execute("SELECT nf FROM boletins WHERE sistema=? AND mes=?", (sistema, mes)).fetchone()
    return float(r["nf"]) if r else 0.0


def save_nf(sistema, mes, nf):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO boletins(sistema, mes, nf) VALUES (?,?,?)", (sistema, mes, nf))


def vazio(tipo):
    d = {
        "os": pd.Series(dtype="str"),
        "a": pd.Series(dtype="datetime64[ns]"),
        "b": pd.Series(dtype="datetime64[ns]"),
    }
    if tipo == "c":
        d["sla"] = pd.Series(dtype="float")
    return pd.DataFrame(d)


def load_occ(sistema, mes, tipo):
    with db() as con:
        rows = con.execute(
            "SELECT os, a, b, sla FROM ocorrencias WHERE sistema=? AND mes=? AND tipo=? ORDER BY id",
            (sistema, mes, tipo),
        ).fetchall()
    if not rows:
        return vazio(tipo)
    df = pd.DataFrame([dict(r) for r in rows])
    df["a"] = pd.to_datetime(df["a"], errors="coerce")
    df["b"] = pd.to_datetime(df["b"], errors="coerce")
    return df if tipo == "c" else df.drop(columns=["sla"])


def save_occ(sistema, mes, tipo, df):
    with db() as con:
        con.execute("DELETE FROM ocorrencias WHERE sistema=? AND mes=? AND tipo=?", (sistema, mes, tipo))
        for _, r in df.iterrows():
            a, b = pd.to_datetime(r.get("a"), errors="coerce"), pd.to_datetime(r.get("b"), errors="coerce")
            os_ = r.get("os")
            if pd.isna(a) and pd.isna(b) and (os_ is None or pd.isna(os_) or not str(os_).strip()):
                continue
            sla = r.get("sla")
            con.execute(
                "INSERT INTO ocorrencias(sistema, mes, tipo, os, a, b, sla) VALUES (?,?,?,?,?,?,?)",
                (
                    sistema, mes, tipo,
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
        return True  # não atendida / não executada / não entregue
    if tipo == "c":
        horas = (b - a).total_seconds() / 3600
        sla = row.get("sla")
        limite = sla if sla is not None and pd.notna(sla) and sla > 0 else sla_padrao
        return horas > limite
    return b.normalize() > a.normalize()


def calcular(contrato, nf, dfs):
    partes, total = [], 0.0
    for tipo, nome in TIPOS.items():
        viol, n = 0, 0
        for _, row in dfs[tipo].iterrows():
            r = atrasado(tipo, row, contrato["sla"])
            if r is None:
                continue
            n += 1
            viol += int(r)
        p = float(contrato[PCT_KEY[tipo]])
        aplicado = 0.0 if viol == 0 else (p if contrato["modo"] == "u" else min(100.0, p * viol))
        valor = nf * aplicado / 100
        total += valor
        partes.append({"tipo": tipo, "nome": nome, "viol": viol, "n": n, "pct": aplicado, "valor": valor})
    total = min(total, nf)
    return {"partes": partes, "total": total, "pagar": nf - total, "nf": nf}


def parecer(sistema, mes, contrato, res):
    linhas = [
        f"• {p['nome']}: {p['viol']} de {p['n']} ocorrência(s) com descumprimento → "
        + (f"glosa de {pct(p['pct'])} ({brl(p['valor'])})" if p["valor"] else "sem glosa")
        for p in res["partes"]
    ]
    forn = f" ({contrato['fornecedor']})" if contrato["fornecedor"] else ""
    fecho = (
        "Solicitamos a emissão de NF/ajuste considerando as glosas acima. "
        "Em caso de contestação, favor encaminhar as evidências."
        if res["total"]
        else "Não há glosas neste período."
    )
    return (
        f"Prezados{forn},\n\nSegue o resultado da medição de {sistema} referente a {mes}.\n\n"
        f"Valor da NF: {brl(res['nf'])}\n\n" + "\n".join(linhas) + "\n\n"
        f"Total de glosas: {brl(res['total'])}\nValor a pagar: {brl(res['pagar'])}\n\n{fecho}\n\n"
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

with st.sidebar:
    sistema = st.selectbox("Sistema", SISTEMAS)
    mes = st.text_input("Mês de referência (AAAA-MM)", value=date.today().strftime("%Y-%m"))
    st.caption("Os dados ficam salvos no servidor e são compartilhados por todos que acessarem.")

contrato = get_contrato(sistema)
k = f"{sistema}_{mes}"

with st.expander(f"Contrato · {sistema}", expanded=not contrato["fornecedor"]):
    c1, c2 = st.columns(2)
    contrato["fornecedor"] = c1.text_input("Fornecedor", contrato["fornecedor"], key=f"f_{sistema}")
    contrato["email"] = c2.text_input("E-mail do fornecedor", contrato["email"], key=f"e_{sistema}")
    g1, g2, g3, g4 = st.columns(4)
    contrato["pc"] = g1.number_input("Corretivas %", 0.0, 100.0, float(contrato["pc"]), 0.5, key=f"pc_{sistema}")
    contrato["pp"] = g2.number_input("Preventivas %", 0.0, 100.0, float(contrato["pp"]), 0.5, key=f"pp_{sistema}")
    contrato["pr"] = g3.number_input("Relatórios %", 0.0, 100.0, float(contrato["pr"]), 0.5, key=f"pr_{sistema}")
    contrato["sla"] = g4.number_input("SLA padrão (h)", 0.0, 720.0, float(contrato["sla"]), 1.0, key=f"sla_{sistema}")
    contrato["modo"] = st.radio(
        "Como aplicar a glosa",
        ["u", "o"],
        index=0 if contrato["modo"] == "u" else 1,
        format_func=lambda x: "Uma vez, se houver ao menos 1 descumprimento" if x == "u" else "Por ocorrência (limitado a 100%)",
        key=f"modo_{sistema}",
    )

nf = st.number_input("Valor da NF (R$)", min_value=0.0, value=get_nf(sistema, mes), step=100.0, format="%.2f", key=f"nf_{k}")

abas = st.tabs(["1 · Corretivas", "2 · Preventivas", "3 · Relatórios"])
dfs = {}
for aba, tipo in zip(abas, TIPOS):
    with aba:
        la, lb = ROTULOS[tipo]
        if tipo == "c":
            st.caption("SLA h em branco = usa o SLA padrão. Sem data de atendimento = fora do SLA.")
        elif tipo == "p":
            st.caption("Sem data de execução = não executada no mês.")
        else:
            st.caption("Sem data de entrega = não entregue.")
        col = st.column_config.DatetimeColumn if tipo == "c" else st.column_config.DateColumn
        fmt = "DD/MM/YYYY HH:mm" if tipo == "c" else "DD/MM/YYYY"
        cfg = {
            "os": st.column_config.TextColumn("OS"),
            "a": col(la, format=fmt),
            "b": col(lb, format=fmt),
        }
        if tipo == "c":
            cfg["sla"] = st.column_config.NumberColumn("SLA h", min_value=0.0)
        dfs[tipo] = st.data_editor(
            load_occ(sistema, mes, tipo),
            column_config=cfg,
            num_rows="dynamic",
            use_container_width=True,
            hide_index=True,
            key=f"ed_{tipo}_{k}",
        )

if st.button("💾 Salvar boletim", type="primary"):
    contrato["sistema"] = sistema
    save_contrato(contrato)
    save_nf(sistema, mes, nf)
    for tipo in TIPOS:
        save_occ(sistema, mes, tipo, dfs[tipo])
    st.success("Boletim salvo.")

# ---------------------------------------------------------------- resultado
res = calcular(contrato, nf, dfs)
st.subheader("Resultado")
m1, m2 = st.columns(2)
m1.metric("Total de glosas", brl(res["total"]))
m2.metric("Valor a pagar", brl(res["pagar"]))
for p in res["partes"]:
    st.write(
        f"**{p['nome']}** — {p['viol']} de {p['n']} com descumprimento · "
        + (f":red[− {brl(p['valor'])}] ({pct(p['pct'])})" if p["valor"] else ":green[Sem glosa]")
    )

st.subheader("Parecer ao fornecedor")
texto = st.text_area("Edite se precisar antes de enviar", parecer(sistema, mes, contrato, res), height=320)
assunto = f"Boletim de Medição - {sistema} - {mes}"
cfg_mail = smtp_cfg()
if not contrato["email"]:
    st.info("Informe o e-mail do fornecedor no bloco Contrato.")
elif cfg_mail:
    if st.button("✉ Enviar e-mail ao fornecedor"):
        try:
            enviar(cfg_mail, contrato["email"], assunto, texto)
            st.success(f"E-mail enviado para {contrato['email']}.")
        except Exception as e:
            st.error(f"Falha no envio: {e}")
else:
    st.warning("SMTP não configurado no servidor. Use o botão abaixo para abrir no seu app de e-mail.")
    st.link_button("✉ Abrir no app de e-mail", f"mailto:{contrato['email']}?subject={quote(assunto)}&body={quote(texto)}")

# ------------------------------------------------------------------- resumo
st.subheader(f"Resumo de {mes} · todos os sistemas")
linhas = []
for s in SISTEMAS:
    c = get_contrato(s)
    d = {t: load_occ(s, mes, t) for t in TIPOS}
    r = calcular(c, get_nf(s, mes), d)
    linhas.append(
        {
            "Sistema": s,
            "Fornecedor": c["fornecedor"],
            "NF": r["nf"],
            "Corretivas": r["partes"][0]["valor"],
            "Preventivas": r["partes"][1]["valor"],
            "Relatórios": r["partes"][2]["valor"],
            "Glosa total": r["total"],
            "A pagar": r["pagar"],
        }
    )
resumo = pd.DataFrame(linhas)
st.dataframe(
    resumo,
    hide_index=True,
    use_container_width=True,
    column_config={c: st.column_config.NumberColumn(format="R$ %.2f") for c in resumo.columns[2:]},
)
st.metric("Total geral de glosas (dados salvos)", brl(float(resumo["Glosa total"].sum())))
st.download_button("Baixar resumo (CSV)", resumo.to_csv(index=False, sep=";", decimal=","), f"resumo_{mes}.csv", "text/csv")
