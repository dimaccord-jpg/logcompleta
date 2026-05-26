import importlib
import os
import sys
from datetime import datetime, timezone


def _load_web_module_with_data_dir(data_dir: str):
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    os.environ["APP_DATA_DIR"] = data_dir
    sys.modules.pop("app.settings", None)
    sys.modules.pop("app.web", None)
    return importlib.import_module("app.web")


def test_noticia_renderiza_imagem_media_generated_quando_url_existe(tmp_path, monkeypatch):
    web = _load_web_module_with_data_dir(str(tmp_path))
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    (generated_dir / "capa.png").write_bytes(b"fakepng")

    class _FakeNoticia:
        id = 77
        tipo = "noticia"
        titulo_julia = "Título"
        titulo_original = "Original"
        link = "https://example.com/noticia-media"
        fonte = "Fonte"
        resumo_julia = "Resumo"
        conteudo_completo = "<p>Conteúdo</p>"
        subtitulo = None
        referencias = None
        cta = None
        objetivo_lead = None
        data_publicacao = None
        publicado_em = datetime.now(timezone.utc).replace(tzinfo=None)
        status_publicacao = "publicado"
        url_imagem = "/media/generated/capa.png"

    class _FakeQuery:
        def get_or_404(self, _id):
            return _FakeNoticia()

    monkeypatch.setattr(web, "NoticiaPortal", type("N", (), {"query": _FakeQuery()}))
    client = web.app.test_client()
    resp = client.get("/noticia/77")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/media/generated/capa.png" in html


def test_noticia_renderiza_fallback_sem_persistir_quando_arquivo_publicado_sumiu(tmp_path, monkeypatch):
    web = _load_web_module_with_data_dir(str(tmp_path))

    class _FakeNoticia:
        id = 88
        tipo = "noticia"
        titulo_julia = "Titulo"
        titulo_original = "Original"
        link = "https://example.com/noticia-missing-media"
        fonte = "Fonte"
        resumo_julia = "Resumo"
        conteudo_completo = "<p>Conteudo</p>"
        subtitulo = None
        referencias = None
        cta = None
        objetivo_lead = None
        data_publicacao = None
        publicado_em = datetime.now(timezone.utc).replace(tzinfo=None)
        status_publicacao = "publicado"
        url_imagem = "/media/generated/sumiu.png"

    class _FakeQuery:
        def get_or_404(self, _id):
            return _FakeNoticia()

    monkeypatch.setattr(web, "NoticiaPortal", type("N", (), {"query": _FakeQuery()}))
    client = web.app.test_client()
    resp = client.get("/noticia/88")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "/static/img/fallback-capa-v1.svg" in html
    assert _FakeNoticia.url_imagem == "/media/generated/sumiu.png"
