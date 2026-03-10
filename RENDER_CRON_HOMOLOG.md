# Cron Job no Render (homolog) – ciclo Cleiton a cada 1h

Para que a **próxima notícia seja publicada automaticamente** em homolog na frequência configurada (ex.: 1h), use o **Cron Job** do Render chamando a rota `/cron/executar-cleiton`.

## 1. Variável de ambiente no serviço web (homolog)

No Render → seu **Web Service** (backend) → **Environment**:

1. Adicione:
   - **Key:** `CRON_SECRET`
   - **Value:** um segredo forte (ex.: gere com `openssl rand -hex 32`).

2. Salve (o Render pode redeployar automaticamente).

## 2. Criar o Cron Job no Render

1. No **Dashboard** do Render, mesmo projeto do backend.
2. **Add New** → **Cron Job**.
3. Configuração:
   - **Name:** `cleiton-ciclo-homolog` (ou outro nome).
   - **Schedule:** `0 * * * *` (a cada hora em ponto; para a cada 30 min: `30 * * * *`).
   - **Command:** não usado para “hit URL”; deixe em branco ou um placeholder.
   - **Service:** selecione o **Web Service** do backend (ex.: `logcompleta-homolog`).

4. Em **Advanced** (ou na documentação do Render para Cron Jobs):
   - O Render Cron Job pode ser configurado para fazer um **HTTP request** ao seu serviço. Se a sua UI tiver “URL to hit” ou “Notify URL”:
     - **URL:** `https://homolog0514.agentefrete.com.br/cron/executar-cleiton`
     - **Secret:** o mesmo valor de `CRON_SECRET` (alguns Crons permitem header; senão use query: `?secret=SEU_CRON_SECRET`).

Se o Render **não** oferecer “URL to hit” no Cron Job, use a opção abaixo.

---

## Alternativa A: Cron Job com `curl` (recomendado)

O Cron Job do Render executa um **comando** em um schedule. Use `curl` para chamar a rota:

1. **Schedule:** `0 * * * *` (a cada hora em ponto; para 1h está de acordo com o quadro).
2. **Command:**
   ```bash
   curl -s -X POST -H "X-Cron-Secret: $CRON_SECRET" -H "Cache-Control: no-cache, no-store, must-revalidate" -H "Pragma: no-cache" "https://homolog0514.agentefrete.com.br/cron/executar-cleiton?ts=$(date +%s)"
   ```
   Ou, se o Cron não tiver acesso a variáveis de ambiente, use o segredo na URL (não compartilhe essa URL):
   ```bash
   curl -s "https://homolog0514.agentefrete.com.br/cron/executar-cleiton?secret=SEU_CRON_SECRET"
   ```
3. No **Environment** do Cron Job, adicione `CRON_SECRET` com o **mesmo valor** definido no Web Service (para o comando com `$CRON_SECRET`).

---

## Alternativa B: Serviço Background Worker (script em loop)

Se preferir um worker em vez de Cron:

1. **Add New** → **Background Worker**.
2. **Build Command:** igual ao do Web Service (ex.: `pip install -r requirements.txt`).
3. **Start Command:** `python -m app.run_cleiton`
4. **Environment:** copie as variáveis do Web Service (incluindo `APP_ENV=homolog`, `CRON_SECRET` não é necessário para o worker).
5. O script `run_cleiton.py` entra em loop e executa a cada `frequencia_horas` (ex.: 1h), respeitando a janela de publicação.

---

## 3. Validar

- Primeiro, confirme que **a rota existe** no ambiente homolog chamando **sem segredo** (deve responder 403, nunca 404):
  ```bash
  curl -i "https://homolog0514.agentefrete.com.br/cron/executar-cleiton"
  ```
  - Se o status for **403**, a rota está publicada e protegida corretamente (falta apenas o segredo).
  - Se o status for **404**, o deploy/roteamento está apontando para um serviço/versão **sem a rota** `"/cron/executar-cleiton"` (ajuste o serviço/branch ou o domínio no Cloudflare/Render antes de seguir).

- Após criar o Cron (ou worker), espere o horário da próxima execução (ou force uma chamada manual **com** segredo):
  ```bash
  curl -H "X-Cron-Secret: SEU_CRON_SECRET" "https://homolog0514.agentefrete.com.br/cron/executar-cleiton"
  ```
- Resposta esperada (200): `{"ok": true ou false, "status": "sucesso"|"ignorado"|"falha", "motivo": "...", "mission_id": "..."}`.
- Em **Admin** → **Agentes - Júlia**, a seção “Próxima notícia automática” deve mostrar a **última execução** atualizada após o cron rodar.
- Se o `mission_id` repetir em execuções seguidas, há cache na borda. No Cloudflare, crie uma **Cache Rule** para `path contains /cron/` com `Bypass cache` e execute `Purge Everything`.
