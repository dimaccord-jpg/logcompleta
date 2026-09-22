# Estado de Produção

Referência de produção informada pela operação em 2026-09-22: branch `producao`, commit `bd874ee` (`release: Sprint 11`), com 13 itens aprovados. A árvore versionada desse commit coincide com o checkout auditado. O deploy foi validado pela operação no Render com `/health` (`ambiente=prod`, `database=connected`, `status=ok`), login, sidebar, plano/créditos, `/perfil` e telas principais. Não houve consulta direta ao Render, Stripe ou banco de produção nesta revisão documental.

## Estado atual confirmado

- Multiuser V1 implantado e operando em produção; contrato funcional em [Multiuser V1](multiuser_v1.md)
- branch de homologação: `homolog`
- branch de produção: `producao`
- `render.yaml` atual aponta homologação para `homolog` e produção para `producao`
- os dois serviços usam `autoDeploy: true`
- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn
- `APP_ENV` é obrigatório; valores aceitos: `dev`, `homolog`, `prod`
- o boot usa `app/.env.{APP_ENV}`
- homolog/prod não aceitam fallback silencioso para diretórios efêmeros locais
- `APP_DATA_DIR` e `INDICES_FILE_PATH` precisam apontar para persistência real em homolog/prod
- `COMMUNICATION_SUPPRESSION_HMAC_SECRET` deve ser configurado externamente; a documentação não trata arquivo local como fonte de verdade desse secret

## Migration head atual

- head atual versionado: `f7g8h9i0j1k2`
- `down_revision`: `e6f7a8b9c0d1`
- migration: `f7g8h9i0j1k2_fase7_lifecycle_comercial.py`
- cadeia Multiuser F1, F3, F4, F5, F6 e F7 em [Banco e Migrations](DATABASE_AND_MIGRATIONS.md); não existe migration Fase 8 nem nova migration executável na Sprint 11
- a tabela anterior `home_cta_experiment_event` continua sustentando `home_chat_cta_v1`

A tabela da home é isolada e não altera o schema do AgenteCompara, da franquia operacional nem de `FunnelEvent`.

## Produto e superfícies atuais

- a home pública prioriza discovery e a marca AgenteFrete
- a home pública não apresenta Julia como experiência principal
- a experiência operacional autenticada continua em `/chat_julia?mode=operational`
- AgenteAudita é a identidade pública da auditoria de fretes, em `/auditoria-frete`
- os endpoints técnicos da auditoria permanecem em `/api/cleide-auditoria/*`
- `/agente-compara`, `/fretes`, `/feed` e `/perfil/*` seguem ativos
- Roberto continua implementado na superfície `/fretes`

## Home CTA

- experimento: `home_chat_cta_v1`
- tabela: `home_cta_experiment_event`
- painel administrativo de leitura por 7, 30 e 90 dias
- telemetria isolada, sem `Lead`, sem `FunnelEvent` e sem PII em claro
- assignment anônimo aleatório por sessão; autenticado determinístico a partir de `user.id`, sem gravar o id em claro
- eventos `impression` e `conversion` são fail-open

## Billing, privacidade e governança

- planos principais visíveis: `Free`, `Starter`, `Pro`, `Multiuser`
- cobrança recorrente mensal
- webhook oficial de produção: `https://www.agentefrete.com.br/api/webhook/stripe`
- `invoice.paid` permanece como evento principal de confirmação contratual documentável
- snapshots sanitizados continuam possíveis via `payload_bruto_sanitizado_json`
- consentimento, suppression, newsletter, lifecycle e masking outbound seguem ativos
- downgrade de banco segue bloqueado por padrão

## Multiuser V1

Conta organizacional/comercial, papéis contratante/membro, vínculo histórico e uma Franquia individual por usuário ativo, inclusive o contratante. Ciclo comum e capacidade contratada são governados pela Conta; consumo e artefatos privados permanecem separados por usuário.

Contratação Stripe, dados empresariais obrigatórios, convite/aceite explícito, reservas, aumentos automático/excepcional, redução futura, revogação e titularidade administrativa estão implementados. O diagnóstico read-only e o CSV administrativo incorporam os estados Multiuser. [Regras, valores de produção, webhook e limites](multiuser_v1.md).

`/perfil` reúne segurança e conta, indicadores de dados/IA, plano e notificações. “Alterar senha” envia o usuário ao fluxo existente de recuperação por e-mail; a senha não é alterada diretamente no perfil. `/contrate-um-plano` mostra os planos e inicia Checkout Stripe incorporado quando solicitado.

## Pendências conhecidas e pós-release

- **Notificações internas:** `/perfil` lista as notificações recentes primeiro, em área com rolagem; o sininho exibe o total não lido. A marcação como lida usa requisição assíncrona, atualiza o badge e o remove quando chega a zero. O CTA “Abrir” aparece apenas para destino útil; o CTA da própria página `/perfil` é omitido. A revogação Multiuser orienta continuidade individual e contratação de plano próprio. Esses mecanismos não significam que toda mensagem do produto produza notificação interna.
- **Verificação financeira:** a documentação não dispõe de evidência de um ciclo real completo de cobrança e renovação Multiuser. Confirmar no Stripe e no banco antes de registrar essa validação como concluída.
- **Evidência de UAT:** a implantação do V1 é informação confirmada pela operação. Os testes versionados cobrem revogação, preservação de consumo, reentrada e CSV administrativo, mas sua existência não comprova uma execução aprovada. Não foi localizado nesta revisão um relatório de execução do UAT 7.3 que sustente a contagem anteriormente citada de quatro testes aprovados. A preservação de histórico operacional preexistente após revogação/reentrada não é integralmente demonstrada pelas assertivas de revogação consultadas; isso não é evidência de perda de histórico nem bloqueador de implantação informado pela operação.

## Evolução futura

O contratante ainda ocupa assento e não pode liberá-lo voluntariamente mantendo o papel de gestão. A redução assistida de assentos e o teardown completo Multiuser → Free ainda exigem validação/hardening; o agendamento do cancelamento não prova esses efeitos. API pública Multiuser, OAuth/API keys/scopes/rate limit de integração, org-admin, troca livre entre Contas e auto-revogação para cumprir redução continuam fora do V1.

Na contratação Multiuser, a consulta online ao ViaCEP preenche campos editáveis de endereço. Há fallback manual; um retorno sem `erro: true` mas sem endereço completo pode receber a mensagem visual “CEP localizado. Complete os campos restantes.” Mesmo quando o CEP não existe, esse caso pode induzir a leitura incorreta de sucesso.

## Feed atual

O estado esperado do feed no código atual é:

- rota `/feed`
- coluna única
- artigos e insights misturados
- ordenação cronológica
- mais novos primeiro
- limite total de 5 itens
- CTA por tipo: `Ver Insight` ou `Ver Artigo`
- preservação de categoria, fonte, data, título, subtítulo e `Fonte Original`

## Limites conhecidos

- retenção automática geral não existe para todo tipo de dado
- masking para IA externa não promete sanitização universal de PDF e texto livre
- newsletter está operacional, mas isso não implica prioridade comercial atual
- o drift histórico de schema em `cleiton_billing_apropriacao`, `franquia` e `multiuser_franquia_codigo` permanece tema separado
