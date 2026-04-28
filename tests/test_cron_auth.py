import importlib
import sys
import types
from types import SimpleNamespace

import pytest


def _load_web(monkeypatch, tmp_path):
    env_loader = importlib.import_module("app.env_loader")
    data_dir = tmp_path / "web_data"
    data_dir.mkdir(exist_ok=True)

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("CRON_SECRET", "cron-secret-test")

    monkeypatch.setattr(env_loader, "load_app_env", lambda: True)
    monkeypatch.setattr(env_loader, "validate_runtime_env", lambda: None)
    monkeypatch.setattr(env_loader, "resolve_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(env_loader, "resolve_indices_file_path", lambda: str(data_dir / "indices.json"))

    sys.modules.pop("app.settings", None)
    sys.modules.pop("app.web", None)
    web = importlib.import_module("app.web")
    monkeypatch.setattr(web, "settings", SimpleNamespace(cron_secret="cron-secret-test"))
    return web


@pytest.fixture
def cron_client(monkeypatch, tmp_path):
    web = _load_web(monkeypatch, tmp_path)
    monkeypatch.setattr(web, "efetivar_mudancas_pendentes_ciclo", lambda: {"status": "noop"})

    fake_run_cleiton = types.ModuleType("app.run_cleiton")
    fake_run_cleiton.executar_orquestracao = (
        lambda _app, bypass_frequencia=False: {"status": "sucesso", "mission_id": "mission-123"}
    )
    monkeypatch.setitem(sys.modules, "app.run_cleiton", fake_run_cleiton)

    fake_billing = types.ModuleType("app.services.billing_bigquery_service")
    fake_billing.collect_and_persist_billing_snapshot = lambda: None
    monkeypatch.setitem(sys.modules, "app.services.billing_bigquery_service", fake_billing)

    fake_consumo = types.ModuleType("app.consumo_identidade")
    fake_consumo.apply_consumo_identidade_before_request = lambda: None
    fake_consumo.ensure_consumo_identidade_no_app_context = lambda: None
    monkeypatch.setitem(sys.modules, "app.consumo_identidade", fake_consumo)

    return web.app.test_client()


@pytest.mark.parametrize("path", ["/cron/executar-cleiton", "/cron/billing-snapshot"])
def test_cron_sem_header_retorna_403(cron_client, path):
    response = cron_client.post(path)
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/cron/executar-cleiton", "/cron/billing-snapshot"])
def test_cron_com_header_invalido_retorna_403(cron_client, path):
    response = cron_client.post(path, headers={"X-Cron-Secret": "invalido"})
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/cron/executar-cleiton", "/cron/billing-snapshot"])
def test_cron_com_header_valido_autentica(cron_client, path):
    response = cron_client.post(path, headers={"X-Cron-Secret": "cron-secret-test"})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


@pytest.mark.parametrize("path", ["/cron/executar-cleiton", "/cron/billing-snapshot"])
def test_cron_query_string_temporaria_ainda_autentica(cron_client, path):
    response = cron_client.post(f"{path}?secret=cron-secret-test")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
