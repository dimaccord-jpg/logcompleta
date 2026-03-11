# 🚀 Manual de Deploy - Produção (DigitalOcean)

Este guia cobre a instalação do projeto em um servidor Ubuntu usando Gunicorn, Systemd e Nginx.

## 1. Preparação do Servidor

Acesse o servidor via SSH e instale os pacotes básicos:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip python3-venv nginx git -y
```

## 2. Estrutura de Pastas e Código

O app usa `app/infra.py` para banco e segurança; `app/ops_routes.py` (Blueprint) para `/health`, `/oauth-diagnostics`, `/ops/user-audit` e `/ops/promote-admin`. Configure `OPS_TOKEN` para as rotas de diagnóstico e operação.

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

## 3. Configuração de Dados e Persistência

Para evitar perder dados em novos deploys, o banco de dados ficará fora da pasta do código.

```bash
mkdir -p /srv/logcompleta/data
```

**Crie o arquivo `.env.prod` no servidor:**
`nano /srv/logcompleta/code/app/.env.prod`

Conteúdo (Ajuste os caminhos do DB e a URL do site):
```ini
APP_ENV=prod
FLASK_DEBUG=False
SECRET_KEY=CHAVE_MUITO_SEGURA_E_LONGA_GERADA_ALEATORIAMENTE
LOG_LEVEL=WARNING

# Caminhos Absolutos para Persistência
DB_URI_AUTH=sqlite:////srv/logcompleta/data/auth.db
DB_URI_LOCALIDADES=sqlite:////srv/logcompleta/data/base_localidades.db
DB_URI_HISTORICO=sqlite:////srv/logcompleta/data/historico_frete.db
DB_URI_LEADS=sqlite:////srv/logcompleta/data/leads.db
DB_URI_NOTICIAS=sqlite:////srv/logcompleta/data/noticias.db

# Não defina OAUTHLIB_INSECURE_TRANSPORT em produção (ou use 0). OAuth deve usar HTTPS.
# Login Google: use a URL pública do seu domínio (auth em app/auth_services.py)
GOOGLE_OAUTH_REDIRECT_URI=https://SEU_DOMINIO/login/google/callback

# E-mail (recuperação de senha)
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USE_TLS=True
MAIL_USERNAME=...
MAIL_PASSWORD=...

# Token para rotas de operação e diagnóstico OAuth (header X-Ops-Token). Gere um valor secreto.
OPS_TOKEN=seu_token_secreto_aqui

# Chaves de API
GEMINI_API_KEY=...          # Cleiton (orquestrador)
GEMINI_API_KEY_ROBERTO=...
GEMINI_API_KEY_1=...
GEMINI_API_KEY_2=...
```

Neste momento, você pode utilizar os mesmos valores de chave de API em DEV, HOMOLOG e PROD; a diferença entre ambientes é controlada principalmente por `APP_ENV` e pelos caminhos de banco de dados.

Checklist rapido de validacao (Indices da Home):
- A coleta de índices roda por `python -m app.finance` e atualiza `app/indices.json`.
- A rota `/` deve exibir o último registro do histórico (`historico[-1]`) no ticker.
- Se houver formato histórico no JSON, a conversão para formato plano deve acontecer apenas na camada web (`index`).

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
ExecStart=/srv/logcompleta/code/venv/bin/gunicorn --workers 3 --bind 127.0.0.1:5000 web:app

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
0 9 * * 1-5 cd /srv/logcompleta/code && /srv/logcompleta/code/venv/bin/python -m app.finance >> /var/log/logcompleta_indices.log 2>&1

# Após as 14h
10 14 * * 1-5 cd /srv/logcompleta/code && /srv/logcompleta/code/venv/bin/python -m app.finance >> /var/log/logcompleta_indices.log 2>&1
```

Validação pós-configuração:
1. Rodar uma execução manual do comando.
2. Confirmar atualização de `app/indices.json`.
3. Abrir `/` e conferir ticker com Petróleo, BDI, FBX e Dólar preenchidos.