# Runtime IA e Observabilidade

Referência auditada em 2026-07-20.

## Trilhas separadas

Modelos confirmados no código:

- `IaConsumoEvento`: tentativas reais de LLM, tokens, status, provider, agente, `flow_type` e identidade quando disponível;
- `ProcessingEvent`: processamento não-LLM, linhas, duração, status, agente e `flow_type`;
- `CleitonBillingApropriacao`: apropriação operacional idempotente;
- `IaBillingCostSnapshot`: snapshot de custo month-to-date via BigQuery billing export;
- `CleitonCostConfig`: régua de custo e conversão em créditos.

Consumo operacional e consumo de IA permanecem separados por desenho e por testes.

## Onboarding e consumo interno

`onboarding_discovery` continua fora do abatimento operacional do cliente. Isso é confirmado por testes em `tests/test_ia_metrics_service.py`.

## Júlia

- documentos apoiam o chat operacional, mas a camada `/api/julia/documents/*` não cria billing próprio;
- o uso de IA da Júlia entra em `IaConsumoEvento`;
- a autorização operacional continua centralizada no Cleiton.

## Cleide Auditoria

Fluxos operacionais observáveis confirmados:

- `cleide_audit_coverage_upload`;
- `cleide_audit_batch_upload`;
- `cleide_audit_batch_processed`, apenas em reprocessamento faturável;
- eventos de `temp_table`, coverage, lote e BI;
- IA documental e chat em `IaConsumoEvento`.

O primeiro `audit/run` não cobra novamente o lote já apropriado no upload inicial.

## Agente Compara

Fluxos observáveis paralelos à Cleide, com namespace próprio:

- `agente_compara_coverage_upload`;
- `agente_compara_batch_upload`;
- `agente_compara_batch_processed`;
- eventos próprios de `temp_table`, chat e BI;
- agregação separada no dashboard administrativo.

Testes confirmam que os eventos do Agente Compara não contaminam os blocos de métricas da Cleide.

## Operação e correlação

Ao investigar incidentes, correlacionar:

- usuário, conta e franquia;
- `execution_id`;
- `request_id`;
- `flow_type`;
- chave idempotente;
- `temp_table_id` e `audit_batch_id` quando existirem;
- status, linhas, duração e erro.

## Cron e snapshots

Rotas confirmadas em código:

- `/cron/executar-cleiton`
- `/cron/finance`
- `/cron/billing-snapshot`

Autenticação por `X-Cron-Secret`, com `?secret=` mantido como compatibilidade temporária. O snapshot de billing pode ser pulado quando BigQuery não estiver configurado.
