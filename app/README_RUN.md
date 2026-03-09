# 🏃 Guia de Execução Local

Este projeto utiliza variáveis de ambiente para alternar entre configurações de Desenvolvimento e Homologação. A lógica de autenticação está em `app/auth_services.py`; a infraestrutura em `app/infra.py`; as rotas operacionais (diagnóstico OAuth, auditoria de usuários, promote-admin, health) estão em `app/ops_routes.py` (Blueprint). O `web.py` apenas expõe as rotas e registra os blueprints.

**Camada gerencial (Cleiton):** Orquestração em `run_cleiton_agente_orquestrador.py`; regras em `run_cleiton_agente_regras.py`; dispatch em `run_cleiton_agente_dispatcher.py`; auditoria em `run_cleiton_agente_auditoria.py`. `run_cleiton.py` é fachada que delega ao orquestrador.

**Camada operacional (Júlia – Etapa 2):** Pipeline em `run_julia_agente_pipeline.py` (pauta → redação → imagem → qualidade → publicação). Pautas vêm da tabela `Pauta`; **apenas pautas com status_verificacao=aprovado** (Fase 3) seguem para a Júlia.

**Fase 3 – Scout + Verificador:** `run_cleiton_agente_scout.py` coleta pautas; `run_cleiton_agente_verificador.py` classifica (score, aprovado/revisar/rejeitado). Só aprovadas seguem para Júlia.

**Fase 4 – Designer + Publisher:** `run_julia_agente_designer.py` gera assets por canal (url_imagem_master, assets_por_canal); `run_julia_agente_publisher.py` publica no portal (obrigatório) e em canais configurados (linkedin, instagram, email em modo mock). Status por canal (pendente/publicado/falha/ignorado); duplicidade por canal bloqueada. Janela e intervalo entre posts podem ser aplicados nos canais externos por `PUBLISHER_JANELA_PUBLICACAO_*` e `PUBLISHER_INTERVALO_MINUTOS_ENTRE_POSTS`. Tabela `publicacao_canal` e colunas em NoticiaPortal (url_imagem_master, assets_canais_json, status_publicacao, publicado_em). Retenção de 18 meses inclui histórico de PublicacaoCanal.

**Fase 5 – Customer Insight:** `run_cleiton_agente_customer_insight.py` mede desempenho por conteúdo/canal, gera recomendações estratégicas (tema, tipo, canal, horário, frequência) e audita com `tipo_decisao=insight`. `run_julia_agente_metricas.py` consolida métricas em `InsightCanal`; recomendações em `RecomendacaoEstrategica`. `POST /executar-insight` aciona o ciclo completo do Cleiton (governança centralizada).

**Fase 6 – Encerramento:** Feedback loop estratégico: o orquestrador consome recomendações pendentes (prioridade DESC) antes do dispatch e aplica tema, tipo_missao e prioridade ao payload; em missão sucesso a recomendação é marcada como aplicada; em falha permanece pendente. Serviço de gestão: `listar_recomendacoes_pendentes`, `selecionar_recomendacao_prioritaria`, `parse_recomendacao_json`/`parse_contexto_json`, `atualizar_status_recomendacao` (com auditoria). Painel admin (Dashboard): KPIs de insight (pendentes, aplicadas, descartadas, total métricas, auditorias) e lista de recomendações recentes com ações **Aplicar** e **Descartar** (POST `/admin/recomendacoes/<id>/aplicar` e `/descartar`). Rotas principais: `/health`, `/executar-cleiton`, `/executar-insight` (ciclo completo), login/home. Suite de testes: `app/tests/test_fase5_insight.py` e `app/tests/test_fase6_encerramento.py` (unitários, integração, regressão Fases 3–5, smoke de rotas). Comando: `python -m unittest app.tests.test_fase6_encerramento -v` (a partir da raiz do projeto, com `PYTHONPATH` e `APP_ENV=dev`).

## Pré-requisitos

1. Instale as dependências:
   ```bash
   pip install -r ../requirements.txt
   ```
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
- `POST /ops/user-audit` e `POST /ops/promote-admin` — exigem `X-Ops-Token`.

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