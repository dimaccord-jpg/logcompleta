# Onboarding Tecnico

Data de consolidacao: `2026-05-29`  
Commit de referencia: `20fa165`

## 1. Objetivo

Acelerar a entrada tecnica no projeto sem criar leituras paralelas da arquitetura.

## 2. Ordem de leitura

1. `README.md`
2. `docs/auditoria_documental_2026-05-29.md`
3. `docs/estado_oficial_consolidado.md`
4. `docs/arquitetura_oficial.md`
5. `docs/runtime_ia_e_observabilidade.md`
6. `docs/runbook_onboarding_copilot.md`
7. `migrations/README`

## 3. Modulos principais para o estado atual

- `app/web.py`: superficies publicas, onboarding discovery, Julia e Roberto
- `app/static/js/chat_behavior.js`: shell visual do Copilot e handoff para Julia
- `app/run_cleiton_discovery.py`: discovery conversational-first
- `app/copilot_capabilities.md`: documento oficial de capacidades do Copilot
- `app/copilot_capabilities.py`: guardrails e regra por atividade-fim
- `app/painel_admin/admin_routes.py`: dashboard admin e hidden terms
- `app/services/ia_metrics_service.py`: metricas de IA
- `app/services/onboarding_admin_analytics_service.py`: word cloud do onboarding
- `app/services/onboarding_word_cloud_hidden_terms_service.py`: ocultacao/reexibicao de termos
- `app/models.py`: modelos de eventos e hidden terms

## 4. Premissas de runtime

- `APP_ENV` e obrigatorio e aceita `dev`, `homolog`, `prod`
- `DATABASE_URL` deve ser PostgreSQL
- `SECRET_KEY` forte em `homolog` e `prod`
- `PUBLIC_BASE_URL` define a base publica oficial
- `APP_DATA_DIR` governa persistencia operacional

## 5. O que mudou no estado atual

- a Home publica usa Copilot de discovery, nao Julia operacional;
- o Copilot aceita sessao anonima;
- existe limite de 5 interacoes anonimas por sessao;
- `Nova conversa` reseta o estado do discovery;
- Julia so entra como agente operacional real com login;
- onboarding passou a ter observabilidade e contagem de tokens separadas;
- o dashboard admin passou a mostrar nuvem de palavras do onboarding;
- termos da nuvem podem ser ocultados e reexibidos sem apagar historico.

## 6. Regras que nao podem ser quebradas

- nao confundir Copilot com Julia;
- nao encaminhar por artefato;
- nao fazer onboarding abater franquia;
- nao apagar historico bruto da word cloud;
- nao criar rota paralela para dashboard ou hidden terms;
- nao mascarar falha do discovery com comportamento inventado.

## 7. Ambientes

### Local

- usar `APP_ENV=dev`
- validar Home, onboarding, dashboard e migrations localmente

### Homolog

- usar branch `homolog`
- validar onboarding discovery, Julia, Roberto, Cleide, admin e migrations

### Producao

- branch `producao`
- estado de referencia deste pacote documental: commit `20fa165`

## 8. Validacao minima de entrada

- abrir `/` e validar Copilot discovery
- confirmar limite anonimo de 5 interacoes
- validar CTA de login ao atingir limite
- validar `Nova conversa`
- validar handoff para Julia
- validar `/admin/dashboard`
- validar hidden terms do onboarding
- validar Roberto e Cleide
