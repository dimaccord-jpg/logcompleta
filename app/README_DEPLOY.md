# Deploy e Promocao

Data de consolidacao: `2026-06-30`
Commit de referencia em producao: `0afe528`

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

- `faae3b0 fix: estabiliza auditoria de frete da Cleide`
- `7cc884e fix: ajusta regra contextual de frete da Cleide`
- `28904e4 fix: refina resolucao contextual de rotas da Cleide`

Confirmacoes operacionais:

- `homolog -> 28904e4`
- `producao -> 0afe528`
- homologacao validada antes da promocao
- producao aprovada apos o deploy
- apos o push, `origin/producao` ficou em `0afe528`
- o ambiente local retornou para `homolog`, limpo e sincronizado
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
As migrations citadas aqui sao preexistentes do ambiente e nao pertencem a esta promocao.

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
10. validar a revisao humana governada via `POST /api/cleide-auditoria/temp-table/save` quando o fluxo exigir ajuste
11. confirmar que o chat da Cleide nao recria nem sobrescreve a tabela temporaria
12. validar PDF governado com Gemini Files quando configurado
13. validar bloqueios por autorizacao/plano/franquia
14. validar `/admin/agentes-cleiton` e o bloco documental
15. validar `/admin/dashboard`, `/fretes` e `/cleide-bi-frete`

## Regra de promocao

Nao promover homolog -> producao sem:

- migrations existentes aplicadas ate `head`
- working tree limpo confirmado com `git status --short`
- validacao da Home publica e da Home logada
- validacao da Julia documental governada
- validacao da Cleide Auditoria com tabela temporaria pos-upload
- validacao da revisao humana da tabela temporaria quando aplicavel
- confirmacao de que a tabela temporaria continua separada do chat
- execucao dos testes especificos:
  `pytest tests/test_cleide_audit_temp_table.py tests/test_cleide_auditoria_page.py tests/test_cleide_admin_routes.py tests/test_cleide_audit_config_service.py tests/test_cleide_audit_doc_routes.py`
- confirmacao do resultado aprovado: `572 passed, 2 warnings` em aproximadamente `220.39s`
- validacao da observabilidade
- validacao de Roberto e Cleide
- confirmacao de que nenhum temporario foi versionado
- push controlado e validacao de `/health` no Render

## Ponto de atencao Render

- a documentacao operacional desta entrega usa `producao` como branch oficial de publicacao
- o `render.yaml` versionado ainda referencia `branch: main` no servico `logcompleta-web-prod`
- o painel real do Render usa `producao` como branch ativa de producao, com deploy manual e `autoDeploy: false`
- nao forcar alteracao de codigo so para alinhar esse ponto; confirmar a configuracao real do painel Render antes de uma nova promocao

Temporarios que nao podem entrar em commit:

- `app/cleiton_doc_tmp/`
- `app/cleiton_doc_tmp/tt_*.json`
- `app/cleiton_doc_tmp/.cleanup_meta.json`
- outros `.json` residuais dessa pasta
- `app/.tmp_repro_unit*`
- qualquer `.db` local ou artefato tecnico descartavel fora do fluxo oficial
