# Troubleshooting Operacional

Data de consolidacao: `2026-05-29`  
Commit de referencia: `20fa165`

## 1. Home sem responder

Validar nesta ordem:

1. `POST /api/onboarding_discovery`
2. disponibilidade da chave Gemini (`GEMINI_API_KEY_1` ou `GEMINI_API_KEY`)
3. fallback local do discovery
4. logs do backend em `app/run_cleiton_discovery.py`

Se Gemini falhar, o Copilot ainda deve responder via fallback local. Se nem isso acontecer, tratar como regressao.

## 2. Limite anonimo do Copilot parece errado

Conferir:

1. contador de sessao em `_SESSION_ONBOARDING_DISCOVERY_COUNT`
2. limite `_ONBOARDING_DISCOVERY_LIMIT = 5`
3. reset por `POST /api/onboarding_discovery/reset`
4. botao `Nova conversa` na Home

Comportamento esperado:

- ate 5 interacoes: responde normalmente
- depois disso: bloqueia discovery, nao chama Gemini e mostra CTA de login

## 3. Handoff para Julia nao acontece

Conferir:

1. se a pergunta do usuario expressa atividade-fim estrategica
2. se `app/copilot_capabilities.md` e `app/copilot_capabilities.py` continuam alinhados
3. se o payload de onboarding retorna `handoff` ou `handoffs` com `destination = "julia_operational"`
4. se o frontend em `app/static/js/chat_behavior.js` esta renderizando as acoes

## 4. Julia aparece no lugar errado

Comportamento esperado:

- na Home, Julia pode aparecer apenas como shell visual do chat;
- o backend da Home continua sendo onboarding discovery;
- Julia operacional real exige login e usa `POST /api/chat_julia`.

Se a Home estiver consumindo `/api/chat_julia` diretamente em modo discovery, tratar como regressao.

## 5. Dashboard admin sem metricas de onboarding

Conferir:

1. `app/services/ia_metrics_service.py`
2. eventos `IaConsumoEvento` com `flow_type = "onboarding_discovery"`
3. renderizacao de `onboarding_discovery_ia` no template admin

Os campos esperados sao:

- `onboarding_tokens_month`
- `operational_tokens_month`
- `total_internal_tokens_month`

## 6. Onboarding abatendo franquia

Isso e comportamento incorreto.

Conferir:

1. se o consumo esta sendo lido como `flow_type = "onboarding_discovery"`
2. se a leitura administrativa esta separando onboarding de operacional
3. se algum ajuste local criou apropriacao indevida no trilho de franquia

Regra oficial:

- onboarding nao abate franquia.

## 7. Nuvem de palavras sem termos

Conferir:

1. existencia de `AuditoriaGerencial.tipo_decisao == "onboarding_discovery"`
2. presenca de `user_terms_normalized` em `contexto_json`
3. normalizacao em `app/utils/onboarding_text_normalization.py`
4. servico `app/services/onboarding_admin_analytics_service.py`

## 8. Termo ocultado nao some do dashboard

Conferir:

1. tabela `onboarding_word_cloud_hidden_term`
2. `is_active = true`
3. normalizacao do termo antes de persistir
4. refresh do dashboard admin

Lembrar:

- ocultacao atua apenas na agregacao;
- o historico bruto permanece.

## 9. Termo reexibido nao volta

Conferir:

1. rota `POST /admin/onboarding-word-cloud/hidden-terms/<term_id>/restore`
2. se `is_active` foi alterado para `false`
3. se o termo ainda aparece nos dados brutos do periodo consultado

## 10. Migration nova nao aplicada

Conferir:

1. `migrations/versions/r2s3t4u5v6w7_onboarding_word_cloud_hidden_term.py`
2. `alembic upgrade head`
3. tabela `onboarding_word_cloud_hidden_term` no banco alvo

Sem essa migration:

- hidden terms nao persistem;
- dashboard admin perde o controle manual da word cloud.
