# Deploy em Homolog/Produção — Guia Operacional

Este documento é o runbook operacional de deploy para homolog/prod da entrega Fase 2.

Status/go-no-go de homologação: `../DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.

## 1) Premissas obrigatórias

- `APP_ENV` definido explicitamente (`homolog` ou `prod`).
- `DATABASE_URL` apontando para o banco correto do ambiente.
- Segredos no provedor (não em arquivo versionado): `SECRET_KEY`, `CRON_SECRET`, OAuth, e-mail, BigQuery.
- Persistência configurada para runtime (`APP_DATA_DIR`, `INDICES_FILE_PATH` ou `RENDER_DISK_PATH`).

## 2) O que precisa subir junto (pacote final Fase 2)

- Upload Roberto com identidade de execução (`execution_id`) e billing idempotente.
- Governança operacional Cleiton (conversão para créditos e status por franquia).
- Modelo conta/franquia/plano/multiuser e telas admin de operação/validação.
- Migrations da cadeia Fase 2.
- Testes automatizados do motor/classificação/idempotência/autorização.

## 3) Checkpoint crítico antes de publicar homolog

Bloqueio atual conhecido em Render:

- `alembic` ausente no `PATH`.
- `python -m alembic current` falha com `No module named alembic`.
- `requirements.txt` sem Alembic.

Sem resolver essa etapa, a publicação final em homolog não é segura.

## 4) Sequência segura de deploy (homolog)

1. Publicar código da branch homolog no serviço.
2. Validar variáveis/segredos e persistência do ambiente.
3. Resolver estratégia de migration para o runtime de homolog.
4. Executar migrations (`upgrade head`) no banco de homolog.
5. Confirmar estado da migration (`current`) no banco alvo.
6. Executar validação funcional e operacional (health, cron, admin e fluxos de Fase 2).

## 5) Validação pós-deploy (mínima)

- `GET /health/liveness` retorna 200.
- `GET /health/readiness` retorna 200 (ou investigar se 503).
- `/cron/executar-cleiton`: 403 sem segredo e 200 com segredo.
- `/cron/billing-snapshot` responde sem erro.
- `python -m app.finance` atualiza índices no caminho persistente.
- Tela admin (`/admin/dashboard`, `/admin/agentes/julia`, `/admin/controle-usuarios`, `/admin/planos`) abre sem regressão funcional crítica.
- Chat Júlia e upload Roberto respeitam autorização operacional por franquia.

## 6) Riscos de regressão a observar

- Não publicar backend sem migrations; schema incompleto quebra governança/conta-franquia.
- Não publicar telas admin sem backend correspondente (ou vice-versa), pois os testes reais de homolog dependem do conjunto.
- Não alterar categoria/plano de usuário sem alinhar estrutura de franquia e ciclo operacional.

## 7) Referências

- Migrations (cadeia e execução): `../migrations/README`.
- Cron específico de homolog/Render: `../RENDER_CRON_HOMOLOG.md`.
- Execução local: `README_RUN.md`.
- Segurança/segredos: `../SECURITY_SECRETS.md`.
