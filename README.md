# LogCompleta / AgenteFrete

Estado de referência confirmado no repositório em 2026-07-20:

- homolog local em `c2575f8`;
- entrega recente: Agente Compara + melhorias em Cleide e Júlia;
- produção informada pelo processo operacional: `8edae63`;
- backup de homolog informado: `backup/homolog-antes-agente-compara-20260720`;
- backup de produção informado: `backup/producao-antes-agente-compara-20260720`.

## Visão do produto

O projeto reúne múltiplas superfícies SaaS para logística e frete:

- Cleiton: discovery público, governança operacional, billing, observabilidade e administração;
- Júlia: consultoria operacional autenticada com contexto documental temporário;
- Roberto: BI e chat analítico de fretes em `/fretes`;
- Cleide: BI legado e auditoria documental de frete em `/auditoria-frete`;
- Agente Compara: fluxo documental e analítico próprio em `/agente-compara`.

## Arquitetura resumida

- app Flask monolítica em `app/web.py`, com blueprints para admin, área do usuário, operações, Cleide, Agente Compara e documentos da Júlia;
- autenticação via senha e Google OAuth, com `next` seguro e bloqueio de redirecionamentos externos;
- banco principal obrigatório em PostgreSQL via `DATABASE_URL`;
- migrations Alembic versionadas em `migrations/versions/`;
- trilha documental temporária em JSON técnico (`app/cleiton_doc_tmp/`), fora do banco;
- consumo operacional, consumo de IA e monetização registrados separadamente.

## Agentes e módulos

- Cleiton: onboarding/discovery, cron, regras de franquia, custos, monetização, governança documental e admin.
- Júlia: chat operacional em `/chat_julia?mode=operational` e APIs `/api/julia/documents/*`.
- Roberto: upload, BI e chat autenticado em `/fretes` e `/api/chat_roberto`.
- Cleide Auditoria: upload documental, `temp_table`, coverage, lote auditado, correções, BI e chat pós-BI em `/api/cleide-auditoria/*`.
- Agente Compara: upload documental, `temp_table`, coverage, lote auditado, correções, BI e chat pós-BI em `/api/agente-compara/*`.

## Estrutura do repositório

- `app/`: aplicação, blueprints, serviços, prompts, templates e JS.
- `docs/`: arquitetura, estado consolidado, monetização, troubleshooting e runbooks.
- `migrations/`: cadeia Alembic existente.
- `tests/`: suíte por domínio, com cobertura explícita para autenticação, Júlia, Cleide, Agente Compara, billing, métricas e cron.
- `render.yaml`, `start.sh`, `build.sh`: infraestrutura e boot.

## Configuração local

Variáveis essenciais confirmadas pelo código:

- `APP_ENV=dev|homolog|prod`
- `DATABASE_URL` PostgreSQL
- `SECRET_KEY`
- `APP_DATA_DIR`
- `INDICES_FILE_PATH`
- `PUBLIC_BASE_URL`
- `CRON_SECRET`

Variáveis dependentes de ambiente:

- OAuth Google;
- Stripe;
- chaves Gemini;
- e-mail;
- URLs comerciais e parâmetros Gunicorn.

## Execução

O boot versionado aplica migrations antes do servidor:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

```powershell
.\.venv\Scripts\python.exe -m flask --app app.web db upgrade
.\.venv\Scripts\python.exe -m flask --app app.web run
```

No Render, `start.sh` executa `python -m flask --app app.web db upgrade` e depois sobe Gunicorn.

## Testes

A suíte inclui, entre outras áreas:

- `tests/test_auth_next_redirect.py`
- `tests/test_julia_documents_api.py`
- `tests/test_cleide_audit_*`
- `tests/test_agente_compara_*`
- `tests/test_cron_auth.py`
- `tests/test_ia_metrics_service.py`

Contagens históricas de testes pertencem à fotografia de cada entrega e não devem ser tratadas como número fixo.

## Banco, migrations e temporários

- a entrega atual não criou migration, tabela, coluna ou banco novo;
- alterações futuras de schema exigem migration versionada;
- `.db` locais e `*.sqlite*` são ignorados e não fazem parte do deploy;
- `app/cleiton_doc_tmp/` guarda JSON técnico temporário, inclusive `tt_*.json`;
- esses JSONs não são tabelas de banco nem persistência definitiva.

## Ambientes e deploy

- desenvolvimento e homologação usam `homolog` nesta fotografia;
- produção operacional informada no processo usa `producao`, mas o `render.yaml` versionado ainda aponta o serviço de produção para `main`;
- o `render.yaml` também declara `healthCheckPath: /health`, enquanto o código expõe `/health/liveness` e `/health/readiness`.

Essas divergências devem ser tratadas como operacionais e validadas antes de deploy.

## Documentação especializada

- `docs/arquitetura_oficial.md`
- `docs/estado_oficial_consolidado.md`
- `docs/guia_monetizacao_franquias.md`
- `docs/runtime_ia_e_observabilidade.md`
- `docs/cleide_auditoria_operacional.md`
- `docs/troubleshooting_operacional.md`
- `docs/onboarding_tecnico.md`
- `app/README_RUN.md`
- `app/README_DEPLOY.md`
