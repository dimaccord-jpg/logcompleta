# Agentes e Identidades

Documentação auditada em 2026-09-04. Este guia separa identidade pública, identidade interna e escopo operacional dos agentes no código atual.

## Identidade do produto

- marca principal nas superfícies públicas: `AgenteFrete`
- empresa: `Logcompleta Agentes Inteligentes LTDA`
- a plataforma não é apenas uma coleção de chatbots; o código atual organiza capacidades especializadas com governança transversal do Cleiton

## Home pública e identidade estratégica atual

- a home pública usa discovery e reforça a marca AgenteFrete
- o template `app/templates/index.html` alterna o título entre discovery público e superfície operacional
- o CTA principal da home participa do experimento `home_chat_cta_v1`
- o fluxo público não deve ser documentado como "Julia pública principal"

## Cleiton

Escopo atual confirmado:

- governança transversal de discovery, billing técnico, franquias e observabilidade
- trilho oficial do onboarding/discovery na home
- contratos e configurações documentais reutilizadas por Julia, AgenteAudita/Cleide e AgenteCompara
- autorização operacional por franquia antes de consumo
- reconciliação e validação de franquia
- logs, eventos de processamento e parte das automações cron

O que não deve ser atribuído ao Cleiton sem evidência:

- identidade pública principal da home
- ownership funcional exclusivo de AgenteAudita/Cleide, AgenteCompara ou Roberto

## Julia e AgenteFrete

Estado atual:

- a superfície autenticada principal continua em `/chat_julia?mode=operational`
- o endpoint operacional é `/api/chat_julia`
- para usuário não autenticado, o sistema apresenta a marca AgenteFrete e o trilho de discovery, não a Julia como rosto público principal
- a mensagem de bloqueio do endpoint autenticado diz: "É necessário estar logado para conversar com o AgenteFrete."

Leitura recomendada para documentação:

- "Julia" permanece como identidade interna e superfície operacional autenticada
- "AgenteFrete" é a identidade priorizada na home, em CTAs públicos e em várias mensagens voltadas ao usuário
- o uso documental real da Julia acontece logado, com contexto temporário governado pelo Cleiton

### Orientação determinística para ferramentas internas

O AgenteFrete operacional possui orientação determinística para ferramentas internas após a resposta normal do chat.

Características confirmadas:

- não altera o motor conversacional
- não cria segunda chamada LLM
- usa o capability resolver local
- as URLs vêm da taxonomy
- destinos iniciais:
  - AgenteAudita → `/auditoria-frete` (handoff técnico `cleide_freight_audit`)
  - Roberto → `/fretes`
  - AgenteCompara → `/agente-compara`
- a ação abre a ferramenta em nova aba
- casos ambíguos não recebem handoff automático
- a resolução é fail-open

## AgenteAudita

Identidade pública da auditoria de fretes: `AgenteAudita`.

Identidade técnica/histórica/interna: `Cleide`.

Escopo atual:

- página principal: `/auditoria-frete`
- APIs autenticadas em `/api/cleide-auditoria/*`
- fluxo com upload documental, `temp_table`, coverage opcional, lote auditado, BI executivo e chat analítico
- isolamento por usuário, franquia, sessão, billing e artefatos
- billing operacional próprio no domínio Cleiton

Importante:

- em textos voltados ao produto/superfície pública, usar AgenteAudita
- em nomes técnicos, endpoints, arquivos, classes, services, agent IDs, `flow_type` ou contexto histórico, manter Cleide quando tecnicamente correto
- a documentação deve tratar a Auditoria de Fretes como superfície atual
- qualquer referência a BI Cleide legado precisa ser marcada como legado ou secundária

## Roberto

O que existe hoje:

- rota `/fretes`
- upload, BI e chat quantitativo autenticado
- configuração operacional própria
- suporte a leitura quantitativa de fretes

Separação obrigatória:

- implementado agora: superfície `/fretes`, upload, BI e chat
- visão futura: previsibilidade mais ampla, estratégia futura ou outros cenários não comprovados pelo runtime devem permanecer como roadmap, nunca como fato implementado sem confirmação

## AgenteCompara

O que existe hoje:

- página `/agente-compara`
- comparação de 2 tabelas obrigatórias e 1 opcional
- fluxo com `comparison_id`, `table_id` e `slot`
- revisão, coverage opcional, arquivo operacional, cálculo comparativo, analytics e chats separados
- isolamento forte frente a Julia e ao domínio técnico Cleide

Limite documental importante:

- o sistema já calcula e consolida resultados comparativos, mas a documentação não deve prometer decisão automática, contratação, envio de concorrência ao mercado ou "cálculo comparativo definitivo" para regras não suportadas

## Roadmap versus implementação

Sempre separar:

- implementado agora: o que está em rotas, serviços, templates, models e testes atuais
- planejado/roadmap: previsões futuras, automações comerciais, contratação automática, concorrência aberta de mercado e outras capacidades não comprovadas no código atual
