"""Guardrails de isolamento de pytest e segurança operacional de banco.

Estes testes não abrem conexão com PostgreSQL (dev/homolog/prod).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.db_operational_safety import (
    ALLOW_DB_DOWNGRADE_DATABASE_ENV,
    ALLOW_DB_DOWNGRADE_ENV,
    SQLITE_MEMORY_URI,
    alembic_migration_operation,
    assert_downgrade_allowed,
    assert_safe_for_destructive_test_schema,
    guard_alembic_env,
    is_disposable_test_database,
    log_admin_db_operation,
    parse_database_identity,
    run_test_schema_operation,
)
from tests.conftest import (
    PYTEST_DISPOSABLE_SQLALCHEMY_URI,
    PYTEST_PROCESS_DATABASE_URL,
    apply_pytest_database_isolation,
)

DEV_URI = "postgresql://ops_user:super-secret@127.0.0.1:5432/logcompleta_dev"
PROD_URI = "postgresql://ops_user:super-secret@db.example.com:5432/logcompleta_prod"
HOMOLOG_URI = "postgresql://ops_user:super-secret@db.example.com:5432/logcompleta_homolog"
PG_TEST_URI = "postgresql://pytest:pytest@127.0.0.1:5432/logcompleta_test"
PG_PROD_TEST_URI = "postgresql://pytest:pytest@127.0.0.1:5432/logcompleta_prod_test"
EMPTY_DB_URI = "postgresql://ops_user:super-secret@127.0.0.1:5432"
AMBIGUOUS_URI = "postgresql://ops_user:super-secret@127.0.0.1:5432/testdb"
SQLITE_FILE_URI = "sqlite:///C:/tmp/real.db"
SQLITE_MEMORY_IN_PATH_URI = "sqlite:///C:/tmp/:memory:.db"
INVALID_URI = "not-a-database-uri"


def _clear_downgrade_auth(monkeypatch) -> None:
    monkeypatch.delenv(ALLOW_DB_DOWNGRADE_ENV, raising=False)
    monkeypatch.delenv(ALLOW_DB_DOWNGRADE_DATABASE_ENV, raising=False)


def _authorize_downgrade(monkeypatch, database: str) -> None:
    monkeypatch.setenv(ALLOW_DB_DOWNGRADE_ENV, "1")
    monkeypatch.setenv(ALLOW_DB_DOWNGRADE_DATABASE_ENV, database)


def test_isolamento_nao_herda_database_url_dev(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DEV_URI)
    monkeypatch.delenv("TESTING", raising=False)
    apply_pytest_database_isolation()

    process_uri = os.environ["DATABASE_URL"]
    assert process_uri != DEV_URI
    assert "logcompleta_dev" not in process_uri
    assert "super-secret" not in process_uri
    assert os.environ["TESTING"] == "1"
    assert process_uri == PYTEST_PROCESS_DATABASE_URL
    assert not is_disposable_test_database(process_uri)
    assert PYTEST_DISPOSABLE_SQLALCHEMY_URI == SQLITE_MEMORY_URI
    assert is_disposable_test_database(PYTEST_DISPOSABLE_SQLALCHEMY_URI)


@pytest.fixture
def inherited_operational_database_url(monkeypatch):
    """Simula processo iniciado com DATABASE_URL operacional ANTES do fixture app."""
    monkeypatch.setenv("DATABASE_URL", DEV_URI)
    return DEV_URI


def test_fixture_usa_sqlite_memory_mesmo_com_env_dev(inherited_operational_database_url, app):
    assert os.environ["DATABASE_URL"] == inherited_operational_database_url
    uri = app.config["SQLALCHEMY_DATABASE_URI"]
    assert uri == SQLITE_MEMORY_URI
    assert uri == PYTEST_DISPOSABLE_SQLALCHEMY_URI
    from app.extensions import db

    with app.app_context():
        bind_url = db.engine.url
        assert str(bind_url.get_backend_name()).startswith("sqlite")
        assert (bind_url.database or "") == ":memory:"
        assert "logcompleta_dev" not in str(bind_url)


@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        (SQLITE_MEMORY_URI, True),
        ("sqlite+pysqlite:///:memory:", True),
        (PG_TEST_URI, False),
        (PG_PROD_TEST_URI, False),
        (DEV_URI, False),
        (PROD_URI, False),
        (HOMOLOG_URI, False),
        (EMPTY_DB_URI, False),
        (AMBIGUOUS_URI, False),
        ("", False),
        (None, False),
        (INVALID_URI, False),
        (SQLITE_FILE_URI, False),
        (SQLITE_MEMORY_IN_PATH_URI, False),
        ("sqlite:////tmp/:memory:.db", False),
        ("postgresql+psycopg2://u:p@localhost:5432/logcompleta_dev", False),
        ("postgresql+psycopg2://u:p@localhost:5432/svc_test", False),
    ],
)
def test_guard_disposable_database(uri, allowed):
    assert is_disposable_test_database(uri) is allowed


def test_sqlite_memory_permitido_com_testing():
    assert_safe_for_destructive_test_schema(
        SQLITE_MEMORY_URI, testing=True, operation="drop_all"
    )


def test_postgres_logcompleta_test_nao_e_descartavel():
    assert is_disposable_test_database(PG_TEST_URI) is False
    with pytest.raises(RuntimeError, match="recusada"):
        assert_safe_for_destructive_test_schema(
            PG_TEST_URI, testing=True, operation="drop_all"
        )


def test_postgres_logcompleta_prod_test_bloqueado_para_drop_all():
    assert is_disposable_test_database(PG_PROD_TEST_URI) is False
    with pytest.raises(RuntimeError, match="recusada"):
        assert_safe_for_destructive_test_schema(
            PG_PROD_TEST_URI, testing=True, operation="create_all"
        )


def test_sqlite_persistente_com_memory_no_path_bloqueado():
    ident = parse_database_identity(SQLITE_MEMORY_IN_PATH_URI)
    assert ident is None or ident.database != ":memory:"
    assert is_disposable_test_database(SQLITE_MEMORY_IN_PATH_URI) is False
    with pytest.raises(RuntimeError, match="recusada"):
        assert_safe_for_destructive_test_schema(
            SQLITE_MEMORY_IN_PATH_URI, testing=True, operation="drop_all"
        )


def test_sqlite_arquivo_real_bloqueado():
    with pytest.raises(RuntimeError, match="recusada"):
        assert_safe_for_destructive_test_schema(
            SQLITE_FILE_URI, testing=True, operation="drop_all"
        )


def test_uri_invalida_bloqueada():
    assert is_disposable_test_database(INVALID_URI) is False
    with pytest.raises(RuntimeError, match="recusada"):
        assert_safe_for_destructive_test_schema(
            INVALID_URI, testing=True, operation="drop_all"
        )


@pytest.mark.parametrize(
    "uri",
    [
        DEV_URI,
        PROD_URI,
        HOMOLOG_URI,
        EMPTY_DB_URI,
        AMBIGUOUS_URI,
        PG_TEST_URI,
        SQLITE_FILE_URI,
        SQLITE_MEMORY_IN_PATH_URI,
        INVALID_URI,
        "",
    ],
)
def test_destinos_operacionais_bloqueados_mesmo_com_testing(uri):
    with pytest.raises(RuntimeError, match="recusada"):
        assert_safe_for_destructive_test_schema(uri, testing=True, operation="drop_all")


def test_testing_sozinho_nao_libera_dev():
    with pytest.raises(RuntimeError, match="recusada"):
        assert_safe_for_destructive_test_schema(DEV_URI, testing=True, operation="drop_all")


def test_sqlite_memory_sem_testing_e_bloqueado(monkeypatch):
    monkeypatch.delenv("TESTING", raising=False)
    with pytest.raises(RuntimeError, match="recusada"):
        assert_safe_for_destructive_test_schema(
            SQLITE_MEMORY_URI, testing=False, operation="drop_all"
        )


def test_drop_all_nao_executa_quando_guard_falha():
    db = Mock()
    with pytest.raises(RuntimeError, match="recusada"):
        run_test_schema_operation(db, DEV_URI, testing=True, operation="drop_all")
    db.drop_all.assert_not_called()
    db.create_all.assert_not_called()


def test_create_all_executa_apenas_em_sqlite_memory():
    db = Mock()
    run_test_schema_operation(db, SQLITE_MEMORY_URI, testing=True, operation="create_all")
    db.create_all.assert_called_once()
    db.drop_all.assert_not_called()


def test_downgrade_sem_autorizacao_e_bloqueado_em_dev(monkeypatch):
    _clear_downgrade_auth(monkeypatch)
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        assert_downgrade_allowed(DEV_URI)


@pytest.mark.parametrize("uri", [PROD_URI, HOMOLOG_URI])
def test_downgrade_sem_autorizacao_bloqueado_em_homolog_prod(monkeypatch, uri):
    _clear_downgrade_auth(monkeypatch)
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        assert_downgrade_allowed(uri)


def test_downgrade_autorizado_com_flag_e_database_exato(monkeypatch):
    _authorize_downgrade(monkeypatch, "logcompleta_dev")
    assert_downgrade_allowed(DEV_URI)


@pytest.mark.parametrize("raw", ["true", "TRUE", "yes", "on", "t", "01", "0", ""])
def test_downgrade_flag_nao_literal_1_bloqueado(monkeypatch, raw):
    monkeypatch.setenv(ALLOW_DB_DOWNGRADE_ENV, raw)
    monkeypatch.setenv(ALLOW_DB_DOWNGRADE_DATABASE_ENV, "logcompleta_dev")
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        assert_downgrade_allowed(DEV_URI)


def test_downgrade_flag_1_sem_confirmacao_de_database_bloqueado(monkeypatch):
    monkeypatch.setenv(ALLOW_DB_DOWNGRADE_ENV, "1")
    monkeypatch.delenv(ALLOW_DB_DOWNGRADE_DATABASE_ENV, raising=False)
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        assert_downgrade_allowed(DEV_URI)


def test_downgrade_flag_1_com_database_diferente_bloqueado(monkeypatch):
    _authorize_downgrade(monkeypatch, "logcompleta_dev")
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        assert_downgrade_allowed(PROD_URI)


def test_downgrade_prod_com_flag_1_sem_confirmacao_exata_bloqueado(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv(ALLOW_DB_DOWNGRADE_ENV, "1")
    monkeypatch.delenv(ALLOW_DB_DOWNGRADE_DATABASE_ENV, raising=False)
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        assert_downgrade_allowed(PROD_URI)


def test_downgrade_sufixo_test_nao_bypassa_alembic(monkeypatch):
    _clear_downgrade_auth(monkeypatch)
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        assert_downgrade_allowed(PG_TEST_URI)


def test_upgrade_normal_nao_e_bloqueado(monkeypatch):
    _clear_downgrade_auth(monkeypatch)
    guard_alembic_env(
        DEV_URI,
        argv=["python", "-m", "flask", "--app", "app.web", "db", "upgrade"],
    )
    guard_alembic_env(
        PROD_URI,
        argv=["flask", "db", "upgrade"],
    )


def test_start_sh_upgrade_argv_nao_e_bloqueado(monkeypatch):
    _clear_downgrade_auth(monkeypatch)
    guard_alembic_env(
        HOMOLOG_URI,
        argv=["python", "-m", "flask", "--app", "app.web", "db", "upgrade"],
    )


def test_downgrade_via_guard_alembic_sem_autorizacao(monkeypatch):
    _clear_downgrade_auth(monkeypatch)
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        guard_alembic_env(
            DEV_URI,
            argv=["python", "-m", "flask", "--app", "app.web", "db", "downgrade"],
        )


def test_downgrade_via_guard_alembic_autorizado(monkeypatch):
    _authorize_downgrade(monkeypatch, "logcompleta_dev")
    guard_alembic_env(
        DEV_URI,
        argv=["flask", "db", "downgrade", "-1"],
    )


def test_downgrade_incompleto_falha_antes_de_create_engine(monkeypatch):
    monkeypatch.setenv(ALLOW_DB_DOWNGRADE_ENV, "1")
    monkeypatch.delenv(ALLOW_DB_DOWNGRADE_DATABASE_ENV, raising=False)
    create_engine = Mock(name="create_engine")
    with pytest.raises(RuntimeError, match="Downgrade Alembic recusado"):
        guard_alembic_env(DEV_URI, argv=["flask", "db", "downgrade"])
    create_engine.assert_not_called()


def test_alembic_detecta_upgrade_e_downgrade_pelo_argv():
    assert alembic_migration_operation(["flask", "db", "upgrade"]) == "upgrade"
    assert alembic_migration_operation(["flask", "db", "downgrade"]) == "downgrade"
    assert alembic_migration_operation(["flask", "db", "current"]) is None
    opts = SimpleNamespace(cmd=SimpleNamespace(__name__="downgrade"))
    assert alembic_migration_operation(["flask"], cmd_opts=opts) == "downgrade"


def test_identidade_nao_expoe_senha():
    ident = parse_database_identity(DEV_URI)
    assert ident is not None
    assert ident.database == "logcompleta_dev"
    assert ident.host == "127.0.0.1"
    assert ident.port == "5432"
    assert "super-secret" not in ident
    assert ident.driver.startswith("postgres")


def test_env_py_guarda_downgrade_antes_de_create_engine():
    src = Path("migrations/env.py").read_text(encoding="utf-8")
    online = src.split("def run_migrations_online():", 1)[1]
    assert "guard_alembic_env" in src
    assert "_guard_alembic_before_connect(url)" in online
    assert online.index("_guard_alembic_before_connect(url)") < online.index("create_engine(")
    offline = src.split("def run_migrations_offline():", 1)[1].split("def run_migrations_online():", 1)[0]
    assert "_guard_alembic_before_connect(url)" in offline


def test_start_sh_continua_usando_upgrade():
    src = Path("start.sh").read_text(encoding="utf-8")
    assert "db upgrade" in src
    assert "db downgrade" not in src


def test_log_nunca_registra_senha_nem_uri_completa(caplog):
    with caplog.at_level(logging.WARNING, logger="app.db_operational_safety"):
        log_admin_db_operation(DEV_URI, "downgrade")
    text = caplog.text
    assert "super-secret" not in text
    assert DEV_URI not in text
    assert "logcompleta_dev" in text
    assert "downgrade" in text
    assert "127.0.0.1" in text
    assert "5432" in text
