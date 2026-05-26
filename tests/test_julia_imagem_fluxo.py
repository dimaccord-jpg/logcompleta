import os
from datetime import timedelta

from app.extensions import db
from app.models import NoticiaPortal, utcnow_naive
from app.run_cleiton_agente_retencao import limpar_imagens_antigas
from app.run_julia_agente_imagem import _salvar_imagem_local, gerar_imagem_publicavel


def test_imagem_ia_bytes_salva_em_storage_persistente_com_url_publica(tmp_path, monkeypatch):
    generated_dir = tmp_path / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.run_julia_agente_imagem._dir_imagens_persistente", lambda: generated_dir)

    url = _salvar_imagem_local(b"\x89PNG\r\n\x1a\nbytes")

    assert url is not None
    assert url.startswith("/media/generated/")
    nome = url.split("/media/generated/", 1)[1]
    assert (generated_dir / nome).exists()


def test_gemini_imagem_falha_usa_fallback_estavel_sem_url_remota(monkeypatch):
    monkeypatch.setattr("app.run_julia_agente_imagem._gerar_via_gemini", lambda _prompt: None)
    monkeypatch.setattr("app.run_julia_agente_imagem._stock_fallback_enabled", lambda: False)
    monkeypatch.setattr(
        "app.run_julia_agente_imagem._fallback_asset_local_existe",
        lambda asset: asset == "/static/img/fallback-capa-v1.svg",
    )
    monkeypatch.delenv("IMAGEM_FALLBACK_URL", raising=False)
    monkeypatch.delenv("IMAGE_PROVIDER", raising=False)

    out = gerar_imagem_publicavel("Cenário logístico portuário no Brasil")

    assert out["status"] == "fallback"
    assert out["provider"] == "fallback"
    assert out["url"] == "/static/img/fallback-capa-v1.svg"
    assert "http://" not in out["url"] and "https://" not in out["url"]


def test_retencao_imagem_nao_deixa_noticia_publica_sem_capa(app):
    with app.app_context():
        antiga = NoticiaPortal(
            tipo="noticia",
            titulo_julia="Antiga",
            titulo_original="Antiga",
            link="https://example.com/antiga-retencao",
            fonte="Fonte",
            url_imagem="/media/generated/antiga.png",
            data_publicacao=utcnow_naive() - timedelta(days=365),
            publicado_em=utcnow_naive() - timedelta(days=365),
            status_publicacao="publicado",
        )
        recente = NoticiaPortal(
            tipo="noticia",
            titulo_julia="Recente",
            titulo_original="Recente",
            link="https://example.com/recente-retencao",
            fonte="Fonte",
            url_imagem="/media/generated/recente.png",
            data_publicacao=utcnow_naive(),
            publicado_em=utcnow_naive(),
            status_publicacao="publicado",
        )
        db.session.add(antiga)
        db.session.add(recente)
        db.session.commit()

        total = limpar_imagens_antigas(app)
        antiga_db = db.session.get(NoticiaPortal, antiga.id)
        recente_db = db.session.get(NoticiaPortal, recente.id)

        assert total >= 1
        assert antiga_db.url_imagem == "/media/generated/antiga.png"
        assert recente_db.url_imagem == "/media/generated/recente.png"
