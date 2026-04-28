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
- migrations ja tratadas no ambiente alvo

## Comandos Render

```bash
curl -fsS -X POST "$APP_BASE_URL/cron/executar-cleiton" -H "X-Cron-Secret: $CRON_SECRET"
curl -fsS -X POST "$APP_BASE_URL/cron/billing-snapshot" -H "X-Cron-Secret: $CRON_SECRET"
```

## Variaveis necessarias no Cron Job

- `APP_BASE_URL`
- `CRON_SECRET`

## Validacao

- cron protegido responde `403` sem segredo;
- cron protegido responde `403` com header invalido;
- cron responde `200` com segredo valido;
- `curl -f` torna falhas `4xx/5xx` visiveis no Render;
- `?secret=` permanece apenas como compatibilidade temporaria e sera removido apos a homologacao;
- a resposta deve expor `monetizacao_downgrade` para inspecao operacional da virada;
- downgrades pendentes para `starter` e `free` so sao efetivados por essa rotina, nao pelo frontend;
- tarefas automaticas nao quebram health checks;
- execucao de indices usa caminho persistente;
- nao considerar cron homologado sem ambiente e schema validos.

## Papel no Fluxo Stripe

- `/cron/executar-cleiton` chama `efetivar_mudancas_pendentes_ciclo()`;
- a rotina procura `ContaMonetizacaoVinculo` ativos com `mudanca_pendente=true`;
- quando `efetivar_em` chega, aplica o `plano_futuro` na `Franquia` e limpa a pendencia;
- para `free`, remove `fim_ciclo`, ajusta `inicio_ciclo` e zera `consumo_acumulado`;
- para `starter`, reinicia o ciclo mensal interno a partir de `efetivar_em`.

## Referencia Principal

Status geral do projeto e fluxo operacional atual ficam consolidados no `README.md` da raiz.
