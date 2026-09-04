# AgenteFrete

Mapa documental auditado em 2026-09-04 a partir do código atual. A consolidação operacional está em `docs/estado_producao.md` e `docs/arquitetura_oficial.md`.

## Visão do produto

AgenteFrete é a plataforma da `Logcompleta Agentes Inteligentes LTDA` para operação logística com agentes especializados, governança central do Cleiton e experiência web única em Flask.

No estado atual do código:

- a home pública prioriza o Copilot de discovery e a marca AgenteFrete;
- a identidade "Julia" continua existindo internamente e na superfície operacional autenticada;
- AgenteAudita é a identidade pública da auditoria de fretes; "Cleide" permanece como identidade técnica/histórica (rotas, services, agent IDs e `flow_type`);
- AgenteCompara e Roberto permanecem como módulos distintos;
- Cleiton concentra governança transversal, billing técnico, franquias, observabilidade e partes da orquestração.

## Home pública e Home autenticada

- **Home pública:** `/` com discovery do AgenteFrete, onboarding, aquisição, consentimento e CTA experimental `home_chat_cta_v1`. Não deve ser documentada como "Julia pública principal".
- **Home autenticada:** após o login, a experiência operacional principal continua em `/chat_julia?mode=operational`, com endpoint `/api/chat_julia`. A mensagem de bloqueio do endpoint autenticado diz: "É necessário estar logado para conversar com o AgenteFrete."

O AgenteFrete operacional também oferece orientação determinística para ferramentas internas após a resposta normal do chat. Essa camada não altera o motor conversacional, não cria segunda chamada LLM, resolve o destino com o capability resolver local e usa URLs da taxonomy. Destinos iniciais: AgenteAudita (`/auditoria-frete`), Roberto (`/fretes`) e AgenteCompara (`/agente-compara`). A ação abre a ferramenta em nova aba. Casos ambíguos não recebem handoff automático. A resolução é fail-open.

## Arquitetura em alto nível

- aplicação principal em `app/web.py`;
- stack principal: Flask + templates + Bootstrap + JavaScript vanilla;
- persistência transacional em PostgreSQL via `DATABASE_URL`;
- schema governado por Alembic em `migrations/versions/`;
- persistência técnica em `APP_DATA_DIR` para documentos, índices, artefatos temporários e resultados fora da sessão;
- deploy Render com `build.sh` + `start.sh`, e `db upgrade` automático no boot.

## Superfícies principais

- `/`: home pública com Copilot, onboarding, aquisição e consentimento.
- `/chat_julia?mode=operational`: superfície operacional autenticada da Julia/AgenteFrete.
- `/auditoria-frete`: AgenteAudita (APIs técnicas em `/api/cleide-auditoria/*`).
- `/agente-compara`: comparação multitabela.
- `/fretes`: Roberto BI e chat quantitativo.
- `/feed`: feed editorial misto de artigos e insights.
- `/contrate-um-plano`, `/perfil`, `/perfil/regularizar-pagamento`: billing e área do usuário.
- `/termos-de-uso` e `/politica-de-privacidade`: documentos legais ativos.

## Principais agentes

- **AgenteFrete / Julia operacional:** chat autenticado com contexto documental governado.
- **AgenteAudita:** superfície pública de auditoria de fretes, coverage, lote auditado, BI executivo e chat analítico. Os endpoints técnicos permanecem em `/api/cleide-auditoria/*`.
- **AgenteCompara:** comparação de 2 tabelas obrigatórias e 1 opcional sobre a mesma base operacional.
- **Roberto:** BI de fretes e chat quantitativo no domínio `/fretes`.
- **Cleiton:** governança, discovery, franquias, billing técnico, observabilidade e contratos semânticos.

## Funcionalidades atuais

- home pública com discovery e experimento `home_chat_cta_v1`;
- telemetria isolada na tabela `home_cta_experiment_event`;
- chat operacional autenticado em `/chat_julia?mode=operational`;
- auditoria de fretes em `/auditoria-frete`;
- comparação multitabela em `/agente-compara`;
- BI de fretes em `/fretes`;
- feed editorial em `/feed`;
- billing, área do usuário, termos e política de privacidade.

## Estrutura do projeto

- `app/`: aplicação, rotas, serviços, modelos e templates
- `docs/`: guias funcionais, operacionais e de governança
- `migrations/`: cadeia Alembic
- `tests/`: cobertura automatizada
- `scripts/`: utilitários operacionais e segurança

## Banco e migrations

- migration head atual no repositório: `z0a1b2c3d4e5`
- a migration `z0a1b2c3d4e5_home_cta_experiment_event.py` adiciona a tabela `home_cta_experiment_event`
- o guard operacional em `app/db_operational_safety.py` bloqueia downgrade sem confirmação explícita
- `upgrade` normal não é bloqueado por esse guard

## Desenvolvimento local

- executar a app com `APP_ENV`, `DATABASE_URL`, `SECRET_KEY` e `APP_DATA_DIR` válidos
- aplicar migrations com `python -m flask --app app.web db upgrade`
- rodar testes com `pytest`

Detalhes em `app/README_RUN.md`.

## Homologação e produção

- `render.yaml` aponta a branch `homolog` para homologação e a branch `producao` para produção
- ambos os serviços estão com `autoDeploy: true`
- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn
- o procedimento atual de promoção é: validar em `homolog` e, depois, promover para `producao` por fast-forward only, sem force push e sem tratar downgrade de banco como rotina

Detalhes em `docs/DEPLOYMENT.md` e `app/README_DEPLOY.md`.

## Segurança operacional de banco

- downgrade Alembic exige `ALLOW_DB_DOWNGRADE=1` e `ALLOW_DB_DOWNGRADE_DATABASE=<nome-exato>`
- operações destrutivas de schema de teste só são permitidas com `TESTING` e SQLite em memória real
- logs de segurança de banco não expõem senha nem URI completa
- masking outbound para IA externa existe, mas não promete anonimização universal de texto livre ou PDF bruto

## Documentação adicional

- arquitetura consolidada: `docs/arquitetura_oficial.md`
- agentes e identidades: `docs/AGENTS.md`
- banco e migrations: `docs/DATABASE_AND_MIGRATIONS.md`
- deploy e promoção: `docs/DEPLOYMENT.md`
- estado de produção: `docs/estado_producao.md`
- AgenteAudita: `docs/cleide_auditoria_operacional.md`
- AgenteCompara: `docs/agente_compara_estado_oficial.md`
- monetização e franquias: `docs/guia_monetizacao_franquias.md`
- LGPD, lifecycle e retenção: `docs/lgpd_governanca_tecnica.md`
- consentimento e marketing: `docs/consentimento_privacidade_marketing.md`
- newsletter e suppression: `docs/comunicacoes_newsletter_suppression.md`
- IA externa e masking: `docs/integracoes_ia_privacidade.md`
- documentos legais: `docs/governanca_documentos_legais.md`
- runtime e observabilidade: `docs/runtime_ia_e_observabilidade.md`
- execução local: `app/README_RUN.md`
