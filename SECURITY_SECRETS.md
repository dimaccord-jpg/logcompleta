# Política de Segredos e Segurança Operacional

## Regras obrigatórias

1. Nunca versionar segredos reais no Git.
2. Manter placeholders somente em `app/.env.example`.
3. Não versionar `app/.env.dev`, `app/.env.homolog`, `app/.env.prod`.
4. Configurar segredos de homolog/prod no provedor (Render/secret manager), não em arquivos versionados.

## Operação segura na preparação de homolog (Fase 2)

- Status oficial da homologação: `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.
- Homolog ainda não está concluída; tratar o ambiente como transição controlada.
- Não registrar workaround de migration com senha/token embutido em comando salvo no repositório.
- Qualquer ajuste para habilitar migrations deve ser aplicado via variáveis secretas do provedor.

## Proteções já adotadas

1. `.gitignore` bloqueia `.env` e `.env.*` (exceto `.env.example`).
2. `pre-commit` com gitleaks para bloqueio local.
3. CI com varredura de segredos em PR/push.

## Variáveis sensíveis da fase

- `DATABASE_URL`
- `SECRET_KEY`
- `CRON_SECRET`
- credenciais BigQuery (`GCP_BILLING_EXPORT_TABLE` e chave associada)
- segredos OAuth/e-mail (`GOOGLE_CLIENT_SECRET`, `RESEND_API_KEY`, etc.)

## Verificação rápida local

- `pre-commit run --all-files`
- `./scripts/security/scan-secrets.ps1`

## Pontos críticos de segurança para homolog

- Não expor `CRON_SECRET` em scripts públicos ou documentação de comando com valor real.
- Não versionar JSON de service account (BigQuery).
- Não reutilizar credenciais de produção em homolog.
- Em suspeita de vazamento: rotacionar segredo, atualizar no provedor, reiniciar serviço e reexecutar scan.

Referências:

- Deploy: `app/README_DEPLOY.md`.
- Execução/runtime: `app/README_RUN.md`.
