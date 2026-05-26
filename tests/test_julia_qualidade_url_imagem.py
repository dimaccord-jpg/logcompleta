from app.run_julia_agente_imagem import gerar_imagem_publicavel
from app.run_julia_agente_publicacao import publicar
from app.run_julia_agente_qualidade import _url_imagem_integra, validar_conteudo


def test_url_media_generated_eh_integra():
    assert _url_imagem_integra("/media/generated/julia_x.png") is True


def test_url_static_continua_integra():
    assert _url_imagem_integra("/static/img/logo.png") is True


def test_url_https_continua_integra():
    assert _url_imagem_integra("https://dominio.com/img.png") is True


def test_url_path_traversal_continua_invalida():
    assert _url_imagem_integra("../../etc/passwd") is False


def test_pipeline_fluxo_gerar_validar_publicar_com_media_generated(app, monkeypatch):
    with app.app_context():
        monkeypatch.setattr(
            "app.run_julia_agente_imagem._gerar_via_gemini",
            lambda _prompt: "/media/generated/julia_test.png",
        )
        monkeypatch.delenv("IMAGE_PROVIDER", raising=False)

        imagem = gerar_imagem_publicavel("Tema logístico específico de portos")
        assert imagem["url"].startswith("/media/generated/")

        conteudo = {
            "titulo_julia": "Titulo de noticia logistica valido",
            "resumo_julia": "Linha 1 com contexto logístico relevante.\nLinha 2 com impacto operacional claro.\nLinha 3 com recomendação objetiva.",
            "link": "https://example.com/noticia-validacao-media",
            "url_imagem": imagem["url"],
        }
        ok, erros = validar_conteudo(conteudo, "noticia")
        assert ok is True
        assert erros == []

        noticia = publicar(
            tipo="noticia",
            titulo_julia=conteudo["titulo_julia"],
            link=conteudo["link"],
            fonte="Fonte Teste",
            resumo_julia=conteudo["resumo_julia"],
            url_imagem=conteudo["url_imagem"],
            status_publicacao="pendente",
        )
        assert noticia is not None
        assert noticia.url_imagem.startswith("/media/generated/")
