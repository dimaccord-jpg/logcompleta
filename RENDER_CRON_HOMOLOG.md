# Render + Cron em Homolog

Referência: `homolog@6701a53`, 2026-07-16.

## Premissas

Configure `APP_ENV=homolog`, `DATABASE_URL`, `CRON_SECRET`, `APP_BASE_URL`, storage persistente e migrations do ambiente.

Os endpoints oficiais são `POST /cron/executar-cleiton`, `POST /cron/finance` e `POST /cron/billing-snapshot`, com header `X-Cron-Secret`. Sem segredo ou com header inválido, a resposta esperada é `403`; com segredo válido, `200`. Use `curl -f` para expor falhas HTTP. `?secret=` é compatibilidade temporária.

Jobs não dependem de `app/cleiton_doc_tmp/`, `tt_*.json` ou banco local.

## Branches

Homologação usa `homolog`. Produção operacional usa `producao`, promovida em `0c3a133`, com backup `backup/producao-antes-cleide-insights-20260716`.

O `render.yaml` ainda declara produção em `main` e `autoDeploy: false`. É divergência a conferir no painel; não torna `main` a branch operacional.

Esta entrega não criou migration, tabela ou coluna. Ver `README.md` e `app/README_DEPLOY.md`.
