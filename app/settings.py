import os
from dataclasses import dataclass
from typing import Literal

from app import env_loader
from app.privacy_marketing import (
    PRIVACY_MARKETING_COOKIE_MAX_AGE_SECONDS,
    PRIVACY_MARKETING_COOKIE_NAME,
)


AppEnv = Literal["dev", "homolog", "prod"]
DEV_SECRET_KEY_FALLBACK = "chave_insegura_padrao_dev"


def _detect_app_env() -> AppEnv:
    """
    Determina APP_ENV em um único ponto.

    Obrigatório em toda execução: não há fallback silencioso para dev.
    """
    raw = (os.getenv("APP_ENV") or "").strip().lower()

    if not raw:
        raise RuntimeError(
            "APP_ENV obrigatório. Defina explicitamente antes do boot um dos valores aceitos: "
            "dev|homolog|prod. Não existe fallback implícito para dev."
        )

    if raw not in ("dev", "homolog", "prod"):
        raise RuntimeError(
            "APP_ENV inválido. Valores aceitos: dev|homolog|prod. "
            "Não existe fallback implícito para dev."
        )

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
    # URL da página comercial de planos (CTA operacional)
    planos_upgrade_url: str
    facebook_pixel_id: str
    google_analytics_measurement_id: str
    openai_ads_pixel_id: str
    openai_ads_debug: bool
    public_base_url: str
    privacy_marketing_cookie_name: str
    privacy_marketing_cookie_max_age_seconds: int
    communication_suppression_hmac_secret: str
    growth_external_event_token_secret: str
    # Meta CAPI (SCRUM-158A). Default OFF; homolog-first com test_event_code.
    meta_capi_enabled: bool
    meta_capi_access_token: str
    meta_graph_api_version: str
    meta_capi_test_event_code: str
    meta_capi_connect_timeout_seconds: float
    meta_capi_read_timeout_seconds: float


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

    # 5) Segurança e banco principal (mono PostgreSQL; contrato canônico: DATABASE_URL)
    secret_key = (os.getenv("SECRET_KEY") or "").strip()
    if not secret_key:
        if app_env == "dev":
            secret_key = DEV_SECRET_KEY_FALLBACK
        else:
            raise RuntimeError(
                "SECRET_KEY obrigatoria em homolog/prod. Defina uma chave forte e unica por ambiente antes do boot."
            )
    elif app_env in ("homolog", "prod") and secret_key == DEV_SECRET_KEY_FALLBACK:
        raise RuntimeError(
            "SECRET_KEY insegura em homolog/prod. O fallback de desenvolvimento e proibido fora de dev."
        )
    sqlalchemy_database_uri = env_loader.resolve_postgresql_sqlalchemy_uri()

    # 7) Sessão
    session_type = "filesystem"
    session_lifetime_seconds = 24 * 3600
    session_cookie_secure = app_env in ("homolog", "prod")
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
    planos_upgrade_url = (os.getenv("PLANOS_UPGRADE_URL") or "").strip()
    facebook_pixel_id = (os.getenv("FACEBOOK_PIXEL_ID") or "").strip()
    google_analytics_measurement_id = (
        os.getenv("GOOGLE_ANALYTICS_MEASUREMENT_ID") or ""
    ).strip()
    openai_ads_pixel_id = (os.getenv("OPENAI_ADS_PIXEL_ID") or "").strip()
    openai_ads_debug = (os.getenv("OPENAI_ADS_DEBUG") or "").strip().lower() in (
        "true",
        "1",
        "t",
    )
    public_base_url = (
        (os.getenv("PUBLIC_BASE_URL") or "https://www.agentefrete.com.br").strip().rstrip("/")
    )
    privacy_marketing_cookie_name = PRIVACY_MARKETING_COOKIE_NAME
    privacy_marketing_cookie_max_age_seconds = PRIVACY_MARKETING_COOKIE_MAX_AGE_SECONDS
    communication_suppression_hmac_secret = (
        os.getenv("COMMUNICATION_SUPPRESSION_HMAC_SECRET") or ""
    ).strip()
    growth_external_event_token_secret = (
        os.getenv("GROWTH_EXTERNAL_EVENT_TOKEN_SECRET") or ""
    ).strip()
    # Meta CAPI server-side (158A). Somente "true" habilita; default false.
    # Sem Google Measurement Protocol / Ads server-side neste lote.
    meta_capi_enabled = (os.getenv("META_CAPI_ENABLED") or "false").strip().lower() == "true"
    meta_capi_access_token = (os.getenv("META_CAPI_ACCESS_TOKEN") or "").strip()
    meta_graph_api_version = (os.getenv("META_GRAPH_API_VERSION") or "").strip()
    meta_capi_test_event_code = (os.getenv("META_CAPI_TEST_EVENT_CODE") or "").strip()
    try:
        meta_capi_connect_timeout_seconds = float(
            (os.getenv("META_CAPI_CONNECT_TIMEOUT_SECONDS") or "0.5").strip() or "0.5"
        )
        if meta_capi_connect_timeout_seconds <= 0:
            meta_capi_connect_timeout_seconds = 0.5
    except (ValueError, TypeError):
        meta_capi_connect_timeout_seconds = 0.5
    try:
        meta_capi_read_timeout_seconds = float(
            (os.getenv("META_CAPI_READ_TIMEOUT_SECONDS") or "1.0").strip() or "1.0"
        )
        if meta_capi_read_timeout_seconds <= 0:
            meta_capi_read_timeout_seconds = 1.0
    except (ValueError, TypeError):
        meta_capi_read_timeout_seconds = 1.0
    print(
        "[VALIDAÇÃO] COMMUNICATION_SUPPRESSION_HMAC_SECRET:",
        "configurado"
        if communication_suppression_hmac_secret
        else "ausente (suppression desabilitada)",
    )
    print(
        "[VALIDAÇÃO] META_CAPI_ENABLED:",
        "true" if meta_capi_enabled else "false (default OFF)",
    )

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
        planos_upgrade_url=planos_upgrade_url,
        facebook_pixel_id=facebook_pixel_id,
        google_analytics_measurement_id=google_analytics_measurement_id,
        openai_ads_pixel_id=openai_ads_pixel_id,
        openai_ads_debug=openai_ads_debug,
        public_base_url=public_base_url,
        privacy_marketing_cookie_name=privacy_marketing_cookie_name,
        privacy_marketing_cookie_max_age_seconds=privacy_marketing_cookie_max_age_seconds,
        communication_suppression_hmac_secret=communication_suppression_hmac_secret,
        growth_external_event_token_secret=growth_external_event_token_secret,
        meta_capi_enabled=meta_capi_enabled,
        meta_capi_access_token=meta_capi_access_token,
        meta_graph_api_version=meta_graph_api_version,
        meta_capi_test_event_code=meta_capi_test_event_code,
        meta_capi_connect_timeout_seconds=meta_capi_connect_timeout_seconds,
        meta_capi_read_timeout_seconds=meta_capi_read_timeout_seconds,
    )


settings: Settings = _build_settings()

