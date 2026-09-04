# Runtime IA e Observabilidade

Referência auditada em 2026-09-04. Estado funcional consolidado em `docs/estado_producao.md`.

## Trilhas separadas

Modelos e serviços confirmados no código:

- `IaConsumoEvento`: uso real de LLM, tokens, status, provider, agente e `flow_type`;
- `ProcessingEvent`: processamento não-LLM, linhas, duração, status, agente e `flow_type`;
- `CleitonBillingApropriacao`: billing operacional idempotente;
- `IaBillingCostSnapshot`: snapshot de custo externo;
- `CleitonCostConfig`: régua de custo e créditos;
- `HomeCtaExperimentEvent`: telemetria isolada do experimento `home_chat_cta_v1`;
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

Observações do runtime atual:

- validação determinística de `temp_table`, memória pública e gate de completeza não geram consumo de IA;
- o cálculo comparativo pode terminar com células `incomplete` sem que isso represente falha de infraestrutura, desde que o motor tenha produzido valor parcial e issues bloqueantes rastreáveis;
- replays idempotentes preservam observabilidade e billing sem duplicar indevidamente o resultado público;
- o chat contextual da comparação (`comparison-chat`) usa `flow_type` próprio (`agente_compara_comparison_chat`) e só fica disponível quando há `result` liberado e `analytics` válidos em estado READY;
- o `comparison-chat` é separado do `audit-chat`, permanece bloqueado antes de READY e não faz fetch pré-READY.

## AgenteAudita/Cleide e Julia

- Julia consome IA, mas não tem billing documental próprio nessa camada;
- o domínio técnico Cleide (superfície pública AgenteAudita) mantém namespace de métricas e processamento separado do AgenteCompara;
- testes confirmam que eventos do AgenteCompara não contaminam os agregados da Cleide.

## Home CTA experiment

- a leitura administrativa do experimento consulta somente `home_cta_experiment_event`;
- os períodos administrativos disponíveis são 7, 30 e 90 dias;
- inconsistências com `conversions > impressions` geram warning e cap visual na leitura administrativa;
- a telemetria é fail-open: falha ao gravar evento não quebra home nem chat.

## Cron e health

Rotas confirmadas:

- `/cron/executar-cleiton`
- `/cron/finance`
- `/cron/billing-snapshot`
- `/health`
- `/health/liveness`
- `/health/readiness`

A autenticação do cron continua por `X-Cron-Secret`, com `?secret=` mantido como compatibilidade temporária.

## Banco e deploy

- o head atual versionado no repositório é `z0a1b2c3d4e5`
- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn
- o guard de `app/db_operational_safety.py` bloqueia downgrade sem confirmação explícita, mas não bloqueia `upgrade` normal
