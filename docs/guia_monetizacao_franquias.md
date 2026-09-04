# Guia de Monetização, Franquias e Planos

Referência auditada em 2026-09-04. Este guia descreve a monetização que o código implementa hoje.

## Visão geral

A monetização do projeto é dividida em duas camadas:

- camada comercial e contratual, centrada em `ContaMonetizacaoVinculo`, `MonetizacaoFato`, checkout e webhook Stripe;
- camada operacional, centrada em `Franquia`, consumo, autorização de uso, billing técnico e governança do Cleiton.

Regra principal:

- Stripe não substitui `Franquia`;
- `User.creditos` é legado;
- decisões de uso, bloqueio, degradação e expiração precisam olhar a franquia operacional.

## Fonte de verdade operacional

Entidades centrais:

- `Conta`
- `Franquia`
- `User`
- `ContaMonetizacaoVinculo`
- `MonetizacaoFato`
- `ProcessingEvent`
- `IaConsumoEvento`
- `CleitonBillingApropriacao`
- `CleitonCostConfig`

Campos operacionais mais relevantes em `Franquia`:

- `limite_total`
- `consumo_acumulado`
- `inicio_ciclo`
- `fim_ciclo`
- `bloqueio_manual`
- `status`

## Planos ativos no código

Planos catalogados:

- `free`
- `starter`
- `pro`
- `multiuser`
- `avulso`
- `uso_adm`

Leitura prática:

- `starter` e `pro` são os fluxos pagos mais claramente integrados ao gateway;
- `multiuser` e `avulso` continuam suportados operacionalmente;
- `uso_adm` é interno;
- `free` continua sendo a entrada freemium.

## Configuração administrativa dos planos

Os planos usam `ConfigRegras` para parâmetros administrativos, incluindo:

- `plano_valor_admin_<codigo>`
- `plano_franquia_ref_admin_<codigo>`
- `plano_gateway_provider_admin_<codigo>`
- `plano_gateway_product_id_admin_<codigo>`
- `plano_gateway_price_id_admin_<codigo>`
- `plano_gateway_currency_admin_<codigo>`
- `plano_gateway_interval_admin_<codigo>`
- `plano_gateway_ready_admin_<codigo>`
- `freemium_trial_dias`

O valor comercial e a franquia operacional não são a mesma coisa.

## Governança de franquia

- cada usuário comercial deve ter vínculo com `Conta` e `Franquia`;
- a autorização operacional central passa por `avaliar_autorizacao_operacao_por_franquia`;
- o plano operacional da franquia é resolvido por serviços do Cleiton;
- `multiuser` pode gerar várias franquias para a mesma conta e códigos em `MultiuserFranquiaCodigo`.

## Estados operacionais

O código trabalha com estados como:

- `active`
- `degraded`
- `blocked`
- `expired`

Leitura operacional resumida:

- `free` tende a bloquear (`blocked`) quando atinge o limite;
- `starter`, `pro` e `multiuser` tendem a degradar (`degraded`) ao atingir o limite;
- `avulso` pode expirar (`expired`) por vigência ou limite;
- `uso_adm` e estruturas internas seguem trilha especial, salvo bloqueio manual.

## Stripe

Fluxos confirmados no código:

- página de contratação em `/contrate-um-plano`;
- criação/início de checkout em `/api/contratacao/stripe/iniciar`;
- conciliação de retorno de checkout na própria área do usuário;
- regularização em `/perfil/regularizar-pagamento` e `/perfil/regularizar-pagamento/stripe`;
- encerramento contratual em `/perfil/encerrar-contrato`;
- webhook oficial em `/api/webhook/stripe`.

Evento contratual principal documentável:

- `invoice.paid`

Variáveis de ambiente principais:

- `STRIPE_API_KEY`
- `STRIPE_PUBLISHABLE_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_SUCCESS_URL`
- `STRIPE_CANCEL_URL`
- `STRIPE_CHECKOUT_API_BASE_URL`

## Eventos e fatos de monetização

- Stripe gera fatos persistidos e auditáveis em `MonetizacaoFato`;
- a conta pode manter customer, subscription, price e snapshots normalizados em `ContaMonetizacaoVinculo`;
- o efeito operacional final continua mediado pelos serviços de governança de franquia e plano;
- a área admin possui trilhas de auditoria para inconsistências de vínculo, múltiplas subscriptions, pendências e guardrails.

## Billing técnico e consumo

Há separação explícita entre:

- consumo de IA em `IaConsumoEvento`;
- processamento em `ProcessingEvent`;
- apropriação idempotente em `CleitonBillingApropriacao`.

Conversões operacionais usam parâmetros de `CleitonCostConfig`, como:

- `credit_tokens_per_credit`
- `credit_lines_per_credit`
- `credit_ms_per_credit`

### Régua de conversão para créditos

As conversões operacionais seguem as fórmulas implementadas em `cleiton_franquia_operacional_service.py`:

- tokens: `creditos = tokens / credit_tokens_per_credit`
- linhas: `creditos = linhas / credit_lines_per_credit`
- tempo de processamento: `creditos = processing_time_ms / credit_ms_per_credit`

As conversões:

- não consideram valores negativos; a entrada é normalizada para no mínimo zero;
- usam `Decimal`;
- são arredondadas para 6 casas decimais com `ROUND_HALF_UP`;
- falham explicitamente quando a régua correspondente está ausente ou é menor ou igual a zero.

Para eventos de IA:

- usa `total_tokens` quando disponível e maior que zero;
- caso contrário, usa `input_tokens + output_tokens`.

Para eventos de processamento:

- converte `rows_processed` em créditos;
- converte `processing_time_ms` em créditos;
- soma as duas parcelas;
- quantiza o total novamente para 6 casas decimais.

Consumo interno, anônimo ou da franquia reservada de sistema não abate da franquia do cliente.

## Roberto

- o upload de Excel do Roberto participa do billing operacional;
- a apropriação é idempotente;
- o fluxo usa storage temporário próprio e não depende de saldo em `User.creditos`;
- o consumo considera processamento técnico real, não apenas exibição posterior.

## AgenteAudita / Cleide

- upload de coverage, upload do lote e processamento do lote têm eventos operacionais separados (`cleide_audit_coverage_upload`, `cleide_audit_batch_upload`, `cleide_audit_batch_processed`);
- o primeiro processamento não deve cobrar de novo o lote já apropriado no upload;
- chat documental e chat analítico podem registrar consumo de IA, mas não substituem billing operacional de linhas.

## AgenteCompara

- uploads documentais podem registrar evento de funil;
- o cálculo comparativo tem billing operacional próprio e liberação pública condicionada a `billing_status=applied`;
- há idempotência por `execution_id` e fingerprint;
- o fluxo também pode marcar `first_audit_completed` quando aplicável.

## Regras que não devem ser quebradas

- não usar `User.creditos` como saldo oficial;
- não aplicar efeitos de Stripe diretamente sobre autorização operacional sem passar pela governança do Cleiton;
- não perder a idempotência de uploads e apropriações;
- não misturar consumo de IA com consumo operacional por linhas/tempo;
- não tratar eventos anônimos ou do sistema como faturáveis ao cliente.
