# Render em Homolog — Cron e bloqueio de migration

Este guia registra o necessário para execução de cron em homolog e o bloqueio atual de migrations.

## 1) Estado real da publicação

- O deploy final de homolog está em preparação controlada.
- A pendência crítica é migration no ambiente Render.
- Não considerar homolog como publicada enquanto migrations não forem validadas.

Referência central de status: `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.

## 2) Bloqueio de migration no Render

Constatado no shell do Render:

- `alembic` ausente no `PATH`.
- `python -m alembic current` falhou com `No module named alembic`.
- `requirements.txt` não expõe Alembic no ambiente atual.

Implicação: qualquer cron/validação que dependa de schema novo fica condicionado à estratégia de migration.

## 3) Pré-requisitos de runtime para cron

No Web Service e no Cron Job:

- `APP_ENV=homolog`.
- `CRON_SECRET` (mesmo valor no backend e nos jobs HTTP protegidos).
- Variáveis de domínio já configuradas no serviço.

Para detalhes de deploy e variáveis de ambiente, usar `app/README_DEPLOY.md`.

## 4) Jobs previstos em homolog

### 4.1 Ciclo Cleiton (endpoint protegido)

- Endpoint: `/cron/executar-cleiton`.
- Exemplo de comando:

```bash
curl -s -X POST -H "X-Cron-Secret: $CRON_SECRET" "https://<dominio-homolog>/cron/executar-cleiton?ts=$(date +%s)"
```

Validação mínima:

- Sem segredo: resposta `403` (rota publicada e protegida).
- Com segredo: resposta `200` com JSON de status.

### 4.2 Coleta de índices da Home

- Comando: `APP_ENV=homolog python -m app.finance`.
- Frequência sugerida: 2x por dia útil.
- Validar atualização de `INDICES_FILE_PATH` e ticker da Home.

### 4.3 Snapshot de billing (Observabilidade Fase 1)

- Endpoint: `/cron/billing-snapshot`.
- Usa o mesmo `CRON_SECRET`.
- Validar atualização de custo no dashboard admin.

## 5) Checklist objetivo

1. Confirmar variáveis de ambiente no serviço e no cron.
2. Confirmar estratégia e execução de migrations (bloqueio atual).
3. Validar cron Cleiton com `403` sem segredo e `200` com segredo.
4. Validar job de índices (`python -m app.finance`).
5. Validar snapshot de billing.
