# Deploy, Ambientes e Promoção

Documentação auditada em 2026-09-04 a partir de `render.yaml`, `start.sh`, `build.sh` e da configuração versionada do repositório.

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

## PRECONDIÇÃO BLOQUEANTE DE DEPLOY — Plano Multiusuário Fase 1

A migration `a2b3c4d5e6f7` (fundação persistente Multiuser) **não pode ser promovida isoladamente** para homologação compartilhada persistente nem para produção.

Motivo: o `start.sh` executa `flask db upgrade` no boot. Promover só a Fase 1 aplicaria o schema e o backfill, mas o fluxo administrativo legado ainda consegue criar Multiuser sem gravar `multiuser_ativa`, quantity e vínculo organizacional. Esses registros novos não participam do backfill da migration.

Condição obrigatória:

- as oito fases do Plano Multiusuário devem estar implementadas e auditadas antes da validação humana e do release final;
- a reconciliação reexecutável (`aplicar_backfill_multiuser_legado`) deve ser reutilizada pela Fase 2 **antes** de ativar o enforcement operacional;
- somente então o conjunto pode integrar o mesmo release seguro.

Esta é condição de deploy, não regra nova de produto. A Fase 1 isolada não é migration segura para promoção.

A Fase 2 fecha tecnicamente a janela H-03 no código (reconciliação reexecutável antes do write admin governado). Isso **não autoriza** promoção isolada de F1+F2: o conjunto das oito fases continua pendente de auditoria e release.

## Sequência de migrations do Plano Multiusuário

Quando o conjunto estiver autorizado para o mesmo release, a ordem de upgrade é:

1. `a2b3c4d5e6f7` — Fase 1 (fundação)
2. `b3c4d5e6f7a8` — Fase 3 (intenção de checkout)
3. `c4d5e6f7a8b9` — Fase 4 (convites)
4. `d5e6f7a8b9c0` — Fase 5 (painel do Contratante e aumento automático)
5. `e6f7a8b9c0d1` — Fase 6 (aumento excepcional e cobrança extraordinária)
6. `f7g8h9i0j1k2` — Fase 7 (lifecycle comercial: redução futura, revogação, notificação e titularidade)

A Fase 7 não altera a política geral de deploy nem autoriza promoção isolada das fases incompletas.
