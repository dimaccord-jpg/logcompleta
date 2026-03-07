"""
Infraestrutura: configuração de banco, bootstrap de admin, segurança (decorators e token ops).
web.py usa estas funções; não contém lógica de negócio de domínio.
"""
import os
import logging
import threading
from functools import wraps

from flask import flash, redirect, url_for, request, abort
from flask_login import current_user

from app.extensions import db
from app.models import User

logger = logging.getLogger(__name__)

# --- Schema e bootstrap (estado por processo) ---
_schema_initialized = False
_schema_lock = threading.Lock()

OPTIONAL_BINDS = ['localidades', 'historico', 'leads', 'noticias']


def resolve_sqlite_path(uri: str, base_dir: str) -> str:
    """Converte URIs relativas de SQLite em absolutas e garante que o diretório exista."""
    if uri and uri.startswith('sqlite:///'):
        path_part = uri[len('sqlite:///'):]
        if not os.path.isabs(path_part):
            absolute_path = os.path.join(base_dir, path_part)
            os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
            return 'sqlite:///' + absolute_path.replace('\\', '/')
    return uri


def ensure_database_schema(db_instance):
    """
    Cria tabelas no bind padrão e nos opcionais (uma vez por processo).
    db_instance: instância do SQLAlchemy (app.extensions.db).
    """
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if _schema_initialized:
            return
        try:
            db_instance.create_all(bind_key=[None])
            for optional_bind in OPTIONAL_BINDS:
                try:
                    db_instance.create_all(bind_key=[optional_bind])
                except Exception as bind_error:
                    logger.warning(
                        "Não foi possível inicializar bind '%s': %s",
                        optional_bind, bind_error
                    )
            _schema_initialized = True
            logger.info("Banco inicializado: tabelas verificadas/criadas com sucesso.")
        except Exception as e:
            logger.exception("Falha ao inicializar banco de dados: %s", e)
            raise


def ensure_bootstrap_admin_user(db_instance):
    """
    Promove um usuário existente a admin no startup, quando BOOTSTRAP_ADMIN_EMAIL ou MAIL_USERNAME está definido.
    """
    admin_email = os.getenv('BOOTSTRAP_ADMIN_EMAIL') or os.getenv('MAIL_USERNAME')
    if not admin_email:
        return
    try:
        user = User.query.filter_by(email=admin_email).first()
        if not user:
            logger.info("Bootstrap admin: usuário '%s' ainda não existe.", admin_email)
            return
        if user.is_admin:
            return
        user.is_admin = True
        db_instance.session.commit()
        logger.info("Bootstrap admin: usuário '%s' promovido para admin.", admin_email)
    except Exception as e:
        logger.exception("Falha ao promover usuário admin no bootstrap: %s", e)


def get_user_by_id(user_id):
    """Retorna User por id (para Flask-Login user_loader)."""
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def admin_required(f):
    """Decorator: exige usuário autenticado e is_admin; redireciona para login caso contrário."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not getattr(current_user, 'is_admin', False):
            flash("Acesso restrito apenas para administradores.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def ops_token_required():
    """Protege endpoints operacionais: exige header X-Ops-Token igual a OPS_TOKEN no ambiente. Aborta 403 se inválido."""
    expected = os.getenv('OPS_TOKEN', '').strip()
    provided = request.headers.get('X-Ops-Token', '').strip()
    if not expected or provided != expected:
        abort(403)
