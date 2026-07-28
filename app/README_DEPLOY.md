# Deploy e Promoção

Referência operacional consolidada em 2026-07-20.

## Premissas

- executar a partir do diretório do projeto;
- ativar a virtualenv local quando a validação ocorrer fora do Render;
- trabalhar primeiro em `homolog`;
- validar `git fetch`, branch atual, sincronização e `git status --short` antes de qualquer promoção.

## Estado atual documentado

- homolog local auditado nesta atividade: `c2575f8`;
- produção informada pelo processo operacional: `8edae63`;
- backup homolog informado: `backup/homolog-antes-agente-compara-20260720`;
- backup produção informado: `backup/producao-antes-agente-compara-20260720`;
- sem migration, tabela, coluna ou banco novo na entrega atual.

## Fluxo recomendado

1. `git fetch --all --prune`
2. `git branch --show-current`
3. `git status --short`
4. validar se `homolog` está sincronizada com o remoto pretendido;
5. executar testes e validações manuais mínimas;
6. confirmar se a cadeia de migrations existente está íntegra;
7. somente após aprovação explícita, fazer commit e push em `homolog`;
8. validar deploy de homologação no Render;
9. criar backup da branch de produção antes da promoção;
10. promover `homolog` para a branch operacional de produção com `git merge --no-ff`;
11. executar novamente as validações pós-merge;
12. push da branch de produção;
13. validar build, migrations, login, agentes, billing, cron e health check;
14. voltar para `homolog`.

## Merge `--no-ff`

Um merge `--no-ff` pode deixar a branch dois commits à frente do remoto de referência:

- o commit já existente da entrega promovida;
- o novo commit de merge.

Isso é esperado e não deve ser interpretado automaticamente como desvio.

## Render e infraestrutura

Confirmado no código versionado:

- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn;
- `render.yaml` declara homolog em `homolog` com `autoDeploy: true`;
- `render.yaml` declara produção em `main` com `autoDeploy: false`.

Divergências a validar fora do código:

- o processo operacional informado usa `producao` como branch de produção;
- o YAML versionado ainda aponta produção para `main`;
- o YAML usa `healthCheckPath: /health`, mas o código expõe `/health/liveness` e `/health/readiness`.

## Banco e migrations

- esta entrega não criou migration nova;
- deploy deve aplicar ou validar a cadeia Alembic já existente;
- não corrigir schema manualmente sem migration correspondente;
- `.db` locais e JSON temporários não participam do deploy.

## Rollback

O rollback deve partir do backup de branch criado antes da promoção, não de ajustes manuais de banco ou troca ad hoc de arquivos.
