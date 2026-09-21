# Runbook Onboarding Copilot

Revisão: `2026-09-19`, release Multiuser V1 (`origin/producao`, `f28f28e`).

## Arquitetura atual

Fluxo oficial:

`Home pública -> Copilot discovery -> Gemini governado ou fallback local -> resposta natural -> handoff opcional conforme objetivo`

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

- Roberto: destino `/fretes`, com BI e chat quantitativo. A taxonomia/prompt menciona previsão, mas não deve ser tomada como prova de capacidades além do runtime.
- AgenteAudita (Cleide): auditoria de fretes e leitura retrospectiva em `/auditoria-frete`.
- AgenteFrete/Júlia: estratégia, negociação, supply chain e interpretação em `/chat_julia?mode=operational`.
- AgenteCompara: comparação multitabela em `/agente-compara`.
- Feed: notícias em `/feed`.

O documento `app/copilot_capabilities.md` é consumido pelo runtime como prompt; sua alteração é funcional. Descrição canônica dos agentes em [AGENTS](AGENTS.md).

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

## Multiuser e privacidade

A continuidade logada respeita o usuário, sua Franquia e a sessão. Pertencer à mesma Conta não transfere memória ou documentos de outro membro para o discovery ou para Júlia. O [Multiuser V1](multiuser_v1.md) não altera essa separação.
