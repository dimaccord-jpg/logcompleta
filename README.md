# LogCompleta / AgenteFrete

Data de consolidacao: `2026-07-10`
Branch operacional confirmada: `homolog`
Commit homolog confirmado no workspace: `d02ce15 feat: aprimora auditoria de frete da Cleide`
Commit de promocao aprovado em producao: `6efa2e2 feat: aprimora auditoria de frete da Cleide`

## Estado oficial atual

- `homolog` local e `origin/homolog` apontam para `d02ce15`
- existe branch `producao` no repositório apontando para `6efa2e2`
- o `render.yaml` versionado aponta o servico de homolog para `branch: homolog` com `autoDeploy: true`
- o `render.yaml` versionado aponta o servico de producao para `branch: main` com `autoDeploy: false`
- antes de promover, o painel do Render continua sendo a confirmacao operacional obrigatoria
- esta entrega nao adicionou tabela, campo, schema, banco ou migration nova
- `app/cleiton_doc_tmp/` continua fora de versionamento; `tt_*.json` ali sao artefatos temporarios locais

## Superficies oficiais

- `/`: Home publica com Copilot de discovery; Home logada com Julia operacional
- `/chat_julia?mode=operational`: acesso direto e handoff da Julia
- `/fretes`: Roberto com upload privado, BI e chat analitico
- `/cleide-bi-frete`: BI estrutural da Cleide sobre dataset de sessao
- `/auditoria-frete`: auditoria documental da Cleide com upload, `temp_table`, coverage opcional, lote auditado, correcao assistida e BI executivo
- `/feed`: superficie editorial
- `/admin/...`: configuracao e operacao administrativa

## Dominios

- Julia: superficie operacional principal para usuario autenticado
- Roberto: leitura quantitativa, historico, previsao e explicacao analitica sobre fretes
- Cleide BI: upload estrutural de XLSX/CSV, validacao estrutural e dashboard de sessao em `/cleide-bi-frete`
- Cleide Auditoria: auditoria documental ponta a ponta em `/auditoria-frete`
- Cleiton: governanca central de autorizacao, limites documentais, TTL, cleanup, identidade de consumo, observabilidade e ownership operacional da `temp_table`

## Cleide Auditoria

### Contrato da pagina

- `GET /auditoria-frete` e publico
- endpoints de upload, status, revisao, coverage, lote auditado, auditoria e chat exigem autenticacao e `avaliar_autorizacao_operacao_por_franquia`
- a superficie reutiliza a trilha documental governada do Cleiton; nao e um subsistema de persistencia separado

### Fluxo real

1. upload documental em `POST /api/cleide-auditoria/documents/upload`
2. extracao tecnica pos-upload via `app/run_cleide_audit_temp_table.py`
3. leitura do estado em `GET /api/cleide-auditoria/documents/status`
4. revisao humana da `temp_table` em `POST /api/cleide-auditoria/temp-table/save`
5. etapa fiscal opcional dentro da revisao da `temp_table`
6. coverage opcional em `POST /api/cleide-auditoria/coverage/upload`
7. download do template do lote auditado em `GET /api/cleide-auditoria/audit-template`
8. upload do lote auditado em `POST /api/cleide-auditoria/audit/upload`
9. processamento da auditoria em `POST /api/cleide-auditoria/audit/run`
10. correcoes assistidas em:
- `POST /api/cleide-auditoria/audit/correction/preview`
- `POST /api/cleide-auditoria/audit/correction/apply`
- `POST /api/cleide-auditoria/audit/correction/undo`
11. chat contextual em `POST /api/cleide-auditoria/chat`

### Regras de arquitetura

- a `temp_table` e artefato temporario, descartavel e governado pelo dominio Cleiton
- a Cleide nao e camada de persistencia definitiva da `temp_table`
- o chat da Cleide consulta contexto documental, mas nao cria, valida nem gerencia o ciclo de vida da `temp_table`
- a extracao tecnica permanece separada do chat conversacional
- a revisao humana nao e checklist fixo de chat, entrevista rigida nem validacao final feita pela IA

### Contrato atual da `temp_table`

Estados tecnicos realmente usados pelo backend:

- `processing`
- `awaiting_validation`
- `validated`
- `needs_review`
- `failed`
- `expired`
- `discarded`

Comportamentos confirmados no codigo e nos testes:

- a `temp_table` fica vinculada aos documentos ativos da sessao
- remocao, limpeza ou troca de documentos fonte pode invalidar o artefato anterior
- o backend preserva coverage e lote auditado quando a extracao e reaplicada sobre o mesmo artefato
- uma revisao humana ja salva pode impedir sobrescrita automatica da extracao sobre o mesmo conjunto de documentos

### Revisao humana e dados auditados

- a edicao comeca em modo governado e salva pelo endpoint dedicado
- a revisao pode alterar tabelas de frete, taxas acessorias, coverage e configuracao fiscal
- o arquivo auditado usa template XLSX oficial em `app/protected_files/templates/template_cleide_auditoria_frete.xlsx`
- o arquivo auditado possui limite de linhas configuravel via `cleide_audit_cfg_audited_file_max_rows`, capado pelo teto global do Cleiton
- alteracoes de regra tarifaria ou configuracao fiscal apos auditoria podem marcar o lote como `needs_reprocess`

### Regras de leitura e auditoria

O codigo atual cobre, entre outros pontos:

- faixas de peso e excesso por kg
- frete valor sobre NF
- pedagio por fracao de peso
- taxas acessorias configuradas por base de calculo
- mapeamento por UF, cidade e regiao de frete
- fallback de cidade quando cobertura nao fecha a regra primaria
- diagnosticos de `pricing_dimension_mismatch`, ausencia de cobertura, ausencia de regra tarifaria e valores invalidos
- calculo fiscal com ICMS e ISS quando a configuracao fiscal esta ativa

### BI executivo da auditoria

O BI executivo dentro de `/auditoria-frete` trabalha hoje com 4 graficos:

- Impacto Financeiro por Transportadora
- Impacto Financeiro por UF Destino
- Evolucao do Impacto Financeiro no Periodo
- Pareto do Valor Cobrado a Mais

Regras atuais do frontend:

- o dashboard usa dataset sanitizado de `audit_bi`
- o filtro e feito no frontend em nivel de linha
- os graficos trabalham com impacto absoluto e nao com o contrato antigo de 7 graficos
- referencias a `UF origem`, `Volume por Transportadora`, `Pareto por UF`, `Divergencia Financeira por UF Destino` e `Evolucao da Divergencia no Periodo` nao representam o contrato atual

### Modulos centrais

- `app/cleide_audit_routes.py`: API oficial da auditoria
- `app/cleide_audit_doc_service.py`: sessao documental, store temporario, `temp_table`, coverage, lote auditado, calculo da auditoria e payloads publicos
- `app/run_cleide_audit_temp_table.py`: extracao tecnica pos-upload, fallback de modelo e parsing seguro de JSON
- `app/cleide_audit_prompt.py`: prompt tecnico da extracao e prompt conversacional
- `app/cleide_audit_correction_service.py`: preview, apply e undo de correcoes assistidas
- `app/cleide_audit_doc_context.py`: contexto documental do chat da auditoria
- `app/static/js/cleide_auditoria.js`: experiencia completa de `/auditoria-frete`
- `app/templates/cleide_auditoria.html`: shell visual, modal da `temp_table` e secao do BI executivo

## Admin

### `/admin/agentes/cleide`

Existem dois blocos independentes:

- configuracao do BI estrutural da Cleide (`cleide_cfg_*`)
- configuracao da Cleide Auditoria (`cleide_audit_cfg_*`)

Campos reais da Cleide Auditoria:

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

As bases de calculo administrativas sao persistidas em `ConfigRegras` e orientam classificacao e calculo de taxas como percentual sobre NF, valor fixo por CTe/documento, por kg e por fracao de 100kg.

## Governanca, observabilidade e seguranca

- a autorizacao operacional central vem de `app/services/cleiton_operacao_autorizacao_service.py`
- a observabilidade de chamadas LLM persiste `IaConsumoEvento`
- o processamento tecnico nao-LLM persiste `ProcessingEvent`
- onboarding discovery continua separado do consumo operacional por franquia
- os limites documentais efetivos da Cleide Auditoria respeitam o teto global definido em `app/services/cleiton_doc_config_service.py`
- o runtime da auditoria nao deve ser documentado com metricas inexistentes por clique, grafico ou detalhe da UI

## Deploy e operacao

- homolog: branch versionada no Render `homolog`, com `autoDeploy: true`
- producao: servico versionado no Render configurado para `main`, com `autoDeploy: false`
- existe branch `producao` no repositório com o commit promovido `6efa2e2`; confirmar no painel qual branch o servico de producao realmente consumira antes de promover
- o deploy de producao e manual na configuracao versionada atual
- validar schema e health checks antes de publicar
- nao publicar `.db`, `__pycache__`, `.pytest_cache`, caches ou temporarios locais

## Testes

Comandos reais de coleta usados nesta auditoria documental:

```powershell
.\.venv\Scripts\python.exe -m pytest --collect-only -q tests/test_cleide_audit_correction_service.py tests/test_cleide_admin_routes.py tests/test_cleide_audit_chat_routes.py tests/test_cleide_audit_config_service.py tests/test_cleide_audit_doc_context.py tests/test_cleide_audit_doc_routes.py tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py
.\.venv\Scripts\python.exe -m pytest --collect-only -q
```

Coleta atual confirmada:

- suite especifica da auditoria Cleide: `804 tests collected`
- suite completa do repositório: `1660 tests collected`

Arquivos de teste usados como fonte primaria desta auditoria:

- `tests/test_cleide_audit_doc_routes.py`
- `tests/test_cleide_audit_chat_routes.py`
- `tests/test_cleide_audit_temp_table.py`
- `tests/test_cleide_auditoria_page.py`
- `tests/test_cleide_admin_routes.py`
- `tests/test_cleide_audit_config_service.py`
- `tests/test_cleide_audit_doc_context.py`
