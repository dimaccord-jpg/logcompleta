# Runbook: Documentos Legais em Storage Persistente

## Objetivo

Consolidar a operacao correta de Termos de Uso e Politica de Privacidade apos a correcao estrutural que removeu a dependencia operacional de `app/static/...`.

## Estado correto atual

- uploads operacionais nao devem gravar em `app/static/terms` nem `app/static/privacy_policies`;
- o storage oficial agora e `settings.data_dir`;
- diretorios canonicos:
  - `${settings.data_dir}/legal/terms`
  - `${settings.data_dir}/legal/privacy_policies`
- o banco deve manter apenas `filename` do documento ativo;
- as rotas publicas continuam sendo:
  - `/termos-de-uso`
  - `/politica-de-privacidade`

## Contexto do incidente corrigido

Problema historico:

- admin fazia upload do arquivo;
- arquivo podia cair dentro da release efemera do deploy;
- novo deploy removia o arquivo fisico;
- banco seguia apontando para `filename` ativo inexistente.

Sintomas observados:

- `/termos-de-uso` retornando `404`;
- `/politica-de-privacidade` retornando `404`;
- `/login` exibindo `Termos de Uso` como texto sem link.

## Regras operacionais

- em `dev`, sem `APP_DATA_DIR`, o sistema pode usar fallback local do app;
- em homolog/prod, exigir `APP_DATA_DIR` ou `RENDER_DISK_PATH`;
- o disco persistente do Render deve estar montado e coerente com `settings.data_dir`;
- upload admin so deve ativar documento quando o arquivo existir fisicamente no storage persistente;
- links publicos, e-mails e notificacoes devem apontar para as rotas publicas, nunca para `/static/...`.

## Procedimento seguro de homolog/producao

1. Confirmar `APP_DATA_DIR` ou `RENDER_DISK_PATH` no ambiente.
2. Confirmar disco persistente montado no servico.
3. Fazer deploy.
4. Validar startup, migrations e health checks.
5. Fazer upload do Termo de Uso pela tela admin.
6. Fazer upload da Politica de Privacidade pela tela admin.
7. Validar `/termos-de-uso` com `200`.
8. Validar `/politica-de-privacidade` com `200`.
9. Validar `/login` com link clicavel de Termos.
10. Fazer redeploy de prova.
11. Revalidar `/termos-de-uso`, `/politica-de-privacidade` e `/login`.

## Criterio de aprovacao

Nao considerar deploy aprovado se qualquer item abaixo falhar:

- documentos ativos nao estiverem disponiveis nas rotas publicas;
- login perder o link de Termos;
- arquivo existir no banco, mas nao existir fisicamente no storage persistente;
- comportamento depender de `app/static/...` como armazenamento operacional.

## Arquivos tecnicos relacionados

- `app/legal_document_storage.py`
- `app/terms_services.py`
- `app/privacy_policy_services.py`
- `app/services/termo_service.py`
- `app/services/privacy_policy_service.py`
- `app/web.py`
- `app/templates/login.html`
- `tests/test_legal_documents_persistent_storage.py`
- `tests/test_login_terms_link.py`

## Testes minimos recomendados

```bash
python -m pytest tests/test_login_terms_link.py tests/test_user_area_checkout_feedback.py tests/test_franquia_operacao_autorizacao_service.py tests/test_legal_documents_persistent_storage.py -q
```

Resultado esperado atual:

- todos passando.
