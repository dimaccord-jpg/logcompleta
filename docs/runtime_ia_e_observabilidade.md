# Runtime IA e Observabilidade

Referência: `homolog@6701a53`, 2026-07-16.

## Trilhas separadas

`IaConsumoEvento` registra tentativa real de LLM: horário, provider, operação, modelo, agente, `flow_type`, chave lógica, status, tokens, erro e identidade de conta/franquia/usuário quando disponível. `ProcessingEvent` registra trabalho não-LLM: agente, fluxo, tipo, linhas, duração, status, erro, `execution_id` e identidade.

`CleitonBillingApropriacao` liga evento operacional, chave idempotente, linhas, créditos e motivo. `CleitonCostConfig` contém parâmetros de custo por segundo e conversão de tokens/linhas/tempo. Não misture tokens com linhas: são dimensões e trilhas diferentes.

## Auditoria Cleide

Fluxos operacionais observáveis:

- `cleide_audit_coverage_upload`;
- `cleide_audit_batch_upload`;
- `cleide_audit_batch_processed`, apenas para reprocessamento faturável;
- estados de documentos e `temp_table`;
- quantidade de linhas, duração, sucesso/falha e `error_summary`;
- idempotência por IDs de sessão/lote/versão e `execution_id`.

Chamadas de extração ou chat Gemini são eventos IA com `flow_type` próprio. O chat analítico usa `request_id` e cache por `batch_scope`; fallback determinístico continua útil quando o provider falha. Falhas de billing tentam registrar `ProcessingEvent` sem aplicar o motor novamente, preservando rastreabilidade.

O dashboard administrativo agrega uploads, eventos, linhas, duração média, falhas e tokens. Métrica por clique/gráfico não existe e não deve ser inventada. `tests/test_ia_metrics_service.py` e `tests/test_cleide_audit_operational_billing.py` são contratos da separação.

## Operação

Ao investigar incidente, correlacione: usuário/franquia, `execution_id`, `request_id`, `temp_table_id`, `audit_batch_id`, `flow_type`, chave idempotente, status, duração, linhas e erro. Repetição da mesma chave não deve criar nova apropriação.

Onboarding `onboarding_discovery` continua consumo interno separado e não abate franquia operacional. Esta entrega não criou migration, tabela ou coluna para observabilidade.
