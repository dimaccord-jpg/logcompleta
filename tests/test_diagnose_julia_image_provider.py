"""Testes mock-only da sonda scripts/diagnose_julia_image_provider.py (SCRUM-214)."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "diagnose_julia_image_provider.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location(
        "diagnose_julia_image_provider",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def probe():
    return _load_probe()


def _env(values: dict[str, str]):
    def getenv(key: str, default=None):
        return values.get(key, default)

    return getenv


def test_imagen_model_chooses_generate_images(probe):
    assert probe.choose_method("imagen-3.0-generate-002") == "generate_images"
    assert probe.modelo_imagem_usa_generate_images("imagen-4.0-generate-001") is True


def test_multimodal_model_chooses_generate_content(probe):
    assert probe.choose_method("gemini-2.0-flash-preview-image-generation") == "generate_content"
    assert probe.choose_method("gemini-3.1-flash-image") == "generate_content"
    assert probe.modelo_imagem_usa_generate_images("gemini-2.0-flash") is False
    assert probe.modelo_imagem_usa_generate_images("gemini-3.1-flash-image") is False


def test_default_model_is_gemini_flash_image(probe):
    assert probe.resolve_image_model(getenv=lambda *_a, **_k: None) == "gemini-3.1-flash-image"


def test_missing_api_key_does_not_call_provider(probe):
    calls = {"factory": 0, "invoke": 0}

    def factory(_key):
        calls["factory"] += 1
        raise AssertionError("client_factory não deve ser chamado sem chave")

    def invoker(*_a, **_k):
        calls["invoke"] += 1
        raise AssertionError("invoker não deve ser chamado sem chave")

    out = probe.run_probe(
        getenv=_env(
            {
                "APP_ENV": "dev",
                "GEMINI_MODEL_IMAGE": "imagen-3.0-generate-002",
            }
        ),
        load_env=lambda: True,
        client_factory=factory,
        invoker=invoker,
    )
    assert calls["factory"] == 0
    assert calls["invoke"] == 0
    assert "method=generate_images" in out
    assert "exception=RuntimeError" in out


def test_success_makes_exactly_one_call(probe):
    models = MagicMock()
    client = SimpleNamespace(models=models)
    models.generate_images.return_value = SimpleNamespace(generated_images=[])

    out = probe.run_probe(
        getenv=_env(
            {
                "APP_ENV": "dev",
                "GEMINI_API_KEY": "secret-key-value-should-not-leak",
                "GEMINI_MODEL_IMAGE": "imagen-3.0-generate-002",
            }
        ),
        load_env=lambda: True,
        client_factory=lambda _k: client,
        invoker=probe.invoke_provider_once,
    )
    models.generate_images.assert_called_once()
    models.generate_content.assert_not_called()
    assert "exception=None" in out
    assert "method=generate_images" in out


def test_exception_makes_exactly_one_call(probe):
    class ProviderBoom(Exception):
        code = 429

    models = MagicMock()
    client = SimpleNamespace(models=models)
    models.generate_content.side_effect = ProviderBoom("raw provider message with key=sk_live_xxx")

    out = probe.run_probe(
        getenv=_env(
            {
                "APP_ENV": "dev",
                "GEMINI_API_KEY_1": "another-secret",
                "GEMINI_MODEL_IMAGE": "gemini-2.0-flash-preview-image-generation",
            }
        ),
        load_env=lambda: True,
        client_factory=lambda _k: client,
        invoker=probe.invoke_provider_once,
    )
    models.generate_content.assert_called_once()
    models.generate_images.assert_not_called()
    assert "exception=ProviderBoom" in out
    assert "http_status=429" in out
    assert "raw provider message" not in out
    assert "sk_live" not in out
    assert "another-secret" not in out


def test_output_excludes_exception_message_and_secrets(probe):
    class LeakyError(Exception):
        def __init__(self):
            super().__init__(
                "Authorization: Basic abc123 payload={'prompt':'x'} headers={'x-api-key':'k'}"
            )

    invoker = MagicMock(side_effect=LeakyError())
    out = probe.run_probe(
        getenv=_env(
            {
                "APP_ENV": "homolog",
                "GEMINI_API_KEY_2": "super-secret-api-key",
                "GEMINI_MODEL_IMAGE": "imagen-3.0-generate-002",
            }
        ),
        load_env=lambda: True,
        client_factory=lambda _k: object(),
        invoker=invoker,
    )
    invoker.assert_called_once()
    assert out == (
        "provider=gemini\n"
        "model=imagen-3.0-generate-002\n"
        "method=generate_images\n"
        "exception=LeakyError\n"
        "http_status="
    )
    assert "Authorization" not in out
    assert "Basic" not in out
    assert "payload" not in out
    assert "headers" not in out
    assert "super-secret-api-key" not in out
    assert "abc123" not in out


def test_probe_does_not_write_image_file(probe, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = {p.name for p in tmp_path.iterdir()}
    models = MagicMock()
    client = SimpleNamespace(models=models)
    models.generate_images.return_value = SimpleNamespace(
        generated_images=[SimpleNamespace(image=SimpleNamespace(image_bytes=b"PNGDATA"))]
    )
    probe.run_probe(
        getenv=_env(
            {
                "APP_ENV": "dev",
                "GEMINI_API_KEY": "k",
                "GEMINI_MODEL_IMAGE": "imagen-3.0-generate-002",
            }
        ),
        load_env=lambda: True,
        client_factory=lambda _k: client,
        invoker=probe.invoke_provider_once,
    )
    after = {p.name for p in tmp_path.iterdir()}
    assert after == before
    assert not list(tmp_path.rglob("*.png"))
    assert not list(tmp_path.rglob("*.jpg"))


def test_probe_does_not_publish_content(probe, monkeypatch):
    published = []

    def fake_publish(*_a, **_k):
        published.append(True)
        raise AssertionError("não deve publicar")

    monkeypatch.setattr(
        "app.run_julia_agente_pipeline",
        SimpleNamespace(publicar=fake_publish),
        raising=False,
    )
    models = MagicMock()
    client = SimpleNamespace(models=models)
    models.generate_images.return_value = object()
    probe.run_probe(
        getenv=_env(
            {
                "APP_ENV": "dev",
                "GEMINI_API_KEY": "k",
                "GEMINI_MODEL_IMAGE": "imagen-3.0-generate-002",
            }
        ),
        load_env=lambda: True,
        client_factory=lambda _k: client,
        invoker=probe.invoke_provider_once,
    )
    assert published == []


def test_requires_explicit_app_env(probe):
    with pytest.raises(SystemExit):
        probe.run_probe(
            getenv=_env({"GEMINI_API_KEY": "k"}),
            load_env=lambda: True,
            client_factory=lambda _k: object(),
            invoker=lambda *_a, **_k: None,
        )


def test_script_not_imported_by_app(probe):
    app_dir = REPO_ROOT / "app"
    offenders = []
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "diagnose_julia_image_provider" in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []
