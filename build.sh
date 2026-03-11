# exit on error
set -o errexit

pip install -r requirements.txt

# Resolve APP_ENV no Render quando não vier explícito.
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
echo "APP_ENV não definido. Inferido como: ${APP_ENV} (branch=${RENDER_GIT_BRANCH})"
fi

case "${APP_ENV}" in
dev|homolog|prod) ;;
*)
echo "ERRO: APP_ENV inválido: ${APP_ENV}. Valores aceitos: dev|homolog|prod"
exit 1
;;
esac

gunicorn --config gunicorn_config.py app.web:app

