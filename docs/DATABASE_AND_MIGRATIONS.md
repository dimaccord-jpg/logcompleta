# Banco e Migrations

Documentação revisada contra a árvore de produção `fa98317` (Sprint 12), models e cadeia física Alembic. O SCRUM-148 adiciona a migration `g8h9i0j1k2l3`, que torna `FunnelEvent.user_id`, `conta_id` e `franquia_id` nullable, preservando as foreign keys e sem criar nova tabela/modelo.

## Fonte de verdade

- banco transacional oficial: PostgreSQL via `DATABASE_URL`
- models: `app/models.py`
- migrations: `migrations/versions/`
- env Alembic: `migrations/env.py`

## Head atual

- head versionado: `f7g8h9i0j1k2`
- `down_revision`: `e6f7a8b9c0d1`
- arquivo: `f7g8h9i0j1k2_fase7_lifecycle_comercial.py`

## Cadeia Multiuser implantada

| Fase de origem | Revisão | Revisão anterior | Conteúdo |
|---|---|---|---|
| Fase 1 | `a2b3c4d5e6f7` | `z0a1b2c3d4e5` | Fundação persistente e backfill |
| Fase 3 | `b3c4d5e6f7a8` | `a2b3c4d5e6f7` | Intenção de checkout |
| Fase 4 | `c4d5e6f7a8b9` | `b3c4d5e6f7a8` | Convites e reservas |
| Fase 5 | `d5e6f7a8b9c0` | `c4d5e6f7a8b9` | Aumento automático |
| Fase 6 | `e6f7a8b9c0d1` | `d5e6f7a8b9c0` | Aumento excepcional e cobrança extraordinária |
| Fase 7 | `f7g8h9i0j1k2` | `e6f7a8b9c0d1` | Lifecycle comercial |

Os nomes das fases identificam a origem das migrations, não trabalho futuro. Fase 2 e hardening Fase 8 não possuem migrations próprias nessa cadeia.

`Conta` governa organização/capacidade; `ContaVinculoOrganizacional` preserva vínculos ativos e encerrados. Os modelos de intenção, convite, aumentos, redução e titularidade suportam idempotência e correlação. Ver [Multiuser V1](multiuser_v1.md).

A revisão `z0a1b2c3d4e5`, anterior ao Multiuser V1, permanece responsável pela telemetria da home descrita a seguir. O head versionado não substitui uma leitura operacional de `alembic_version`; esta revisão documental não consultou o banco de produção.

## Tabela `home_cta_experiment_event`

Finalidade:

- persistir telemetria isolada do experimento `home_chat_cta_v1`
- não reutiliza `FunnelEvent`
- não cria relacionamento com `User`, `Conta` ou `Franquia`
- não persiste PII em claro

Estrutura confirmada:

- `id`: `Integer`, primary key, not null
- `experiment`: `String(40)`, not null
- `assignment_id`: `String(64)`, not null
- `variant`: `String(16)`, not null
- `event_type`: `String(20)`, not null
- `interaction_origin`: `String(20)`, nullable
- `occurred_at`: `DateTime`, not null

Restrições e índice:

- primary key: `id`
- unique constraint: `uq_home_cta_experiment_event_assignment_type` em `experiment`, `assignment_id`, `event_type`
- índice: `ix_home_cta_experiment_event_experiment_occurred_at` em `experiment`, `occurred_at`

Semântica atual:

- `interaction_origin` só faz sentido para `conversion`
- valores esperados de origem: `typed` e `suggestion`
- a deduplicação é por `experiment + assignment_id + event_type`

O que não fica armazenado nesta tabela:

- e-mail
- nome
- `user_id`
- `conta_id`
- `franquia_id`
- payload bruto de conversa

Retenção:

- não existe regra de retenção explícita implementada para esta tabela no código auditado

## Experimento `home_chat_cta_v1`

Variantes confirmadas:

- `cta_a`: "Como posso ajudar sua operação logística hoje?"
- `cta_b`: "Descreva seu desafio logístico e veja como o AgenteFrete pode ajudar."
- `cta_c`: "Tem uma dúvida de logística? Conte o cenário e receba uma orientação."

Assignment:

- anônimo: aleatório por sessão, com `assignment_id` em token hex
- autenticado: determinístico a partir do `user.id`, sem gravar o id em claro na tabela
- o assignment é salvo em sessão Flask na chave `home_chat_cta_experiment`

Eventos:

- `impression`
- `conversion`

Privacidade e resiliência:

- telemetria fail-open: falha ao gravar evento não quebra home nem chat
- a leitura administrativa consulta apenas `home_cta_experiment_event`
- o dashboard administrativo oferece períodos de 7, 30 e 90 dias
- inconsistências com `conversions > impressions` geram warning e cap visual, sem alterar o dado histórico

## Segurança operacional de banco

Arquivo central: `app/db_operational_safety.py`

Filosofia:

- default-deny para operações destrutivas
- sem correção automática da URI
- sem bypass implícito por ambiente, por sufixo `_test` ou por PostgreSQL de teste

### Upgrade versus downgrade

- `upgrade` normal de migration não é bloqueado pelo guard
- `downgrade` Alembic é bloqueado por padrão
- o bloqueio acontece antes de abrir conexão no `migrations/env.py`

### Confirmação exigida para downgrade

Para permitir downgrade, o código exige simultaneamente:

- `ALLOW_DB_DOWNGRADE=1`
- `ALLOW_DB_DOWNGRADE_DATABASE=<nome-exato-do-database>`

Regras:

- o valor de `ALLOW_DB_DOWNGRADE` precisa ser literalmente `1`
- o nome do database precisa bater exatamente com o nome resolvido da URI
- `TESTING` não libera downgrade
- SQLite em memória não libera downgrade por exceção

### Fixtures e bancos descartáveis

Operações destrutivas de schema de teste (`create_all`/`drop_all`) só passam quando:

- `TESTING` está habilitado
- o destino é SQLite em memória real: `sqlite:///:memory:`

Consequências:

- SQLite em arquivo: negado
- PostgreSQL, inclusive com nome parecendo teste: negado por padrão

### Logs

- os logs operacionais registram ambiente, host, porta, database e operação
- senha, usuário e URI completa não são expostos

## Start e migrations no boot

O fluxo real do projeto está em `start.sh`:

```bash
python -m flask --app app.web db upgrade
```

Depois disso o processo sobe o Gunicorn.

Implicação documental:

- migrations pendentes são aplicadas antes da aplicação subir
- isso não significa que downgrade faça parte do fluxo normal

## Drift histórico conhecido

Durante validações anteriores, houve diferenças históricas em `db check` envolvendo:

- `cleiton_billing_apropriacao`
- `franquia.conta_id`
- `multiuser_franquia_codigo`

Nesta documentação esses itens devem ser tratados apenas como `drift histórico a investigar separadamente`.
