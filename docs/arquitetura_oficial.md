# Arquitetura Oficial

Data de consolidacao: `2026-06-05`
Commit de referencia: `b5fc444`

Este documento registra a arquitetura oficial do projeto no estado promovido em producao apos a entrega documental da Julia.

## Principios

Nao criar:

- rota paralela de produto para upload documental
- bypass de autorizacao ou franquia
- leitura documental inventada
- persistencia desnecessaria em banco para contexto temporario

Sempre:

- usar os trilhos oficiais
- tratar o codigo atual como fonte de verdade
- manter observabilidade e governanca centrais no Cleiton

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
- nao deve ser descrito apenas como agente de chat

### Roberto

- superficie: `/fretes`
- papel: BI operacional, tendencias, previsoes e horizonte futuro

### Cleide

- superficie BI atual: `/cleide-bi-frete`
- papel: auditoria, conferencia, desvios e horizonte historico

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

## Arquitetura documental da Julia

O fluxo documental faz parte da experiencia operacional logada da Julia. Nao existe fluxo documental independente de produto.

Contratos reais:

- a UI da Julia aceita `.txt,.xml,.csv,.xlsx,.docx,.pdf`
- a API exige autenticacao
- a API chama `avaliar_autorizacao_operacao_por_franquia`
- o store temporario usa `app/cleiton_doc_tmp/`
- o store persiste JSON tecnico temporario, nao cria tabela nova
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
