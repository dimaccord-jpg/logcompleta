# Deploy e Promoção

Referência auditada em 2026-07-29. A visão consolidada do estado atual está em `docs/estado_oficial_consolidado.md`.

## O que o repositório comprova

- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn;
- `start.sh` infere `APP_ENV=prod` para `main`, `master`, `producao` e `prod`;
- `render.yaml` versionado declara homolog em `homolog` com `autoDeploy: true`;
- `render.yaml` versionado declara produção em `main` com `autoDeploy: false`;
- o código expõe `/health/liveness` e `/health/readiness`.

## O que o processo operacional informado registra

- desenvolvimento e validação em `homolog`;
- entrega homologada no commit `81d36aa`;
- promoção para produção por `cherry-pick`, com hash equivalente `6b0672e`;
- deploy de produção manual no Render.

## Fluxo operacional recomendado

1. validar `git status -sb`, branch atual e commit auditado;
2. validar testes e checagens necessárias da entrega;
3. confirmar que não houve mudança de schema fora das migrations versionadas;
4. validar homologação na branch `homolog`;
5. promover para a branch operacional de produção adotada pelo time, preservando a equivalência de conteúdo;
6. executar o deploy manual de produção no Render;
7. validar logs, health check e fluxos críticos após o deploy.

## Banco e migrations

A entrega recente do AgenteCompara não criou migration, tabela ou coluna nova. Ainda assim, o deploy continua dependente do `db upgrade` no boot para manter a cadeia Alembic já versionada aplicada.

## Divergências operacionais vigentes

- o processo informado trata `producao` como branch operacional de produção;
- o `render.yaml` versionado ainda aponta produção para `main`;
- o YAML segue com `healthCheckPath: /health`, enquanto o código expõe `/health/liveness` e `/health/readiness`.

Esses pontos devem ser tratados como validações obrigatórias antes de qualquer novo deploy.