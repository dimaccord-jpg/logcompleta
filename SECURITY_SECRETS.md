# Politica Anti-Vazamento de Segredos

Este projeto adota um padrao de prevencao de vazamento em camadas.

## Regras obrigatorias

1. Nunca commitar segredos reais.
2. Manter somente placeholders em `app/.env.example`.
3. `app/.env.dev`, `app/.env.homolog` e `app/.env.prod` devem conter apenas valores locais e nao devem ser versionados.
4. Segredos de homolog/prod devem ser definidos no provedor (Render/Systemd/GitHub Secrets/Vault).

## Protecoes implementadas

1. `.gitignore` bloqueia `.env` e `.env.*` (exceto `.env.example`).
2. `pre-commit` com hook do `gitleaks` bloqueia commit local com segredo detectado.
3. GitHub Actions roda scan em pull requests e pushes para branches principais.
4. No CI, o scan do diretório atual é bloqueante (previne novos vazamentos);
	o scan de histórico completo é informativo para auditoria de legado.

## Setup local (obrigatorio)

1. Instale pre-commit:

```bash
pip install pre-commit
```

2. Ative os hooks no repositório:

```bash
pre-commit install
```

3. Rode scan manual antes de abrir PR:

```bash
pre-commit run --all-files
```

## Scan manual com gitleaks

No Windows PowerShell:

```powershell
./scripts/security/scan-secrets.ps1
```

Somente historico:

```powershell
./scripts/security/scan-secrets.ps1 -HistoryOnly
```

Somente diretorio local:

```powershell
./scripts/security/scan-secrets.ps1 -DirOnly
```

## Rotacao automatizada

1. Script Python: `scripts/security/rotate_secrets.py`
2. Wrapper PowerShell: `scripts/security/rotate-secrets.ps1`
3. Guia detalhado: `scripts/security/ROTATION_AUTOMATION.md`
4. Sync de env em deploy Render: `scripts/security/render_sync_env.ps1`
5. Smoke test pos-rotacao: `scripts/security/post_rotation_check.ps1`

Exemplo rapido (preview):

```powershell
./scripts/security/rotate-secrets.ps1 -DryRun
```

Exemplo rapido (somente segredos internos):

```powershell
./scripts/security/rotate-secrets.ps1 -AutoOnly
```

Se precisar inserir chaves ausentes no arquivo alvo, adicione `-InsertMissing`.

Exemplo completo com segredos externos ja renovados no provedor:

```powershell
./scripts/security/rotate-secrets.ps1 \
	-SetValues "GOOGLE_OAUTH_CLIENT_ID=NOVO_ID" \
	-SetValues "GOOGLE_OAUTH_CLIENT_SECRET=NOVO_SECRET" \
	-SetValues "MAIL_PASSWORD=NOVA_SENHA_APP" \
	-SetValues "GEMINI_API_KEY=NOVA_KEY" \
	-SetValues "GEMINI_API_KEY_1=NOVA_KEY_1" \
	-SetValues "GEMINI_API_KEY_2=NOVA_KEY_2" \
	-SetValues "GEMINI_API_KEY_ROBERTO=NOVA_KEY_ROBERTO"
```

## Resposta a incidente

1. Revogar e recriar a credencial imediatamente no provedor.
2. Atualizar o segredo no ambiente alvo.
3. Reiniciar o servico.
4. Rodar scan local e no CI.
5. Registrar a ocorrencia e ajustar allowlist apenas para placeholders legitimos.
