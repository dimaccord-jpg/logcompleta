import os
from dataclasses import dataclass
from typing import Dict, Literal

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
    sqlalchemy_binds: Dict[str, str]
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


def _build_settings() -> Settings:
    # 1) Determina e fixa APP_ENV
    app_env = _detect_app_env()
    os.environ["APP_ENV"] = app_env

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
    db_uri_auth = os.getenv("DB_URI_AUTH", f"sqlite:///{os.path.join(data_dir, 'auth.db')}")
    sqlalchemy_database_uri = resolve_sqlite_path(db_uri_auth, env_loader.get_app_dir())

    # 6) Binds adicionais
    base_dir = env_loader.get_app_dir()
    db_binds_raw = {
        "localidades": os.getenv("DB_URI_LOCALIDADES", f"sqlite:///{os.path.join(data_dir, 'base_localidades.db')}"),
        "historico": os.getenv("DB_URI_HISTORICO", f"sqlite:///{os.path.join(data_dir, 'historico_frete.db')}"),
        "leads": os.getenv("DB_URI_LEADS", f"sqlite:///{os.path.join(data_dir, 'leads.db')}"),
        "noticias": os.getenv("DB_URI_NOTICIAS", f"sqlite:///{os.path.join(data_dir, 'noticias.db')}"),
        "gerencial": os.getenv("DB_URI_GERENCIAL", f"sqlite:///{os.path.join(data_dir, 'gerencial.db')}"),
    }
    sqlalchemy_binds = {k: resolve_sqlite_path(v, base_dir) for k, v in db_binds_raw.items()}

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
        sqlalchemy_binds=sqlalchemy_binds,
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
    )


settings: Settings = _build_settings()

