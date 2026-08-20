# Agente Frete

Mapa documental auditado em 2026-08-19. A consolidação mais detalhada está em `docs/estado_producao.md` e `docs/arquitetura_oficial.md`.

## Visão do produto

Agente Frete é o produto operado pela `Logcompleta Agentes Inteligentes LTDA`, com experiência pública e autenticada na mesma aplicação Flask.

Antes do login, a superfície principal é o Copiloto público da home. Depois do login, a experiência principal passa a ser a Júlia.

Os domínios ativos no código atual são:

- Copiloto público: discovery e triagem inicial na home.
- Júlia: assistente operacional do ambiente autenticado, com suporte documental.
- Cleide: auditoria de fretes.
- AgenteCompara: comparação de até 3 tabelas de frete sobre a mesma base operacional.
- Roberto: BI de fretes; a rota existe, mas está escondida/não priorizada na experiência atual.
- Cleiton: governança, orquestração, billing técnico e observabilidade.

## Arquitetura em alto nível

- aplicação principal em `app/web.py`;
- stack principal: Flask + templates + Bootstrap;
- persistência transacional em PostgreSQL via `DATABASE_URL`;
- schema governado por Alembic em `migrations/versions/`;
- persistência técnica em `APP_DATA_DIR` para documentos, índices, artefatos temporários e resultados fora da sessão;
- deploy Render com `build.sh` + `start.sh`, e `db upgrade` automático no boot.

## Superfícies principais

- `/`: home pública com Copiloto, onboarding, aquisição e consentimento.
- `/chat_julia`: copiloto público e modo operacional autenticado.
- `/auditoria-frete`: Cleide Auditoria.
- `/agente-compara`: comparação multitabela.
- `/fretes`: Roberto BI.
- `/feed`: feed editorial misto de artigos e insights.
- `/contrate-um-plano`, `/perfil`, `/perfil/regularizar-pagamento`: billing e área do usuário.
- `/termos-de-uso` e `/politica-de-privacidade`: documentos legais ativos.

## Estado atual relevante

- branch operacional de produção documentada: `producao`;
- migration head esperada em produção: `y9z0a1b2c3d4`;
- deploy atual executa `python -m flask --app app.web db upgrade` antes do Gunicorn;
- `APP_ENV` é obrigatório e aceito apenas como `dev`, `homolog` ou `prod`;
- em homolog/prod, `APP_DATA_DIR` e `INDICES_FILE_PATH` precisam apontar para storage persistente;
- Stripe usa `/api/webhook/stripe`, com `invoice.paid` como evento principal de confirmação contratual;
- consentimento de marketing, suppression, newsletter, lifecycle e masking outbound já estão implantados e documentados abaixo.

## Documentação principal

- arquitetura do produto: `docs/arquitetura_oficial.md`
- deploy e operação: `app/README_DEPLOY.md`
- estado consolidado de produção: `docs/estado_producao.md`
- LGPD, lifecycle e retenção: `docs/lgpd_governanca_tecnica.md`
- consentimento e marketing: `docs/consentimento_privacidade_marketing.md`
- newsletter e suppression: `docs/comunicacoes_newsletter_suppression.md`
- IA externa e masking: `docs/integracoes_ia_privacidade.md`
- governança técnica dos documentos legais: `docs/governanca_documentos_legais.md`
- monetização e franquias: `docs/guia_monetizacao_franquias.md`
- Cleide Auditoria: `docs/cleide_auditoria_operacional.md`
- runtime e observabilidade: `docs/runtime_ia_e_observabilidade.md`
- execução local: `app/README_RUN.md`
