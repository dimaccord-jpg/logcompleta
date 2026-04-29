from datetime import datetime, timedelta, timezone

from app.models import ConfigRegras


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _load_finance(monkeypatch, tmp_path):
    import importlib
    import sys
    from pathlib import Path

    import app.env_loader as env_loader

    data_dir = tmp_path / "finance_data"
    data_dir.mkdir(exist_ok=True)

    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg2://user:pass@localhost:5432/testdb")
    monkeypatch.setenv("APP_DATA_DIR", str(data_dir))
    monkeypatch.setattr(env_loader, "load_app_env", lambda: True)
    monkeypatch.setattr(env_loader, "validate_runtime_env", lambda: None)
    monkeypatch.setattr(env_loader, "resolve_data_dir", lambda: str(data_dir))
    monkeypatch.setattr(env_loader, "resolve_indices_file_path", lambda: str(Path(data_dir) / "indices.json"))

    sys.modules.pop("app.settings", None)
    sys.modules.pop("app.finance", None)
    return importlib.import_module("app.finance")


def test_finance_bootstrap_and_config(app, monkeypatch, tmp_path):
    finance = _load_finance(monkeypatch, tmp_path)

    with app.app_context():
        finance.bootstrap_finance_regras()
        assert finance.obter_finance_frequencia_horas() == 6
        assert finance.obter_finance_frequencia_minutos() == 360

        finance.configurar_finance_frequencia_horas(12)
        assert finance.obter_finance_frequencia_horas() == 12
        assert finance.obter_finance_frequencia_minutos() == 720

        cfg = ConfigRegras.query.filter_by(chave=finance.CHAVE_FINANCE_FREQUENCIA_HORAS).first()
        assert cfg is not None
        assert cfg.valor_inteiro == 12


def test_finance_config_aceita_minutos_menor_que_uma_hora(app, monkeypatch, tmp_path):
    finance = _load_finance(monkeypatch, tmp_path)

    with app.app_context():
        finance.configurar_finance_frequencia_minutos(15)

        assert finance.obter_finance_frequencia_minutos() == 15
        assert finance.obter_finance_frequencia_horas() == 1


def test_atualizar_indices_pula_por_frequencia(app, monkeypatch, tmp_path):
    finance = _load_finance(monkeypatch, tmp_path)

    with app.app_context():
        monkeypatch.setattr(finance, "INDICES_FILE", tmp_path / "indices.json")
        finance.configurar_finance_frequencia_horas(6)
        finance.persistir_finance_ultima_execucao(_utcnow_naive())

        resultado = finance.atualizar_indices()

        assert resultado["status_global"] == "ignorado"
        assert resultado["motivo"] == "pulado_frequencia"
        assert resultado["executou"] is False


def test_finance_respeita_frequencia_menor_que_uma_hora(app, monkeypatch, tmp_path):
    finance = _load_finance(monkeypatch, tmp_path)

    with app.app_context():
        finance.configurar_finance_frequencia_minutos(5)
        ultima = _utcnow_naive() - timedelta(minutes=4)
        assert finance.pode_atualizar_indices_por_frequencia(ultima, agora=_utcnow_naive()) is False

        ultima_liberada = _utcnow_naive() - timedelta(minutes=6)
        assert finance.pode_atualizar_indices_por_frequencia(
            ultima_liberada,
            agora=_utcnow_naive(),
        ) is True


def test_atualizar_indices_executa_quando_fora_da_janela(app, monkeypatch, tmp_path):
    finance = _load_finance(monkeypatch, tmp_path)

    class _Series:
        def dropna(self):
            return self

        @property
        def iloc(self):
            return self

        def __getitem__(self, _index):
            return 5.25

    class _History:
        def __getitem__(self, _key):
            return _Series()

    class _Ticker:
        def history(self, period="5d"):
            return _History()

    with app.app_context():
        monkeypatch.setattr(finance, "INDICES_FILE", tmp_path / "indices.json")
        monkeypatch.setattr(finance.yf, "Ticker", lambda _symbol: _Ticker())
        monkeypatch.setattr(finance, "get_bdi_index", lambda fallback: "2000.00")
        monkeypatch.setattr(finance, "get_fbx_index", lambda fallback: "1500.00")
        finance.configurar_finance_frequencia_horas(1)
        finance.persistir_finance_ultima_execucao(_utcnow_naive() - timedelta(hours=2))

        resultado = finance.atualizar_indices()

        assert resultado["status_global"] in ("sucesso", "sucesso_parcial")
        assert resultado["motivo"] == "executou"
        assert resultado["executou"] is True
        assert (tmp_path / "indices.json").exists()
