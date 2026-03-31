"""
Carregamento robusto de .env com caminho absoluto baseado no diretório app.
Evita inconsistência por CWD ao rodar scripts de qualquer pasta.
"""
import os
from pathlib import Path


def _can_use_dir(path_str: str) -> bool:
    """Retorna True quando o diretório existe/pode ser criado e é gravável."""
    try:
        path = Path(path_str)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        probe.unlink(missing_ok=True)
        return True
    except Exception:
        return False

def get_app_dir() -> str:
    """Retorna o diretório absoluto da pasta app."""
    return str(Path(__file__).resolve().parent)

def resolve_postgresql_sqlalchemy_uri() -> str:
    """
    Lê DATABASE_URL (após load_app_env), valida parse SQLAlchemy e exige URI PostgreSQL.
    """
    from sqlalchemy.engine.url import make_url

    raw = (os.getenv("DATABASE_URL") or "").strip()
    if not raw:
        raise RuntimeError(
            "DATABASE_URL ausente. Defina em app/.env.{APP_ENV} ou nas variáveis de ambiente."
        )
    low = raw.lower()
    if low.startswith("sqlite://") or not low.startswith("postgres"):
        raise RuntimeError(
            "DATABASE_URL inválida: é obrigatória uma URI PostgreSQL. SQLite e outros SGBDs não são suportados."
        )
    make_url(raw)
    return raw


def mask_database_url_for_log(url: str) -> str:
    """
    Mascara usuário/senha em uma URI de banco para logs (nunca use para conexão).
    """
    from urllib.parse import urlparse, urlunparse

    try:
        p = urlparse(url)
        if p.username is None and p.password is None:
            return url
        netloc_host = p.hostname or ""
        port = p.port
        host_part = f"{netloc_host}:{port}" if port else netloc_host
        user = (p.username or "").strip()
        if user:
            safe_netloc = f"{user}:***@{host_part}"
        else:
            safe_netloc = f"***@{host_part}"
        return urlunparse((p.scheme, safe_netloc, p.path, "", p.query, ""))
    except Exception:
        return "<uri indisponível para log seguro>"


def log_database_boot_diagnostics(uri: str, logger) -> None:
    """
    Valida a URI com SQLAlchemy make_url sem abrir conexão.
    Uma linha de log com URI mascarada e host/porta/database; falha de parse com traceback.
    """
    from sqlalchemy.engine.url import make_url

    if not isinstance(uri, str):
        logger.error("SQLALCHEMY_DATABASE_URI deve ser str; obtido: %s", type(uri).__name__)
        return
    try:
        u = make_url(uri)
    except Exception:
        logger.exception("URI do banco: parse SQLAlchemy (make_url) falhou; conexão não foi tentada")
        return
    masked = mask_database_url_for_log(uri)
    logger.info(
        "Banco configurado (URI mascarada): %s | driver=%s host=%s port=%s database=%s",
        masked,
        u.drivername,
        u.host,
        u.port,
        u.database,
    )


def load_app_env() -> bool:
    """
    Carrega somente app/.env.{APP_ENV} (diretório do pacote app).
    APP_ENV: dev | homolog | prod. Sem fallback para app/.env.
    Retorna True se o arquivo foi carregado com sucesso.
    """
    from dotenv import load_dotenv

    # No boot web/worker, `app/settings` já definiu APP_ENV antes de chamar isto.
    # O default "dev" cobre chamadas diretas (ex.: scripts) sem passar por settings.
    env_name = os.getenv("APP_ENV", "dev").strip().lower() or "dev"
    path = Path(__file__).resolve().parent / f".env.{env_name}"
    return load_dotenv(str(path), override=False, encoding="utf-8-sig")



def resolve_data_dir() -> str:
    """
    Resolve diretório de dados persistentes com prioridade:
    1) APP_DATA_DIR explícito
    2) RENDER_DISK_PATH
    3) /var/data (Render)
    4) fallback local em app/
    """
    app_env = (os.getenv("APP_ENV") or "dev").strip().lower() or "dev"
    is_render = (os.getenv("RENDER") or "").strip().lower() == "true"

    explicit_data_dir = (os.getenv("APP_DATA_DIR") or "").strip()
    if explicit_data_dir:
        explicit_data_dir = str(Path(explicit_data_dir).expanduser())
        if _can_use_dir(explicit_data_dir):
            return explicit_data_dir
        print(f"[WARN] APP_DATA_DIR inválido/inacessível: {explicit_data_dir}. Aplicando fallback.")

    render_disk = (os.getenv("RENDER_DISK_PATH") or "").strip()
    if render_disk:
        if _can_use_dir(render_disk):
            return str(Path(render_disk))
        print(f"[WARN] RENDER_DISK_PATH inválido/inacessível: {render_disk}. Aplicando fallback.")

    if is_render:
        render_default = "/var/data"
        if _can_use_dir(render_default):
            return render_default
        msg = "[ERROR] /var/data indisponível em ambiente Render."
        if app_env in ("homolog", "prod"):
            # Em homolog/prod, não permitimos cair silenciosamente para diretório efêmero.
            raise RuntimeError(
                msg
                + " Configure o disco persistente (ex.: mount em /var/data) "
                "e/ou APP_DATA_DIR/RENDER_DISK_PATH antes de subir a aplicação."
            )
        print(msg + " Usando fallback local em app/ apenas para desenvolvimento.")

    # Fallback final: apenas aceitável em desenvolvimento local.
    app_dir = get_app_dir()
    if app_env in ("homolog", "prod"):
        raise RuntimeError(
            "[ERROR] Diretório de dados não pôde ser resolvido para um volume persistente "
            "em homolog/prod. Configure APP_DATA_DIR ou RENDER_DISK_PATH apontando para "
            "o disco persistente (ex.: /var/data)."
        )
    print(f"[WARN] DATA_DIR caindo para diretório local do app (dev only): {app_dir}")
    return app_dir


def resolve_indices_file_path() -> str:
    """
    Resolve o caminho do arquivo de índices com prioridade:
    1) INDICES_FILE_PATH explícito
    2) RENDER_DISK_PATH/indices.json (persistente no Render)
    3) fallback local em app/indices.json (apenas para dev)
    """
    explicit_path = (os.getenv("INDICES_FILE_PATH") or "").strip()
    if explicit_path:
        return str(Path(explicit_path).expanduser())

    return str(Path(resolve_data_dir()) / "indices.json")


def validate_runtime_env() -> None:
    """
    Evita boot inseguro em homolog/prod com fallbacks locais efêmeros.
    """
    app_env = (os.getenv("APP_ENV") or "dev").strip().lower() or "dev"
    if app_env not in ("dev", "homolog", "prod"):
        raise RuntimeError("APP_ENV inválido. Valores aceitos: dev|homolog|prod")

    if app_env not in ("homolog", "prod"):
        return

    indices_path = resolve_indices_file_path()
    app_dir = Path(get_app_dir()).resolve()
    try:
        indices_resolved = Path(indices_path).resolve()
    except Exception as exc:
        # Em homolog/prod, falha de resolução de índices deve ser fatal para evitar
        # boot "saudável" sem persistência garantida.
        raise RuntimeError(
            "[ERROR] INDICES_FILE_PATH inválido em homolog/prod "
            f"({indices_path}): {exc}. Configure APP_DATA_DIR/RENDER_DISK_PATH/"
            "INDICES_FILE_PATH apontando para volume persistente antes de subir a aplicação."
        ) from exc

    # Em homolog/prod, não aceitamos índices apontando para pasta da release (efêmera).
    if app_dir in indices_resolved.parents or indices_resolved == app_dir / "indices.json":
        raise RuntimeError(
            "[ERROR] INDICES_FILE_PATH aponta para pasta da aplicação em homolog/prod. "
            "Configure APP_DATA_DIR/RENDER_DISK_PATH/INDICES_FILE_PATH para um diretório "
            "montado em disco persistente (ex.: /var/data/indices.json)."
        )
