# LogCompleta / AgenteFrete

Referência principal auditada em 2026-07-28: `docs/estado_oficial_consolidado.md`.

## Visão do produto

O repositório continua concentrando as superfícies de Cleiton, Júlia, Roberto, Cleide e AgenteCompara em uma aplicação Flask única, com PostgreSQL como banco oficial e trilhas técnicas temporárias em JSON para fluxos documentais e de cálculo.

A visão oficial do AgenteCompara agora é a de um fluxo multitabela com:

- 2 tabelas obrigatórias e 3ª opcional;
- estado em sessão por `comparison_id` e `table_id`;
- extração técnica para `temp_table`;
- revisão humana;
- configuração fiscal global;
- coverage opcional;
- arquivo operacional com template oficial;
- cálculo comparativo com lock, fingerprint, idempotência e storage dedicado fora da sessão;
- observabilidade e billing operacional próprios.

## Arquitetura resumida

- aplicação principal em `app/web.py`;
- schema governado por Alembic em `migrations/versions/`;
- boot versionado via `start.sh`, com `db upgrade` antes do Gunicorn;
- infraestrutura versionada em `render.yaml`;
- temporários técnicos sob `app/cleiton_doc_tmp/`.

## Banco e migrations

A entrega recente do AgenteCompara não adicionou migrations, tabelas nem colunas novas. A cadeia versionada permanece linear até `r2s3t4u5v6w7`, mas o sistema continua dependendo da aplicação das migrations existentes no boot.

## Deploy e ambientes

O código versionado comprova:

- homolog em `homolog` com `autoDeploy: true` no `render.yaml`;
- produção em `main` com `autoDeploy: false` no `render.yaml`;
- health checks reais em `/health/liveness` e `/health/readiness`;
- `healthCheckPath: /health` ainda declarado no YAML.

O processo operacional informado para a última entrega registra homologação em `homolog`, promoção por merge para `producao` e deploy manual no Render. Essa diferença entre processo e YAML continua sendo uma divergência operacional aberta.

## Arquivos locais ignorados

Não entram no Git nem no deploy versionado:

- `app/indices.json`;
- `cache_*.json`, incluindo `cache_noticias.json`;
- `scripts/security/rotation-report*.json`;
- arquivos `*.db`, `*.sqlite` e `*.sqlite3`;
- `app/cleiton_doc_tmp/`.

Templates oficiais `.xlsx` seguem versionados por exceção explícita do `.gitignore`.

## Documentação oficial

- visão consolidada: `docs/estado_oficial_consolidado.md`
- arquitetura: `docs/arquitetura_oficial.md`
- runtime e observabilidade: `docs/runtime_ia_e_observabilidade.md`
- execução local: `app/README_RUN.md`
- deploy: `app/README_DEPLOY.md`
- troubleshooting: `docs/troubleshooting_operacional.md`
