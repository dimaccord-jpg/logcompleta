# Deploy e Promocao

Data de consolidacao: `2026-06-16`
Commit de referencia em producao: `5f10f6d`

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

- `c5a73e1 feat: estabiliza tabela temporaria da auditoria Cleide`
- `5f10f6d merge: promove estabilizacao da auditoria Cleide para producao`

Confirmacoes operacionais:

- `homolog -> c5a73e1`
- `producao -> 5f10f6d`
- homologacao validada antes da promocao
- producao aprovada apos o deploy
- working tree limpo antes e depois dos pushes

## Migrations

Nao houve migration nova nesta entrega.
Nao houve nova tabela, novo campo ou alteracao manual de banco nesta entrega.
Nao houve schema novo nem arquivo `.db` versionado nesta entrega.

Aplicar a cadeia existente normalmente antes de validar um ambiente:

```powershell
alembic -c migrations/alembic.ini upgrade head
```

Esta etapa continua obrigatoria para o ambiente, mas a entrega da estabilizacao da auditoria Cleide nao adiciona migration propria.

## Smoke checks obrigatorios

1. abrir `/` deslogado e validar onboarding discovery
2. confirmar limite anonimo de `5` interacoes
3. validar CTA de login
4. fazer login e validar Julia operacional na Home
5. validar `/chat_julia?mode=operational`
6. validar upload documental da Julia
7. validar `/auditoria-frete` com upload, status documental e tabela temporaria extraida
8. confirmar que a tabela temporaria aparece como card clicavel no painel de anexos/documentos
9. abrir o modal e confirmar modo somente leitura com validacao humana obrigatoria
10. confirmar que o chat da Cleide nao recria nem sobrescreve a tabela temporaria
11. validar PDF governado com Gemini Files quando configurado
12. validar bloqueios por autorizacao/plano/franquia
13. validar `/admin/agentes-cleiton` e o bloco documental
14. validar `/admin/dashboard`, `/fretes` e `/cleide-bi-frete`

## Regra de promocao

Nao promover homolog -> producao sem:

- migrations existentes aplicadas ate `head`
- validacao da Home publica e da Home logada
- validacao da Julia documental governada
- validacao da Cleide Auditoria com tabela temporaria pos-upload
- confirmacao de que a tabela temporaria continua separada do chat
- validacao da observabilidade
- validacao de Roberto e Cleide
- confirmacao de que nenhum temporario foi versionado

Temporarios que nao podem entrar em commit:

- `app/cleiton_doc_tmp/`
- `app/cleiton_doc_tmp/tt_*.json`
- `app/cleiton_doc_tmp/.cleanup_meta.json`
- outros `.json` residuais dessa pasta
- `app/.tmp_repro_unit*`
- qualquer `.db` local ou artefato tecnico descartavel fora do fluxo oficial
