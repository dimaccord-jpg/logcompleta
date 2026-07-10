# Deploy e Promocao

Data de consolidacao: `2026-07-08`
Commit homologado: `efd54b5`
Commit promovido em producao: `3d5332b`

## Ambientes

- `dev`: desenvolvimento local
- `homolog`: homologacao oficial
- `producao`: branch funcional de producao

## Estado promovido

- `homolog` validada em `efd54b5`
- `producao` atualizada em `3d5332b`
- a promocao ocorreu por cherry-pick seletivo, nao por merge cego
- producao foi validada apos deploy
- a entrega nao adicionou migration, schema, tabela, campo ou banco

## Checklist antes de promover

1. confirmar branch correta e working tree limpo
2. validar schema do ambiente antes de commit/push/deploy
3. aplicar a cadeia de migrations preexistente do ambiente quando necessario
4. validar `/health`
5. validar Home publica e Home logada
6. validar Julia documental governada
7. validar Roberto em `/fretes`
8. validar `/cleide-bi-frete`
9. validar `/auditoria-frete` com:
- upload documental
- `temp_table`
- revisao humana
- coverage quando aplicavel
- upload do lote auditado
- `audit/run`
- preview/apply/undo de correcao quando houver diagnostico
10. validar autorizacao por franquia e bloqueios esperados
11. confirmar que nenhum temporario entrou em commit

## Suite alvo recentemente validada

```powershell
pytest tests/test_cleide_audit_correction_service.py tests/test_cleide_admin_routes.py tests/test_cleide_audit_chat_routes.py tests/test_cleide_audit_config_service.py tests/test_cleide_audit_doc_context.py tests/test_cleide_audit_doc_routes.py tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py -q
```

Resultado conhecido:

- `717 passed, 2 warnings`
- warnings conhecidos de `flask_session.filesystem.FileSystemSessionInterface`
- warnings conhecidos de `google.genai.types._UnionGenericAlias`

## Regras operacionais

- homologacao ocorre em `homolog`
- producao ocorre em `producao`
- usar cherry-pick seletivo quando `producao` nao puder receber merge direto de `homolog`
- nao publicar `.db`, caches, `__pycache__`, `.pytest_cache` ou temporarios locais
- limpar e manter fora de commit os temporarios de `app/cleiton_doc_tmp/`

## Ponto de atencao Render

- a operacao aprovada usa `producao` como branch de producao
- se houver divergencia com configuracao versionada ou painel, o painel do Render continua sendo a referencia operacional a conferir antes da promocao
