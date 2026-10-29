# Boletim de Medição – Glosas (Streamlit) · versão 2

Fluxo: **Unidade → Sistema → Contrato → Ocorrências → Glosa e e-mail → Resumo**.
Os contratos vêm da planilha `contratos.xlsx` (colunas: Unidade, Sistema, Fornecedor, Contrato, Valor anual).

## O que mudou na v2 (atualização sem NF)
- Cada unidade escolhe seu sistema; fornecedor, contrato e valor anual vêm da planilha.
- Sem campo de NF: o boletim é feito antes da emissão. O app calcula as glosas sobre o valor mensal do contrato (anual ÷ 12) e informa o valor a faturar.
- Glosas, SLA e e-mail são guardados por unidade + contrato (o mesmo código de contrato pode existir em unidades diferentes).
- Resumo por unidade, com status Lançado/Pendente, e resumo de todas as unidades (CSV).
- Planilha atualizável pelo menu lateral (Cadastro de contratos), sem mexer no código.

## Como atualizar no GitHub
No repositório, use *Add file → Upload files* e envie: `app.py`, `requirements.txt`,
`contratos.xlsx` e `README.md` (confirme a substituição). O Streamlit atualiza sozinho.
Os dados de teste da versão 1 não são aproveitados (a v2 usa tabelas novas).

## Rodar no computador
```
pip install -r requirements.txt
streamlit run app.py
```

## Segredos (opcionais)
Em *Settings → Secrets* (Streamlit Cloud) ou variáveis de ambiente:
`APP_PASSWORD`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_CC`, `BOLETIM_DB`.
Sem SMTP, o app mostra o botão "Abrir no app de e-mail".

## Atenção
O Streamlit Community Cloud apaga o disco ao reiniciar. Para uso oficial, publique no
Render/Railway com disco persistente ou troque o SQLite por PostgreSQL (Supabase/Neon).

## Regras de cálculo
- Corretiva: fora do SLA se (atendimento − abertura) > SLA em horas, ou sem atendimento.
- Preventiva/relatório: fora do prazo se a data real > prazo, ou em branco.
- Glosa "uma vez": % cheio se houver ao menos 1 descumprimento. "Por ocorrência": % × nº de descumprimentos (máx. 100%). A soma nunca passa do valor da NF.
