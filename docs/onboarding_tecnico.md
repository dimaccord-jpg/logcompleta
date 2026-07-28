# Onboarding Tecnico

Data de consolidação: `2026-07-20`
Commit local de referência auditado: `c2575f8`

## Ordem de leitura

1. `README.md`
2. `docs/estado_oficial_consolidado.md`
3. `docs/arquitetura_oficial.md`
4. `docs/cleide_auditoria_operacional.md`
5. `docs/runtime_ia_e_observabilidade.md`
6. `docs/troubleshooting_operacional.md`
7. `app/copilot_capabilities.md`
8. `app/README_RUN.md`
9. `app/README_DEPLOY.md`
10. `docs/runbooks/cleide_homologacao_controlada_checklist.md`

## Módulos principais do estado atual

- `app/web.py`: home, auth, Roberto, cron, health e rotas centrais
- `app/julia_documents_routes.py`: API documental da Júlia
- `app/cleide_audit_routes.py`: fluxo documental/auditoria da Cleide
- `app/agente_compara_api_routes.py`: fluxo documental/auditoria do Agente Compara
- `app/cleiton_doc_store.py`: store temporário compartilhado
- `app/services/*`: governança operacional, billing, custos, métricas e configurações

## Premissas de arquitetura

- home pública usa discovery Cleiton;
- home logada usa Júlia operacional;
- Roberto permanece no `web.py` principal;
- Cleide Auditoria e Agente Compara usam namespaces paralelos e isolados;
- `source_agent` e `session_key` protegem ownership documental;
- `temp_table` é estado temporário, não persistência definitiva;
- health check real exposto pelo código está em `/health/liveness` e `/health/readiness`.

## Regras que não podem ser quebradas

- não versionar `app/cleiton_doc_tmp/`, `tt_*.json`, `.cleanup_meta.json` ou `.db` local;
- não documentar schema inexistente;
- não misturar os namespaces de Júlia, Cleide e Agente Compara;
- não atribuir ao chat a responsabilidade de alterar `temp_table` sem fluxo explícito de revisão/correção;
- não tratar o YAML de produção como fonte absoluta quando ele divergir do processo validado.
