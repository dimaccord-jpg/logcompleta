# Agentefrete / Log Completa

Data de consolidacao: `2026-06-05`
Estado de referencia: `producao` / commit `b5fc444`

Este `README.md` resume o estado real promovido em producao apos a entrega do upload documental governado da Julia:

- `98012c8 feat: adiciona upload documental governado à Julia`
- `b5fc444 fix: estabiliza desempenho documental da Julia`

## Estado atual do produto

Superficies oficiais ativas:

- `/`: Home publica com Copilot de discovery para usuario anonimo ou logado
- `/`: Home logada com Julia operacional embutida como superficie principal do usuario autenticado
- `/chat_julia?mode=operational`: rota operacional dedicada da Julia, mantida para handoff e acesso direto
- `/fretes`: Roberto BI operacional e preditivo
- `/cleide-bi-frete`: Cleide BI operacional (upload, KPIs, dashboard e chat)
- `/feed`: superficie editorial
- `/admin/dashboard`: painel administrativo

## Personas do produto

- Copilot: descoberta publica e onboarding sem login
- Julia: assistente estrategica e operacional logada, com suporte documental governado
- Cleiton: governanca operacional, autorizacao, observabilidade, upload documental, limites e integracao com IA
- Roberto: BI, previsoes e leitura forward-looking de fretes
- Cleide: auditoria, conferencia e leitura retrospectiva

## Copilot da Home

Contrato oficial:

- endpoint: `POST /api/onboarding_discovery`
- reset: `POST /api/onboarding_discovery/reset`
- funciona com ou sem login
- limite anonimo: `5` interacoes por sessao
- ao atingir o limite, retorna CTA de login para continuar gratuitamente com a Julia
- usa Gemini governado por Cleiton Discovery, com fallback local quando necessario
- nao consome franquia operacional do cliente

O Copilot nao se confunde com a Julia. Onboarding publico continua separado da experiencia operacional logada.

## Julia operacional

A Julia e a superficie operacional logada na Home. A rota `/chat_julia?mode=operational` continua valida, mas a experiencia principal consolidada no codigo atual e a Home autenticada.

Contrato real:

- endpoint oficial: `POST /api/chat_julia`
- exige autenticacao
- valida autorizacao operacional por franquia antes de consumir IA
- pode receber contexto resumido do Copilot quando houver handoff `julia_operational`
- exibe o botao de documentos como parte da experiencia operacional logada

## Upload documental governado

O upload documental da Julia nao e um fluxo solto nem uma rota de produto paralela. Ele faz parte da conversa operacional logada e e governado pelo Cleiton.

Fluxo oficial:

- upload: `POST /api/julia/documents/upload`
- listagem: `GET /api/julia/documents`
- remocao: `DELETE /api/julia/documents/<doc_id>`
- limpeza: `POST /api/julia/documents/clear`

Tipos aceitos na UI e nos conversores atuais:

- `TXT`
- `XML`
- `CSV`
- `XLSX`
- `DOCX`
- `PDF`

Governanca aplicada pelo Cleiton:

- autenticacao obrigatoria
- autorizacao operacional por franquia
- validacao por extensao/tamanho/tipo habilitado
- limite por sessao e por arquivo
- TTL de contexto documental
- limpeza automatica opcional
- preparo de contexto textual e multimodal para a IA
- integracao governada com Gemini Files para PDF quando aplicavel

Garantias importantes:

- PDF pode entrar como contexto multimodal via Gemini Files
- o sistema nao deve fingir leitura quando houver apenas placeholder ou quando o conteudo nao estiver pronto
- arquivos temporarios ficam em `app/cleiton_doc_tmp/` e nao devem ser versionados
- o store temporario persiste apenas JSON tecnico, sem bruto documental no banco

## Cleiton

Cleiton nao deve ser descrito apenas como agente de chat. No estado atual ele e a camada central de governanca operacional.

Responsabilidades relevantes nesta entrega:

- autorizacao operacional por franquia
- observabilidade do consumo
- validacao e seguranca documental
- controle de sessao e TTL
- limites administrativos por tipo de arquivo
- preparo de contexto para Julia
- integracao com Gemini Files para PDF

Configuracao administrativa documental:

- fica no admin de Cleiton
- usa `ConfigRegras`, sem nova migration nesta entrega
- bloco oficial: upload habilitado, maximo de arquivos por sessao, bytes totais da sessao, TTL, cleanup, limite de caracteres de contexto, maximo de arquivos considerados por resposta e limites por tipo

## Observabilidade oficial

Eventos oficiais:

- `IaConsumoEvento`: chamadas LLM reais
- `ProcessingEvent`: processamento tecnico e snapshots auxiliares

Fluxos documentados:

- `onboarding_discovery`
- `operacional`
- `administrativo`

Leituras administrativas:

- `operational_tokens_month`
- `onboarding_tokens_month`
- `total_internal_tokens_month`

Regra critica:

- onboarding conta como consumo interno
- onboarding nao abate franquia

## Banco e migrations

Esta entrega documental e funcional da Julia:

- nao criou migration nova em `migrations/versions`
- nao criou nova tabela para configuracao documental
- reutiliza o mecanismo existente `ConfigRegras`

Migration relevante ja existente e mantida:

- `r2s3t4u5v6w7_onboarding_word_cloud_hidden_term.py`

## Testes relevantes da entrega

Cobertura diretamente relacionada:

- `tests/test_cleiton_doc_config_service.py`
- `tests/test_cleiton_doc_store.py`
- `tests/test_cleiton_doc_converters.py`
- `tests/test_cleiton_admin_routes.py`
- `tests/test_julia_chat_documental.py`
- `tests/test_julia_documents_ui.py`
- `tests/test_onboarding_discovery.py`
- `tests/test_cleiton_doc_pdf_gemini.py`
- `tests/test_julia_chat_plan_limit.py`
- `tests/test_julia_pdf_documental_chat.py`

Validacao critica registrada antes da promocao:

- `233 passed, 2 warnings`
- warnings de dependencia/deprecacao, sem falha funcional

## Documentos oficiais de apoio

Ler nesta ordem:

1. `docs/estado_oficial_consolidado.md`
2. `docs/arquitetura_oficial.md`
3. `docs/runtime_ia_e_observabilidade.md`
4. `docs/onboarding_tecnico.md`
5. `docs/troubleshooting_operacional.md`
6. `docs/runbook_onboarding_copilot.md`
7. `app/README_RUN.md`
8. `app/README_DEPLOY.md`
9. `migrations/README`

## Honestidade de produto

Nao documentar nem prometer:

- cotacao automatizada de fretes
- BID
- TMS/WMS
- automacao operacional inexistente
- leitura documental fingida quando o conteudo nao foi extraido
