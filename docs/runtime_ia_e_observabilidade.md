# Runtime IA e Observabilidade

Data de consolidacao: `2026-05-26`

## 1. Objetivo

Este documento consolida o runtime oficial de IA, consumo tecnico, fallback e observabilidade do projeto.

## 2. Regras globais

- runtime de IA precisa ser rastreavel;
- fallback nao pode mascarar erro;
- erro precisa manter causa resumida e auditavel;
- consumo de IA so e oficial quando passa pelo trilho do Cleiton;
- acao visual ou pagina publica nao deve gerar consumo tecnico.

## 3. Eventos oficiais

### `IaConsumoEvento`

Representa tentativa real de chamada LLM, com persistencia de:

- `provider`
- `operation`
- `model`
- `agent`
- `flow_type`
- `api_key_label`
- tokens
- status
- identidade `conta_id` / `franquia_id` / `usuario_id`

### `ProcessingEvent`

Representa processamento tecnico nao-LLM ou etapa auxiliar auditavel, incluindo:

- upload operacional
- snapshot de contexto
- tempo de processamento
- linhas processadas
- status
- `error_summary` quando aplicavel

## 4. Runtime oficial da Julia

### Redacao

- chamadas de redacao passam pelo trilho governado do Cleiton;
- artigo exige `redacao_status=sucesso` e `redacao_fallback=False`;
- fallback de redacao encerra pipeline antes de imagem e publicacao.

### Imagem

Configuracao efetiva exposta por `get_image_runtime_config()`:

- `IMAGE_PROVIDER`
- `GEMINI_MODEL_IMAGE`
- `GEMINI_MODEL_IMAGE_FALLBACK`
- `GEMINI_HTTP_TIMEOUT_MS`
- `GEMINI_IMAGE_HTTP_TIMEOUT_MS`
- provider efetivo
- modelo efetivo principal
- modelo efetivo fallback
- timeout efetivo

Persistencia:

- storage atual: `settings.data_dir/generated`
- URL publica: `/media/generated/`

Observabilidade de imagem:

- `imagem_status`
- `imagem_provider`
- `imagem_motivo`
- `imagem_origem`
- `imagem_url_final`
- `prompt_imagem_usado`

## 5. Runtime oficial da Cleide

Fluxos principais:

- upload `POST /api/cleide/upload`
- status `GET /api/cleide/upload/status`
- clear `POST /api/cleide/upload/clear`
- filtro `POST /api/cleide/dashboard/filter`
- chat `POST /api/chat_cleide`

Indicadores observaveis no payload ou na UI:

- `flow_type`
- `ai_flow_type`
- `ai_used`
- `fallback_used`
- `policy_blocked`
- `context_status`
- `view_scope`
- `active_filters`
- `error_code`

Fallbacks documentados:

- `provider_error`
- `fallback_intent_desconhecida`
- `fallback_fora_de_escopo`
- `fallback_bloqueio_semantico`
- `fallback_contexto_indisponivel`
- `fallback_pergunta_invalida`
- `fallback_pergunta_muito_longa`

## 6. Regras de compartilhamento social

O bloco de share publico:

- nao usa IA;
- nao usa `IaConsumoEvento`;
- nao usa billing;
- nao dispara pipeline;
- apenas monta URLs publicas para Facebook, Threads, X, LinkedIn e WhatsApp.

## 7. Homolog e producao

Contrato de ambiente:

- `APP_ENV` obrigatorio;
- homolog e producao nao aceitam fallback implicito de ambiente;
- `PUBLIC_BASE_URL` controla canonical, `og:url` e `share_url_abs`;
- `settings.data_dir` define o storage persistente oficial;
- `debug=False` em homolog e producao.

## 8. Checklist operacional

- confirmar `APP_ENV`
- confirmar `DATABASE_URL` PostgreSQL
- confirmar `PUBLIC_BASE_URL`
- confirmar `settings.data_dir`
- confirmar logs de provider/model/timeout/tentativa
- confirmar `IaConsumoEvento` apenas em chamadas LLM reais
- confirmar `ProcessingEvent` nos fluxos de upload e processamento
- confirmar fallback auditado sem write indevido em patrimonio editorial
