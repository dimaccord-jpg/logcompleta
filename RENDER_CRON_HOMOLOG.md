# Render em Homolog — Cron e bloqueio de migration

Este guia cobre apenas o que é específico de Render + cron na homologação da Fase 2.

## 1) Estado real

- O deploy final de homolog está em preparação controlada.
- Cron pode ser validado, mas a publicação final depende de migrations.
- Documento principal de status/go-no-go: `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.

## 2) Bloqueio atual de migration (Render)

Evidência registrada no shell Render:

- `alembic` não disponível no `PATH`.
- `python -m alembic current` falhou com `No module named alembic`.
- `requirements.txt` atual não inclui Alembic.

Conclusão: validação completa de cron em homolog só é confiável após alinhar estratégia de migration/schema.

## 3) Pré-requisitos de cron em homolog

- `APP_ENV=homolog`.
- `CRON_SECRET` igual entre serviço e agendador.
- `DATABASE_URL` apontando para o banco correto de homolog.
- Persistência (`APP_DATA_DIR`/`INDICES_FILE_PATH`) configurada para índices da Home.

## 4) Rotas/jobs de cron relevantes

### 4.1 Cleiton operacional

- Endpoint: `/cron/executar-cleiton`.
- Proteção: header `X-Cron-Secret` (ou query `secret`).
- Esperado:
  - sem segredo: `403`
  - com segredo válido: `200` com JSON de status.

### 4.2 Billing snapshot

- Endpoint: `/cron/billing-snapshot`.
- Também protegido por `CRON_SECRET`.
- Atualiza snapshot de custo para painel admin quando integração BigQuery estiver configurada.

### 4.3 Índices financeiros da Home

- Execução via comando: `python -m app.finance` com `APP_ENV=homolog`.
- Resultado esperado: atualização de `indices.json` no path configurado de persistência.

## 5) Sensibilidades para regressão

- Não mudar `CRON_SECRET` somente em um lado (serviço ou agendador), senão rotas ficam 403 permanentemente.
- Não validar cron ignorando migration pendente: endpoints podem responder, mas comportamento de dados pode ficar inconsistente com schema.
- Não separar teste de cron da validação de dashboard/admin, pois os resultados operacionais são consumidos nesses painéis.

## 6) Checklist objetivo

1. Confirmar variáveis (`APP_ENV`, `CRON_SECRET`, `DATABASE_URL`, paths de persistência).
2. Confirmar resolução do bloqueio Alembic e execução de migrations.
3. Validar `/cron/executar-cleiton` (403 sem segredo / 200 com segredo).
4. Validar `/cron/billing-snapshot`.
5. Validar `python -m app.finance` e reflexo da atualização na Home/admin.
