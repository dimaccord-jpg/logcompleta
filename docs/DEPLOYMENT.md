# Deploy, Ambientes e Promoção

Documentação operacional auditada contra a árvore de produção `bd874ee`, `render.yaml`, `start.sh` e `build.sh`.

## Ambientes e branches

No `render.yaml` atual:

- homologação: branch `homolog`
- produção: branch `producao`
- domínio conhecido de homologação: `homolog0514.agentefrete.com.br`
- domínio de produção: `www.agentefrete.com.br`
- ambos os serviços usam `autoDeploy: true`

Mapeamento de ambiente:

- `APP_ENV=homolog` para homologação
- `APP_ENV=prod` para produção
- `APP_ENV` válido: `dev`, `homolog`, `prod`

## Build e start reais

`build.sh` e `start.sh` inferem `APP_ENV` pelo branch quando necessário:

- `homolog` → `homolog`
- `main|master|producao|prod` → `prod`
- qualquer outro branch → `dev`

Fluxo real de start:

1. validar `APP_ENV`
2. executar `python -m flask --app app.web db upgrade`
3. subir `gunicorn --config gunicorn_config.py app.web:app`

## Consequência operacional

- migrations pendentes são aplicadas antes do boot da aplicação
- upgrade faz parte do deploy normal
- downgrade não é procedimento normal de deploy

## Persistência obrigatória

Em homolog/prod, o projeto depende de storage persistente para:

- uploads e artefatos técnicos
- índices
- documentos legais ativos

Entradas relevantes:

- `APP_DATA_DIR`
- `INDICES_FILE_PATH`
- `DATABASE_URL`

## Health checks

- `healthCheckPath` no Render: `/health`
- a aplicação também expõe `/health/liveness` e `/health/readiness`

## Fluxo cauteloso de promoção

O procedimento atual de promoção é:

1. desenvolver em branch própria
2. promover para `homolog`
3. validar em homologação
4. atualizar a referência remota de `producao`
5. verificar divergência
6. promover por fast-forward only
7. fazer push em `producao`
8. deixar o deploy automático ocorrer

Orientações:

- não recomendar `force push`
- não tratar downgrade de banco como rotina de promoção
- não substituir validação de homologação por merge destrutivo

## Promoção e rollback

Validar `homolog`, fazer merge controlado em `producao`, push, conferir o deploy no Render, `/health` e smoke test de login, sidebar, plano/créditos, `/perfil` e telas principais. O Multiuser V1 e a Sprint 11 já integram o produto; a identificação do commit implantado fica em [estado de produção](estado_producao.md).

Antes de uma promoção, registrar uma tag imutável no commit de produção anterior. Em caso de rollback, identificar a tag anterior, restaurar o commit pelo fluxo de promoção controlada e conferir compatibilidade de banco antes do deploy. A tag `pre-sprint11-prod-20260921` identifica apenas o ponto anterior à Sprint 11; não é convenção permanente.

O head versionado é `f7g8h9i0j1k2`. A cadeia física está em [Banco e Migrations](DATABASE_AND_MIGRATIONS.md); não há migration Fase 8. Novos deploys aplicam os upgrades pendentes antes do Gunicorn.

Render mantém serviços separados e Auto-Deploy por commit também em produção. Enviar commits para `producao` pode iniciar deploy.

A configuração comercial está no [guia Multiuser V1](multiuser_v1.md). Limitações atuais e evidência financeira estão no [estado de produção](estado_producao.md#pendências-conhecidas-e-pós-release).
