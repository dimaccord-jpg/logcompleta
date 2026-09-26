# Troubleshooting Operacional

Revisão documental: árvore de produção `fa98317` (Sprint 12).

## 1. Home pública sem responder

Conferir:

1. `POST /api/onboarding_discovery`
2. chaves/configuração do discovery
3. fallback local previsto para discovery

## 2. Home / chat não mostra a identidade correta

Comportamento esperado:

- usuário anônimo: discovery público AgenteFrete (às vezes chamado Copilot no código)
- usuário logado no chat operacional: identidade **AgenteFrete** (não Júlia)
- rota: `/chat_julia?mode=operational` (nome técnico)
- labels/prompt: AgenteFrete; Júlia é editorial/interna

## 3. Handoff e login com `next` falham

Conferir:

1. `app/shell_navigation.py` e `app/capability_taxonomy.py`
2. `_safe_next_redirect` / `_login_href` em `app/web.py`
3. `tests/test_auth_next_redirect.py` e `tests/test_scrum211_fretes_login_gate.py`
4. destino externo deve ser rejeitado; `/fretes` exige login

## 4. Upload documental do chat autenticado falha

Conferir:

1. autenticação do usuário
2. autorização por franquia
3. `upload_enabled`
4. limites de sessão e arquivo
5. tipos suportados
6. TTL e cleanup

## 5. PDF parece lido quando não deveria

Conferir:

1. `app/cleiton_doc_gemini_files.py`
2. `app/julia_doc_context.py`
3. cobertura de testes documentais/PDF
4. governança Cleiton (fail-closed) em [cleiton_ai_data_governance](cleiton_ai_data_governance.md)

## 6. Arquivos temporários apareceram no Git

Conferir:

1. `.gitignore`
2. diretórios temporários de runtime
3. arquivos `tt_*.json`
4. metadados de cleanup
5. bancos locais temporários

## 7. Health check do Render falha

Conferir:

1. `render.yaml`
2. `app/web.py`
3. rotas `/health`, `/health/liveness` e `/health/readiness`

## 8. Branch errada no Render

Conferir:

1. `render.yaml`
2. painel do Render
3. branch conectada: homolog → `homolog`; produção → `producao`

## 9. Tema incorreto (claro/escuro)

Esperado:

- preferência em `localStorage` `af-theme`: `system` | `dark` | `light`
- templates `base.html` respeitam preferência global
- admin e `/acesso-desktop` fora do contrato
- se a UI “sempre dark” em página do shell, verificar se o template herda `base.html` e se o init `af-theme-init` está presente

## 10. Evergreen não dispara / pauta errada

Conferir:

1. admin Júlia: intenção evergreen / analysis e `pauta_id`
2. `ConfigRegras`: `evergreen_automatico_habilitado`, `evergreen_frequencia_minutos`
3. ID inválido deve falhar fechado (sem fallback)
4. cadência independente de `decidir_tipo_missao`
5. testes `test_scrum_215a_*` / `test_scrum_215b_*`

## 11. Imagem editorial 404 / modelo obsoleto

Esperado: `GEMINI_MODEL_IMAGE` default `gemini-3.1-flash-image` via `generate_content`.

Conferir:

1. variável no **Render** (não assumir `.env.prod` local)
2. `app/run_julia_agente_imagem.py`
3. `scripts/diagnose_julia_image_provider.py`
4. não usar `imagen-3.0-generate-002` como default atual

## 12. Checkout Multiusuário com iframe residual

Esperado: ao escolher Multiusuário, checkout Starter/Pro é destruído; formulário aparece; checkout só após “Ir para o checkout”.

Conferir `contrate_plano.html` e `tests/test_scrum216_checkout_cleanup.py`.

## 13. `/acesso-desktop` parece obrigatório

Não é. É landing de campanha opcional/legada. O fluxo principal é Home → habilidade → login → operacional.

## 14. Multiuser / billing

Seguir [Multiuser V1](multiuser_v1.md), [monetização](guia_monetizacao_franquias.md) e [estado de produção](estado_producao.md).

## Checklist rápido pós-deploy

1. `/health` ok
2. login + `next` interno
3. shell/habilidades + tema
4. chat AgenteFrete
5. `/fretes` exige auth
6. Feed / uma `/noticia/<id>`
7. checkout Starter e fluxo Multiusuário limpo
8. isolamento entre chat AgenteFrete, Cleide e AgenteCompara quando a mudança tocar esses domínios
