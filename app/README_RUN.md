# 🏃 Guia de Execução Local


## 📢 Changelog - Última Atualização (Mar 2026)

- **Roberto Intelligence (upload / localidades):** documentação alinhada ao código: upload resolve `base_localidades` com **uma consulta em lote** (`WHERE chave_busca IN (...)`), **mapa em memória** no loop e **sem** consulta por linha; `get_localidade_completa_por_chave` documentada com **igualdade direta** em `chave_busca`. Removidas referências obsoletas a `LOWER(TRIM(chave_busca))` no fluxo de upload.

- **Termo de Aceite implementado:**
  - Checkbox obrigatório para aceite dos Termos de Uso nas telas de cadastro e complete-profile.
  - Link dinâmico para download/visualização do PDF do termo vigente.
  - Registro do aceite no banco de dados (coluna accepted_terms_at em user).
  - Painel admin permite upload e versionamento do termo (tabela terms_of_use).
  - Notificação automática por e-mail para usuários ativos após atualização do termo.
  - Opção de encerramento de contrato e anonimização de dados no perfil do usuário.
  - Scripts de migração para atualização suave do banco (sem perda de dados).
  - Processo validado em dev, homolog e produção, com checklist de deploy e testes automatizados.

- **Documentação e equipe informada:**
  - Este changelog e o README_DEPLOY.md foram atualizados.
  - Equipe notificada sobre as mudanças e novos fluxos.

- **Ciclo de atualização completo:**
  - Merge e validação de todas as alterações recentes das branches de feature/dev para homolog e produção.
  - Testes automatizados executados e validados em todos os ambientes.
  - Hardening de ambiente: validação reativa de paths críticos, bloqueio de fallback em produção/homolog.
- Área do Usuário criada: acesso pelo avatar, cards de Segurança, Pagamento e Notificações.
- Painel ADM exclusivo para admin, visível na área do usuário.
- Blueprint `user_bp` registrado em `app/web.py`.
- Template `user_area.html` implementado.
- Testes automatizados em `app/tests/test_user_area.py`.
- Ajustes de UX na navegação e login.
- Deploy validado em homologação e atualizado em produção.

**Checklist final executado:**
- Documentação e changelog atualizados.
- Equipe informada sobre ciclo de atualização, merges e validações.


Este projeto utiliza variáveis de ambiente para alternar entre configurações de Desenvolvimento, Homologação e Produção. A lógica de autenticação está em `app/auth_services.py`; a infraestrutura em `app/infra.py`; as rotas operacionais (diagnóstico OAuth, auditoria de usuários, promote-admin, reset de pautas e health) estão em `app/ops_routes.py` (Blueprint). O `web.py` apenas expõe as rotas e registra os blueprints.

---

## Ambientes, PostgreSQL e arquivos `.env` (leitura obrigatória)

### Diferença entre dev local, homolog e prod

| | **dev** (local) | **homolog** | **prod** |
|---|-------------------|-------------|----------|
| `APP_ENV` | `dev` | `homolog` | `prod` |
| Arquivo carregado pelo app | `app/.env.dev` | `app/.env.homolog` | `app/.env.prod` |
| PostgreSQL (referência) | Neste projeto, o estado **validado** no Windows usou **PostgreSQL 18** como host do banco de trabalho. | **PostgreSQL 16** | **PostgreSQL 16** |
| Persistência de dados/índices | Fallback local em `app/` permitido se não houver volume configurado. | Obrigatório volume persistente (`APP_DATA_DIR` / `RENDER_DISK_PATH` / caminho em servidor). Boot falha se só houver filesystem efêmero. | Idem homolog. |

Homolog e prod **não** usam a instalação PostgreSQL da sua máquina de desenvolvimento; o que importa é sempre o destino em `DATABASE_URL` **e** que essa URI aponte para o **serviço/cluster correto**.

### `APP_ENV` obrigatório e ordem de carregamento

1. **`APP_ENV` deve estar definido no processo** antes de qualquer `import` que carregue `app/settings.py` (por exemplo: `$env:APP_ENV="dev"` no PowerShell, `export APP_ENV=dev` no bash, variável no systemd/Render/DigitalOcean).
2. `app/settings.py` lê **somente** `os.environ` para decidir o ambiente. Se `APP_ENV` estiver vazio, o boot **falha** com `RuntimeError` — **não** existe fallback implícito para `dev`.
3. **Depois** dessa validação, `app/env_loader.load_app_env()` carrega **apenas** o arquivo `app/.env.{APP_ENV}` (caminho absoluto; não depende do diretório atual do terminal). Não existe `app/.env` genérico no fluxo principal.
4. O arquivo `.env.{APP_ENV}` pode conter uma linha `APP_ENV=...` para documentação, mas **`load_dotenv(override=False)` não sobrescreve** variáveis já definidas no ambiente. O ponto crítico continua sendo: **definir `APP_ENV` ao iniciar o Python**, não apenas dentro do arquivo.
5. **Exceção (somente migrações):** `migrations/env.py` define `APP_ENV=dev` se estiver ausente, para permitir `alembic` sem export manual. Isso **não** se aplica ao Gunicorn, `python -m app.web` nem aos workers (`run_cleiton`, etc.).

Valores aceitos: `dev`, `homolog`, `prod` (minúsculas; normalização em `app/settings.py`).

### `APP_ENV` — verificação imediata (obrigatório antes de `python` / `gunicorn`)

Na **mesma** janela de terminal em que você vai subir o processo:

| Shell | Depois de definir a variável | Comando | Esperado |
|-------|------------------------------|---------|----------|
| PowerShell | `$env:APP_ENV="dev"` (ou `homolog` / `prod`) | `echo $env:APP_ENV` | Uma linha com exatamente `dev`, `homolog` ou `prod` — **nunca** vazio. |
| Bash | `export APP_ENV=dev` | `echo $APP_ENV` | Idem. |

Se o `echo` vier **vazio**, **não** execute o app: o boot falhará com `APP_ENV obrigatório` (`app/settings.py`). Em servidores com systemd, confira `Environment=` no unit (ou `systemctl show <serviço> -p Environment`). No Render, confira o painel **Environment** do serviço (não há shell; o painel é a fonte da verdade).

### Papel de cada arquivo

| Arquivo | Uso |
|---------|-----|
| `app/.env.example` | Modelo versionado, **sem segredos**. Copie para criar `.env.dev` / `.env.homolog` / `.env.prod`. **Não** é lido automaticamente como fonte única de configuração. |
| `app/.env.dev` | Desenvolvimento local. Carregado quando `APP_ENV=dev`. |
| `app/.env.homolog` | Homologação. Carregado quando `APP_ENV=homolog`. |
| `app/.env.prod` | Produção. Carregado quando `APP_ENV=prod`. |

Arquivos `.env.*` fora da pasta `app/` **não** entram no loader principal (`app/env_loader.py`).

### PostgreSQL: instância correta ≠ nome do banco

- A URI usa host, porta, usuário, senha e **nome do banco**. Ter `localhost:5432` e o nome do banco “corretos” na URL **não garante** o cluster certo: vale o processo PostgreSQL que **está escutando** essa porta (incidente validado: dois clusters na mesma porta; um vazio, outro com dados).
- Dois serviços PostgreSQL distintos na mesma máquina não podem escutar a mesma porta ao mesmo tempo; quem estiver **ativo** em `5432` é quem recebe `localhost:5432`.
- **Sintoma típico de instância errada:** aplicação sobe, mas listas/admin parecem **vazios**, migrações “sumiram” ou tabelas não existem — **sem** necessariamente haver perda de dados no cluster onde o banco realmente vive.
- **Homolog/prod** usam PostgreSQL **16**. No **dev local** validado neste repositório, o banco de trabalho estava no PostgreSQL **18**; manter dois clusters (16 e 18) ambos na porta **5432** é um cenário de alto risco até que apenas um escute essa porta ou até usar portas distintas na URI (`DATABASE_URL`).

### Validação segura no Windows (antes de culpar senha ou código)

1. **Serviços:** `services.msc` — identifique `postgresql-x64-16` e `postgresql-x64-18` (nomes podem variar). Anote qual está **Em execução** e qual está **Parado/Manual**.
2. **Regra operacional que evitou regressão no incidente documentado:** manter **um** cluster como serviço ativo (o que contém o banco de trabalho), o outro **parado** ou **manual** para não disputar a porta.
3. **Porta:** confirme qual processo escuta `5432` (PowerShell como administrador: `Get-NetTCPConnection -LocalPort 5432` e correlacione com o PID/serviço).
4. **Conexão direta:** `psql` ou cliente GUI apontando para **a mesma** host/porta/usuário/senha de `DATABASE_URL` — confira `SELECT current_database(), inet_server_addr(), version();` e se as tabelas esperadas existem (`\dt`).
5. **Pela aplicação:** nos logs de boot, a URI é diagnosticada com host/porta/database mascarados (`app/env_loader.log_database_boot_diagnostics` em `app/web.py`). Compare com o que você validou no passo 4.

### Boot da aplicação (diretório e comando)

- **Diretório de trabalho:** clone na **raiz do repositório** (pasta que contém o pacote `app/` e `requirements.txt`).
- **Windows (PowerShell), a partir da raiz:**
  ```powershell
  $env:APP_ENV="dev"
  echo $env:APP_ENV
  python app/web.py
  ```
  Se `echo $env:APP_ENV` não mostrar `dev`, não prossiga.
- **Linux/macOS, a partir da raiz:**
  ```bash
  export APP_ENV=dev
  echo $APP_ENV
  python -m app.web
  ```
  Se `echo $APP_ENV` estiver vazio, não prossiga.
- Rodar a partir de uma pasta que **não** seja a raiz (ou sem o pacote `app` no `PYTHONPATH`) costuma gerar `ModuleNotFoundError: No module named 'app'`. Sempre suba a partir da raiz ou defina `PYTHONPATH` para a raiz explicitamente.
- Dependências: na raiz, `pip install -r requirements.txt` (o guia antigo referia `../requirements.txt` a partir de `app/` — equivalente: `pip install -r requirements.txt` com o shell na raiz).

### Roberto Intelligence (BI de fretes): upload, localidades e sessão

- **Banco:** um único PostgreSQL (`DATABASE_URL`). A tabela `base_localidades` está nesse banco; **não** há bind alternativo nem SQLite para esse fluxo.

#### Upload (.xlsx) — fluxo atual

- **Arquivo:** `app/upload_handler.py` (rota `POST /api/roberto/upload` em `app/web.py`). Valida colunas obrigatórias, lê o `.xlsx` em modo `read_only` e **materializa as linhas em uma lista** (o openpyxl só permite iterar a aba uma vez nesse modo; a lista permite pré-coleta + processamento sem reabrir o arquivo).
- **Chaves de localidade:** para montar `cidade-uf` em minúsculas (ex.: `cariacica-es`), o código usa `strip` + `lower` nas células, como antes. **Cada linha da planilha continua sendo um serviço distinto** — não há deduplicação de linhas no resultado; apenas um **conjunto de chaves únicas** (origem/destino) é montado para consultar o banco **uma vez**.
- **Consulta em lote:** `carregar_localidades_por_chaves` em `app/infra.py` executa **uma** query: `SELECT ... FROM base_localidades WHERE chave_busca IN (...)` (lista com parâmetro `expanding` no SQLAlchemy). O resultado vira um **mapa em memória** (`chave →` dados de localidade). No loop por linha **não há** `engine.connect()` nem SELECT por linha para resolver cidade/UF — só leitura do mapa.
- **Motivação:** o desenho anterior (consulta(s) por linha) gerava **N+1**, custo alto de round-trip e, com o predicado antigo na coluna, tendência a **Seq Scan**; em planilhas grandes isso contribuía para **timeout de worker**. O lote com **igualdade direta** em `chave_busca` usa o índice/PK e reduz drasticamente o número de idas ao banco durante o upload.
- **Lookup fora do upload:** `get_localidade_completa_por_chave` em `app/infra.py` (usada por outros módulos, ex. `get_id_localidade_por_chave`) consulta com `WHERE chave_busca = :c` e parâmetro já normalizado — **sem** `LOWER`/`TRIM` na expressão da coluna na SQL.

#### Dados em `base_localidades`

- **`chave_busca`:** persistida **normalizada** (minúsculas, sem espaços nas bordas), alinhada ao contrato `cidade-uf`. A aplicação continua normalizando a **entrada** com `strip()` e `lower()` antes de comparar.

#### Sessão, payload e BI

- **Payload na sessão (por linha):** campos financeiros/operacionais (`data_emissao`, `peso_real`, `valor_nf`, `valor_frete_total`, `modal`, opcional `valor_imposto`) mais `id_cidade_origem`, `id_uf_origem`, `uf_origem`, `id_cidade_destino`, `id_uf_destino`, `uf_destino` (UF em duas letras, alinhada a `uf_nome` da base).
- **Efemeridade:** os dados ficam em chaves de sessão (`roberto_upload_data`); há TTL; **nenhuma** linha da planilha é gravada em tabela de negócio pelo upload. Após amostragem por mês (grandes volumes), a lista reduzida é a que entra na sessão.
- **BI (`app/roberto_bi.py`):** com upload ativo, o dataset é só o da sessão. **Rankings e filtros por UF usam `uf_origem` / `uf_destino` do payload.** `_enriquecer_ufs_cliente` só preenche UF a partir de `id_cidade` via `base_localidades` quando a UF ainda vier vazia (fallback, p.ex. sessões antigas).
- **Base ouro:** sem upload na sessão, o BI usa `frete_real` (persistido), com UFs já nas colunas do modelo quando presentes.

---

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
- `APP_ENV` é obrigatório em toda execução (`dev`, `homolog` ou `prod`), definido antes do boot; não há fallback silencioso para `dev`.
- `DATABASE_URL` é **obrigatória** e deve apontar para PostgreSQL (banco único). Sem fallback para outro SGBD. Arquivos de dados (índices, `last_admin_run.json`, etc.) continuam exigindo diretório persistente em homolog/prod (`APP_DATA_DIR` / `RENDER_DISK_PATH` / `/var/data` quando aplicável).
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

1. Instale as dependências a partir da **raiz do repositório**:
   ```bash
   pip install -r requirements.txt
   ```
   (Se o shell estiver em `app/`, use `pip install -r ../requirements.txt` — equivalente.)
  - O `requirements.txt` já inclui `google-generativeai` para integração com Gemini (Cleiton/Júlia/Roberto).
2. Garanta que os arquivos `app/.env.dev` e `app/.env.homolog` (e `app/.env.prod` se for subir prod localmente) existam.
   - O arquivo `.env` simples é legado e **não deve ser usado**.
   - Use `app/.env.example` como base, copiando para `.env.dev` / `.env.homolog` / `.env.prod` e ajustando apenas os valores.
   - A leitura desses arquivos é feita de forma centralizada por `app/settings.py`, que carrega `.env.{APP_ENV}` via `env_loader` em um único ponto antes de construir o objeto `settings`.
   - Para login com Google, defina `GOOGLE_OAUTH_REDIRECT_URI` (ex.: `http://127.0.0.1:5000/login/google/callback`) e, em dev, `OAUTHLIB_INSECURE_TRANSPORT=1`.
   - Camada gerencial (Cleiton) e demais domínios compartilham o **mesmo** PostgreSQL definido em `DATABASE_URL` no `.env.*`.
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

**Diretório:** raiz do repositório (veja seção “Ambientes, PostgreSQL e arquivos `.env`” acima).

**Comando (Windows PowerShell):**
```powershell
$env:APP_ENV="dev"
echo $env:APP_ENV
python app/web.py
```

**Comando (Linux/Mac):**
```bash
APP_ENV=dev python -m app.web
```
(Em sessão interativa, prefira `export APP_ENV=dev`, `echo $APP_ENV`, depois `python -m app.web`.)

---

## 2. Ambiente de Homologação (HOMOLOG)

*Características:* Debug OFF, Simulação de Produção, Logs INFO.

**Comando (Windows PowerShell):**
```powershell
$env:APP_ENV="homolog"
echo $env:APP_ENV
python app/web.py
```

**Comando (Linux/Mac - Via Gunicorn - Recomendado):**
```bash
export APP_ENV=homolog
echo $APP_ENV
gunicorn -w 2 -b 0.0.0.0:8000 app.web:app
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
- **Script em loop:** na **raiz** do repositório, com `APP_ENV` definido (confirme com `echo $APP_ENV` / `echo $env:APP_ENV`):
  ```bash
  APP_ENV=dev python -m app.run_cleiton
  ```
  A partir de `app/`, também é possível `python run_cleiton.py`, mas **`APP_ENV` ainda deve estar exportado** no ambiente (o script importa `app.settings`).
  O intervalo entre ciclos vem da regra persistida `frequencia_horas` (config gerencial); padrão 3h.

---

## 5. Pautas para a Júlia (Etapa 2)

O pipeline da Júlia consome pautas da tabela `Pauta` no banco único PostgreSQL. Há dois caminhos principais:

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

## 6. Troubleshooting (conexão, banco e “vazio”)

| Sintoma ou erro | O que verificar |
|-----------------|-----------------|
| `APP_ENV obrigatório` / `RuntimeError` em `app/settings.py` | Definir `APP_ENV` **no processo** antes de iniciar; confirmar com `echo $env:APP_ENV` (PowerShell) ou `echo $APP_ENV` (bash). Só ter `APP_ENV` dentro de `app/.env.dev` não basta para a primeira leitura. |
| `DATABASE_URL ausente ou inválida` | PostgreSQL na URI (`postgresql+psycopg2://...`). Valor em `app/.env.{APP_ENV}` ou variável de ambiente já exportada antes do `load_dotenv` (override=False preserva env). |
| `banco vazio` / listas sem dados / admin sem usuários | **Instância errada** na mesma porta (ver seção PostgreSQL acima). Confirmar com `psql` ou cliente a mesma URI e checar `version`, tabelas e contagem. Não assumir perda de dados no cluster correto. |
| `relation "..." does not exist` / tabela inexistente | Migrações não aplicadas nesse cluster **ou** cluster errado. Rodar Alembic contra o mesmo `DATABASE_URL` (com `APP_ENV` coerente para carregar `app/.env.{APP_ENV}`). |
| `password authentication failed` / senha inválida | Senha do usuário na URI diferente da do servidor PostgreSQL. **Atualizar** `DATABASE_URL` em `app/.env.dev` (ou ambiente) para coincidir com o servidor que você validou no `psql`. |
| Dois PostgreSQL (ex.: 16 e 18) na porta 5432 | Apenas um pode escutar a porta. Parar/manualizar o serviço errado; manter ativo o cluster com o banco de trabalho; ou usar **portas diferentes** e refletir na URI. |
| Porta em uso / `could not bind` | Outro processo na 5432 (outro PostgreSQL ou app). Identificar com `Get-NetTCPConnection` (Windows) / `ss -lntp` (Linux). |
| `ModuleNotFoundError: No module named 'app'` | Subir a partir da **raiz** do repo ou `PYTHONPATH` apontando para a raiz; `python -m app.web` exige layout `.../projeto/app/`. |
| `UnicodeDecodeError` no bootstrap (ver `app/infra.py`) | Encoding da URI/senha no `.env` (UTF-8; evitar bytes inválidos). Pode **mascarar** falha de autenticação — corrigir encoding e revalidar credenciais. |
| Dúvida: credencial vs instância errada | Se `psql` com a mesma URI conecta e vê dados, a credencial está ok; se a app “vê vazio”, confira **host/porta** (processo na porta) e se o arquivo `.env` carregado é o do `APP_ENV` atual. |

---

## 7. Testes (Fase 6 – suite robusta)

- **Sprint 4 (meta diaria e fallback):** `python -m unittest app.tests.test_fase4_meta_diaria -v`
- **Sprint 5 (estado de serie):** `python -m unittest app.tests.test_fase5_estado_serie -v`
- **Fase 5 (insight):** `python -m unittest app.tests.test_fase5_insight -v`
- **Sprint 6 (admin pautas e series):** `python -m unittest app.tests.test_sprint6_admin_pautas_e_series -v`
- **Fase 6 (encerramento):** `python -m unittest app.tests.test_fase6_encerramento -v`

Execute a partir da raiz do projeto com `PYTHONPATH` apontando para a raiz e `APP_ENV=dev`. Testes que dependem do app Flask (ex.: rotas, contexto de BD) podem ser ignorados (skip) se dependências não estiverem disponíveis; os demais validam parser, classificação, payload, regressão e o alinhamento de `/executar-insight` ao mesmo ciclo completo disparado por `/executar-cleiton`.

---

## 8. Homolog – validação do bypass e do cron

Esta seção resume como validar o fluxo completo da Júlia em **homolog**, tanto pelo botão de bypass quanto pelo agendamento automático.

- **Pré-requisitos em homolog**
  - `APP_ENV=homolog` definido no **ambiente do processo** (Render/systemd); o arquivo `app/.env.homolog` complementa variáveis, mas não substitui a necessidade de `APP_ENV` no serviço (ver seção “Ambientes, PostgreSQL e arquivos `.env`” acima).
  - Variável `CRON_SECRET` definida no ambiente homolog (veja `app/.env.example` e `RENDER_CRON_HOMOLOG.md`).
  - `SCOUT_ENABLED=true` e `SCOUT_SOURCES_JSON` configurado com JSON **válido em uma linha** e pelo menos uma fonte funcional.

- **8.1 – Validar botão “Executar agora (bypass)” no painel admin**
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

- **8.2 – Validar rota de cron `/cron/executar-cleiton`**
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

- **8.3 – Validar execução automática (Cron/worker)**
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

## 9. Homolog – índices do ticker da Home (2x ao dia)

Objetivo: manter a faixa da Home (`/`) atualizada com **Petróleo, BDI, FBX e Dólar** sem depender do carregamento da página.

- **Fonte da coleta:** `python -m app.finance`
- **Arquivo persistido:** em **homolog/prod**, o caminho vem de `INDICES_FILE_PATH` / `APP_DATA_DIR` (ver `app/settings.py`). Em **dev**, se não houver `INDICES_FILE_PATH`, costuma cair em arquivo sob o diretório de dados resolvido (frequentemente `app/indices.json`).
- **Leitura na web:** `GET /` (rota `index` em `app/web.py`)

### 9.1 Agendamento recomendado (homolog/prod)

Crie um job dedicado para índices com duas execuções diárias (abertura do mercado e após 14h).

Exemplo de referência de horários (ajuste ao fuso da operação):
- `0 9 * * 1-5`
- `10 14 * * 1-5`

O comando deve sempre respeitar a configuração centralizada de ambiente:

```bash
APP_ENV=homolog python -m app.finance
```

Neste fluxo, `app/finance.py` utilizará `app/settings.py` para resolver o caminho persistente dos índices de forma consistente com o serviço web.

### 9.2 Validação rápida pós-agendamento

1. Execute uma coleta manual no mesmo ambiente:
   ```bash
   APP_ENV=homolog python -m app.finance
   ```
2. Verifique se o arquivo apontado por `INDICES_FILE_PATH` (resolvido por `settings.indices_file_path`) recebeu novo registro em `historico`.
3. Abra `/` e confirme valores visíveis no ticker (sem campos vazios).
4. Se o ticker ficar vazio, valide o contrato: JSON em formato histórico (ou legado) + extração do último registro na rota `/`. Enquanto a migração completa para banco não estiver concluída, este JSON continua sendo a fonte de verdade dos índices.
