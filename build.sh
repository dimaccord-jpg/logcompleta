# exit on error
set -o errexit

pip install -r requirements.txt

gunicorn --config gunicorn_config.py app.web:app
