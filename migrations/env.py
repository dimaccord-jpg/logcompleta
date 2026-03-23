import logging
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import MetaData, create_engine

# Raiz do repositório (pai de migrations/) para importar o pacote app com qualquer CWD.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)
logger = logging.getLogger("alembic.env")

_database_url_cache: str | None = None


def _resolve_database_url() -> str:
    """
    Contrato do projeto: DATABASE_URL (PostgreSQL), carregada após .env.{APP_ENV} via env_loader.
    Sem Flask e sem current_app.
    """
    global _database_url_cache
    if _database_url_cache is not None:
        return _database_url_cache

    import os

    if not (os.getenv("APP_ENV") or "").strip():
        os.environ["APP_ENV"] = "dev"

    from app import env_loader

    env_loader.load_app_env()

    raw = (os.getenv("DATABASE_URL") or "").strip()
    low = raw.lower()
    if not raw or low.startswith("sqlite://") or not low.startswith("postgres"):
        raise RuntimeError(
            "Alembic exige DATABASE_URL com URI PostgreSQL (banco único). "
            "Defina no ambiente ou em app/.env.{APP_ENV}."
        )

    _database_url_cache = raw
    return raw


# Offline e online: URL no config (%% escapa % para ConfigParser).
config.set_main_option("sqlalchemy.url", _resolve_database_url().replace("%", "%%"))


def get_metadata():
    """
    MetaData alvo = modelos Flask-SQLAlchemy (app.models), mono-banco PostgreSQL.
    Combina metadatas quando o SQLAlchemy expõe vários (ex.: binds legados).
    """
    from app.extensions import db
    import app.models  # noqa: F401 — registra tabelas no metadata

    if hasattr(db, "metadatas") and db.metadatas:
        combined = MetaData()
        for md in db.metadatas.values():
            for table in md.tables.values():
                table.tometadata(combined)
        return combined
    return db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=get_metadata(), literal_binds=True)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode (engine direto a partir de DATABASE_URL)."""

    def process_revision_directives(context, revision, directives):
        opts = getattr(config, "cmd_opts", None)
        if getattr(opts, "autogenerate", False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info("No changes in schema detected.")

    connectable = create_engine(
        _resolve_database_url(),
        pool_pre_ping=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=get_metadata(),
            process_revision_directives=process_revision_directives,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
