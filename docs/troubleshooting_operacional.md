# Troubleshooting Operacional

Data de consolidação: `2026-08-19`

## 1. Home pública sem responder

Conferir:

1. `POST /api/onboarding_discovery`
2. chaves/configuração do discovery
3. fallback local previsto para discovery

## 2. Home logada não mostra a experiência correta

Comportamento esperado:

- usuário anônimo: Copiloto público
- usuário logado: experiência principal com Júlia
- `/chat_julia?mode=operational`: rota dedicada para o modo operacional

## 3. Handoff e login com `next` falham

Conferir:

1. `app/copilot_capabilities.md`
2. `app/capability_taxonomy.py`
3. `_safe_next_redirect`
4. `tests/test_auth_next_redirect.py`

## 4. Upload documental da Júlia falha

Conferir:

1. autenticação do usuário
2. autorização por franquia
3. `upload_enabled`
4. limites de sessão e arquivo
5. tipos suportados
6. TTL e cleanup

## 5. PDF parece lido quando não deveria

Conferir:

1. `app/cleiton_doc_gemini_files.py`
2. `app/julia_doc_context.py`
3. cobertura de testes documentais/PDF

## 6. Arquivos temporários apareceram no Git

Conferir:

1. `.gitignore`
2. diretórios temporários de runtime
3. arquivos `tt_*.json`
4. metadados de cleanup
5. bancos locais temporários

## 7. Health check do Render falha

Conferir:

1. `render.yaml`
2. `app/web.py`
3. rotas reais `/health`, `/health/liveness` e `/health/readiness`

`healthCheckPath` continua em `/health`, enquanto o código também expõe checks mais específicos.

## 8. Branch errada no Render

Conferir:

1. `render.yaml`
2. painel do Render
3. branch efetivamente conectada ao serviço

No arquivo versionado atual:

- homolog: `homolog`
- produção: `producao`

## 9. Onboarding abatendo franquia

Isso continua incorreto.

Conferir:

1. `flow_type = onboarding_discovery`
2. dashboard admin
3. cobertura de métricas/IA

## 10. Tabela temporária da Cleide não aparece após upload

Conferir:

1. `POST /api/cleide-auditoria/documents/upload`
2. `GET /api/cleide-auditoria/documents/status`
3. runner da `temp_table`
4. service documental da auditoria

## 11. Chat analítico da Cleide continua bloqueado

Conferir, nesta ordem:

1. lote processado
2. BI pronto
3. unlock do chat analítico
4. `batch_scope`
5. autenticação e autorização

## 12. Consumo de linhas da Cleide parece duplicado

Correlacionar `CleitonBillingApropriacao`, `ProcessingEvent`, `execution_id` e a chave idempotente do fluxo.

## 13. Tabela temporária do AgenteCompara não aparece após upload

Conferir:

1. `POST /api/agente-compara/documents/upload`
2. `GET /api/agente-compara/documents/status`
3. runner da `temp_table`
4. service documental do AgenteCompara

## 14. Chat analítico do AgenteCompara continua bloqueado

Conferir:

1. lote processado
2. BI válido
3. `POST /api/agente-compara/audit-chat/unlock`
4. `batch_scope`
5. autenticação e autorização

## 15. Chat contextual da comparação continua bloqueado

Conferir:

1. comparação ativa na sessão correta
2. `GET /api/agente-compara/comparison/calculation`
3. `status = CALCULATION_READY`
4. `stale = false`
5. `billing_status = applied`
6. presença simultânea de `result` e `analytics`
7. `POST /api/agente-compara/comparison-chat`
8. autenticação e autorização por franquia

## 16. Consumo de linhas do AgenteCompara parece duplicado

Correlacionar `CleitonBillingApropriacao`, `ProcessingEvent`, `execution_id` e a chave idempotente do namespace `agente-compara-`.

## 17. Documento removido incorretamente entre agentes

Conferir:

1. `source_agent`
2. `session_key`
3. ownership antes da remoção física
4. testes de isolamento entre Júlia, Cleide e AgenteCompara

## 18. Cron falha por autenticação

Conferir:

1. header `X-Cron-Secret`
2. valor de `CRON_SECRET`
3. `tests/test_cron_auth.py`

`?secret=` ainda existe apenas por compatibilidade temporária.
