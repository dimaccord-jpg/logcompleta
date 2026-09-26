# Índice e governança da documentação

Inventário revisado para a produção `fa98317` (Sprint 12). A documentação viva descreve o comportamento da árvore versionada desse release; datas e conclusões de relatórios históricos pertencem às respectivas entregas.

Classes: **A** = canônica viva; **B** = histórica/auditoria; **C** = temporária; **D** = duplicada/obsoleta. A extensão Markdown não torna um arquivo necessariamente documentação: prompts carregados pela aplicação são configuração funcional.

## A — Guias vivos

| Documento | Autoridade / uso |
|---|---|
| [README](../README.md) | Entrada do projeto e navegação |
| [Índice documental](indice_documentacao.md) | Classificação e fontes canônicas |
| [Estado de produção](estado_producao.md) | Release implantado (Sprint 12), pendências e pós-release |
| [Arquitetura](arquitetura_oficial.md) | Stack, domínios, tema, auth e isolamento |
| [Multiuser V1](multiuser_v1.md) | Regras funcionais, Stripe, capacity/quantity e lifecycle |
| [Monetização e franquias](guia_monetizacao_franquias.md) | Catálogo de planos, consumo e billing técnico |
| [Agentes](AGENTS.md) | Identidade pública AgenteFrete vs nomes internos |
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
| [Templates HTML](../app/GUIA_TEMPLATES_HTML.md) | Guia visual, tema global e superfícies |
| [Discovery](runbook_onboarding_copilot.md) | Operação do onboarding/discovery e handoffs |
| [Runtime e observabilidade](runtime_ia_e_observabilidade.md) | Eventos, billing e diagnóstico |
| [Troubleshooting](troubleshooting_operacional.md) | Triagem operacional |
| [Privacidade técnica](lgpd_governanca_tecnica.md) | Lifecycle, retenção e separação da revogação Multiuser |
| [IA e privacidade](integracoes_ia_privacidade.md) | Fronteira de saída e masking |
| [Governança contextual de IA](cleiton_ai_data_governance.md) | Contrato canônico de classificação e minimização local antes do provedor |
| [Consentimento de marketing](consentimento_privacidade_marketing.md) | Cookie, endpoint e medição opcional; permanece válido |
| [Comunicações/newsletter](comunicacoes_newsletter_suppression.md) | Suppression e newsletter; histórico de backfills identificado |
| [Documentos legais](governanca_documentos_legais.md) | Governança técnica; não substitui documentos jurídicos |
| [Storage de documentos legais](runbooks/documentos_legais_storage_persistente.md) | Runbook de persistência; permanece válido |
| [Marketing](guia_de_mkt.md) | Posicionamento, limites, SEO editorial e evergreen |

Não é necessário replicar regras Multiuser em todos os guias: o contrato está em `multiuser_v1.md`; o estado de release e as pendências estão em `estado_producao.md`. Guias de consentimento, newsletter e documentos legais mantêm suas regras próprias.

## B — Histórico/auditoria preservado

| Documento | Status / motivo |
|---|---|
| `docs/changelog_consolidado.md` | **historical / superseded** — marco de maio/2026; não é estado permanente |
| `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md` | Fotografia da publicação AgenteCompara em agosto de 2026 |
| `docs/auditoria_documental_2026-05-29.md` | Auditoria documental datada |
| `docs/testes_removidos.md` | Registro de remoções/substituições e referências históricas |
| `docs/runbooks/cleide_homologacao_controlada_checklist.md` | Checklist histórico da entrega Cleide |
| `AUDITORIA_AGENTEFRETE.md` | Relatório local de auditoria (se presente no workspace) |
| `AUDITORIA_HOLISTICA_FINAL_MULTIUSER_V1_20260916.md` | Auditoria local do Multiuser anterior ao release |
| `AUDITORIA_INDEPENDENTE_F5_20260912.md` | Auditoria local F5 |
| `AUDITORIA_INDEPENDENTE_FINAL_F4_20260912.md` | Auditoria local F4 |
| `AUDITORIA_INDEPENDENTE_FINAL_F8_20260914.md` | Auditoria local F8 |
| `VERIFICACAO_FUNCIONAL_F4_20260912.md` | Verificação local F4 |

Esses arquivos são preservados. Expressões como “estado vigente”, heads antigos e condições de release dentro deles valem para sua data/escopo, não para a produção atual (Sprint 12). Não foram promovidos a documentação viva nem excluídos.

## C — Material temporário

`UAT_7.7A_comando_executado.txt`, arquivos `.tmp*`, saídas de pytest e artefatos de execução locais são evidências auxiliares/temporárias. Não definem contrato funcional.

## D — Ponto de entrada substituído

`docs/estado_oficial_consolidado.md` permanece como redirecionamento para preservar links existentes; não duplica o estado de produção.

## Arquivos funcionais fora do escopo documental

`app/copilot_capabilities.md` é referência viva **A de natureza funcional**, carregada por `app/copilot_capabilities.py` para uso pelo runtime. Ficou intacta nesta revisão exclusivamente documental: editá-la alteraria comportamento. Expressões de capacidade do prompt não substituem as capacidades comprovadas nos guias dos agentes.

Diretórios de configuração de ferramentas (`.cursor/`, etc.) não constituem guia canônico do produto.

## Base de verdade e limites de atualização

- Referência de código: commit de produção `fa98317` (Sprint 12). O checkout de trabalho pode estar em `homolog` com commits equivalentes ao conteúdo funcional merged.
- Runtime: services/routes/models, `render.yaml`, `start.sh`, migrations e testes existentes foram confrontados com as regras documentadas.
- Estado externo: implantação no Render, catálogo Stripe e validação operacional das histórias sem artefato no repo são informações da operação. Não houve leitura de banco de produção nesta revisão.
- `.env.prod` local **não** substitui a configuração remota do Render.
- Contagens permanentes de testes (“N testes passam”) não pertencem a guias vivos; prefira “há cobertura automatizada para X”.

Ao atualizar o release, revisar primeiro produção e implementação, depois os guias A pertinentes. Manter registros B com sua data e contexto, sem reescrever resultados históricos.
