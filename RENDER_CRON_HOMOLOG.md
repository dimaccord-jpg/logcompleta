# Render + Cron em Homolog

Documento complementar ao `README.md`, focado apenas em cron/Render.

## Premissas

- `APP_ENV=homolog`
- `DATABASE_URL` correto
- `CRON_SECRET` configurado
- `APP_BASE_URL` configurado no Cron Job
- persistencia valida
- cadeia de migrations preexistente do ambiente ja tratada
- estado homologado de referencia no workspace: `d02ce15`

## Comandos oficiais

```bash
curl -fsS -X POST "$APP_BASE_URL/cron/executar-cleiton" -H "X-Cron-Secret: $CRON_SECRET"
curl -fsS -X POST "$APP_BASE_URL/cron/finance" -H "X-Cron-Secret: $CRON_SECRET"
curl -fsS -X POST "$APP_BASE_URL/cron/billing-snapshot" -H "X-Cron-Secret: $CRON_SECRET"
```

## Variaveis necessarias no Cron Job

- `APP_BASE_URL`
- `CRON_SECRET`

## Validacao

- cron protegido responde `403` sem segredo
- cron protegido responde `403` com header invalido
- cron responde `200` com segredo valido
- `curl -f` torna falhas `4xx/5xx` visiveis no Render
- `?secret=` permanece apenas como compatibilidade temporaria do backend
- `/cron/finance` executa a mesma coleta do modulo `app.finance`, mas dentro do servico principal
- as tarefas automaticas nao devem quebrar health checks
- execucao de indices usa caminho persistente
- nao considerar cron homologado sem ambiente e schema validos

## Observacao sobre a entrega Cleide

- a entrega aprovada da auditoria Cleide nao adicionou migration propria ao cron
- a `temp_table` da auditoria continua fora do schema persistente
- os cron jobs nao devem depender de `app/cleiton_doc_tmp/`
- nenhum `.db` local ou artefato tecnico da `temp_table` faz parte do deploy

## Ponto de atencao de branch

- no arquivo versionado, homolog usa `branch: homolog` com `autoDeploy: true`
- no arquivo versionado, producao usa `branch: main` com `autoDeploy: false`
- o reposit�rio tambem possui `producao@6efa2e2` como referencia promovida
- confirmar no painel do Render qual branch esta conectada antes de promover

## Referencia principal

Status geral do projeto e fluxo operacional atual ficam consolidados no `README.md` da raiz.
