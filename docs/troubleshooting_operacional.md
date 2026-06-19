# Troubleshooting Operacional

Data de consolidacao: `2026-06-17`
Commit de referencia: `284b340`

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

## 9. Tabela temporaria da Cleide nao aparece apos upload

Conferir:

1. `POST /api/cleide-auditoria/documents/upload`
2. `GET /api/cleide-auditoria/documents/status`
3. `app/run_cleide_audit_temp_table.py`
4. `app/cleide_audit_doc_service.py`
5. `app/cleide_audit_prompt.py`

Regra oficial:

- a extracao acontece apos upload bem-sucedido
- o chat da Cleide continua separado da extracao tecnica
- Cleiton e o owner operacional da tabela temporaria
- `temp_table` pode nao existir se a extracao falhar, expirar ou se os documentos fonte mudarem
- o modal da tabela temporaria permanece somente leitura

## 10. Revisao humana da tabela temporaria falhou

Conferir:

1. `POST /api/cleide-auditoria/temp-table/save`
2. escopo de sessao, usuario e franquia do artefato ativo
3. tamanho do payload enviado
4. se a `temp_table` ainda existe e nao expirou

Regra oficial:

- a revisao humana e permitida apenas para a `temp_table` ativa da sessao
- erro de escopo, expiracao ou `temp_table_id` divergente deve bloquear a operacao
- o chat da Cleide nao substitui esse fluxo de revisao

## 11. Tabela temporaria sumiu depois de remover ou trocar documentos

Comportamento esperado:

- a tabela temporaria acompanha os documentos ativos da sessao
- ao remover ou substituir documento fonte, a tabela anterior pode ser invalidada
- o estado pode ir para `discarded` ou deixar de aparecer no payload publico

Isso nao e regressao por si so. E o comportamento oficial do ciclo temporario governado.

## 12. Chat da Cleide alterou a tabela temporaria

Isso deve ser tratado como regressao.

Conferir:

1. se a alteracao ocorreu apos `POST /api/cleide-auditoria/chat`
2. se houve novo upload, remocao ou limpeza documental
3. se o payload de status mudou sem evento documental correspondente

Regra oficial:

- o chat consulta contexto documental
- o chat nao deve recriar, alterar ou sobrescrever a tabela temporaria

## 13. Arquivos temporarios tecnicos apareceram no Git

Conferir:

1. `.gitignore`
2. residuos `app/.tmp_repro_unit*`
3. residuos `app/cleiton_doc_tmp/tt_*.json`
4. `.cleanup_meta.json` e outros `.json` residuais em `app/cleiton_doc_tmp/`

Regra oficial:

- artefatos temporarios de teste e da temp_table nao devem ser versionados
- `app/cleiton_doc_tmp/` esta coberto pelo `.gitignore`
