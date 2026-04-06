# Log Completa

Aplicação Flask com módulos Júlia (editorial), Cleiton (governança operacional) e Roberto (upload/BI), com painel admin para operação.

## Fase 2 (entrega atual) — visão executiva

Escopo técnico consolidado no pacote final:

- Identidade de execução no upload (`execution_id`) e apropriação idempotente no billing por chave de idempotência.
- Governança operacional por franquia (status `active/degraded/expired/blocked`) aplicada sobre eventos técnicos.
- Motor de conversão de consumo em créditos (tokens, linhas e milissegundos) com reconciliação e validação administrativa.
- Modelo Conta/Franquia/Usuário com suporte a multiuser, códigos por franquia e atribuição administrativa de plano.
- Rotas/telas admin para validar operação real em homolog (dashboard, agentes, controle de usuários, planos).
- Cadeia de migrations da Fase 2 + testes automatizados de classificação, motor, idempotência e autorização operacional.

## Estado real da homologação

- Merge local para homolog concluído.
- Publicação final de homolog **ainda pendente**.
- Bloqueio operacional confirmado no Render:
  - `alembic` ausente no `PATH`.
  - `python -m alembic current` falhou com `No module named alembic`.
  - `requirements.txt` atual não expõe Alembic.

Não tratar homologação como concluída até a estratégia de migration ser resolvida e validada no ambiente.

## Onde consultar cada assunto

- Fonte principal de status/go-no-go: `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`.
- Runbook de deploy/homolog: `app/README_DEPLOY.md`.
- Execução local e validação operacional: `app/README_RUN.md`.
- Cron em Render (homolog): `RENDER_CRON_HOMOLOG.md`.
- Migrations e cadeia Fase 2: `migrations/README`.
- Segurança/segredos: `SECURITY_SECRETS.md`.
