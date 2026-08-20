# Estado de Produção

Referência auditada em 2026-08-19 a partir do código versionado, migrations, templates, serviços e testes.

## Produção atual

- branch operacional de produção: `producao`
- homologação operacional: `homolog`
- conteúdo desta rodada já foi promovido para produção
- `render.yaml` atual aponta homolog para `homolog` e produção para `producao`
- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn

## Migration head esperado

Head esperado em produção:

- `y9z0a1b2c3d4`

Migrations desta rodada:

- `v6w7x8y9z0a1` `CommunicationSuppression`
- `w7x8y9z0a1b2` activation journey ended
- `x8y9z0a1b2c3` `NewsletterSubscription`
- `y9z0a1b2c3d4` `Lead.email_hmac`

## Deploy real

- `APP_ENV` é obrigatório
- valores aceitos: `dev`, `homolog`, `prod`
- o boot usa `app/.env.{APP_ENV}`
- homolog/prod não aceitam fallback silencioso para diretórios efêmeros locais
- `APP_DATA_DIR` e `INDICES_FILE_PATH` precisam apontar para persistência real em homolog/prod
- `COMMUNICATION_SUPPRESSION_HMAC_SECRET` deve ser configurado externamente; a documentação não trata arquivo local como fonte de verdade desse secret

## Estado funcional do produto

- home anônima usa Copiloto público
- a home anônima não apresenta Júlia como experiência principal
- após login, a experiência principal passa a ser Júlia
- `/auditoria-frete`, `/agente-compara`, `/feed`, `/perfil` e `/fretes` seguem existentes
- Roberto continua implementado, mas escondido/não priorizado na experiência atual

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

## Billing e Stripe

- planos principais visíveis: `Free`, `Starter`, `Pro`
- cobrança recorrente mensal
- webhook oficial: `/api/webhook/stripe`
- `invoice.paid` é o evento principal de confirmação contratual documentável
- snapshots sanitizados continuam possíveis via `payload_bruto_sanitizado_json`

## Rollout histórico desta entrega

### Communication suppression

Estado histórico conhecido após leitura do banco em produção:

- leads com `opt_out_at`: `0`
- leads com `activation_opt_out_at`: `0`

Conclusão operacional daquele rollout:

- não havia registros históricos elegíveis para backfill
- não foi necessário `--apply` do `communication-suppression-backfill`

Isso é histórico de rollout, não regra estrutural da arquitetura.

### Newsletter

Estado histórico conhecido do saneamento:

- antes do backfill, havia `12` users com `subscribes_to_newsletter=True` e sem `NewsletterSubscription`
- o dry-run oficial reportou `would_create=12`
- o apply posterior reportou `created=12`
- a validação final ficou em `users_newsletter_true_sem_subscription=0`

Conclusão:

- o backfill histórico de newsletter foi concluído em produção

### Lead.email minimization

Estado histórico conhecido da análise:

- `total_leads=2`
- `converted=0`
- `converted_com_email_hmac=0`
- `converted_ja_minimizados=0`

Conclusão:

- como `converted_user_id` era requisito, nenhum `Lead` era elegível
- não foi executado `--apply` de `lead-email-minimization`

## Smoke operacional aprovado

O rollout desta entrega já foi validado em produção para:

- home anônima
- Copiloto público
- banner de privacidade
- painel de preferências
- rejeição de marketing
- persistência após F5
- alteração posterior para aceite
- persistência do aceite
- login
- experiência autenticada com Júlia
- Perfil
- Cleide
- AgenteCompara
- Feed
- logout
- retorno correto ao Copiloto

Também ficou validado que o banner não cobre a sidebar.

## Limites conhecidos

- o smoke acima é registro operacional, não suíte formal automatizada
- retenção automática geral não existe para todo tipo de dado
- masking para IA externa não promete sanitização universal de PDF e texto livre
- newsletter está operacional, mas isso não implica prioridade comercial atual
