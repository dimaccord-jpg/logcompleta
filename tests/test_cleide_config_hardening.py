import importlib
import os

import pytest
from flask import g
from sqlalchemy.exc import OperationalError

import app.services.cleide_config_service as cfg
from app.extensions import db


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _op_err() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("db down"))


class _FakeQueryRaise:
    def filter(self, *_args, **_kwargs):
        raise _op_err()


class _FakeConfigRegrasRaise:
    query = _FakeQueryRaise()
    class chave:
        @staticmethod
        def in_(_keys):
            return []


class _FakeConfigRow:
    def __init__(self, chave=None):
        self.chave = chave
        self.valor_texto = None
        self.valor_inteiro = None
        self.valor_real = None


class _FakeQueryStore:
    def __init__(self):
        self.rows = {}

    def filter(self, *_args, **_kwargs):
        class _Result:
            def all(_self):
                return []

        return _Result()

    def filter_by(self, **kwargs):
        key = kwargs.get("chave")
        row = self.rows.get(key)

        class _Result:
            def first(_self):
                return row

        return _Result()


class _FakeConfigRegrasStore:
    query = _FakeQueryStore()

    def __init__(self, chave=None):
        self.chave = chave
        self.valor_texto = None
        self.valor_inteiro = None
        self.valor_real = None

    class chave:
        @staticmethod
        def in_(_keys):
            return []


def test_config_rethrow_em_runtime_real(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(cfg, "ConfigRegras", _FakeConfigRegrasRaise)
    with web.app.test_request_context("/cleide-bi-frete"):
        with pytest.raises(OperationalError):
            cfg._load_cfg_map()


def test_config_fallback_controlado_em_contexto_explicito(monkeypatch):
    web = _load_web_module()
    monkeypatch.setattr(cfg, "ConfigRegras", _FakeConfigRegrasRaise)
    with web.app.test_request_context("/cleide-bi-frete"):
        g.cleide_allow_config_fallback = True
        loaded_map = cfg._load_cfg_map()
        assert loaded_map == {}
        loaded = cfg.get_cleide_config()
        assert loaded.upload_total_max >= 100


def test_salvar_cleide_config_persiste_campos_novos(app, monkeypatch):
    fake_model = _FakeConfigRegrasStore
    fake_model.query = _FakeQueryStore()
    monkeypatch.setattr(cfg, "ConfigRegras", fake_model)

    staged = {}

    class _Session:
        def __init__(self):
            self._real = db.session

        def add(self, row):
            staged[row.chave] = row
            fake_model.query.rows[row.chave] = row

        def commit(self):
            return None

        def remove(self):
            return None

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(cfg.db, "session", _Session())

    with app.test_request_context("/admin/agentes/cleide"):
        cfg.salvar_cleide_config(
            {
                "upload_total_max": "12345",
                "chat_context_max_items_per_table": "7",
                "chat_context_max_text_len": "90",
                "chat_context_rankings_limit": "5",
                "chat_response_max_chars": "5000",
                "chat_context_include_transportadora": "1",
                "chat_context_include_uf_origem": "0",
                "chat_context_include_uf_destino": "1",
                "chat_context_include_temporal": "0",
                "chat_context_include_paretos": "1",
                "chat_context_mode": "conservador",
                "chat_context_max_chars": "7000",
            }
        )
        loaded = cfg.get_cleide_config()
        assert loaded.upload_total_max == 12345
        assert loaded.chat_context_max_items_per_table == 7
        assert loaded.chat_context_max_text_len == 90
        assert loaded.chat_context_rankings_limit == 5
        assert loaded.chat_context_include_uf_origem == 0
        assert loaded.chat_context_include_temporal == 0
        assert loaded.chat_context_mode == "conservador"
        assert loaded.chat_context_max_chars == 7000
        assert loaded.chat_response_max_chars == 5000
        assert "cleide_cfg_upload_total_max" in staged
