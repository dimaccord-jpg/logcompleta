from __future__ import annotations

import csv
import io
import importlib
import sys
from datetime import datetime, timedelta

from sqlalchemy import text

from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import (
    ConfigRegras,
    ContaMonetizacaoVinculo,
    Franquia,
    MonetizacaoFato,
    User,
    utcnow_naive,
)
from app.services.cleiton_monetizacao_service import STATUS_TEC_APLICADO
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _build_admin_client(app, monkeypatch, tmp_path):
    app.config["SECRET_KEY"] = "test-secret-admin-csv"
    app.config["TESTING"] = True
    app.config["SERVER_NAME"] = "localhost"
    data_dir = tmp_path / "admin_csv_data"
    data_dir.mkdir(exist_ok=True)

    env_loader = importlib.import_module("app.env_loader")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("SECRET_KEY", "test-secret-admin-csv")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg2://user:pass@localhost:5432/testdb",
    )
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(env_loader, "load_app_env", lambda: True)
    monkeypatch.setattr(env_loader, "validate_runtime_env", lambda: None)
    monkeypatch.setattr(env_loader, "resolve_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(
        env_loader, "resolve_indices_file_path", lambda: str(data_dir / "indices.json")
    )

    sys.modules.pop("app.settings", None)
    from app.painel_admin.admin_routes import admin_bp

    if "admin" not in app.blueprints:
        app.register_blueprint(admin_bp)
    if "login" not in app.view_functions:
        app.add_url_rule("/login", "login", lambda: "login")
    login_manager.init_app(app)

    @login_manager.user_loader
    def _load_user(user_id):  # noqa: ANN001
        return get_user_by_id(user_id)

    return app.test_client()


def _login(client, user: User) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        sess["_fresh"] = True


def _read_csv_rows(response_data: bytes) -> list[dict[str, str]]:
    text = response_data.decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def _permitir_multiplos_vinculos_sqlite() -> None:
    # Em SQLite de testes, o índice parcial vira UNIQUE simples; removemos para
    # simular o comportamento histórico permitido em PostgreSQL.
    db.session.execute(
        text("DROP INDEX IF EXISTS uq_conta_monetizacao_vinculo_conta_ativo_true")
    )
    db.session.commit()


def _criar_vinculo(
    *,
    conta_id: int,
    ativo: bool,
    customer_id: str,
    subscription_id: str,
    plano_interno: str = "pro",
    status_contratual_externo: str = "active",
    snapshot: dict | None = None,
    price_id: str = "price_test",
) -> ContaMonetizacaoVinculo:
    import json

    row = ContaMonetizacaoVinculo(
        conta_id=conta_id,
        provider="stripe",
        customer_id=customer_id,
        subscription_id=subscription_id,
        price_id=price_id,
        plano_interno=plano_interno,
        status_contratual_externo=status_contratual_externo,
        ativo=ativo,
        snapshot_normalizado_json=json.dumps(snapshot or {}),
        payload_bruto_sanitizado_json='{"source":"test"}',
    )
    db.session.add(row)
    db.session.flush()
    if not ativo:
        row.desativado_em = utcnow_naive()
    db.session.add(row)
    db.session.commit()
    return row


def _criar_fato(
    conta_id: int,
    *,
    customer_id: str,
    subscription_id: str,
    status_tecnico: str = "ok",
    timestamp: datetime | None = None,
    event_id: str = "evt_test",
) -> MonetizacaoFato:
    row = MonetizacaoFato(
        tipo_fato="stripe_event",
        status_tecnico=status_tecnico,
        conta_id=conta_id,
        snapshot_normalizado_json="{}",
        provider="stripe",
        external_event_id=event_id,
        invoice_id="in_test",
        customer_id=customer_id,
        subscription_id=subscription_id,
        price_id="price_test",
        timestamp_interno=timestamp or utcnow_naive(),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _upsert_config(chave: str, valor_texto: str) -> None:
    row = ConfigRegras.query.filter_by(chave=chave).first()
    if row is None:
        row = ConfigRegras(chave=chave, descricao="teste")
        db.session.add(row)
    row.valor_texto = valor_texto
    db.session.add(row)
    db.session.commit()


def test_rota_auditoria_csv_exige_login(app, monkeypatch, tmp_path):
    client = _build_admin_client(app, monkeypatch, tmp_path)
    response = client.get("/admin/dashboard/auditoria-clientes.csv")
    assert response.status_code == 302
    assert "/login" in (response.headers.get("Location") or "")


def test_rota_auditoria_csv_bloqueia_usuario_nao_admin(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-nao-admin")
        user = seed_usuario(franquia.id, conta.id, email="naoadmin@test.com", categoria="free")
        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, user)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")
    assert response.status_code == 403


def test_admin_baixa_csv_com_header_obrigatorio(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-header")
        admin = seed_usuario(
            franquia.id,
            conta.id,
            email="admin-header@test.com",
            categoria="free",
        )
        admin.is_admin = True
        db.session.add(admin)
        db.session.commit()
        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")
    assert response.status_code == 200
    assert response.headers.get("Content-Type", "").startswith("text/csv")
    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment;" in disposition
    assert "auditoria_clientes_admin_" in disposition
    rows = _read_csv_rows(response.data)
    assert rows
    assert "user_id" in rows[0]
    assert "plano_usuario_legacy" in rows[0]
    assert "plano_contratual_vinculo" in rows[0]
    assert "status_operacional_franquia" in rows[0]
    assert "fonte_verdade_operacional" in rows[0]
    assert "fonte_verdade_contratual" in rows[0]
    assert "vinculo_canonico_ambiguo" in rows[0]
    assert "criterio_vinculo_exibido" in rows[0]
    assert "vinculo_confiabilidade" in rows[0]
    assert "motivo_vinculo_confiabilidade" in rows[0]
    assert "ultimo_fato_efeito_status" in rows[0]
    assert "ultimo_fato_relevante_tipo" in rows[0]
    assert "price_id_esperado_plano_contratual" in rows[0]
    assert "price_id_configurado_encontrado" in rows[0]
    assert "flag_price_id_config_ausente" in rows[0]
    assert "flag_price_id_incompativel_plano" in rows[0]
    assert "observacao_legacy_categoria" in rows[0]
    assert "status_divergencia_severidade" in rows[0]
    assert "nivel_risco_auditoria" in rows[0]
    assert "flag_requer_revisao_manual" in rows[0]


def test_filtros_dashboard_refletem_no_csv(app, monkeypatch, tmp_path):
    with app.app_context():
        conta1, franquia1 = seed_conta_franquia_cliente(slug="conta-filtros-1")
        conta2, franquia2 = seed_conta_franquia_cliente(slug="conta-filtros-2")
        franquia1.status = "expired"
        franquia2.status = "active"
        db.session.add(franquia1)
        db.session.add(franquia2)
        db.session.flush()

        admin = seed_usuario(franquia1.id, conta1.id, email="admin-filter@test.com", categoria="free")
        admin.is_admin = True
        user_ok = seed_usuario(franquia1.id, conta1.id, email="ok-filter@test.com", categoria="free")
        seed_usuario(
            franquia2.id,
            conta2.id,
            email="encerrado_user_01@anon.local",
            categoria="pro",
        )
        db.session.add(admin)
        db.session.add(user_ok)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get(
            "/admin/dashboard/auditoria-clientes.csv"
            "?categoria=free&franquia_status=expired&cancelado=ativos"
        )
    assert response.status_code == 200
    rows = _read_csv_rows(response.data)
    assert len(rows) == 2
    assert {row["email"] for row in rows} == {"admin-filter@test.com", "ok-filter@test.com"}


def test_flag_free_com_fim_ciclo(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-free-fim")
        franquia.fim_ciclo = utcnow_naive() + timedelta(days=7)
        db.session.add(franquia)
        admin = seed_usuario(franquia.id, conta.id, email="admin-free-fim@test.com", categoria="free")
        admin.is_admin = True
        alvo = seed_usuario(franquia.id, conta.id, email="free-fim@test.com", categoria="free")
        db.session.add(admin)
        db.session.add(alvo)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv?categoria=free")
    rows = _read_csv_rows(response.data)
    alvo_row = next(r for r in rows if r["email"] == "free-fim@test.com")
    assert alvo_row["free_com_fim_ciclo_preenchido"] == "true"
    assert alvo_row["flag_free_com_fim_ciclo"] == "true"


def test_flag_free_expired_e_expired_sem_bloqueio_manual(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-free-expired")
        franquia.status = "expired"
        franquia.bloqueio_manual = False
        db.session.add(franquia)
        admin = seed_usuario(franquia.id, conta.id, email="admin-free-exp@test.com", categoria="free")
        admin.is_admin = True
        alvo = seed_usuario(franquia.id, conta.id, email="free-expired@test.com", categoria="free")
        db.session.add(admin)
        db.session.add(alvo)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv?categoria=free")
    rows = _read_csv_rows(response.data)
    alvo_row = next(r for r in rows if r["email"] == "free-expired@test.com")
    assert alvo_row["flag_free_expired"] == "true"
    assert alvo_row["expired_sem_bloqueio_manual"] == "true"


def test_pago_com_vinculo_inconclusivo_por_historico_sinaliza_risco(app, monkeypatch, tmp_path):
    with app.app_context():
        _permitir_multiplos_vinculos_sqlite()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-multi-customer")
        admin = seed_usuario(franquia.id, conta.id, email="admin-multi-cus@test.com", categoria="pro")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="multi-customer@test.com", categoria="pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=False,
            customer_id="cus_old",
            subscription_id="sub_old",
            plano_interno="pro",
        )
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_new",
            subscription_id="sub_new",
            plano_interno="pro",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")
    rows = _read_csv_rows(response.data)
    alvo_row = next(r for r in rows if r["email"] == "multi-customer@test.com")
    assert alvo_row["flag_multiplos_customers_historico"] == "true"
    assert alvo_row["vinculo_confiabilidade"] == "inconclusivo"
    assert alvo_row["vinculo_confiabilidade_conclusiva"] == "false"
    assert alvo_row["flag_pago_vinculo_inconclusivo"] == "true"
    assert alvo_row["flag_pago_sem_subscription_ativa"] == "true"
    assert alvo_row["flag_ids_entrelacados"] == "false"
    assert alvo_row["nivel_risco_auditoria"] == "crítico"


def test_pago_com_subscription_historica_multipla_sinaliza_risco(app, monkeypatch, tmp_path):
    with app.app_context():
        _permitir_multiplos_vinculos_sqlite()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-multi-sub")
        admin = seed_usuario(franquia.id, conta.id, email="admin-multi-sub@test.com", categoria="pro")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="multi-sub@test.com", categoria="pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=False,
            customer_id="cus_same",
            subscription_id="sub_old",
            plano_interno="pro",
        )
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_same",
            subscription_id="sub_new",
            plano_interno="pro",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")
    rows = _read_csv_rows(response.data)
    alvo_row = next(r for r in rows if r["email"] == "multi-sub@test.com")
    assert alvo_row["flag_multiplas_subscriptions_historico"] == "true"
    assert alvo_row["vinculo_confiabilidade"] == "inconclusivo"
    assert alvo_row["flag_pago_vinculo_inconclusivo"] == "true"
    assert alvo_row["nivel_risco_auditoria"] == "crítico"


def test_mismatch_legacy_nao_gera_critico_sozinho(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-plano-div")
        _upsert_config("plano_gateway_price_id_admin_pro", "price_pro_legado")
        admin = seed_usuario(franquia.id, conta.id, email="admin-plano@test.com", categoria="starter")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="plano-div@test.com", categoria="starter")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_plan",
            subscription_id="sub_plan",
            plano_interno="pro",
            price_id="price_pro_legado",
            status_contratual_externo="active",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")
    rows = _read_csv_rows(response.data)
    alvo_row = next(r for r in rows if r["email"] == "plano-div@test.com")
    assert alvo_row["flag_legacy_user_categoria_vs_vinculo"] == "true"
    assert alvo_row["flag_plano_user_vs_vinculo"] == "true"
    assert "nao usar isoladamente" in alvo_row["observacao_legacy_categoria"]
    assert alvo_row["nivel_risco_auditoria"] == "atenção"


def test_flag_pendencia_perdida(app, monkeypatch, tmp_path):
    with app.app_context():
        _permitir_multiplos_vinculos_sqlite()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pendencia-perdida")
        admin = seed_usuario(franquia.id, conta.id, email="admin-pend@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="pendencia@test.com", categoria="pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=False,
            customer_id="cus_old",
            subscription_id="sub_old",
            plano_interno="pro",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "free",
                "efetivar_em": (utcnow_naive() - timedelta(days=4)).isoformat(),
            },
        )
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_new",
            subscription_id="sub_new",
            plano_interno="pro",
            snapshot={"mudanca_pendente": False},
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")
    rows = _read_csv_rows(response.data)
    alvo_row = next(r for r in rows if r["email"] == "pendencia@test.com")
    assert alvo_row["flag_pendencia_perdida"] == "true"
    assert alvo_row["pendencia_desativada_vencida"] == "true"
    assert alvo_row["flag_pendencia_perdida_vencida"] == "true"
    assert alvo_row["pendencia_resolvida_por_fato_correlacionado"] == "false"


def test_pendencia_perdida_resolvida_por_fato_correlacionado(app, monkeypatch, tmp_path):
    with app.app_context():
        _permitir_multiplos_vinculos_sqlite()
        base_now = utcnow_naive()
        efetivar = base_now - timedelta(days=3)
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pendencia-resolvida")
        admin = seed_usuario(franquia.id, conta.id, email="admin-pr@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="pend-resolvida@test.com", categoria="pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=False,
            customer_id="cus_corr",
            subscription_id="sub_corr",
            plano_interno="pro",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": efetivar.isoformat(),
            },
        )
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_corr",
            subscription_id="sub_corr",
            plano_interno="starter",
            snapshot={"mudanca_pendente": False},
        )
        _criar_fato(
            conta.id,
            customer_id="cus_corr",
            subscription_id="sub_corr",
            status_tecnico=STATUS_TEC_APLICADO,
            timestamp=efetivar + timedelta(hours=2),
            event_id="evt_corr",
        )
        fato_corr = MonetizacaoFato.query.filter_by(
            conta_id=conta.id, external_event_id="evt_corr"
        ).first()
        fato_corr.tipo_fato = "cleiton_downgrade_efetivado_virada_ciclo"
        db.session.add(fato_corr)
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "pend-resolvida@test.com")
    assert row["pendencia_resolvida_por_fato_correlacionado"] == "true"
    assert row["flag_pendencia_perdida_vencida"] == "false"
    assert row["pendencia_fato_correlacionado_tipo"] != ""
    assert row["pendencia_correlacao_forca"] == "forte"
    assert row["pendencia_janela_resolucao_horas"] == "48"


def test_fato_aplicado_posterior_sem_correlacao_nao_neutraliza_pendencia(app, monkeypatch, tmp_path):
    with app.app_context():
        _permitir_multiplos_vinculos_sqlite()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pendencia-sem-corr")
        admin = seed_usuario(franquia.id, conta.id, email="admin-psc@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="pend-sem-corr@test.com", categoria="pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=False,
            customer_id="cus_old_corr",
            subscription_id="sub_old_corr",
            plano_interno="pro",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (utcnow_naive() - timedelta(days=3)).isoformat(),
            },
        )
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_new_corr",
            subscription_id="sub_new_corr",
            plano_interno="pro",
            snapshot={"mudanca_pendente": False},
        )
        _criar_fato(
            conta.id,
            customer_id="cus_outro",
            subscription_id="sub_outro",
            status_tecnico=STATUS_TEC_APLICADO,
            timestamp=utcnow_naive() - timedelta(days=1),
            event_id="evt_sem_corr",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "pend-sem-corr@test.com")
    assert row["pendencia_resolvida_por_fato_correlacionado"] == "false"
    assert row["pendencia_correlacao_forca"] in {"fraca", "ausente"}
    assert row["flag_pendencia_perdida_vencida"] == "true"


def test_fato_correlato_fora_da_janela_nao_neutraliza_pendencia(app, monkeypatch, tmp_path):
    with app.app_context():
        _permitir_multiplos_vinculos_sqlite()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pendencia-fora-janela")
        admin = seed_usuario(franquia.id, conta.id, email="admin-pfj@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="pend-fora-janela@test.com", categoria="pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=False,
            customer_id="cus_jan",
            subscription_id="sub_jan",
            plano_interno="pro",
            snapshot={
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": (utcnow_naive() - timedelta(days=4)).isoformat(),
            },
        )
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_jan",
            subscription_id="sub_jan",
            plano_interno="starter",
            snapshot={"mudanca_pendente": False},
        )
        _criar_fato(
            conta.id,
            customer_id="cus_jan",
            subscription_id="sub_jan",
            status_tecnico=STATUS_TEC_APLICADO,
            timestamp=utcnow_naive(),
            event_id="evt_fora_janela",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(
        r for r in _read_csv_rows(response.data) if r["email"] == "pend-fora-janela@test.com"
    )
    assert row["pendencia_correlacao_forca"] == "ausente"
    assert row["pendencia_resolvida_por_fato_correlacionado"] == "false"
    assert row["flag_pendencia_perdida_vencida"] == "true"


def test_export_csv_nao_altera_banco(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-readonly")
        admin = seed_usuario(franquia.id, conta.id, email="admin-read@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="readonly@test.com", categoria="free")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_ro",
            subscription_id="sub_ro",
            plano_interno="starter",
        )
        _criar_fato(conta.id, customer_id="cus_ro", subscription_id="sub_ro")
        db.session.add(admin)
        db.session.commit()

        antes_vinculos = ContaMonetizacaoVinculo.query.count()
        antes_fatos = MonetizacaoFato.query.count()
        before_users = User.query.count()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")
        assert response.status_code == 200

        depois_vinculos = ContaMonetizacaoVinculo.query.count()
        depois_fatos = MonetizacaoFato.query.count()
        after_users = User.query.count()

    assert antes_vinculos == depois_vinculos
    assert antes_fatos == depois_fatos
    assert before_users == after_users


def test_multiplos_vinculos_ativos_gera_ambiguo_e_risco_critico(app, monkeypatch, tmp_path):
    with app.app_context():
        _permitir_multiplos_vinculos_sqlite()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ambigua")
        admin = seed_usuario(franquia.id, conta.id, email="admin-amb@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="ambigua@test.com", categoria="pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_a1",
            subscription_id="sub_a1",
            plano_interno="pro",
        )
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_a2",
            subscription_id="sub_a2",
            plano_interno="pro",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "ambigua@test.com")
    assert row["vinculo_canonico_ambiguo"] == "true"
    assert row["criterio_vinculo_exibido"] == "multiplos_ativos_mais_recente_para_exibicao"
    assert row["vinculo_confiabilidade"] == "ambiguo"
    assert row["flag_multiplos_vinculos_ativos"] == "true"
    assert row["nivel_risco_auditoria"] == "crítico"


def test_price_id_incompativel_plano_sinaliza_flag(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-price-mismatch")
        admin = seed_usuario(franquia.id, conta.id, email="admin-price@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="price-mismatch@test.com", categoria="pro")
        _upsert_config("plano_gateway_price_id_admin_pro", "price_expected_pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_pm",
            subscription_id="sub_pm",
            plano_interno="pro",
            price_id="price_other",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(
        r for r in _read_csv_rows(response.data) if r["email"] == "price-mismatch@test.com"
    )
    assert row["price_id_esperado_plano_contratual"] == "price_expected_pro"
    assert row["price_id_configurado_encontrado"] == "true"
    assert row["flag_price_id_config_ausente"] == "false"
    assert row["flag_price_id_incompativel_plano"] == "true"
    assert row["nivel_risco_auditoria"] == "crítico"


def test_price_id_config_ausente_gera_atencao(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-price-missing")
        admin = seed_usuario(franquia.id, conta.id, email="admin-pmiss@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="price-missing@test.com", categoria="pro")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_pmiss",
            subscription_id="sub_pmiss",
            plano_interno="pro",
            price_id="price_any",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "price-missing@test.com")
    assert row["price_id_configurado_encontrado"] == "false"
    assert row["flag_price_id_config_ausente"] == "true"
    assert row["plano_contratual_eh_pago"] == "true"
    assert row["nivel_risco_auditoria"] in {"atenção", "crítico"}


def test_novo_plano_pago_vindo_de_config_eh_tratado_como_pago(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-plano-dinamico")
        admin = seed_usuario(franquia.id, conta.id, email="admin-dyn@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="plano-dyn@test.com", categoria="free")
        _upsert_config("plano_valor_admin_growth", "79.90")
        _upsert_config("plano_gateway_price_id_admin_growth", "price_growth_01")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_growth",
            subscription_id="sub_growth",
            plano_interno="growth",
            price_id="price_growth_01",
            status_contratual_externo="active",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "plano-dyn@test.com")
    assert row["plano_contratual_eh_pago"] == "true"


def test_vinculo_unico_limpo_pode_ser_confiavel(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-vinculo-confiavel")
        admin = seed_usuario(franquia.id, conta.id, email="admin-conf@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="vinculo-conf@test.com", categoria="free")
        _upsert_config("plano_gateway_price_id_admin_pro", "price_pro_ok")
        _criar_vinculo(
            conta_id=conta.id,
            ativo=True,
            customer_id="cus_conf",
            subscription_id="sub_conf",
            plano_interno="pro",
            price_id="price_pro_ok",
            status_contratual_externo="active",
        )
        _criar_fato(
            conta.id,
            customer_id="cus_conf",
            subscription_id="sub_conf",
            status_tecnico=STATUS_TEC_APLICADO,
            timestamp=utcnow_naive(),
            event_id="evt_conf",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "vinculo-conf@test.com")
    assert row["vinculo_confiabilidade"] == "confiavel"
    assert row["vinculo_confiabilidade_conclusiva"] == "true"
    assert row["flag_pago_sem_subscription_ativa"] == "false"


def test_ultimo_fato_sem_efeito_nao_preenche_colunas_efeito(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-fato-sem-efeito")
        admin = seed_usuario(franquia.id, conta.id, email="admin-fse@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="fato-sem-efeito@test.com", categoria="free")
        _criar_fato(
            conta.id,
            customer_id="cus_fse",
            subscription_id="sub_fse",
            status_tecnico="registrado_sem_efeito_operacional",
            event_id="evt_sem_efeito",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(
        r for r in _read_csv_rows(response.data) if r["email"] == "fato-sem-efeito@test.com"
    )
    assert row["ultimo_fato_geral_status"] == "registrado_sem_efeito_operacional"
    assert row["ultimo_fato_efeito_status"] == ""
    assert row["ultimo_fato_efeito_event_id"] == ""
    assert row["ultimo_fato_relevante_status"] == ""


def test_fato_guardrail_entra_como_ultimo_relevante(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-fato-guardrail")
        admin = seed_usuario(franquia.id, conta.id, email="admin-fr@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="fato-guardrail@test.com", categoria="free")
        _criar_fato(
            conta.id,
            customer_id="cus_guard",
            subscription_id="sub_guard",
            status_tecnico="registrado_sem_efeito_operacional",
            event_id="evt_guard",
        )
        fato = MonetizacaoFato.query.filter_by(conta_id=conta.id).first()
        fato.tipo_fato = "stripe_vinculo_guardrail_ids_inconsistentes"
        db.session.add(fato)
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "fato-guardrail@test.com")
    assert row["ultimo_fato_relevante_tipo"] == "stripe_vinculo_guardrail_ids_inconsistentes"
    assert row["ultimo_fato_relevante_event_id"] == "evt_guard"
    assert row["ultimo_fato_relevante_criterio"] == "tipo_explicito"


def test_fato_com_nome_bloque_fora_da_lista_nao_entra_como_relevante(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-fato-bloque-fallback")
        admin = seed_usuario(franquia.id, conta.id, email="admin-bloq@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="fato-bloq@test.com", categoria="free")
        _criar_fato(
            conta.id,
            customer_id="cus_bq",
            subscription_id="sub_bq",
            status_tecnico="registrado_sem_efeito_operacional",
            event_id="evt_bq",
        )
        fato = MonetizacaoFato.query.filter_by(conta_id=conta.id).first()
        fato.tipo_fato = "fato_teste_bloqueio_custom_nao_listado"
        db.session.add(fato)
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "fato-bloq@test.com")
    assert row["ultimo_fato_relevante_tipo"] == ""
    assert row["ultimo_fato_relevante_criterio"] == ""


def test_ultimo_fato_com_efeito_preenche_colunas_efeito(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-fato-com-efeito")
        admin = seed_usuario(franquia.id, conta.id, email="admin-fce@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="fato-com-efeito@test.com", categoria="free")
        _criar_fato(
            conta.id,
            customer_id="cus_old",
            subscription_id="sub_old",
            status_tecnico="registrado_sem_efeito_operacional",
            timestamp=utcnow_naive() - timedelta(days=1),
            event_id="evt_old",
        )
        _criar_fato(
            conta.id,
            customer_id="cus_eff",
            subscription_id="sub_eff",
            status_tecnico=STATUS_TEC_APLICADO,
            timestamp=utcnow_naive(),
            event_id="evt_eff",
        )
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(
        r for r in _read_csv_rows(response.data) if r["email"] == "fato-com-efeito@test.com"
    )
    assert row["ultimo_fato_efeito_status"] == STATUS_TEC_APLICADO
    assert row["ultimo_fato_efeito_event_id"] == "evt_eff"
    assert row["ultimo_fato_efeito_customer_id"] == "cus_eff"


def test_status_persistido_diverge_isolado_gera_atencao(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-status-diverge")
        franquia.status = "active"
        franquia.fim_ciclo = utcnow_naive() - timedelta(days=2)
        db.session.add(franquia)
        admin = seed_usuario(franquia.id, conta.id, email="admin-status@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="status-diverge@test.com", categoria="free")
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "status-diverge@test.com")
    assert row["status_operacional_franquia"] == "active"
    assert row["status_operacional_recalculado"] == "expired"
    assert row["flag_status_persistido_diverge_recalculado"] == "true"
    assert row["status_divergencia_severidade"] == "atenção"
    assert row["nivel_risco_auditoria"] == "atenção"


def test_status_divergente_com_free_expired_fim_ciclo_vira_critico(app, monkeypatch, tmp_path):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-status-critico")
        franquia.status = "expired"
        franquia.fim_ciclo = utcnow_naive() + timedelta(days=3)
        db.session.add(franquia)
        admin = seed_usuario(franquia.id, conta.id, email="admin-status-crit@test.com", categoria="free")
        admin.is_admin = True
        seed_usuario(franquia.id, conta.id, email="status-critico@test.com", categoria="free")
        db.session.add(admin)
        db.session.commit()

        client = _build_admin_client(app, monkeypatch, tmp_path)
        _login(client, admin)
        response = client.get("/admin/dashboard/auditoria-clientes.csv")

    row = next(r for r in _read_csv_rows(response.data) if r["email"] == "status-critico@test.com")
    assert row["flag_status_persistido_diverge_recalculado"] == "true"
    assert row["status_divergencia_severidade"] == "crítico"
    assert row["nivel_risco_auditoria"] == "crítico"
