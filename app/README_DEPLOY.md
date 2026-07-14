# Deploy e Promocao

Data de consolidacao: `2026-07-10`
Commit homologado no workspace: `d02ce15`
Commit promovido aprovado no repositório: `6efa2e2`

## Ambientes

- `dev`: desenvolvimento local
- `homolog`: homologacao oficial
- `main`: branch configurada no `render.yaml` versionado para o servico de producao
- `producao`: branch ainda existente no repositório com a promocao aprovada `6efa2e2`

## Estado promovido

- `homolog` validada em `d02ce15`
- `producao` aponta para `6efa2e2`
- `render.yaml` usa `main` para o servico `logcompleta-web-prod`
- `logcompleta-web-prod` esta com `autoDeploy: false` no arquivo versionado
- a entrega nao adicionou migration, schema, tabela, campo ou banco

## Checklist antes de promover

1. confirmar branch correta e working tree limpo
2. validar a branch efetivamente conectada no painel do Render
3. validar schema do ambiente antes de deploy
4. aplicar a cadeia de migrations preexistente do ambiente quando necessario
5. validar `/health`
6. validar Home publica e Home logada
7. validar Julia documental governada
8. validar Roberto em `/fretes`
9. validar `/cleide-bi-frete`
10. validar `/auditoria-frete` com:
- upload documental
- `temp_table`
- revisao humana
- coverage quando aplicavel
- upload do lote auditado
- `audit/run`
- preview/apply/undo de correcao quando houver diagnostico
11. validar autorizacao por franquia e bloqueios esperados
12. confirmar que nenhum temporario entrou em commit

## Testes

Comandos reais verificados nesta auditoria documental:

```powershell
.\.venv\Scripts\python.exe -m pytest --collect-only -q tests/test_cleide_audit_correction_service.py tests/test_cleide_admin_routes.py tests/test_cleide_audit_chat_routes.py tests/test_cleide_audit_config_service.py tests/test_cleide_audit_doc_context.py tests/test_cleide_audit_doc_routes.py tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

Coleta atual:

- suite especifica da auditoria Cleide: `804 tests collected`
- suite completa: `1660 tests collected`

## Regras operacionais

- homolog ocorre em `homolog`
- na configuracao versionada atual, producao ocorre por deploy manual do servico configurado para `main`
- como o repositório ainda tem `producao@6efa2e2`, divergencia entre branch promovida e branch configurada no Render deve ser tratada explicitamente antes de publicar
- nao publicar `.db`, caches, `__pycache__`, `.pytest_cache` ou temporarios locais
- limpar e manter fora de commit os temporarios de `app/cleiton_doc_tmp/`

## Ponto de atencao Render

- `logcompleta-web-homolog`: `branch: homolog`, `autoDeploy: true`
- `logcompleta-web-prod`: `branch: main`, `autoDeploy: false`
- a confirmacao do painel do Render continua sendo a referencia operacional final antes da promocao
