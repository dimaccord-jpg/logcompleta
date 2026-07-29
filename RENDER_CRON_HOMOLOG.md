# Render + Cron em Homolog

Referência auditada em 2026-07-28.

## Cron confirmado no código

- `POST /cron/executar-cleiton`
- `POST /cron/finance`
- `POST /cron/billing-snapshot`

Autenticação:

- oficial: header `X-Cron-Secret`;
- compatibilidade temporária: `?secret=`.

## Boot do serviço

- `start.sh` infere `APP_ENV` quando necessário;
- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn.

## Pontos operacionais a validar

- homolog continua versionado em `homolog` no `render.yaml`;
- produção continua versionada em `main` no `render.yaml`;
- o processo operacional informado usa `producao` como branch de produção;
- o YAML ainda mantém `healthCheckPath: /health`, enquanto o código expõe `/health/liveness` e `/health/readiness`.
