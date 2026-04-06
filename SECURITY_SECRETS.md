# Política de Segredos e Segurança Operacional

## Regras obrigatórias

1. Nunca versionar segredos reais.
2. Manter placeholders apenas em `app/.env.example`.
3. Não versionar `app/.env.dev`, `app/.env.homolog` e `app/.env.prod`.
4. Definir segredos de homolog/prod no provedor (Render/systemd/secrets manager).

## Operação segura na preparação de homolog (Fase 2)

- O status de publicação está em `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.
- O deploy de homolog ainda não está concluído; tratar credenciais como ambiente em transição controlada.
- Não executar workaround de migration com segredo embutido em comando versionado.
- Se houver ajuste de runtime para viabilizar migrations, aplicar somente via variáveis/segredos do provedor.

## Proteções já adotadas

1. `.gitignore` bloqueia `.env` e `.env.*` (exceto `.env.example`).
2. `pre-commit` com gitleaks para bloqueio local.
3. CI com varredura de segredos em PR/push.

## Setup mínimo local

```bash
pip install pre-commit
pre-commit install
pre-commit run --all-files
```

## Scan manual (PowerShell)

```powershell
./scripts/security/scan-secrets.ps1
./scripts/security/scan-secrets.ps1 -HistoryOnly
./scripts/security/scan-secrets.ps1 -DirOnly
```

## Credenciais de billing (BigQuery)

Para `/cron/billing-snapshot`, usar credencial com leitura na tabela definida em `GCP_BILLING_EXPORT_TABLE`.

- Não versionar JSON de service account.
- Preferir segredo do provedor ou arquivo montado em volume.

## Rotação e resposta a incidente

- Ferramentas: `scripts/security/rotate_secrets.py` e `scripts/security/rotate-secrets.ps1`.
- Em incidente: revogar credencial, atualizar no provedor, reiniciar serviço, rodar scans e registrar ocorrência.

Referências:

- Deploy: `app/README_DEPLOY.md`.
- Execução/runtime: `app/README_RUN.md`.
