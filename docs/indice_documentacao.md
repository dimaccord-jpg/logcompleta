# Índice e governança da documentação

Inventário revisado para a produção `bd874ee` (Sprint 11). A documentação viva descreve o comportamento da árvore versionada desse commit; datas e conclusões de relatórios históricos pertencem às respectivas entregas.

Classes: **A** = canônica viva; **B** = histórica/auditoria; **C** = temporária; **D** = duplicada/obsoleta. A extensão Markdown não torna um arquivo necessariamente documentação: prompts carregados pela aplicação são configuração funcional.

## A — Guias vivos

| Documento | Autoridade / uso |
|---|---|
| [README](../README.md) | Entrada do projeto e navegação |
| [Índice documental](indice_documentacao.md) | Classificação e fontes canônicas |
| [Estado de produção](estado_producao.md) | Release implantado, pendências e pós-release |
| [Arquitetura](arquitetura_oficial.md) | Stack, domínios, organização e isolamento |
| [Multiuser V1](multiuser_v1.md) | Regras funcionais, Stripe, capacity/quantity e lifecycle |
| [Monetização e franquias](guia_monetizacao_franquias.md) | Catálogo de planos, consumo e billing técnico |
| [Agentes](AGENTS.md) | Identidades e responsabilidades |
| [Onboarding técnico](onboarding_tecnico.md) | Ordem de leitura e premissas de desenvolvimento |
| [Deploy](DEPLOYMENT.md) | Ambientes, branches, promoção e start |
| [Operação do deploy](../app/README_DEPLOY.md) | Variáveis, storage e checklist de deploy |
| [Execução local](../app/README_RUN.md) | Ambiente de desenvolvimento e validação |
| [Banco e migrations](DATABASE_AND_MIGRATIONS.md) | Cadeia física e segurança do schema |
| [README de migrations](../migrations/README) | Entrada local para o guia de banco e comando oficial |
| [Cron Render](../RENDER_CRON_HOMOLOG.md) | Rotas de cron e ambiente-alvo |
| [Segredos](../SECURITY_SECRETS.md) | Política de credenciais |
| [Rotação de segredos](../scripts/security/ROTATION_AUTOMATION.md) | Utilitários e limites da automação |
| [AgenteAudita](cleide_auditoria_operacional.md) | Fluxo de auditoria de fretes e isolamento |
| [AgenteCompara](agente_compara_estado_oficial.md) | Fluxo comparativo, resultados e isolamento |
| [Templates HTML](../app/GUIA_TEMPLATES_HTML.md) | Guia visual e superfícies existentes |
| [Discovery](runbook_onboarding_copilot.md) | Operação do onboarding e handoffs |
| [Runtime e observabilidade](runtime_ia_e_observabilidade.md) | Eventos, billing e diagnóstico |
| [Troubleshooting](troubleshooting_operacional.md) | Triagem operacional |
| [Privacidade técnica](lgpd_governanca_tecnica.md) | Lifecycle, retenção e separação da revogação Multiuser |
| [IA e privacidade](integracoes_ia_privacidade.md) | Fronteira de saída e masking |
| [Governança contextual de IA](cleiton_ai_data_governance.md) | Contrato canônico de classificação e minimização local antes do provedor |
| [Consentimento de marketing](consentimento_privacidade_marketing.md) | Cookie, endpoint e medição opcional; permanece válido |
| [Comunicações/newsletter](comunicacoes_newsletter_suppression.md) | Suppression e newsletter; histórico de backfills identificado |
| [Documentos legais](governanca_documentos_legais.md) | Governança técnica; não substitui documentos jurídicos |
| [Storage de documentos legais](runbooks/documentos_legais_storage_persistente.md) | Runbook de persistência; permanece válido |
| [Marketing](guia_de_mkt.md) | Posicionamento, limites e SEO editorial |

Não é necessário replicar regras Multiuser em todos os guias: o contrato está em `multiuser_v1.md`; o estado de release e as pendências estão em `estado_producao.md`. Guias de consentimento, newsletter e documentos legais mantêm suas regras próprias, sem alterações artificiais por causa do Multiuser.

## B — Histórico/auditoria preservado

| Documento | Motivo para não usar como guia atual |
|---|---|
| `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md` | Fotografia da publicação AgenteCompara em agosto de 2026 |
| `docs/auditoria_documental_2026-05-29.md` | Auditoria documental datada |
| `docs/changelog_consolidado.md` | Registro consolidado de maio de 2026, não estado permanente |
| `docs/testes_removidos.md` | Registro de remoções/substituições e referências históricas |
| `docs/runbooks/cleide_homologacao_controlada_checklist.md` | Checklist histórico da entrega Cleide, com commits e contagens próprios |
| `AUDITORIA_AGENTEFRETE.md` | Relatório local de auditoria |
| `AUDITORIA_HOLISTICA_FINAL_MULTIUSER_V1_20260916.md` | Auditoria local do Multiuser em data anterior ao release |
| `AUDITORIA_INDEPENDENTE_F5_20260912.md` | Auditoria local F5 |
| `AUDITORIA_INDEPENDENTE_FINAL_F4_20260912.md` | Auditoria local F4 |
| `AUDITORIA_INDEPENDENTE_FINAL_F8_20260914.md` | Auditoria local F8 |
| `VERIFICACAO_FUNCIONAL_F4_20260912.md` | Verificação local F4 |

Esses arquivos são preservados integralmente. Expressões como “estado vigente”, heads antigos e condições de release dentro deles valem para sua data/escopo, não para a produção atual. Não foram promovidos a documentação viva nem excluídos. Os relatórios locais já estavam sem rastreamento no Git antes desta revisão.

## C — Material temporário

`UAT_7.7A_comando_executado.txt`, arquivos `.tmp*`, saídas de pytest e artefatos de execução locais são evidências auxiliares/temporárias. Não definem contrato funcional e não foram alterados ou removidos nesta revisão. Código de teste local não é documentação canônica.

## D — Ponto de entrada substituído

`docs/estado_oficial_consolidado.md` foi substituído por guias de domínio. O arquivo permanece como redirecionamento para preservar links existentes; não duplica o estado de produção. Nenhum documento foi excluído.

## Arquivos funcionais fora do escopo documental

`app/copilot_capabilities.md` é referência viva **A de natureza funcional**, carregada por `app/copilot_capabilities.py` para uso pelo runtime. Ficou intacta: sua edição alteraria comportamento, contrariando o escopo exclusivamente documental. As expressões de capacidade do prompt (por exemplo, previsão e vencedoras) não substituem as capacidades comprovadas descritas nos guias dos agentes e dos contratos de resultado.

Diretórios de configuração de ferramentas, como `.cursor/`, não constituem guia canônico do produto e foram preservados. Nenhum ADR separado ou outro guia versionado em `.rst` foi encontrado no inventário; arquitetura e decisões operacionais estão nos guias acima.

## Base de verdade e limites de atualização

- Referência de código: commit `bd874ee`, cuja árvore versionada coincide com o checkout `homolog` auditado. A operação informou que `bd874ee` está implantado em `producao`.
- Runtime: services/routes/models, `render.yaml`, `start.sh`, migrations e testes existentes foram confrontados com as regras documentadas.
- Estado externo: implantação, catálogo Stripe, eventos habilitados e pendências são informações fornecidas pela operação. Não houve leitura de banco de produção nem consulta ao painel Stripe/Render nesta revisão.
- Evidência de testes não equivale a teste financeiro real. As limitações de validação constam em [estado de produção](estado_producao.md#pendências-conhecidas-e-pós-release).

Ao atualizar o release, revisar primeiro produção e implementação, depois os guias A pertinentes. Manter registros B com sua data e contexto, sem reescrever resultados históricos.
