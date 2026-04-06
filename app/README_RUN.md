# Guia de Execução Local

Este documento cobre execução local e validação operacional rápida da Fase 2.

Para deploy/homolog: `README_DEPLOY.md`.  
Para status oficial: `../DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.

## 1) Pré-requisitos

- Dependências instaladas: `pip install -r requirements.txt`.
- `DATABASE_URL` válido para o banco local.
- `APP_ENV` definido (`dev`, `homolog` ou `prod`).
- Arquivo de ambiente correspondente baseado em `app/.env.example`.

## 2) Subida local da aplicação

PowerShell:

```powershell
$env:APP_ENV="dev"
python -m app.web
```

Bash:

```bash
export APP_ENV=dev
python -m app.web
```

## 3) Fluxos mínimos para validar Fase 2 localmente

1. Acessar aplicação e autenticar.
2. Validar chat Júlia (`/api/chat_julia`) com usuário permitido.
3. Validar upload Roberto (`/api/roberto/upload`) com usuário permitido.
4. Validar painel admin:
   - `/admin/dashboard`
   - `/admin/agentes/julia`
   - `/admin/agentes/cleiton`
   - `/admin/controle-usuarios`
   - `/admin/planos`
5. Validar endpoint admin de franquia:
   - `/admin/api/cleiton-franquia/<franquia_id>/validacao`

## 4) Execuções locais úteis

- Web: `python -m app.web`
- Índices financeiros: `python -m app.finance`
- Ciclo Cleiton: `python -m app.run_cleiton`
- Testes focados Fase 2:
  - `pytest tests/test_cleiton_classificacao_status.py`
  - `pytest tests/test_cleiton_motor_reconciliacao.py`
  - `pytest tests/test_cleiton_upload_billing_service.py`
  - `pytest tests/test_franquia_operacao_autorizacao_service.py`

## 5) Troubleshooting objetivo

- `No module named alembic`: esperado no estado atual se Alembic não estiver instalado no ambiente.
- `relation does not exist`: schema do banco não alinhado; revisar `../migrations/README`.
- `DATABASE_URL` inválido: app não inicia ou falha em readiness.
- chat/upload bloqueado para usuário: verificar status operacional de franquia (`blocked/expired`).

## 6) Referências

- Deploy/homolog: `README_DEPLOY.md`.
- Migrations: `../migrations/README`.
- Cron em homolog: `../RENDER_CRON_HOMOLOG.md`.
- Segurança: `../SECURITY_SECRETS.md`.
