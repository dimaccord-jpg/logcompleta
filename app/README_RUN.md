# Guia de Execução Local

Este documento cobre execução local e validação rápida de runtime.  
Para deploy/homologação: `README_DEPLOY.md`.  
Para status real da Fase 2: `../DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.

## 1) Ambiente e variáveis

- `APP_ENV` deve estar definido antes de iniciar a aplicação.
- Valores válidos: `dev`, `homolog`, `prod`.
- O loader usa `app/.env.{APP_ENV}`.

Verificação imediata:

- PowerShell: `echo $env:APP_ENV`
- Bash: `echo $APP_ENV`

## 2) Comandos de execução

Na raiz do repositório:

```powershell
$env:APP_ENV="dev"
python app/web.py
```

Ou:

```bash
export APP_ENV=dev
python -m app.web
```

## 3) Pré-requisitos mínimos

- Dependências: `pip install -r requirements.txt`.
- `DATABASE_URL` válido para PostgreSQL.
- `.env` do ambiente correspondente criado a partir de `app/.env.example`.

## 4) Troubleshooting objetivo

- `APP_ENV obrigatório`: variável não exportada na sessão.
- `DATABASE_URL ausente/inválida`: ajustar URI PostgreSQL.
- `relation does not exist`: migrations pendentes no banco alvo (`../migrations/README`).
- `No module named app`: comando executado fora da raiz do repositório.

## 5) Execuções de validação local úteis

- Aplicação web: `python -m app.web`.
- Rotina de índices: `python -m app.finance`.
- Ciclo Cleiton: `python -m app.run_cleiton`.

## 6) Referências

- Deploy/homolog: `README_DEPLOY.md`.
- Cron homolog no Render: `../RENDER_CRON_HOMOLOG.md`.
- Segurança de segredos: `../SECURITY_SECRETS.md`.
