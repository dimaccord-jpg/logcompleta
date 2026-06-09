# Onboarding Tecnico

Data de consolidacao: `2026-06-05`
Commit de referencia: `b5fc444`

## Objetivo

Acelerar a entrada tecnica no projeto sem criar leitura paralela da arquitetura atual.

## Ordem de leitura

1. `README.md`
2. `docs/estado_oficial_consolidado.md`
3. `docs/arquitetura_oficial.md`
4. `docs/runtime_ia_e_observabilidade.md`
5. `docs/troubleshooting_operacional.md`
6. `docs/runbook_onboarding_copilot.md`
7. `app/README_RUN.md`
8. `app/README_DEPLOY.md`
9. `migrations/README`

## Modulos principais do estado atual

- `app/web.py`: Home, onboarding, Julia, Roberto e healthchecks
- `app/static/js/chat_behavior.js`: shell visual do Copilot e da Julia
- `app/julia_documents_routes.py`: API documental da Julia
- `app/julia_doc_context.py`: montagem do contexto documental para o chat
- `app/cleiton_doc_converters.py`: conversores dos tipos aceitos
- `app/cleiton_doc_gemini_files.py`: governanca de PDF via Gemini Files
- `app/cleiton_doc_store.py`: store temporario local
- `app/services/cleiton_doc_config_service.py`: configuracao documental em `ConfigRegras`
- `app/painel_admin/admin_routes.py`: admin com bloco documental do Cleiton

## Premissas de arquitetura

- Home publica usa Copilot de discovery
- Home logada usa Julia operacional
- Copilot e Julia nao devem ser confundidos
- upload documental da Julia nao e produto paralelo
- Cleiton e camada central de governanca operacional

## Regras que nao podem ser quebradas

- nao encaminhar por artefato
- nao inventar leitura de documento
- nao fazer onboarding abater franquia
- nao versionar `app/cleiton_doc_tmp/`
- nao documentar tabela ou migration inexistente

## Validacao minima de entrada

- abrir `/` deslogado e validar discovery
- abrir `/` logado e validar Julia operacional
- validar `/chat_julia?mode=operational`
- validar documentos da Julia com os tipos suportados
- validar admin do Cleiton
- validar Roberto e Cleide
