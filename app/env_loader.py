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
