import importlib
import os
import sys
from datetime import datetime, timezone
from flask_login import UserMixin
from types import SimpleNamespace

from app.extensions import db
from app.models import NoticiaPortal
from app.news_ai import buscar_noticias_portal


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    sys.modules.pop("app.web", None)
    return importlib.import_module("app.web")


class _AuthUser(UserMixin):
    def __init__(self, user_id: str = "123", is_admin: bool = False):
        self.id = user_id
        self.is_admin = is_admin
        self.conta_id = 1
        self.franquia_id = 1
        self.email = "tester@example.com"
        self.full_name = "Tester User"


def _force_login(client, web, *, is_admin=False):
    user = _AuthUser(is_admin=is_admin)
    setattr(web, "get_user_by_id", lambda _user_id: user)
    with client.session_transaction() as sess:
        sess["_user_id"] = user.get_id()
        sess["_fresh"] = True
    return user


class _FakeQuery:
    def __init__(self, noticia=None, noticias=None):
        self._noticia = noticia
        self._noticias = noticias or []

    def get_or_404(self, _id):
        return self._noticia

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._noticias


class _FakeField:
    def isnot(self, _value):
        return self

    def in_(self, _values):
        return self

    def desc(self):
        return self


def test_noticia_render_admin_mostra_botao_despublicar(monkeypatch):
    web = _load_web_module()
    fake_noticia = SimpleNamespace(
        id=42,
        tipo="artigo",
        titulo_julia="Conteúdo público",
        titulo_original="Original",
        link="https://example.com/editorial-publico",
        fonte="Fonte",
        status_publicacao="publicado",
        publicado_em=datetime.now(timezone.utc).replace(tzinfo=None),
        data_publicacao=datetime.now(timezone.utc).replace(tzinfo=None),
        resumo_julia="Resumo",
        conteudo_completo="<p>Conteúdo</p>",
        subtitulo="Sub",
        referencias="Ref",
        cta="CTA",
        objetivo_lead="contato",
        url_imagem=None,
    )
    monkeypatch.setattr(web, "NoticiaPortal", SimpleNamespace(query=_FakeQuery(noticia=fake_noticia)))
    client = web.app.test_client()
    _force_login(client, web, is_admin=True)
    resp = client.get("/noticia/42")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Despublicar" in html


def test_noticia_render_usuario_nao_admin_nao_mostra_botao(monkeypatch):
    web = _load_web_module()
    fake_noticia = SimpleNamespace(
        id=42,
        tipo="artigo",
        titulo_julia="Conteúdo público",
        titulo_original="Original",
        link="https://example.com/editorial-publico",
        fonte="Fonte",
        status_publicacao="publicado",
        publicado_em=datetime.now(timezone.utc).replace(tzinfo=None),
        data_publicacao=datetime.now(timezone.utc).replace(tzinfo=None),
        resumo_julia="Resumo",
        conteudo_completo="<p>Conteúdo</p>",
        subtitulo="Sub",
        referencias="Ref",
        cta="CTA",
        objetivo_lead="contato",
        url_imagem=None,
    )
    monkeypatch.setattr(web, "NoticiaPortal", SimpleNamespace(query=_FakeQuery(noticia=fake_noticia)))
    client = web.app.test_client()
    _force_login(client, web, is_admin=False)
    resp = client.get("/noticia/42")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Despublicar" not in html


def test_noticia_render_visitante_nao_mostra_botao(monkeypatch):
    web = _load_web_module()
    fake_noticia = SimpleNamespace(
        id=42,
        tipo="artigo",
        titulo_julia="Conteúdo público",
        titulo_original="Original",
        link="https://example.com/editorial-publico",
        fonte="Fonte",
        status_publicacao="publicado",
        publicado_em=datetime.now(timezone.utc).replace(tzinfo=None),
        data_publicacao=datetime.now(timezone.utc).replace(tzinfo=None),
        resumo_julia="Resumo",
        conteudo_completo="<p>Conteúdo</p>",
        subtitulo="Sub",
        referencias="Ref",
        cta="CTA",
        objetivo_lead="contato",
        url_imagem=None,
    )
    monkeypatch.setattr(web, "NoticiaPortal", SimpleNamespace(query=_FakeQuery(noticia=fake_noticia)))
    client = web.app.test_client()
    resp = client.get("/noticia/42")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Despublicar" not in html


def test_sitemap_nao_inclui_despublicado(monkeypatch):
    web = _load_web_module()
    publicado = SimpleNamespace(
        id=100,
        publicado_em=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    fake_model = SimpleNamespace(
        query=_FakeQuery(noticias=[publicado]),
        publicado_em=_FakeField(),
        status_publicacao=_FakeField(),
    )
    monkeypatch.setattr(web, "NoticiaPortal", fake_model)
    client = web.app.test_client()
    resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    xml = resp.get_data(as_text=True)
    assert "/noticia/100" in xml
    assert "despublicado" not in xml


def test_busca_noticias_portal_exclui_despublicado(app):
    with app.app_context():
        from app.models import utcnow_naive

        publicado = NoticiaPortal(
            tipo="noticia",
            titulo_julia="Publicado na busca",
            titulo_original="Original 1",
            link="https://example.com/newsai-publicado",
            fonte="Fonte",
            status_publicacao="publicado",
            publicado_em=utcnow_naive(),
        )
        despublicado = NoticiaPortal(
            tipo="noticia",
            titulo_julia="Despublicado na busca",
            titulo_original="Original 2",
            link="https://example.com/newsai-despublicado",
            fonte="Fonte",
            status_publicacao="despublicado",
            publicado_em=utcnow_naive(),
        )
        db.session.add(publicado)
        db.session.add(despublicado)
        db.session.commit()

        out = buscar_noticias_portal()
        ids = {n.id for n in out}
        assert publicado.id in ids
        assert despublicado.id not in ids


def test_abrir_noticia_20x_nao_altera_estado_publicado(app):
    web = _load_web_module()
    fake_noticia = SimpleNamespace(
        id=420,
        tipo="noticia",
        titulo_julia="Imutavel",
        titulo_original="Original imutavel",
        link="https://example.com/noticia-imutavel",
        fonte="Fonte",
        status_publicacao="publicado",
        publicado_em=datetime.now(timezone.utc).replace(tzinfo=None),
        data_publicacao=datetime.now(timezone.utc).replace(tzinfo=None),
        resumo_julia="Resumo estavel",
        conteudo_completo="<p>Conteudo</p>",
        subtitulo=None,
        referencias=None,
        cta=None,
        objetivo_lead=None,
        url_imagem="/media/generated/imutavel.png",
        url_imagem_master="/media/generated/imutavel.png",
        assets_canais_json='{"imagem_status":"sucesso","imagem_provider":"gemini","imagem_url_final":"/media/generated/imutavel.png"}',
    )
    before = (
        fake_noticia.url_imagem,
        fake_noticia.assets_canais_json,
        fake_noticia.url_imagem_master,
    )
    class _FakeQueryImutavel:
        def get_or_404(self, _id):
            return fake_noticia

    web.NoticiaPortal = SimpleNamespace(query=_FakeQueryImutavel())
    client = web.app.test_client()
    for _ in range(20):
        resp = client.get("/noticia/420")
        assert resp.status_code == 200
    after = (
        fake_noticia.url_imagem,
        fake_noticia.assets_canais_json,
        fake_noticia.url_imagem_master,
    )
    assert before == after
    assert fake_noticia.url_imagem == "/media/generated/imutavel.png"
    assert '"imagem_status":"sucesso"' in fake_noticia.assets_canais_json
    assert '"imagem_provider":"gemini"' in fake_noticia.assets_canais_json
    assert '"imagem_url_final":"/media/generated/imutavel.png"' in fake_noticia.assets_canais_json
