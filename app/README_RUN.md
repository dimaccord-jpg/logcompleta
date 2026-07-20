# Execução Local

Referência: `homolog@6701a53`, 2026-07-16.

Configure `APP_ENV=dev`, `DATABASE_URL`, `SECRET_KEY`, `PUBLIC_BASE_URL`, `APP_DATA_DIR` e as chaves necessárias. Use a `.venv` local sem versioná-la.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Validação manual mínima

1. `/` anônimo: discovery, handoffs e `/login?next=`.
2. `/` logado e `/chat_julia?mode=operational`: Júlia e documentos.
3. `/fretes`: upload, BI, previsão e chat Roberto.
4. `/auditoria-frete`: chat inicialmente bloqueado, upload, revisão, fiscal, cobertura, lote e processamento.
5. Gerar os quatro gráficos e confirmar que o backend libera `/api/cleide-auditoria/audit-chat`.
6. Validar filtros, `request_id`, loading, fallback, copiar resposta e isolamento entre chats.
7. Validar limite/CTA de plano e que upload inicial, clique repetido e reprocessamento cobram conforme o contrato.
8. Conferir admin e métricas de IA/processamento.

`app/cleiton_doc_tmp/`, `tt_*.json` e `.tmp_pytest_fixture/` são temporários. Não limpe JSON fora dessas áreas sem inspeção. Detalhes: `docs/cleide_auditoria_operacional.md`.
