# Estado de Produção

Referência auditada em 2026-09-04 a partir do código versionado, migrations, templates, serviços, testes e da configuração do repositório.

## Estado atual confirmado

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

- head atual versionado no repositório: `z0a1b2c3d4e5`
- `down_revision`: `y9z0a1b2c3d4`
- migration nova: `z0a1b2c3d4e5_home_cta_experiment_event.py`
- tabela nova: `home_cta_experiment_event`
- experimento associado: `home_chat_cta_v1`

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

- planos principais visíveis: `Free`, `Starter`, `Pro`
- cobrança recorrente mensal
- webhook oficial: `/api/webhook/stripe`
- `invoice.paid` permanece como evento principal de confirmação contratual documentável
- snapshots sanitizados continuam possíveis via `payload_bruto_sanitizado_json`
- consentimento, suppression, newsletter, lifecycle e masking outbound seguem ativos
- downgrade de banco segue bloqueado por padrão

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

---

## HISTÓRICO — Migration head anterior

Antes da tabela `home_cta_experiment_event`, o head esperado em produção era:

- `y9z0a1b2c3d4`

Migrations daquele ciclo:

- `v6w7x8y9z0a1` `CommunicationSuppression`
- `w7x8y9z0a1b2` activation journey ended
- `x8y9z0a1b2c3` `NewsletterSubscription`
- `y9z0a1b2c3d4` `Lead.email_hmac`

Isso é histórico de schema, não o head atual.

## ROLLOUT ANTERIOR — Communication suppression

Estado histórico conhecido após leitura do banco em produção naquele rollout:

- leads com `opt_out_at`: `0`
- leads com `activation_opt_out_at`: `0`

Conclusão operacional daquele rollout:

- não havia registros históricos elegíveis para backfill
- não foi necessário `--apply` do `communication-suppression-backfill`

Isso é histórico de rollout, não regra estrutural da arquitetura.

## ROLLOUT ANTERIOR — Newsletter

Estado histórico conhecido do saneamento:

- antes do backfill, havia `12` users com `subscribes_to_newsletter=True` e sem `NewsletterSubscription`
- o dry-run oficial reportou `would_create=12`
- o apply posterior reportou `created=12`
- a validação final ficou em `users_newsletter_true_sem_subscription=0`

Conclusão:

- o backfill histórico de newsletter foi concluído em produção

## ROLLOUT ANTERIOR — Lead.email minimization

Estado histórico conhecido da análise:

- `total_leads=2`
- `converted=0`
- `converted_com_email_hmac=0`
- `converted_ja_minimizados=0`

Conclusão:

- como `converted_user_id` era requisito, nenhum `Lead` era elegível
- não foi executado `--apply` de `lead-email-minimization`

## HISTÓRICO — Smoke operacional anterior

O rollout anterior já foi validado em produção para:

- home anônima
- Copiloto público
- banner de privacidade
- painel de preferências
- rejeição de marketing
- persistência após F5
- alteração posterior para aceite
- persistência do aceite
- login
- experiência autenticada com Julia
- Perfil
- auditoria de fretes
- AgenteCompara
- Feed
- logout
- retorno correto ao Copiloto

Também ficou validado que o banner não cobre a sidebar.

O smoke acima é registro operacional histórico, não suíte formal automatizada nem definição permanente do estado atual.
