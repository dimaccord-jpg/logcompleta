# exit on error
set -o errexit

pip install -r requirements.txt

# Garante modo produção no Render (evita depender só do painel de env vars)
export APP_ENV="${APP_ENV:-prod}"

gunicorn --config gunicorn_config.py app.web:app
