# Execucao Local

Data de consolidacao: `2026-06-19`
Commit de referencia: `17675d0`

## Objetivo

Executar o projeto localmente e validar o estado atual de Home, onboarding, Julia operacional logada, Cleide Auditoria e governanca documental.

## Premissas

- `APP_ENV=dev`
- `DATABASE_URL` apontando para PostgreSQL
- `PUBLIC_BASE_URL` coerente com o host local
- `APP_DATA_DIR` configurado

## Validacao manual minima

1. abrir `/` deslogado e validar o Copilot de discovery
2. confirmar limite de `5` interacoes anonimas e reset via `Nova conversa`
3. fazer login e reabrir `/`
4. validar que a Home logada mostra a Julia operacional
5. validar conversa em `POST /api/chat_julia`
6. validar botao de documentos na UI da Julia
7. anexar `TXT`, `XML`, `CSV`, `XLSX`, `DOCX` e `PDF` conforme ambiente permitir
8. validar bloqueio por franquia/plano quando aplicavel
9. validar `/admin/agentes-cleiton` com o bloco documental
10. validar `/auditoria-frete` com upload, status e tabela temporaria extraida
11. validar que a tabela temporaria continua separada do chat da Cleide
12. validar que a tabela temporaria aparece como card clicavel no painel de anexos/documentos
13. abrir o modal e validar modo somente leitura com revisao/validacao humana
14. validar revisao humana da tabela temporaria quando o fluxo exigir ajuste
15. validar `/admin/dashboard`, `/fretes` e `/cleide-bi-frete`

## Pontos de atencao

- a Home publica continua sendo discovery, nao consumo operacional da Julia
- a Home logada consolida a Julia como superficie operacional principal
- `/chat_julia?mode=operational` continua valido para handoff e acesso direto
- documentos da Julia dependem de autenticacao e autorizacao operacional
- a Cleide Auditoria tenta extrair tabela temporaria apos upload e exige validacao humana
- a validacao da tabela temporaria e humana e governada; nao e uma nova conversa de IA
- o chat da Cleide consulta contexto, mas nao e owner da tabela temporaria
- `app/cleiton_doc_tmp/` e temporario, local e ignorado no Git
- o `.gitignore` protege `app/cleiton_doc_tmp/`
- `tt_*.json`, `.cleanup_meta.json` e outros `.json` dessa pasta nao devem entrar em commit
- `app/.tmp_repro_unit*` nao deve entrar em commit
- configuracao documental usa `ConfigRegras`, sem migration nova
- nenhum `.db` local faz parte do estado oficial
