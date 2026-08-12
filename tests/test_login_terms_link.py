import importlib
import os
import re
from pathlib import Path
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _opening_tag(html: str, elem_id: str) -> str:
    match = re.search(
        rf"<[a-zA-Z]+\b[^>]*\bid=\"{re.escape(elem_id)}\"[^>]*>",
        html,
    )
    assert match is not None, f"elemento id={elem_id} não encontrado"
    return match.group(0)


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


def test_login_padrao_abre_tab_entrar(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    client = web.app.test_client()

    resp = client.get("/login")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    tab_login = _opening_tag(html, "tab-login")
    tab_cadastro = _opening_tag(html, "tab-cadastro")
    content_login = _opening_tag(html, "content-login")
    content_cadastro = _opening_tag(html, "content-cadastro")

    assert "active" in tab_login
    assert 'aria-selected="true"' in tab_login
    assert "show active" in content_login
    assert "active" not in tab_cadastro
    assert 'aria-selected="false"' in tab_cadastro
    assert "show active" not in content_cadastro


def test_login_mode_register_abre_tab_criar_conta(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    client = web.app.test_client()

    resp = client.get("/login?mode=register")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    tab_login = _opening_tag(html, "tab-login")
    tab_cadastro = _opening_tag(html, "tab-cadastro")
    content_login = _opening_tag(html, "content-login")
    content_cadastro = _opening_tag(html, "content-cadastro")

    assert "active" in tab_cadastro
    assert 'aria-selected="true"' in tab_cadastro
    assert "show active" in content_cadastro
    assert "active" not in tab_login
    assert 'aria-selected="false"' in tab_login
    assert "show active" not in content_login
    assert 'action="/register"' in html
    assert re.search(r'<form[^>]*method="POST"[^>]*action="/register"', html) or re.search(
        r'<form[^>]*action="/register"[^>]*method="POST"',
        html,
    )


def test_login_mode_invalido_abre_tab_entrar(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    client = web.app.test_client()

    resp = client.get("/login?mode=qualquer-coisa")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    tab_login = _opening_tag(html, "tab-login")
    tab_cadastro = _opening_tag(html, "tab-cadastro")
    content_login = _opening_tag(html, "content-login")

    assert "active" in tab_login
    assert "show active" in content_login
    assert "active" not in tab_cadastro


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


def _assert_criar_conta_ativa(html: str):
    tab_cadastro = _opening_tag(html, "tab-cadastro")
    content_cadastro = _opening_tag(html, "content-cadastro")
    tab_login = _opening_tag(html, "tab-login")
    content_login = _opening_tag(html, "content-login")
    assert "active" in tab_cadastro
    assert "show active" in content_cadastro
    assert "active" not in tab_login
    assert "show active" not in content_login


def test_register_sem_termos_preserva_aba_criar_conta(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    called = {"register_user": False}

    def _register_user_should_not_run(*args, **kwargs):
        called["register_user"] = True
        return None, "não deveria chamar register_user"

    monkeypatch.setattr(web, "register_user", _register_user_should_not_run)
    client = web.app.test_client()

    resp = client.post(
        "/register",
        data={
            "nome": "Usuario Teste",
            "email": "sem.termos@example.com",
            "password": "senha-segura-123",
            "job_role": "analista",
            "usage_purpose": "trabalho",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/login" in location
    assert "mode=register" in location
    assert called["register_user"] is False

    follow = client.get(location, follow_redirects=True)
    assert follow.status_code == 200
    html = follow.get_data(as_text=True)
    _assert_criar_conta_ativa(html)
    assert "É obrigatório aceitar os Termos de Uso para criar sua conta." in html


def test_register_falha_register_user_preserva_aba_criar_conta(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    monkeypatch.setattr(
        web,
        "register_user",
        lambda *args, **kwargs: (None, "Este e-mail já está cadastrado."),
    )
    client = web.app.test_client()

    resp = client.post(
        "/register",
        data={
            "nome": "Usuario Teste",
            "email": "ja.existe@example.com",
            "password": "senha-segura-123",
            "job_role": "analista",
            "usage_purpose": "trabalho",
            "accept_terms": "1",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert "/login" in location
    assert "mode=register" in location

    follow = client.get(location, follow_redirects=True)
    assert follow.status_code == 200
    html = follow.get_data(as_text=True)
    _assert_criar_conta_ativa(html)
    assert "Este e-mail já está cadastrado." in html
    # Falha de cadastro não autentica.
    with client.session_transaction() as sess:
        assert "_user_id" not in sess


def test_login_invalido_continua_aba_entrar(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(web, "get_active_term", lambda: None)
    monkeypatch.setattr(
        web,
        "authenticate_user",
        lambda email, password: (None, "Email ou senha incorretos."),
    )
    client = web.app.test_client()

    resp = client.post(
        "/login",
        data={"email": "nao.existe@example.com", "password": "errada"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    tab_login = _opening_tag(html, "tab-login")
    tab_cadastro = _opening_tag(html, "tab-cadastro")
    content_login = _opening_tag(html, "content-login")
    assert "active" in tab_login
    assert "show active" in content_login
    assert "active" not in tab_cadastro
    assert "Email ou senha incorretos." in html
