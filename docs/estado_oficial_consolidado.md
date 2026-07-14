# Estado Oficial Consolidado

Data de consolidacao: `2026-07-10`
Commit de referencia: `homolog@d02ce15` e `producao@6efa2e2`

## Escopo confirmado

- Copilot de discovery na Home publica
- Home logada consolidada como superficie operacional da Julia
- Julia documental governada pelo Cleiton
- Roberto com upload, BI e chat privados em `/fretes`
- Cleide BI estrutural em `/cleide-bi-frete`
- Cleide Auditoria em `/auditoria-frete`
- observabilidade de IA e processamento mantida

## Estado de ambiente

- `homolog` contem `d02ce15`
- `origin/homolog` esta sincronizada com `homolog`
- `producao` contem `6efa2e2`
- o `render.yaml` versionado define producao em `main` com deploy manual
- nao houve migration nova
- nao houve schema novo
- nao houve tabela nova
- nao houve campo novo

## Cleide Auditoria

- upload documental, `temp_table`, coverage opcional, lote auditado e chat estao ativos
- a `temp_table` continua artefato temporario governado pelo dominio Cleiton
- a extracao tecnica permanece separada do chat
- a revisao humana pode editar e salvar a tabela antes de avancar
- ha correcoes assistidas com preview, apply e undo
- o BI executivo da auditoria usa 4 graficos:
- Impacto Financeiro por Transportadora
- Impacto Financeiro por UF Destino
- Evolucao do Impacto Financeiro no Periodo
- Pareto do Valor Cobrado a Mais

## Admin

- `/admin/agentes/cleide` separa configuracao do BI estrutural e da auditoria documental
- `/admin/agentes/cleiton` continua dono dos limites globais documentais, TTL, cleanup e limites por tipo
- persistencia continua em `ConfigRegras`

## Git e temporarios

- `app/cleiton_doc_tmp/` permanece temporaria e ignorada
- `tt_*.json`, caches e residuos tecnicos nao entram em commit
- `.db`, `__pycache__` e `.pytest_cache` nao fazem parte do estado oficial

## Testes verificados nesta auditoria documental

- suite especifica da auditoria Cleide: `804 tests collected`
- suite completa do repositório: `1660 tests collected`
