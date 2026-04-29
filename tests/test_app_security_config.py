import importlib
import sys
from pathlib import Path

import pytest


def _load_settings(monkeypatch, tmp_path, app_env: str, secret_key: str | None):
    env_loader = importlib.import_module("app.env_loader")
    data_dir = tmp_path / f"data_{app_env}"
    data_dir.mkdir(exist_ok=True)

    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.delenv("FLASK_DEBUG", raising=False)

    if secret_key is None:
        monkeypatch.delenv("SECRET_KEY", raising=False)
    else:
        monkeypatch.setenv("SECRET_KEY", secret_key)

    monkeypatch.setattr(env_loader, "load_app_env", lambda: True)
    monkeypatch.setattr(env_loader, "validate_runtime_env", lambda: None)
    monkeypatch.setattr(env_loader, "resolve_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(env_loader, "resolve_indices_file_path", lambda: str(data_dir / "indices.json"))

    sys.modules.pop("app.settings", None)
    return importlib.import_module("app.settings")


def test_secret_key_dev_sem_secret_permite_boot(monkeypatch, tmp_path):
    settings_module = _load_settings(monkeypatch, tmp_path, "dev", None)
    assert settings_module.settings.app_env == "dev"
    assert settings_module.settings.secret_key


@pytest.mark.parametrize("app_env", ["homolog", "prod"])
def test_secret_key_ausente_falha_em_homolog_e_prod(monkeypatch, tmp_path, app_env):
    with pytest.raises(RuntimeError):
        _load_settings(monkeypatch, tmp_path, app_env, None)


@pytest.mark.parametrize("app_env", ["homolog", "prod"])
def test_secret_key_valida_permite_boot_em_homolog_e_prod(monkeypatch, tmp_path, app_env):
    settings_module = _load_settings(monkeypatch, tmp_path, app_env, "super-secret-key-1234567890")
    assert settings_module.settings.app_env == app_env
    assert settings_module.settings.secret_key == "super-secret-key-1234567890"


@pytest.mark.parametrize(
    ("app_env", "expected_secure"),
    [
        ("dev", False),
        ("homolog", True),
        ("prod", True),
    ],
)
def test_session_cookie_secure_por_ambiente(monkeypatch, tmp_path, app_env, expected_secure):
    settings_module = _load_settings(monkeypatch, tmp_path, app_env, "super-secret-key-1234567890")
    assert settings_module.settings.session_cookie_secure is expected_secure
