import importlib
import os
from pathlib import Path
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def test_login_renderiza_link_publico_de_termos(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(
        web,
        "get_active_term",
        lambda: SimpleNamespace(filename="termo_de_aceite_revisado_20260314_152251.pdf"),
    )

    client = web.app.test_client()
    resp = client.get("/login")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'href="/termos-de-uso"' in html
    assert "Termos de Uso</a>." in html


def test_terms_of_use_entrega_pdf_ativo(monkeypatch, tmp_path):
    web = _load_web_module()
    import app.terms_services as terms_services

    pdf_name = "termo.pdf"
    pdf_path = Path(tmp_path) / pdf_name
    pdf_path.write_bytes(b"%PDF-1.4 test")

    monkeypatch.setattr(
        terms_services,
        "get_active_term",
        lambda: SimpleNamespace(filename=pdf_name),
    )
    monkeypatch.setattr(terms_services, "get_terms_upload_dir", lambda app=None: str(tmp_path))

    client = web.app.test_client()
    resp = client.get("/termos-de-uso")

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data.startswith(b"%PDF-1.4")
