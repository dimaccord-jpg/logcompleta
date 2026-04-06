import pytest
from flask import Flask

from app.extensions import db


@pytest.fixture(scope="function")
def app():
    flask_app = Flask(__name__)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401 — registra metadados das tabelas
        db.create_all()
    yield flask_app
    with flask_app.app_context():
        db.drop_all()


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
