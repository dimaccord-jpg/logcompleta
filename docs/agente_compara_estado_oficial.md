# AgenteCompara — Estado Oficial

Referência auditada em 2026-09-04. O código atual é a fonte de verdade.

## Superfícies e isolamento

- página: `/agente-compara`;
- template principal: `app/templates/agente_compara.html`;
- frontend: `app/static/js/agente_compara.js`;
- APIs: namespace `/api/agente-compara/*`;
- estado da comparação: sessão Flask em `agente_compara_comparison_state`;
- documentos do fluxo: session keys `agente_compara_*`;
- isolamento confirmado em código e testes frente ao domínio técnico Cleide e à Julia por `comparison_id`, `table_id`, `temp_table_id`, `flow_type`, billing e eventos próprios.

## Jornada oficial

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

## Estados oficiais

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

## Regras de slots e identidade

- slots 1 e 2 são obrigatórios; slot 3 é opcional;
- o slot 2 nasce bloqueado e só é liberado após a confirmação da tabela 1;
- após a confirmação da tabela 2, o fluxo entra em `ASK_TABLE_3`;
- `TABLES_READY` só é alcançado quando as tabelas obrigatórias estão confirmadas e, se a terceira foi escolhida, ela também está confirmada;
- `primary_temp_table_id` aponta para a primeira `temp_table` confirmada como compatibilidade legada e referência do arquivo operacional compartilhado; ele não representa sozinho o conjunto comparativo;
- o fluxo protege escopo com identidade composta `comparison_id` + `table_id` + `slot`;
- `comparison/start` é idempotente por sessão;
- `resolve_table_identity` não aceita usar `comparison_id` ou `table_id` de outra sessão;
- `documents/status`, upload, limpeza, save e reset rejeitam escopos incompatíveis.

## Upload e `temp_table`

- `POST /api/agente-compara/documents/upload` exige autenticação, autorização, `carrier_name` e escopo válido de `comparison_id`, `table_id` e `slot`;
- o upload registra documento no trilho técnico compartilhado do Cleiton, mas com namespace do AgenteCompara, e dispara a extração técnica da `temp_table`;
- a extração usa Gemini quando configurado, é idempotente por conjunto de documentos + `comparison_id` + `table_id` e pode resultar em `processing`, `awaiting_validation`, `validated`, `needs_review`, `failed`, `expired` ou `discarded`;
- resposta inválida do modelo não quebra o upload: a `temp_table` pode permanecer com erro ou exigir revisão;
- quando há dados parciais úteis, o backend força `needs_review`;
- revisão humana preserva artefatos e impede overwrite automático do mesmo conjunto de origem.

## Edição, revisão e avanço

- `POST /api/agente-compara/temp-table/save` permite salvar rascunho, salvar e avançar, avançar para coverage e atualizar apenas `carrier_name`;
- o frontend usa modal obrigatório para identificar a transportadora, edição manual dos dados extraídos, abas de frete, impostos, coverage e arquivo operacional, além de `review_presentation` por taxa acessória com estados `resolved`, `blocking` e `informational`.

## Validação determinística antes do avanço

Arquivo central:

- `app/agente_compara_temp_table_validation_service.py`

Comportamento atual:

- a confirmação da tabela depende de `validation.can_confirm`;
- bloqueios retornam `blocking_issues` determinísticos e serializáveis;
- o backend devolve `TEMP_TABLE_HAS_BLOCKING_ISSUES` quando o usuário tenta salvar e avançar com pendências;
- alertas genéricos de leitura e `uncertain_fields` não bloqueiam sozinhos;
- bases de cálculo não mapeadas, valores ausentes ou inválidos, unidades incompatíveis, mínimos sem vínculo e regras extraídas não confirmadas bloqueiam o avanço;
- condições textuais não suportadas e operações não executáveis pelo motor também são barradas antes do cálculo.

## Configuração fiscal global

- `POST /api/agente-compara/comparison/taxes` salva configuração global da comparação;
- `include_taxes=false` é permitido e produz status fiscal `no_taxes`;
- `include_taxes=true` exige configuração válida;
- quando há impostos, é obrigatório selecionar ao menos uma transportadora em `selected_table_ids`;
- `tax_calculation_version = agente_compara_tax_v2` e o modo fiscal atual é `inside`;
- há suporte a ICMS e ISS conforme configuração global.

## Coverage opcional

- `POST /api/agente-compara/coverage/upload` usa as colunas `UF destino`, `Cidade destino` e `Região de frete`;
- coverage é opcional e pode ser pulado;
- quando a regra tarifária depende de região e o coverage não existe, o cálculo pode retornar `missing_coverage_mapping` ou `ambiguous_coverage_mapping`.

## Arquivo operacional para comparação

- template oficial: `app/protected_files/templates/template_agente_compara.xlsx`;
- download: `GET /api/agente-compara/audit-template`;
- contrato atual: schema `agente_compara_input_v1`, aba `Modelo AgenteCompara`, colunas obrigatórias `numero_documento`, `cidade_destino`, `uf_destino`, `peso` e opcionais `cidade_origem`, `uf_origem`, `valor_nf`, `modal`, `data_emissao`;
- colunas legadas `transportadora`, `data_entrega` e `valor_frete` são toleradas, mas não definem a transportadora do cálculo;
- a transportadora do cálculo vem do `carrier_name` associado a cada tabela confirmada.

## Cálculo comparativo

Arquivos centrais:

- `app/agente_compara_calculation_service.py`
- `app/agente_compara_comparison_calculation_service.py`
- `app/agente_compara_calculation_execution_service.py`

Camadas:

- motor unitário: calcula uma tabela por vez;
- orquestrador multitabela: executa 2 ou 3 tabelas confirmadas e consolida por `row_index`;
- serviço de execução: controla fingerprint, lock, idempotência, storage, analytics liberado e billing.

O motor atual suporta e expõe componentes públicos para faixas de peso, frete por peso, frete por kg, frete por tonelada quando mapeado, excedente, pedágio, percentuais sobre valor da nota, taxas acessórias configuradas, mínimos vinculados a taxa principal, ICMS, ISS, subtotal antes de impostos e total final calculado.

O motor não deve ser documentado como compatível com qualquer taxa acessória textual apenas porque ela aparece na revisão. Quando a operação ou a condição não é suportada, o fluxo bloqueia a confirmação ou marca a célula resultante como incompleta.

Status unitários possíveis:

- `calculated`
- `calculated_with_warnings`
- `incomplete`
- `missing_coverage_mapping`
- `ambiguous_coverage_mapping`
- `missing_freight_rule`
- `invalid_weight`
- `invalid_invoice_value`
- `unsupported_pricing_model`

## Contrato público do cálculo

- `POST /api/agente-compara/comparison/calculate` exige `execution_id` e protege contra clique duplo no frontend;
- replays idempotentes da mesma configuração retornam `200`;
- tentativas com etapa inválida, configuração incompleta ou identidade incompatível retornam erro sem mutação indevida do estado;
- `GET /api/agente-compara/comparison/calculation` é o caminho oficial de leitura e acompanhamento;
- `GET /api/agente-compara/comparison/calculation-memory` expõe a memória pública detalhada de uma célula já calculada, com proteção de escopo e limites técnicos;
- o resultado é comparativo por `row_index`, com `table_results` por `table_id`;
- cada célula expõe `calculated_freight`, `status`, `final_status`, `error`, `components`, `evidence`, `completeness`, `blocking_issues` e `calculation_memory`;
- o payload público e o analytics derivado não expõem `charged_freight`, `expected_freight`, `difference`, `winner`, `ranking` ou `recommendation`.

## Completeza, memória, storage e idempotência

Arquivos centrais:

- `app/agente_compara_calculation_completeness_service.py`
- `app/agente_compara_calculation_memory_service.py`
- `app/agente_compara_calculation_result_storage.py`
- `app/agente_compara_calculation_lock.py`

Responsabilidades reais:

- a completeza não recalcula o valor; ela classifica o resultado bruto do motor;
- componentes ignorados são classificados como benignos, avisos ou bloqueantes;
- células `incomplete` preservam `partial_value` e memória compatível com esse estado;
- a memória pública lista componentes aplicados, componentes ignorados, subtotal, impostos, evidências e total coerente com a célula;
- `not_calculated` produz diagnóstico estruturado sem inventar total;
- o contrato é determinístico, serializável e sem dependência de Flask, billing, Gemini ou Cleide;
- o payload completo do cálculo é gravado fora da sessão em `app/cleiton_doc_tmp/agente_compara_calc`, com gravação atômica, checksum SHA-256 e chave estável por `comparison_id` + fingerprint.

Proteções confirmadas:

- lock exclusivo por `comparison_id`;
- lock de sistema operacional em arquivo e mutex in-process adicional;
- timeout padrão de 8 segundos;
- erro público de conflito quando já há cálculo em andamento;
- fingerprint da configuração impede replay com insumo diferente;
- reexecução com mesma configuração retorna replay idempotente;
- mudança de semântica do motor ou da completeza invalida replay anterior por `calculation_algorithm_version`;
- mudança de entrada durante a execução invalida o resultado e força nova tentativa;
- o cálculo não recalcula silenciosamente quando o storage esperado some ou fica corrompido.

## Billing e liberação do resultado

- o fluxo só libera `result` publicamente quando `billing_status=applied`;
- estados confirmados: `not_started`, `pending`, `applied`, `failed`;
- o cálculo matemático pode estar pronto e o endpoint ainda responder sem `result` se o billing estiver pendente.

## Reset e invalidação do fluxo

- `POST /api/agente-compara/comparison/reset` remove a comparação atual, os `temp_table_id` vinculados e o cache de idempotência do save;
- o reset é idempotente e não recria comparação por efeito colateral;
- após reset, `GET /api/agente-compara/documents/status` continua vazio até novo start/upload;
- um novo upload após reset gera `comparison_id` novo;
- o reset preserva chaves de sessão de outros domínios, como o domínio técnico Cleide;
- limpar os documentos de um slot rebaixa a jornada para a etapa correspondente e invalida configuração global derivada quando necessário;
- mudanças em tabela, fiscalidade ou regra de preço podem marcar resultado ou batch como `stale`, exigindo nova execução ou nova revisão.

## APIs confirmadas do AgenteCompara

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
- `GET /api/agente-compara/comparison/calculation-memory`
- `POST /api/agente-compara/temp-table/save`
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
- `POST /api/agente-compara/comparison-chat`

## Interface atual

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
- dashboard comparativo oficial abaixo do chat e fora do modal, com 9 widgets hideable, preferências persistidas em `localStorage` e seção geográfica própria;
- modal de memória de cálculo por célula, inclusive para estados incompletos e diagnósticos bloqueantes;
- `audit-chat` liberado por `unlock` depois do bundle analítico;
- `comparison-chat` separado do `audit-chat`, bloqueado antes de READY, sem fetch pré-READY e disponível somente com `result` + `analytics` liberados;
- linguagem neutra sem vencedor, ranking ou recomendação automática.

## Limites e honestidade

- o sistema não deve ser documentado como marketplace de cotação;
- não envia concorrência ao mercado;
- não contrata transportadora;
- não decide automaticamente pelo usuário;
- regras não suportadas pelo motor não devem ser tratadas como cálculo definitivo.

## Banco, deploy e limites atuais

- PostgreSQL é o banco oficial quando configurado via `DATABASE_URL`;
- Alembic/Flask-Migrate governam o schema e a cadeia versionada permanece linear até `z0a1b2c3d4e5`;
- a migration da home adiciona `home_cta_experiment_event` e não altera o schema isolado do AgenteCompara;
- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn;
- `render.yaml` atual aponta homologação para a branch `homolog` e produção para a branch `producao`, ambos com `autoDeploy: true`;
- o Render usa `healthCheckPath: /health`; a aplicação também expõe `/health/liveness` e `/health/readiness`;
- divergências antigas identificadas em `flask db check` para `cleiton_billing_apropriacao`, `franquia` e `multiuser_franquia_codigo` são preexistentes e exigem investigação separada;
- Playwright é dependência de desenvolvimento (`requirements-dev.txt`) e não integra o runtime de produção.

## Testes e cobertura efetivamente comprovada

Suites diretamente relacionadas:

- `tests/test_agente_compara_comparison_start.py`
- `tests/test_agente_compara_comparison_journey.py`
- `tests/test_agente_compara_multitable.py`
- `tests/test_agente_compara_taxes_multicarrier.py`
- `tests/test_agente_compara_configuration_review.py`
- `tests/test_agente_compara_comparison_file_contract.py`
- `tests/test_agente_compara_temp_table_save.py`
- `tests/test_agente_compara_doc_upload.py`
- `tests/test_agente_compara_calculation_execution.py`
- `tests/test_agente_compara_calculation_completeness_service.py`
- `tests/test_agente_compara_calculation_memory_service.py`
- `tests/test_agente_compara_calculation_lock.py`
- `tests/test_agente_compara_calculation_result_storage.py`
- `tests/test_agente_compara_calculation_storage_integration.py`
- `tests/test_agente_compara_comparison_analytics.py`
- `tests/test_agente_compara_comparison_chat_context.py`
- `tests/test_agente_compara_comparison_chat_routes_ui.py`
- `tests/test_agente_compara_comparison_reset.py`
- `tests/test_agente_compara_calculation_journey_ui.py`
- `tests/test_agente_compara_billing_and_metrics.py`
- `tests/test_agente_compara_isolation.py`
- `tests/test_agente_compara_accessorial_review_presentation.py`
- `tests/test_agente_compara_review_memory_consistency.py`
- `tests/test_agente_compara_temp_table_validation_service.py`

Garantias efetivamente cobertas:

- start/reset e isolamento de sessão;
- jornada multitabela com 2 obrigatórias e 3ª opcional;
- reset completo e recomeço limpo da jornada;
- regras de impostos globais e coverage;
- contrato do arquivo operacional;
- validação bloqueante das taxas acessórias e apresentação pública da revisão;
- confirmação, idempotência e conflitos de `execution_id`;
- lock, concorrência e storage do resultado;
- gate de completeza e memória pública do cálculo;
- integração cálculo + billing + leitura pública;
- analytics comparativo, determinismo, neutralidade e ausência de campos proibidos;
- gate de disponibilidade, isolamento e contratos de UI do `comparison-chat`;
- contratos de UI para estados, mensagens, filtros, paginação e gráficos.
