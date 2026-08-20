# Arquitetura do Produto

Referência auditada em 2026-08-19. A fonte de verdade é o código atual.

## Identidade do produto

- produto: Agente Frete
- empresa: `Logcompleta Agentes Inteligentes LTDA`
- domínio principal: `https://www.agentefrete.com.br/`
- stack principal: Flask + templates + Bootstrap

## Experiência pública vs autenticada

- antes do login, a home apresenta o Copiloto público;
- antes do login, a home não apresenta Júlia como experiência principal;
- após login, a Júlia passa a ser a experiência principal;
- a aplicação mantém na mesma base as superfícies pública, autenticada, administrativa e operacional.

## Superfícies principais

| Superfície | Acesso | Papel atual |
|---|---|---|
| `/` | público/logado | home, discovery, aquisição e Copiloto público |
| `/chat_julia` | público ou autenticado | conversa inicial e modo operacional da Júlia |
| `/auditoria-frete` | página pública; APIs autenticadas | Cleide Auditoria |
| `/agente-compara` | página pública; APIs autenticadas | comparação multitabela |
| `/fretes` | autenticado | Roberto BI |
| `/feed` | público | feed editorial misto |
| `/contrate-um-plano`, `/perfil/*` | autenticado | billing e área do usuário |
| `/admin/*` | admin | governança, operação e dashboards |
| `/cron/*`, `/ops/*`, `/health*` | operacional | automação, diagnóstico e health |

## Domínios/agentes

### Copiloto público

- vive principalmente na home e na jornada pública;
- faz triagem, onboarding e handoff;
- não representa o mesmo fluxo operacional autenticado da Júlia.

### Júlia

- assistente operacional do ambiente autenticado;
- também existe em modo público inicial em `/chat_julia`;
- combina chat, documentos e pipeline editorial;
- usa rotas e serviços documentais próprios.

### Cleide

- foco em auditoria de fretes;
- rota prioritária atual: `/auditoria-frete`;
- upload de Excel, CSV e PDF no fluxo operacional;
- entrevista proativa, memória temporária, confirmações e BI operacional;
- mantém isolamento de sessão, eventos e billing.

### AgenteCompara

- compara até 3 tabelas de frete;
- 2 tabelas são obrigatórias e a 3ª é opcional;
- usa `comparison_id`, `table_id` e `slot` para identidade do fluxo;
- tem preparação, revisão, cálculo, memória e dashboard próprios;
- o código já implementa cálculo comparativo, mas a documentação deve seguir o que estiver confirmado no runtime real de cada etapa.

### Roberto

- rota existente: `/fretes`;
- domínio de BI e leitura quantitativa de fretes;
- permanece implementado, mas escondido/não priorizado na experiência atual;
- não foi removido.

### Cleiton

- camada transversal de governança, billing técnico, observabilidade e orquestração;
- concentra regras de franquia, custos, eventos de processamento e parte das automações.

## Composição técnica

- núcleo Flask em `app/web.py`;
- blueprints ativos incluem admin, ops, user, Cleide, Cleide Auditoria, AgenteCompara e documentos da Júlia;
- Roberto, OAuth, onboarding, newsletter, webhooks e rotas gerais permanecem no `app/web.py`;
- persistência transacional em PostgreSQL;
- schema evoluído via Alembic;
- persistência técnica em disco para documentos, índices, resultados e artefatos temporários.

## Isolamento entre domínios

- Júlia, Cleide e AgenteCompara usam chaves e escopos distintos em sessão;
- Cleide Auditoria e AgenteCompara compartilham parte do trilho técnico documental do Cleiton, mas não compartilham identidade funcional;
- AgenteCompara usa storage comparativo próprio;
- Cleide Auditoria usa coverage, lote e contexto analítico próprios;
- billing e eventos não devem ser misturados entre os domínios.

## Persistência e runtime

- `DATABASE_URL` é a fonte de persistência transacional;
- `APP_DATA_DIR` sustenta storage técnico fora da sessão;
- `INDICES_FILE_PATH` fica fora da pasta efêmera da release em homolog/prod;
- `start.sh` roda `db upgrade` antes do Gunicorn.
- `render.yaml` versionado define homolog na branch `homolog` com `APP_ENV=homolog` e produção na branch `producao` com `APP_ENV=prod`;
- os dois serviços versionados usam `autoDeploy: true`, `build.sh`, `start.sh` e `healthCheckPath: /health`;
- quando `APP_ENV` não vem explícito, `start.sh` reconhece `main`, `master`, `producao` e `prod` como ambiente de produção;
- o runtime também expõe `/health/liveness` e `/health/readiness`.


## Relações importantes

- autenticação e lifecycle do usuário convivem com billing e auditoria, mas não apagam estrutura contratual;
- documentos legais têm governança própria por upload/admin e storage persistente;
- consentimento de marketing é separado de cookies/sessão necessários;
- masking para IA externa acontece na boundary outbound, não no dado persistido interno.
