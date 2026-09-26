# Deploy, Ambientes e Promoção

Documentação operacional auditada contra a árvore de produção `fa98317` (Sprint 12), `render.yaml`, `start.sh` e `build.sh`.

## Ambientes e branches

No `render.yaml` atual:

- homologação: branch `homolog`
- produção: branch `producao`
- domínio conhecido de homologação: `homolog0514.agentefrete.com.br`
- domínio de produção: `www.agentefrete.com.br`
- ambos os serviços usam `autoDeploy: true`

Mapeamento de ambiente:

| Ambiente | Branch típica | `APP_ENV` |
|---|---|---|
| DEV | feature / local | `dev` |
| HOMOLOG | `homolog` | `homolog` |
| PROD | `producao` | `prod` |

`APP_ENV` válido: `dev`, `homolog`, `prod`.

## Precedência de env

1. Variáveis já definidas no processo (ex.: painel Render) prevalecem.
2. `load_app_env()` carrega somente `app/.env.{APP_ENV}` com `override=False`.
3. Arquivos `.env.*` locais podem estar gitignored e **não** são fonte autoritativa de produção.
4. Divergência entre `.env.prod` local e Render: prevalece o remoto.

Não registrar API keys na documentação.

## Build e start reais

### `build.sh`

- instala dependências de runtime (`requirements.txt`)
- **não** contém a lógica de inferência de `APP_ENV` por branch (essa lógica está em `start.sh`)

### `start.sh`

Quando `APP_ENV` não vem explícito, infere pelo branch (`RENDER_GIT_BRANCH`):

- `homolog` → `homolog`
- `main|master|producao|prod` → `prod`
- qualquer outro → `dev`

Fluxo de start:

1. validar `APP_ENV`
2. `python -m flask --app app.web db upgrade`
3. `gunicorn --config gunicorn_config.py app.web:app`

## Consequência operacional

- migrations pendentes aplicam-se antes do boot
- upgrade faz parte do deploy normal
- downgrade não é procedimento normal
- não há pipeline CI/CD separado que execute testes automaticamente no `build.sh`; a promoção permanece operacional/manual com Auto-Deploy por push na branch do serviço

## Persistência obrigatória

Em homolog/prod:

- `APP_DATA_DIR`
- `INDICES_FILE_PATH`
- `DATABASE_URL` (PostgreSQL)

## Health checks e smoke

- `healthCheckPath` no Render: `/health`
- também: `/health/liveness`, `/health/readiness`
- smoke sugerido pós-deploy: login, shell/habilidades, tema, `/perfil`, planos, `/fretes` (auth), Feed, chat operacional AgenteFrete
- validar HEAD implantado contra o commit esperado (`fa98317` na Sprint 12, ou o commit promovido depois)

## Fluxo cauteloso de promoção

Procedimento atual:

1. desenvolver em branch própria
2. promover para `homolog`
3. validar em homologação
4. atualizar a referência remota de `producao`
5. verificar divergência (`git log` / diff FF)
6. promover por **fast-forward only**
7. push em `producao`
8. deixar o Auto-Deploy ocorrer
9. após validar produção, retornar o trabalho ativo para `homolog` quando for o fluxo do time

Orientações:

- não recomendar `force push`
- não tratar downgrade de banco como rotina
- não substituir validação de homologação por merge destrutivo

## Promoção e rollback

Validar `homolog`, fazer merge controlado em `producao`, push, conferir deploy no Render, `/health` e smoke. Identificação do commit implantado: [estado de produção](estado_producao.md).

Antes de uma promoção, registrar tag imutável no commit de produção anterior. Em rollback, restaurar pelo fluxo controlado e conferir compatibilidade de banco. A tag `pre-sprint11-prod-20260921` identifica apenas o ponto anterior à Sprint 11.

Head versionado: `g8h9i0j1k2l3`, com `down_revision=f7g8h9i0j1k2`. A migration Growth do SCRUM-148 altera apenas a nulabilidade da identidade em `FunnelEvent`. Cadeia em [Banco e Migrations](DATABASE_AND_MIGRATIONS.md).

Enviar commits para `producao` inicia deploy (Auto-Deploy).

Configuração comercial: [Multiuser V1](multiuser_v1.md). Limitações: [estado de produção](estado_producao.md#pendências-conhecidas-e-pós-release).
