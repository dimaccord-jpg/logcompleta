# Deploy e Operação

Referência auditada em 2026-08-19. O estado operacional consolidado está em `docs/estado_producao.md`.

## Ambientes e branches

- `APP_ENV` é obrigatório.
- Valores aceitos: `dev`, `homolog`, `prod`.
- O carregamento de ambiente usa apenas `app/.env.{APP_ENV}`.
- No `render.yaml` atual:
- homolog usa a branch `homolog`;
- produção usa a branch `producao`.

## Build e start reais

### `build.sh`

- infere `APP_ENV` pelo branch apenas se o serviço não o informar;
- trata `main|master|producao|prod` como alias de produção;
- rejeita valores fora de `dev|homolog|prod`;
- instala `requirements.txt`.

### `start.sh`

- repete a validação de `APP_ENV`;
- executa:

```bash
python -m flask --app app.web db upgrade
```

- depois sobe:

```bash
gunicorn --config gunicorn_config.py app.web:app
```

Isso significa que o deploy atual depende de migrations aplicáveis no boot.

## Persistência obrigatória em homolog/prod

O runtime falha de forma explícita se não houver caminho persistente válido para dados operacionais.

Prioridade de resolução:

1. `APP_DATA_DIR`
2. `RENDER_DISK_PATH`
3. `/var/data` no Render, quando disponível

Também é necessário configurar:

- `INDICES_FILE_PATH`, ou deixar que ele derive de `APP_DATA_DIR`;
- volume persistente compatível com os uploads, índices e documentos legais.

Em homolog/prod, o sistema não aceita fallback silencioso para a pasta da release.

## Health checks

- `render.yaml` mantém `healthCheckPath: /health`;
- a aplicação expõe `/health`, `/health/liveness` e `/health/readiness`;
- o readiness verifica banco principal e índices.

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

## Fluxo operacional recomendado

1. Validar branch, diff e commit auditado.
2. Confirmar que o conteúdo promovido para `producao` é o mesmo validado em `homolog`.
3. Confirmar migrations versionadas compatíveis com o head esperado.
4. Garantir secrets e volume persistente no ambiente-alvo.
5. Publicar e observar build, `db upgrade`, boot e health.
6. Executar smoke mínimo pós-deploy.

## Estado desta rodada

- produção já foi promovida;
- head esperado de migration em produção: `y9z0a1b2c3d4`;
- as migrations desta entrega foram:
- `v6w7x8y9z0a1` `CommunicationSuppression`
- `w7x8y9z0a1b2` activation journey ended
- `x8y9z0a1b2c3` `NewsletterSubscription`
- `y9z0a1b2c3d4` `Lead.email_hmac`

O smoke operacional desta entrega já foi concluído e está resumido em `docs/estado_producao.md`.
