"""
Guardrails mínimos de segurança operacional de banco.

Não conecta ao banco. Não corrige URI automaticamente.
Destinado a operações administrativas destrutivas (schema de teste,
downgrade Alembic) — não a queries normais da aplicação.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

ALLOW_DB_DOWNGRADE_ENV = "ALLOW_DB_DOWNGRADE"
ALLOW_DB_DOWNGRADE_DATABASE_ENV = "ALLOW_DB_DOWNGRADE_DATABASE"
SQLITE_MEMORY_URI = "sqlite:///:memory:"
SQLITE_MEMORY_DATABASE = ":memory:"

_TESTING_TRUTHY = frozenset({"1", "true", "t", "yes", "on"})
_DOWNGRADE_OPS = frozenset({"downgrade"})
_LOGGED_MIGRATION_OPS = frozenset({"upgrade", "downgrade"})


class DatabaseIdentity(NamedTuple):
    app_env: str
    host: str
    port: str
    database: str
    driver: str
    is_sqlite_memory: bool
    is_postgres: bool


def _app_env() -> str:
    return (os.getenv("APP_ENV") or "").strip().lower() or "(unset)"


def _is_truthy_env(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in _TESTING_TRUTHY


def is_testing_enabled(testing: bool | None = None) -> bool:
    if testing is True:
        return True
    return _is_truthy_env("TESTING")


def is_downgrade_flag_enabled() -> bool:
    """Somente o literal '1' autoriza a primeira etapa. Sem strip, lowercase ou aliases."""
    return os.getenv(ALLOW_DB_DOWNGRADE_ENV) == "1"


def requested_downgrade_database() -> str | None:
    """Valor bruto da confirmação de database; None se a variável não existir."""
    if ALLOW_DB_DOWNGRADE_DATABASE_ENV not in os.environ:
        return None
    return os.environ.get(ALLOW_DB_DOWNGRADE_DATABASE_ENV)


def parse_database_identity(uri: str | None) -> DatabaseIdentity | None:
    """Extrai identidade sanitizada da URI. Não inclui usuário, senha nem URI completa."""
    raw = (uri or "").strip()
    if not raw:
        return None
    try:
        from sqlalchemy.engine.url import make_url

        url = make_url(raw)
    except Exception:
        return None

    driver = (url.drivername or "").strip().lower()
    database = url.database if url.database is not None else ""
    host = (url.host or "").strip()
    port = str(url.port) if url.port is not None else ""
    is_sqlite = driver.startswith("sqlite")
    is_postgres = driver.startswith("postgres")
    is_memory = bool(is_sqlite and database == SQLITE_MEMORY_DATABASE)
    if is_memory:
        host = ""
        port = ""

    return DatabaseIdentity(
        app_env=_app_env(),
        host=host,
        port=port,
        database=database,
        driver=driver,
        is_sqlite_memory=bool(is_sqlite and is_memory),
        is_postgres=is_postgres,
    )


def is_disposable_test_database(uri: str | None) -> bool:
    """
    Banco reconhecidamente descartável de teste.

    Permitido somente SQLite em memória real (database estrutural == ':memory:').
    PostgreSQL nunca é descartável automaticamente (inclusive sufixo _test).
    URI inválida, dialect desconhecido, SQLite em arquivo: default-deny.
    """
    ident = parse_database_identity(uri)
    if ident is None:
        return False
    return bool(ident.is_sqlite_memory)


def format_db_identity_log(ident: DatabaseIdentity | None, operation: str) -> str:
    if ident is None:
        return (
            "DB destructive operation requested: "
            f"env={_app_env()} host=(unparsed) port=(unparsed) "
            f"database=(unparsed) operation={operation}"
        )
    return (
        "DB destructive operation requested: "
        f"env={ident.app_env} host={ident.host or '(none)'} "
        f"port={ident.port or '(none)'} database={ident.database or '(none)'} "
        f"operation={operation}"
    )


def log_admin_db_operation(uri: str | None, operation: str) -> None:
    """Telemetria mínima: timestamp via logging, sem PII, senha ou URI completa."""
    ident = parse_database_identity(uri)
    logger.warning(format_db_identity_log(ident, operation))


def _refuse(message: str) -> None:
    raise RuntimeError(message)


def assert_safe_for_destructive_test_schema(
    uri: str | None,
    *,
    testing: bool | None = None,
    operation: str,
) -> None:
    """
    TESTING sozinho não basta: exige flag de teste E destino descartável.
    Não tenta corrigir a URI.
    """
    ident = parse_database_identity(uri)
    testing_on = is_testing_enabled(testing)
    disposable = is_disposable_test_database(uri)
    log_admin_db_operation(uri, operation)
    if testing_on and disposable:
        return
    ident_bits = format_db_identity_log(ident, operation)
    _refuse(
        "Operação destrutiva de schema de teste recusada. "
        "TESTING + banco descartável são obrigatórios; a URI não é corrigida automaticamente. "
        f"testing={testing_on} disposable={disposable}. {ident_bits}"
    )


def run_test_schema_operation(
    db,
    uri: str | None,
    *,
    testing: bool | None = None,
    operation: str,
) -> None:
    """Caminho único de create_all/drop_all dos fixtures de teste."""
    assert_safe_for_destructive_test_schema(uri, testing=testing, operation=operation)
    if operation == "create_all":
        db.create_all()
        return
    if operation == "drop_all":
        db.drop_all()
        return
    _refuse(f"Operação de schema de teste desconhecida: {operation}")


def alembic_migration_operation(
    argv: list[str] | None = None,
    cmd_opts: Any = None,
) -> str | None:
    tokens = [str(a).lower() for a in (argv if argv is not None else sys.argv)]
    for token in tokens:
        if token in ("downgrade", "upgrade"):
            return token
    if cmd_opts is not None:
        cmd = getattr(cmd_opts, "cmd", None)
        name = (getattr(cmd, "__name__", "") or "").strip().lower()
        if name in ("downgrade", "upgrade"):
            return name
    return None


def assert_downgrade_allowed(uri: str | None) -> None:
    """
    Downgrade exige as duas confirmações literais:
    ALLOW_DB_DOWNGRADE == '1' e ALLOW_DB_DOWNGRADE_DATABASE == nome exato.
    Sem bypass por SQLite, TESTING ou sufixo _test.
    """
    ident = parse_database_identity(uri)
    ident_bits = format_db_identity_log(ident, "downgrade")
    target = ident.database if ident is not None else ""
    confirmed = requested_downgrade_database()
    if (
        is_downgrade_flag_enabled()
        and ident is not None
        and target != ""
        and confirmed is not None
        and confirmed == target
    ):
        return
    _refuse(
        "Downgrade Alembic recusado. "
        f"Exige {ALLOW_DB_DOWNGRADE_ENV}=1 e "
        f"{ALLOW_DB_DOWNGRADE_DATABASE_ENV}=<nome-exato-do-database>. "
        f"{ident_bits}"
    )


def guard_alembic_env(
    database_url: str | None,
    *,
    argv: list[str] | None = None,
    cmd_opts: Any = None,
) -> None:
    """
    Barreira de env.py: loga identidade sanitizada em upgrade/downgrade;
    bloqueia downgrade sem autorização. Não bloqueia upgrade.
    Deve ser chamada antes de create_engine/connect.
    """
    operation = alembic_migration_operation(argv=argv, cmd_opts=cmd_opts)
    if operation is None:
        return
    if operation in _LOGGED_MIGRATION_OPS:
        log_admin_db_operation(database_url, operation)
    if operation in _DOWNGRADE_OPS:
        assert_downgrade_allowed(database_url)
