# Estado Oficial Consolidado

Data de consolidacao: `2026-05-29`  
Commit de referencia: `20fa165`

Este documento registra o estado funcional promovido apos o onboarding discovery conversational-first e o controle administrativo da nuvem de palavras.

## 1. Escopo promovido

Estado confirmado no codigo:

- Home publica com Copilot do AgenteFrete;
- onboarding discovery conversational-first;
- limite de 5 interacoes anonimas por sessao;
- CTA de login ao atingir limite;
- reset de sessao por `Nova conversa`;
- preservacao de contexto para Julia apenas quando houver handoff para `julia_operational`;
- separacao entre Copilot e Julia operacional;
- observabilidade de onboarding em `IaConsumoEvento`;
- separacao administrativa entre tokens operacionais, tokens onboarding e total interno;
- dashboard admin com analise de termos do onboarding;
- Pareto 80/20 para exibicao da nuvem de palavras;
- ocultacao manual e reexibicao de termos;
- migration `r2s3t4u5v6w7`.

## 2. Superficies oficiais

- `/`: Home publica com Copilot de discovery
- `/chat_julia?mode=operational`: Julia operacional
- `/fretes`: Roberto BI e chat preditivo
- `/auditoria-frete`: Cleide auditoria retrospectiva
- `/feed`: superficie editorial
- `/admin/dashboard`: painel administrativo consolidado

## 3. Copilot da Home

Contrato real:

- o backend oficial e `POST /api/onboarding_discovery`;
- o reset oficial e `POST /api/onboarding_discovery/reset`;
- nao exige login para iniciar;
- usa sessao anonima com contador em servidor;
- limite anonimo por sessao: `5`;
- ao atingir o limite, nao chama Gemini e devolve payload de bloqueio com CTA de login;
- o shell visual esta em `app/static/js/chat_behavior.js`;
- o documento de capacidades oficial esta em `app/copilot_capabilities.md`;
- o fallback local conversacional continua disponivel se Gemini falhar ou estiver indisponivel.

## 4. Regra de agente por atividade-fim

Regras oficiais:

- Roberto = previsao, projecao, tendencias e horizonte futuro;
- Cleide = auditoria, conferencia, desvios, anomalias e horizonte historico;
- Julia = estrategia, supply chain, interpretacao executiva, negociacao e plano de acao.

Regras negativas:

- artefato nao define agente;
- planilha nao define agente;
- dashboard nao define agente;
- BI nao define agente;
- custo nao define agente;
- transportadora nao define agente.

Quando a atividade-fim e ambigua, o Copilot pede contexto e nao faz handoff automatico.

## 5. Julia

Julia tem dois papeis documentais distintos:

- shell visual da Home quando o modo discovery esta ativo;
- agente operacional real no endpoint `POST /api/chat_julia`.

Contrato da Julia operacional:

- exige login;
- valida autorizacao operacional por franquia;
- recebe handoff do Copilot com contexto resumido apenas quando o discovery sinalizou `julia_operational`;
- nao se confunde com o Copilot.

## 6. Roberto

Roberto continua como superficie quantitativa preditiva:

- BI operacional;
- dashboards;
- tendencias;
- previsoes;
- chat explicativo;
- horizonte futuro.

Contrato tecnico atual:

- pagina oficial: `/fretes`
- chat oficial: `POST /api/chat_roberto`
- login obrigatorio
- autorizacao operacional obrigatoria
- historico controlado por configuracao admin `chat_max_history`

## 7. Cleide

Cleide continua como superficie quantitativa investigativa:

- auditoria operacional;
- conferencia de custos;
- identificacao de desvios;
- leitura por transportadora;
- analise retrospectiva;
- horizonte historico.

Superficie oficial:

- `/auditoria-frete`
- `/api/cleide/upload`
- `/api/cleide/upload/status`
- `/api/cleide/upload/clear`
- `/api/cleide/dashboard/filter`
- `POST /api/chat_cleide`

## 8. Observabilidade e metricas

Eventos oficiais:

- `IaConsumoEvento`: chamadas LLM reais
- `ProcessingEvent`: processamento tecnico e snapshots

Fluxos documentados:

- `onboarding_discovery`
- `operacional`
- `administrativo`

Metricas administrativas oficiais:

- `operational_tokens_month`
- `onboarding_tokens_month`
- `total_internal_tokens_month`

Regra vigente:

- onboarding entra no total interno;
- onboarding nao abate franquia.

## 9. Dashboard admin

O dashboard administrativo atual consolida:

- consumo IA operacional;
- consumo IA onboarding;
- total interno de tokens;
- tokens por chave de API;
- eventos do onboarding com e sem metrica;
- processamento analitico Roberto;
- processamento analitico Cleide;
- nuvem de termos do onboarding.

## 10. Nuvem de palavras do onboarding

Origem:

- eventos `AuditoriaGerencial` com `tipo_decisao == "onboarding_discovery"`
- lista `user_terms_normalized` em `contexto_json`

Pipeline real:

1. leitura do historico bruto;
2. normalizacao do termo;
3. remocao de stopwords;
4. remocao de termos ocultos admin;
5. contagem por frequencia;
6. corte Pareto 80/20;
7. exibicao limitada no dashboard.

Garantias:

- historico bruto e preservado;
- ocultar termo nao apaga passado;
- reexibir termo so reativa a agregacao futura/leitura atual;
- termos ocultos ficam persistidos em tabela dedicada.

## 11. Banco de dados

Novo modelo oficial:

`OnboardingWordCloudHiddenTerm`

Campos:

- `id`
- `term_normalized`
- `is_active`
- `created_at`
- `updated_at`
- `hidden_by_user_id`
- `notes`

Migration associada:

- `r2s3t4u5v6w7_onboarding_word_cloud_hidden_term.py`

## 12. Ambientes

Contratos oficiais:

- `APP_ENV`: `dev`, `homolog`, `prod`
- `DATABASE_URL`: PostgreSQL
- `PUBLIC_BASE_URL`: base publica canonica do ambiente
- `APP_DATA_DIR`: persistencia operacional

Promocao esperada:

- local -> `homolog` -> `producao`
- migrations aplicadas ate `head`
- validacao do onboarding, dashboard, Roberto e Cleide antes do promote
