"""SCRUM-214 — modelo de imagem da Júlia: default + roteamento generate_content."""
from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_model_image_is_gemini_flash_image(monkeypatch):
    from app.run_julia_agente_imagem import _get_model_image

    monkeypatch.delenv("GEMINI_MODEL_IMAGE", raising=False)
    assert _get_model_image() == "gemini-3.1-flash-image"


def test_new_model_uses_generate_content_not_generate_images():
    from app.run_julia_agente_imagem import _modelo_imagem_usa_generate_images

    assert _modelo_imagem_usa_generate_images("gemini-3.1-flash-image") is False


def test_imagen_prefix_still_uses_generate_images():
    from app.run_julia_agente_imagem import _modelo_imagem_usa_generate_images

    assert _modelo_imagem_usa_generate_images("imagen-3.0-generate-002") is True
    assert _modelo_imagem_usa_generate_images("imagen-4.0-generate-001") is True


def test_new_model_selects_multimodal_path(monkeypatch):
    from app.run_julia_agente_imagem import _gerar_via_gemini

    monkeypatch.setenv("GEMINI_API_KEY_2", "secret")
    monkeypatch.setenv("GEMINI_MODEL_IMAGE", "gemini-3.1-flash-image")
    called = {"imagen": 0, "multi": 0}

    def _imagen(*_args, **_kwargs):
        called["imagen"] += 1
        return None

    def _multi(*_args, **kwargs):
        called["multi"] += 1
        assert kwargs.get("model_override") == "gemini-3.1-flash-image"
        return "/media/generated/principal.png"

    monkeypatch.setattr("app.run_julia_agente_imagem._gerar_via_gemini_imagen", _imagen)
    monkeypatch.setattr("app.run_julia_agente_imagem._gerar_via_gemini_multimodal", _multi)

    out = _gerar_via_gemini("porto logistico")
    assert out == "/media/generated/principal.png"
    assert called["imagen"] == 0
    assert called["multi"] == 1


def test_env_dev_does_not_select_obsolete_imagen():
    path = REPO_ROOT / "app" / ".env.dev"
    if not path.is_file():
        pytest.skip("app/.env.dev não versionado neste checkout")
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert "GEMINI_MODEL_IMAGE=imagen-3.0-generate-002" not in text
    assert "GEMINI_MODEL_IMAGE=gemini-3.1-flash-image" in text


def test_env_homolog_does_not_select_preview_model():
    path = REPO_ROOT / "app" / ".env.homolog"
    if not path.is_file():
        pytest.skip("app/.env.homolog não versionado neste checkout")
    text = path.read_text(encoding="utf-8", errors="ignore")
    assert "gemini-3.1-flash-image-preview" not in text
    assert "GEMINI_MODEL_IMAGE=gemini-3.1-flash-image" in text


def test_env_example_documents_new_image_model():
    text = (REPO_ROOT / "app" / ".env.example").read_text(encoding="utf-8")
    assert "GEMINI_MODEL_IMAGE=gemini-3.1-flash-image" in text
    assert "GEMINI_MODEL_IMAGE=imagen-3.0-generate-002" not in text


def test_diagnose_probe_default_matches_pipeline(monkeypatch):
    import importlib.util

    script = REPO_ROOT / "scripts" / "diagnose_julia_image_provider.py"
    spec = importlib.util.spec_from_file_location("diagnose_julia_image_provider", script)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.delenv("GEMINI_MODEL_IMAGE", raising=False)
    assert mod.resolve_image_model() == "gemini-3.1-flash-image"
    assert mod.choose_method("gemini-3.1-flash-image") == "generate_content"
