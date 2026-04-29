from flask import get_flashed_messages


def test_admin_julia_frequencia_aceita_minutos_em_homolog(app, monkeypatch):
    app.secret_key = "test-secret"
    app.config["SERVER_NAME"] = "localhost"

    from app.painel_admin.admin_routes import admin_bp, agentes_julia_configurar_frequencia
    from app.services import agent_service

    app.register_blueprint(admin_bp)
    monkeypatch.setenv("APP_ENV", "homolog")
    monkeypatch.setattr("app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True)

    with app.app_context():
        with app.test_request_context(
            "/admin/agentes/julia/frequencia",
            method="POST",
            data={"frequencia_minutos": "5"},
        ):
            response = agentes_julia_configurar_frequencia.__wrapped__()
            assert response.status_code == 302
            assert agent_service.obter_frequencia_minutos() == 5
            mensagens = get_flashed_messages(with_categories=True)
            assert any("5 min" in msg for _cat, msg in mensagens)


def test_admin_finance_frequencia_aceita_minutos_em_homolog(app, monkeypatch):
    app.secret_key = "test-secret"
    app.config["SERVER_NAME"] = "localhost"

    from app.finance import obter_finance_frequencia_minutos
    from app.painel_admin.admin_routes import admin_bp, indices_configurar_frequencia

    app.register_blueprint(admin_bp)
    monkeypatch.setenv("APP_ENV", "homolog")
    monkeypatch.setattr("app.painel_admin.admin_routes.verificar_acesso_admin", lambda: True)

    with app.app_context():
        with app.test_request_context(
            "/admin/indices/frequencia",
            method="POST",
            data={"finance_frequencia_minutos": "15"},
        ):
            response = indices_configurar_frequencia.__wrapped__()
            assert response.status_code == 302
            assert obter_finance_frequencia_minutos() == 15
            mensagens = get_flashed_messages(with_categories=True)
            assert any("15 min" in msg for _cat, msg in mensagens)
