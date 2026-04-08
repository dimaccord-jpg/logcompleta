# Render + Cron em Homolog

Documento complementar ao `README.md` principal, focado apenas em cron/Render.

## Uso

Consulte este arquivo somente para detalhes específicos de execução automática em homolog.

## Premissas

- `APP_ENV=homolog`
- `DATABASE_URL` correto
- `CRON_SECRET` configurado
- persistência válida
- migrations já tratadas no ambiente alvo

## Validação

- cron protegido responde 403 sem segredo;
- cron responde 200 com segredo válido;
- tarefas automáticas não quebram health checks;
- execução de índices usa caminho persistente;
- não considerar cron homologado sem ambiente e schema válidos.

## Referência Principal

Status geral do projeto e fluxo operacional atual ficam consolidados no `README.md` da raiz.
