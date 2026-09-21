# Render + Cron em Homolog

Referência revisada em 2026-09-19 para o release Multiuser V1. Deploy canônico em [DEPLOYMENT](docs/DEPLOYMENT.md).

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
- produção usa `producao` e `APP_ENV=prod`; homolog usa `APP_ENV=homolog`;
- Render mantém serviços separados com Auto-Deploy por commit em ambos;
- `healthCheckPath: /health` corresponde à rota existente; o código também expõe `/health/liveness` e `/health/readiness`;
- configurar cron com host e segredo do serviço-alvo, sem reutilizar credenciais de produção em homolog.
