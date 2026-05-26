# Estado Oficial Consolidado

Data de consolidacao: `2026-05-26`

Suite automatizada validada nesta consolidacao: `434 passed` em `81.32s`.

## 1. Escopo homologado e promovido

Estado funcional confirmado no repositorio:

- pipeline editorial da Julia com bloqueio oficial de fallback de redacao para artigos;
- geracao de imagem com persistencia em `settings.data_dir/generated` e exposicao publica por `/media/generated/`;
- retencao sem sobrescrita de `NoticiaPortal.url_imagem`;
- despublicacao editorial por `POST /admin/noticias/<id>/despublicar`;
- compartilhamento social publico em `app/templates/noticia_interna.html` + `app/templates/partials/social_share.html`;
- contrato SEO com `canonical == og:url == share_url_abs`;
- dashboard admin com bloco visual de Customer Insight ocultado, backend preservado;
- Cleide operacional com upload, template, filtros, analytics e chat controlado;
- observabilidade de IA e processamento via `IaConsumoEvento` e `ProcessingEvent`.

## 2. Arquitetura oficial resumida

- Roberto: estrategia, produto, direcionamento operacional e camada estrategica
- Cleiton: governanca, missao operacional, retencao, orquestracao, decisao operacional e execucao sistemica
- Julia: editorial, redacao, imagem, publicacao e conteudo publico
- Cleide: documentos, upload, perguntas, leitura operacional, extracao contextual e suporte operacional IA

Fluxo oficial:

`Roberto -> Cleiton -> Julia`

Camada operacional paralela oficial:

`Cleide -> Cleiton`

## 3. Julia: pipeline editorial oficial

Contrato de redacao:

- sucesso: `redacao_status=sucesso` e `redacao_fallback=False`
- fallback: `redacao_status=fallback` e `redacao_fallback=True`

Motivos confirmados no repositorio:

- `timeout`
- `json_parse_error`
- `empty_or_invalid_response`
- `model_error`
- `gemini_client_unavailable`

Regra oficial para artigo:

- se `redacao_status != sucesso`, bloquear;
- se `redacao_fallback=True`, bloquear;
- quando bloqueia, nao gera imagem, nao publica, audita e encerra pipeline.

Imagem Julia:

- persistencia antiga: `app/static/generated`
- persistencia atual: `settings.data_dir/generated`
- rota publica atual: `/media/generated/`

Observabilidade persistida em `assets_canais_json`:

- `imagem_status`
- `imagem_provider`
- `imagem_motivo`
- `imagem_origem`
- `imagem_url_final`
- `prompt_imagem_usado`

Runtime documentado:

- provider controlado por `IMAGE_PROVIDER`
- modelo principal por `GEMINI_MODEL_IMAGE`
- modelo fallback por `GEMINI_MODEL_IMAGE_FALLBACK`
- timeout efetivo por `GEMINI_IMAGE_HTTP_TIMEOUT_MS` ou `GEMINI_HTTP_TIMEOUT_MS`

Retencao editorial:

- a retencao nao altera `NoticiaPortal.url_imagem`;
- imagem publicada e patrimonio editorial imutavel;
- fallback visual e permitido apenas em renderizacao read-only quando o arquivo sumiu.

## 4. Editorial

Regras confirmadas:

- TTL oficial: 5 dias para `rss`, `api` e `import_legacy`;
- pauta manual nao e bloqueada por TTL;
- serie editorial nao e bloqueada por TTL;
- reprocessamento elegivel: `status=pendente` e `arquivada=False`.

Despublicacao:

- rota oficial: `POST /admin/noticias/<id>/despublicar`
- efeitos oficiais: `publicado_em=None` e `status_publicacao=despublicado`
- conteudo despublicado sai da superficie publica.

## 5. Compartilhamento social e SEO

Escopo:

- conteudo publico da Julia: `artigo` e `noticia`/insight na superficie `/noticia/<id>`

Templates oficiais:

- pagina: `app/templates/noticia_interna.html`
- partial: `app/templates/partials/social_share.html`

Redes publicas confirmadas:

- Facebook
- Threads
- X
- LinkedIn
- WhatsApp

Contrato de WhatsApp:

- `https://api.whatsapp.com/send`

Contrato de ambiente:

- producao usa `https://www.agentefrete.com.br` por padrao;
- homolog usa o host homolog configurado;
- `PUBLIC_BASE_URL` e a fonte de verdade da base publica.

Contrato publico obrigatório:

- `canonical == og:url == share_url_abs`

Limites do recurso:

- nao usa IA;
- nao gera `IaConsumoEvento`;
- nao faz billing;
- nao dispara pipeline.

## 6. Cleide: IA operacional

Objetivo oficial:

- documentos
- upload
- leitura operacional
- perguntas e respostas
- extracao de informacao
- suporte operacional

Fluxo oficial:

`usuario -> upload/pergunta -> Cleide -> processamento IA/controlado -> resposta operacional`

Responsabilidades:

- interpretacao documental
- perguntas operacionais
- extracao contextual
- fallback inteligente
- observabilidade operacional

Limites de governanca:

- nao substitui Cleiton
- nao substitui Roberto
- nao substitui Julia

Admin Cleide confirmado:

- tela oficial `/admin/agentes/cleide`
- controles de contexto e upload
- `chat_context_mode`
- `upload_total_max`
- toggles de `transportadora`, `uf_origem`, `uf_destino`, `temporal` e `paretos`

Superficie operacional confirmada:

- upload exige autenticacao e autorizacao operacional;
- download do template nao exige login;
- chat exige autenticacao e autorizacao operacional;
- filtros analiticos passam pelo backend oficial.

Observabilidade e fallback:

- `flow_type`
- `ai_flow_type`
- `ai_used`
- `fallback_used`
- `policy_blocked`
- `context_status`
- `view_scope`
- `active_filters`
- `error_code`

Falha IA:

- fallback governado e auditavel;
- `provider_error` entrega resposta por contingencia controlada.

Falha upload:

- resposta deve expor causa e orientacao operacional;
- upload continua com metricas e status rastreaveis.

Privacidade historica:

- perguntas historicas nao devem ser armazenadas integralmente;
- somente nuvem de palavras e agregacoes editoriais/operacionais devem permanecer como contrato documental.

## 7. Consumo IA e governanca

Contratos oficiais:

- `IaConsumoEvento`: tentativa real de chamada LLM;
- `ProcessingEvent`: processamento tecnico nao-LLM e snapshots intermediarios;
- identidade operacional: `conta_id`, `franquia_id`, `usuario_id`;
- trilho oficial: Cleiton.

Campos de runtime e observabilidade confirmados:

- `provider`
- `model`
- `timeout`
- `tentativa`
- `duracao`
- `error_summary`
- `status`
- `flow_type`
- `agent`

## 8. Dashboard admin

Contrato oficial atual:

- bloco visual `Insight Estratégico (Customer Insight)` ocultado do dashboard;
- backend e servicos associados permanecem preservados.

## 9. Testes mapeados

Suite atual:

- `434 passed`

Coberturas confirmadas:

- editorial: `tests/test_julia_pipeline.py`, `tests/test_julia_redacao_metadata.py`
- imagem: `tests/test_julia_imagem_fluxo.py`, `tests/test_web_media_generated.py`
- pipeline: `tests/test_editorial_surface_contract.py`, `tests/test_julia_pipeline.py`
- share e SEO: `tests/test_social_share.py`
- runtime e observabilidade: `tests/test_ia_metrics_service.py`
- Cleide: `tests/test_cleide_*`
- upload e documentos: `tests/test_cleide_upload_api.py`, `tests/test_legal_documents_persistent_storage.py`
- admin/editorial: `tests/test_admin_despublicacao_editorial.py`, `tests/test_admin_dashboard_visual.py`

## 10. Resumo executivo

O estado oficial consolidado do LogCompleta / Agentefrete hoje e um sistema com governanca central do Cleiton, editorial publico da Julia, estrategia e BI do Roberto e IA operacional controlada da Cleide. O projeto preserva contratos claros, runtime rastreavel, superficie publica canonica, fallback auditado e trilha oficial de consumo sem bypasss paralelos.
