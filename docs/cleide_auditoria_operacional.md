# Cleide Auditoria — guia operacional

Referência: `homolog@6701a53`, 2026-07-16. Este é o guia detalhado da auditoria; os demais documentos devem apontar para ele em vez de duplicar o contrato.

## Escopo e autorização

`GET /auditoria-frete` renderiza a página sem exigir login. Os endpoints `/api/cleide-auditoria/...` exigem usuário autenticado e passam por `avaliar_autorizacao_operacao_por_franquia`. O artefato ativo é isolado por sessão, usuário e franquia. Respostas de bloqueio preservam `authorization.upgrade_cta` quando o plano oferece upgrade.

## Fluxo ponta a ponta

1. Upload da tabela negociada em `POST /api/cleide-auditoria/documents/upload`.
2. `app/run_cleide_audit_temp_table.py` extrai dados e `app/cleide_audit_doc_service.py` grava um `tt_*.json` temporário.
3. O frontend consulta `GET /api/cleide-auditoria/documents/status` e revisa a `temp_table`.
4. `POST /api/cleide-auditoria/temp-table/save` salva tabela, rotas, taxas, cobertura e configuração fiscal revisadas.
5. CSV/XLSX de cobertura pode ser enviado em `POST /api/cleide-auditoria/coverage/upload`.
6. O template vem de `GET /api/cleide-auditoria/audit-template` e `app/protected_files/templates/template_cleide_auditoria_frete.xlsx`.
7. `POST /api/cleide-auditoria/audit/upload` aceita CSV/XLSX, normaliza o lote, aplica limites e cobra suas linhas.
8. `POST /api/cleide-auditoria/audit/run` calcula o esperado e persiste resultados e diagnósticos.
9. Diagnósticos suportados usam `correction/preview`, `correction/apply` e `correction/undo`.
10. **Gerar Gráficos** renderiza o BI e chama `POST /api/cleide-auditoria/audit-chat/unlock`.
11. Após validação backend, o frontend habilita `POST /api/cleide-auditoria/audit-chat`.

`/api/cleide-auditoria/chat` é o chat documental anterior ao BI. O chat analítico pós-auditoria usa `/audit-chat`; caches, `flow_type` e contexto são isolados.

## Dados, cálculo e estados

A `temp_table` pode estar em `processing`, `awaiting_validation`, `validated`, `needs_review`, `failed`, `expired` ou `discarded`. Remover, limpar ou trocar fontes pode invalidá-la. Revisão salva pode impedir sobrescrita automática pelo mesmo conjunto de fontes.

O motor cobre faixas de peso, excesso por kg, frete valor sobre NF, pedágio por fração, taxas fixas/percentuais, UF/cidade/região, fallback de cidade e ICMS/ISS quando ativados. Diagnósticos incluem cobertura ausente/ambígua, regra ausente, modelo não suportado, peso/valor/NF inválidos e incompatibilidade de dimensão de preço.

Resultados mantêm linha normalizada, cobrado, esperado, divergência, status, motivo, componentes e diagnóstico. Alteração fiscal ou tarifária após processamento marca `needs_reprocess`; o resultado anterior fica obsoleto.

## BI e chat analítico

`audit_bi.ready` exige lote processado e linha válida. Sem lote, conclusão ou resultado identificável, BI/chat permanece indisponível. Gráficos: impacto por transportadora, impacto por UF destino, evolução no período e Pareto do cobrado a mais.

- `app/cleide_audit_insights_bi.py`: filtros, métricas e agregações determinísticas;
- `app/cleide_audit_insights_context.py`: artefato, lote, escopo de desbloqueio e foco;
- `app/cleide_audit_insights_query.py`: intenções e respostas determinísticas;
- `app/cleide_audit_insights_prompt.py`: contrato e contexto limitado para IA;
- `app/run_cleide_audit_insights_chat.py`: cache idempotente, memória, consultas e complemento Gemini.

Filtros: `carrier`, `origin_uf`, `destination_uf` e `issue_date`. Foco e desbloqueio valem apenas para o `batch_scope` atual.

O chat começa com “Faça o upload da tabela de frete.” e placeholder “Faça o upload da tabela de frete para liberar o chat.” O backend só desbloqueia BI válido naquela sessão. A requisição leva `message`, histórico sanitizado, `request_id` e `visual_focus`. O frontend mostra “Cleide está analisando...”, bloqueia o composer, trata autenticação, plano e serviço, usa fallback útil, efeito de digitação na orientação e botão **Copiar** nas respostas.

## Billing e observabilidade

Eventos operacionais usam `agent=cleide`, `processing_type=non_llm` e chave idempotente:

- `cleide_audit_coverage_upload`: linhas de cobertura;
- `cleide_audit_batch_upload`: linhas do lote no upload inicial;
- `cleide_audit_batch_processed`: apenas reprocessamento explícito de lote já processado e obsoleto.

O primeiro processamento e clique repetido sem `needs_reprocess` não debitam novamente. Falha antes da persistência não debita. `request_id`, `execution_id`, IDs e chaves únicas dão rastreabilidade. Gemini fica em `IaConsumoEvento`; chat analítico não apropria linhas.

## Fontes e testes

Rotas: `app/cleide_audit_routes.py`; ciclo e cálculo: `app/cleide_audit_doc_service.py`; interface: `app/templates/cleide_auditoria.html` e `app/static/js/cleide_auditoria.js`; testes: `tests/test_cleide_audit_*`, `tests/test_cleide_auditoria_page.py`, `tests/test_cleide_phase2_ui.py` e `tests/test_cleide_isolation.py`.
