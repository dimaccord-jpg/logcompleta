# AgenteFrete

Guia do AgenteFrete. A produção informada pela operação executa `fa98317` (Sprint 12); comece pelo [estado de produção](docs/estado_producao.md), pela [arquitetura](docs/arquitetura_oficial.md) e pelo [índice documental](docs/indice_documentacao.md).

## Visão do produto

**AgenteFrete** é o assistente virtual especializado em logística da `Logcompleta Agentes Inteligentes LTDA`. É a identidade pública única da plataforma: o usuário não precisa conhecer nomes de agentes internos.

No estado atual do código:

- a home pública prioriza discovery e a marca AgenteFrete (definição: “Assistente virtual especializado em logística”);
- o chat operacional autenticado apresenta-se como **AgenteFrete** (não como Júlia);
- nomes internos (Júlia, Roberto, Cleide/AgenteAudita, Cleiton, AgenteCompara) existem por arquitetura/compatibilidade e aparecem como **habilidades/capacidades** do AgenteFrete, não como produtos separados;
- rotas e IDs técnicos (`/chat_julia`, `/api/chat_julia`, `run_julia_*`, `agent="julia"`) foram preservados por compatibilidade e **não** significam identidade pública;
- Cleiton concentra governança transversal, billing técnico, franquias, observabilidade e partes da orquestração.

O produto **não** é TMS, WMS nem ERP.

## Jornada pública → operacional

1. Home pública `/` — discovery anônimo e cards de habilidade
2. Escolha de habilidade (ou discovery com handoff)
3. Login quando a habilidade exige autenticação (`/login?next=...`)
4. Superfície operacional correspondente

Habilidades principais (rótulos externos):

| Rótulo | Destino | Auth |
|---|---|---|
| Consultar o AgenteFrete | `/chat_julia?mode=operational` | autenticado |
| Analisar fretes | `/fretes` | autenticado |
| Auditar cobranças de frete | `/auditoria-frete` | APIs autenticadas; página no shell autenticado |
| Comparar tabelas | `/agente-compara` | APIs autenticadas |

Catálogo canônico: `app/shell_navigation.py`. Discovery permanece público.

## Chat principal

- superfície autenticada: `/chat_julia?mode=operational`
- endpoint: `/api/chat_julia`
- system prompt e labels conversacionais: identidade **AgenteFrete**
- Júlia permanece persona interna/editorial (pipeline de conteúdo), não o rosto do chat operacional
- histórico da sessão: client-side no fluxo atual; **não** há threads persistentes como feature de produto
- mensagem de bloqueio: “É necessário estar logado para conversar com o AgenteFrete.”

Após a resposta normal, o AgenteFrete operacional pode oferecer orientação determinística para ferramentas internas (sem segunda chamada LLM, capability resolver local, URLs da taxonomy, nova aba, fail-open; ambíguos sem handoff). Destinos: AgenteAudita (`/auditoria-frete`), Roberto (`/fretes`), AgenteCompara (`/agente-compara`).

## Tema

Preferência global `system` | `dark` | `light` em `localStorage` (`af-theme`), aplicada aos templates que herdam `base.html`. Não há opt-in por página. Admin e `/acesso-desktop` ficam fora desse contrato. Detalhes em [templates](app/GUIA_TEMPLATES_HTML.md) e [estado de produção](docs/estado_producao.md).

## Arquitetura em alto nível

- aplicação principal em `app/web.py`;
- stack: Flask + templates + Bootstrap + JavaScript vanilla;
- persistência transacional em PostgreSQL via `DATABASE_URL`;
- schema governado por Alembic em `migrations/versions/`;
- persistência técnica em `APP_DATA_DIR`;
- deploy Render com `build.sh` + `start.sh`, e `db upgrade` automático no boot (`start.sh`).

## Superfícies principais

- `/`: home pública com discovery, habilidades, onboarding, aquisição e consentimento
- `/chat_julia?mode=operational`: chat operacional AgenteFrete (nome de rota técnico)
- `/auditoria-frete`: AgenteAudita (`/api/cleide-auditoria/*`)
- `/agente-compara`: comparação multitabela
- `/fretes`: Roberto BI e chat quantitativo (**login obrigatório**)
- `/feed` e `/noticia/<id>`: Feed editorial (news / analysis / evergreen)
- `/contrate-um-plano`, `/perfil`, `/perfil/regularizar-pagamento`: planos e área do usuário
- `/gestao-multiuser` e `/convite/<token>`: Multiuser
- `/acesso-desktop`: landing de campanha opcional/legada — **não** é gate do produto
- `/termos-de-uso` e `/politica-de-privacidade`: documentos legais

## Domínios internos (compatibilidade)

- **AgenteFrete (público)** / módulos `julia_*` (técnico): chat autenticado e contexto documental
- **AgenteAudita** / Cleide (técnico): auditoria de fretes
- **AgenteCompara**: comparação de tabelas
- **Roberto**: BI de fretes em `/fretes`
- **Cleiton**: governança, discovery, franquias, billing técnico, observabilidade, editorial/orquestração

## Funcionalidades atuais

- shell unificado de habilidades e Home
- tema global e responsividade mobile da Home/Login
- chat operacional AgenteFrete
- auditoria, comparação e BI autenticados
- Feed / SEO editorial / evergreen manual e automático
- imagem editorial com `gemini-3.1-flash-image` (`generate_content`)
- billing Free / Starter / Pro / Multiuser (checkout Multiusuário limpa embedded anterior)
- Multiuser V1: contratação Stripe, convites, aumentos, redução futura, revogação, titularidade e diagnóstico

Contrato Multiuser: [guia Multiuser](docs/multiuser_v1.md). Conta comum **não** compartilha artefatos privados entre membros.

## Estrutura do projeto

- `app/`: aplicação, rotas, serviços, modelos e templates
- `docs/`: guias funcionais, operacionais e de governança
- `migrations/`: cadeia Alembic
- `tests/`: cobertura automatizada
- `scripts/`: utilitários operacionais e segurança

## Banco e migrations

- migration head versionado atual: `g8h9i0j1k2l3` (Growth / SCRUM-148), com `down_revision=f7g8h9i0j1k2`; cadeia em [Banco e Migrations](docs/DATABASE_AND_MIGRATIONS.md)
- a migration Growth torna `FunnelEvent.user_id`, `conta_id` e `franquia_id` nullable; não cria nova tabela/modelo
- a migration `z0a1b2c3d4e5_home_cta_experiment_event.py` adiciona `home_cta_experiment_event`
- o guard em `app/db_operational_safety.py` bloqueia downgrade sem confirmação explícita

## Desenvolvimento local

- executar com `APP_ENV`, `DATABASE_URL`, `SECRET_KEY` e `APP_DATA_DIR` válidos
- `load_app_env()` carrega `app/.env.{APP_ENV}` com `override=False` (env do processo vence)
- aplicar migrations com `python -m flask --app app.web db upgrade`
- rodar testes com `pytest`

Detalhes em `app/README_RUN.md`.

## Homologação e produção

- `render.yaml`: branch `homolog` → homologação; branch `producao` → produção
- ambos com `autoDeploy: true`
- `start.sh` aplica `db upgrade` antes do Gunicorn e pode inferir `APP_ENV` pelo branch quando ausente
- promoção: validar em `homolog` → promover para `producao` por fast-forward only, sem force push; downgrade de banco não é rotina

Detalhes em `docs/DEPLOYMENT.md` e `app/README_DEPLOY.md`.

## Segurança operacional de banco

- downgrade Alembic exige `ALLOW_DB_DOWNGRADE=1` e `ALLOW_DB_DOWNGRADE_DATABASE=<nome-exato>`
- operações destrutivas de schema de teste só com `TESTING` e SQLite em memória real
- masking outbound para IA externa existe, mas não promete anonimização universal

## Documentação adicional

- arquitetura: `docs/arquitetura_oficial.md`
- agentes: `docs/AGENTS.md`
- banco: `docs/DATABASE_AND_MIGRATIONS.md`
- deploy: `docs/DEPLOYMENT.md`
- estado de produção: [estado_producao.md](docs/estado_producao.md)
- Multiuser V1: [multiuser_v1.md](docs/multiuser_v1.md)
- onboarding técnico: [onboarding_tecnico.md](docs/onboarding_tecnico.md)
- AgenteAudita: `docs/cleide_auditoria_operacional.md`
- AgenteCompara: `docs/agente_compara_estado_oficial.md`
- monetização: `docs/guia_monetizacao_franquias.md`
- LGPD: `docs/lgpd_governanca_tecnica.md`
- consentimento: `docs/consentimento_privacidade_marketing.md`
- newsletter: `docs/comunicacoes_newsletter_suppression.md`
- IA externa: `docs/integracoes_ia_privacidade.md`
- governança IA: `docs/cleiton_ai_data_governance.md`
- documentos legais: `docs/governanca_documentos_legais.md`
- runtime: `docs/runtime_ia_e_observabilidade.md`
- marketing/SEO: `docs/guia_de_mkt.md`
- execução local: `app/README_RUN.md`
