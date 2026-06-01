# Auditoria Documental

Data da auditoria: `2026-05-29`  
Commit de referencia: `20fa165`

Objetivo desta auditoria: localizar a documentacao existente, mapear aderencia ao codigo atual e registrar o que foi atualizado para refletir o estado real promovido.

## Documentacao atualizada

| Documento | Finalidade | Ultima atualizacao anterior | Aderencia anterior | Acao nesta auditoria |
| --- | --- | --- | --- | --- |
| `README.md` | Fonte principal de contexto funcional e operacional | `2026-05-26` (`a5eab21`) | Parcialmente desatualizado | Atualizado |
| `docs/estado_oficial_consolidado.md` | Fotografia do estado homologado/promovido | `2026-05-26` (`a5eab21`) | Parcialmente desatualizado | Atualizado |
| `docs/arquitetura_oficial.md` | Fronteiras e responsabilidades oficiais | `2026-05-26` (`a5eab21`) | Parcialmente desatualizado | Atualizado |
| `docs/runtime_ia_e_observabilidade.md` | Runtime de IA, eventos e metricas | `2026-05-26` (`a5eab21`) | Parcialmente desatualizado | Atualizado |
| `docs/onboarding_tecnico.md` | Entrada tecnica no projeto | `2026-05-26` (`a5eab21`) | Parcialmente desatualizado | Atualizado |
| `docs/troubleshooting_operacional.md` | Troubleshooting homolog/producao | `2026-05-26` (`a5eab21`) | Parcialmente desatualizado | Atualizado |
| `app/README_DEPLOY.md` | Deploy, promocao e migrations | `2026-05-13` (`6dc0072`) | Parcialmente desatualizado | Atualizado |
| `app/README_RUN.md` | Operacao local e smoke checks | `2026-05-13` (`6dc0072`) | Parcialmente desatualizado | Atualizado |
| `migrations/README` | Cadeia e operacao de migrations | `2026-04-06` (`c8e4447`) | Obsoleto para o estado atual | Atualizado |

## Documentacao parcialmente desatualizada

| Documento | Finalidade | Ultima atualizacao anterior | Divergencia principal |
| --- | --- | --- | --- |
| `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md` | Fotografia de homologacao/publicacao | `2026-05-13` (`6dc0072`) | Nao reflete integralmente Copilot conversational-first e hidden terms |
| `RENDER_CRON_HOMOLOG.md` | Cron de homolog | `2026-04-29` (`600b0b9`) | Continua util, mas nao cobre onboarding/word cloud/admin |
| `docs/guia_monetizacao_franquias.md` | Monetizacao, franquia e billing | `2026-05-13` (`6dc0072`) | Continua valido como trilho de governanca, mas nao detalha onboarding interno |
| `docs/changelog_consolidado.md` | Historico de mudancas | sem consolidacao recente | Nao e fonte de verdade do estado atual |

## Documentacao obsoleta

| Documento | Motivo |
| --- | --- |
| Nenhum arquivo foi removido nesta auditoria | Os documentos antigos foram mantidos como apoio historico, mas deixam de ser fontes canonicas quando conflitarem com os documentos atualizados |

## Documentacao ausente

Arquivos ausentes identificados e criados nesta auditoria:

- `docs/runbook_onboarding_copilot.md`  
  Runbook unico para onboarding, observabilidade, handoffs, dashboard, word cloud e promocao homolog -> producao.

- `docs/auditoria_documental_2026-05-29.md`  
  Relatorio formal da auditoria documental solicitada.

## Divergencias encontradas no inicio da auditoria

- O `README.md` e os docs centrais ainda priorizavam a arquitetura antiga `Roberto -> Cleiton -> Julia`, sem tratar o Copilot da Home como superficie oficial separada.
- A separacao entre Copilot e Julia estava clara no codigo, mas incompleta nos documentos.
- A regra-mae por atividade-fim e horizonte temporal existia em `app/copilot_capabilities.md`, mas nao estava consolidada na documentacao principal.
- A observabilidade de onboarding e a separacao entre `operational_tokens_month`, `onboarding_tokens_month` e `total_internal_tokens_month` estavam implementadas, mas nao descritas de forma canonica.
- O dashboard admin ja exibia a analise de termos do onboarding, Pareto 80/20 e hidden terms, mas isso nao estava documentado como fluxo oficial.
- A migration `r2s3t4u5v6w7` e o modelo `OnboardingWordCloudHiddenTerm` ainda nao estavam descritos nos documentos de banco e operacao.

## Fonte de verdade desta auditoria

Esta auditoria foi cruzada com:

- `app/copilot_capabilities.md`
- `app/static/js/chat_behavior.js`
- `app/web.py`
- `app/run_cleiton_discovery.py`
- `app/copilot_capabilities.py`
- `app/models.py`
- `app/services/ia_metrics_service.py`
- `app/services/onboarding_admin_analytics_service.py`
- `app/services/onboarding_word_cloud_hidden_terms_service.py`
- `app/painel_admin/admin_routes.py`
- `migrations/versions/r2s3t4u5v6w7_onboarding_word_cloud_hidden_term.py`
