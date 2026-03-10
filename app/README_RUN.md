# 🏃 Guia de Execução Local

Este projeto utiliza variáveis de ambiente para alternar entre configurações de Desenvolvimento e Homologação. A lógica de autenticação está em `app/auth_services.py`; a infraestrutura em `app/infra.py`; as rotas operacionais (diagnóstico OAuth, auditoria de usuários, promote-admin, reset de pautas e health) estão em `app/ops_routes.py` (Blueprint). O `web.py` apenas expõe as rotas e registra os blueprints.

**Camada gerencial (Cleiton):** Orquestração em `run_cleiton_agente_orquestrador.py`; regras em `run_cleiton_agente_regras.py`; dispatch em `run_cleiton_agente_dispatcher.py`; auditoria em `run_cleiton_agente_auditoria.py`. `run_cleiton.py` é fachada que delega ao orquestrador.

**Camada operacional (Júlia – Etapa 2):** Pipeline em `run_julia_agente_pipeline.py` (pauta → redação → imagem → qualidade → publicação). Pautas vêm da tabela `Pauta`; **apenas pautas com status_verificacao=aprovado** (Fase 3) seguem para a Júlia.

**Fase 3 – Scout + Verificador:** `run_cleiton_agente_scout.py` coleta pautas; `run_cleiton_agente_verificador.py` classifica (score, aprovado/revisar/rejeitado). Só aprovadas seguem para Júlia.

**Fase 4 – Designer + Publisher:** `run_julia_agente_designer.py` gera assets por canal (url_imagem_master, assets_por_canal); `run_julia_agente_publisher.py` publica no portal (obrigatório) e em canais configurados (linkedin, instagram, email em modo mock). Status por canal (pendente/publicado/falha/ignorado); duplicidade por canal bloqueada. Janela e intervalo entre posts podem ser aplicados nos canais externos por `PUBLISHER_JANELA_PUBLICACAO_*` e `PUBLISHER_INTERVALO_MINUTOS_ENTRE_POSTS`. Tabela `publicacao_canal` e colunas em NoticiaPortal (url_imagem_master, assets_canais_json, status_publicacao, publicado_em). Retenção de 18 meses inclui histórico de PublicacaoCanal.

**Fase 5 – Customer Insight:** `run_cleiton_agente_customer_insight.py` mede desempenho por conteúdo/canal, gera recomendações estratégicas (tema, tipo, canal, horário, frequência) e audita com `tipo_decisao=insight`. `run_julia_agente_metricas.py` consolida métricas em `InsightCanal`; recomendações em `RecomendacaoEstrategica`. `POST /executar-insight` aciona o ciclo completo do Cleiton (governança centralizada).

**Fase 6 – Encerramento:** Feedback loop estratégico: o orquestrador consome recomendações pendentes (prioridade DESC) antes do dispatch e aplica tema, tipo_missao e prioridade ao payload; em missão sucesso a recomendação é marcada como aplicada; em falha permanece pendente. Serviço de gestão: `listar_recomendacoes_pendentes`, `selecionar_recomendacao_prioritaria`, `parse_recomendacao_json`/`parse_contexto_json`, `atualizar_status_recomendacao` (com auditoria). Painel admin (Dashboard): KPIs de insight (pendentes, aplicadas, descartadas, total métricas, auditorias) e lista de recomendações recentes com ações **Aplicar** e **Descartar** (POST `/admin/recomendacoes/<id>/aplicar` e `/descartar`). Rotas principais: `/health`, `/executar-cleiton`, `/executar-insight` (ciclo completo), login/home. Suite de testes: `app/tests/test_fase5_insight.py` e `app/tests/test_fase6_encerramento.py` (unitários, integração, regressão Fases 3–5, smoke de rotas). Comando: `python -m unittest app.tests.test_fase6_encerramento -v` (a partir da raiz do projeto, com `PYTHONPATH` e `APP_ENV=dev`).

**Execução manual no Admin (hotfix homolog/prod):** A rota `/admin/agentes/julia/executar-cleiton` roda em **background** por padrão quando `APP_ENV` é `homolog` ou `prod`, evitando timeout de worker na requisição HTTP. Em `dev`, o padrão continua síncrono para facilitar validação local. É possível forçar via `ADMIN_CLEITON_EXEC_MODE=sync|async`.

## Pré-requisitos

1. Instale as dependências:
   ```bash
   pip install -r ../requirements.txt
   ```
  - O `requirements.txt` já inclui `google-generativeai` para integração com Gemini (Cleiton/Júlia/Roberto).
2. Garanta que os arquivos `.env.dev` e `.env.homolog` existam na pasta `app/`.
   - O arquivo `.env` simples é legado e **não deve ser usado**.
   - Use `app/.env.example` como base, copiando para `.env.dev` e `.env.homolog` e ajustando apenas os valores.
   - Para login com Google, defina `GOOGLE_OAUTH_REDIRECT_URI` (ex.: `http://127.0.0.1:5000/login/google/callback`) e, em dev, `OAUTHLIB_INSECURE_TRANSPORT=1`.
   - Para a camada gerencial, configure `DB_URI_GERENCIAL` (ex.: `sqlite:///gerencial.db`) no `.env.*`; se omitido, usa `app/gerencial.db`.
   - Para a Júlia, `GEMINI_MODEL_TEXT` permite definir o modelo preferencial; se indisponível, o sistema tenta fallback automático.
   - Para imagens da Júlia (insights/artigos), configure `IMAGE_PROVIDER=gemini`, `GEMINI_MODEL_IMAGE` e opcionalmente `GEMINI_MODEL_IMAGE_FALLBACK`.
   - Quando o retorno vem em bytes (sem URL pública), o sistema salva o arquivo em `app/static/generated/` e publica via URL local (`/static/generated/...`).
   - `IMAGEM_FALLBACK_URL` é opcional; se vazio, o sistema usa fallback visual temático para não exibir card quebrado.
   - Avatar da editora no detalhe da notícia: `JULIA_AVATAR_URL=/static/img/julia-avatar.png` (ou URL externa).
   - Para o Roberto, `GEMINI_MODEL_FRETE` define o modelo preferencial de análise; há fallback automático para evitar indisponibilidade.

---

## 1. Ambiente de Desenvolvimento (DEV)

*Características:* Debug ATIVO, Reload automático, Logs no console.

**Comando (Windows PowerShell):**
```powershell
$env:APP_ENV="dev"; python web.py
```

**Comando (Linux/Mac):**
```bash
APP_ENV=dev python web.py
```

---

## 2. Ambiente de Homologação (HOMOLOG)

*Características:* Debug OFF, Simulação de Produção, Logs INFO.

**Comando (Windows PowerShell):**
```powershell
$env:APP_ENV="homolog"; python web.py
```

**Comando (Linux/Mac - Via Gunicorn - Recomendado):**
```bash
APP_ENV=homolog gunicorn -w 2 -b 0.0.0.0:8000 web:app
```

---

## 3. Diagnóstico OAuth (opcional)

As rotas de ops (`app/ops_routes.py`) incluem:
- `GET /health` — health check (não exige token).
- `GET /oauth-diagnostics` — estado do OAuth; exige header `X-Ops-Token`.
- `POST /ops/user-audit`, `POST /ops/promote-admin` e `POST /ops/reset-pautas` — exigem `X-Ops-Token`.

Exemplo de diagnóstico OAuth:
```bash
curl -H "X-Ops-Token: SEU_OPS_TOKEN" http://127.0.0.1:5000/oauth-diagnostics
```

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

- **Fase 5:** `python -m unittest app.tests.test_fase5_insight -v`
- **Fase 6 (encerramento):** `python -m unittest app.tests.test_fase6_encerramento -v`

Execute a partir da raiz do projeto com `PYTHONPATH` apontando para a raiz e `APP_ENV=dev`. Testes que dependem do app Flask (ex.: rotas, contexto de BD) podem ser ignorados (skip) se dependências não estiverem disponíveis; os demais validam parser, classificação, payload, regressão e alinhamento de `/executar-insight` ao ciclo completo.

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