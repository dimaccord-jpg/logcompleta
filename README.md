# LogCompleta / AgenteFrete

Data de consolidacao: `2026-07-08`
Branch operacional esperada: `homolog`
Commit de referencia em homolog: `efd54b5`
Commit promovido em producao: `3d5332b`

## Estado oficial atual

- `homolog` contem `efd54b5`
- `origin/homolog` esta sincronizada com `homolog`
- a promocao mais recente para producao foi feita por cherry-pick seletivo
- `producao` recebeu `3d5332b`
- a entrega foi validada sem alteracao de schema, migration, tabela, campo ou banco
- os temporarios `app/cleiton_doc_tmp/tt_*.json` continuam fora de versionamento

## Superficies oficiais

- `/`: Home publica com Copilot de discovery; Home logada com Julia operacional
- `/chat_julia?mode=operational`: rota dedicada da Julia
- `/fretes`: Roberto BI e chat analitico
- `/cleide-bi-frete`: BI operacional legado/estrutural da Cleide sobre dataset de sessao
- `/auditoria-frete`: Cleide Auditoria documental com upload, tabela temporaria, auditoria e BI executivo da auditoria
- `/feed`: superficie editorial
- `/admin/...`: configuracao e operacao administrativa

## Dominios

- Julia: superficie operacional principal do usuario autenticado
- Roberto: leitura quantitativa, historico, previsao e explicacao analitica sobre fretes
- Cleide: duas superficies separadas
- Cleide BI em `/cleide-bi-frete`: upload de XLSX/CSV, validacao estrutural, KPIs e dashboard estrutural
- Cleide Auditoria em `/auditoria-frete`: auditoria de frete com upload documental, tabela temporaria, lote auditado e BI executivo da auditoria
- Cleiton: governanca central de autorizacao, limites, sessao documental, observabilidade e identidade de consumo

## Cleide Auditoria

### Contrato da pagina

- a rota visual `/auditoria-frete` e publica
- os endpoints de upload, status, auditoria e chat exigem autenticacao e autorizacao operacional por franquia
- a pagina nao e um produto paralelo ao Cleiton; ela reutiliza a trilha documental governada

### Fluxo real

1. upload documental em `POST /api/cleide-auditoria/documents/upload`
2. extracao tecnica pos-upload tenta gerar a `temp_table`
3. revisao humana da tabela temporaria em `POST /api/cleide-auditoria/temp-table/save`
4. etapa opcional de coverage em `POST /api/cleide-auditoria/coverage/upload`
5. download do template do lote auditado em `GET /api/cleide-auditoria/audit-template`
6. upload do lote auditado em `POST /api/cleide-auditoria/audit/upload`
7. processamento da auditoria em `POST /api/cleide-auditoria/audit/run`
8. correcoes assistidas em:
- `POST /api/cleide-auditoria/audit/correction/preview`
- `POST /api/cleide-auditoria/audit/correction/apply`
- `POST /api/cleide-auditoria/audit/correction/undo`
9. chat contextual em `POST /api/cleide-auditoria/chat`

### Regras de arquitetura

- a `temp_table` e artefato temporario, descartavel e governado pelo dominio Cleiton
- a Cleide nao e camada de persistencia definitiva da `temp_table`
- o chat da Cleide consulta contexto documental, mas nao cria nem gerencia o ciclo de vida da `temp_table`
- a extração tecnica e separada do chat conversacional
- a extração tecnica nao e checklist fixo de chat, entrevista rigida, regex de negocio nem Q&A engessado

### BI executivo da auditoria

O BI executivo da auditoria fica dentro de `/auditoria-frete` e hoje trabalha com 4 graficos:

- Impacto Financeiro por Transportadora
- Divergencia Financeira por UF Destino
- Evolucao da Divergencia no Periodo
- Pareto do Valor Cobrado a Mais

Regras documentadas no frontend atual:

- o calculo financeiro usa `charged_freight`, `expected_freight` e `divergence_value` quando esses campos estao disponiveis
- o BI executivo da auditoria nao usa mais o contrato antigo de 7 graficos
- referencias a `UF origem`, `Volume por Transportadora` e `Pareto por UF` nao representam o contrato atual desse BI da auditoria

### Modulos centrais

- `app/cleide_audit_doc_service.py`: sessao documental da auditoria, ciclo de vida da `temp_table`, coverage table, lote auditado e calculo da auditoria
- `app/run_cleide_audit_temp_table.py`: pipeline tecnico pos-upload, separado do chat, com fallback de modelo e parsing seguro de JSON
- `app/cleide_audit_prompt.py`: prompt tecnico da extração e prompt conversacional da auditoria
- `app/cleide_audit_routes.py`: endpoints documentais, lote auditado, correcoes e chat
- `app/cleide_audit_correction_service.py`: preview, apply e undo de correcoes assistidas sobre a tabela cadastrada
- `app/static/js/cleide_auditoria.js`: experiencia completa de `/auditoria-frete`, incluindo BI executivo, modal da `temp_table`, coverage, auditoria e correcoes
- `app/templates/cleide_auditoria.html`: shell visual da auditoria

## Cleide BI em `/cleide-bi-frete`

- continua existindo como superficie separada
- trabalha com upload operacional de XLSX/CSV, validacao estrutural, KPIs e dashboard estrutural
- o dashboard atual dessa superficie ainda expoe agregacoes como transportadora, UF origem, UF destino, serie temporal e paretos estruturais
- esse contrato nao deve ser confundido com o BI executivo da auditoria em `/auditoria-frete`

## Admin

### `/admin/agentes/cleide`

Hoje ha dois blocos independentes:

- configuracao do BI estrutural da Cleide (`cleide_cfg_*`)
- configuracao da Cleide Auditoria (`cleide_audit_cfg_*`)

Opcoes reais da Cleide Auditoria:

- `chat_enabled`
- `upload_enabled`
- `chat_max_history`
- `document_context_max_chars`
- `max_documents_considered`
- `question_max_chars`
- `audited_file_max_bytes`
- `audited_file_max_rows`
- `no_documents_behavior`
- `show_documents_used`
- `no_hallucination_instruction_enabled`
- `fallback_message`
- `calculation_bases`

As bases de calculo administrativas sao configuraveis e usadas para orientar a interpretacao de taxas e operacoes como percentual sobre NF, valor fixo por CTe/documento, por kg e por fracao de 100kg.

## Governanca, observabilidade e seguranca

- a autorizacao operacional central vem de `avaliar_autorizacao_operacao_por_franquia`
- o upload documental governado do Cleiton continua sendo o teto superior para sessao, TTL, cleanup e limites por tipo
- a observabilidade de consumo LLM persiste `IaConsumoEvento`
- o processamento tecnico nao-LLM persiste `ProcessingEvent`
- ha metricas administrativas de tokens, custos e contagem de eventos em `app/services/ia_metrics_service.py`
- a identidade de consumo considera `conta_id`, `franquia_id` e `usuario_id`
- onboarding discovery continua consumo interno e nao abate franquia operacional
- na auditoria da Cleide ha observabilidade parcial do ciclo documental e do processamento; nao documentar metricas inexistentes por grafico ou por acao da UI

## Deploy e operacao

- branch oficial de homologacao: `homolog`
- branch funcional de producao: `producao`
- em divergencia entre `homolog` e `producao`, a promocao deve continuar seletiva por cherry-pick e nao por merge cego
- validar schema do ambiente antes de commit/push/deploy
- manter fora de commit: `.db`, `__pycache__`, `.pytest_cache`, caches e temporarios locais
- validar producao apos deploy

## Testes

Suite recentemente validada:

```powershell
pytest tests/test_cleide_audit_correction_service.py tests/test_cleide_admin_routes.py tests/test_cleide_audit_chat_routes.py tests/test_cleide_audit_config_service.py tests/test_cleide_audit_doc_context.py tests/test_cleide_audit_doc_routes.py tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py -q
```

Resultado conhecido:

- `717 passed, 2 warnings`
- warnings conhecidos:
- `DeprecationWarning` de `flask_session.filesystem.FileSystemSessionInterface`
- `DeprecationWarning` de `google.genai.types._UnionGenericAlias`
