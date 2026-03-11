"""
Carregamento robusto de .env com caminho absoluto baseado no diretório app.
Evita inconsistência por CWD ao rodar scripts de qualquer pasta.
"""
import os
from pathlib import Path

def get_app_dir() -> str:
    """Retorna o diretório absoluto da pasta app."""
    return str(Path(__file__).resolve().parent)

def load_app_env() -> bool:
    """
    Carrega .env.{APP_ENV} do diretório app.
    APP_ENV: dev | homolog | prod.
    Retorna True se o arquivo foi carregado.
    """
    from dotenv import load_dotenv
    env_name = os.getenv("APP_ENV", "dev").strip().lower() or "dev"
    app_dir = get_app_dir()
    dotenv_path = os.path.join(app_dir, f".env.{env_name}")
    return load_dotenv(dotenv_path)


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

    render_disk = (os.getenv("RENDER_DISK_PATH") or "").strip()
    if render_disk:
        return str(Path(render_disk) / "indices.json")

    return str(Path(get_app_dir()) / "indices.json")


def validate_runtime_env() -> None:
    """
    Evita boot inseguro em homolog/prod com fallbacks locais efêmeros.
    """
    app_env = (os.getenv("APP_ENV") or "dev").strip().lower() or "dev"
    if app_env not in ("homolog", "prod"):
        return

    required_db_vars = [
        "DB_URI_AUTH",
        "DB_URI_LOCALIDADES",
        "DB_URI_HISTORICO",
        "DB_URI_LEADS",
        "DB_URI_NOTICIAS",
        "DB_URI_GERENCIAL",
    ]
    missing_db = [name for name in required_db_vars if not (os.getenv(name) or "").strip()]
    if missing_db:
        raise RuntimeError(
            "Ambiente inseguro: faltam variáveis DB_URI_* obrigatórias em homolog/prod: "
            + ", ".join(missing_db)
        )

    indices_path = resolve_indices_file_path()
    app_dir = Path(get_app_dir()).resolve()
    try:
        indices_resolved = Path(indices_path).resolve()
    except Exception:
        raise RuntimeError("INDICES_FILE_PATH inválido em homolog/prod.")

    # Em homolog/prod, não aceitamos índices dentro da pasta da release.
    if app_dir in indices_resolved.parents or indices_resolved == app_dir / "indices.json":
        raise RuntimeError(
            "Ambiente inseguro: configure INDICES_FILE_PATH (ou RENDER_DISK_PATH) "
            "para persistência fora da pasta app em homolog/prod."
        )
