# Deploy e Promocao

Data de consolidacao: `2026-06-16`
Commit de referencia em producao: `41c9271`

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

- `f4ffeb1 feat: adiciona tabela temporaria na auditoria da Cleide`
- `41c9271 merge: promove tabela temporaria da Cleide para producao`

Confirmacoes operacionais:

- `homolog -> f4ffeb1`
- `producao -> 41c9271`
- homologacao validada antes da promocao
- producao aprovada apos a promocao
- working tree limpo apos o push de promocao

## Migrations

Nao houve migration nova nesta entrega.
Nao houve alteracao manual de banco nesta entrega.

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
7. validar `/auditoria-frete` com upload, status e tabela temporaria extraida
8. validar PDF governado com Gemini Files quando configurado
9. validar bloqueios por autorizacao/plano/franquia
10. validar `/admin/agentes-cleiton` e o bloco documental
11. validar `/admin/dashboard`, `/fretes` e `/cleide-bi-frete`

## Regra de promocao

Nao promover homolog -> producao sem:

- migrations existentes aplicadas ate `head`
- validacao da Home publica e da Home logada
- validacao da Julia documental governada
- validacao da Cleide Auditoria com tabela temporaria pos-upload
- validacao da observabilidade
- validacao de Roberto e Cleide
- confirmacao de que nenhum temporario foi versionado

Temporarios que nao podem entrar em commit:

- `app/.tmp_repro_unit*`
- `app/cleiton_doc_tmp/tt_*.json`
