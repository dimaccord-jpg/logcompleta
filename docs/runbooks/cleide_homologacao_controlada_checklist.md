# Cleide - Homologacao Operacional Controlada

Objetivo: validar comportamento real ponta-a-ponta antes da promocao definitiva, sem alterar arquitetura, endpoint, governanca, billing, observabilidade ou os modulos Roberto/Julia.

## Como usar

1. Executar a bateria automatizada da Cleide.
2. Executar o roteiro manual abaixo em ambiente de homolog.
3. Registrar `pass` ou `fail` por cenario.
4. Encerrar com classificacao final:
   - `A) homolog`
   - `B) producao`
   - `C) precisa ajuste`

## Evidencias automatizadas atuais

Cobertura automatizada ja existente no projeto:

- Contexto insuficiente e fallback seguro: `tests/test_cleide_controlled_chat_phase9.py`
- Upload valido, contexto seguro e analytics: `tests/test_cleide_upload_api.py`
- Limites de contexto e `max_chars`: `tests/test_cleide_chat_context.py`
- Modos `executivo` e `conservador`: `tests/test_cleide_chat_context.py`
- Toggle de `temporal` e `transportadora`: `tests/test_cleide_chat_context.py`
- Provider error, bloqueios semanticos e IA desligada: `tests/test_cleide_controlled_chat_phase9.py`
- Admin 403, salvar config e reabrir config: `tests/test_cleide_admin_routes.py`
- Observabilidade exposta no payload: `tests/test_cleide_controlled_chat_phase9.py` e `tests/test_cleide_phase2_ui.py`

## Checklist operacional

| ID | Cenario | Evidencia esperada | Tipo | Status |
|---|---|---|---|---|
| 1 | Sem upload. Pergunta: "Quais UFs possuem frete relacionado?" | `context_status=insufficient`, fallback coerente, sem crash, sem provider error | Auto + manual | [ ] |
| 2 | Upload valido pequeno | Resposta IA ou deterministica correta, PT-BR correto, chat sequencial correto | Auto + manual | [ ] |
| 3 | Upload grande | Contexto respeita `max_chars`, sem vazamento, sem travamento | Auto + manual | [ ] |
| 4 | Modo executivo | Menos detalhe, mais sintese | Auto | [ ] |
| 5 | Modo conservador | Mais protecao, menos blocos caros | Auto | [ ] |
| 6 | Desligar temporal | `temporal` ausente do contexto | Auto | [ ] |
| 7 | Desligar transportadora | `transportadora` ausente do contexto | Auto | [ ] |
| 8 | Provider error | Fallback governado, `fallback_used=true`, `error_code=provider_error` | Auto | [ ] |
| 9 | IA desligada | Modo controlado, sem chamada ao provider | Auto | [ ] |
| 10 | Pergunta fora dominio | Bloqueio | Auto | [ ] |
| 11 | Pergunta Roberto | Bloqueio | Auto | [ ] |
| 12 | Pergunta Julia | Bloqueio | Auto | [ ] |
| 13 | Pergunta operacional desconhecida | Gemini supervisionado quando habilitado | Auto | [ ] |
| 14 | Prompt com numeros | PT-BR correto | Auto | [ ] |
| 15 | Chat multiplas perguntas | Timeline preservada no front | Auto + manual | [ ] |

## Observabilidade a validar

Conferir em cada resposta JSON e no comportamento da UI:

- `flow_type`
- `ai_flow_type`
- `ai_used`
- `fallback_used`
- `policy_blocked`
- `context_status`
- `view_scope`
- `active_filters`
- `provider_error` via `error_code=provider_error`

## Admin

Validar:

- admin acessa `/admin/agentes/cleide`
- nao-admin recebe `403`
- salvar config
- reabrir config
- config refletir `chat_context`

## Roteiro manual minimo

1. Abrir `/auditoria-frete` autenticado com perfil autorizado.
2. Validar upload pequeno e conversar em sequencia com pelo menos 3 perguntas.
3. Validar upload grande e observar que a tela continua responsiva.
4. Alternar `executivo` e `conservador` no admin e confirmar mudanca no contexto exposto ao chat.
5. Desabilitar `temporal` e depois `transportadora` no admin, salvar e revalidar o payload do chat.
6. Simular indisponibilidade do provider em homolog e confirmar fallback governado.
7. Validar bloqueios para fora de dominio, Roberto e Julia.
8. Repetir com usuario nao-admin no admin e confirmar `403`.

## Registro final

- Checklist executado: `sim` ou `nao`
- Pass/fail por cenario: preencher tabela acima
- Riscos restantes: descrever
- Apta para:
  - `A) homolog`
  - `B) producao`
  - `C) precisa ajuste`
