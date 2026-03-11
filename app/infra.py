"""
Infraestrutura: configuração de banco, bootstrap de admin, segurança (decorators e token ops).
web.py usa estas funções; não contém lógica de negócio de domínio.
"""
import os
import shutil
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

OPTIONAL_BINDS = ['localidades', 'historico', 'leads', 'noticias', 'gerencial']

# Colunas adicionadas na Etapa 2 (NoticiaPortal); migração suave para bases existentes
NOTICIAS_PORTAL_EXTRA_COLUMNS = [
    ("cta", "TEXT"),
    ("objetivo_lead", "VARCHAR(100)"),
    ("status_qualidade", "VARCHAR(30)"),
    ("origem_pauta", "VARCHAR(50)"),
]

# Colunas adicionadas na Fase 4 (NoticiaPortal - Designer/Publisher)
NOTICIAS_PORTAL_EXTRA_COLUMNS_FASE4 = [
    ("url_imagem_master", "VARCHAR(500)"),
    ("assets_canais_json", "TEXT"),
    ("status_publicacao", "VARCHAR(30)"),
    ("publicado_em", "DATETIME"),
]

# Colunas adicionadas na Fase 3 (Pauta - Scout/Verificador)
PAUTAS_EXTRA_COLUMNS = [
    ("status_verificacao", "VARCHAR(30)"),
    ("score_confiabilidade", "REAL"),
    ("motivo_verificacao", "TEXT"),
    ("fonte_tipo", "VARCHAR(30)"),
    ("hash_conteudo", "VARCHAR(64)"),
    ("coletado_em", "DATETIME"),
    ("verificado_em", "DATETIME"),
    ("arquivada", "BOOLEAN DEFAULT 0"),
]


def _ensure_noticias_portal_columns(db_instance):
    """Adiciona colunas Etapa 2 em noticias_portal se não existirem (retrocompatível)."""
    from sqlalchemy import text
    try:
        engine = db_instance.engines["noticias"]
    except Exception:
        return
    for col_name, col_type in NOTICIAS_PORTAL_EXTRA_COLUMNS:
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE noticias_portal ADD COLUMN " + col_name + " " + col_type
                ))
                conn.commit()
            logger.info("Coluna noticias_portal.%s adicionada.", col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                pass
            else:
                raise


def _ensure_noticias_portal_columns_fase4(db_instance):
    """Adiciona colunas Fase 4 em noticias_portal (Designer/Publisher)."""
    from sqlalchemy import text
    try:
        engine = db_instance.engines["noticias"]
    except Exception:
        return
    for col_name, col_type in NOTICIAS_PORTAL_EXTRA_COLUMNS_FASE4:
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE noticias_portal ADD COLUMN " + col_name + " " + col_type
                ))
                conn.commit()
            logger.info("Coluna noticias_portal.%s adicionada (Fase 4).", col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                pass
            else:
                raise


def _ensure_pautas_columns(db_instance):
    """Adiciona colunas Fase 3 em pautas se não existirem (retrocompatível)."""
    from sqlalchemy import text
    try:
        engine = db_instance.engines["noticias"]
    except Exception:
        return
    for col_name, col_type in PAUTAS_EXTRA_COLUMNS:
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE pautas ADD COLUMN " + col_name + " " + col_type
                ))
                conn.commit()
            logger.info("Coluna pautas.%s adicionada.", col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                pass
            else:
                raise


def _warn_non_sqlite_migration_limits(db_instance):
    """Alerta operacional: migração suave foi validada principalmente em SQLite."""
    try:
        eng_noticias = db_instance.engines["noticias"]
        eng_gerencial = db_instance.engines["gerencial"]
        dialects = {eng_noticias.dialect.name, eng_gerencial.dialect.name}
        if any(d != "sqlite" for d in dialects):
            logger.warning(
                "Ambiente com SGBD não SQLite detectado (%s). "
                "Revise migrações de colunas/tipos antes de promover para produção.",
                ",".join(sorted(dialects)),
            )
    except Exception:
        # Aviso apenas informativo; não deve bloquear bootstrap.
        pass


def resolve_sqlite_path(uri: str, base_dir: str) -> str:
    """
    Converte URIs relativas de SQLite em absolutas e garante que o diretório exista.

    Em Render homolog/prod, URIs relativas são redirecionadas para diretório persistente
    quando disponível, evitando perda de dados entre deploys.
    """

    def _prefer_persistent_sqlite_dir() -> str | None:
        app_env = (os.getenv("APP_ENV", "dev") or "dev").strip().lower()
        render_service = (os.getenv("RENDER_SERVICE_ID") or "").strip()
        if app_env not in ("homolog", "prod") or not render_service:
            return None

        candidates = [
            (os.getenv("PERSISTENT_DATA_DIR") or "").strip(),
            (os.getenv("RENDER_DISK_MOUNT_PATH") or "").strip(),
            "/var/data",
        ]
        for c in candidates:
            if c and os.path.isdir(c):
                return c
        return None

    if uri and uri.startswith('sqlite:///'):
        path_part = uri[len('sqlite:///'):]
        if not os.path.isabs(path_part):
            preferred_dir = _prefer_persistent_sqlite_dir()
            if preferred_dir:
                absolute_path = os.path.join(preferred_dir, os.path.basename(path_part))
                # Migração suave: se existia arquivo no caminho antigo do app e ainda não existe no persistente, copia.
                old_path = os.path.join(base_dir, path_part)
                if os.path.exists(old_path) and not os.path.exists(absolute_path):
                    os.makedirs(os.path.dirname(absolute_path), exist_ok=True)
                    try:
                        shutil.copy2(old_path, absolute_path)
                        logger.warning(
                            "SQLite movido para diretório persistente (%s -> %s).",
                            old_path,
                            absolute_path,
                        )
                    except Exception as copy_err:
                        logger.warning(
                            "Falha ao copiar SQLite para diretório persistente (%s -> %s): %s",
                            old_path,
                            absolute_path,
                            copy_err,
                        )
            else:
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
            try:
                _ensure_noticias_portal_columns(db_instance)
            except Exception as col_err:
                logger.warning("Colunas adicionais noticias_portal: %s", col_err)
            try:
                _ensure_noticias_portal_columns_fase4(db_instance)
            except Exception as col_err:
                logger.warning("Colunas adicionais noticias_portal Fase 4: %s", col_err)
            try:
                _ensure_pautas_columns(db_instance)
            except Exception as col_err:
                logger.warning("Colunas adicionais pautas: %s", col_err)
            _warn_non_sqlite_migration_limits(db_instance)
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
