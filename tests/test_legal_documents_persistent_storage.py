import importlib
import io
import os
from pathlib import Path
from types import SimpleNamespace

from werkzeug.datastructures import FileStorage

os.environ.setdefault("APP_ENV", "dev")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
os.environ.setdefault("SECRET_KEY", "test-secret")

from app.extensions import db
from app.models import Conta, Franquia, PrivacyPolicy, TermsOfUse, User


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _patch_legal_data_dir(monkeypatch, tmp_path: Path) -> None:
    import app.legal_document_storage as legal_storage

    monkeypatch.setattr(
        legal_storage,
        "settings",
        SimpleNamespace(data_dir=str(tmp_path)),
    )


def _seed_basic_user(email: str = "user@test.com") -> User:
    conta = Conta(nome="Conta Teste", slug=f"conta-{email}", status=Conta.STATUS_ATIVA)
    db.session.add(conta)
    db.session.flush()
    franquia = Franquia(
        conta_id=conta.id,
        nome="Franquia Teste",
        slug=f"franquia-{email}",
        status=Franquia.STATUS_ACTIVE,
    )
    db.session.add(franquia)
    db.session.flush()
    user = User(
        email=email,
        full_name="Usuario Teste",
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    db.session.add(user)
    db.session.commit()
    return user


def test_get_terms_upload_dir_usa_data_dir_persistente(monkeypatch, tmp_path):
    import app.terms_services as terms_services

    _patch_legal_data_dir(monkeypatch, tmp_path)
    expected = tmp_path / "legal" / "terms"

    assert Path(terms_services.get_terms_upload_dir()) == expected


def test_get_privacy_policy_upload_dir_usa_data_dir_persistente(monkeypatch, tmp_path):
    import app.privacy_policy_services as privacy_services

    _patch_legal_data_dir(monkeypatch, tmp_path)
    expected = tmp_path / "legal" / "privacy_policies"

    assert Path(privacy_services.get_privacy_policy_upload_dir()) == expected


def test_upload_termos_salva_no_storage_persistente_e_notifica_link_publico(
    app, ctx, monkeypatch, tmp_path
):
    from app.services import termo_service

    _patch_legal_data_dir(monkeypatch, tmp_path)
    _seed_basic_user(email="termos@test.com")
    captured_urls = []
    monkeypatch.setattr(
        termo_service,
        "send_terms_updated_notification",
        lambda _email, _name, terms_url: captured_urls.append(terms_url),
    )
    upload = FileStorage(
        stream=io.BytesIO(b"%PDF-1.4 termo persistente"),
        filename="termo_atualizado.pdf",
        content_type="application/pdf",
    )
    app.add_url_rule("/termos-de-uso", endpoint="terms_of_use", view_func=lambda: "ok")

    with app.test_request_context("/", base_url="https://agentefrete.test"):
        sent, failed = termo_service.processar_upload_termo(app, upload)

    persisted_dir = tmp_path / "legal" / "terms"
    active = TermsOfUse.query.filter_by(is_active=True).first()

    assert sent == 1
    assert failed == 0
    assert active is not None
    assert active.filename
    assert (persisted_dir / active.filename).is_file()
    assert captured_urls == ["https://agentefrete.test/termos-de-uso"]


def test_upload_privacy_policy_salva_no_storage_persistente(app, ctx, monkeypatch, tmp_path):
    from app.services import privacy_policy_service

    _patch_legal_data_dir(monkeypatch, tmp_path)
    _seed_basic_user(email="privacy@test.com")
    captured = {}

    class _FailingExecutor:
        def submit(self, *_args, **_kwargs):
            raise RuntimeError("executor indisponivel no teste")

    def _fake_notify(_app, policy_url, _upload_date):
        captured["policy_url"] = policy_url
        return 1, 0

    monkeypatch.setattr(privacy_policy_service, "get_admin_executor", lambda: _FailingExecutor())
    monkeypatch.setattr(privacy_policy_service, "_notify_privacy_policy_update", _fake_notify)

    upload = FileStorage(
        stream=io.BytesIO(b"%PDF-1.4 privacy persistente"),
        filename="politica.pdf",
        content_type="application/pdf",
    )
    app.add_url_rule(
        "/politica-de-privacidade",
        endpoint="privacy_policy",
        view_func=lambda: "ok",
    )
    with app.test_request_context("/", base_url="https://agentefrete.test"):
        active_policy, sent, failed, mode = privacy_policy_service.processar_upload_privacy_policy(
            app,
            upload,
            uploaded_by_user_id=None,
        )

    persisted_dir = tmp_path / "legal" / "privacy_policies"
    assert active_policy is not None
    assert (persisted_dir / active_policy.filename).is_file()
    assert sent == 1
    assert failed == 0
    assert mode == "sync_fallback"
    assert captured["policy_url"] == "https://agentefrete.test/politica-de-privacidade"


def test_terms_route_serve_pdf_do_storage_persistente(monkeypatch, tmp_path):
    web = _load_web_module()
    import app.terms_services as terms_services

    pdf_name = "termo_persistente.pdf"
    terms_dir = tmp_path / "legal" / "terms"
    terms_dir.mkdir(parents=True, exist_ok=True)
    (terms_dir / pdf_name).write_bytes(b"%PDF-1.4 content")

    monkeypatch.setattr(terms_services, "get_active_term", lambda: SimpleNamespace(filename=pdf_name))
    monkeypatch.setattr(terms_services, "get_terms_upload_dir", lambda app=None: str(terms_dir))

    client = web.app.test_client()
    resp = client.get("/termos-de-uso")

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF-1.4")


def test_privacy_policy_route_serve_pdf_do_storage_persistente(monkeypatch, tmp_path):
    web = _load_web_module()
    import app.privacy_policy_services as privacy_services

    pdf_name = "politica_persistente.pdf"
    privacy_dir = tmp_path / "legal" / "privacy_policies"
    privacy_dir.mkdir(parents=True, exist_ok=True)
    (privacy_dir / pdf_name).write_bytes(b"%PDF-1.4 content")

    monkeypatch.setattr(
        privacy_services,
        "get_active_privacy_policy",
        lambda: SimpleNamespace(filename=pdf_name),
    )
    monkeypatch.setattr(privacy_services, "get_privacy_policy_upload_dir", lambda app=None: str(privacy_dir))

    client = web.app.test_client()
    resp = client.get("/politica-de-privacidade")

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF-1.4")


def test_active_documents_ausentes_retorna_none_nos_servicos(app, ctx, monkeypatch, tmp_path):
    import app.privacy_policy_services as privacy_services
    import app.terms_services as terms_services

    _patch_legal_data_dir(monkeypatch, tmp_path)
    db.session.add(TermsOfUse(filename="nao_existe.pdf", is_active=True))
    db.session.add(PrivacyPolicy(filename="nao_existe_privacy.pdf", is_active=True))
    db.session.commit()

    assert terms_services.get_active_term() is None
    assert privacy_services.get_active_privacy_policy() is None


def test_rotas_publicas_retorna_404_quando_documento_ativo_ausente(monkeypatch):
    web = _load_web_module()
    import app.privacy_policy_services as privacy_services
    import app.terms_services as terms_services

    monkeypatch.setattr(terms_services, "get_active_term", lambda: None)
    monkeypatch.setattr(privacy_services, "get_active_privacy_policy", lambda: None)

    client = web.app.test_client()
    terms_resp = client.get("/termos-de-uso")
    privacy_resp = client.get("/politica-de-privacidade")

    assert terms_resp.status_code == 404
    assert privacy_resp.status_code == 404
