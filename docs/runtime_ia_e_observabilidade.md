# Runtime IA e Observabilidade

Data de consolidacao: `2026-07-10`
Commit de referencia: `homolog@d02ce15`

## Eventos oficiais

### `IaConsumoEvento`

Representa tentativa real de chamada LLM governada, com persistencia de:

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
- `conta_id`
- `franquia_id`
- `usuario_id`

### `ProcessingEvent`

Representa processamento tecnico nao-LLM e etapas auxiliares auditaveis.

## Fluxos observados

- `onboarding_discovery`
- fluxos operacionais de Julia, Roberto e Cleide
- fluxos administrativos que registram evento interno ou agregado

## Onboarding discovery

- endpoint: `POST /api/onboarding_discovery`
- reset: `POST /api/onboarding_discovery/reset`
- limite anonimo: `5`
- fallback local quando Gemini falha
- onboarding continua consumo interno e nao abate franquia operacional

## Julia documental

- autorizacao por franquia antes do upload
- limites por sessao e por tipo
- contexto textual governado
- `gemini_file_parts` para PDF quando disponivel
- degradacao segura quando o contexto documental nao puder ser montado

## Cleide Auditoria

Pontos efetivamente observaveis no codigo atual:

- upload e status documental podem retornar `temp_table`
- a extracao tecnica da `temp_table` ocorre no fluxo pos-upload
- o estado tecnico pode transitar por `processing`, `awaiting_validation`, `validated`, `needs_review`, `failed`, `expired` e `discarded`
- a revisao humana salva via endpoint dedicado
- coverage complementar e lote auditado compartilham a mesma trilha autenticada
- preview/apply/undo de correcao assistida existem para diagnosticos suportados
- o chat usa `flow_type` proprio, contexto documental isolado e idempotencia por `request_id`

Ponto importante:

- nao documentar metrica inexistente por grafico, por clique de UI ou por detalhe de BI
- o que existe hoje e observabilidade de consumo IA, eventos de processamento e estados documentais principais

## Metricas administrativas

- `operational_tokens_month`
- `onboarding_tokens_month`
- `total_internal_tokens_month`
- agregacoes por `api_key_label`
- contagem de eventos com e sem metricas
- contagem de falhas

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

Nao houve migration nova, tabela nova, campo novo ou `.db` versionado para esse runtime.
