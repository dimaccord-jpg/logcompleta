# Runbook Onboarding Copilot

Data de consolidacao: `2026-05-29`  
Commit de referencia: `20fa165`

## 1. Arquitetura atual

Fluxo oficial:

`Home publica -> Copilot discovery -> Gemini governado ou fallback local -> resposta natural -> handoff opcional`

Superficies envolvidas:

- Copilot: `/`
- API discovery: `POST /api/onboarding_discovery`
- Reset: `POST /api/onboarding_discovery/reset`
- Julia operacional: `/chat_julia?mode=operational`
- Dashboard admin: `/admin/dashboard`

## 2. Limites

- sessao anonima permitida;
- limite de 5 interacoes anonimas por sessao;
- ao atingir o limite, o backend bloqueia novas chamadas Gemini;
- o usuario recebe CTA de login para continuar gratuitamente com Julia;
- `Nova conversa` reseta o estado do discovery.

## 3. Handoffs

Regra-mae:

- artefato nao define agente;
- atividade-fim e horizonte temporal definem agente.

Encaminhamentos:

- Roberto: previsao, projecao, tendencia, horizonte futuro
- Cleide: auditoria, desvios, conferencia, horizonte historico
- Julia: estrategia, negociacao, supply chain, interpretacao executiva

## 4. Observabilidade

Eventos oficiais:

- `IaConsumoEvento`
- `ProcessingEvent`
- `AuditoriaGerencial`

Fluxos rastreados:

- `onboarding_discovery`
- `operacional`
- `administrativo`

## 5. Metricas

No dashboard admin, validar:

- `operational_tokens_month`
- `onboarding_tokens_month`
- `total_internal_tokens_month`

Regra critica:

- onboarding conta como consumo interno;
- onboarding nao abate franquia.

## 6. Dashboard admin

Blocos esperados:

- consumo IA operacional;
- consumo IA onboarding;
- total interno;
- tokens por chave;
- processamento Roberto;
- processamento Cleide;
- analise de termos do onboarding.

## 7. Nuvem de palavras

Origem:

- `AuditoriaGerencial.tipo_decisao = "onboarding_discovery"`
- `user_terms_normalized` em `contexto_json`

Pipeline:

`normalizacao -> stopwords -> hidden terms -> frequencia -> Pareto 80/20`

Controles administrativos:

- ocultar termo;
- reexibir termo;
- preservar historico bruto.

Persistencia:

- modelo `OnboardingWordCloudHiddenTerm`
- migration `r2s3t4u5v6w7`

## 8. Troubleshooting rapido

### Copilot nao responde

- validar `POST /api/onboarding_discovery`
- validar Gemini
- validar fallback local

### Limite anonimo falhou

- validar contador de sessao
- validar limite `5`
- validar reset

### Julia nao recebeu contexto

- validar se houve handoff `julia_operational`
- validar armazenamento em sessao do contexto de onboarding

### Word cloud vazia

- validar `AuditoriaGerencial`
- validar termos normalizados
- validar filtros de stopwords/hidden terms

### Hidden term nao funcionou

- validar migration
- validar tabela `onboarding_word_cloud_hidden_term`
- validar `is_active`

## 9. Promocao homolog -> producao

Checklist:

1. aplicar migrations ate `head`
2. validar Home com discovery
3. validar limite de 5 interacoes
4. validar CTA de login
5. validar handoff Julia
6. validar dashboard admin
7. validar word cloud e hidden terms
8. validar Roberto
9. validar Cleide
10. validar logs e eventos de observabilidade
