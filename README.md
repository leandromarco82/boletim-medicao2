# Boletim de Medição – Glosas (Streamlit)

Controle mensal de glosas na NF por sistema: Gerador, Sistema Elétrico, SDAI,
Elevadores, Nobreaks e Combate Convencional de Incêndio.

## Rodar no seu computador
```
pip install -r requirements.txt
streamlit run app.py
```
Abre em http://localhost:8501. Os dados ficam no arquivo `boletim.db` (SQLite).

## Variáveis / segredos (opcionais)
Em servidor, defina como variáveis de ambiente. No Streamlit Community Cloud,
cole em *Settings → Secrets* (formato TOML, ex.: `SMTP_HOST = "smtp.gmail.com"`).

| Nome | Para quê |
|---|---|
| `APP_PASSWORD` | Senha de acesso ao app. Sem ela, qualquer um com o link entra. |
| `SMTP_HOST`, `SMTP_PORT` | Servidor de e-mail (587 STARTTLS ou 465 SSL). |
| `SMTP_USER`, `SMTP_PASSWORD` | Login do e-mail. No Gmail, use "senha de app". |
| `SMTP_FROM`, `SMTP_CC` | Remetente e cópia (opcionais). |
| `BOLETIM_DB` | Caminho do arquivo do banco (ex.: `/data/boletim.db`). |

Sem SMTP configurado, o app mostra o botão "Abrir no app de e-mail" (mailto).

## Publicar
**Importante:** o SQLite precisa de disco que não seja apagado.
- **Render** (Web Service + Persistent Disk): build `pip install -r requirements.txt`,
  start `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`,
  disco montado em `/data` e `BOLETIM_DB=/data/boletim.db`.
- **Railway**: igual, com um Volume.
- **Streamlit Community Cloud**: mais fácil, mas o disco é temporário (dados podem
  sumir ao reiniciar). Serve para testar; para uso real, troque o SQLite por
  PostgreSQL (Supabase/Neon, gratuitos).

## Regras de cálculo
- Corretiva: fora do SLA se (atendimento − abertura) > SLA em horas, ou se não houver atendimento.
- Preventiva / relatório: fora do prazo se a data real > prazo, ou se estiver em branco.
- Glosa "uma vez": aplica o % cheio se houver ao menos 1 descumprimento.
  "Por ocorrência": % × nº de descumprimentos, limitado a 100%. A soma nunca passa do valor da NF.
