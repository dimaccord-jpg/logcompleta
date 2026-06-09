# Runbook Onboarding Copilot

Data de consolidacao: `2026-06-05`
Commit de referencia: `b5fc444`

## Arquitetura atual

Fluxo oficial:

`Home publica -> Copilot discovery -> Gemini governado ou fallback local -> resposta natural -> handoff opcional para Julia`

Superficies envolvidas:

- Copilot: `/`
- API discovery: `POST /api/onboarding_discovery`
- Reset: `POST /api/onboarding_discovery/reset`
- Julia operacional: Home logada e `/chat_julia?mode=operational`

## Limites

- sessao anonima permitida
- limite de `5` interacoes anonimas por sessao
- ao atingir o limite, o backend bloqueia novas chamadas discovery e devolve CTA de login

## Handoffs

Regra-mae:

- artefato nao define agente
- atividade-fim e horizonte temporal definem agente

Encaminhamentos:

- Roberto: previsao e tendencia futura
- Cleide: auditoria e leitura retrospectiva
- Julia: estrategia, negociacao, supply chain e interpretacao executiva

## Integracao com Julia

Quando o usuario autentica e continua no trilho operacional:

- a Home logada passa a operar com Julia
- o contexto do onboarding so e preservado quando o handoff for `julia_operational`
- a experiencia documental da Julia fica no trilho logado, nao no onboarding publico

## Observabilidade

- `IaConsumoEvento`
- `ProcessingEvent`
- `AuditoriaGerencial`

Fluxos rastreados:

- `onboarding_discovery`
- `operacional`
- `administrativo`

Regra critica:

- onboarding conta como consumo interno
- onboarding nao abate franquia
