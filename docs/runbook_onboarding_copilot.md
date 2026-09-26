# Runbook Onboarding / Discovery

Revisão funcional: produção `fa98317` (Sprint 12).

O trilho público de discovery é frequentemente chamado de “Copilot” no código e em prompts (`app/copilot_capabilities.md`). **Copilot não é a identidade pública do produto** — a marca é AgenteFrete; o discovery é o modo anônimo da home.

## Arquitetura atual

Fluxo oficial:

`Home pública → discovery AgenteFrete → Gemini governado ou fallback local → resposta natural → handoff opcional (habilidade) → login se necessário → superfície operacional`

Superfícies:

- Discovery (home): `/`
- API discovery: `POST /api/onboarding_discovery`
- Reset: `POST /api/onboarding_discovery/reset`
- Chat operacional AgenteFrete: `/chat_julia?mode=operational` (nome de rota técnico)

## Limites

- sessão anônima permitida
- limite de `5` interações anônimas por sessão
- ao atingir o limite, o backend bloqueia novas chamadas discovery e devolve CTA de login

## Habilidades e handoffs

Regra-mãe:

- artefato não define agente
- atividade-fim e horizonte temporal definem o destino

Encaminhamentos (rótulos externos / destinos):

| Habilidade | Destino | Auth |
|---|---|---|
| Consultar o AgenteFrete | `/chat_julia?mode=operational` | autenticado |
| Analisar fretes | `/fretes` | autenticado |
| Auditar cobranças de frete | `/auditoria-frete` | autenticado (APIs) |
| Comparar tabelas | `/agente-compara` | autenticado (APIs) |
| Feed | `/feed` | público |

Anônimo recebe `/login?next=<destino canônico>`. `_safe_next_redirect` rejeita destino externo.

O documento `app/copilot_capabilities.md` é consumido pelo runtime como prompt; sua alteração é funcional. Descrição canônica: [AGENTS](AGENTS.md).

## Continuidade após login

Quando o usuário autentica e continua no trilho operacional:

- a experiência autenticada principal é o **AgenteFrete** (não se apresenta como Júlia)
- o contexto do onboarding só é preservado quando o handoff for `julia_operational` (ID técnico)
- a experiência documental autenticada fica no trilho logado, não no onboarding público
- histórico conversacional do chat operacional permanece client-side (sem threads persistentes de produto)

## Observabilidade

- `IaConsumoEvento`
- `ProcessingEvent`
- `AuditoriaGerencial`

Fluxos rastreados: `onboarding_discovery`, `operacional`, `administrativo`.

Regra crítica:

- onboarding conta como consumo interno
- onboarding não abate franquia

## Multiuser e privacidade

A continuidade logada respeita o usuário, sua Franquia e a sessão. Pertencer à mesma Conta não transfere memória ou documentos de outro membro. O [Multiuser V1](multiuser_v1.md) não altera essa separação.
