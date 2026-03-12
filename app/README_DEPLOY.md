# 🚀 Manual de Deploy - Produção (DigitalOcean)

Este guia cobre a instalação do projeto em um servidor Ubuntu usando Gunicorn, Systemd e Nginx.

## 1. Preparação do Servidor

Acesse o servidor via SSH e instale os pacotes básicos:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-venv nginx git -y
```

## 2. Estrutura de Pastas e Código

O app usa `app/infra.py` para banco e segurança; `app/ops_routes.py` (Blueprint) para `/health`, `/oauth-diagnostics`, `/ops/user-audit`, `/ops/promote-admin` e `/ops/reset-pautas`. Configure `OPS_TOKEN` para as rotas de diagnóstico e operação. A camada gerencial Cleiton usa o bind `gerencial` (`DB_URI_GERENCIAL`). A configuração de ambiente (incluindo o carregamento de `.env`) é centralizada em `app/settings.py`, que usa `app/env_loader.py` internamente — defina `APP_ENV=prod` no systemd para carregar `app/.env.prod` de forma consistente.

Importante: para `APP_ENV=prod`, o arquivo lido pela aplicação é `app/.env.prod` (dentro da pasta `app`). Arquivos `.env.*` fora dessa pasta não são usados pelo loader principal.

Vamos criar uma estrutura organizada em `/srv`.

```bash
# 1. Criar diretório do app
sudo mkdir -p /srv/logcompleta
sudo chown $USER:$USER /srv/logcompleta

# 2. Clonar o repositório (ou copiar arquivos)
git clone <SEU_REPO_GIT> /srv/logcompleta/code
# OU via SCP/SFTP se não tiver git remoto

# 3. Criar ambiente virtual
cd /srv/logcompleta/code
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install gunicorn
```

`requirements.txt` já inclui `google-generativeai`, necessário para integrações Gemini.

## 3. Configuração de Dados e Persistência

Para evitar perder dados em novos deploys, o banco de dados e os arquivos de índices **precisam** ficar fora da pasta do código.  
Homologação e Produção são tratados como ambientes com persistência **obrigatória**: se a aplicação detectar apenas filesystem efêmero para dados/índices, ela **falhará no boot** em vez de subir “saudável” com dados voláteis.

```bash
mkdir -p /srv/logcompleta/data
```

**Crie o arquivo `.env.prod` no servidor (lido por `app/settings.py`):**
`nano /srv/logcompleta/code/app/.env.prod`

Conteúdo (ajuste o diretório de dados, os caminhos do DB e a URL do site):
```ini
APP_ENV=prod
FLASK_DEBUG=False
SECRET_KEY=CHAVE_MUITO_SEGURA_E_LONGA_GERADA_ALEATORIAMENTE
LOG_LEVEL=WARNING

# Diretório de dados (persistente) – obrigatório em homolog/prod
# Em servidores próprios, use um caminho fora da pasta do código.
APP_DATA_DIR=/srv/logcompleta/data

# Caminhos absolutos para persistência (opcional).
# Se você omitir os DB_URI_*, o código usará APP_DATA_DIR/auth.db, APP_DATA_DIR/base_localidades.db etc.
DB_URI_AUTH=sqlite:////srv/logcompleta/data/auth.db
DB_URI_LOCALIDADES=sqlite:////srv/logcompleta/data/base_localidades.db
DB_URI_HISTORICO=sqlite:////srv/logcompleta/data/historico_frete.db
DB_URI_LEADS=sqlite:////srv/logcompleta/data/leads.db
DB_URI_NOTICIAS=sqlite:////srv/logcompleta/data/noticias.db
DB_URI_GERENCIAL=sqlite:////srv/logcompleta/data/gerencial.db

# Não defina OAUTHLIB_INSECURE_TRANSPORT em produção (ou use 0). OAuth deve usar HTTPS.
# Login Google: use a URL pública do seu domínio (auth em app/auth_services.py)
GOOGLE_OAUTH_REDIRECT_URI=https://SEU_DOMINIO/login/google/callback
# Hotfix OAuth state/CSRF: o callback aceita múltiplos states pendentes na sessão
# para evitar falso negativo quando o fluxo OAuth é iniciado mais de uma vez.

# E-mail (recuperação de senha via Resend)
# O envio de e-mails de recuperação de senha é feito via Resend API (sem SMTP).
# MAIL_USERNAME é usado apenas para bootstrap/admin; não é mais usado como conta SMTP.
MAIL_FROM=noreply@agentefrete.com.br
RESEND_API_KEY=RE_SUA_CHAVE_RESSEND_AQUI

# Token para rotas de operação e diagnóstico OAuth (header X-Ops-Token). Gere um valor secreto.
OPS_TOKEN=seu_token_secreto_aqui

# Chaves de API e modelos (Etapa 2: Júlia pipeline + imagem)
GEMINI_API_KEY=...
GEMINI_API_KEY_ROBERTO=...
GEMINI_API_KEY_1=...        # Júlia notícias
GEMINI_API_KEY_2=...        # Júlia artigos
GEMINI_MODEL_TEXT=gemini-2.5-flash
IMAGE_PROVIDER=gemini
GEMINI_MODEL_IMAGE=imagen-3.0-generate-002
# Fallback Gemini (multimodal) opcional para quando Imagen não retornar URL pública
# GEMINI_MODEL_IMAGE_FALLBACK=gemini-2.5-flash-image-preview
# Retentativas da camada de imagem
# IMAGE_RETRY_ATTEMPTS=3
# IMAGE_RETRY_BACKOFF_MS=800
# Fallback fotográfico contextual local (recomendado manter true)
# IMAGE_STOCK_FALLBACK_ENABLED=true
# Timeouts HTTP Gemini (ms)
GEMINI_HTTP_TIMEOUT_MS=20000
GEMINI_IMAGE_HTTP_TIMEOUT_MS=20000
# Timeouts Gunicorn (segundos)
GUNICORN_TIMEOUT_SECONDS=120
GUNICORN_GRACEFUL_TIMEOUT_SECONDS=30
GUNICORN_KEEPALIVE_SECONDS=5
# Índices da Home em storage persistente (obrigatório em homolog/prod).
# Este caminho será usado tanto pelo serviço web (rota `/`) quanto pelo job de coleta (`python -m app.finance`),
# via configuração centralizada em app/settings.py e validação rígida em app/env_loader.validate_runtime_env.
INDICES_FILE_PATH=/srv/logcompleta/data/indices.json
# Execução manual no painel admin: em homolog/prod o padrão já é async
# ADMIN_CLEITON_EXEC_MODE=async
# Opcional: fallback visual estático prioritário (CDN própria)
# IMAGEM_FALLBACK_URL=https://sua-cdn.com/imagens/fallback-logistica.jpg
# Se IMAGEM_FALLBACK_URL ficar vazio, o sistema tenta fallback contextual local (stock) e,
# só na indisponibilidade final, usa o asset versionado /static/img/fallback-capa-v1.svg
# Padrão recomendado: manter false para evitar imagem remota variável por refresh
# IMAGE_ALLOW_REMOTE_FALLBACK=false
# Avatar da editora (opcional)
# JULIA_AVATAR_URL=https://sua-cdn.com/imagens/julia-avatar.png
# Fase 3: Scout + Verificador (apenas pautas aprovadas vão para Júlia)
# SCOUT_ENABLED=true
# IMPORTANTE: SCOUT_SOURCES_JSON deve ficar em UMA linha, sem comentarios e sem quebra.
# SCOUT_SOURCES_JSON=[{"url":"https://g1.globo.com/rss/g1/","tipo":"noticia","tipo_fonte":"rss"},{"url":"https://news.google.com/rss/search?q=logistica&hl=pt-BR&gl=BR&ceid=BR:pt-419","tipo":"noticia","tipo_fonte":"rss"}]
# SCOUT_MAX_ITENS_POR_CICLO=20
# SCOUT_HTTP_TIMEOUT_SECONDS=10
# VERIFICADOR_SCORE_MINIMO=0.5
# VERIFICADOR_SIMILARIDADE_TITULO=0.85
# VERIFICADOR_MAX_REGISTROS_SIMILARIDADE=500
# VERIFICADOR_FONTES_CONFIAVEIS=valor.globo.com,g1.globo.com,transportemoderno.com.br,logweb.com.br,portosenavios.com.br,tecnologistica.com.br,supplychaindive.com,supplychainbrain.com,logisticsmgmt.com,freightwaves.com
# VERIFICADOR_BLOQUEAR_DOMINIOS=example-spam.com,agregador-ruido.net,dominio-suspeito.xyz
# Fase 4: Designer + Publisher (portal + canais; PUBLISHER_MODO=mock para canais externos)
# DESIGNER_ENABLED=true
# PUBLISHER_CANAIS_ATIVOS=portal,linkedin,instagram,email
# PUBLISHER_MODO=mock
# Fase 5: Customer Insight (métricas e recomendações; INSIGHT_COLETA_MODO=mock até APIs reais)
# INSIGHT_ENABLED=true
# INSIGHT_COLETA_MODO=mock
# INSIGHT_JANELA_DIAS=30
# INSIGHT_SCORE_ESCALAR=70
# INSIGHT_SCORE_PAUSAR=25
# INSIGHT_MIN_IMPRESSOES=100
# Logs de importação do painel admin (opcional; se ausente usa logs_fallback)
# LOG_DIR=/srv/logcompleta/logs
# Fase 6/Sprint 6: feedback loop e operações admin reutilizam as mesmas configs base.
```

Use credenciais distintas por ambiente (dev, homolog, prod) e rotacione imediatamente qualquer chave/tokens expostos.

Checklist rapido de validacao (Fase 3):
- `SCOUT_SOURCES_JSON` em linha unica e JSON valido.
- Sem comentarios dentro do valor de `SCOUT_SOURCES_JSON`.
- Em caso de restricao de fontes, informar domínios sem `https://` e sem caminho.
- Se uma fonte RSS falhar (parse/rede), o Scout registra erro dessa fonte e continua o ciclo com as demais.
- Reiniciar o serviço (`systemctl restart logcompleta`) após alterar `.env.prod`.

Checklist rapido de validacao (Indices da Home):
- A coleta de índices roda por `python -m app.finance` e atualiza o arquivo apontado por `INDICES_FILE_PATH`, resolvido por `app/settings.py`.
- A rota `/` deve exibir o último registro do histórico (`historico[-1]`) no ticker, lendo o mesmo caminho via `settings.indices_file_path`.
- Se houver formato histórico no JSON, a conversão para formato plano deve acontecer apenas na camada web (`index`), mantendo compatibilidade com o JSON legado simples enquanto durar a janela de transição.
- Em homolog/prod, `INDICES_FILE_PATH` deve apontar para storage persistente fora da pasta da release. Caso a configuração esteja incorreta, `env_loader.validate_runtime_env` agora interromperá o boot com erro explícito para evitar rodar em filesystem efêmero.

## 4. Configurar Gunicorn com Systemd

Crie o serviço para gerenciar o app:
`sudo nano /etc/systemd/system/logcompleta.service`

```ini
[Unit]
Description=Gunicorn instance to serve Log Completa
After=network.target

[Service]
User=root
Group=www-data
WorkingDirectory=/srv/logcompleta/code/app
Environment="PATH=/srv/logcompleta/code/venv/bin"
Environment="APP_ENV=prod"
ExecStart=/srv/logcompleta/code/venv/bin/gunicorn --config /srv/logcompleta/code/gunicorn_config.py --workers 3 --bind 127.0.0.1:5000 app.web:app

[Install]
WantedBy=multi-user.target
```

Ative o serviço:
```bash
sudo systemctl start logcompleta
sudo systemctl enable logcompleta
sudo systemctl status logcompleta
```

## 5. Configurar Nginx (Proxy Reverso)

Crie a configuração do site:
`sudo nano /etc/nginx/sites-available/logcompleta`

```nginx
server {
    listen 80;
    listen 80 default_server;
    server_name SEU_DOMINIO_OU_IP;

    location / {
        include proxy_params;
        proxy_pass http://127.0.0.1:5000;
    }
}
```

Ative e reinicie:
```bash
sudo ln -s /etc/nginx/sites-available/logcompleta /etc/nginx/sites-enabled
sudo nginx -t
sudo systemctl restart nginx
```

## 6. Backup Automático (Opcional)

Adicione no crontab (`crontab -e`) para backup diário às 03:00am:
```bash
0 3 * * * cp -r /srv/logcompleta/data /srv/logcompleta/backup_$(date +\%Y\%m\%d)
```

## 7. Agendamento de índices (homolog/prod)

Além do ciclo editorial do Cleiton, agende a coleta de indicadores da Home em dois horários diários.

Exemplo com cron (dias úteis):

```bash
# Abertura do mercado
0 9 * * 1-5 cd /srv/logcompleta/code && APP_ENV=prod /srv/logcompleta/code/venv/bin/python -m app.finance >> /var/log/logcompleta_indices.log 2>&1

# Após as 14h
10 14 * * 1-5 cd /srv/logcompleta/code && APP_ENV=prod /srv/logcompleta/code/venv/bin/python -m app.finance >> /var/log/logcompleta_indices.log 2>&1
```

Validação pós-configuração:
1. Rodar uma execução manual do comando com `APP_ENV=prod`.
2. Confirmar atualização do arquivo apontado por `INDICES_FILE_PATH` (ex.: `/srv/logcompleta/data/indices.json`).
3. Abrir `/` e conferir ticker com Petróleo, BDI, FBX e Dólar preenchidos.

## 8. Status de validação recomendado (pós-deploy)

Após cada deploy em homolog/prod, validar este fluxo mínimo:

1. `/health/liveness` responde 200.
2. `/health/readiness` responde 200 com `checks.database="ok"` e `checks.indices_exists=true`.
3. Job de índices executa com sucesso (`python -m app.finance` no mesmo `APP_ENV` do serviço).
4. O arquivo apontado por `INDICES_FILE_PATH` contém `historico` com pelo menos um registro.
5. Home (`/`) exibe os quatro indicadores sem campos vazios.
6. Fluxos de login e admin seguem sem regressão.
