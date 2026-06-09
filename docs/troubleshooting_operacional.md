# Troubleshooting Operacional

Data de consolidacao: `2026-06-05`
Commit de referencia: `b5fc444`

## 1. Home publica sem responder

Validar:

1. `POST /api/onboarding_discovery`
2. disponibilidade das chaves Gemini de discovery
3. fallback local do discovery

Se Gemini falhar, o Copilot ainda deve responder via fallback local.

## 2. Home logada nao mostra a Julia corretamente

Comportamento esperado:

- usuario anonimo: Copilot de discovery na Home
- usuario logado: Julia operacional na Home
- `/chat_julia?mode=operational`: rota dedicada de acesso direto e handoff

Se a Home logada regredir para discovery puro, tratar como regressao.

## 3. Handoff para Julia nao acontece

Conferir:

1. atividade-fim estrategica do pedido
2. `app/copilot_capabilities.md` e `app/capability_taxonomy.py`
3. payload com `destination = "julia_operational"`
4. renderizacao das acoes em `app/static/js/chat_behavior.js`

## 4. Upload documental da Julia falha

Conferir:

1. login do usuario
2. `avaliar_autorizacao_operacao_por_franquia`
3. `upload_enabled`
4. limite de sessao e de arquivo
5. tipo aceito `.txt,.xml,.csv,.xlsx,.docx,.pdf`
6. limpeza de expirados e TTL

## 5. PDF parece lido quando nao deveria

Conferir:

1. `app/cleiton_doc_gemini_files.py`
2. `app/julia_doc_context.py`
3. tests `test_julia_chat_documental.py` e `test_cleiton_doc_pdf_gemini.py`

Regra oficial:

- nao fingir leitura de conteudo quando houver apenas placeholder
- se nao encontrar informacao no PDF, a resposta deve deixar isso explicito

## 6. Arquivos temporarios apareceram no Git

Conferir:

1. `.gitignore`
2. pasta `app/cleiton_doc_tmp/`

Essa pasta e artefato local/temporario e nao deve ser versionada.

## 7. Admin do Cleiton sem configuracao documental

Conferir:

1. rota/admin `agentes_cleiton`
2. template `app/painel_admin/template_admin/agentes_cleiton.html`
3. servico `app/services/cleiton_doc_config_service.py`

Regra oficial:

- configuracao documental usa `ConfigRegras`
- nao depende de migration nova

## 8. Onboarding abatendo franquia

Isso continua incorreto.

Conferir:

1. `flow_type = "onboarding_discovery"`
2. separacao de metricas no dashboard
3. ausencia de apropriacao operacional indevida
