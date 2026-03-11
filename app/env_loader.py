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


def resolve_data_dir() -> str:
    """
    Resolve diretório de dados persistentes com prioridade:
    1) APP_DATA_DIR explícito
    2) RENDER_DISK_PATH
    3) /var/data (Render)
    4) fallback local em app/
    """
    explicit_data_dir = (os.getenv("APP_DATA_DIR") or "").strip()
    if explicit_data_dir:
        return str(Path(explicit_data_dir).expanduser())

    render_disk = (os.getenv("RENDER_DISK_PATH") or "").strip()
    if render_disk:
        return str(Path(render_disk))

    if (os.getenv("RENDER") or "").strip().lower() == "true":
        return "/var/data"

    return get_app_dir()


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
    except Exception:
        raise RuntimeError("INDICES_FILE_PATH inválido em homolog/prod.")

    # Em homolog/prod, alertamos quando índices apontam para pasta da release.
    if app_dir in indices_resolved.parents or indices_resolved == app_dir / "indices.json":
        print(
            "[WARN] INDICES_FILE_PATH aponta para pasta da app em homolog/prod. "
            "Configure APP_DATA_DIR/RENDER_DISK_PATH/INDICES_FILE_PATH para persistência real."
        )
