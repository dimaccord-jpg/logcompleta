# Cleide - Homologacao Operacional Controlada

Este documento continua sendo um runbook historico/de validacao controlada da entrega aprovada. Nao e a fonte primaria da arquitetura ativa; para estado vigente, consultar `README.md`.

Estado desta entrega:

- homolog aprovada no commit `d02ce15`
- producao aprovada no commit `6efa2e2`
- `origin/homolog` aponta para `d02ce15`
- `origin/producao` aponta para `6efa2e2`
- sem migration nova
- sem nova tabela de banco
- sem novo campo
- sem alteracao manual de banco

## Como usar

1. Executar a bateria automatizada da Cleide.
2. Executar o roteiro manual abaixo em ambiente de homolog.
3. Registrar `pass` ou `fail` por cenario.
4. Encerrar com classificacao final:
- `A) homolog`
- `B) producao`
- `C) precisa ajuste`

## Evidencias automatizadas atuais

Coleta validada nesta auditoria documental:

- `.\.venv\Scripts\python.exe -m pytest --collect-only -q tests/test_cleide_audit_correction_service.py tests/test_cleide_admin_routes.py tests/test_cleide_audit_chat_routes.py tests/test_cleide_audit_config_service.py tests/test_cleide_audit_doc_context.py tests/test_cleide_audit_doc_routes.py tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py`
- resultado atual de coleta: `804 tests collected`

Cobertura automatizada usada como referencia:

- contexto documental da auditoria: `tests/test_cleide_audit_doc_context.py`
- chat da auditoria: `tests/test_cleide_audit_chat_routes.py`
- temp table, coverage, fiscal, lote auditado e BI: `tests/test_cleide_audit_temp_table.py`
- rotas documentais: `tests/test_cleide_audit_doc_routes.py`
- pagina `/auditoria-frete`: `tests/test_cleide_auditoria_page.py`
- admin da auditoria: `tests/test_cleide_admin_routes.py`
- configuracao da auditoria: `tests/test_cleide_audit_config_service.py`
- correcoes assistidas: `tests/test_cleide_audit_correction_service.py`

## Checklist operacional

| ID | Cenario | Evidencia esperada | Tipo | Status |
|---|---|---|---|---|
| 1 | Upload documental da Cleide Auditoria | Documento registrado e retorno de `temp_table` quando disponivel | Auto + manual | [ ] |
| 2 | Status documental da Cleide Auditoria | Endpoint devolve `documents`, `temp_table` e flags de upload coerentes | Auto + manual | [ ] |
| 3 | Mudanca ou remocao de documento fonte | `temp_table` anterior invalidada corretamente | Auto + manual | [ ] |
| 4 | Extracao parcial | Estado `needs_review`, sempre com validacao humana obrigatoria | Auto + manual | [ ] |
| 5 | Conversa apos extracao | Chat usa contexto, mas nao recria nem sobrescreve a `temp_table` | Auto + manual | [ ] |
| 6 | Revisao humana da `temp_table` | `POST /api/cleide-auditoria/temp-table/save` salva revisao governada sem quebrar escopo da sessao | Auto + manual | [ ] |
| 7 | Etapa fiscal | Dados fiscais podem ser salvos sem perder integridade do artefato | Auto + manual | [ ] |
| 8 | Coverage opcional | Upload CSV/XLSX popula UF, cidade e regiao quando aplicavel | Auto + manual | [ ] |
| 9 | Upload do lote auditado | Template oficial aceito e lote salvo no artefato atual | Auto + manual | [ ] |
| 10 | `audit/run` | Resultado calculado sem alterar documentos de origem | Auto + manual | [ ] |
| 11 | Correcao assistida | preview/apply/undo funcionam apenas sobre diagnosticos suportados | Auto + manual | [ ] |
| 12 | BI executivo | Quatro graficos atuais de impacto financeiro renderizam com filtros locais | Auto + manual | [ ] |
| 13 | Arquivos temporarios | Nenhum `tt_*.json`, `.cleanup_meta.json`, `.db` local ou residuo tecnico entrou em versionamento | Manual | [ ] |

## Observabilidade a validar

Conferir em payloads e comportamento:

- `flow_type`
- `documents_used` quando habilitado
- estados da `temp_table`
- dataset sanitizado de `audit_bi`
- bloqueios de autorizacao por franquia

## Roteiro manual minimo

1. Abrir `/auditoria-frete`.
2. Fazer upload documental valido.
3. Confirmar surgimento do card da `temp_table`.
4. Abrir modal, revisar frete, fiscal e coverage quando aplicavel.
5. Salvar revisao humana.
6. Baixar template oficial do lote auditado.
7. Enviar lote auditado e executar `audit/run`.
8. Validar BI executivo com 4 graficos.
9. Testar preview/apply/undo quando houver diagnostico suportado.
10. Remover ou trocar documento anexado e confirmar invalidacao do artefato anterior.
11. Conferir que nenhum temporario local entrou em versionamento.

## Registro final

- Checklist executado: `sim` ou `nao`
- Pass/fail por cenario: preencher tabela acima
- Riscos restantes: descrever
- Apta para:
- `A) homolog`
- `B) producao`
- `C) precisa ajuste`
