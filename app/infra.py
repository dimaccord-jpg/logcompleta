"""
Infraestrutura: configuração de banco, bootstrap de admin, segurança (decorators e token ops).
web.py usa estas funções; não contém lógica de negócio de domínio.
"""
import os
import sys
import logging
import threading
from functools import wraps

from flask import flash, redirect, url_for, request, abort
from flask_login import current_user

from app.extensions import db
from app.models import User

# Chaves em ConfigRegras para freemium (chat Júlia)
CHAVE_JULIA_CHAT_MAX_HISTORY = "julia_chat_max_history"
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

# Colunas Freemium (User): trial
USER_EXTRA_COLUMNS_FREEMIUM = [
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
    """Adiciona colunas freemium em user se não existirem (trial_start_date)."""
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


def user_is_admin(user) -> bool:
    """True somente se is_admin for explicitamente True (None/outros valores => sem privilégio admin)."""
    return getattr(user, "is_admin", None) is True


def ensure_bootstrap_admin_user(db_instance, *, raise_on_failure: bool = False) -> bool:
    """
    Promove um usuário **já existente** a admin quando BOOTSTRAP_ADMIN_EMAIL está definido.
    Alvo exclusivo por configuração (sem fallback para MAIL_USERNAME). Idempotente.
    Uso: comando `flask bootstrap-admin` ou chamada operacional explícita — nunca a partir de request HTTP público.
    Se raise_on_failure=True, propaga exceção após log (adequado para CLI).
    """
    admin_email = (os.getenv("BOOTSTRAP_ADMIN_EMAIL") or "").strip()
    if not admin_email:
        logger.info(
            "Bootstrap admin: BOOTSTRAP_ADMIN_EMAIL não definido; nenhuma promoção (use ADMIN_EMAILS para OAuth, "
            "ou defina BOOTSTRAP_ADMIN_EMAIL e execute: flask --app app.web bootstrap-admin)."
        )
        return True
    try:
        user = User.query.filter_by(email=admin_email).first()
        if not user:
            logger.info("Bootstrap admin: usuário '%s' ainda não existe.", admin_email)
            return True
        if user_is_admin(user):
            return True
        user.is_admin = True
        db_instance.session.commit()
        logger.info("Bootstrap admin: usuário '%s' promovido para admin.", admin_email)
    except Exception as e:
        logger.exception(
            "Bootstrap admin: falha (tipo=%s, python=%s, msg=%r). "
            "Senha e DATABASE_URL completa não são logadas.",
            type(e).__name__,
            sys.executable,
            str(e),
        )
        if isinstance(e, UnicodeDecodeError):
            logger.error(
                "Bootstrap admin: UnicodeDecodeError costuma indicar DATABASE_URL/.env com encoding incorreto "
                "ou bytes inválidos na URI (ex.: senha salva em Latin-1). Garanta UTF-8 e evite caracteres "
                "fora do esperado na string de conexão."
            )
        if raise_on_failure:
            raise
        return False
    return True


def get_user_by_id(user_id):
    """Retorna User por id (para Flask-Login user_loader)."""
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


# --- Vínculo geográfico (Roberto Intelligence / BI) ---
def _localidade_row_to_dict(row) -> dict:
    """Converte uma linha de SELECT em base_localidades para o dict padrão de localidade."""
    id_cidade, id_uf, cidade_nome, uf_nome, chave_busca = row
    return {
        "id_cidade": int(id_cidade) if id_cidade is not None else None,
        "id_uf": int(id_uf) if id_uf is not None else None,
        "cidade_nome": (cidade_nome or "").strip(),
        "uf_nome": (uf_nome or "").strip(),
        "chave_busca": (chave_busca or "").strip(),
    }


def carregar_localidades_por_chaves(chaves: set[str] | list[str]) -> dict[str, dict]:
    """
    Carrega várias localidades em uma única consulta (WHERE chave_busca IN (...)),
    usando o índice/PK em chave_busca. Entrada deve seguir a mesma normalização
    do upload (strip + lower).

    :param chaves: conjunto ou lista de chaves no formato "cidade-uf"
    :return: mapa chave normalizada -> dict (mesmo formato de get_localidade_completa_por_chave)
    """
    from sqlalchemy import bindparam, text

    norm: set[str] = set()
    for c in chaves:
        if not c or not isinstance(c, str):
            continue
        k = c.strip().lower()
        if k:
            norm.add(k)
    if not norm:
        return {}

    keys_list = list(norm)
    stmt = text(
        """
        SELECT id_cidade, id_uf, cidade_nome, uf_nome, chave_busca
        FROM base_localidades
        WHERE chave_busca IN :keys
        """
    ).bindparams(bindparam("keys", expanding=True))

    try:
        engine = db.get_engine()
        with engine.connect() as conn:
            rows = conn.execute(stmt, {"keys": keys_list}).fetchall()
    except Exception as e:
        logger.debug("carregar_localidades_por_chaves: %s", e)
        return {}

    out: dict[str, dict] = {}
    for row in rows:
        d = _localidade_row_to_dict(row)
        kb = (d.get("chave_busca") or "").strip().lower()
        if kb:
            out[kb] = d
    return out


def get_localidade_completa_por_chave(chave_cidade_uf: str) -> dict | None:
    """
    Consulta base_localidades no banco padrão por chave_busca (igualdade direta).
    O parâmetro deve estar no formato "cidade-uf" com normalização strip + lower,
    alinhada aos dados persistidos em chave_busca.

    :param chave_cidade_uf: string no formato "cidade-uf" (ex: "são paulo-sp")
    :return: dict com id_cidade, id_uf, cidade_nome, uf_nome, chave_busca;
             None se inválido ou não encontrado
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
                text(
                    """
                    SELECT id_cidade, id_uf, cidade_nome, uf_nome, chave_busca
                    FROM base_localidades
                    WHERE chave_busca = :c
                    """
                ),
                {"c": chave},
            ).fetchone()
            if not row:
                return None
            return _localidade_row_to_dict(row)
    except Exception as e:
        logger.debug("get_localidade_completa_por_chave(%r): %s", chave_cidade_uf, e)
        return None


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
    loc = get_localidade_completa_por_chave(chave_cidade_uf)
    if not loc:
        return None
    cid = loc.get("id_cidade")
    return int(cid) if cid is not None else None


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


def admin_required(f):
    """Decorator: exige usuário autenticado e is_admin explícito; redireciona para login caso contrário."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not user_is_admin(current_user):
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
