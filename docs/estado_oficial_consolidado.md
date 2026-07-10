# Estado Oficial Consolidado

Data de consolidacao: `2026-07-08`
Commit de referencia: `homolog@efd54b5` e `producao@3d5332b`

## Escopo confirmado

- Copilot de discovery na Home publica
- Home logada consolidada como superficie operacional da Julia
- Julia documental governada pelo Cleiton
- Roberto com upload, BI e chat privados em `/fretes`
- Cleide BI estrutural em `/cleide-bi-frete`
- Cleide Auditoria em `/auditoria-frete`
- observabilidade de IA e processamento mantida

## Estado de ambiente

- `homolog` contem `efd54b5`
- `origin/homolog` esta sincronizada com `homolog`
- a promocao recente de producao ocorreu por cherry-pick seletivo
- `producao` contem `3d5332b`
- nao houve migration nova
- nao houve schema novo
- nao houve tabela nova
- nao houve campo novo

## Cleide Auditoria

- upload documental, `temp_table`, coverage, lote auditado e chat estao ativos
- a `temp_table` continua artefato temporario governado pelo dominio Cleiton
- a extração tecnica permanece separada do chat
- a revisao humana pode editar e salvar a tabela antes de avancar
- ha correcoes assistidas com preview, apply e undo
- o BI executivo da auditoria usa 4 graficos:
- Impacto Financeiro por Transportadora
- Divergencia Financeira por UF Destino
- Evolucao da Divergencia no Periodo
- Pareto do Valor Cobrado a Mais

## Admin

- `/admin/agentes/cleide` separa configuracao do BI estrutural e da auditoria documental
- `/admin/agentes/cleiton` continua dono dos limites globais documentais, TTL, cleanup e limites por tipo
- persistencia continua em `ConfigRegras`

## Git e temporarios

- `app/cleiton_doc_tmp/` permanece temporaria e ignorada
- `tt_*.json`, caches e residuos tecnicos nao entram em commit
- `.db`, `__pycache__` e `.pytest_cache` nao fazem parte do estado oficial

## Testes validados

```powershell
pytest tests/test_cleide_audit_correction_service.py tests/test_cleide_admin_routes.py tests/test_cleide_audit_chat_routes.py tests/test_cleide_audit_config_service.py tests/test_cleide_audit_doc_context.py tests/test_cleide_audit_doc_routes.py tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py -q
```

- resultado conhecido: `717 passed, 2 warnings`
