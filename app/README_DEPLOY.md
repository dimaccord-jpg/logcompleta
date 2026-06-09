# Deploy e Promocao

Data de consolidacao: `2026-06-05`
Commit de referencia em producao: `b5fc444`

## Ambientes

- `dev`: desenvolvimento local
- `homolog`: validacao antes de promover
- `prod`: producao

Variaveis contratuais:

- `APP_ENV`
- `DATABASE_URL`
- `PUBLIC_BASE_URL`
- `APP_DATA_DIR`
- chaves Gemini

## Estado promovido

Entrega validada e promovida:

- `98012c8 feat: adiciona upload documental governado à Julia`
- `b5fc444 fix: estabiliza desempenho documental da Julia`

Confirmacoes operacionais:

- `producao -> b5fc444`
- homologacao validada antes da promocao
- working tree limpo apos o push de promocao

## Migrations

Nao houve migration nova nesta entrega.

Aplicar a cadeia existente normalmente antes de validar um ambiente:

```powershell
alembic -c migrations/alembic.ini upgrade head
```

## Smoke checks obrigatorios

1. abrir `/` deslogado e validar onboarding discovery
2. confirmar limite anonimo de `5` interacoes
3. validar CTA de login
4. fazer login e validar Julia operacional na Home
5. validar `/chat_julia?mode=operational`
6. validar upload documental da Julia
7. validar PDF governado com Gemini Files quando configurado
8. validar bloqueios por autorizacao/plano/franquia
9. validar `/admin/agentes-cleiton` e o bloco documental
10. validar `/admin/dashboard`, `/fretes` e `/cleide-bi-frete`

## Regra de promocao

Nao promover homolog -> producao sem:

- migrations existentes aplicadas ate `head`
- validacao da Home publica e da Home logada
- validacao da Julia documental governada
- validacao da observabilidade
- validacao de Roberto e Cleide
- confirmacao de que nenhum temporario foi versionado
