from pathlib import Path
from types import SimpleNamespace

from flask import Blueprint, Flask, render_template_string
from flask_login import LoginManager


def _build_base_render_app(register_cleide: bool) -> Flask:
    root = Path(__file__).resolve().parents[1]
    app = Flask(
        __name__,
        template_folder=str(root / "app" / "templates"),
        static_folder=str(root / "app" / "static"),
    )
    app.secret_key = "test"
    login_manager = LoginManager(app)

    @login_manager.user_loader
    def _load_user(_user_id):
        return None

    @app.route("/")
    def index():
        return "ok"

    @app.route("/fretes")
    def fretes():
        return "ok"

    @app.route("/controle-estoque")
    def controle_estoque():
        return "ok"

    @app.route("/insights-frete")
    def insights_frete():
        return "ok"

    @app.route("/politica-de-privacidade")
    def privacy_policy():
        return "ok"

    @app.route("/login")
    def login():
        return "ok"

    @app.route("/logout")
    def logout():
        return "ok"

    user_bp = Blueprint("user", __name__)

    @user_bp.route("/perfil")
    def perfil():
        return "ok"

    app.register_blueprint(user_bp)

    if register_cleide:
        from app.cleide_routes import cleide_bp

        app.register_blueprint(cleide_bp)

    return app


def _render_base(app: Flask) -> str:
    app.context_processor(
        lambda: {
            "has_endpoint": lambda endpoint_name: endpoint_name in app.view_functions,
        }
    )
    with app.test_request_context("/"):
        from flask import g

        g._login_user = SimpleNamespace(
            is_authenticated=False,
            full_name="",
            email="",
            categoria="free",
            franquia=None,
        )
        return render_template_string('{% include "base.html" %}')


def test_base_render_nao_quebra_sem_blueprint_cleide():
    app = _build_base_render_app(register_cleide=False)
    html = _render_base(app)
    assert "Auditoria de Frete" in html
    assert 'href="/login?next=/auditoria-frete"' in html


def test_base_render_com_blueprint_cleide():
    app = _build_base_render_app(register_cleide=True)
    html = _render_base(app)
    assert "Auditoria de Frete" in html
    assert 'href="/login?next=/auditoria-frete"' in html
