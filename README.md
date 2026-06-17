# Agentefrete / Log Completa

Data de consolidacao: `2026-06-16`
Estado de referencia: `producao` / merge `5f10f6d`

Este `README.md` resume o estado real homologado e promovido apos a estabilizacao da auditoria documental da Cleide:

- `c5a73e1 feat: estabiliza tabela temporaria da auditoria Cleide`
- `5f10f6d merge: promove estabilizacao da auditoria Cleide para producao`

Confirmacoes operacionais:

- `homolog` contem `c5a73e1`
- `producao` contem merge `5f10f6d`
- homolog foi validada antes da promocao
- producao foi validada e aprovada apos deploy
- nao houve migration nova, nova tabela, novo campo ou alteracao manual de schema

## Estado atual do produto

Superficies oficiais ativas:

- `/`: Home publica com Copilot de discovery para usuario anonimo ou logado
- `/`: Home logada com Julia operacional embutida como superficie principal do usuario autenticado
- `/chat_julia?mode=operational`: rota operacional dedicada da Julia, mantida para handoff e acesso direto
- `/fretes`: Roberto BI operacional e preditivo
- `/cleide-bi-frete`: Cleide BI operacional (upload, KPIs, dashboard e chat)
- `/auditoria-frete`: Cleide Auditoria documental com upload, chat e tabela temporaria extraida
- `/feed`: superficie editorial
- `/admin/dashboard`: painel administrativo

## Personas do produto

- Copilot: descoberta publica e onboarding sem login
- Julia: assistente estrategica e operacional logada, com suporte documental governado
- Cleiton: governanca operacional, autorizacao, observabilidade, upload documental, limites e integracao com IA
- Roberto: BI, previsoes e leitura forward-looking de fretes
- Cleide: entrevista de auditoria, conferencia e leitura retrospectiva

## Cleide Auditoria documental

Estado atual promovido:

- a rota visual oficial e `/auditoria-frete`
- o upload documental usa a governanca existente do Cleiton
- apos cada upload valido, o backend tenta extrair uma tabela temporaria de frete
- a extracao/atualizacao da tabela temporaria ocorre no fluxo pos-upload/status/documentos
- a tabela temporaria fica separada do chat conversacional da Cleide
- o chat consulta contexto documental, mas nao deve criar, alterar ou sobrescrever a tabela temporaria
- a interface exibe a tabela temporaria na area de documentos anexados/estado documental como card clicavel
- o modal da tabela temporaria opera em modo somente leitura, com revisao e validacao humana
- a extracao nao cria rota paralela de produto nem nova tabela de banco

Contratos oficiais do fluxo:

- upload: `POST /api/cleide-auditoria/documents/upload`
- status documental: `GET /api/cleide-auditoria/documents/status`
- remocao: `DELETE /api/cleide-auditoria/documents/<doc_id>`
- limpeza: `POST /api/cleide-auditoria/documents/clear`
- chat: `POST /api/cleide-auditoria/chat`

Tabela temporaria extraida:

- exibida na interface como `Tabela temporaria extraida`
- persistida apenas como artefato tecnico temporario em arquivo/session/JSON
- descartavel, derivada dos documentos da sessao e sujeita a TTL
- governada operacionalmente pelo Cleiton, nao pela Cleide
- vinculada ao ciclo documental da sessao
- invalidada quando os documentos fonte mudam, sao removidos ou deixam de existir na sessao
- obrigatoriamente sujeita a validacao humana
- nao substitui auditoria final nem deve ser descrita como dado persistente
- nao cria migration, tabela, campo nem alteracao manual de schema

Estados documentados no codigo atual:

- `processing`
- `awaiting_validation`
- `validated`
- `needs_review`
- `failed`
- `expired`
- `discarded`

Extracao tecnica:

- modulo principal: `app/run_cleide_audit_temp_table.py`
- prompt tecnico: `build_cleide_audit_temp_table_technical_prompt()` em `app/cleide_audit_prompt.py`
- usa Gemini governado pelo Cleiton, com timeout proprio e fallback de modelo
- responde JSON tecnico para estruturacao de tabela temporaria, sem montar auditoria final

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
- `app/cleiton_doc_tmp/` esta protegido no `.gitignore`
- arquivos `tt_*.json`, `.cleanup_meta.json` e residuos `.json` dessa pasta nao devem entrar em commit
- residuos `app/.tmp_repro_unit*` nao devem ser versionados
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
- ownership operacional da tabela temporaria da Cleide Auditoria

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

Esta entrega da Cleide Auditoria:

- nao criou migration nova em `migrations/versions`
- nao alterou `app/models.py`
- nao criou nova tabela de banco nem novo campo
- nao exigiu alteracao manual de schema
- nao versiona banco local nem arquivo `.db`
- reutiliza o store temporario e a governanca existente

Migration relevante ja existente e mantida:

- `r2s3t4u5v6w7_onboarding_word_cloud_hidden_term.py`

## Testes relevantes da entrega

Cobertura diretamente relacionada:

- `tests/test_cleide_audit_temp_table.py`
- `tests/test_cleide_auditoria_page.py`

Validacoes registradas antes da promocao:

- `python -m pytest tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py`
- resultado: `109 passed, 2 warnings`
- `python -m pytest`
- resultado: `1124 passed, 36 warnings`
- warnings conhecidos de dependencia/uso legado, sem bloqueio da entrega

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
- tabela temporaria da Cleide como auditoria final
