# Onboarding Tecnico

Data de consolidacao: `2026-07-10`
Commit de referencia: `d02ce15`

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
9. `docs/runbooks/cleide_homologacao_controlada_checklist.md`

## Modulos principais do estado atual

- `app/web.py`: Home, onboarding, Julia, Roberto e healthchecks
- `app/static/js/chat_behavior.js`: shell visual do Copilot e da Julia
- `app/julia_documents_routes.py`: API documental da Julia
- `app/julia_doc_context.py`: montagem do contexto documental para o chat
- `app/cleide_audit_routes.py`: upload, status, remocao, coverage, template, lote auditado e chat da Cleide Auditoria
- `app/cleide_routes.py`: superficies visuais da Cleide, incluindo `/auditoria-frete`
- `app/cleide_audit_doc_service.py`: sessao documental e ciclo de vida da tabela temporaria
- `app/run_cleide_audit_temp_table.py`: extracao tecnica pos-upload da tabela temporaria
- `app/cleide_audit_prompt.py`: prompt tecnico e prompt conversacional da Cleide Auditoria
- `app/cleiton_doc_converters.py`: conversores dos tipos aceitos
- `app/cleiton_doc_gemini_files.py`: governanca de PDF via Gemini Files
- `app/cleiton_doc_store.py`: store temporario local
- `app/services/cleiton_doc_config_service.py`: configuracao documental em `ConfigRegras`
- `app/painel_admin/admin_routes.py`: admin com bloco documental do Cleiton
- `app/painel_admin/template_admin/agentes_cleide.html`: admin da Cleide com blocos independentes de BI e Auditoria

## Premissas de arquitetura

- Home publica usa Copilot de discovery
- Home logada usa Julia operacional
- Copilot e Julia nao devem ser confundidos
- upload documental da Julia nao e produto paralelo
- Cleiton e camada central de governanca operacional
- a tabela temporaria da Cleide Auditoria e fluxo temporario governado, nao persistencia definitiva
- Cleide continua responsavel pela orientacao conversacional da auditoria
- Cleiton e o owner operacional da tabela temporaria
- a tabela temporaria nasce em modo governado e pode entrar em edicao humana no modal
- a validacao da tabela temporaria e humana e governada, nao uma nova conversa de IA

## Regras que nao podem ser quebradas

- nao encaminhar por artefato
- nao inventar leitura de documento
- nao fazer onboarding abater franquia
- nao versionar `app/cleiton_doc_tmp/`
- nao versionar `app/.tmp_repro_unit*`
- nao versionar `tt_*.json`, `.cleanup_meta.json` nem outros `.json` residuais de `app/cleiton_doc_tmp/`
- nao versionar `.db` local ou banco embarcado
- nao documentar tabela ou migration inexistente
- nao documentar a tabela temporaria da Cleide como auditoria final
- nao atribuir ownership da tabela temporaria a Cleide
- nao misturar o chat da Cleide com o fluxo de validacao da tabela temporaria
- nao transformar a auditoria em checklist rigido dentro do chat
- nao usar regex como solucao principal de interpretacao de frete

## Validacao minima de entrada

- abrir `/` deslogado e validar discovery
- abrir `/` logado e validar Julia operacional
- validar `/chat_julia?mode=operational`
- validar documentos da Julia com os tipos suportados
- validar `/auditoria-frete`, upload da Cleide Auditoria e status com `temp_table`
- validar que a tabela temporaria aparece como artefato temporario sujeito a revisao humana
- validar o modal da tabela temporaria e a entrada em modo de edicao governada
- validar a revisao humana da tabela temporaria via endpoint dedicado quando aplicavel
- validar coverage complementar e lote auditado quando o fluxo exigir
- validar admin do Cleiton
- validar Roberto e Cleide
