# Log Completa

Aplicação Flask com módulos Júlia (editorial), Cleiton (governança), Roberto (BI de fretes) e painel admin.

## Fase 2 em homologação — estado real

- A branch de feature foi organizada em commits separados.
- O merge local para a branch de homolog foi concluído sem conflito.
- O pacote técnico da Fase 2 foi integrado no código local.
- A publicação final de homolog ainda está bloqueada por estratégia de migration no Render.
- Não considerar a homologação como concluída até validar migrations no ambiente alvo.

## O que entrou nesta entrega

- `execution_id` como identidade da execução no upload do Roberto.
- Billing idempotente por `execution_id`.
- Motor de governança/franquia/billing.
- Migrations da Fase 2.
- Testes automatizados relacionados.

## Bloqueio atual para publicação final em homolog

No Render (shell aberto com sucesso), os comandos de migração falharam:

- `python -m alembic current` retornou `No module named alembic`.
- `alembic` não está disponível no `PATH`.
- `requirements.txt` atual não expõe Alembic nesse ambiente.

Sem resolver esse ponto, não há confirmação segura de aplicação de schema da Fase 2 em homolog.

## Fonte de verdade e mapa de documentos

- Estado de homolog/publicação (go/no-go): `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md` (documento principal).
- Operação de deploy (procedimento): `app/README_DEPLOY.md`.
- Execução local e validações de runtime: `app/README_RUN.md`.
- Cron de homolog no Render: `RENDER_CRON_HOMOLOG.md`.
- Política de segredos: `SECURITY_SECRETS.md`.
- Operação de migrations e checklist de schema: `migrations/README`.
