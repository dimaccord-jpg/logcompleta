# Arquitetura Oficial

Data de consolidacao: `2026-05-29`  
Commit de referencia: `20fa165`

Este documento registra a arquitetura oficial do projeto no estado promovido em producao.

## 1. Principios

Nao criar:

- rota paralela;
- blueprint paralelo;
- bypass operacional;
- fallback mascarando erro;
- regra documental concorrente.

Sempre:

- causa raiz primeiro;
- trilho oficial;
- auditoria;
- observabilidade;
- governanca;
- contratos claros.

## 2. Superficies e papeis

### Copilot

- superficie: Home publica `/`
- natureza: discovery conversational-first
- papel: entender a atividade-fim, pedir contexto e sugerir handoff quando houver clareza
- nao executa BI, auditoria ou estrategia operacional completa na propria Home

### Julia

- superficie operacional: `/chat_julia?mode=operational`
- papel: estrategia, supply chain, interpretacao executiva, negociacao e plano de acao
- requisito: login e autorizacao operacional

### Roberto

- superficie: `/fretes`
- papel: BI operacional, dashboards, tendencias, previsoes e horizonte futuro
- requisito: login e autorizacao operacional

### Cleide

- superficie: `/auditoria-frete`
- papel: auditoria operacional, conferencia, desvios, anomalias e horizonte historico
- requisito: login para upload e chat; template publico permanece acessivel

### Dashboard admin

- superficie: `/admin/dashboard`
- papel: consolidacao administrativa de IA, processamento e termos do onboarding

## 3. Regra-mae de handoff

A arquitetura oficial de handoff do Copilot obedece esta regra:

- artefato nao define agente;
- atividade-fim e horizonte temporal definem agente.

Tabela canonicamente valida:

| Objetivo do usuario | Agente |
| --- | --- |
| Prever, projetar, estimar proximos meses, olhar tendencia futura | Roberto |
| Auditar, conferir, investigar o ocorrido, analisar desvios passados | Cleide |
| Decidir, negociar, planejar, interpretar executivamente | Julia |

Consequencias arquiteturais:

- planilha nao define Roberto;
- dashboard nao define Cleide;
- custo nao define Roberto;
- transportadora nao define Cleide;
- BI nao define agente por si so.

## 4. Fronteiras tecnicas oficiais

### Copilot

- `POST /api/onboarding_discovery`
- `POST /api/onboarding_discovery/reset`
- documento de capacidades: `app/copilot_capabilities.md`
- shell visual: `app/static/js/chat_behavior.js`

### Julia

- `POST /api/chat_julia`
- handoff web: `/chat_julia?mode=operational`

### Roberto

- upload oficial: `/api/roberto/upload`
- limpeza: `/api/roberto/clear_upload`
- chat oficial: `POST /api/chat_roberto`
- superficie principal: `/fretes`

### Cleide

- health: `/api/cleide/health`
- template: `/api/cleide/template`
- upload: `/api/cleide/upload`
- status: `/api/cleide/upload/status`
- clear: `/api/cleide/upload/clear`
- filtro: `/api/cleide/dashboard/filter`
- chat: `POST /api/chat_cleide`

## 5. Governanca e observabilidade

Toda operacao relevante continua subordinada ao trilho oficial:

- autorizacao operacional por franquia;
- identidade `conta_id` / `franquia_id` / `usuario_id`;
- `IaConsumoEvento`;
- `ProcessingEvent`;
- `AuditoriaGerencial`;
- billing tecnico e leitura administrativa.

Regra especial do onboarding:

- onboarding e consumo interno do sistema;
- onboarding nao abate franquia operacional do cliente.

## 6. Arquitetura do dashboard de onboarding

Fluxo oficial da nuvem de palavras:

`AuditoriaGerencial(user_terms_normalized) -> normalizacao -> stopwords -> admin hidden terms -> frequencia -> Pareto 80/20 -> dashboard admin`

Persistencia de ocultacao:

- modelo: `OnboardingWordCloudHiddenTerm`
- uso: ocultar/reexibir termo sem apagar historico bruto

## 7. Ambientes e promocao

Contrato de ambiente:

- `APP_ENV` obrigatorio: `dev`, `homolog`, `prod`
- `DATABASE_URL` em PostgreSQL
- `PUBLIC_BASE_URL` como base publica canonica
- `APP_DATA_DIR` como storage persistente

Promocao oficial:

- branch de trabalho/local
- `homolog`
- `producao`

Nao existe promocao segura sem:

- migrations ate `head`;
- validacao do onboarding discovery;
- validacao do dashboard admin;
- validacao de Roberto e Cleide;
- revisao da observabilidade.
