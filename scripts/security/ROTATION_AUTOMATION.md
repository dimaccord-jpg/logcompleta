# Automacao de Rotacao de Segredos

Este fluxo automatiza todas as rotacoes tecnicamente possiveis dentro do repositorio.

## O que e automatizado

1. Geracao de novos segredos internos:
   - SECRET_KEY
   - OPS_TOKEN
   - CRON_SECRET
2. Aplicacao desses valores em um ou mais arquivos `.env` alvo.
3. Atualizacao de segredos externos, quando os novos valores forem fornecidos via parametro `--set KEY=VALUE`.
4. Emissao de relatorio JSON com valores mascarados.

Comportamento padrao:

1. Apenas chaves existentes sao atualizadas.
2. Chaves ausentes so sao inseridas quando `--insert-missing` (PowerShell: `-InsertMissing`) for informado.

## O que depende de provedor externo

Estas chaves precisam ser renovadas no provedor antes de atualizar localmente:

1. GOOGLE_OAUTH_CLIENT_ID
2. GOOGLE_OAUTH_CLIENT_SECRET
3. GEMINI_API_KEY
4. GEMINI_API_KEY_1
5. GEMINI_API_KEY_2
6. GEMINI_API_KEY_ROBERTO

## Uso rapido (PowerShell)

Preview sem alterar arquivos:

```powershell
./scripts/security/rotate-secrets.ps1 -DryRun
```

Rotacao automatica apenas de segredos internos:

```powershell
./scripts/security/rotate-secrets.ps1 -AutoOnly
```

Se precisar inserir chaves ausentes no arquivo alvo:

```powershell
./scripts/security/rotate-secrets.ps1 -AutoOnly -InsertMissing
```

Rotacao completa (internos + externos ja renovados no provedor):

> OBS: `MAIL_PASSWORD` era usado na configuracao SMTP/Gmail legada e nao e mais utilizado pelo app.
> A rotacao de segredos de e-mail agora deve focar em `RESEND_API_KEY` configurado nos arquivos `.env.*` dentro de `app/`.

```powershell
./scripts/security/rotate-secrets.ps1 \
  -SetValues "GOOGLE_OAUTH_CLIENT_ID=NOVO_ID" \
  -SetValues "GOOGLE_OAUTH_CLIENT_SECRET=NOVO_SECRET" \
  -SetValues "GEMINI_API_KEY=NOVA_KEY" \
  -SetValues "GEMINI_API_KEY_1=NOVA_KEY_1" \
  -SetValues "GEMINI_API_KEY_2=NOVA_KEY_2" \
  -SetValues "GEMINI_API_KEY_ROBERTO=NOVA_KEY_ROBERTO"
```

Por padrao, o relatorio e salvo em `scripts/security/rotation-report.json`.

## CI agendado

Workflow: `.github/workflows/rotate-internal-secrets.yml`

1. Executa mensalmente (dia 1, 03:00 UTC) em modo preview.
2. Gera artefato com relatorio de rotacao interna.
3. Nao grava segredos reais no repositorio.
4. Notificacao opcional por webhook usando o secret `ROTATION_NOTIFY_WEBHOOK_URL`.

## Aplicacao no deploy (Render)

Script: `scripts/security/render_sync_env.ps1`

ATENCAO: a operacao de update de env-vars na Render pode substituir o conjunto inteiro de variaveis.
Por seguranca, o script exige:

1. `-UnsafeReplaceAll`
2. `-ConfirmServiceId` igual ao valor de `RENDER_SERVICE_ID`
3. `-ConfirmPhrase "EU_ASSUMO_REPLACE_TOTAL_DA_RENDER"`
4. Backup automatico do ambiente remoto em `scripts/security/backups/` antes de aplicar

Variaveis obrigatorias no shell:

1. `RENDER_API_TOKEN`
2. `RENDER_SERVICE_ID`

Exemplo de preview (sem aplicar):

```powershell
./scripts/security/render_sync_env.ps1 -EnvFile app/.env.homolog -DryRun
```

Exemplo aplicando no servico:

```powershell
./scripts/security/render_sync_env.ps1 -EnvFile app/.env.homolog -UnsafeReplaceAll -ConfirmServiceId $env:RENDER_SERVICE_ID -ConfirmPhrase "EU_ASSUMO_REPLACE_TOTAL_DA_RENDER"
```

## Validacao pos-rotacao

Script: `scripts/security/post_rotation_check.ps1`

```powershell
./scripts/security/post_rotation_check.ps1 -BaseUrl "https://seu-dominio" -OpsToken "SEU_OPS_TOKEN"
```

## Boas praticas

1. Sempre rodar `-DryRun` antes de aplicar.
2. Rotacionar primeiro no provedor e depois atualizar arquivos locais.
3. Reiniciar aplicacao apos atualizar variaveis.
4. Validar fluxo OAuth, e-mail e rotas operacionais apos cada rotacao.
