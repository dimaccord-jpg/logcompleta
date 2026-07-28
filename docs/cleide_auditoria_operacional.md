# Cleide Auditoria - guia operacional

Referência auditada no código em 2026-07-20. Este continua sendo o guia detalhado da Cleide Auditoria.

## Escopo e autorização

- `GET /auditoria-frete` renderiza a página sem exigir login;
- endpoints `/api/cleide-auditoria/*` exigem autenticação e passam por `avaliar_autorizacao_operacao_por_franquia`;
- o artefato ativo é isolado por sessão, usuário e franquia;
- bloqueios podem devolver `authorization` com CTA de upgrade.

## Fluxo ponta a ponta

1. Upload da tabela negociada em `POST /api/cleide-auditoria/documents/upload`.
2. Extração pós-upload via `run_cleide_audit_temp_table.py`.
3. Status em `GET /api/cleide-auditoria/documents/status`.
4. Revisão humana em `POST /api/cleide-auditoria/temp-table/save`.
5. Coverage opcional em `POST /api/cleide-auditoria/coverage/upload`.
6. Download do template oficial em `GET /api/cleide-auditoria/audit-template`.
7. Upload do lote auditado em `POST /api/cleide-auditoria/audit/upload`.
8. Processamento em `POST /api/cleide-auditoria/audit/run`.
9. Correções assistidas em `correction/preview`, `correction/apply` e `correction/undo`.
10. Desbloqueio do chat analítico em `POST /api/cleide-auditoria/audit-chat/unlock`.
11. Chat analítico pós-BI em `POST /api/cleide-auditoria/audit-chat`.

`/api/cleide-auditoria/chat` continua sendo o chat documental anterior ao BI.

## Dados, cálculo e estados

Estados confirmados da `temp_table` na documentação e nos testes:

- `processing`
- `awaiting_validation`
- `validated`
- `needs_review`
- `failed`
- `expired`
- `discarded`

A `temp_table` é temporária, revisável e sujeita a invalidação quando documentos de origem mudam.

## BI e chat analítico

- o backend só libera o chat analítico após BI válido;
- filtros de foco visual continuam restritos ao escopo do lote atual;
- o BI executivo segue com quatro gráficos, não sete;
- o chat pós-BI usa contexto analítico separado do chat documental.

## Billing e observabilidade

Eventos operacionais confirmados:

- `cleide_audit_coverage_upload`
- `cleide_audit_batch_upload`
- `cleide_audit_batch_processed`

O primeiro processamento não cobra novamente as linhas do upload inicial. IA fica em `IaConsumoEvento`; billing de linhas fica em `CleitonBillingApropriacao` e `ProcessingEvent`.

## Limites atuais

- o contrato continua dependente da qualidade dos dados enviados;
- a decisão final continua humana;
- upload, chat e processamento permanecem sujeitos a autenticação, autorização e configuração administrativa.

## Fontes e testes

- rotas: `app/cleide_audit_routes.py`
- serviço central: `app/cleide_audit_doc_service.py`
- interface: `app/templates/cleide_auditoria.html` e `app/static/js/cleide_auditoria.js`
- testes: `tests/test_cleide_audit_*`, `tests/test_cleide_auditoria_page.py`, `tests/test_cleide_phase2_ui.py`, `tests/test_cleide_isolation.py`
