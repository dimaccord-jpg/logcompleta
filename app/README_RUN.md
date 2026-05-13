# Guia de Execucao Local

Este arquivo e apenas um anexo operacional curto.
A fonte principal do cenario atual do projeto e o `README.md` da raiz.

## Subida Local

Pre-requisitos minimos:

- `pip install -r requirements.txt`
- `DATABASE_URL` valido
- `APP_ENV` definido explicitamente
- `APP_DATA_DIR` definido quando quiser reproduzir localmente o comportamento persistente de documentos legais
- ambiente carregado a partir de `app/.env.example`

PowerShell:

```powershell
$env:APP_ENV="dev"
python -m app.web
```

## Comandos Uteis

- web: `python -m app.web`
- indices financeiros: `python -m app.finance`
- cron financeiro HTTP: `POST /cron/finance` com `X-Cron-Secret`
- ciclo Cleiton: `python -m app.run_cleiton`
- testes Roberto/Cleiton:
  - `pytest tests/test_roberto_controles.py`
  - `pytest tests/test_cleiton_upload_billing_service.py`
  - `pytest tests/test_franquia_operacao_autorizacao_service.py`
- testes minimos pos-alteracoes recentes:
  - `python -m pytest tests/test_login_terms_link.py tests/test_user_area_checkout_feedback.py tests/test_franquia_operacao_autorizacao_service.py tests/test_legal_documents_persistent_storage.py -q`
- suites Stripe/monetizacao relevantes quando o assunto for checkout, downgrade ou guardrails:
  - `pytest tests/test_cleiton_monetizacao_stripe_guardrails.py`
  - `pytest tests/test_cleiton_monetizacao_stripe_blockers.py`

## Referencia Principal

Nao replique regras funcionais aqui.
Atualize primeiro o `README.md` da raiz e mantenha este arquivo como lembrete operacional.
Isso inclui mudancas recentes de experiencia visual quando elas afetarem o comportamento percebido do produto.
