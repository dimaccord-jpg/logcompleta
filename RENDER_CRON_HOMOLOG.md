# Render + Cron em Homolog

Documento complementar ao `README.md` principal, focado apenas em cron/Render.

## Uso

Consulte este arquivo somente para detalhes especificos de execucao automatica em homolog.

## Premissas

- `APP_ENV=homolog`
- `DATABASE_URL` correto
- `CRON_SECRET` configurado
- `APP_BASE_URL` configurado no Cron Job
- persistencia valida
- migrations existentes ja tratadas no ambiente alvo
- estado homologado de referencia: `c5a73e1`

## Comandos Render

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
- `?secret=` permanece apenas como compatibilidade temporaria
- `/cron/finance` executa a mesma coleta do comando `python -m app.finance`, mas dentro do servico principal
- a resposta deve expor `monetizacao_downgrade` para inspecao operacional da virada
- downgrades pendentes para `starter` e `free` so sao efetivados por essa rotina, nao pelo frontend
- tarefas automaticas nao quebram health checks
- execucao de indices usa caminho persistente
- nao considerar cron homologado sem ambiente e schema validos

## Observacao sobre a entrega Cleide

- a promocao da estabilizacao da auditoria Cleide nao adicionou migration propria ao cron
- a tabela temporaria da auditoria continua fora do schema persistente
- os cron jobs nao devem depender de `app/cleiton_doc_tmp/` nem versionar residuos locais
- nenhum `.db` local ou artefato tecnico da temp table faz parte do deploy

## Referencia Principal

Status geral do projeto e fluxo operacional atual ficam consolidados no `README.md` da raiz.
