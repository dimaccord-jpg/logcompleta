# Diagnóstico de Homologação e Publicação — Fase 2

Este é o documento principal de verdade para publicação em homolog da Fase 2.

## 1) Estado atual confirmado

- Branch de feature criada, commits organizados e merge local em homolog concluído.
- Pacote final da Fase 2 está integrado no código local.
- Publicação final em homolog ainda **não concluída**.
- Nenhuma conclusão de homolog deve ser registrada antes de validar migrations no ambiente alvo.

## 2) Escopo final realmente incluído

### 2.1 Upload Roberto + identidade de execução + billing

- Upload usa `execution_id` (`X-Execution-ID` / form / fallback UUID) em `app/upload_handler.py`.
- Upload registra processamento (`processing_events`) com identidade de negócio.
- Apropriação de billing do upload é idempotente por `idempotency_key=roberto-upload:{execution_id}` em `app/services/cleiton_upload_billing_service.py`.
- Tabela de idempotência: `cleiton_billing_apropriacao`.

### 2.2 Governança operacional Cleiton (franquia)

- Conversão de consumo técnico para créditos por régua em `cleiton_cost_config`:
  - tokens por crédito
  - linhas por crédito
  - ms por crédito
- Motor aplica abatimento em `franquia.consumo_acumulado` e recalcula status (`active`, `degraded`, `expired`, `blocked`) em `app/services/cleiton_franquia_operacional_service.py`.
- Regras de autorização operacional antes da execução do chat/upload: `app/services/cleiton_operacao_autorizacao_service.py`.
- Reconciliação leitura vs soma histórica de eventos: `app/services/cleiton_franquia_reconciliacao_service.py`.

### 2.3 Modelo de conta/franquia/plano/multiuser

- Estrutura de negócio com `Conta`, `Franquia`, vínculo obrigatório em `User` (`conta_id`, `franquia_id`) e franquia interna de sistema.
- Resolução de plano operacional por categoria (`free`, `starter`, `pro`, `multiuser`, `avulso`, `interna`) em `app/services/cleiton_plano_resolver.py`.
- Atribuição administrativa de plano com suporte multiuser e geração de código por franquia em `app/services/user_plan_control_service.py`.

### 2.4 Operação de tela/admin incluída para homolog real

- Rotas admin para:
  - dashboard com métricas/filtros
  - agentes (Júlia/Cleiton)
  - controle de usuários (convite/revogação admin e atribuição de plano)
  - planos/SaaS
  - validação de franquia (`/api/cleiton-franquia/<id>/validacao`)
- Templates relevantes: `agentes_julia.html`, `agentes_cleiton.html`, `dashboard.html`, `controle_usuarios.html`, `planos.html`, `admin_confirmacao_acao.html`.
- `agentes_cleito.html` não faz parte do pacote atual (arquivo ausente); template ativo é `agentes_cleiton.html`.

### 2.5 Migrations e testes incluídos

- Cadeia Fase 2 de `g4h5i6j7k8l9` até `n7o8p9q0r1s2` (identidade, conta/franquia, régua, status operacional, bloqueio manual, cleanup freemium legado, multiuser, billing idempotente).
- Testes principais incluídos:
  - `tests/test_cleiton_classificacao_status.py`
  - `tests/test_cleiton_motor_reconciliacao.py`
  - `tests/test_cleiton_upload_billing_service.py`
  - `tests/test_franquia_operacao_autorizacao_service.py`

## 3) Bloqueio operacional atual (Render)

No ambiente Render, foi observado:

- `alembic` ausente no `PATH`.
- `python -m alembic current` com erro `No module named alembic`.
- `requirements.txt` atual sem Alembic.

Implicação direta: o pacote de migrations da Fase 2 não está garantido no runtime atual de homolog.  
Sem resolver isso, não há go/no-go para publicação final.

## 4) O que NÃO deve ser tratado como concluído

- Migrations aplicadas em homolog (não confirmado).
- Schema em `head` no banco de homolog (não confirmado).
- Homolog publicada com sucesso (não confirmado).

## 5) Dependências críticas e risco de regressão

- Não separar indevidamente `upload_handler` de `cleiton_upload_billing_service` e do motor de governança; isso quebra rastreabilidade e idempotência.
- Não alterar autorização operacional sem considerar `cleiton_operacao_autorizacao_service` usado por chat Júlia e upload Roberto.
- Não mudar categoria/plano sem manter coerência entre `user_plan_control_service`, `cleiton_plano_resolver` e estado da `franquia`.
- Não promover validação de homolog sem backend + telas admin alinhados; os testes reais dependem das rotas/templates de operação.
- Migrations da Fase 2 são parte obrigatória da entrega, não pós-ajuste opcional.

## 6) Go/No-Go para homolog (objetivo)

### No-Go (estado atual)

- Bloqueio de migration no Render ainda aberto.

### Go (somente quando todos os itens forem verdadeiros)

1. Estratégia de migration definida para o runtime de homolog.
2. `upgrade head` executado no banco alvo sem erro.
3. Verificação de `current` coerente com o head esperado.
4. Health checks, rotas cron protegidas e telas admin validadas após deploy.

## 7) Referências

- Deploy/runbook: `app/README_DEPLOY.md`.
- Migrations e cadeia de revisões: `migrations/README`.
- Render cron em homolog: `RENDER_CRON_HOMOLOG.md`.
- Execução local e troubleshooting: `app/README_RUN.md`.
- Segurança/segredos: `SECURITY_SECRETS.md`.
