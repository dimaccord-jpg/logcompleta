import os
import shutil
from pathlib import Path
from uuid import uuid4

# Isolamento de pytest DEVE rodar antes de qualquer import da aplicação.
# Assignment (não setdefault): DATABASE_URL herdada do shell não pode vencer.
PYTEST_DISPOSABLE_SQLALCHEMY_URI = "sqlite:///:memory:"
PYTEST_PROCESS_DATABASE_URL = (
    "postgresql://pytest:pytest@pytest-isolation.invalid:5432/logcompleta_pytest_test"
)


def apply_pytest_database_isolation() -> None:
    """Força ambiente de teste descartável. Não herda DATABASE_URL operacional."""
    os.environ["TESTING"] = "1"
    os.environ["DATABASE_URL"] = PYTEST_PROCESS_DATABASE_URL


apply_pytest_database_isolation()

import pytest
from flask import Flask

from app.db_operational_safety import run_test_schema_operation
from app.extensions import db


def _patch_pytest_tmp_cleanup_for_windows() -> None:
    import sys

    if sys.platform != "win32":
        return

    from _pytest import pathlib as pytest_pathlib
    from _pytest import tmpdir as pytest_tmpdir

    original_cleanup = pytest_pathlib.cleanup_dead_symlinks

    def _safe_cleanup_dead_symlinks(root):
        try:
            return original_cleanup(root)
        except PermissionError:
            # Python 3.14 + Windows can deny scandir/rmtree on pytest's own
            # temp root during session teardown even when tests passed.
            return None

    pytest_pathlib.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks
    pytest_tmpdir.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks


_patch_pytest_tmp_cleanup_for_windows()

pytest_plugins = ("tests.cleiton_doc_fixtures",)


def pytest_configure(config):
    apply_pytest_database_isolation()


def pytest_sessionstart(session):
    apply_pytest_database_isolation()
    from app.db_operational_safety import is_disposable_test_database

    process_uri = os.environ.get("DATABASE_URL") or ""
    if process_uri != PYTEST_PROCESS_DATABASE_URL:
        raise RuntimeError(
            "pytest recusado: isolamento central de DATABASE_URL não aplicado. "
            "URI operacional herdada não pode permanecer no processo de teste."
        )
    if not is_disposable_test_database(PYTEST_DISPOSABLE_SQLALCHEMY_URI):
        raise RuntimeError(
            "pytest recusado: URI descartável do fixture de teste é inválida."
        )


@pytest.fixture(scope="function")
def app():
    flask_app = Flask(__name__)
    # URI do fixture é constante — DATABASE_URL do processo não decide este banco.
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = PYTEST_DISPOSABLE_SQLALCHEMY_URI
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401 — registra metadados das tabelas
        run_test_schema_operation(
            db,
            flask_app.config["SQLALCHEMY_DATABASE_URI"],
            testing=True,
            operation="create_all",
        )
    yield flask_app
    with flask_app.app_context():
        run_test_schema_operation(
            db,
            flask_app.config["SQLALCHEMY_DATABASE_URI"],
            testing=True,
            operation="drop_all",
        )


@pytest.fixture
def tmp_path():
    root = Path.cwd() / ".tmp_pytest_fixture"
    root.mkdir(exist_ok=True)
    path = root / f"logcompleta_{uuid4().hex}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_sistema_interno_cache():
    import app.services.conta_franquia_service as cfs

    cfs._sistema_ids_cache = None
    yield
    cfs._sistema_ids_cache = None


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield


def seed_sistema_interno():
    from app.models import Conta, Franquia

    c = Conta(nome="Sistema", slug=Conta.SLUG_SISTEMA, status=Conta.STATUS_ATIVA)
    db.session.add(c)
    db.session.flush()
    f = Franquia(
        conta_id=c.id,
        nome="Operacional interno",
        slug=Franquia.SLUG_SISTEMA_OPERACIONAL,
        status=Franquia.STATUS_ACTIVE,
    )
    db.session.add(f)
    db.session.commit()
    return c, f


def seed_cleiton_cost_config():
    from app.models import CleitonCostConfig

    row = CleitonCostConfig(
        id=1,
        month_seconds=2592000,
        allocation_percent=1.0,
        overhead_factor=1.0,
        credit_tokens_per_credit=1000.0,
        credit_lines_per_credit=100.0,
        credit_ms_per_credit=1000.0,
    )
    db.session.add(row)
    db.session.commit()
    return row


def seed_conta_franquia_cliente(slug="conta-cli"):
    from app.models import Conta, Franquia

    c = Conta(nome="Cliente", slug=slug, status=Conta.STATUS_ATIVA)
    db.session.add(c)
    db.session.flush()
    f = Franquia(
        conta_id=c.id,
        nome="Principal",
        slug="principal",
        status=Franquia.STATUS_ACTIVE,
    )
    db.session.add(f)
    db.session.commit()
    return c, f


def seed_usuario(franquia_id: int, conta_id: int, email="u@test.com", categoria="free"):
    from app.models import User

    u = User(
        email=email,
        full_name="Test",
        categoria=categoria,
        conta_id=conta_id,
        franquia_id=franquia_id,
    )
    db.session.add(u)
    db.session.commit()
    return u
