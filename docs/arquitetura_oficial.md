# Arquitetura Oficial

Referência arquitetural auditada em 2026-07-28. A fonte oficial de estado e contratos é `docs/estado_oficial_consolidado.md`.

## Mapa de superfícies

| Superfície | Acesso | Responsabilidade |
|---|---|---|
| `/` | público/logado | discovery Cleiton e handoff |
| `/chat_julia?mode=operational` | autenticado | consultoria operacional da Júlia |
| `/fretes` | autenticado | BI e chat do Roberto |
| `/auditoria-frete` | página pública; API protegida | auditoria operacional da Cleide |
| `/agente-compara` | página pública; API protegida | comparação multitabela do AgenteCompara |
| `/admin/...` | admin | billing, métricas, custos e observabilidade |

## Aplicação e blueprints

`app/web.py` registra os blueprints de admin, operações, usuário, Cleide, AgenteCompara e documentos da Júlia. Roberto continua sem blueprint próprio separado.

## Camadas estruturais

- camada web Flask em `app/web.py` e blueprints;
- camada de domínio do AgenteCompara em `app/agente_compara_*`;
- trilho documental técnico compartilhado do Cleiton em `app/cleiton_doc_*`;
- métricas e billing operacional em serviços e modelos próprios;
- banco PostgreSQL para persistência transacional;
- JSON temporário para documentos, `temp_table`, lotes e resultado comparativo fora da sessão.

## AgenteCompara

O AgenteCompara implementa arquitetura em camadas:

- rotas HTTP em `app/agente_compara_api_routes.py`;
- estado multitabela em `app/agente_compara_comparison_state.py`;
- upload, revisão, coverage e arquivo operacional em `app/agente_compara_doc_service.py`;
- extração técnica da `temp_table` em `app/run_agente_compara_temp_table.py`;
- cálculo unitário em `app/agente_compara_calculation_service.py`;
- orquestração multitabela em `app/agente_compara_comparison_calculation_service.py`;
- execução, lock, idempotência, storage e billing em `app/agente_compara_calculation_execution_service.py`, `app/agente_compara_calculation_lock.py` e `app/agente_compara_calculation_result_storage.py`.

## Isolamento entre agentes

- Júlia, Cleide e AgenteCompara usam namespaces de sessão distintos;
- AgenteCompara usa `flow_type`, billing e eventos de processamento próprios;
- não há compartilhamento de `comparison_id`, `table_id`, `temp_table_id` ou lotes entre AgenteCompara e Cleide;
- o isolamento é reforçado por testes específicos do domínio.

## Infraestrutura versionada

- `start.sh` aplica `db upgrade` antes do Gunicorn;
- `render.yaml` versionado aponta homolog para `homolog` e produção para `main`;
- o código expõe `/health/liveness` e `/health/readiness`.

A diferença entre branch operacional informada de produção (`producao`) e branch versionada no YAML (`main`) continua sendo uma divergência operacional, não uma equivalência comprovada em código.
