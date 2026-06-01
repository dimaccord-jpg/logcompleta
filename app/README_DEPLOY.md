# Deploy e Promocao

Data de consolidacao: `2026-05-29`  
Commit de referencia: `20fa165`

## 1. Ambientes

- `dev`: desenvolvimento local
- `homolog`: validacao antes de promover
- `prod`: producao

Variaveis contratuais:

- `APP_ENV`
- `DATABASE_URL`
- `PUBLIC_BASE_URL`
- `APP_DATA_DIR`
- chaves Gemini

## 2. Branches

Fluxo documentado:

- trabalho local
- `homolog`
- `producao`

Estado de referencia desta documentacao:

- `producao` em `20fa165`

## 3. Migrations

Aplicar sempre antes da validacao final:

```powershell
alembic -c migrations/alembic.ini upgrade head
```

Migration relevante do estado atual:

- `r2s3t4u5v6w7_onboarding_word_cloud_hidden_term.py`

## 4. Smoke checks obrigatorios

1. abrir `/`
2. validar onboarding discovery
3. validar limite anonimo de 5 interacoes
4. validar `Nova conversa`
5. validar CTA de login
6. validar `/admin/dashboard`
7. validar hidden terms do onboarding
8. validar `/fretes`
9. validar `/auditoria-frete`

## 5. Regra de promocao

Nao promover homolog -> producao sem:

- migrations aplicadas;
- dashboard admin valido;
- observabilidade do onboarding valida;
- Roberto valido;
- Cleide valida;
- regras de franquia preservadas;
- onboarding sem abatimento de franquia.
