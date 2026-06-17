# Estado Oficial Consolidado

Data de consolidacao: `2026-06-16`
Commit de referencia: `5f10f6d`

## Escopo promovido

Estado confirmado no codigo e na promocao:

- Copilot de discovery na Home publica
- Home logada consolidada como superficie operacional da Julia
- upload documental governado da Julia
- observabilidade de onboarding e operacional mantida
- configuracao documental no admin do Cleiton sem migration nova
- Cleide Auditoria com tabela temporaria estabilizada no fluxo pos-upload/status

## Estado oficial de ambiente

- `homolog` contem `c5a73e1`
- `producao` contem merge `5f10f6d`
- producao foi validada e aprovada apos deploy
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
- o chat consulta o contexto, mas nao deve recriar, alterar ou sobrescrever a tabela temporaria
- a interface exibe card clicavel da tabela temporaria no painel de anexos/documentos
- o modal da tabela temporaria e somente leitura

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

- `python -m pytest tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py`
- resultado: `109 passed, 2 warnings`
- `python -m pytest`
- resultado: `1124 passed, 36 warnings`
- warnings conhecidos de dependencia/uso legado, sem falha funcional
