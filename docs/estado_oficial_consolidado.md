# Estado Oficial Consolidado

Referência consolidada em 2026-07-29, auditada diretamente no código da branch `homolog`.

## Base desta consolidação

- branch local auditada: `homolog`;
- working tree auditado: limpo;
- upstream auditado: `origin/homolog`;
- relação auditada com o upstream: sem diferença de conteúdo no checkout local (`0  0` em `git rev-list --left-right --count HEAD...origin/homolog`);
- commit atual auditado: `81d36aa` - `feat: aprimora jornada e analytics do AgenteCompara`;
- commit equivalente informado em produção por `cherry-pick`: `6b0672e` - `feat: aprimora jornada e analytics do AgenteCompara`;
- esta consolidação usa como fonte principal o código atual do repositório, os testes do domínio e a infraestrutura versionada (`render.yaml`, `start.sh`, migrations e `.gitignore`);
- quando há divergência entre processo operacional informado e arquivos versionados, a divergência é registrada explicitamente.

## Visão oficial do projeto

- `app/web.py` continua como aplicação Flask monolítica que registra as áreas de Cleiton, Júlia, Roberto, Cleide e AgenteCompara.
- O banco oficial do sistema segue sendo PostgreSQL via `DATABASE_URL`, com schema governado por Alembic em `migrations/versions/`.
- O projeto possui duas trilhas de persistência distintas:
  - persistência transacional em banco;
  - persistência técnica temporária em JSON sob `app/cleiton_doc_tmp/`, usada para documentos, `temp_table`, lotes e resultados comparativos fora da sessão.
- O AgenteCompara implementa um fluxo multitabela com estado, revisão humana, configuração fiscal global, coverage opcional, arquivo operacional, cálculo comparativo, storage dedicado de resultado, analytics leve de comparação e observabilidade própria.

## AgenteCompara: arquitetura atual

### Superfícies e isolamento

- página: `/agente-compara`;
- template principal: `app/templates/agente_compara.html`;
- frontend: `app/static/js/agente_compara.js`;
- APIs: namespace `/api/agente-compara/*`;
- estado da comparação: sessão Flask em `agente_compara_comparison_state`;
- documentos do fluxo: session keys `agente_compara_*`;
- isolamento confirmado em código e testes frente a Cleide e Júlia:
  - session keys dedicadas;
  - `comparison_id`;
  - `table_id`;
  - `temp_table_id`;
  - `flow_type`;
  - eventos de processamento e billing próprios.

### Jornada oficial

O fluxo implementado hoje é:

1. `POST /api/agente-compara/comparison/start`
2. preparação da tabela 1
3. preparação da tabela 2
4. decisão sobre terceira tabela opcional
5. preparação da tabela 3, se escolhida
6. etapa global de impostos
7. etapa opcional de cidades atendidas
8. upload do arquivo operacional para comparação
9. revisão final da configuração
10. confirmação explícita e cálculo comparativo multitabela
11. leitura do resultado consolidado e analytics comparativo

### Estados oficiais da comparação

Status globais confirmados em `app/agente_compara_comparison_state.py`:

- `preparing_tables`
- `tables_ready`
- `configuration_ready`
- `calculation_running`
- `calculation_ready`
- `calculation_failed`

Etapas (`current_step`) confirmadas:

- `PREPARE_TABLE_1`
- `PREPARE_TABLE_2`
- `ASK_TABLE_3`
- `PREPARE_TABLE_3`
- `TABLES_READY`
- `TAXES`
- `COVERAGE`
- `CALCULATION_FILE`
- `CONFIGURATION_READY`
- `CALCULATION_RUNNING`
- `CALCULATION_READY`
- `CALCULATION_FAILED`
- `ANALYSIS`

Status por tabela:

- `empty`
- `locked`
- `processing`
- `needs_review`
- `confirmed`
- `failed`
- `discarded`

### Duas tabelas obrigatórias e terceira opcional

- slots 1 e 2 são obrigatórios;
- slot 3 é opcional;
- o slot 2 nasce bloqueado e só é liberado após a confirmação da tabela 1;
- após a confirmação da tabela 2, o fluxo entra em `ASK_TABLE_3`;
- `TABLES_READY` só é alcançado quando as tabelas obrigatórias já estão confirmadas e, se a terceira foi escolhida, ela também está confirmada;
- `primary_temp_table_id` aponta para a primeira `temp_table` confirmada e é usado como compatibilidade legada e para localizar o arquivo operacional compartilhado; ele não representa sozinho o conjunto comparativo;
- se existir slot 3, mas ele não estiver confirmado, o cálculo segue com 2 tabelas.

### Identidade, escopo e proteção contra mistura de contexto

O fluxo protege escopo com identidade composta:

- `comparison_id`
- `table_id`
- `slot`

Regras confirmadas:

- `comparison/start` é idempotente por sessão;
- `resolve_table_identity` não aceita usar `comparison_id` ou `table_id` de outra sessão;
- `documents/status`, upload, limpeza, save e reset rejeitam escopos incompatíveis;
- `table_id` e `slot` divergentes geram conflito de escopo;
- não existe comparação compartilhada entre usuários ou sessões;
- o resultado completo do cálculo não fica na sessão.

## AgenteCompara: documentos, temp_table e revisão

### Upload documental

Endpoint:

- `POST /api/agente-compara/documents/upload`

Comportamento atual:

- exige autenticação;
- exige autorização via `avaliar_autorizacao_operacao_por_franquia`;
- exige identificação de `carrier_name` antes do envio;
- aceita `comparison_id`, `table_id` e `slot`;
- registra documento no trilho técnico compartilhado do Cleiton, mas com namespace do AgenteCompara;
- dispara extração técnica da `temp_table` após o upload;
- devolve `document`, `session`, `allowed_formats`, `calculation_bases`, `temp_table` e `comparison`.

### Extração técnica e `temp_table`

Arquivos centrais:

- `app/run_agente_compara_temp_table.py`
- `app/agente_compara_doc_service.py`

Comportamento atual:

- a extração técnica usa Gemini quando configurado;
- a extração é idempotente por conjunto de documentos + `comparison_id` + `table_id`;
- resposta inválida do modelo não quebra o upload: a `temp_table` pode permanecer com erro ou exigir revisão;
- quando há dados parciais úteis, o backend força `needs_review`;
- revisão humana preserva artefatos e impede overwrite automático do mesmo conjunto de origem.

Status de `temp_table` confirmados:

- `processing`
- `awaiting_validation`
- `validated`
- `needs_review`
- `failed`
- `expired`
- `discarded`

### Edição e avanço

Endpoint principal:

- `POST /api/agente-compara/temp-table/save`

A revisão permite:

- salvar rascunho;
- salvar e avançar;
- avançar para coverage;
- atualizar apenas `carrier_name`.

O frontend implementa:

- modal obrigatório para identificar a transportadora antes do upload;
- edição manual dos dados extraídos;
- abas de frete, impostos, coverage e arquivo operacional;
- reinício da jornada;
- revisão final por transportadora na etapa `CONFIGURATION_READY`.

## Impostos, coverage e arquivo operacional

### Configuração fiscal global

Endpoint:

- `POST /api/agente-compara/comparison/taxes`

Regras atuais:

- a configuração é global da comparação, não por linha;
- `include_taxes=false` é permitido e produz status fiscal `no_taxes`;
- `include_taxes=true` exige configuração válida;
- quando há impostos, é obrigatório selecionar pelo menos uma transportadora em `selected_table_ids`;
- a configuração é salva no estado da comparação e reaproveitada no cálculo;
- tabelas não selecionadas recebem cálculo efetivo sem impostos no motor unitário.

Metodologia fiscal confirmada:

- `tax_calculation_version = agente_compara_tax_v2`
- modo fiscal atual: `inside`
- há suporte a ICMS e ISS conforme configuração global;
- a validação usa UF de origem, cidade de origem e UFs de destino.

### Coverage opcional

Endpoint:

- `POST /api/agente-compara/coverage/upload`

Colunas oficiais:

- `UF destino`
- `Cidade destino`
- `Região de frete`

Observações:

- coverage é opcional;
- o frontend permite pular a etapa;
- se a regra tarifária depender de região e o coverage não existir, o cálculo pode retornar status de falta ou ambiguidade de mapeamento.

### Arquivo operacional para comparação

Template oficial:

- `app/protected_files/templates/template_agente_compara.xlsx`

Download:

- `GET /api/agente-compara/audit-template`

Contrato atual do arquivo de entrada, confirmado em `app/agente_compara_doc_service.py`:

- versão de schema: `agente_compara_input_v1`
- aba oficial: `Modelo AgenteCompara`
- colunas obrigatórias: `numero_documento`, `cidade_destino`, `uf_destino`, `peso`
- colunas opcionais: `cidade_origem`, `uf_origem`, `valor_nf`, `modal`, `data_emissao`
- colunas legadas toleradas e ignoradas como base do novo lote: `transportadora`, `data_entrega`, `valor_frete`

Ponto importante:

- a transportadora do cálculo não vem da planilha linha a linha;
- ela vem do `carrier_name` associado a cada tabela confirmada da comparação.

## Cálculo comparativo

### Estrutura do cálculo

Arquivos centrais:

- `app/agente_compara_calculation_service.py`
- `app/agente_compara_comparison_calculation_service.py`
- `app/agente_compara_calculation_execution_service.py`

Camadas:

- motor unitário: calcula uma tabela por vez;
- orquestrador multitabela: executa 2 ou 3 tabelas confirmadas e consolida por `row_index`;
- serviço de execução: controla fingerprint, lock, idempotência, storage, analytics liberado e billing.

### Regras de cálculo confirmadas

O motor atual suporta e expõe componentes públicos para:

- faixas de peso;
- frete por peso;
- frete por kg;
- frete por tonelada, quando mapeado pelas bases de cálculo;
- excedente;
- pedágio;
- percentuais sobre valor da nota;
- taxas acessórias configuradas;
- mínimos vinculados a taxa principal;
- ICMS;
- ISS;
- subtotal antes de impostos;
- total final calculado.

O resultado unitário pode produzir os status:

- `calculated`
- `missing_coverage_mapping`
- `ambiguous_coverage_mapping`
- `missing_freight_rule`
- `invalid_weight`
- `invalid_invoice_value`
- `unsupported_pricing_model`

### Resultado do cálculo

Endpoint de execução:

- `POST /api/agente-compara/comparison/calculate`

Endpoint de leitura:

- `GET /api/agente-compara/comparison/calculation?comparison_id=...`

Contrato público atual:

- o start exige `execution_id`;
- há proteção explícita contra clique duplo no frontend;
- replays idempotentes de mesma configuração retornam `200`;
- tentativas com etapa inválida, configuração incompleta ou identidade incompatível retornam erro de negócio sem mutação indevida do estado;
- o resultado é comparativo por `row_index`;
- cada linha consolida `table_results` por `table_id`;
- cada célula por transportadora expõe `calculated_freight`, `status`, `error`, `components` e `evidence`;
- o payload público e o analytics derivado não expõem campos proibidos como `charged_freight`, `expected_freight`, `difference`, `winner`, `ranking` e `recommendation`.

### Analytics comparativo

Arquivo central:

- `app/agente_compara_comparison_analytics_service.py`

Responsabilidade real:

- recebe apenas o resultado comparativo já validado;
- não persiste estado;
- não chama billing, Gemini nem serviços externos;
- não muta o resultado de entrada;
- produz payload serializável com `schema_version`, `comparison_id`, `table_count`, `row_count`, `global_summary` e `tables`.

Métricas confirmadas:

- contagem de documentos;
- peso total;
- valor total de NF, quando disponível;
- período inicial/final, quando disponível;
- total de células, células calculadas, células com erro e cobertura global;
- por tabela: total calculado, média, total de peso processado, frete por kg, quantidade de linhas calculadas/com erro, cobertura e contagem de rotas/UFs/cidades.

Limitações confirmadas:

- não calcula vencedor, ranking, recomendação ou economia;
- depende de `schema_version=1`, `table_count` de 2 ou 3 e `comparative_rows` consistentes;
- quando não há dados completos, retorna nulos ou percentuais coerentes, em vez de inferir valores artificiais.

### Storage do resultado

Arquivo central:

- `app/agente_compara_calculation_result_storage.py`

Comportamento atual:

- o payload completo do cálculo é gravado fora da sessão;
- storage físico: subdiretório `agente_compara_calc` dentro de `app/cleiton_doc_tmp/`;
- gravação atômica;
- checksum SHA-256;
- chave estável por `comparison_id` + fingerprint;
- leitura valida identidade e integridade;
- resultado antigo pode ser limpo quando novo resultado válido o substitui.

### Lock, concorrência e idempotência

Arquivo central:

- `app/agente_compara_calculation_lock.py`

Proteções confirmadas:

- lock exclusivo por `comparison_id`;
- lock de sistema operacional em arquivo;
- mutex in-process adicional;
- timeout padrão de 8 segundos;
- erro público de conflito quando já há cálculo em andamento;
- `execution_id` obrigatório no start do cálculo;
- fingerprint da configuração impede replay com insumo diferente;
- reexecução com mesma configuração retorna replay idempotente;
- mudança de entrada durante a execução invalida o resultado e força nova tentativa;
- o cálculo não recalcula silenciosamente quando o storage esperado some ou fica corrompido.

### Billing e liberação do resultado

O fluxo de cálculo só libera `result` publicamente quando `billing_status=applied`.

Estados confirmados:

- `not_started`
- `pending`
- `applied`
- `failed`

Efeito prático:

- cálculo matemático pode estar pronto e o endpoint ainda responder sem `result` se o billing estiver pendente;
- `GET /comparison/calculation` continua sendo o caminho oficial para acompanhar esse estado.

## APIs confirmadas do AgenteCompara

### Documentos e comparação

- `POST /api/agente-compara/documents/upload`
- `GET /api/agente-compara/documents/status`
- `DELETE /api/agente-compara/documents/<doc_id>`
- `POST /api/agente-compara/documents/clear`
- `POST /api/agente-compara/comparison/start`
- `POST /api/agente-compara/comparison/reset`
- `POST /api/agente-compara/comparison/proceed-two-tables`
- `POST /api/agente-compara/comparison/add-third-table`
- `POST /api/agente-compara/comparison/set-active-table`
- `POST /api/agente-compara/comparison/taxes`
- `POST /api/agente-compara/comparison/calculate`
- `GET /api/agente-compara/comparison/calculation`
- `POST /api/agente-compara/temp-table/save`

### Coverage, arquivo, correções e chat

- `POST /api/agente-compara/coverage/upload`
- `GET /api/agente-compara/audit-template`
- `POST /api/agente-compara/audit/upload`
- `POST /api/agente-compara/audit/run`
- `POST /api/agente-compara/audit/correction/preview`
- `POST /api/agente-compara/audit/correction/apply`
- `POST /api/agente-compara/audit/correction/undo`
- `POST /api/agente-compara/chat`
- `POST /api/agente-compara/audit-chat/unlock`
- `POST /api/agente-compara/audit-chat`

### Códigos HTTP de negócio recorrentes

- `200` sucesso e replays idempotentes;
- `400` payload inválido ou regra de negócio obrigatória não atendida;
- `401` autenticação ausente;
- `403` usuário sem permissão de franquia ou escopo bloqueado;
- `404` comparação, tabela, `temp_table` ou lote inexistente/expirado;
- `409` conflito de estado, escopo, etapa, lock ou execução em andamento;
- `413` payload ou arquivo acima do limite;
- `500` falha inesperada;
- `503` indisponibilidade de serviço de IA quando aplicável.

## Interface atual do AgenteCompara

Comportamentos confirmados em `app/static/js/agente_compara.js`, `app/templates/agente_compara.html` e testes de UI:

- abertura da jornada via botão de upload;
- modal obrigatório para identificar `carrier_name`;
- prevenção de múltiplos envios e de múltiplos disparos de cálculo;
- bloqueios visuais durante preparação, upload, cálculo e regularização;
- cards/listagem de documentos anexados por sessão;
- botão visível para reiniciar comparação;
- polling de `temp_table` e sincronização com `documents/status`;
- modal de revisão com modo edição;
- abas de frete, impostos, coverage, arquivo operacional e resultados;
- confirmação explícita antes de processar os cálculos;
- mensagens de estado para cálculo em andamento, billing pendente, falha de regularização e resultado obsoleto;
- resumo comparativo, filtros, paginação e gráficos no bloco de resultados;
- BI executivo do lote auditado no próprio template;
- `audit-chat` liberado por `unlock` depois do bundle analítico;
- linguagem neutra sem vencedor, ranking ou recomendação automática.

## Observabilidade, métricas e isolamento frente a outros agentes

### Métricas e eventos

Arquivos centrais:

- `app/services/ia_metrics_service.py`
- testes `tests/test_agente_compara_billing_and_metrics.py`
- testes `tests/test_ia_metrics_service.py`

Eventos de processamento consolidados do AgenteCompara:

- `agente_compara_coverage_upload`
- `agente_compara_batch_upload`
- `agente_compara_batch_processed`
- `agente_compara_comparison_calculation`

Observação importante:

- reprocessamentos e cálculo comparativo entram no bloco de eventos do agente, mas não devem ser lidos como duplicação automática de linhas faturadas do upload original.

### Relação com Cleide e Júlia

- AgenteCompara reutiliza infraestrutura documental e de governança do Cleiton;
- não usa as session keys da Júlia;
- não usa o namespace de documentos da Cleide;
- não mistura `flow_type`, métricas nem billing da Cleide;
- compartilha conceitos estruturais com Cleide, mas mantém rotas, eventos e chaves de domínio próprios.

## Banco e migrations

Estado confirmado no repositório:

- PostgreSQL é o banco oficial quando configurado via `DATABASE_URL`;
- Alembic/Flask-Migrate governam o schema;
- nenhuma migration nova foi adicionada por esta entrega do AgenteCompara;
- nenhuma tabela nova foi criada nesta entrega;
- nenhuma coluna nova foi criada nesta entrega;
- a pasta `migrations/` não recebeu alteração funcional desta entrega;
- a cadeia Alembic versionada existente permanece linear até `r2s3t4u5v6w7`.

Aplicação das migrations:

- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn;
- isso vale para o boot versionado no Render;
- a documentação correta é: esta entrega não alterou schema, mas o sistema continua dependendo das migrations versionadas no boot.

Leitura operacional importante:

- arquivos `.db`, `.sqlite`, `.sqlite3` e JSON locais ignorados não fazem parte do banco oficial do deploy;
- divergências antigas identificadas em `flask db check` para `cleiton_billing_apropriacao`, `franquia` e `multiuser_franquia_codigo` são preexistentes e exigem investigação separada;
- `flask db migrate` não deve ser usado automaticamente para “corrigir” essas divergências sem investigação.

## Deploy, homologação e produção

### O que o código versionado comprova

- `start.sh` infere `APP_ENV` quando necessário;
- `render.yaml` versionado declara homolog com branch `homolog` e `autoDeploy: true`;
- `render.yaml` versionado declara produção com branch `main` e `autoDeploy: false`;
- o boot versionado roda migrations antes da aplicação subir.

### O que o processo operacional informado registra

- homologação pela branch `homolog`;
- promoção para produção por `cherry-pick`;
- equivalência funcional entre `81d36aa` em homolog e `6b0672e` em produção;
- deploy manual de produção no Render.

### Divergência operacional vigente

Hoje existe divergência entre:

- processo informado: produção em `producao`;
- arquivo versionado `render.yaml`: produção em `main`.

Essa divergência não foi resolvida no código desta atividade e deve continuar tratada como item operacional a validar antes de qualquer novo deploy.

### Health checks

O código expõe:

- `/health/liveness`
- `/health/readiness`

O `render.yaml` versionado ainda aponta:

- `healthCheckPath: /health`

Isso também permanece como divergência operacional vigente.

## Arquivos persistentes, temporários e ignorados

Confirmado em `.gitignore`:

- `app/indices.json` é artefato local ignorado;
- `cache_*.json` é ignorado, incluindo `cache_noticias.json`;
- `scripts/security/rotation-report*.json` é ignorado;
- `*.db`, `*.sqlite` e `*.sqlite3` são ignorados;
- `app/cleiton_doc_tmp/` é ignorado;
- templates oficiais `.xlsx` do produto são exceções explícitas e permanecem versionados.

Leitura oficial:

- esses arquivos locais podem existir no ambiente de trabalho;
- não fazem parte do deploy versionado;
- não devem ser documentados como dependências obrigatórias de produção.

## Testes e cobertura efetivamente comprovada

Suites diretamente relacionadas e verificadas:

- `tests/test_agente_compara_comparison_start.py`
- `tests/test_agente_compara_comparison_journey.py`
- `tests/test_agente_compara_multitable.py`
- `tests/test_agente_compara_taxes_multicarrier.py`
- `tests/test_agente_compara_configuration_review.py`
- `tests/test_agente_compara_comparison_file_contract.py`
- `tests/test_agente_compara_temp_table_save.py`
- `tests/test_agente_compara_doc_upload.py`
- `tests/test_agente_compara_calculation_execution.py`
- `tests/test_agente_compara_calculation_lock.py`
- `tests/test_agente_compara_calculation_result_storage.py`
- `tests/test_agente_compara_calculation_storage_integration.py`
- `tests/test_agente_compara_comparison_analytics.py`
- `tests/test_agente_compara_calculation_journey_ui.py`
- `tests/test_agente_compara_billing_and_metrics.py`
- `tests/test_agente_compara_isolation.py`

Garantias efetivamente cobertas:

- start/reset e isolamento de sessão;
- jornada multitabela com 2 obrigatórias e 3ª opcional;
- regras de impostos globais e coverage;
- contrato do arquivo operacional;
- confirmação, idempotência e conflitos de `execution_id`;
- lock, concorrência e storage do resultado;
- integração cálculo + billing + leitura pública;
- analytics comparativo, determinismo, neutralidade e ausência de campos proibidos;
- contratos de UI para estados, mensagens, filtros, paginação e gráficos.

Aspectos que a documentação não deve vender como cobertos sem ressalva:

- não foi comprovada aqui cobertura exaustiva de todas as rotas não relacionadas ao AgenteCompara;
- esta atividade não reexecutou a suíte inteira, apenas auditou o escopo de testes existente no repositório.

## Limitações e decisões arquiteturais atuais

- o estado da comparação é mantido em sessão Flask, não em banco nem Redis;
- o cálculo comparativo usa lock por arquivo, não fila externa;
- o resultado completo do cálculo vive em storage temporário dedicado, não em tabela de banco;
- a terceira tabela continua opcional;
- coverage permanece opcional;
- o chat documental e o chat analítico são fluxos separados;
- o analytics comparativo não expõe ranking, vencedor ou recomendação final;
- a liberação pública do resultado depende da conclusão do billing operacional;
- divergências de branch de produção e health check ainda não foram corrigidas no código versionado.