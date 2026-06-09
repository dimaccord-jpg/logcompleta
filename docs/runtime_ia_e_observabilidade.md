# Runtime IA e Observabilidade

Data de consolidacao: `2026-06-05`
Commit de referencia: `b5fc444`

## Objetivo

Consolidar o runtime oficial de IA, consumo tecnico e governanca documental apos a entrega da Julia documental.

## Eventos oficiais

### `IaConsumoEvento`

Representa tentativa real de chamada LLM com:

- `provider`
- `operation`
- `model`
- `agent`
- `flow_type`
- `api_key_label`
- `status`
- tokens
- `conta_id`
- `franquia_id`
- `usuario_id`

### `ProcessingEvent`

Representa processamento tecnico nao-LLM e etapas auxiliares auditaveis.

## Fluxos oficiais

- `onboarding_discovery`
- `operacional`
- `administrativo`

Leitura pratica:

- `onboarding_discovery`: Copilot da Home
- `operacional`: Julia, Roberto e Cleide em consumo real
- `administrativo`: painois e agregacoes administrativas

## Onboarding discovery

Contrato real:

- endpoint: `POST /api/onboarding_discovery`
- reset: `POST /api/onboarding_discovery/reset`
- limite anonimo: `5`
- bloqueio com CTA de login quando atinge o limite
- fallback local quando Gemini falha

Regra critica:

- onboarding conta como consumo interno
- onboarding nao abate franquia

## Julia documental e observabilidade

O upload documental da Julia opera no trilho operacional, com governanca do Cleiton.

Pontos observaveis:

- autorizacao por franquia antes do upload
- limites por sessao e por tipo
- contexto textual preparado para prompt
- `gemini_file_parts` para PDF quando disponivel
- degradacao segura para chat textual se a montagem de contexto falhar

O sistema deve deixar explicito quando o PDF ainda nao esta pronto para leitura pela IA ou quando o contexto e apenas multimodal.

## Metricas administrativas

- `operational_tokens_month`
- `onboarding_tokens_month`
- `total_internal_tokens_month`

Regras:

- `operational_tokens_month`: consumo operacional do mes
- `onboarding_tokens_month`: apenas `flow_type=onboarding_discovery`
- `total_internal_tokens_month`: soma de operacional e onboarding

## Configuracao documental do Cleiton

O runtime documental usa `ConfigRegras` e `app/services/cleiton_doc_config_service.py`.

Campos principais:

- `upload_enabled`
- `max_files_per_session`
- `session_max_bytes`
- `upload_ttl_hours`
- `cleanup_enabled`
- `prompt_context_max_chars`
- `prompt_max_files_considered`
- limites por tipo para `pdf`, `excel`, `docx`, `txt`, `xml`, `csv`

Nao houve migration nova para essa configuracao.
