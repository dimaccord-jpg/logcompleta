# Execucao Local

Data de consolidacao: `2026-07-10`
Commit de referencia: `d02ce15`

## Objetivo

Executar localmente e validar Home, Julia, Roberto, Cleide BI, Cleide Auditoria e governanca documental sem depender de leitura documental antiga.

## Premissas

- `APP_ENV=dev`
- `DATABASE_URL` apontando para PostgreSQL do ambiente de trabalho
- `PUBLIC_BASE_URL` coerente com o host local
- `APP_DATA_DIR` configurado

## Validacao manual minima

1. abrir `/` deslogado e validar o Copilot de discovery
2. confirmar limite de `5` interacoes anonimas e reset via `Nova conversa`
3. fazer login e reabrir `/`
4. validar que a Home logada mostra a Julia operacional
5. validar `POST /api/chat_julia`
6. validar upload documental da Julia e bloqueios por franquia/plano quando aplicavel
7. validar `/fretes` com upload privado, BI e chat do Roberto
8. validar `/cleide-bi-frete` como superficie separada da auditoria
9. validar `/auditoria-frete` com:
- upload documental
- status documental
- `temp_table`
- revisao humana governada
- etapa fiscal quando aplicavel
- coverage opcional quando aplicavel
- upload do lote auditado
- `audit/run`
- preview/apply/undo de correcao quando houver diagnostico suportado
10. validar `/admin/agentes/cleiton` e `/admin/agentes/cleide`
11. validar `/admin/dashboard`

## Pontos de atencao

- a Home publica continua sendo discovery, nao Julia operacional
- a Home logada e a superficie principal da Julia
- `/chat_julia?mode=operational` continua valido para handoff e acesso direto
- a `temp_table` da Cleide e temporaria e governada pelo dominio Cleiton
- o chat da Cleide nao recria nem controla o ciclo de vida da `temp_table`
- a pagina `/auditoria-frete` e publica, mas os endpoints operacionais sao privados e autorizados por franquia
- o BI executivo da auditoria em `/auditoria-frete` tem 4 graficos de impacto financeiro; nao confundir com o BI estrutural de `/cleide-bi-frete`
- `app/cleiton_doc_tmp/` e temporario, local e ignorado no Git
- `.db`, caches, `__pycache__`, `.pytest_cache` e temporarios locais nao fazem parte do estado oficial
