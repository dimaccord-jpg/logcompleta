# Cleide Auditoria

Referência auditada em 2026-08-15. Este guia descreve o fluxo operacional real da Cleide Auditoria no código atual.

## Escopo e autorização

- `GET /auditoria-frete` renderiza a página;
- os endpoints `/api/cleide-auditoria/*` exigem autenticação;
- a autorização operacional passa por `avaliar_autorizacao_operacao_por_franquia`;
- bloqueios podem devolver contexto de autorização com CTA de upgrade;
- o fluxo mantém isolamento por usuário, franquia, sessão e artefatos próprios.

## Jornada ponta a ponta

1. Upload documental em `POST /api/cleide-auditoria/documents/upload`.
2. Extração e preparação de `temp_table`.
3. Consulta de status em `GET /api/cleide-auditoria/documents/status`.
4. Revisão humana em `POST /api/cleide-auditoria/temp-table/save`.
5. Coverage opcional em `POST /api/cleide-auditoria/coverage/upload`.
6. Download do template em `GET /api/cleide-auditoria/audit-template`.
7. Upload do lote auditado em `POST /api/cleide-auditoria/audit/upload`.
8. Processamento em `POST /api/cleide-auditoria/audit/run`.
9. Correções assistidas em `audit/correction/preview`, `apply` e `undo`.
10. Desbloqueio do chat analítico em `POST /api/cleide-auditoria/audit-chat/unlock`.
11. Chat analítico em `POST /api/cleide-auditoria/audit-chat`.

`/api/cleide-auditoria/chat` continua sendo o chat documental separado do chat pós-BI.

## Estados e revisão

Estados documentais confirmados:

- `processing`
- `awaiting_validation`
- `validated`
- `needs_review`
- `failed`
- `expired`
- `discarded`

Leitura correta:

- a extração pode falhar sem impedir o fluxo de revisão manual;
- a `temp_table` é temporária e pode ser invalidada quando a origem muda;
- a confirmação depende da revisão humana quando a leitura automática não entrega segurança suficiente.

## Coverage, lote e BI

- coverage é opcional;
- o lote auditado usa template oficial próprio;
- o BI executivo e o chat analítico só ficam disponíveis após processamento válido do lote;
- filtros e contexto do BI permanecem restritos ao escopo do lote atual.
- o BI executivo expõe quatro gráficos canônicos: `transportadora`, `uf_destino`, `temporal` e `pareto_transportadora`.

## Billing, funil e primeira auditoria

Eventos operacionais relevantes:

- `cleide_audit_coverage_upload`
- `cleide_audit_batch_upload`
- `cleide_audit_batch_processed`

Comportamento atual:

- o primeiro `audit/run` não cobra novamente o lote já apropriado no upload;
- o fluxo pode registrar eventos de funil de upload e de conclusão;
- a conclusão também pode marcar `first_audit_completed` de forma idempotente;
- o backend informa `allow_meta_pixel` quando o front pode refletir o evento no Meta Pixel;
- falha de pixel não bloqueia o fluxo de negócio.

## IA, configuração e isolamento

- a Cleide Auditoria usa configuração persistida própria em `app/services/cleide_audit_config_service.py`;
- os limites documentais respeitam tetos globais compartilhados do ecossistema Cleiton;
- chat documental e chat analítico usam contextos distintos;
- a leitura analítica depende da qualidade e da confiança dos dados do lote; quando a confiança for média ou baixa, a limitação deve ser destacada antes das recomendações;
- a Cleide apresenta fatos, leitura gerencial, hipóteses e próximos passos, mas não toma a decisão final sobre cobranças, responsabilidades ou providências;
- o domínio da Cleide permanece isolado do AgenteCompara em sessão, billing, eventos e artefatos.

## Fontes de código

- rotas: `app/cleide_audit_routes.py`
- serviços: `app/cleide_audit_doc_service.py`, `app/cleide_audit_correction_service.py`
- contexto e BI: `app/cleide_audit_doc_context.py`, `app/cleide_audit_insights_*`
- interface: `app/templates/cleide_auditoria.html`, `app/static/js/cleide_auditoria.js`

## Testes úteis

- `tests/test_cleide_audit_doc_routes.py`
- `tests/test_cleide_audit_doc_service.py`
- `tests/test_cleide_audit_operational_billing.py`
- `tests/test_cleide_audit_insights_chat.py`
- `tests/test_cleide_isolation.py`
- `tests/test_cleide_analytics.py`
