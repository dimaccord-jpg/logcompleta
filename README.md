# LogCompleta / AgenteFrete

Estado de referência: `homolog@6701a53`, em 2026-07-16.

## Estado operacional

- desenvolvimento e homologação: `homolog`;
- produção operacional: `producao`;
- commit promovido: `producao@0c3a133`;
- backup anterior: `backup/producao-antes-cleide-insights-20260716`;
- esta entrega não criou migration, tabela ou coluna;
- fotografia do commit: 1.896 testes aprovados; a contagem não é permanente.

O `render.yaml` ainda declara `main` no serviço de produção. Isso diverge do fluxo operacional validado; confirme a branch no painel do Render antes do deploy. `main` é a branch padrão remota, não a branch operacional de produção desta fotografia.

## Produto e superfícies

- Cleiton é gestor/copiloto: conduz discovery público e centraliza governança, autorização, limites, consumo e observabilidade.
- Júlia é a consultora operacional autenticada: interpreta logística e supply chain, apoia planejamento e usa contexto documental temporário. Não substitui auditoria nem previsão quantitativa.
- Roberto é o motor quantitativo em `/fretes`: processa histórico, exibe BI e gera previsão estatística; a IA apenas explica o resultado.
- Cleide audita retrospectivamente em `/auditoria-frete`: estrutura a tabela negociada, calcula o esperado e explica a diferença para o cobrado.

Rotas: `/` é discovery público ou Júlia logada; `/chat_julia?mode=operational`, `/fretes` e os destinos operacionais da Cleide exigem login; `/feed` é público. `/cleide-bi-frete` é o BI anterior da Cleide, legado e não indicado para auditoria de cobrança. A página `GET /auditoria-frete` é pública, mas sua API é autenticada e autorizada por franquia.

Artefato não define agente: intenção e horizonte definem o destino. Fontes: `app/capability_taxonomy.py`, `app/copilot_capabilities.py`, `app/copilot_capabilities.md` e `app/run_cleiton_discovery.py`.

## Cleide Auditoria

O contrato completo está em `docs/cleide_auditoria_operacional.md`. Fluxo resumido:

1. upload da tabela negociada;
2. extração para `temp_table` e revisão humana;
3. configuração fiscal e cobertura opcional;
4. upload CSV/XLSX do lote;
5. cálculo determinístico do esperado e comparação com o cobrado;
6. resultados, diagnósticos, correções e reprocessamento quando obsoleto;
7. BI executivo;
8. chat analítico liberado pelo backend somente após BI válido.

Estados da `temp_table`: `processing`, `awaiting_validation`, `validated`, `needs_review`, `failed`, `expired` e `discarded`. O artefato é temporário e isolado por documentos, sessão, usuário e franquia.

O BI tem quatro gráficos: impacto por transportadora, impacto por UF destino, evolução temporal e Pareto do cobrado a mais. Filtros de transportadora, UF origem, UF destino e data são aplicados no frontend e enviados como `visual_focus` ao chat.

## Autenticação

Destinos protegidos têm `requires_login=True`. Para anônimo, o handoff vira `/login?next=<destino>`; senha, OAuth e conclusão de perfil preservam o retorno. `_safe_next_redirect` aceita apenas caminho interno e rejeita URL absoluta, `//`, barra invertida, controles, `/api` e `/admin`. APIs retornam `401` JSON.

## Planos, consumo e observabilidade

- autorização: `app/services/cleiton_operacao_autorizacao_service.py`;
- billing idempotente: `CleitonBillingApropriacao` e `app/services/cleiton_upload_billing_service.py`;
- processamento não-LLM: `ProcessingEvent`;
- chamadas IA: `IaConsumoEvento`;
- custo/créditos: `CleitonCostConfig` e `app/services/cleiton_cost_service.py`.

Na auditoria, cobertura e upload do lote geram consumo por linhas. O upload inicial já cobra as linhas; o primeiro `audit/run` e cliques repetidos sem `needs_reprocess` não cobram novamente. Reprocessamento explícito de lote processado e obsoleto gera novo evento. Chat e insights podem consumir IA, mas não debitam linhas operacionais.

Bloqueios preservam o CTA de upgrade. Consumo operacional e IA são trilhas separadas e conciliáveis.

## Banco e temporários

Mudança futura de schema deve incluir migration versionada, aplicada antes de o código depender do novo schema e validada após deploy. Não faça ajuste manual sem migration correspondente.

O Git ignora `.venv/`, `.tmp_pytest_fixture/`, `.pytest_cache/`, `__pycache__/` e `app/cleiton_doc_tmp/`. `tt_*.json` são temporários e não entram no deploy. Antes de remover JSON, inspecione caminho e conteúdo; não apague JSON operacional fora de pasta temporária. Nunca versione `.venv`.

## Testes e guias

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Áreas principais: `tests/test_cleide_*`, autenticação, onboarding, billing, insights, métricas, Júlia, Roberto e contratos de interface. A fotografia de 1.896 aprovados pertence a `6701a53`.

Leitura: `docs/estado_oficial_consolidado.md`, `docs/arquitetura_oficial.md`, `docs/cleide_auditoria_operacional.md`, `docs/runtime_ia_e_observabilidade.md`, `docs/guia_monetizacao_franquias.md`, `app/README_RUN.md` e `app/README_DEPLOY.md`.
