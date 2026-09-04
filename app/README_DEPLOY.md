# Deploy e Operação

Referência auditada em 2026-09-04. Este arquivo descreve o deploy real. Detalhes adicionais estão em `docs/DEPLOYMENT.md` e `docs/DATABASE_AND_MIGRATIONS.md`. O estado operacional consolidado está em `docs/estado_producao.md`.

## Ambientes e branches

- `APP_ENV` é obrigatório.
- Valores aceitos: `dev`, `homolog`, `prod`.
- O carregamento de ambiente usa `app/.env.{APP_ENV}`.
- No `render.yaml` atual:
  - homologação usa a branch `homolog` (`APP_ENV=homolog`);
  - produção usa a branch `producao` (`APP_ENV=prod`);
  - ambos os serviços usam `autoDeploy: true`.

## Build e start reais

### `build.sh`

- infere `APP_ENV` pelo branch quando o serviço não o informar;
- trata `homolog` como homologação;
- trata `main|master|producao|prod` como aliases de produção;
- qualquer outro branch cai em `dev`;
- rejeita valores fora de `dev|homolog|prod`;
- instala as dependências de runtime em `requirements.txt`.

### `start.sh`

Repete a validação de `APP_ENV` e executa, nesta ordem:

```bash
python -m flask --app app.web db upgrade
gunicorn --config gunicorn_config.py app.web:app
```

Isso significa que o deploy atual aplica migrations pendentes antes de subir a aplicação. Downgrade não faz parte do fluxo normal.

## Persistência obrigatória em homolog/prod

O runtime depende de caminhos persistentes válidos para uploads, artefatos técnicos, índices e documentos legais ativos.

Prioridade de resolução:

1. `APP_DATA_DIR`
2. `RENDER_DISK_PATH`
3. `/var/data` no Render, quando disponível

Também é necessário configurar:

- `INDICES_FILE_PATH`, ou deixar que ele derive de `APP_DATA_DIR`;
- `DATABASE_URL` apontando para PostgreSQL;
- volume persistente compatível com os uploads, índices e documentos legais.

Em homolog/prod, o sistema não aceita fallback silencioso para a pasta efêmera da release.

## Health checks

- `healthCheckPath` no Render: `/health`
- a aplicação também expõe `/health/liveness` e `/health/readiness`
- o readiness verifica banco principal e índices

## Variáveis de ambiente críticas

Sem expor segredos reais, o contrato operacional atual inclui pelo menos:

- `APP_ENV`
- `DATABASE_URL`
- `SECRET_KEY`
- `APP_DATA_DIR`
- `INDICES_FILE_PATH`
- `CRON_SECRET`
- `OPS_TOKEN`
- `COMMUNICATION_SUPPRESSION_HMAC_SECRET`
- `STRIPE_API_KEY`
- `STRIPE_PUBLISHABLE_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_SUCCESS_URL`
- `STRIPE_CANCEL_URL`
- `GOOGLE_OAUTH_CLIENT_ID`
- `GOOGLE_OAUTH_CLIENT_SECRET`
- `GOOGLE_OAUTH_REDIRECT_URI`
- `RESEND_API_KEY`
- `MAIL_FROM`
- `MAIL_DEFAULT_SENDER`
- `OPENAI_ADS_PIXEL_ID`
- `OPENAI_ADS_DEBUG`
- `GEMINI_API_KEY`
- `GEMINI_API_KEY_1`
- `GEMINI_API_KEY_2`
- `GEMINI_API_KEY_ROBERTO`

Use sempre placeholders em exemplos. Não versionar valores reais.

## Migration head atual

- head atual versionado: `z0a1b2c3d4e5`
- `down_revision`: `y9z0a1b2c3d4`
- migration nova: `z0a1b2c3d4e5_home_cta_experiment_event.py`

Antes de promover, confirmar que as migrations versionadas são compatíveis com esse head.

## Segurança operacional de banco

- `upgrade` normal não é bloqueado pelo guard de `app/db_operational_safety.py`
- `downgrade` exige simultaneamente `ALLOW_DB_DOWNGRADE=1` e `ALLOW_DB_DOWNGRADE_DATABASE=<nome-exato-do-database>`
- logs de segurança não expõem senha, usuário nem URI completa
- downgrade não é procedimento normal de deploy

## Checklist cauteloso de deploy

1. Validar branch, diff e conteúdo a promover.
2. Confirmar que o conteúdo destinado a `producao` é o mesmo já validado em `homolog`.
3. Confirmar migrations versionadas compatíveis com o head `z0a1b2c3d4e5`.
4. Garantir secrets e volume persistente no ambiente-alvo, via placeholders/configuração externa.
5. Promover por fast-forward only. Não usar force push.
6. Publicar e observar build, `db upgrade`, boot e health.
7. Executar smoke mínimo pós-deploy.

Não tratar downgrade de banco como rotina de promoção.
