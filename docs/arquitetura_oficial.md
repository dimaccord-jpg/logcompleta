# Arquitetura Oficial

Data de consolidacao: `2026-06-16`
Commit de referencia: `5f10f6d`

Este documento registra a arquitetura oficial do projeto no estado promovido em producao apos a estabilizacao da auditoria documental da Cleide.

## Principios

Nao criar:

- rota paralela de produto para upload documental
- bypass de autorizacao ou franquia
- leitura documental inventada
- persistencia desnecessaria em banco para contexto temporario
- documentacao que trate a tabela temporaria como auditoria final ou dado persistente

Sempre:

- usar os trilhos oficiais
- tratar o codigo atual como fonte de verdade
- manter observabilidade e governanca centrais no Cleiton
- manter a separacao entre extracao documental e chat conversacional da Cleide

## Superficies e papeis

### Copilot

- superficie: Home publica `/`
- papel: discovery, onboarding e esclarecimento da atividade-fim
- publico: anonimo ou logado
- nao executa a conversa operacional plena da Julia

### Julia

- superficie principal do usuario autenticado: Home `/`
- superficie dedicada mantida: `/chat_julia?mode=operational`
- endpoint: `POST /api/chat_julia`
- papel: estrategia, supply chain, negociacao, interpretacao executiva e plano de acao
- requisito: login e autorizacao operacional

### Cleiton

- papel: governanca operacional central
- controla autorizacao, observabilidade, limites, upload documental, preparo de contexto e integracao com IA
- e o owner operacional da tabela temporaria da auditoria documental
- nao deve ser descrito apenas como agente de chat

### Roberto

- superficie: `/fretes`
- papel: BI operacional, tendencias, previsoes e horizonte futuro

### Cleide

- superficie BI atual: `/cleide-bi-frete`
- superficie documental atual: `/auditoria-frete`
- papel: auditoria, conferencia, desvios e horizonte historico
- na superficie documental, o chat e separado da extracao tecnica de tabela temporaria

## Regra-mae de handoff

- artefato nao define agente
- atividade-fim e horizonte temporal definem agente

Tabela canonica:

| Objetivo do usuario | Agente |
| --- | --- |
| Prever, projetar, estimar tendencia futura | Roberto |
| Auditar, conferir, investigar o ocorrido | Cleide |
| Decidir, negociar, planejar, interpretar executivamente | Julia |

## Fronteiras tecnicas oficiais

### Copilot

- `POST /api/onboarding_discovery`
- `POST /api/onboarding_discovery/reset`

### Julia

- `POST /api/chat_julia`
- `POST /api/julia/documents/upload`
- `GET /api/julia/documents`
- `DELETE /api/julia/documents/<doc_id>`
- `POST /api/julia/documents/clear`

### Roberto

- `POST /api/chat_roberto`
- `/fretes`

### Cleide

- `POST /api/chat_cleide`
- `/api/cleide/upload`
- `/api/cleide/upload/status`
- `/api/cleide/upload/clear`
- `POST /api/cleide-auditoria/documents/upload`
- `GET /api/cleide-auditoria/documents/status`
- `DELETE /api/cleide-auditoria/documents/<doc_id>`
- `POST /api/cleide-auditoria/documents/clear`
- `POST /api/cleide-auditoria/chat`

## Arquitetura documental da Cleide Auditoria

O fluxo documental da Cleide Auditoria opera dentro da governanca existente e nao deve ser tratado como produto paralelo.

Contratos reais:

- o upload registra o documento na sessao e tenta acionar extracao tecnica pos-upload
- a atualizacao da tabela temporaria continua no fluxo documental/status, nao no chat
- a extracao tecnica usa `app/run_cleide_audit_temp_table.py`
- o prompt tecnico fica em `build_cleide_audit_temp_table_technical_prompt()`
- a resposta esperada do modelo e JSON tecnico, sem auditoria final
- o endpoint de status devolve `temp_table` quando houver artefato ativo
- a UI exibe um card clicavel de tabela temporaria e abre modal somente leitura
- a priorizacao visual e operacional recai sobre blocos operacionais, rotas/tabelas de frete e informacoes adicionais

Garantias:

- a tabela temporaria e separada do chat conversacional
- a tabela temporaria e temporaria, descartavel e sujeita a validacao humana
- a tabela temporaria e readonly na experiencia da Cleide
- o `operational_owner` e `cleiton`
- o ciclo de vida depende do TTL dos documentos da sessao
- invalidacoes ocorrem quando os documentos fonte mudam ou sao removidos
- nao ha migration nova, nova tabela de banco, novo campo ou alteracao manual de schema para esse fluxo
- nao ha conexao nova com APIs da Julia ou do BI nessa superficie documental

## Arquitetura documental da Julia

O fluxo documental faz parte da experiencia operacional logada da Julia. Nao existe fluxo documental independente de produto.

Contratos reais:

- a UI da Julia aceita `.txt,.xml,.csv,.xlsx,.docx,.pdf`
- a API exige autenticacao
- a API chama `avaliar_autorizacao_operacao_por_franquia`
- o store temporario usa `app/cleiton_doc_tmp/`
- o store persiste JSON tecnico temporario, nao cria tabela nova
- o diretorio `app/cleiton_doc_tmp/` esta protegido no `.gitignore`
- PDF pode usar Gemini Files como contexto multimodal governado

Tipos suportados na implementacao atual:

- `TXT`
- `XML`
- `CSV`
- `XLSX`
- `DOCX`
- `PDF`

Garantias:

- limites de sessao e por tipo sao configuraveis no admin do Cleiton
- placeholder nao deve fingir leitura de conteudo inexistente
- TTL e cleanup sao governados pelo Cleiton

## Configuracao administrativa

O bloco documental do Cleiton fica no admin e reutiliza `ConfigRegras`.

Campos documentados pelo codigo atual:

- `upload_enabled`
- `max_files_per_session`
- `session_max_bytes`
- `upload_ttl_hours`
- `cleanup_enabled`
- `prompt_context_max_chars`
- `prompt_max_files_considered`
- chaves `pdf_*`, `excel_*`, `docx_*`, `txt_*`, `xml_*`, `csv_*`

Nao ha migration nova nem tabela nova para esse bloco.
