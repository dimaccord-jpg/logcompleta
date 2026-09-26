# Estado de Produção

Referência de produção informada pela operação em 2026-09-23: branch `producao`, commit `fa98317` (`release: Sprint 12`). A árvore versionada desse merge inclui o trabalho funcional consolidado em `04fcfc0` e o histórico da Sprint 11 (`bd874ee`). A validação em produção e o inventário de histórias abaixo são informação operacional; esta revisão documental confrontou o código e os testes versionados, sem consulta direta ao painel Render, Stripe ou banco de produção.

## Sprint 12 — estado consolidado

História principal: **SCRUM-147**.

Itens informados pela operação como concluídos e validados em produção:

| Item | Evidência no repositório |
|---|---|
| SCRUM-147 | Marco de entrega (Sprint 12); não há artefato de teste com esse ID |
| SCRUM-151, 152, 153, 154 | Informados pela operação; IDs não aparecem em testes/docs versionados |
| SCRUM-191 | `tests/test_scrum191_agentefrete_chat_identity.py` |
| SCRUM-207, 208, 209 | Informados pela operação; IDs não aparecem em testes/docs versionados |
| SCRUM-210 | `tests/test_scrum210_mobile_responsiveness.py` |
| SCRUM-211 | `tests/test_scrum211_fretes_login_gate.py` |
| SCRUM-212 | Informado pela operação; ID não aparece em testes/docs versionados |
| SCRUM-214 | `tests/test_scrum214_julia_image_model.py`, `scripts/diagnose_julia_image_provider.py` |
| SCRUM-215 | `tests/test_scrum_215a_evergreen_admin.py`, `tests/test_scrum_215b_evergreen_automatico.py` |
| SCRUM-216 | `tests/test_scrum216_checkout_cleanup.py` |

Produto em produção após a Sprint 12:

- identidade pública única **AgenteFrete**
- shell e habilidades unificados
- tema global (`system` / `dark` / `light`)
- Home e Login corrigidos para mobile de referência
- autenticação consistente (`/fretes` protegido; `next` seguro)
- chat principal com identidade AgenteFrete
- Feed / pipeline editorial / SEO evoluídos (`news` / `analysis` / `evergreen`)
- evergreen manual e automático operáveis
- modelo de imagem atualizado
- checkout Multiusuário limpa checkout Starter/Pro anterior

Breaking changes de contrato de produto: nenhum informado além da consolidação de identidade/tema/auth já listada.

## Estado atual confirmado

- Multiuser V1 permanece implantado; contrato em [Multiuser V1](multiuser_v1.md)
- branch de homologação: `homolog`
- branch de produção: `producao`
- `render.yaml` aponta homologação para `homolog` e produção para `producao`
- os dois serviços usam `autoDeploy: true`
- `start.sh` executa `python -m flask --app app.web db upgrade` antes do Gunicorn
- `APP_ENV` é obrigatório; valores aceitos: `dev`, `homolog`, `prod`
- o boot usa `app/.env.{APP_ENV}` via `load_app_env()` com `override=False` (variáveis já presentes no processo prevalecem sobre o arquivo)
- arquivos `.env.*` locais podem estar gitignored; o ambiente remoto (Render) é a fonte de verdade de produção
- homolog/prod não aceitam fallback silencioso para diretórios efêmeros locais
- `APP_DATA_DIR` e `INDICES_FILE_PATH` precisam apontar para persistência real em homolog/prod
- `COMMUNICATION_SUPPRESSION_HMAC_SECRET` deve ser configurado externamente; `.env.prod` local não é fonte autoritativa do Render

## Migration head atual

- head aplicado correspondente ao release de produção `fa98317`: `f7g8h9i0j1k2`
- `down_revision` desse head de produção: `e6f7a8b9c0d1`
- migration: `f7g8h9i0j1k2_fase7_lifecycle_comercial.py`
- cadeia Multiuser F1, F3, F4, F5, F6 e F7 em [Banco e Migrations](DATABASE_AND_MIGRATIONS.md)
- o release de produção `fa98317` não inclui a migration Growth `g8h9i0j1k2l3`; essa migration pertence ao candidato de homologação do SCRUM-148 e só deve ser descrita como aplicada em produção após promoção efetiva
- a tabela `home_cta_experiment_event` continua sustentando `home_chat_cta_v1`

## Identidade e superfícies

- definição pública: **Assistente virtual especializado em logística da LogCompleta**
- marca pública: **AgenteFrete**; o usuário não precisa conhecer nomes internos
- nomes internos (Júlia, Roberto, Cleide, Cleiton, AgenteCompara etc.) permanecem por compatibilidade arquitetural e **não** são marcas/produtos separados
- home pública `/`: discovery anônimo + cards de habilidade + marca AgenteFrete
- chat operacional autenticado: `/chat_julia?mode=operational` com identidade pública AgenteFrete (rotas/IDs `chat_julia` / `julia` são compatibilidade técnica)
- histórico conversacional do chat operacional: client-side no fluxo atual; não há feature de threads persistentes de produto
- AgenteAudita em `/auditoria-frete` (APIs `/api/cleide-auditoria/*`)
- AgenteCompara em `/agente-compara`
- Roberto em `/fretes` (**autenticado**)
- Feed em `/feed`; detalhe editorial em `/noticia/<id>`
- `/acesso-desktop`: landing de campanha opcional/legada; **não** é gate técnico nem requisito de uso

## Habilidades

Catálogo canônico em `app/shell_navigation.py` (shell desktop/mobile e Home).

Na Home (cards com `home_summary`):

- Analisar fretes → `/fretes` (login se anônimo)
- Auditar cobranças de frete → `/auditoria-frete` (login se anônimo)
- Comparar tabelas → `/agente-compara` (login se anônimo)

No shell (habilidades):

- Consultar o AgenteFrete → `/chat_julia?mode=operational` (a capacidade de conversar com o assistente)
- as três habilidades acima
- Feed

Discovery permanece público. Habilidades operacionais exigem autenticação conforme taxonomia. Login preserva destino via `next`; `_safe_next_redirect` rejeita destino externo / inseguro.

O produto **não** é TMS, WMS nem ERP.

## Tema (contrato atual)

- preferências: `system`, `dark`, `light`
- persistência: `localStorage` chave `af-theme`
- tema **global** para templates que herdam `base.html`
- não existe mais opt-in por página (`af_theme_enabled` / `data-af-theme-enabled` removidos do contrato)
- `system` acompanha `prefers-color-scheme`; mudança do SO reflete enquanto a preferência for `system`
- admin (`base_admin.html`) permanece fora deste contrato
- `/acesso-desktop` tem contrato visual próprio (standalone)

## Mobile

- Home e Login possuem correções de responsividade cobertas por teste automatizado
- viewport de referência operacional: aproximadamente 360–430 px (faixa de validação de produto; não há assert de device específico no código)
- expectativa: sem overflow horizontal; menu mobile do shell funcional; desktop/tablet preservados

## Feed / editorial / evergreen

Intenções editoriais explícitas: `news`, `analysis`, `evergreen`.

- news: notícia rápida / atualidade
- analysis: análise / aplicação prática
- evergreen: conteúdo perene orientado a intenção de busca

Pipeline inclui metadata editorial, SEO, meta description, alt de imagem, JSON-LD e CTA contextual quando há habilidade real. URLs públicas continuam `/noticia/<id>`. O Feed é superfície de informação + aquisição + recorrência.

Admin da Júlia (editorial interno):

- permite escolher Analysis ou Evergreen e selecionar pauta explícita
- `pauta_id` explícito é preservado ponta a ponta
- ID inválido/malformado: fail-closed, sem fallback para outra pauta
- ausência de `pauta_id`: comportamento legado permitido

Cadência evergreen:

- `evergreen_automatico_habilitado` e `evergreen_frequencia_minutos` em `ConfigRegras`
- independente do ciclo legado; não disputa `decidir_tipo_missao`
- mesma infraestrutura de acionamento; sem scheduler paralelo
- execução manual não desloca cadência automática; news/analysis não deslocam evergreen
- somente sucesso automático atualiza a referência temporal (“próxima elegibilidade”, não execução garantida)
- pautas evergreen explícitas/reconhecidas não são consumidas pelo fluxo legado automático; com automático ligado, o side-car evergreen pode consumi-las

## Imagem editorial

- modelo principal versionado (default de código / `.env.example`): `gemini-3.1-flash-image`
- método para esse modelo: `generate_content` (modelos `imagen-*` ainda usariam `generate_images`)
- provider Gemini; uma imagem editorial por conteúdo; fallback continua disponível
- o modelo antigo `imagen-3.0-generate-002` não é o default atual (obsoleto / 404)
- valor efetivo no Render pode diferir do `.env.prod` local; o remoto é a fonte de verdade

Instrumentação temporária associada a diagnósticos da SCRUM-214 não faz parte da arquitetura permanente; o diagnóstico versionado é utilitário (`scripts/diagnose_julia_image_provider.py`).

## Checkout / planos

- Starter / Pro: checkout embedded
- ao selecionar Multiusuário: checkout Starter/Pro anterior é destruído/limpo; iframe residual não permanece; container vazio colapsa; formulário Multiusuário aparece; checkout Multiusuário só abre após **Ir para o checkout**
- preços, Price IDs, regras comerciais e Stripe permanecem os documentados em [Multiuser V1](multiuser_v1.md) / monetização — sem mudança comercial inventada nesta sprint

## Billing, privacidade e governança

- planos principais visíveis: `Free`, `Starter`, `Pro`, `Multiuser`
- cobrança recorrente mensal
- webhook oficial de produção: `https://www.agentefrete.com.br/api/webhook/stripe`
- `invoice.paid` permanece como evento principal de confirmação contratual documentável
- consentimento, suppression, newsletter, lifecycle e masking outbound seguem ativos
- governança Cleiton (outbound control, minimização, fail-closed quando aplicável) permanece; ver [governança de IA](cleiton_ai_data_governance.md)
- downgrade de banco segue bloqueado por padrão

## Multiuser V1

Conta organizacional/comercial, papéis contratante/membro, vínculo histórico e uma Franquia individual por usuário ativo, inclusive o contratante. Ciclo comum e capacidade contratada são governados pela Conta; consumo e artefatos privados permanecem separados por usuário. [Regras](multiuser_v1.md).

`/perfil` reúne Central de Segurança e Conta, status de proteção, acesso/credenciais, alteração de senha (via fluxo de recuperação por e-mail), Conta, encerramento/cancelamento, notificações, gestão multiusuário e contratação/plano — sob o tema global quando a página herda `base.html`.

## Home CTA

- experimento: `home_chat_cta_v1`
- tabela: `home_cta_experiment_event`
- painel administrativo de leitura por 7, 30 e 90 dias
- telemetria isolada, sem `Lead`, sem `FunnelEvent` e sem PII em claro
- assignment anônimo aleatório por sessão; autenticado determinístico a partir de `user.id`, sem gravar o id em claro
- eventos `impression` e `conversion` são fail-open

## Feed (layout)

Estado esperado do feed no código atual:

- rota `/feed`
- coluna única; artigos e insights misturados; ordenação cronológica; mais novos primeiro
- limite total de 5 itens
- CTA por tipo e preservação de categoria, fonte, data, título, subtítulo e `Fonte Original`

## Pendências conhecidas e pós-release

- **Notificações internas:** listagem recente com rolagem; sininho com total não lido; marcação assíncrona; CTA “Abrir” só para destino útil. Não implica que toda mensagem do produto gere notificação.
- **Verificação financeira Multiuser:** ciclo real completo de cobrança/renovação ainda depende de evidência operacional Stripe/banco.
- **Evidência de UAT Multiuser legado:** permanece o mesmo limite documentado na Sprint 11; ver histórico abaixo.
- **Tickets 151–154 / 207–209 / 212:** validados pela operação; detalhe de escopo por ID não está versionado neste repositório.

## Evolução futura

O contratante ainda ocupa assento e não pode liberá-lo voluntariamente mantendo o papel de gestão. Redução assistida de assentos e teardown completo Multiuser → Free ainda exigem validação/hardening. API pública Multiuser, OAuth/API keys de integração, org-admin, troca livre entre Contas e auto-revogação para cumprir redução continuam fora do V1.

## Limites conhecidos

- retenção automática geral não existe para todo tipo de dado
- masking para IA externa não promete sanitização universal de PDF e texto livre
- newsletter está operacional, mas isso não implica prioridade comercial atual
- o drift histórico de schema em `cleiton_billing_apropriacao`, `franquia` e `multiuser_franquia_codigo` permanece tema separado

## Histórico de releases recentes

| Release | Commit | Nota |
|---|---|---|
| Sprint 12 | `fa98317` | Estado atual deste documento |
| Sprint 11 | `bd874ee` | Multiuser V1 e baseline anterior; superseded como estado vivo |
| Tag `pre-sprint11-prod-20260921` | — | Ponto anterior à Sprint 11; não é convenção permanente |
