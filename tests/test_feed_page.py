import importlib
import os
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    sys.modules.pop("app.web", None)
    return importlib.import_module("app.web")


def _item(*, item_id, tipo, titulo, hours_ago, fonte="Google Notícias", resumo=None, subtitulo=None):
    return SimpleNamespace(
        id=item_id,
        tipo=tipo,
        titulo_julia=titulo,
        fonte=fonte,
        link=f"https://example.com/fonte-{item_id}",
        resumo_julia=resumo if resumo is not None else (None if tipo == "artigo" else f"Resumo de {titulo}"),
        subtitulo=subtitulo if subtitulo is not None else (f"Subtítulo de {titulo}" if tipo == "artigo" else None),
        data_publicacao=datetime(2026, 8, 19, 12, 0, 0) - timedelta(hours=hours_ago),
    )


def test_ordenar_feed_editorial_mescla_por_data_e_limita_a_cinco():
    web = _load_web_module()
    itens = [
        _item(item_id=1, tipo="noticia", titulo="Insight antigo", hours_ago=10),
        _item(item_id=2, tipo="artigo", titulo="Artigo recente", hours_ago=1),
        _item(item_id=3, tipo="noticia", titulo="Insight meio", hours_ago=3),
        _item(item_id=4, tipo="artigo", titulo="Artigo meio", hours_ago=2),
        _item(item_id=5, tipo="noticia", titulo="Insight extra", hours_ago=4),
        _item(item_id=6, tipo="artigo", titulo="Artigo extra", hours_ago=5),
        _item(item_id=7, tipo="noticia", titulo="Insight mais recente", hours_ago=0),
    ]
    ordered = web._ordenar_feed_editorial(itens)
    assert [item.titulo_julia for item in ordered] == [
        "Insight mais recente",
        "Artigo recente",
        "Artigo meio",
        "Insight meio",
        "Insight extra",
    ]
    assert len(ordered) == 5


def test_feed_renderiza_coluna_unica_mista_com_botoes_por_tipo(monkeypatch):
    web = _load_web_module()
    feed_itens = [
        _item(item_id=10, tipo="noticia", titulo="Insight do dia", hours_ago=0, fonte="Valor Econômico"),
        _item(item_id=11, tipo="artigo", titulo="Artigo da Júlia", hours_ago=2, fonte="Agentefrete"),
        _item(item_id=12, tipo="noticia", titulo="Insight da manhã", hours_ago=5, fonte="Google Notícias"),
    ]
    monkeypatch.setattr(web, "_load_feed_editorial", lambda: feed_itens)
    resp = web.app.test_client().get("/feed")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)

    assert 'class="col-lg-8"' not in html
    assert 'class="col-lg-4"' not in html
    assert "Artigos da Júlia" not in html
    assert "af-hero-home" in html
    assert "Últimas da Logística" in html
    assert "Newsletter Agentefrete" in html
    assert html.count("card-noticia ri-card") == 3
    assert html.count("Fonte Original") == 3
    assert "Ver Insight" in html
    assert "Ver Artigo" in html
    assert "Ver Insight Log Completa" not in html

    pos_insight = html.find("Insight do dia")
    pos_artigo = html.find("Artigo da Júlia")
    pos_manha = html.find("Insight da manhã")
    assert 0 < pos_insight < pos_artigo < pos_manha

    insight_block = html[pos_insight:pos_artigo]
    artigo_block = html[pos_artigo:pos_manha]
    assert "Ver Insight" in insight_block
    assert "Ver Artigo" not in insight_block
    assert "Ver Artigo" in artigo_block
    assert "Valor Econômico" in html
    assert "Google Notícias" in html
    assert "Subtítulo de Artigo da Júlia" in html
