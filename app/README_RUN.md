# Execução Local

Referência auditada em 2026-07-20.

## Pré-requisitos

- estar no diretório raiz do projeto;
- usar `.venv` local sem versioná-la;
- definir `APP_ENV`, `DATABASE_URL`, `SECRET_KEY`, `APP_DATA_DIR`, `INDICES_FILE_PATH` e `PUBLIC_BASE_URL`.

## Comandos úteis

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

```powershell
.\.venv\Scripts\python.exe -m flask --app app.web db upgrade
.\.venv\Scripts\python.exe -m flask --app app.web run
```

## Validação manual mínima

1. `/` anônimo: discovery, limites de onboarding e handoff com `next` seguro.
2. `/` logado e `/chat_julia?mode=operational`: Júlia operacional.
3. `/api/julia/documents/*`: upload, listagem, remoção e limpeza com isolamento por sessão.
4. `/fretes`: upload, BI, previsão e chat Roberto.
5. `/auditoria-frete`: upload, `temp_table`, revisão, coverage, lote, BI e `audit-chat/unlock`.
6. `/agente-compara`: upload, `temp_table`, revisão, coverage, lote, BI e `audit-chat/unlock`.
7. `/admin/...`: métricas de IA/processamento, billing e configurações.
8. `/cron/*`: somente em contexto controlado e com `X-Cron-Secret` válido.

## Temporários e artefatos

- `app/cleiton_doc_tmp/` e `tt_*.json` são temporários técnicos;
- `.db` locais não fazem parte do fluxo oficial;
- templates `.xlsx` oficiais versionados são exceções explícitas do `.gitignore`.

## Observações importantes

- o boot versionado do Render aplica migrations antes do servidor;
- `APP_ENV` é obrigatório e não tem fallback implícito;
- o banco oficial é PostgreSQL em todos os ambientes.
