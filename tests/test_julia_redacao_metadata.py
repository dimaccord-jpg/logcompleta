from types import SimpleNamespace

from app.run_julia_agente_redacao import (
    _chamar_modelo,
    gerar_artigo_completo,
    gerar_noticia_curta,
)


def test_artigo_sucesso_adiciona_metadados_redacao(monkeypatch):
    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", lambda _tipo: object())
    monkeypatch.setattr(
        "app.run_julia_agente_redacao._chamar_modelo",
        lambda *args, **kwargs: (
            {
                "titulo_julia": "Artigo valido",
                "subtitulo": "Sub",
                "resumo_julia": "Resumo",
                "conteudo_completo": "<p>Conteudo</p>",
                "cta": "CTA suficiente",
                "objetivo_lead": "contato_comercial",
                "referencias": "Fonte",
            },
            None,
        ),
    )

    out = gerar_artigo_completo("Titulo", "Fonte", "https://example.com/a")
    assert out is not None
    assert out.get("redacao_status") == "sucesso"
    assert out.get("redacao_fallback") is False
    assert out.get("redacao_motivo") is None


def test_artigo_sem_cliente_marca_fallback_com_motivo(monkeypatch):
    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", lambda _tipo: None)

    out = gerar_artigo_completo("Titulo", "Fonte", "https://example.com/a")
    assert out is not None
    assert out.get("redacao_status") == "fallback"
    assert out.get("redacao_fallback") is True
    assert out.get("redacao_motivo") == "gemini_client_unavailable"


def test_artigo_falha_json_invalido_marca_motivo_json_parse_error(monkeypatch):
    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", lambda _tipo: object())
    monkeypatch.setattr(
        "app.run_julia_agente_redacao._chamar_modelo",
        lambda *args, **kwargs: (None, "json_parse_error"),
    )

    out = gerar_artigo_completo("Titulo", "Fonte", "https://example.com/a")
    assert out is not None
    assert out.get("redacao_status") == "fallback"
    assert out.get("redacao_fallback") is True
    assert out.get("redacao_motivo") == "json_parse_error"


def test_noticia_curta_permanece_compativel_com_tupla_de_retorno(monkeypatch):
    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", lambda _tipo: object())
    monkeypatch.setattr(
        "app.run_julia_agente_redacao._chamar_modelo",
        lambda *args, **kwargs: (
            {
                "titulo_julia": "Noticia",
                "resumo_julia": "Linha 1. Linha 2. Linha 3.",
                "prompt_imagem": "Prompt",
            },
            None,
        ),
    )

    out = gerar_noticia_curta("Titulo", "Fonte", "https://example.com/n")
    assert out is not None
    assert out.get("titulo_julia") == "Noticia"
    assert isinstance(out.get("resumo_julia"), str)


def test_chamar_modelo_usa_json_nativo_do_sdk_quando_disponivel(monkeypatch):
    captured = {}

    def _fake_governed(client, **kwargs):
        captured["config"] = kwargs.get("config")
        return SimpleNamespace(
            parsed={
                "titulo_julia": "Artigo estruturado",
                "subtitulo": "Sub",
                "resumo_julia": "R" * 90,
                "conteudo_completo": "<p>a</p><p>b</p><p>c</p><p>d</p>",
                "prompt_imagem": "Prompt",
                "cta": "CTA com tamanho suficiente",
                "objetivo_lead": "contato_comercial",
                "referencias": "Fonte: x",
            },
            text="",
        )

    monkeypatch.setattr(
        "app.run_julia_agente_redacao.cleiton_governed_generate_content",
        _fake_governed,
    )

    data, motivo = _chamar_modelo(object(), "prompt", "artigo")
    assert motivo is None
    assert data is not None
    assert data["titulo_julia"] == "Artigo estruturado"
    assert captured["config"] is not None
    assert captured["config"].response_mime_type == "application/json"


def test_chamar_modelo_aceita_json_com_fence_e_texto_extra(monkeypatch):
    def _fake_governed(client, **kwargs):
        return SimpleNamespace(
            parsed=None,
            text='Resposta abaixo:\n```json\n{"titulo_julia":"Artigo","subtitulo":"Sub","resumo_julia":"'
            + ("R" * 90)
            + '","conteudo_completo":"<p>a</p><p>b</p><p>c</p><p>d</p>","prompt_imagem":"Prompt","cta":"CTA suficiente","objetivo_lead":"contato_comercial","referencias":"Fonte"}\n```\nFim.',
        )

    monkeypatch.setattr(
        "app.run_julia_agente_redacao.cleiton_governed_generate_content",
        _fake_governed,
    )

    data, motivo = _chamar_modelo(object(), "prompt", "artigo")
    assert motivo is None
    assert data is not None
    assert data["referencias"] == "Fonte"


def test_chamar_modelo_classifica_json_invalido(monkeypatch):
    def _fake_governed(client, **kwargs):
        return SimpleNamespace(parsed=None, text='```json\n{"titulo_julia":"Artigo",\n```')

    monkeypatch.setattr(
        "app.run_julia_agente_redacao.cleiton_governed_generate_content",
        _fake_governed,
    )

    data, motivo = _chamar_modelo(object(), "prompt", "artigo")
    assert data is None
    assert motivo in {"json_parse_error", "empty_or_invalid_response"}
