# Diagnóstico de Homologação e Publicação

Referência auditada em 2026-07-31.

## Estado consolidado

- branch local auditada: `homolog`;
- working tree auditado no início da revisão documental: com alterações locais pré-existentes fora deste diagnóstico; o estado funcional auditado do AgenteCompara foi conferido pelo commit `29b8500`;
- commit atual auditado: `29b8500` - `feat(agente-compara): consolida cálculo, validação e revisão multietapas`;
- upstream auditado: `origin/homolog`;
- relação local x upstream auditada: `0  0` em `git rev-list --left-right --count HEAD...origin/homolog`;
- commit equivalente informado em produção por `cherry-pick`: `d20672a` - `feat(agente-compara): consolida cálculo, validação e revisão multietapas`;
- nenhuma migration nova na entrega recente do AgenteCompara;
- cadeia Alembic versionada mantida até `r2s3t4u5v6w7`.

## Validação registrada da publicação

- homologação aprovada em `29b8500`;
- publicação equivalente em produção na branch operacional `producao`, commit `d20672a`;
- suíte direcionada do AgenteCompara: `612 passed, 1 skipped, 2 warnings`;
- warnings restritos a depreciações externas, sem evidência de regressão funcional do fluxo publicado.

## Go / No-Go

No-Go se houver:

- falha de autenticação ou bloqueio de franquia não previsto;
- inconsistência na jornada multitabela do AgenteCompara;
- divergência não resolvida entre branch operacional e branch alvo real do serviço no Render;
- falha de `db upgrade` no boot;
- problema de health check, billing, observabilidade, storage ou lock do cálculo comparativo.

Go somente após:

- validação da jornada crítica do AgenteCompara;
- conferência de `/health/liveness` e `/health/readiness`;
- validação de logs e fluxos críticos pós-deploy;
- confirmação da estratégia de branch usada pelo serviço de produção no Render;
- confirmação de que o conteúdo promovido para produção corresponde funcionalmente ao commit `29b8500`, ainda que o hash publicado em produção seja `d20672a`.
