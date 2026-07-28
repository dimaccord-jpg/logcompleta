# Render + Cron em Homolog

Referência auditada em 2026-07-20.

## Premissas

Configurar no ambiente:

- `APP_ENV=homolog`
- `DATABASE_URL`
- `CRON_SECRET`
- storage persistente
- cadeia de migrations existente

## Cron oficial

Endpoints confirmados no código:

- `POST /cron/executar-cleiton`
- `POST /cron/finance`
- `POST /cron/billing-snapshot`

Autenticação:

- oficial: header `X-Cron-Secret`
- compatibilidade temporária: `?secret=`

Sem segredo válido, a resposta esperada é `403`. Com segredo válido, `200`.

## Observações de infraestrutura

- jobs não dependem de `.db` local nem de `app/cleiton_doc_tmp/`;
- `start.sh` aplica migrations antes do Gunicorn;
- `render.yaml` versionado declara homolog em `homolog` e produção em `main`.

## Divergências vigentes

- o processo operacional informado trata `producao` como branch de produção;
- o YAML versionado ainda aponta produção para `main`;
- o YAML usa `healthCheckPath: /health`, mas o código expõe `/health/liveness` e `/health/readiness`.
