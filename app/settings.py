import os
from dataclasses import dataclass
from typing import Literal

from app import env_loader
from app.infra import resolve_sqlite_path


AppEnv = Literal["dev", "homolog", "prod"]


def _detect_app_env() -> AppEnv:
    """
    Determina APP_ENV em um único ponto.

    Em ambientes Render (RENDER=true), APP_ENV é obrigatório.
    Fora disso, default para dev quando ausente.
    """
    raw = (os.getenv("APP_ENV") or "").strip().lower()
    is_render = (os.getenv("RENDER") or "").strip().lower() == "true"

    if not raw:
        if is_render:
            raise RuntimeError(
                "APP_ENV obrigatório em ambiente gerenciado (ex.: Render). "
                "Configure dev|homolog|prod nas variáveis de ambiente."
            )
        raw = "dev"

    if raw not in ("dev", "homolog", "prod"):
        raise RuntimeError("APP_ENV inválido. Valores aceitos: dev|homolog|prod")

    return raw  # type: ignore[return-value]


@dataclass(frozen=True)
class Settings:
    app_env: AppEnv
    log_level: str
    secret_key: str
    debug: bool
    data_dir: str
    indices_file_path: str
    sqlalchemy_database_uri: str
    session_type: str
    session_cookie_secure: bool
    session_cookie_samesite: str
    session_cookie_httponly: bool
    session_lifetime_seconds: int
    mail_server: str
    mail_port: int
    mail_use_tls: bool
    mail_username: str | None
    mail_password: str | None
    mail_default_sender: str | None
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    oauth_insecure_transport: bool
    cron_secret: str
    ops_token: str
    # Limites da home (notícias e artigos) — alteração refatoração segura
    noticias_limite: int
    artigos_limite: int
    # Janela de memória do chat Júlia (mensagens anteriores enviadas ao LLM)
    julia_chat_max_history: int


def _build_settings() -> Settings:
    # 1) Determina e fixa APP_ENV
    app_env = _detect_app_env()
    os.environ["APP_ENV"] = app_env
    print("[VALIDAÇÃO] APP_DATA_DIR:", os.environ.get("APP_DATA_DIR"))

    # 2) Carrega .env.{APP_ENV} uma vez e valida runtime
    env_loader.load_app_env()
    env_loader.validate_runtime_env()

    # 3) Diretórios de dados e índices
    data_dir = env_loader.resolve_data_dir()
    indices_file_path = env_loader.resolve_indices_file_path()

    # 4) Logging
    log_level = (os.getenv("LOG_LEVEL", "INFO") or "INFO").upper()

    # 5) Segurança e banco principal
    secret_key = os.getenv("SECRET_KEY", "chave_insegura_padrao_dev")
    db_uri_auth_raw = (os.getenv("DB_URI_AUTH") or "").strip()
    if app_env in ("homolog", "prod"):
        # SQLite é proibido fora de `dev`; falhar cedo e de forma rastreável.
        db_uri_auth_l = db_uri_auth_raw.lower()
        if (not db_uri_auth_raw) or db_uri_auth_l.startswith("sqlite://") or not db_uri_auth_l.startswith("postgres"):
            raise RuntimeError(
                "DB_URI_AUTH ausente/vazio ou apontando para SQLite em homolog/prod. "
                "Configure DB_URI_AUTH para PostgreSQL (mono-banco)."
            )
    else:
        # Fallback SQLite permitido apenas em dev.
        if not db_uri_auth_raw:
            db_uri_auth_raw = f"sqlite:///{os.path.join(data_dir, 'auth.db')}"

    db_uri_auth = db_uri_auth_raw
    sqlalchemy_database_uri = resolve_sqlite_path(db_uri_auth, env_loader.get_app_dir())

    # 7) Sessão
    session_type = "filesystem"
    session_lifetime_seconds = 24 * 3600
    session_cookie_secure = False
    session_cookie_httponly = True
    session_cookie_samesite = "Lax"

    # 8) E-mail
    mail_server = os.getenv("MAIL_SERVER", "smtp.gmail.com")
    mail_port = int(os.getenv("MAIL_PORT", "587"))
    mail_use_tls = (os.getenv("MAIL_USE_TLS", "True") or "True").lower() in ("true", "1", "t")
    mail_username = os.getenv("MAIL_USERNAME")
    mail_password = os.getenv("MAIL_PASSWORD")
    mail_default_sender = os.getenv("MAIL_DEFAULT_SENDER", mail_username)

    # 9) OAuth Google
    google_client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "") or ""
    google_client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "") or ""
    google_redirect_uri = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "http://127.0.0.1:5000/login/google/callback")
    oauth_insecure_transport = (os.getenv("OAUTHLIB_INSECURE_TRANSPORT", "").strip().lower() in ("1", "true", "t"))

    # 10) Tokens operacionais
    cron_secret = (os.getenv("CRON_SECRET") or "").strip()
    ops_token = (os.getenv("OPS_TOKEN") or "").strip()

    # Limites de exibição na home (parametrizados; sem valores hardcoded)
    try:
        noticias_limite = int(os.getenv("NOTICIAS_LIMITE", "5").strip() or "5")
    except (ValueError, TypeError):
        noticias_limite = 5
    try:
        artigos_limite = int(os.getenv("ARTIGOS_LIMITE", "5").strip() or "5")
    except (ValueError, TypeError):
        artigos_limite = 5
    try:
        julia_chat_max_history = int(os.getenv("JULIA_CHAT_MAX_HISTORY", "10").strip() or "10")
    except (ValueError, TypeError):
        julia_chat_max_history = 10

    # 11) Debug: forçamos False em homolog/prod por segurança
    debug = (os.getenv("FLASK_DEBUG", "False") or "False").lower() in ("true", "1", "t")
    if app_env in ("homolog", "prod"):
        debug = False

    return Settings(
        app_env=app_env,
        log_level=log_level,
        secret_key=secret_key,
        debug=debug,
        data_dir=data_dir,
        indices_file_path=indices_file_path,
        sqlalchemy_database_uri=sqlalchemy_database_uri,
        session_type=session_type,
        session_cookie_secure=session_cookie_secure,
        session_cookie_samesite=session_cookie_samesite,
        session_cookie_httponly=session_cookie_httponly,
        session_lifetime_seconds=session_lifetime_seconds,
        mail_server=mail_server,
        mail_port=mail_port,
        mail_use_tls=mail_use_tls,
        mail_username=mail_username,
        mail_password=mail_password,
        mail_default_sender=mail_default_sender,
        google_client_id=google_client_id,
        google_client_secret=google_client_secret,
        google_redirect_uri=google_redirect_uri,
        oauth_insecure_transport=oauth_insecure_transport,
        cron_secret=cron_secret,
        ops_token=ops_token,
        noticias_limite=noticias_limite,
        artigos_limite=artigos_limite,
        julia_chat_max_history=julia_chat_max_history,
    )


settings: Settings = _build_settings()

