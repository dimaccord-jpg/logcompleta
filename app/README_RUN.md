# Execução Local

Referência auditada em 2026-07-28. A visão funcional oficial está em `docs/estado_oficial_consolidado.md`.

## Pré-requisitos

- usar a raiz do projeto;
- ativar `.venv` local quando aplicável;
- definir `APP_ENV`, `DATABASE_URL`, `SECRET_KEY`, `APP_DATA_DIR`, `INDICES_FILE_PATH` e `PUBLIC_BASE_URL`.

## Comandos principais

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

```powershell
.\.venv\Scripts\python.exe -m flask --app app.web db upgrade
.\.venv\Scripts\python.exe -m flask --app app.web run
```

## Pontos de validação manual

1. autenticação e redirecionamento seguro;
2. `/chat_julia?mode=operational`;
3. `/fretes`;
4. `/auditoria-frete`;
5. `/agente-compara` com jornada completa: start, upload de duas tabelas obrigatórias, terceira opcional, revisão, impostos, coverage, arquivo operacional, cálculo e leitura do resultado;
6. `/health/liveness` e `/health/readiness`;
7. `/admin/...` para métricas, billing e observabilidade;
8. `/cron/*` apenas com segredo válido e em contexto controlado.

## Observações importantes

- o banco oficial continua sendo PostgreSQL;
- o sistema depende das migrations Alembic existentes;
- `.db` locais, `cache_*.json`, `app/indices.json` e `app/cleiton_doc_tmp/` não são dependências oficiais do deploy;
- templates oficiais `.xlsx` permanecem versionados por exceção do `.gitignore`.
