# Agentes e Identidades

Documentação revisada em 2026-09-23 (Sprint 12). Separa identidade pública, identidade interna e escopo operacional no código atual.

## Identidade do produto (PUBLIC)

- marca pública: **AgenteFrete**
- definição pública: **Assistente virtual especializado em logística da LogCompleta**
- empresa: `Logcompleta Agentes Inteligentes LTDA`
- o usuário **não** precisa conhecer nomes de agentes internos
- habilidades são apresentadas como capacidades do AgenteFrete, não como produtos separados
- a plataforma não é TMS, WMS nem ERP

## Identidades internas (INTERNAL)

Preservadas por compatibilidade arquitetural. **Não** são marcas públicas nem produtos distintos:

| Nome interno | Papel |
|---|---|
| Júlia | Persona editorial / módulos técnicos `julia_*`, rotas `/chat_julia`, `/api/chat_julia` |
| Roberto | BI e chat quantitativo em `/fretes` |
| Cleide | Identidade técnica/histórica da auditoria (`/api/cleide-auditoria/*`, services, `flow_type`) |
| AgenteAudita | Nome público da superfície de auditoria (mesmo domínio Cleide) |
| Cleiton | Governança transversal, discovery, billing, franquias, orquestração editorial |
| AgenteCompara | Comparação multitabela |

Não renomear módulos apenas para “bater” com a marca pública.

## Home pública e jornada

- home `/` usa discovery e reforça AgenteFrete
- cards de habilidade (Home): Analisar fretes, Auditar cobranças de frete, Comparar tabelas
- shell também oferece **Consultar o AgenteFrete** (chat operacional)
- CTA experimental `home_chat_cta_v1` permanece
- o fluxo público **não** deve ser documentado como “Julia pública principal”
- discovery permanece público; habilidades operacionais exigem login conforme taxonomia

## Cleiton

Escopo atual confirmado:

- governança transversal de discovery, billing técnico, franquias e observabilidade
- trilho oficial do onboarding/discovery na home
- contratos documentais reutilizados pelos módulos internos
- autorização operacional por franquia antes de consumo
- reconciliação e validação de franquia
- logs, eventos de processamento, automações cron e orquestração editorial (incl. side-car evergreen)

O que não deve ser atribuído ao Cleiton sem evidência:

- identidade pública principal da home
- ownership funcional exclusivo de AgenteAudita/Cleide, AgenteCompara ou Roberto

## Chat operacional AgenteFrete (módulo técnico Julia)

Estado atual:

- superfície autenticada principal: `/chat_julia?mode=operational`
- endpoint: `/api/chat_julia`
- system prompt e labels de UI: identidade **AgenteFrete** (“Nunca se apresente como Júlia”)
- para anônimos: marca AgenteFrete + discovery; não Júlia como rosto público
- bloqueio do endpoint: “É necessário estar logado para conversar com o AgenteFrete.”
- histórico: client-side; sem threads persistentes de produto
- Júlia permanece persona **editorial** (pipeline `/noticia/<id>`, admin editorial)

Leitura recomendada:

- **AgenteFrete** = identidade priorizada em home, CTAs, chat operacional e mensagens ao usuário
- **Júlia** = nome interno/editorial e surface técnica preservada
- uso documental autenticado com contexto temporário governado pelo Cleiton

### Orientação determinística para ferramentas internas

Após a resposta normal do chat:

- não altera o motor conversacional
- não cria segunda chamada LLM
- usa o capability resolver local e URLs da taxonomy
- destinos: AgenteAudita → `/auditoria-frete`; Roberto → `/fretes`; AgenteCompara → `/agente-compara`
- abre em nova aba; ambíguos sem handoff; fail-open

## AgenteAudita

Identidade pública: `AgenteAudita`. Identidade técnica/histórica: `Cleide`.

- página: `/auditoria-frete`
- APIs: `/api/cleide-auditoria/*`
- upload, `temp_table`, coverage opcional, lote, BI executivo e chat analítico
- isolamento por usuário, franquia, sessão, billing e artefatos

Em texto de produto usar AgenteAudita; em código/endpoints/histórico manter Cleide quando tecnicamente correto.

## Roberto

- rota `/fretes` (**autenticada**; SCRUM-211)
- upload, BI e chat quantitativo
- documentar somente o implementado; previsibilidade ampla é roadmap se não comprovada no runtime

## AgenteCompara

- página `/agente-compara`
- 2 tabelas obrigatórias + 1 opcional
- `comparison_id`, `table_id`, `slot`; revisão, coverage, cálculo, analytics e chats
- isolamento forte frente ao chat AgenteFrete e ao domínio técnico Cleide
- não prometer decisão automática, contratação ou concorrência de mercado não suportadas

## Editorial (persona Júlia)

- intenções: `news`, `analysis`, `evergreen`
- admin editorial permite Analysis/Evergreen e `pauta_id` explícito (fail-closed se inválido)
- evergreen automático via `ConfigRegras` (`evergreen_automatico_habilitado`, `evergreen_frequencia_minutos`)
- imagem: `gemini-3.1-flash-image` / `generate_content`

Detalhes em [estado de produção](estado_producao.md) e [marketing/SEO](guia_de_mkt.md).

## Roadmap versus implementação

- implementado agora: rotas, serviços, templates, models e testes atuais
- roadmap: capacidades futuras sem evidência no runtime — nunca como fato

## Isolamento Multiuser em produção

Multiuser V1 não altera as responsabilidades acima. Conta comum não compartilha memória ou artefatos privados.

- Cleiton governa autorização por Franquia individual; não concede leitura coletiva
- o chat AgenteFrete (módulo Julia) mantém contexto do usuário/sessão autenticados
- Cleide/AgenteAudita e AgenteCompara mantêm artefatos no ownership autorizado
- `conta_id` sozinho não autoriza artefatos privados

Contrato completo em [Multiuser V1](multiuser_v1.md).
