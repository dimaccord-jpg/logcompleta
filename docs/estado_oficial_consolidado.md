# Estado Oficial Consolidado

Referência consolidada em 2026-07-28, auditada diretamente no código da branch `homolog`.

## Base desta consolidação

- branch local auditada: `homolog`;
- commits operacionais informados para a entrega recente do AgenteCompara:
  - `a6fccdc` - `feat: conclui melhorias e calculos do AgenteCompara`
  - `2653ca2` - `merge: promove melhorias e calculos do AgenteCompara para producao`
- esta consolidação usa como fonte principal o código atual do repositório, os testes do domínio e a infraestrutura versionada (`render.yaml`, `start.sh`, migrations e `.gitignore`);
- quando há divergência entre processo operacional informado e arquivos versionados, a divergência é registrada explicitamente.

## Visão oficial do projeto

- `app/web.py` continua como aplicação Flask monolítica que registra as áreas de Cleiton, Júlia, Roberto, Cleide e AgenteCompara.
- O banco oficial do sistema segue sendo PostgreSQL via `DATABASE_URL`, com schema governado por Alembic em `migrations/versions/`.
- O projeto possui duas trilhas de persistência distintas:
  - persistência transacional em banco;
  - persistência técnica temporária em JSON sob `app/cleiton_doc_tmp/`, usada para documentos, `temp_table`, lotes e resultados auxiliares fora da sessão.
- O AgenteCompara não é um comparador simples de planilhas. Ele implementa um fluxo multitabela com estado, revisão humana, configuração fiscal global, coverage opcional, arquivo operacional, cálculo comparativo, storage dedicado de resultado e observabilidade própria.

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
10. cálculo comparativo multitabela
11. leitura do resultado consolidado

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
- a comparação só avança para parâmetros globais depois que as duas tabelas obrigatórias estiverem confirmadas;
- se a terceira tabela for confirmada, ela entra no cálculo;
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
- aceita `comparison_id`, `table_id`, `slot` e `carrier_name`;
- registra documento no trilho técnico compartilhado do Cleiton, mas com namespace do AgenteCompara;
- dispara extração técnica da `temp_table` após o upload;
- devolve:
  - `document`
  - `session`
  - `allowed_formats`
  - `calculation_bases`
  - `temp_table`
  - `comparison`

### Extração técnica e `temp_table`

Arquivos centrais:

- `app/run_agente_compara_temp_table.py`
- `app/agente_compara_doc_service.py`

Comportamento atual:

- a extração técnica usa Gemini quando configurado;
- o timeout da extração é próprio do fluxo (`AGENTE_COMPARA_TEMP_TABLE_TIMEOUT_MS`) e não herda o timeout genérico de chat;
- a extração é idempotente por conjunto de documentos + `comparison_id` + `table_id`;
- resposta inválida do modelo não quebra o upload: a `temp_table` é marcada como `failed`;
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

O frontend implementa modal com:

- identificação obrigatória da transportadora antes do upload;
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
- colunas obrigatórias:
  - `numero_documento`
  - `cidade_destino`
  - `uf_destino`
  - `peso`
- colunas opcionais:
  - `cidade_origem`
  - `uf_origem`
  - `valor_nf`
  - `modal`
  - `data_emissao`
- colunas legadas toleradas e ignoradas como base do novo lote:
  - `transportadora`
  - `data_entrega`
  - `valor_frete`

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
- serviço de execução: controla fingerprint, lock, idempotência, storage e billing.

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

- o resultado é comparativo por `row_index`;
- cada linha consolida `table_results` por `table_id`;
- cada célula por transportadora expõe:
  - `calculated_freight`
  - `status`
  - `error`
  - `components`
  - `evidence`
- o payload público não expõe campos proibidos como:
  - `charged_freight`
  - `expected_freight`
  - `difference`
  - `winner`
  - `ranking`
  - `recommendation`

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

- `POST /api/agente-compara/comparison/start`
- `POST /api/agente-compara/comparison/reset`
- `POST /api/agente-compara/comparison/proceed-two-tables`
- `POST /api/agente-compara/comparison/add-third-table`
- `POST /api/agente-compara/comparison/set-active-table`
- `POST /api/agente-compara/documents/upload`
- `GET /api/agente-compara/documents/status`
- `DELETE /api/agente-compara/documents/<doc_id>`
- `POST /api/agente-compara/documents/clear`

### Revisão, impostos, coverage e arquivo

- `POST /api/agente-compara/temp-table/save`
- `POST /api/agente-compara/comparison/taxes`
- `POST /api/agente-compara/coverage/upload`
- `GET /api/agente-compara/audit-template`
- `POST /api/agente-compara/audit/upload`
- `POST /api/agente-compara/audit/run`

### Correções e chat

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

Comportamentos confirmados em `app/static/js/agente_compara.js` e `app/templates/agente_compara.html`:

- abertura da jornada via botão de upload;
- modal obrigatório para identificar `carrier_name`;
- cards/listagem de documentos anexados por sessão;
- botão visível para reiniciar comparação;
- polling de `temp_table` e sincronização com `documents/status`;
- modal de revisão com modo edição;
- abas de frete, impostos, coverage e arquivo operacional;
- etapa final de revisão por transportadora antes do cálculo;
- BI executivo no próprio fluxo;
- `audit-chat` liberado por `unlock` depois do bundle analítico;
- mensagens explícitas para processamento, arquivo ausente, conflito de etapa e reinício.

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

- nenhuma migration nova foi adicionada por esta entrega do AgenteCompara;
- nenhuma tabela nova foi criada nesta entrega;
- nenhuma coluna nova foi criada nesta entrega;
- a pasta `migrations/` não recebeu alteração funcional desta entrega;
- a cadeia Alembic versionada existente permanece linear até `r2s3t4u5v6w7`.

Aplicação das migrations:

- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn;
- isso vale para boot versionado no Render;
- a documentação correta é: esta entrega não alterou schema, mas o sistema continua dependendo das migrations versionadas no boot.

## Deploy, homologação e produção

### O que o código versionado comprova

- `start.sh` infere `APP_ENV` quando necessário;
- `render.yaml` versionado declara:
  - homolog com branch `homolog` e `autoDeploy: true`
  - produção com branch `main` e `autoDeploy: false`
- o boot versionado roda migrations antes da aplicação subir.

### O que o processo operacional informado registra

- homologação pela branch `homolog`;
- promoção por merge para `producao`;
- deploy de produção manual no Render.

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

## Limitações e decisões arquiteturais atuais

- o estado da comparação é mantido em sessão Flask, não em banco nem Redis;
- o cálculo comparativo usa lock por arquivo, não fila externa;
- o resultado completo do cálculo vive em storage temporário dedicado, não em tabela de banco;
- a terceira tabela continua opcional;
- coverage permanece opcional;
- o chat documental e o chat analítico são fluxos separados;
- o cálculo comparativo não expõe ranking ou recomendação final de vencedor no contrato público atual;
- a liberação pública do resultado depende da conclusão do billing operacional;
- divergências de branch de produção e health check ainda não foram corrigidas no código versionado.
