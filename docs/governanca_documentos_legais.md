# Governança Técnica dos Documentos Legais

Referência auditada em 2026-08-19.

## Escopo

Este documento descreve a governança técnica de:

- Termo de Uso ativo
- Política de Privacidade ativa

Ele não reescreve cláusulas jurídicas nem substitui os arquivos legais ativos.

## Administração e armazenamento

- a gestão operacional ocorre por upload/admin
- o storage oficial usa diretório persistente em `settings.data_dir`
- uploads operacionais não devem depender de `app/static/terms` ou `app/static/privacy_policies` como storage ativo

Diretórios canônicos:

- `${settings.data_dir}/legal/terms`
- `${settings.data_dir}/legal/privacy_policies`

## Fonte de verdade

- o banco mantém o `filename` ativo e o histórico versionado
- a disponibilidade pública depende da existência física do arquivo persistido
- em homolog/prod, `APP_DATA_DIR` ou `RENDER_DISK_PATH` precisam estar configurados corretamente

## Rotas públicas

- `/termos-de-uso`
- `/politica-de-privacidade`

## Relação com o usuário

- o usuário aceita os termos por fluxo próprio
- `accepted_terms_at` pode ser preservado como evidência mesmo após desidentificação operacional
- atualizações legais não devem depender de arquivos efêmeros da release

## Notificações e integridade operacional

- links públicos, login e mensagens devem apontar para as rotas públicas
- o deploy não deve ser considerado válido se o banco apontar para arquivo inexistente no storage persistente

## Limites documentados

- esta documentação descreve governança técnica, não o conteúdo jurídico
- PDFs e arquivos jurídicos ativos não devem ser editados nesta missão documental

## Referências úteis

- `docs/runbooks/documentos_legais_storage_persistente.md`
- `app/legal_document_storage.py`
- `app/terms_services.py`
- `app/privacy_policy_services.py`
- `app/services/termo_service.py`
- `app/services/privacy_policy_service.py`
