# exit on error
set -o errexit

pip install -r requirements.txt

# Exige APP_ENV explícito para evitar subir homolog como prod por engano.
if [ -z "${APP_ENV}" ]; then
	echo "ERRO: APP_ENV não definido. Use APP_ENV=homolog ou APP_ENV=prod no ambiente do serviço."
	exit 1
fi

case "${APP_ENV}" in
	dev|homolog|prod) ;;
	*)
		echo "ERRO: APP_ENV inválido: ${APP_ENV}. Valores aceitos: dev|homolog|prod"
		exit 1
		;;
esac

gunicorn --config gunicorn_config.py app.web:app
