# Onboarding técnico

Guia revisado em 2026-09-19 para o release Multiuser V1 em produção (`origin/producao`, `f28f28e`).

## Ordem de leitura

1. [README](../README.md) e [inventário documental](indice_documentacao.md).
2. [Estado de produção](estado_producao.md): release, pendências e limites de evidência.
3. [Arquitetura](arquitetura_oficial.md) e [agentes](AGENTS.md).
4. [Multiuser V1](multiuser_v1.md) e [monetização/franquias](guia_monetizacao_franquias.md).
5. [Execução local](../app/README_RUN.md), [deploy](DEPLOYMENT.md) e [banco/migrations](DATABASE_AND_MIGRATIONS.md).
6. [AgenteAudita](cleide_auditoria_operacional.md), [AgenteCompara](agente_compara_estado_oficial.md) e [discovery](runbook_onboarding_copilot.md).
7. [Privacidade](lgpd_governanca_tecnica.md), [IA externa](integracoes_ia_privacidade.md), [observabilidade](runtime_ia_e_observabilidade.md) e [troubleshooting](troubleshooting_operacional.md).

## Módulos principais

- `app/web.py`: aplicação Flask, home, autenticação, Roberto, cron, webhook e health.
- `app/models.py`: Conta, User, Franquia, vínculos e operações comerciais persistentes.
- `app/conta_multiuser_painel_routes.py` e `app/conta_multiuser_convite_routes.py`: gestão e convites.
- `app/services/conta_multiuser_*`: contratação, capacidade, ciclo, convites, aumentos, redução, revogação, titularidade e diagnóstico.
- `app/julia_documents_routes.py`, `app/cleide_audit_routes.py` e `app/agente_compara_api_routes.py`: domínios documentais separados.
- `app/cleiton_doc_store.py`: infraestrutura documental comum com ownership, não repositório compartilhado entre membros.
- `app/services/*`: autorização, billing, custos, métricas e configurações.

## Premissas para desenvolvimento

Use `APP_ENV=dev` e banco de desenvolvimento. `homolog` corresponde a `APP_ENV=homolog`; `producao` a `APP_ENV=prod`, em serviços Render separados. Ambos têm Auto-Deploy por commit. `start.sh` aplica `python -m flask --app app.web db upgrade` antes de `gunicorn --config gunicorn_config.py app.web:app`. Head atual: `f7g8h9i0j1k2`. Health do Render: `/health`, com `/health/liveness` e `/health/readiness` adicionais.

`User.categoria` é plano, `User.is_admin` é admin global, e contratante/membro são papéis do vínculo organizacional. Capacidade e ciclo são da Conta; consumo é da Franquia individual. Endpoints internos não são API pública de integração.

## Regras de integridade

- Preservar namespaces, usuário e sessão ao acessar artefatos; `conta_id` sozinho não autoriza documentos privados.
- Preservar histórico e consumo em revogação/reentrada; não confundir isso com o reset idempotente de uma renovação paga.
- Manter `temp_table` como estado temporário, com revisão explícita; chat não altera tabela por autorização implícita.
- Não versionar `.env` reais, bancos locais, caches ou artefatos técnicos.
- Selecionar testes existentes relacionados à mudança. Os fixtures centrais usam SQLite em memória; validar mocks dos serviços externos.

`app/copilot_capabilities.md` é carregado como prompt pelo runtime. Alterá-lo é mudança funcional, mesmo tendo extensão Markdown. Não usar relatórios de auditoria ou checklists históricos como especificação vigente.
