# Deploy, Ambientes e Promoção

Documentação revisada em 2026-09-19 a partir de `render.yaml`, `start.sh`, `build.sh` e da configuração versionada do repositório.

## Ambientes e branches

No `render.yaml` atual:

- homologação: branch `homolog`
- produção: branch `producao`
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

## Release Multiuser V1 implantado

O conjunto Multiuser V1 já está em produção no release `f28f28e`. As antigas condições de promoção de fases isoladas não descrevem o estado atual. Reconciliação do legado e enforcement organizacional já integram o runtime.

O head versionado é `f7g8h9i0j1k2`. A cadeia física está em [Banco e Migrations](DATABASE_AND_MIGRATIONS.md); não há migration Fase 8. Novos deploys aplicam os upgrades pendentes antes do Gunicorn.

Render mantém serviços separados e Auto-Deploy por commit também em produção. Enviar commits para `producao` pode iniciar deploy.

A configuração comercial está no [guia Multiuser V1](multiuser_v1.md). Pendências de UX e teste financeiro real deliberadamente adiado estão no [pós-release](estado_producao.md#pendências-conhecidas-e-pós-release).
