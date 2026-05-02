# Testes removidos ou substituidos

## Objetivo

Registrar testes antigos identificados por referencias documentais ou residuos compilados, mantendo historico minimo para futuras limpezas seguras.

## Criterios

- nao remover cobertura critica sem substituto equivalente;
- priorizar suites alinhadas ao comportamento atual do produto;
- registrar quando a remocao ocorreu por legado, refatoracao ou consolidacao;
- usar dominios funcionais para descrever cobertura, evitando depender de nomes de arquivos instaveis.

## Casos identificados

| Teste antigo | Evidencia encontrada | Provavel substituto atual | Motivo provavel |
|---|---|---|---|
| `tests/test_cleiton_monetizacao_service.py` | citado no `README` e encontrado anteriormente em residuos `.pyc` | `tests/test_cleiton_monetizacao_stripe_blockers.py` e `tests/test_cleiton_monetizacao_stripe_guardrails.py` | refatoracao e consolidacao da cobertura Stripe/Cleiton |
| `tests/test_cleiton_franquia_validacao_admin_service.py` | citado no `README` e encontrado anteriormente em residuos `.pyc` | `tests/test_admin_dashboard_auditoria_csv.py` e suites de franquia/autorizacao | consolidacao da validacao administrativa em cobertura mais ampla |
| `tests/test_stripe_webhook_route.py` | citado no `README` e encontrado anteriormente em residuos `.pyc` | `tests/test_cleiton_monetizacao_stripe_blockers.py` e `tests/test_cleiton_monetizacao_stripe_guardrails.py` | reorganizacao da cobertura de webhook e idempotencia dentro da suite Stripe atual |
| `tests/test_contratacao_stripe_route.py` | citado no `README` e encontrado anteriormente em residuos `.pyc` | `tests/test_cleiton_monetizacao_stripe_blockers.py` | consolidacao da cobertura HTTP de contratacao Stripe |
| `tests/test_admin_validacao_route.py` | encontrado anteriormente em residuos `.pyc` | `tests/test_admin_dashboard_auditoria_csv.py` e suites de franquia/autorizacao | legado de rota/admin substituido por trilhos atuais |
| `tests/test_plano_service.py` | encontrado anteriormente em residuos `.pyc` | suites de Stripe/franquia/auditoria com foco em comportamento atual | consolidacao por dominio funcional |
| `tests/test_stripe_invoice_paid_nested_metadata.py` | encontrado anteriormente em residuos `.pyc` | suites Stripe atuais com guardrails e conciliacao | consolidacao da cobertura de eventos Stripe |
| `tests/test_iugu_fase1.py` | encontrado anteriormente em residuos `.pyc` | sem substituto direto; escopo atual prioriza Stripe | legado de gateway anterior/removido |
| `tests/test_iugu_fase4.py` | encontrado anteriormente em residuos `.pyc` | sem substituto direto; escopo atual prioriza Stripe | legado de gateway anterior/removido |

## Suite critica atual por dominio

- Stripe e monetizacao: blockers, guardrails, contratacao, upgrade/downgrade, vinculo comercial e idempotencia.
- Cron seguro: autenticacao por `X-Cron-Secret` e fluxos oficiais de cron.
- Franquia e autorizacao: classificacao operacional, bloqueio, degradacao e mensagens de uso.
- Billing e reconciliacao: apropriacao, abatimento de consumo, reconciliacao e auditoria admin.

## Regra para futuras remocoes

- remover testes somente com evidencia forte de que:
  - o fluxo deixou de existir; ou
  - a cobertura foi substituida por teste melhor e verificavel; ou
  - o teste valida detalhe interno irrelevante sem proteger comportamento observavel.
- nunca remover primeiro a cobertura ligada a Stripe, cron, franquia, billing ou vinculo comercial.
