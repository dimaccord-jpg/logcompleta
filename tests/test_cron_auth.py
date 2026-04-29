import importlib
import json
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
    monkeypatch.setattr(
        web,
        "settings",
        SimpleNamespace(
            cron_secret="cron-secret-test",
            indices_file_path=str(data_dir / "indices.json"),
        ),
    )
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

    fake_finance = types.ModuleType("app.finance")
    fake_finance.executar_coleta_financeira = lambda bypass_frequencia=False: {
        "status_global": "sucesso",
        "motivo": "executou",
        "executou": True,
        "mensagem": "ok",
        "arquivo_destino": str(tmp_path / "web_data" / "indices.json"),
        "data_referencia": "2026-04-29",
    }
    monkeypatch.setitem(sys.modules, "app.finance", fake_finance)

    fake_consumo = types.ModuleType("app.consumo_identidade")
    fake_consumo.apply_consumo_identidade_before_request = lambda: None
    fake_consumo.ensure_consumo_identidade_no_app_context = lambda: None
    monkeypatch.setitem(sys.modules, "app.consumo_identidade", fake_consumo)

    return web.app.test_client()


@pytest.mark.parametrize("path", ["/cron/executar-cleiton", "/cron/finance", "/cron/billing-snapshot"])
def test_cron_sem_header_retorna_403(cron_client, path):
    response = cron_client.post(path)
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/cron/executar-cleiton", "/cron/finance", "/cron/billing-snapshot"])
def test_cron_com_header_invalido_retorna_403(cron_client, path):
    response = cron_client.post(path, headers={"X-Cron-Secret": "invalido"})
    assert response.status_code == 403


@pytest.mark.parametrize("path", ["/cron/executar-cleiton", "/cron/finance", "/cron/billing-snapshot"])
def test_cron_com_header_valido_autentica(cron_client, path):
    response = cron_client.post(path, headers={"X-Cron-Secret": "cron-secret-test"})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


@pytest.mark.parametrize("path", ["/cron/executar-cleiton", "/cron/finance", "/cron/billing-snapshot"])
def test_cron_query_string_temporaria_ainda_autentica(cron_client, path):
    response = cron_client.post(f"{path}?secret=cron-secret-test")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_cron_executar_cleiton_mantem_fluxo_oficial(monkeypatch, tmp_path):
    web = _load_web(monkeypatch, tmp_path)
    chamadas = {"monetizacao": 0, "bypass": None}

    monkeypatch.setattr(
        web,
        "efetivar_mudancas_pendentes_ciclo",
        lambda: chamadas.__setitem__("monetizacao", chamadas["monetizacao"] + 1) or {"status": "noop"},
    )

    fake_run_cleiton = types.ModuleType("app.run_cleiton")

    def _executar(_app, bypass_frequencia=False):
        chamadas["bypass"] = bypass_frequencia
        return {"status": "sucesso", "mission_id": "mission-123"}

    fake_run_cleiton.executar_orquestracao = _executar
    monkeypatch.setitem(sys.modules, "app.run_cleiton", fake_run_cleiton)

    fake_consumo = types.ModuleType("app.consumo_identidade")
    fake_consumo.apply_consumo_identidade_before_request = lambda: None
    fake_consumo.ensure_consumo_identidade_no_app_context = lambda: None
    monkeypatch.setitem(sys.modules, "app.consumo_identidade", fake_consumo)

    client = web.app.test_client()
    response = client.post(
        "/cron/executar-cleiton",
        headers={"X-Cron-Secret": "cron-secret-test"},
    )

    assert response.status_code == 200
    assert chamadas["monetizacao"] == 1
    assert chamadas["bypass"] is False


def test_cron_finance_reutiliza_fluxo_financeiro(monkeypatch, tmp_path):
    web = _load_web(monkeypatch, tmp_path)

    fake_consumo = types.ModuleType("app.consumo_identidade")
    fake_consumo.apply_consumo_identidade_before_request = lambda: None
    fake_consumo.ensure_consumo_identidade_no_app_context = lambda: None
    monkeypatch.setitem(sys.modules, "app.consumo_identidade", fake_consumo)

    chamadas = {"bypass": None}
    fake_finance = types.ModuleType("app.finance")

    def _executar(bypass_frequencia=False):
        chamadas["bypass"] = bypass_frequencia
        return {
            "status_global": "sucesso_parcial",
            "motivo": "executou",
            "executou": True,
            "mensagem": "ok",
            "arquivo_destino": str(tmp_path / "web_data" / "indices.json"),
            "data_referencia": "2026-04-29",
        }

    fake_finance.executar_coleta_financeira = _executar
    monkeypatch.setitem(sys.modules, "app.finance", fake_finance)

    client = web.app.test_client()
    response = client.post(
        "/cron/finance",
        headers={"X-Cron-Secret": "cron-secret-test"},
    )

    assert response.status_code == 200
    assert chamadas["bypass"] is False
    payload = response.get_json()
    assert payload["ok"] is True
    assert payload["status"] == "sucesso_parcial"


def test_fretes_e_home_compartilham_mesma_fonte_de_indices(monkeypatch, tmp_path):
    web = _load_web(monkeypatch, tmp_path)
    indices_path = tmp_path / "web_data" / "indices.json"
    indices_path.write_text(
        json.dumps(
            {
                "ultima_atualizacao": "2026-04-29",
                "historico": [
                    {"data": "2026-04-29", "dolar": 5.67, "petroleo": 70.12, "bdi": "2100.00", "fbx": "1400.00"}
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = web._load_indices_payload()
    indicadores = web._load_home_indicadores()

    assert payload["historico"][-1]["dolar"] == 5.67
    assert indicadores["dolar"] == 5.67
    assert indicadores["bdi"] == "2100.00"
