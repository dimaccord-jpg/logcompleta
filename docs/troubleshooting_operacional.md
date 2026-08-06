# Troubleshooting Operacional

Data de consolidação: `2026-08-05`
Fotografia principal auditada no repositório local: `939b73e`

## 1. Home pública sem responder

Validar:

1. `POST /api/onboarding_discovery`
2. chaves Gemini de discovery
3. fallback local do discovery

## 2. Home logada não mostra a Júlia corretamente

Comportamento esperado:

- usuário anônimo: Copilot de discovery
- usuário logado: Júlia operacional na Home
- `/chat_julia?mode=operational`: rota dedicada

## 3. Handoff e login com `next` falham

Conferir:

1. `app/copilot_capabilities.md`
2. `app/capability_taxonomy.py`
3. `_safe_next_redirect`
4. `tests/test_auth_next_redirect.py`

## 4. Upload documental da Júlia falha

Conferir:

1. login do usuário
2. `avaliar_autorizacao_operacao_por_franquia`
3. `upload_enabled`
4. limite de sessão e de arquivo
5. tipos suportados
6. TTL e cleanup

## 5. PDF parece lido quando não deveria

Conferir:

1. `app/cleiton_doc_gemini_files.py`
2. `app/julia_doc_context.py`
3. testes da Júlia e PDF

## 6. Arquivos temporários apareceram no Git

Conferir:

1. `.gitignore`
2. `app/cleiton_doc_tmp/`
3. `tt_*.json`
4. `.cleanup_meta.json`
5. `.db` locais

## 7. Health check do Render falha

Conferir:

1. `render.yaml`
2. `app/web.py`
3. rotas reais `/health/liveness` e `/health/readiness`

Há divergência conhecida porque o YAML versionado ainda aponta `healthCheckPath: /health`.

## 8. Branch errada no Render

Conferir:

1. `render.yaml`
2. painel do Render
3. branch operacional realmente conectada ao serviço

Há divergência conhecida porque o YAML versionado ainda aponta produção para `main`, enquanto o processo operacional informado usa `producao`.

## 9. Onboarding abatendo franquia

Isso continua incorreto.

Conferir:

1. `flow_type = onboarding_discovery`
2. dashboard admin
3. `tests/test_ia_metrics_service.py`

## 10. Tabela temporária da Cleide não aparece após upload

Conferir:

1. `POST /api/cleide-auditoria/documents/upload`
2. `GET /api/cleide-auditoria/documents/status`
3. `run_cleide_audit_temp_table.py`
4. `cleide_audit_doc_service.py`

## 11. Chat analítico da Cleide continua bloqueado

Conferir, nesta ordem:

1. lote processado
2. `audit_bi.ready`
3. clique em gerar gráficos
4. `POST /api/cleide-auditoria/audit-chat/unlock`
5. `batch_scope`
6. autenticação e autorização

## 12. Consumo de linhas da Cleide parece duplicado

Correlacione `CleitonBillingApropriacao`, `ProcessingEvent`, `execution_id` e chave idempotente.

## 13. Tabela temporária do Agente Compara não aparece após upload

Conferir:

1. `POST /api/agente-compara/documents/upload`
2. `GET /api/agente-compara/documents/status`
3. `run_agente_compara_temp_table.py`
4. `agente_compara_doc_service.py`

## 14. Chat analítico do Agente Compara continua bloqueado

Conferir:

1. lote processado
2. BI válido
3. `POST /api/agente-compara/audit-chat/unlock`
4. `batch_scope`
5. autenticação e autorização

## 15. Chat contextual da comparação continua bloqueado

Conferir:

1. existência de comparação ativa na sessão correta
2. `GET /api/agente-compara/comparison/calculation`
3. status `READY` sem `stale`
4. `billing_status = applied`
5. presença simultânea de `result` e `analytics`
6. `POST /api/agente-compara/comparison-chat`
7. autenticação e autorização por franquia

## 16. Consumo de linhas do Agente Compara parece duplicado

Correlacione `CleitonBillingApropriacao`, `ProcessingEvent`, `execution_id` e chave idempotente próprias do namespace `agente-compara-`.

## 17. Documento removido incorretamente entre agentes

Conferir:

1. `source_agent`
2. `session_key`
3. ownership antes da remoção física
4. testes de isolamento de Júlia, Cleide e Agente Compara

## 18. Cron falha por autenticação

Conferir:

1. header `X-Cron-Secret`
2. valor de `CRON_SECRET`
3. `tests/test_cron_auth.py`

`?secret=` ainda existe apenas por compatibilidade temporária.
