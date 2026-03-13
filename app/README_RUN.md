# 🏃 Guia de Execução Local

Este projeto utiliza variáveis de ambiente para alternar entre configurações de Desenvolvimento e Homologação. A lógica de autenticação está em `app/auth_services.py`; a infraestrutura em `app/infra.py`; as rotas operacionais (diagnóstico OAuth, auditoria de usuários, promote-admin, reset de pautas e health) estão em `app/ops_routes.py` (Blueprint). O `web.py` apenas expõe as rotas e registra os blueprints.
## Novidade: Área do Usuário

O sistema possui uma Área do Usuário acessível pelo avatar no rodapé da sidebar. Usuários autenticados podem acessar `/perfil` para visualizar cards de Segurança, Pagamento e Notificações. Admins veem um atalho Painel ADM.

### Teste manual
- Faça login com usuário comum: clique no avatar/BEM-VINDO → `/perfil` exibe os cards, sem Painel ADM.
- Faça login com admin: clique no avatar/BEM-VINDO → `/perfil` exibe os cards e o Painel ADM.
- Logout funciona normalmente.
- Acesso a `/perfil` sem login redireciona para tela de login.

### Referência
- Blueprint: `user_bp` em `app/user_area.py`
- Template: `app/templates/user_area.html`
- Testes: `app/tests/test_user_area.py`

**Camada gerencial (Cleiton):** Orquestração em `run_cleiton_agente_orquestrador.py`; regras em `run_cleiton_agente_regras.py`; dispatch em `run_cleiton_agente_dispatcher.py`; auditoria em `run_cleiton_agente_auditoria.py`. `run_cleiton.py` é fachada que delega ao orquestrador.

**Camada operacional (Júlia – Etapa 2):** Pipeline em `run_julia_agente_pipeline.py` (pauta → redação → imagem → qualidade → publicação). Pautas vêm da tabela `Pauta`; **apenas pautas com status_verificacao=aprovado** (Fase 3) seguem para a Júlia.

**Fase 3 – Scout + Verificador:** `run_cleiton_agente_scout.py` coleta pautas; `run_cleiton_agente_verificador.py` classifica (score, aprovado/revisar/rejeitado). Só aprovadas seguem para Júlia.

**Fase 4 – Designer + Publisher:** `run_julia_agente_designer.py` gera assets por canal (url_imagem_master, assets_por_canal); `run_julia_agente_publisher.py` publica no portal (obrigatório) e em canais configurados (linkedin, instagram, email em modo mock). Status por canal (pendente/publicado/falha/ignorado); duplicidade por canal bloqueada. Janela e intervalo entre posts podem ser aplicados nos canais externos por `PUBLISHER_JANELA_PUBLICACAO_*` e `PUBLISHER_INTERVALO_MINUTOS_ENTRE_POSTS`. Tabela `publicacao_canal` e colunas em NoticiaPortal (url_imagem_master, assets_canais_json, status_publicacao, publicado_em). Retenção de 18 meses inclui histórico de PublicacaoCanal.

**Fase 5 – Customer Insight:** `run_cleiton_agente_customer_insight.py` mede desempenho por conteúdo/canal, gera recomendações estratégicas (tema, tipo, canal, horário, frequência) e audita com `tipo_decisao=insight`. `run_julia_agente_metricas.py` consolida métricas em `InsightCanal`; recomendações em `RecomendacaoEstrategica`. A rota `POST /executar-insight` é mantida por compatibilidade e aciona o ciclo completo do Cleiton (mesmo fluxo do `/executar-cleiton`, com Insight ao final).

**Fase 6 – Encerramento:** Feedback loop estratégico: o orquestrador consome recomendações pendentes (prioridade DESC) antes do dispatch e aplica tema, tipo_missao e prioridade ao payload; em missão sucesso a recomendação é marcada como aplicada; em falha permanece pendente. Serviço de gestão: `listar_recomendacoes_pendentes`, `selecionar_recomendacao_prioritaria`, `parse_recomendacao_json`/`parse_contexto_json`, `atualizar_status_recomendacao` (com auditoria). Painel admin inclui operações de backoffice para série/pauta: CRUD de séries e itens, vincular/desvincular pauta, reabrir/pular item, criação/edição de pautas manuais, arquivar pauta e reprocessar/marcar revisão; pautas arquivadas saem do backlog elegível de artigo. Dashboard mantém KPIs de insight e ações em recomendações (`/admin/recomendacoes/<id>/aplicar` e `/descartar`). Rotas principais: `/health/liveness`, `/health/readiness`, `/executar-cleiton` (ciclo completo) e `/executar-insight` (compatibilidade, mesmo ciclo completo), login/home.

**Execução manual no Admin (hotfix homolog/prod):** A rota `/admin/agentes/julia/executar-cleiton` roda em **background** por padrão quando `APP_ENV` é `homolog` ou `prod`, evitando timeout de worker na requisição HTTP. Em `dev`, o padrão continua síncrono para facilitar validação local. É possível forçar via `ADMIN_CLEITON_EXEC_MODE=sync|async`.

**Indicadores no topo da Home (Petróleo, BDI, FBX e Dólar):**
- Coleta: `app/finance.py` (`atualizar_indices`) usa configuração centralizada em `app/settings.py` (`settings.indices_file_path`) para resolver o caminho persistente dos índices por ambiente.
- Persistência (fase atual): formato histórico em arquivo (`ultima_atualizacao` + `historico`), armazenado em diretório de dados resolvido por `env_loader.resolve_data_dir` / `APP_DATA_DIR` / `RENDER_DISK_PATH`.  
  - Em **dev**, se nada estiver configurado, o fallback é o diretório local do app.
  - Em **homolog/prod**, `env_loader.resolve_data_dir` **não permite** fallback para diretório efêmero da release: se não houver diretório persistente válido, a aplicação falha no boot com erro explícito.
- Exibição: a rota `/` em `app/web.py` lê o mesmo caminho de índices via `settings.indices_file_path`, extrai o último item do histórico e envia para `index.html` no formato plano esperado pelo ticker (`dolar`, `petroleo`, `bdi`, `fbx`), mantendo compatibilidade com o JSON legado simples e o formato histórico.
- Contrato importante: o formato histórico deve ser mantido para análises do Roberto (`run_roberto.py`), e a conversão para formato plano deve ficar restrita à rota da Home. Em caso de falha na leitura, a Home continua exibindo um fallback seguro (campos não são zerados silenciosamente pelo backend).

**Hardening de ambiente (homolog/prod):**
- A configuração de ambiente é centralizada em `app/settings.py`. O módulo determina `APP_ENV` em um único ponto e chama `env_loader` apenas uma vez.
- Em ambientes gerenciados (ex.: Render, com `RENDER=true`), `APP_ENV` é obrigatório e deve estar explícito no serviço (`homolog` ou `prod`). Fora desse contexto, o default é `dev` apenas para execução local.
- `DB_URI_*` críticos continuam devendo ser definidos no ambiente ou serão resolvidos para caminhos persistentes em diretório de dados dedicado (por padrão fora da pasta da release, via `APP_DATA_DIR` / `RENDER_DISK_PATH` / `/var/data`).
- `INDICES_FILE_PATH` deve apontar para storage persistente fora da pasta `app` (ex.: `/var/data/indices.json` ou diretório definido por `APP_DATA_DIR`/`RENDER_DISK_PATH`). A validação em `env_loader.validate_runtime_env` agora é **reativa**: em homolog/prod, um caminho inválido ou apontando para a pasta da release provoca erro de boot, evitando deploy “verde” com persistência quebrada.

## Status Atual

- Status operacional vigente: Home (`/`) renderiza os quatro indicadores com compatibilidade para JSON histórico e JSON legado.
- Fluxo recomendado em homolog/prod: coleta desacoplada por agendamento (`python -m app.finance`) e leitura somente do último registro na camada web.
- Critério de aceite funcional: ticker sem campos vazios para Dólar, Petróleo, BDI e FBX.

## Seguranca de Segredos (obrigatorio)

O projeto possui padrao anti-vazamento com bloqueio local e no CI.

1. Instale hooks locais:

```bash
pip install pre-commit
pre-commit install
```

2. Rode validacao antes de commit/PR:

```bash
pre-commit run --all-files
```

3. Consulte o guia completo em `SECURITY_SECRETS.md`.

## Pré-requisitos

1. Instale as dependências:
   ```bash
   pip install -r ../requirements.txt
   ```
  - O `requirements.txt` já inclui `google-generativeai` para integração com Gemini (Cleiton/Júlia/Roberto).
2. Garanta que os arquivos `.env.dev` e `.env.homolog` existam na pasta `app/`.
   - O arquivo `.env` simples é legado e **não deve ser usado**.
   - Use `app/.env.example` como base, copiando para `.env.dev` e `.env.homolog` e ajustando apenas os valores.
   - A leitura desses arquivos é feita de forma centralizada por `app/settings.py`, que carrega `.env.{APP_ENV}` via `env_loader` em um único ponto antes de construir o objeto `settings`.
   - Para login com Google, defina `GOOGLE_OAUTH_REDIRECT_URI` (ex.: `http://127.0.0.1:5000/login/google/callback`) e, em dev, `OAUTHLIB_INSECURE_TRANSPORT=1`.
   - Para a camada gerencial, configure `DB_URI_GERENCIAL` (ex.: `sqlite:///gerencial.db`) no `.env.*`; se omitido, usa `app/gerencial.db`.
   - Para a Júlia, `GEMINI_MODEL_TEXT` permite definir o modelo preferencial; se indisponível, o sistema tenta fallback automático.
  - Para imagens da Júlia (insights/artigos), configure `IMAGE_PROVIDER=gemini`, `GEMINI_MODEL_IMAGE` e opcionalmente `GEMINI_MODEL_IMAGE_FALLBACK`.
  - O pipeline enriquece o prompt de imagem com contexto da pauta + título/subtítulo/resumo gerados, para manter coesão semântica entre texto e capa.
  - A geração principal usa retries configuráveis (`IMAGE_RETRY_ATTEMPTS`, `IMAGE_RETRY_BACKOFF_MS`) antes de degradar para fallback.
  - Quando a IA retorna bytes (sem URL pública), o sistema salva o arquivo em `app/static/generated/` e publica via URL local (`/static/generated/...`).
  - Se a IA falhar, o fallback preferencial é fotográfico contextual salvo localmente em `app/static/generated/julia_stock_<hash>.jpg`.
  - Apenas sem stock disponível o sistema usa asset fixo versionado em `app/static/img/fallback-capa-v1.svg`.
  - `IMAGEM_FALLBACK_URL` é opcional e tem prioridade quando definido.
  - `IMAGE_ALLOW_REMOTE_FALLBACK=false` (padrão) evita fallback remoto variável; use `true` somente se quiser permitir placeholders externos em último caso.
   - Avatar da editora no detalhe da notícia: `JULIA_AVATAR_URL=/static/img/julia-avatar.png` (ou URL externa).
   - Para o Roberto, `GEMINI_MODEL_FRETE` define o modelo preferencial de análise; há fallback automático para evitar indisponibilidade.

---

## Diretriz UX de Contraste (tema dark)

- O projeto usa tema escuro global em `static/css/agentefrete-theme.css`; por isso, evite definir `color: #...` inline em templates para corpo de texto.
- Para conteúdo editorial/rico (HTML vindo do banco), aplique classes reutilizáveis de leitura:
  - Superfície do card: `af-readable-surface`
  - Bloco de conteúdo: `af-readable-content`
- Essas classes padronizam contraste de `p`, `li`, `span`, `div`, `blockquote`, `td`, `th`, além de links e headings, evitando regressões de legibilidade em páginas novas.

---

## 1. Ambiente de Desenvolvimento (DEV)

*Características:* Debug ATIVO, Reload automático, Logs no console.

**Comando (Windows PowerShell):**
```powershell
$env:APP_ENV="dev"; python app/web.py
```

**Comando (Linux/Mac):**
```bash
APP_ENV=dev python -m app.web
```

---

## 2. Ambiente de Homologação (HOMOLOG)

*Características:* Debug OFF, Simulação de Produção, Logs INFO.

**Comando (Windows PowerShell):**
```powershell
$env:APP_ENV="homolog"; python app/web.py
```

**Comando (Linux/Mac - Via Gunicorn - Recomendado):**
```bash
APP_ENV=homolog gunicorn -w 2 -b 0.0.0.0:8000 app.web:app
```

---

### 2.1 Configuração de e-mail (recuperação de senha via Resend)

- O fluxo de “esqueci minha senha” usa a API do Resend a partir de `app/auth_services.py`.
- Configure no `.env.{APP_ENV}` dentro de `app/`:

```env
MAIL_DEFAULT_SENDER=noreply@agentefrete.com.br
MAIL_FROM=noreply@agentefrete.com.br
RESEND_API_KEY=sua_resend_api_key_aqui
```

- `MAIL_USERNAME` permanece opcional e é usado apenas para bootstrap/admin (ver `app/infra.py` e `app/auth_services.py`); não é mais utilizado como conta SMTP.

## 3. Diagnóstico OAuth (opcional)

As rotas de ops (`app/ops_routes.py`) incluem:
- `GET /health` — health check (não exige token).
- `GET /oauth-diagnostics` — estado do OAuth; exige header `X-Ops-Token`.
- `POST /ops/user-audit`, `POST /ops/promote-admin` e `POST /ops/reset-pautas` — exigem `X-Ops-Token`.

Exemplo de diagnóstico OAuth:
```bash
curl -H "X-Ops-Token: SEU_OPS_TOKEN" http://127.0.0.1:5000/oauth-diagnostics
```

### Hotfix OAuth (state/CSRF)

- O fluxo de login Google agora suporta mais de um `state` pendente na mesma sessão,
  evitando falso erro de segurança quando o usuário inicia o OAuth mais de uma vez
  antes do callback retornar.
- Implementação no backend: `app/web.py` (`/login/google` e `/login/google/callback`).
- Mensagem relacionada na UI: `Falha na validação de segurança. Tente novamente.`

Checklist pós-deploy:

1. Abrir a página de login em janela anônima.
2. Clicar uma única vez em "Entrar com Google".
3. Concluir consentimento Google e confirmar callback sem erro.
4. Se houver falha de state, limpar cookies da origem e repetir.
5. Validar `GET /oauth-diagnostics` com `X-Ops-Token`.

---

## 4. Executar Cleiton (orquestrador gerencial)

- **Pela rota (usuário logado):** `POST /executar-cleiton` — o `web.py` delega para `run_cleiton.executar_orquestracao(app)`, que por sua vez chama o orquestrador gerencial.
- **Script em loop:** na pasta do projeto (raiz ou `app`), com `APP_ENV` definido:
  ```bash
  APP_ENV=dev python -m app.run_cleiton
  ```
  ou, a partir de `app/`:
  ```bash
  python run_cleiton.py
  ```
  O intervalo entre ciclos vem da regra persistida `frequencia_horas` (config gerencial); padrão 3h.

---

## 5. Pautas para a Júlia (Etapa 2)

O pipeline da Júlia consome pautas da tabela `Pauta` (bind noticias). Há dois caminhos principais:

- **Notícias (automático):** o Scout (`run_cleiton_agente_scout.py`), chamado pelo ciclo do Cleiton, coleta notícias de fontes configuradas via `SCOUT_SOURCES_JSON` (consulte `app/.env.example` para o formato detalhado) e insere registros em `Pauta` com `tipo='noticia'` e `status_verificacao='pendente'`. Não é necessário inserir pautas de notícia manualmente.
- Em caso de feed inválido/inacessível, o Scout registra erro da fonte e continua para as demais (não interrompe o ciclo completo).
- **Artigos (manual/assistido):** artigos continuam aceitando input manual. Você pode inserir linhas na tabela `pautas` com `tipo='artigo'` (ou usar importadores legados) para abastecer a Júlia com conteúdos mais profundos.

Para semear pautas a partir do arquivo legado:

```python
from app.web import app
from app.news_ai import popular_pautas_de_arquivo_json
with app.app_context():
    popular_pautas_de_arquivo_json()  # usa app/processadas.json se existir
```

Ou passe o caminho e tipo: `popular_pautas_de_arquivo_json("/caminho/arquivo.json", tipo_padrao="artigo")`.

**Fase 3:** pautas importadas ou coletadas pelo Scout ficam com `status_verificacao=pendente`; o Verificador (`run_cleiton_agente_verificador.py`) calcula um score de confiabilidade considerando:

- recência da pauta (para notícias, com pesos configuráveis por `VERIFICADOR_RECENCIA_*`),
- confiabilidade da fonte (`VERIFICADOR_FONTES_CONFIAVEIS` / `VERIFICADOR_BLOQUEAR_DOMINIOS`),
- deduplicação semântica básica por similaridade de título (`VERIFICADOR_SIMILARIDADE_TITULO`),
- presença de termos de relevância simples para logística/supply chain/frete (`VERIFICADOR_TERMOS_RELEVANTES`).

Ao final, o Verificador define `status_verificacao` como `aprovado`, `revisar` ou `rejeitado`. Só pautas **aprovadas** são consumidas pela Júlia. Para abastecer automaticamente notícias, configure `SCOUT_SOURCES_JSON` (ver `.env.example`); artigos seguem aceitando input manual.

### Formato obrigatório de `SCOUT_SOURCES_JSON` (evita coleta zerada)

- Use **JSON válido em uma única linha**.
- Não use comentários dentro do JSON.
- Não quebre em múltiplas linhas.
- Recomendado: não usar espaço ao redor do `=`.

Exemplo válido:

```env
SCOUT_SOURCES_JSON=[{"url":"https://news.google.com/rss/search?q=logistica&hl=pt-BR&gl=BR&ceid=BR:pt-419","tipo":"noticia","tipo_fonte":"rss"},{"url":"https://g1.globo.com/rss/g1/economia/","tipo":"noticia","tipo_fonte":"rss"}]
```

Exemplo inválido (não usar):

```env
SCOUT_SOURCES_JSON = [
# comentario
{"url":"https://g1.globo.com/rss/g1/economia/","tipo":"noticia","tipo_fonte":"rss"}
]
```

Se quiser restringir fontes no Verificador, use domínios separados por vírgula:

```env
VERIFICADOR_FONTES_CONFIAVEIS=valor.globo.com,g1.globo.com,transportemoderno.com.br,logweb.com.br,portosenavios.com.br,tecnologistica.com.br,supplychaindive.com,supplychainbrain.com,logisticsmgmt.com,freightwaves.com
VERIFICADOR_BLOQUEAR_DOMINIOS=example-spam.com,agregador-ruido.net,dominio-suspeito.xyz
```

Observação: o match de domínio é exato (`valor.globo.com` é diferente de `globo.com`).

---

## 6. Testes (Fase 6 – suite robusta)

- **Sprint 4 (meta diaria e fallback):** `python -m unittest app.tests.test_fase4_meta_diaria -v`
- **Sprint 5 (estado de serie):** `python -m unittest app.tests.test_fase5_estado_serie -v`
- **Fase 5 (insight):** `python -m unittest app.tests.test_fase5_insight -v`
- **Sprint 6 (admin pautas e series):** `python -m unittest app.tests.test_sprint6_admin_pautas_e_series -v`
- **Fase 6 (encerramento):** `python -m unittest app.tests.test_fase6_encerramento -v`

Execute a partir da raiz do projeto com `PYTHONPATH` apontando para a raiz e `APP_ENV=dev`. Testes que dependem do app Flask (ex.: rotas, contexto de BD) podem ser ignorados (skip) se dependências não estiverem disponíveis; os demais validam parser, classificação, payload, regressão e o alinhamento de `/executar-insight` ao mesmo ciclo completo disparado por `/executar-cleiton`.

---

## 7. Homolog – validação do bypass e do cron

Esta seção resume como validar o fluxo completo da Júlia em **homolog**, tanto pelo botão de bypass quanto pelo agendamento automático.

- **Pré-requisitos em homolog**
  - Serviço web subindo com `APP_ENV=homolog` (via variável de ambiente ou arquivo `.env.homolog` carregado por `env_loader.py`).
  - Variável `CRON_SECRET` definida no ambiente homolog (veja `app/.env.example` e `RENDER_CRON_HOMOLOG.md`).
  - `SCOUT_ENABLED=true` e `SCOUT_SOURCES_JSON` configurado com JSON **válido em uma linha** e pelo menos uma fonte funcional.

- **7.1 – Validar botão “Executar agora (bypass)” no painel admin**
  1. Acesse `/login`, entre com um usuário admin.
  2. Vá em `Admin` → `Agentes - Júlia`.
  3. Clique em **Executar agora (bypass de frequencia)**.
  4. Mensagem esperada no topo:
       - Em homolog/prod (modo async padrão): `Execução do Cleiton iniciada em segundo plano. Acompanhe os logs para status final.`.
       - Se já houver execução em andamento: `Já existe uma execução do Cleiton em andamento. Aguarde a conclusão.`.
       - Em dev (modo sync): texto contendo o motivo e os contadores do Scout/Verificador, por exemplo  
         `Scout: inseridas=..., reativadas=..., ignoradas=..., erros=... | Fontes Scout: processadas=..., com_erro=..., sem_itens=... | Verificador: aprovadas=...`.
     - Se não houver pauta elegível (nenhuma pauta com `status_verificacao` permitido): a mensagem pode indicar falha de publicação; o detalhe auditável fica registrado em `AuditoriaGerencial` com `tipo_decisao="julia"` e decisão `"Nenhuma pauta elegível para processamento"`.
    5. Em modo async, o resultado final fica no log com `Cleiton admin (async) concluído: status=... mission_id=... motivo=...`.
    6. Após sucesso, confirme que existe uma nova linha em `noticias_portal` e que a home (`/`) exibe a notícia recente.

  Observação importante: o Scout pode reativar pautas em `status="falha"` quando o mesmo link reaparece e ainda não existe publicação em `noticias_portal`. Isso evita ficar preso em ciclos com muitas duplicatas históricas.

- **7.2 – Validar rota de cron `/cron/executar-cleiton`**
  1. Sem segredo (apenas para teste rápido de deploy):
     ```bash
     curl -i "https://SEU_DOMINIO_HOMOLOG/cron/executar-cleiton"
     ```
     - Esperado: **403 Unauthorized** (rota existe e está protegida).
     - Se retornar **404**, o problema é de deploy/roteamento (serviço/branch errado ou domínio apontando para outro backend).
  2. Com segredo correto:
     ```bash
      curl -X POST -H "X-Cron-Secret: SEU_CRON_SECRET" -H "Cache-Control: no-cache, no-store, must-revalidate" -H "Pragma: no-cache" "https://SEU_DOMINIO_HOMOLOG/cron/executar-cleiton?ts=$(date +%s)"
     ```
     - Esperado: `HTTP 200` com JSON `{ "ok": true|false, "status": "...", "motivo": "...", "mission_id": "..." }`.
     - Se `status` for `"ignorado"`, verifique frequência/janela (`run_cleiton_agente_regras.py` e painel `Agentes - Júlia`).
      - Se o mesmo `mission_id` se repetir em execuções consecutivas, há indício de cache na borda (Cloudflare). Configure regra para **bypass de cache** no path `/cron/*`.

- **7.3 – Validar execução automática (Cron/worker)**
  - Configure o Cron Job conforme `RENDER_CRON_HOMOLOG.md` (ou o worker `python -m app.run_cleiton` com `APP_ENV=homolog`).
  - Após o horário agendado:
    - Em `Admin` → `Agentes - Júlia`, confira:
      - **Última execução (ciclo automático)** atualizada.
      - **Próxima execução prevista** consistente com a frequência configurada.
    - Verifique se novas notícias foram inseridas em `noticias_portal` e aparecem na home (`/`) e na rota de detalhe `/noticia/<id>`.

  ### Ajustes operacionais de timeout (homolog/prod)

  Em ambientes com latência externa maior (RSS/IA), ajuste no ambiente do serviço:

  ```env
  GUNICORN_TIMEOUT_SECONDS=120
  GUNICORN_GRACEFUL_TIMEOUT_SECONDS=30
  GUNICORN_KEEPALIVE_SECONDS=5
  GEMINI_HTTP_TIMEOUT_MS=20000
  GEMINI_IMAGE_HTTP_TIMEOUT_MS=20000
  ```

  ---

  ## 8. Homolog – índices do ticker da Home (2x ao dia)

  Objetivo: manter a faixa da Home (`/`) atualizada com **Petróleo, BDI, FBX e Dólar** sem depender do carregamento da página.

  - **Fonte da coleta:** `python -m app.finance`
  - **Arquivo persistido:** `app/indices.json`
  - **Leitura na web:** `GET /` (rota `index` em `app/web.py`)

### 8.1 Agendamento recomendado (homolog/prod)

Crie um job dedicado para índices com duas execuções diárias (abertura do mercado e após 14h).

Exemplo de referência de horários (ajuste ao fuso da operação):
- `0 9 * * 1-5`
- `10 14 * * 1-5`

O comando deve sempre respeitar a configuração centralizada de ambiente:

```bash
APP_ENV=homolog python -m app.finance
```

Neste fluxo, `app/finance.py` utilizará `app/settings.py` para resolver o caminho persistente dos índices de forma consistente com o serviço web.

### 8.2 Validação rápida pós-agendamento

1. Execute uma coleta manual no mesmo ambiente:
   ```bash
   APP_ENV=homolog python -m app.finance
   ```
2. Verifique se o arquivo apontado por `INDICES_FILE_PATH` (resolvido por `settings.indices_file_path`) recebeu novo registro em `historico`.
3. Abra `/` e confirme valores visíveis no ticker (sem campos vazios).
4. Se o ticker ficar vazio, valide o contrato: JSON em formato histórico (ou legado) + extração do último registro na rota `/`. Enquanto a migração completa para banco não estiver concluída, este JSON continua sendo a fonte de verdade dos índices.
