#!/usr/bin/env bash
set -o errexit

# Resolve APP_ENV no Render quando nÃ£o vier explÃ­cito.
if [ -z "${APP_ENV}" ]; then
  case "${RENDER_GIT_BRANCH}" in
    homolog)
      export APP_ENV="homolog"
      ;;
    main|master|producao|prod)
      export APP_ENV="prod"
      ;;
    *)
      export APP_ENV="dev"
      ;;
  esac
  echo "APP_ENV nÃ£o definido. Inferido como: ${APP_ENV} (branch=${RENDER_GIT_BRANCH})"
fi

case "${APP_ENV}" in
  dev|homolog|prod) ;;
  *)
    echo "ERRO: APP_ENV invÃ¡lido: ${APP_ENV}. Valores aceitos: dev|homolog|prod"
    exit 1
    ;;
esac

echo "Aplicando migrations antes do start da aplicaÃ§Ã£o..."
python -m flask --app app.web db upgrade

echo "Subindo servidor web..."
exec gunicorn --config gunicorn_config.py app.web:app
