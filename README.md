# Agentefrete / Log Completa

Data de consolidacao: `2026-05-29`  
Estado de referencia: `producao` / commit `20fa165`

Este `README.md` e a fonte principal de contexto funcional e operacional do projeto. Ele resume o estado real promovido em producao apos:

- onboarding discovery conversational-first;
- Copilot do AgenteFrete na Home publica;
- separacao Copilot x Julia;
- observabilidade do onboarding;
- contagem de tokens de onboarding;
- dashboard administrativo de IA;
- controle administrativo da nuvem de palavras;
- Pareto 80/20;
- ocultacao manual e reexibicao de termos;
- regra de encaminhamento por atividade-fim;
- migration `r2s3t4u5v6w7`.

## Estado atual do produto

As superficies oficiais ativas sao:

- Home publica com Copilot de descoberta: `/`
- Julia operacional com login: `/chat_julia?mode=operational`
- Roberto BI operacional e preditivo: `/fretes`
- Cleide auditoria operacional e retrospectiva: `/auditoria-frete`
- Feed editorial: `/feed`
- Dashboard admin: `/admin/dashboard`

## Regra-mae de roteamento

Artefato nao define agente.  
Planilha nao define agente.  
Dashboard nao define agente.  
BI nao define agente.  
Custo nao define agente.

O agente e definido pela atividade-fim e pelo horizonte temporal:

- Roberto: previsao, projecao, tendencia, estimativa e horizonte futuro.
- Cleide: auditoria, conferencia, desvios, anomalias, transportadoras e horizonte historico.
- Julia: estrategia, supply chain, negociacao, interpretacao executiva e plano de acao.

Se o usuario mencionar apenas artefatos ou temas genericos, o Copilot nao deve fazer handoff automatico. Ele precisa pedir contexto antes.

## Copilot da Home

O Copilot da Home e um fluxo de discovery conversational-first:

- endpoint oficial: `POST /api/onboarding_discovery`
- reset oficial: `POST /api/onboarding_discovery/reset`
- shell visual: `app/static/js/chat_behavior.js`
- documento oficial de capacidades: `app/copilot_capabilities.md`
- motor principal: Gemini governado por Cleiton Discovery
- fallback: resposta local conversacional quando Gemini nao estiver disponivel

Contrato real do fluxo:

- funciona com ou sem login;
- usa sessao anonima para contar interacoes;
- limite anonimo de 5 interacoes por sessao;
- ao atingir o limite, mostra CTA de login para continuar gratuitamente com a Julia;
- a acao `Nova conversa` zera o contador e limpa o contexto visual da sessao;
- o contexto de onboarding para Julia so e preservado quando o handoff sugerido inclui `julia_operational`;
- onboarding nao consome franquia operacional do cliente.

## Julia

Julia nao e o Copilot da Home.

Julia aparece em dois contextos:

- como shell visual do chat da Home, enquanto o backend ainda e o onboarding discovery;
- como agente operacional real em `/chat_julia?mode=operational`, sempre com login.

Contrato atual da Julia operacional:

- endpoint oficial: `POST /api/chat_julia`
- exige autenticacao;
- valida autorizacao operacional por franquia antes de consumir IA;
- recebe contexto vindo do Copilot apenas quando houve handoff de onboarding para Julia.

## Roberto

Roberto e o agente para BI operacional com foco preditivo e forward-looking.

Escopo real:

- dashboards e indicadores;
- leitura de base historica de fretes;
- tendencias e previsoes;
- horizonte futuro;
- chat Roberto com memoria propria;
- upload e BI em `/fretes`.

Contrato tecnico:

- pagina oficial: `/fretes`
- endpoint do chat: `POST /api/chat_roberto`
- exige login;
- usa `avaliar_autorizacao_operacao_por_franquia`;
- historico do chat controlado por `chat_max_history` em configuracao admin.

## Cleide

Cleide e o agente de auditoria operacional retrospectiva.

Escopo real:

- conferencia de custos realizados;
- identificacao de desvios;
- leitura historica;
- concentracao por transportadora;
- apoio quantitativo investigativo;
- horizonte passado.

Superficie oficial:

- pagina: `/auditoria-frete`
- health: `/api/cleide/health`
- template: `/api/cleide/template`
- upload: `/api/cleide/upload`
- status: `/api/cleide/upload/status`
- limpeza: `/api/cleide/upload/clear`
- filtro analitico: `/api/cleide/dashboard/filter`
- chat: `POST /api/chat_cleide`

## Observabilidade oficial

Os trilhos oficiais continuam centralizados em eventos persistidos.

`IaConsumoEvento` registra tentativa real de chamada LLM com:

- `provider`
- `operation`
- `model`
- `agent`
- `flow_type`
- `api_key_label`
- tokens
- `status`
- `conta_id`
- `franquia_id`
- `usuario_id`

`ProcessingEvent` registra processamento tecnico nao-LLM e snapshots auxiliares.

Fluxos documentados no estado atual:

- `onboarding_discovery`
- `operacional`
- `administrativo`

Leituras administrativas expostas no dashboard:

- `operational_tokens_month`
- `onboarding_tokens_month`
- `total_internal_tokens_month`

Regra critica:

- onboarding conta como consumo interno de IA;
- onboarding nao abate franquia do cliente.

## Dashboard admin

O dashboard administrativo consolidado mostra:

- consumo IA operacional;
- consumo IA onboarding;
- total interno de tokens;
- processamento analitico Roberto;
- processamento analitico Cleide;
- analise de termos do onboarding.

### Nuvem de palavras do onboarding

Origem real:

- `AuditoriaGerencial.tipo_decisao == "onboarding_discovery"`
- campo `user_terms_normalized` em `contexto_json`

Pipeline real:

`user_terms_normalized -> normalizacao -> stopwords -> termos ocultos admin -> frequencia -> Pareto 80/20`

Regras atuais:

- stopwords sao removidas apenas na agregacao;
- ocultacao manual nao altera o historico bruto;
- reexibicao apenas desativa o ocultamento;
- historico em `AuditoriaGerencial` permanece preservado;
- o admin pode ocultar e reexibir termos pelo dashboard.

## Banco de dados

Modelo novo relevante:

`OnboardingWordCloudHiddenTerm`

Finalidade:

- persistir termos ocultados manualmente na nuvem do onboarding;
- permitir reativacao individual;
- manter auditoria por termo sem apagar o historico bruto.

Campos:

- `id`
- `term_normalized`
- `is_active`
- `created_at`
- `updated_at`
- `hidden_by_user_id`
- `notes`

Relacionamentos:

- `hidden_by_user_id -> user.id`

Migration associada:

- `migrations/versions/r2s3t4u5v6w7_onboarding_word_cloud_hidden_term.py`

## Ambientes

Contrato atual:

- `APP_ENV` obrigatorio: `dev`, `homolog`, `prod`
- `DATABASE_URL` deve apontar para PostgreSQL
- `PUBLIC_BASE_URL` define a base publica oficial do ambiente
- `APP_DATA_DIR` governa persistencia operacional via `settings.data_dir`

Fluxo de promocao:

- desenvolvimento local
- branch `homolog`
- branch `producao`

Estado confirmado neste commit:

- `HEAD` local = `20fa165`
- `20fa165` tambem esta em `origin/producao`

Antes de promover homolog -> producao:

- aplicar migrations ate `head`;
- validar onboarding discovery;
- validar handoff para Julia;
- validar dashboard admin;
- validar word cloud e hidden terms;
- validar Roberto e Cleide;
- validar suite critica.

## Documentos oficiais de apoio

Ler nesta ordem:

1. `docs/auditoria_documental_2026-05-29.md`
2. `docs/estado_oficial_consolidado.md`
3. `docs/arquitetura_oficial.md`
4. `docs/runtime_ia_e_observabilidade.md`
5. `docs/onboarding_tecnico.md`
6. `docs/runbook_onboarding_copilot.md`
7. `migrations/README`

## Fora de escopo e honestidade do produto

O produto nao deve prometer:

- cotacao automatizada de fretes;
- BID de frete;
- execucao operacional tipo TMS/WMS;
- handoff automatico baseado apenas em planilha, dashboard, BI ou custo.
