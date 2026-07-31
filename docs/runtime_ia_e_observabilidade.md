# Runtime IA e Observabilidade

Referência auditada em 2026-07-29. Estado funcional consolidado em `docs/estado_oficial_consolidado.md`.

## Trilhas separadas

Modelos e serviços confirmados no código:

- `IaConsumoEvento`: uso real de LLM, tokens, status, provider, agente e `flow_type`;
- `ProcessingEvent`: processamento não-LLM, linhas, duração, status, agente e `flow_type`;
- `CleitonBillingApropriacao`: billing operacional idempotente;
- `IaBillingCostSnapshot`: snapshot de custo externo;
- `CleitonCostConfig`: régua de custo e créditos;
- `app/services/ia_metrics_service.py`: agregações administrativas.

## AgenteCompara

Eventos de processamento consolidados no domínio:

- `agente_compara_coverage_upload`
- `agente_compara_batch_upload`
- `agente_compara_batch_processed`
- `agente_compara_comparison_calculation`

Leitura operacional correta:

- upload do arquivo operacional e reprocessamentos têm eventos distintos;
- o cálculo comparativo também gera evento próprio;
- isso não significa, por si só, duplicação automática de linhas faturadas;
- o resultado comparativo só é liberado publicamente quando `billing_status=applied`;
- o analytics comparativo retornado ao frontend é derivado do resultado já liberado e não executa billing nem Gemini.

## Cleide e Júlia

- Júlia consome IA, mas não tem billing documental próprio nessa camada;
- Cleide mantém namespace de métricas e processamento separado do AgenteCompara;
- testes confirmam que eventos do AgenteCompara não contaminam os agregados da Cleide.

## Cron e health

Rotas confirmadas:

- `/cron/executar-cleiton`
- `/cron/finance`
- `/cron/billing-snapshot`
- `/health/liveness`
- `/health/readiness`

A autenticação do cron continua por `X-Cron-Secret`, com `?secret=` mantido como compatibilidade temporária.