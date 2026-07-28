# Estado Oficial Consolidado

Fotografia consolidada em 2026-07-20 com base no código local e no contexto operacional informado:

- homolog local: `c2575f8`;
- produção promovida informada: `8edae63`;
- backup homolog: `backup/homolog-antes-agente-compara-20260720`;
- backup produção: `backup/producao-antes-agente-compara-20260720`.

## Produto

- Cleiton: discovery, governança, billing, cron, admin e observabilidade.
- Júlia: consultoria operacional autenticada com documentos temporários compartilhados.
- Roberto: BI e chat quantitativo em `/fretes`.
- Cleide: BI legado e auditoria documental em `/auditoria-frete`.
- Agente Compara: novo módulo documental e analítico em `/agente-compara`.

## Entrega confirmada

- novo módulo Agente Compara, com página pública, APIs protegidas, template oficial, `temp_table`, coverage, lote auditado, correções, BI e chat analítico;
- melhorias no fluxo documental da Júlia, ainda apoiado no store temporário compartilhado do Cleiton;
- melhorias na Cleide Auditoria, com continuidade de billing idempotente, BI e chat pós-BI;
- integração do Agente Compara ao painel administrativo, billing e métricas de IA/processamento;
- nenhuma migration, tabela, coluna ou banco novo nesta entrega.

## Ambientes e Git

- branch local auditada: `homolog`;
- worktree local auditado: limpo no início desta atividade;
- `render.yaml` versionado ainda aponta produção para `main`, embora o processo operacional informado trate `producao` como branch oficial de produção;
- `start.sh` infere `APP_ENV=prod` para `main|master|producao|prod`.

## Infraestrutura confirmada no código

- migrations aplicadas no boot por `start.sh`;
- cron protegida por `X-Cron-Secret` ou `?secret=` temporário;
- rotas expostas para saúde: `/health/liveness` e `/health/readiness`;
- `render.yaml` versionado ainda usa `healthCheckPath: /health`, o que deve ser tratado como divergência operacional vigente.

## Temporários, banco e artefatos locais

- `app/cleiton_doc_tmp/` e `tt_*.json` são temporários técnicos e não banco;
- `.db`, `.sqlite`, `.sqlite3`, `.venv/`, `.tmp_pytest_fixture/` e caches são ignorados pelo Git;
- templates oficiais versionados: `template_cleide_auditoria_frete.xlsx` e `template_agente_compara.xlsx`.

## Testes

O repositório possui cobertura explícita para autenticação, cron, Júlia, Cleide, Agente Compara, billing e métricas. Contagens de testes aprovados anteriores são históricas e não devem ser projetadas automaticamente para este commit sem nova execução da suíte.

## Riscos residuais reais

- divergência entre branch de produção informada e `render.yaml` versionado;
- divergência entre `healthCheckPath` versionado e rotas reais de health check;
- confirmações de Render, variáveis e branches ativas permanecem dependentes de ambiente e não são comprováveis só pelo código local.
