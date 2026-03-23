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

# Chaves em ConfigRegras para freemium (chat Júlia)
CHAVE_JULIA_CHAT_MAX_HISTORY = "julia_chat_max_history"
CHAVE_FREEMIUM_CONSULTAS_DIA = "freemium_consultas_dia"
CHAVE_FREEMIUM_TRIAL_DIAS = "freemium_trial_dias"

logger = logging.getLogger(__name__)

# --- Schema e bootstrap (estado por processo) ---
_schema_initialized = False
_schema_lock = threading.Lock()

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

# Coluna adicionada para Termo de Aceite (User)
USER_EXTRA_COLUMNS_TERMS = [
    ("accepted_terms_at", "DATETIME"),
]

# Colunas Freemium (User): contador chat e trial
USER_EXTRA_COLUMNS_FREEMIUM = [
    ("chat_consultas_hoje", "INTEGER DEFAULT 0"),
    ("chat_data_ultima_consulta", "DATETIME"),
    ("trial_start_date", "DATETIME"),
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
        engine = db_instance.get_engine()
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
        engine = db_instance.get_engine()
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


def _ensure_user_terms_column(db_instance):
    """Adiciona coluna accepted_terms_at em user se não existir (retrocompatível)."""
    from sqlalchemy import text
    try:
        engine = db_instance.get_engine()
    except Exception:
        return
    for col_name, col_type in USER_EXTRA_COLUMNS_TERMS:
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE user ADD COLUMN " + col_name + " " + col_type
                ))
                conn.commit()
            logger.info("Coluna user.%s adicionada.", col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                pass
            else:
                raise


def _ensure_user_freemium_columns(db_instance):
    """Adiciona colunas freemium em user se não existirem (chat_consultas_hoje, trial_start_date, etc.)."""
    from sqlalchemy import text
    try:
        engine = db_instance.get_engine()
    except Exception:
        return
    for col_name, col_type in USER_EXTRA_COLUMNS_FREEMIUM:
        try:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE user ADD COLUMN " + col_name + " " + col_type
                ))
                conn.commit()
            logger.info("Coluna user.%s (freemium) adicionada.", col_name)
        except Exception as e:
            if "duplicate column name" in str(e).lower() or "already exists" in str(e).lower():
                pass
            else:
                raise


def _ensure_pautas_columns(db_instance):
    """Adiciona colunas Fase 3 em pautas se não existirem (retrocompatível)."""
    from sqlalchemy import text
    try:
        engine = db_instance.get_engine()
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


def ensure_database_schema(db_instance):
    """
    Cria tabelas no engine padrão (mono-banco; uma vez por processo).
    db_instance: instância do SQLAlchemy (app.extensions.db).
    """
    global _schema_initialized
    if _schema_initialized:
        return
    with _schema_lock:
        if _schema_initialized:
            return
        try:
            db_instance.create_all()
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
            try:
                _ensure_user_terms_column(db_instance)
            except Exception as col_err:
                logger.warning("Coluna user accepted_terms_at: %s", col_err)
            try:
                _ensure_user_freemium_columns(db_instance)
            except Exception as col_err:
                logger.warning("Colunas user freemium: %s", col_err)
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


# --- Vínculo geográfico (Roberto Intelligence / BI) ---
def get_id_localidade_por_chave(chave_cidade_uf: str) -> int | None:
    """
    Consulta base_localidades no banco padrão e retorna o id_cidade
    correspondente ao par cidade-uf (chave no formato 'cidade-uf', minúsculo).

    Use esta função para obter IDs de localidade em qualquer módulo que precise
    de vínculo geográfico padronizado (upload de fretes, BI, etc.).
    Deve ser chamada dentro do contexto da aplicação Flask.

    :param chave_cidade_uf: string no formato "cidade-uf" (ex: "são paulo-sp")
    :return: id_cidade (int) ou None se não encontrado
    """
    from sqlalchemy import text
    if not chave_cidade_uf or not isinstance(chave_cidade_uf, str):
        return None
    chave = chave_cidade_uf.strip().lower()
    if not chave:
        return None
    try:
        engine = db.get_engine()
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id_cidade FROM base_localidades WHERE LOWER(TRIM(chave_busca)) = :c"),
                {"c": chave},
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else None
    except Exception as e:
        logger.debug("get_id_localidade_por_chave(%r): %s", chave_cidade_uf, e)
        return None


def get_julia_chat_max_history():
    """
    Retorna o limite de mensagens de histórico do chat Júlia (freemium).
    Prioridade: valor persistido em ConfigRegras; fallback: settings (env).
    Valor sempre entre 1 e 100 (evita inválidos e excede capacidade do modelo).
    """
    from app.models import ConfigRegras
    from app.settings import settings
    try:
        r = ConfigRegras.query.filter_by(chave=CHAVE_JULIA_CHAT_MAX_HISTORY).first()
        if r is not None and r.valor_inteiro is not None:
            return max(1, min(100, int(r.valor_inteiro)))
    except Exception:
        pass
    return max(1, getattr(settings, "julia_chat_max_history", 10))


def get_freemium_consultas_dia():
    """Limite diário de interações no chat (plano grátis). ConfigRegras; padrão 5."""
    from app.models import ConfigRegras
    try:
        r = ConfigRegras.query.filter_by(chave=CHAVE_FREEMIUM_CONSULTAS_DIA).first()
        if r is not None and r.valor_inteiro is not None:
            return max(1, int(r.valor_inteiro))
    except Exception:
        pass
    return 5


def get_freemium_trial_dias():
    """Dias de trial (ex.: 999999999 = ilimitado). ConfigRegras; padrão 7."""
    from app.models import ConfigRegras
    try:
        r = ConfigRegras.query.filter_by(chave=CHAVE_FREEMIUM_TRIAL_DIAS).first()
        if r is not None and r.valor_inteiro is not None:
            return max(0, int(r.valor_inteiro))
    except Exception:
        pass
    return 7


def get_chat_limits_for_user(user):
    """
    Retorna dict para o frontend e validação: limite_dia, usadas_hoje, trial_dias_restantes,
    pode_usar_chat, in_trial. user pode ser None (anônimo: sem contador, pode_usar_chat True).
    """
    from datetime import date, datetime, timezone
    from app.models import User as UserModel

    if user is None or not getattr(user, "is_authenticated", False):
        return {
            "limite_dia": None,
            "usadas_hoje": 0,
            "trial_dias_restantes": None,
            "pode_usar_chat": True,
            "in_trial": False,
        }
    limite_dia = get_freemium_consultas_dia()
    trial_dias = get_freemium_trial_dias()
    hoje = date.today()
    ultima = getattr(user, "chat_data_ultima_consulta", None)
    consultas_hoje = getattr(user, "chat_consultas_hoje", 0) or 0
    if ultima is None:
        usadas_hoje = 0
    else:
        ultima_date = ultima.date() if hasattr(ultima, "date") else (ultima if isinstance(ultima, date) else None)
        if ultima_date != hoje:
            usadas_hoje = 0
        else:
            usadas_hoje = consultas_hoje

    trial_start = getattr(user, "trial_start_date", None)
    if trial_start is None:
        trial_dias_restantes = None
        in_trial = False
    else:
        if trial_dias >= 999999999:
            trial_dias_restantes = None
            in_trial = True
        else:
            start_date = trial_start.date() if hasattr(trial_start, "date") else trial_start
            delta = (hoje - start_date).days
            trial_dias_restantes = max(0, trial_dias - delta)
            in_trial = trial_dias_restantes > 0

    if in_trial:
        pode_usar_chat = True
    else:
        pode_usar_chat = usadas_hoje < limite_dia
    restantes = (limite_dia - usadas_hoje) if not in_trial else None
    return {
        "limite_dia": limite_dia,
        "usadas_hoje": usadas_hoje,
        "restantes_hoje": restantes if restantes is not None else None,
        "trial_dias_restantes": trial_dias_restantes,
        "pode_usar_chat": pode_usar_chat,
        "in_trial": in_trial,
    }


def increment_user_chat_usage(user):
    """Incrementa contador diário do chat; reseta se for outro dia. Persiste no User."""
    from datetime import date, datetime, timezone
    from app.models import User as UserModel

    if user is None or not getattr(user, "id", None):
        return
    hoje = date.today()
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
    ultima = getattr(user, "chat_data_ultima_consulta", None)
    consultas_hoje = getattr(user, "chat_consultas_hoje", 0) or 0
    if ultima is None:
        user.chat_consultas_hoje = 1
        user.chat_data_ultima_consulta = now_naive
    else:
        ultima_date = ultima.date() if hasattr(ultima, "date") else None
        if ultima_date != hoje:
            user.chat_consultas_hoje = 1
            user.chat_data_ultima_consulta = now_naive
        else:
            user.chat_consultas_hoje = consultas_hoje + 1
            user.chat_data_ultima_consulta = now_naive
    try:
        db.session.add(user)
        db.session.commit()
    except Exception as e:
        logger.exception("Falha ao incrementar chat_consultas_hoje: %s", e)
        db.session.rollback()


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


# --- Executor para tarefas admin em background (Cleiton, artigo manual) ---
_admin_executor = None
_admin_executor_lock = threading.Lock()


def get_admin_executor():
    """Retorna ThreadPoolExecutor singleton para execução em background do painel admin (max 1 worker)."""
    global _admin_executor
    if _admin_executor is None:
        with _admin_executor_lock:
            if _admin_executor is None:
                from concurrent.futures import ThreadPoolExecutor
                _admin_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cleiton-admin")
    return _admin_executor
