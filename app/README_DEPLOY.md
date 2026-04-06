# Deploy em Homolog/Produção — Guia Operacional

Este documento concentra o procedimento de deploy.  
Status da publicação atual da Fase 2: `../DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.

## 1) Premissas de ambiente

- Um PostgreSQL por ambiente (`DATABASE_URL`).
- `APP_ENV` obrigatório no processo (`homolog` ou `prod`) antes do boot.
- Arquivos `.env` carregados conforme `APP_ENV` (`app/.env.homolog` ou `app/.env.prod`).
- Persistência obrigatória para dados de runtime (`APP_DATA_DIR` / `INDICES_FILE_PATH`) em homolog/prod.

## 2) O que esta entrega já inclui (Fase 2)

- `execution_id` no upload do Roberto.
- Idempotência de billing por `execution_id`.
- Motor de governança/franquia/billing.
- Migrations e testes associados no repositório.

## 3) Bloqueio atual antes do publish final de homolog

No Render:

- `python -m alembic current` falhou (`No module named alembic`).
- `alembic` não está disponível no ambiente.
- `requirements.txt` atual não expõe Alembic.

Implicação: publicação final depende de resolver e executar estratégia de migration.  
Procedimento de migration: `../migrations/README`.

## 4) Sequência segura de deploy (homolog)

1. Publicar código da branch homolog no serviço.
2. Confirmar variáveis obrigatórias (`APP_ENV`, `DATABASE_URL`, `CRON_SECRET`, demais segredos).
3. Resolver execução de migrations no ambiente alvo.
4. Executar migrations no banco de homolog.
5. Rodar smoke tests funcionais e cron essencial.

Sem o passo 3/4 validado, não fechar homolog como concluída.

## 5) Checklist pós-deploy

- `GET /health/liveness` responde `200`.
- `GET /health/readiness` responde `200`.
- `/cron/executar-cleiton`: `403` sem segredo e `200` com segredo.
- `APP_ENV=homolog python -m app.finance` executa e atualiza índices.
- Dashboard/admin sem regressão operacional crítica.

## 6) Segurança e segredos

- Não versionar segredos reais.
- Configurar segredos no provedor.
- Política completa: `../SECURITY_SECRETS.md`.

## 7) Referências

- Execução local e troubleshooting: `README_RUN.md`.
- Cron em homolog: `../RENDER_CRON_HOMOLOG.md`.
- Status real de publicação: `../DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.
