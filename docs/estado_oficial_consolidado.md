# Estado Oficial Consolidado

Data de consolidacao: `2026-06-30`
Commit de referencia: `homolog@28904e4` e `producao@0afe528`

## Escopo promovido

Estado confirmado no codigo e na promocao:

- Copilot de discovery na Home publica
- Home logada consolidada como superficie operacional da Julia
- upload documental governado da Julia
- observabilidade de onboarding e operacional mantida
- configuracao documental no admin do Cleiton sem migration nova
- Cleide Auditoria com tabela temporaria estabilizada no fluxo pos-upload/status

## Estado oficial de ambiente

- `homolog` contem `28904e4`
- `origin/homolog` esta sincronizada com `homolog`
- `producao` contem `0afe528`
- producao foi validada e aprovada apos deploy
- apos o push, `origin/producao` ficou em `0afe528`
- o ambiente local voltou para `homolog`, limpo e sincronizado
- nao houve migration nova
- nao houve schema novo
- nao houve campos novos
- nao houve tabelas novas

## Superficies oficiais

- `/`: Home publica com Copilot
- `/`: Home logada com Julia operacional
- `/chat_julia?mode=operational`: acesso dedicado da Julia
- `/fretes`: Roberto
- `/cleide-bi-frete`: Cleide BI operacional
- `/auditoria-frete`: Cleide Auditoria documental
- `/feed`: editorial
- `/admin/dashboard`: admin

## Copilot da Home

- backend oficial: `POST /api/onboarding_discovery`
- reset oficial: `POST /api/onboarding_discovery/reset`
- limite anonimo por sessao: `5`
- CTA de login ao atingir o limite
- contexto so e preservado para Julia quando houver handoff `julia_operational`

## Julia

- endpoint oficial: `POST /api/chat_julia`
- exige login
- valida autorizacao operacional por franquia
- funciona na Home logada como superficie principal
- mantem a rota dedicada `/chat_julia?mode=operational`

## Fluxo documental

O fluxo documental atual da Julia:

- faz parte da experiencia operacional logada
- usa Cleiton como camada de governanca
- respeita autorizacao, plano/franquia, limites e seguranca

Tipos aceitos:

- `TXT`
- `XML`
- `CSV`
- `XLSX`
- `DOCX`
- `PDF`

Sobre PDF:

- tem tratamento governado proprio
- pode usar Gemini Files quando aplicavel
- nao deve simular leitura quando so houver placeholder ou contexto nao pronto

## Cleiton

Papel correto no estado atual:

- governanca operacional
- autorizacao por franquia
- observabilidade
- upload documental
- validacao e seguranca
- TTL, cleanup e sessao
- preparo de contexto e integracao com IA
- ownership operacional da tabela temporaria da Cleide Auditoria

## Cleide Auditoria

Estado promovido:

- upload documental e chat ativos em `/auditoria-frete`
- upload/documentos alimentam a tabela temporaria
- extracao tecnica pos-upload/status permanece separada do chat conversacional
- retorno de `temp_table` no upload quando disponivel
- retorno de `temp_table` no status documental
- revisao humana da `temp_table` disponivel em `POST /api/cleide-auditoria/temp-table/save`
- coverage complementar em `POST /api/cleide-auditoria/coverage/upload`
- template do lote auditado em `GET /api/cleide-auditoria/audit-template`
- upload do lote auditado em `POST /api/cleide-auditoria/audit/upload`
- processamento do lote auditado em `POST /api/cleide-auditoria/audit/run`
- o chat consulta o contexto, mas nao deve recriar, alterar ou sobrescrever a tabela temporaria
- a interface exibe card clicavel da tabela temporaria no painel de anexos/documentos
- o modal da tabela temporaria e somente leitura
- a validacao da `temp_table` e humana e governada, nao uma nova conversa de IA

Garantias:

- a tabela temporaria e descartavel e nao representa auditoria final
- a validacao humana continua obrigatoria
- nao houve migration, nova tabela de banco ou alteracao estrutural de schema
- nao houve banco local ou arquivo `.db` versionado
- o `operational_owner` da tabela temporaria e `cleiton`
- o ciclo de vida acompanha o TTL dos documentos da sessao

## Configuracao administrativa

O admin de Cleiton possui bloco documental governado em `agentes_cleiton`.

Persistencia:

- usa `ConfigRegras`
- nao adicionou migration
- nao criou tabela nova nesta entrega

## Banco e migrations

Esta entrega:

- nao adicionou arquivo em `migrations/versions`
- nao criou tabela para upload documental
- nao criou campo para a temp table da Cleide
- manteve o mecanismo existente de configuracao

## Git e temporarios

- `app/cleiton_doc_tmp/` permanece local, temporaria e ignorada no Git
- `app/cleiton_doc_tmp/` esta protegido pelo `.gitignore`
- `tt_*.json`, `.cleanup_meta.json` e outros `.json` dessa pasta nao devem ser versionados
- residuos `app/.tmp_repro_unit*` nao devem ser versionados

## Testes e validacao

Validacoes registradas para a promocao:

- `pytest tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py tests/test_cleide_admin_routes.py tests/test_cleide_audit_config_service.py tests/test_cleide_audit_doc_routes.py`
- resultado: `572 passed, 2 warnings` em aproximadamente `220.39s`
- warnings conhecidos: `DeprecationWarning` de `flask_session` filesystem e `DeprecationWarning` de `google genai` / `_UnionGenericAlias` em Python 3.14
- esses warnings nao foram tratados como falha funcional nem bloqueio da promocao

## Ponto de atencao operacional

- `homolog` e a branch oficial de homologacao
- `producao` e a branch funcional de producao usada no painel do Render
- `main` nao deve ser usada como destino operacional automatico de producao neste momento
- o `render.yaml` do repositorio ainda referencia `branch: main` no servico de producao
- manter a confirmacao do painel Render como etapa obrigatoria antes de novas promocoes
