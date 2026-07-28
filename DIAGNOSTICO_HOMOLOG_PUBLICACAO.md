# Diagnóstico de Homologação e Publicação

Fotografia operacional consolidada em 2026-07-20:

- `homolog@c2575f8` auditado localmente;
- produção promovida informada: `8edae63`;
- backups informados: `backup/homolog-antes-agente-compara-20260720` e `backup/producao-antes-agente-compara-20260720`;
- nenhuma migration, tabela ou coluna criada na entrega atual.

## Go / No-Go

No-Go se:

- worktree contiver resíduo não inspecionado;
- suite falhar;
- migration necessária estiver ausente;
- branch real do Render divergir do processo aprovado;
- health check, autenticação, billing ou cron falharem.

Go somente após:

- validação da suite relevante;
- conferência de `/health/liveness` e `/health/readiness`;
- login e `next`;
- Júlia, Roberto, Cleide e Agente Compara;
- billing e observabilidade;
- inspeção de temporários e schema.

## Branch e Render

O processo operacional informado usa `homolog` e `producao`, mas o `render.yaml` versionado ainda contém produção em `main`. Tratar isso como divergência confirmada entre documentação versionada e processo informado, nunca como equivalência automática.
