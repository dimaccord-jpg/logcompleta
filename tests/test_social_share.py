import importlib
import inspect
import os
import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from urllib.parse import quote

import pytest


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    sys.modules.pop("app.web", None)
    return importlib.import_module("app.web")


def _fake_noticia(*, tipo: str = "artigo", noticia_id: int = 42):
    return SimpleNamespace(
        id=noticia_id,
        tipo=tipo,
        titulo_julia="Título de Compartilhamento & Logística",
        titulo_original="Original externo",
        link="https://example.com/fonte-externa-nao-usar-no-share",
        fonte="Fonte",
        status_publicacao="publicado",
        publicado_em=datetime.now(timezone.utc).replace(tzinfo=None),
        data_publicacao=datetime.now(timezone.utc).replace(tzinfo=None),
        resumo_julia="Resumo operacional para insight rápido.",
        conteudo_completo="<p>Corpo do artigo estratégico.</p>",
        subtitulo="Subtítulo premium",
        referencias="Ref",
        cta="CTA",
        objetivo_lead="contato",
        url_imagem=None,
    )


class _FakeQuery:
    def __init__(self, noticia):
        self._noticia = noticia

    def get_or_404(self, _id):
        if self._noticia is None:
            from werkzeug.exceptions import NotFound

            raise NotFound()
        return self._noticia


def _share_block(html: str) -> str:
    marker = 'class="social-share'
    start = html.find(marker)
    assert start != -1, "Bloco social-share ausente no HTML"
    end = html.find('<div class="mb-5">', start)
    if end == -1:
        end = html.find('<div class="content af-readable-content', start)
    assert end != -1, "Fim do bloco social-share não encontrado"
    return html[start:end]


def _assert_common_share_contract(html: str, *, share_url_abs: str, share_title: str):
    block = _share_block(html)
    url_encoded = quote(share_url_abs, safe="")
    title_encoded = quote(share_title, safe="")

    assert f"https://www.facebook.com/sharer/sharer.php?u={url_encoded}" in block
    assert f"https://www.linkedin.com/sharing/share-offsite/?url={url_encoded}" in block
    assert f"https://twitter.com/intent/tweet?url={url_encoded}&amp;text={title_encoded}" in block
    assert f"https://www.threads.net/intent/post?text={title_encoded}%20{url_encoded}" in block
    assert f"https://api.whatsapp.com/send?text={title_encoded}%20{url_encoded}" in block

    assert block.count('target="_blank"') >= 5
    assert block.count('rel="noopener noreferrer"') >= 5
    assert 'aria-label="Compartilhar no Facebook"' in block
    assert 'aria-label="Compartilhar no Threads"' in block
    assert 'aria-label="Compartilhar no X"' in block
    assert 'aria-label="Compartilhar no LinkedIn"' in block
    assert 'aria-label="Compartilhar no WhatsApp"' in block
    assert 'title="Compartilhar no WhatsApp"' in block

    assert "https://example.com/fonte-externa-nao-usar-no-share" not in block


def _extract_canonical(html: str) -> str:
    match = re.search(r'<link rel="canonical" href="([^"]+)"', html)
    assert match, "canonical ausente no HTML"
    return match.group(1)


def _extract_og_url(html: str) -> str:
    match = re.search(r'<meta property="og:url" content="([^"]+)"', html)
    assert match, "og:url ausente no HTML"
    return match.group(1)


def _assert_seo_urls_consistentes(html: str, *, share_url_abs: str):
    canonical = _extract_canonical(html)
    og_url = _extract_og_url(html)
    assert canonical == share_url_abs
    assert og_url == share_url_abs
    assert canonical == og_url


@pytest.mark.parametrize("tipo", ["artigo", "noticia"])
def test_producao_canonical_og_url_e_share_url_abs_iguais(tipo):
    web = _load_web_module()
    fake = _fake_noticia(tipo=tipo, noticia_id=42)
    web.NoticiaPortal = SimpleNamespace(query=_FakeQuery(fake))
    html = web.app.test_client().get(f"/noticia/{fake.id}").get_data(as_text=True)
    share_url = "https://www.agentefrete.com.br/noticia/42"
    _assert_seo_urls_consistentes(html, share_url_abs=share_url)


def test_homolog_canonical_og_url_e_share_url_abs_iguais(monkeypatch):
    homolog_base = "https://homolog0514.agentefrete.com.br"
    web = _load_web_module()
    monkeypatch.setattr(web, "_public_base_url", lambda: homolog_base)
    fake = _fake_noticia(tipo="artigo", noticia_id=99)
    web.NoticiaPortal = SimpleNamespace(query=_FakeQuery(fake))
    html = web.app.test_client().get("/noticia/99").get_data(as_text=True)
    share_url = f"{homolog_base}/noticia/99"
    _assert_seo_urls_consistentes(html, share_url_abs=share_url)
    _assert_common_share_contract(html, share_url_abs=share_url, share_title=fake.titulo_julia)


@pytest.mark.parametrize("tipo", ["artigo", "noticia"])
def test_conteudo_publico_renderiza_cinco_links_sociais(tipo):
    web = _load_web_module()
    fake = _fake_noticia(tipo=tipo)
    web.NoticiaPortal = SimpleNamespace(query=_FakeQuery(fake))
    client = web.app.test_client()
    resp = client.get(f"/noticia/{fake.id}")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    share_url = f"https://www.agentefrete.com.br/noticia/{fake.id}"
    _assert_common_share_contract(html, share_url_abs=share_url, share_title=fake.titulo_julia)
    if tipo == "artigo":
        assert "Artigo Estratégico" in html
    else:
        assert "Insight Rápido" in html


def test_share_respeita_public_base_url(monkeypatch):
    homolog_base = "https://homolog0514.agentefrete.com.br"
    web = _load_web_module()
    monkeypatch.setattr(web, "_public_base_url", lambda: homolog_base)
    fake = _fake_noticia(tipo="noticia", noticia_id=99)
    web.NoticiaPortal = SimpleNamespace(query=_FakeQuery(fake))
    client = web.app.test_client()
    resp = client.get("/noticia/99")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    share_url = f"{homolog_base}/noticia/99"
    _assert_common_share_contract(html, share_url_abs=share_url, share_title=fake.titulo_julia)


def test_conteudo_despublicado_continua_inacessivel():
    web = _load_web_module()
    fake = _fake_noticia()
    fake.publicado_em = None
    web.NoticiaPortal = SimpleNamespace(query=_FakeQuery(fake))
    client = web.app.test_client()
    resp = client.get(f"/noticia/{fake.id}")
    assert resp.status_code == 404


def test_render_noticia_nao_aciona_pipeline_nem_consumo_ia(monkeypatch):
    web = _load_web_module()
    fake = _fake_noticia()
    web.NoticiaPortal = SimpleNamespace(query=_FakeQuery(fake))

    pipeline_calls = {"count": 0}

    def _pipeline(*args, **kwargs):
        pipeline_calls["count"] += 1
        return False

    monkeypatch.setattr("app.run_julia_agente_pipeline.executar_pipeline", _pipeline)

    source = inspect.getsource(web.detalhe_noticia)
    assert "executar_pipeline" not in source
    assert "IaConsumoEvento" not in source
    assert "gerar_conteudo" not in source
    assert "gerar_imagem_publicavel" not in source
    assert "cleiton_governed_generate_content" not in source

    client = web.app.test_client()
    resp = client.get(f"/noticia/{fake.id}")
    assert resp.status_code == 200
    assert pipeline_calls["count"] == 0


def test_exemplo_html_dos_cinco_botoes():
    web = _load_web_module()
    fake = _fake_noticia(tipo="artigo", noticia_id=7)
    web.NoticiaPortal = SimpleNamespace(query=_FakeQuery(fake))
    client = web.app.test_client()
    html = client.get("/noticia/7").get_data(as_text=True)
    block = _share_block(html)
    assert "Facebook" in block
    assert "Threads" in block
    assert "bi-twitter-x" in block
    assert "LinkedIn" in block
    assert "WhatsApp" in block
    assert "bi-whatsapp" in block
