# LogCompleta / AgenteFrete

Referência principal auditada em 2026-08-05: `docs/estado_oficial_consolidado.md`.

## Visão do produto

O repositório continua concentrando as superfícies de Cleiton, Júlia, Roberto, Cleide e AgenteCompara em uma aplicação Flask única, com PostgreSQL como banco oficial e trilhas técnicas temporárias em JSON para fluxos documentais, lotes e cálculo comparativo.

A visão oficial do AgenteCompara no código atual é a de um fluxo multitabela com:

- 2 tabelas obrigatórias e 3ª opcional;
- estado em sessão por `comparison_id`, `table_id` e slot;
- extração técnica para `temp_table`;
- revisão humana por tabela;
- configuração fiscal global;
- coverage opcional;
- arquivo operacional com template oficial;
- cálculo comparativo com lock, fingerprint, idempotência, gate de completude e storage dedicado fora da sessão;
- memória pública determinística por linha, incluindo componentes aplicados/ignorados e diagnósticos de cálculo;
- validação determinística de taxas acessórias antes do avanço da revisão;
- analytics comparativo leve no resultado liberado, chat contextual comparativo pós-READY e observabilidade/billing operacional próprios.

## Arquitetura resumida

- aplicação principal em `app/web.py`;
- página do AgenteCompara em `app/agente_compara_routes.py`;
- APIs do AgenteCompara em `app/agente_compara_api_routes.py`;
- schema governado por Alembic em `migrations/versions/`;
- boot versionado via `start.sh`, com `db upgrade` antes do Gunicorn;
- infraestrutura versionada em `render.yaml`;
- temporários técnicos sob `app/cleiton_doc_tmp/`.

## Banco e migrations

A entrega recente do AgenteCompara não adicionou migration, tabela nem coluna nova. A cadeia versionada permanece linear até `r2s3t4u5v6w7`, mas o sistema continua dependendo da aplicação das migrations existentes no boot.

## Deploy e ambientes

O código versionado comprova:

- branch local auditada: `homolog`;
- commit auditado na branch atual: `939b73e` (`feat: consolida fluxo e calculos do AgenteCompara`);
- `origin/homolog` apontando para o mesmo conteúdo do checkout local auditado;
- `origin/producao` apontando para o commit promovido `fdec64a` (`feat: consolida fluxo e calculos do AgenteCompara`);
- homolog em `homolog` com `autoDeploy: true` no `render.yaml`;
- produção em `main` com `autoDeploy: false` no `render.yaml`;
- `start.sh` inferindo `APP_ENV=prod` para `main`, `master`, `producao` e `prod`;
- health checks reais em `/health/liveness` e `/health/readiness`;
- `healthCheckPath: /health` ainda declarado no YAML.

O processo operacional informado para a publicação validada em 2026-08-05 registra homologação em `939b73e`, promoção para produção na branch `producao` pelos commits `fdec64a`, `db72007` e `f9591dc`, com deploy manual no Render. A divergência entre a branch operacional informada (`producao`) e a branch versionada no `render.yaml` (`main`) continua aberta.

## Testes e dependências de desenvolvimento

- a suíte direcionada validada do AgenteCompara registrou `612 passed, 1 skipped, 2 warnings`;
- os warnings observados eram depreciações de bibliotecas externas, sem falha funcional atribuída ao fluxo publicado;
- `playwright>=1.40.0` foi adicionado somente em `requirements-dev.txt`;
- o Render instala apenas `requirements.txt` pelo `build.sh`, então Playwright não é dependência de produção nem etapa automática do deploy.

## Arquivos locais ignorados

Não entram no Git nem no deploy versionado:

- `app/indices.json`;
- `cache_*.json`, incluindo `cache_noticias.json`;
- `scripts/security/rotation-report*.json`;
- arquivos `*.db`, `*.sqlite` e `*.sqlite3`;
- `app/cleiton_doc_tmp/`;
- uploads temporários e diretórios locais de suporte operacional.

Templates oficiais `.xlsx` seguem versionados por exceção explícita do `.gitignore`.

## Documentação oficial

- visão consolidada: `docs/estado_oficial_consolidado.md`
- arquitetura: `docs/arquitetura_oficial.md`
- runtime e observabilidade: `docs/runtime_ia_e_observabilidade.md`
- execução local: `app/README_RUN.md`
- deploy: `app/README_DEPLOY.md`
- troubleshooting: `docs/troubleshooting_operacional.md`
