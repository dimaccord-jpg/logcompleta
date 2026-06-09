# Estado Oficial Consolidado

Data de consolidacao: `2026-06-05`
Commit de referencia: `b5fc444`

## Escopo promovido

Estado confirmado no codigo e na promocao:

- Copilot de discovery na Home publica
- Home logada consolidada como superficie operacional da Julia
- upload documental governado da Julia
- estabilizacao do desempenho documental
- separacao preservada entre Copilot publico e Julia operacional
- observabilidade de onboarding e operacional mantida
- configuracao documental no admin do Cleiton sem migration nova

## Superficies oficiais

- `/`: Home publica com Copilot
- `/`: Home logada com Julia operacional
- `/chat_julia?mode=operational`: acesso dedicado da Julia
- `/fretes`: Roberto
- `/cleide-bi-frete`: Cleide BI operacional
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
- manteve o mecanismo existente de configuracao

## Testes e validacao

Testes relevantes da entrega:

- `tests/test_cleiton_doc_config_service.py`
- `tests/test_cleiton_doc_store.py`
- `tests/test_cleiton_doc_converters.py`
- `tests/test_cleiton_admin_routes.py`
- `tests/test_julia_chat_documental.py`
- `tests/test_julia_documents_ui.py`
- `tests/test_onboarding_discovery.py`
- `tests/test_cleiton_doc_pdf_gemini.py`
- `tests/test_julia_chat_plan_limit.py`
- `tests/test_julia_pdf_documental_chat.py`

Resultado critico registrado:

- `233 passed, 2 warnings`
- warnings de dependencia/deprecacao, sem falha funcional
