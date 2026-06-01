# Runtime IA e Observabilidade

Data de consolidacao: `2026-05-29`  
Commit de referencia: `20fa165`

## 1. Objetivo

Este documento consolida o runtime oficial de IA, consumo tecnico, fallback, observabilidade e metricas administrativas do estado atual.

## 2. Eventos oficiais

### `IaConsumoEvento`

Representa tentativa real de chamada LLM.

Campos de runtime observados no estado atual:

- `provider`
- `operation`
- `model`
- `agent`
- `flow_type`
- `api_key_label`
- `status`
- `input_tokens`
- `output_tokens`
- `total_tokens`
- `error_summary`
- `conta_id`
- `franquia_id`
- `usuario_id`

### `ProcessingEvent`

Representa processamento tecnico nao-LLM ou etapa auxiliar auditavel.

Campos relevantes:

- `agent`
- `flow_type`
- `status`
- `rows_processed`
- `processing_time_ms`
- `error_summary`
- identidade operacional quando houver

## 3. Fluxos oficiais

Fluxos atualmente documentados:

- `onboarding_discovery`
- `operacional`
- `administrativo`

Leitura pratica desses fluxos:

- `onboarding_discovery`: discovery da Home, com consumo interno de IA;
- `operacional`: chat e processamento dos agentes produtivos;
- `administrativo`: leituras, agregacoes e controles do painel admin.

## 4. Copilot / onboarding discovery

Contrato real do runtime:

- endpoint: `POST /api/onboarding_discovery`
- reset: `POST /api/onboarding_discovery/reset`
- limite anonimo: `5` interacoes por sessao
- ao atingir limite, o backend devolve bloqueio com CTA de login e nao chama Gemini
- sessao anonima usa identidade interna/sistema para observabilidade

Observabilidade do onboarding:

- `flow_type = "onboarding_discovery"` em `IaConsumoEvento`
- auditoria gerencial do discovery
- contagem anonima por sessao
- contexto opcional para handoff Julia

Fallback:

- se Gemini nao estiver disponivel, o sistema usa resposta local conversacional baseada no documento de capacidades
- fallback nao deve inventar funcionalidades nem mudar a regra de handoff por atividade-fim

## 5. Separacao de metricas

O dashboard admin usa tres leituras canonicas:

- `operational_tokens_month`
- `onboarding_tokens_month`
- `total_internal_tokens_month`

Regras:

- `operational_tokens_month`: soma mensal de `IaConsumoEvento` excluindo `flow_type=onboarding_discovery`
- `onboarding_tokens_month`: soma mensal apenas de `flow_type=onboarding_discovery`
- `total_internal_tokens_month`: soma de operacional + onboarding

Franquia:

- onboarding nao abate franquia;
- onboarding conta apenas como consumo interno/admin.

## 6. Dashboard admin de IA

O payload consolidado do painel inclui:

- tokens operacionais do mes;
- tokens onboarding do mes;
- total interno do mes;
- tokens por chave de API;
- contagem de eventos onboarding com e sem metrica;
- falhas onboarding;
- processamento Roberto;
- processamento Cleide.

## 7. Nuvem de palavras do onboarding

Origem dos termos:

- `AuditoriaGerencial.tipo_decisao == "onboarding_discovery"`
- lista `user_terms_normalized` em `contexto_json`

Pipeline real:

1. ler termos normalizados do historico;
2. normalizar novamente para visualizacao;
3. remover stopwords;
4. remover termos ocultos manualmente pelo admin;
5. rankear por frequencia;
6. aplicar Pareto 80/20;
7. limitar exibicao no dashboard.

Preservacao historica:

- o historico bruto em `AuditoriaGerencial` nao e alterado;
- ocultar termo age apenas na agregacao;
- reexibir termo apenas desativa o ocultamento.

## 8. Controles administrativos de hidden terms

Persistencia:

- tabela `onboarding_word_cloud_hidden_term`
- modelo `OnboardingWordCloudHiddenTerm`

Operacoes:

- ocultar termo: `POST /admin/onboarding-word-cloud/hidden-terms`
- reexibir termo: `POST /admin/onboarding-word-cloud/hidden-terms/<term_id>/restore`

Campos persistidos:

- `term_normalized`
- `is_active`
- `hidden_by_user_id`
- `notes`
- timestamps

## 9. Ambientes

Contratos:

- `APP_ENV` obrigatorio
- `DATABASE_URL` em PostgreSQL
- `PUBLIC_BASE_URL` como base canonica
- `APP_DATA_DIR` para persistencia operacional

Checklist rapido de observabilidade:

- confirmar eventos de onboarding em `IaConsumoEvento`
- confirmar separacao de tokens no dashboard admin
- confirmar hidden terms no admin
- confirmar que onboarding nao abate franquia
