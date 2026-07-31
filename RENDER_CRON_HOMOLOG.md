# Render + Cron em Homolog

Referência auditada em 2026-07-29.

## Cron confirmado no código

- `GET|POST /cron/executar-cleiton`
- `GET|POST /cron/finance`
- `GET|POST /cron/billing-snapshot`

Autenticação:

- oficial: header `X-Cron-Secret`;
- compatibilidade temporária: `?secret=`.

## Boot do serviço

- `start.sh` infere `APP_ENV` quando necessário;
- `start.sh` trata `main`, `master`, `producao` e `prod` como produção;
- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn.

## Pontos operacionais a validar

- homolog continua versionado em `homolog` no `render.yaml`;
- produção continua versionada em `main` no `render.yaml`;
- o processo operacional informado usa `producao` como branch de promoção para produção;
- a última entrega conhecida foi homologada em `81d36aa` e promovida por `cherry-pick` para `6b0672e`;
- o YAML ainda mantém `healthCheckPath: /health`, enquanto o código expõe `/health/liveness` e `/health/readiness`.