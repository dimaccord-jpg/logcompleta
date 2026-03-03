# extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

# Criamos as instâncias sem ligá-las a nenhum app ainda (init_app fará isso depois)
db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = 'login' # Mantendo sua configuração original