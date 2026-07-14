# Arquitetura Oficial

Data de consolidacao: `2026-07-10`
Commit de referencia: `homolog@d02ce15`

## Superficies

- `/`: discovery publico ou Julia operacional, conforme autenticacao
- `/fretes`: Roberto
- `/cleide-bi-frete`: BI estrutural da Cleide
- `/auditoria-frete`: auditoria documental da Cleide
- `/admin/...`: operacao administrativa

## Separacao de dominios

### Julia

- superficie operacional principal do usuario autenticado
- usa governanca Cleiton para autorizacao, contexto documental e consumo IA

### Roberto

- leitura quantitativa, previsao e interpretacao analitica de fretes
- upload, BI e chat privados em `/fretes`

### Cleide BI

- superficie estrutural separada em `/cleide-bi-frete`
- upload de XLSX/CSV por sessao
- validacao estrutural
- KPIs e dashboard estrutural sobre dataset da sessao

### Cleide Auditoria

- superficie separada em `/auditoria-frete`
- pagina publica, com endpoints privados e autorizados
- upload documental, `temp_table`, coverage opcional, lote auditado, BI executivo da auditoria e chat contextual

### Cleiton

- governanca operacional central
- autorizacao por franquia
- identidade de consumo
- limites documentais
- TTL e cleanup
- observabilidade de IA e processamento
- ownership operacional da `temp_table` da Cleide Auditoria

## Rotas principais da Cleide Auditoria

- `GET /auditoria-frete`
- `POST /api/cleide-auditoria/documents/upload`
- `GET /api/cleide-auditoria/documents/status`
- `DELETE /api/cleide-auditoria/documents/<doc_id>`
- `POST /api/cleide-auditoria/documents/clear`
- `POST /api/cleide-auditoria/temp-table/save`
- `POST /api/cleide-auditoria/coverage/upload`
- `GET /api/cleide-auditoria/audit-template`
- `POST /api/cleide-auditoria/audit/upload`
- `POST /api/cleide-auditoria/audit/run`
- `POST /api/cleide-auditoria/audit/correction/preview`
- `POST /api/cleide-auditoria/audit/correction/apply`
- `POST /api/cleide-auditoria/audit/correction/undo`
- `POST /api/cleide-auditoria/chat`

## Arquitetura da auditoria

- o upload documental e governado pelos limites e TTL do Cleiton
- a extracao tecnica da `temp_table` ocorre apos upload e permanece separada do chat
- a `temp_table` e temporaria, descartavel e vinculada ao estado documental da sessao
- a `temp_table` nao e auditoria final nem persistencia definitiva
- o chat da Cleide consulta contexto documental; nao gere o ciclo de vida da `temp_table`
- a revisao humana pode editar tabela de frete, taxas acessorias, coverage e configuracao fiscal antes de avancar

## Responsabilidade dos modulos centrais

- `app/cleide_audit_doc_service.py`: store e ciclo de vida documental, `temp_table`, coverage, lote auditado, calculo da auditoria e payloads publicos
- `app/run_cleide_audit_temp_table.py`: extracao tecnica pos-upload, parsing JSON e fallback de modelo
- `app/cleide_audit_prompt.py`: prompt tecnico da extracao e prompt do chat
- `app/cleide_audit_routes.py`: API oficial da auditoria
- `app/cleide_audit_correction_service.py`: correcoes assistidas com preview, apply e undo
- `app/static/js/cleide_auditoria.js`: experiencia visual da auditoria, BI executivo, modal da `temp_table`, coverage e correcao
- `app/templates/cleide_auditoria.html`: contrato visual da pagina

## BI executivo da auditoria

O BI executivo dentro de `/auditoria-frete` possui 4 graficos:

- Impacto Financeiro por Transportadora
- Impacto Financeiro por UF Destino
- Evolucao do Impacto Financeiro no Periodo
- Pareto do Valor Cobrado a Mais

Base de calculo do BI:

- o dataset publico usa campos sanitizados de `audit_bi`
- o filtro ocorre no frontend em nivel de linha
- a visualizacao trabalha com impacto financeiro absoluto
- o contrato atual nao deve ser documentado como o BI antigo de 7 graficos

## Admin da Cleide

`/admin/agentes/cleide` contem dois blocos independentes:

- bloco `cleide_cfg_*` do BI estrutural
- bloco `cleide_audit_cfg_*` da auditoria documental

No bloco da auditoria, o codigo atual expoe configuracao de:

- habilitacao de chat e upload
- janela de historico
- limites de contexto e documentos considerados
- limite de pergunta
- comportamento sem documentos
- exibicao de documentos usados
- mensagem de fallback
- limites do arquivo auditado
- bases de calculo administrativas
