# Onboarding técnico

Guia auditado contra a árvore de produção `fa98317` (Sprint 12).

## Ordem de leitura

1. [README](../README.md) e [inventário documental](indice_documentacao.md).
2. [Estado de produção](estado_producao.md): Sprint 12, pendências e limites de evidência.
3. [Arquitetura](arquitetura_oficial.md) e [agentes](AGENTS.md) — AgenteFrete público vs nomes internos.
4. [Multiuser V1](multiuser_v1.md) e [monetização/franquias](guia_monetizacao_franquias.md).
5. [Execução local](../app/README_RUN.md), [deploy](DEPLOYMENT.md) e [banco/migrations](DATABASE_AND_MIGRATIONS.md).
6. [AgenteAudita](cleide_auditoria_operacional.md), [AgenteCompara](agente_compara_estado_oficial.md), [discovery](runbook_onboarding_copilot.md) e [marketing/editorial](guia_de_mkt.md).
7. [Privacidade](lgpd_governanca_tecnica.md), [IA externa](integracoes_ia_privacidade.md), [governança IA](cleiton_ai_data_governance.md), [observabilidade](runtime_ia_e_observabilidade.md) e [troubleshooting](troubleshooting_operacional.md).

## Módulos principais

- `app/web.py`: Flask, home, autenticação (`_safe_next_redirect`), Roberto, cron, webhook e health.
- `app/shell_navigation.py`: catálogo único de Início / Habilidades / Feed.
- `app/models.py`: Conta, User, Franquia, vínculos e operações comerciais.
- `app/conta_multiuser_*` e `app/services/conta_multiuser_*`: Multiuser.
- `app/julia_*`, `app/cleide_audit_*`, `app/agente_compara_*`: domínios separados (nomes técnicos).
- `app/editorial_metadata.py`, `app/run_cleiton_agente_*`, `app/run_julia_agente_*`: pipeline editorial / evergreen / imagem.
- `app/cleiton_doc_store.py`: infraestrutura documental comum com ownership.
- `app/services/cleiton_ai_data_governance.py`: fronteira outbound.

## Premissas para desenvolvimento

Use `APP_ENV=dev` e banco de desenvolvimento. Homolog: `homolog` / `APP_ENV=homolog`; produção: `producao` / `APP_ENV=prod`. Ambos Auto-Deploy. `start.sh` aplica `db upgrade` antes do Gunicorn. Head versionado: `g8h9i0j1k2l3` (`down_revision=f7g8h9i0j1k2`).

`load_app_env()` carrega `app/.env.{APP_ENV}` com `override=False` — variáveis do processo vencem o arquivo. Não tratar `.env.prod` local como configuração do Render.

Identidade pública do chat: AgenteFrete. Não reintroduzir “Júlia” como marca no UI operacional. Tema global via `af-theme`; sem opt-in por página.

## Regras de integridade

- Preservar namespaces, usuário e sessão; `conta_id` sozinho não autoriza documentos privados.
- Preservar histórico e consumo em revogação/reentrada.
- Manter `temp_table` como estado temporário com revisão explícita.
- Não versionar `.env` reais, bancos locais, caches ou artefatos técnicos.
- Selecionar testes existentes relacionados à mudança (há cobertura para tema, auth `/fretes`, identidade AgenteFrete, evergreen, imagem, checkout Multiusuário, mobile). Evitar tratar contagens globais de testes como verdade permanente em guias.

`app/copilot_capabilities.md` é prompt de runtime. Relatórios históricos (classe B no índice) não são especificação vigente.
