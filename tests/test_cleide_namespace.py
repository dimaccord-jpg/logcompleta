from app.cleide_upload_store import get_cleide_upload_tmp_dir
from app.cleide_contracts import (
    SESSION_KEY_CLEIDE_UPLOAD_IN_PROGRESS,
    SESSION_KEY_CLEIDE_UPLOAD_REF,
    is_cleide_upload_in_progress,
    mark_cleide_upload_in_progress,
    clear_cleide_upload_in_progress,
    get_cleide_upload_ref,
)
from app.services.cleide_config_service import _CFG_PREFIX, get_cleide_config


def test_cleide_storage_usa_namespace_proprio():
    path = get_cleide_upload_tmp_dir()
    normalized = path.replace("\\", "/")
    assert normalized.endswith("/cleide_upload_tmp")
    assert "roberto_upload_tmp" not in normalized


def test_cleide_config_usa_prefixo_proprio():
    assert _CFG_PREFIX == "cleide_cfg_"


def test_cleide_config_defaults_basicos_em_request_context():
    from app.web import app

    with app.test_request_context("/auditoria-frete"):
        from flask import g
        g.cleide_allow_config_fallback = True
        cfg = get_cleide_config()
        assert cfg.upload_total_max >= 100
        assert cfg.upload_ttl_minutes >= 5
        assert cfg.chat_max_history >= 1


def test_cleide_upload_ref_contract():
    assert SESSION_KEY_CLEIDE_UPLOAD_REF == "cleide_upload_ref"
    assert get_cleide_upload_ref({SESSION_KEY_CLEIDE_UPLOAD_REF: " abc123 "}) == "abc123"
    assert get_cleide_upload_ref({SESSION_KEY_CLEIDE_UPLOAD_REF: "   "}) is None


def test_cleide_upload_progress_contract():
    session_obj = {}
    assert SESSION_KEY_CLEIDE_UPLOAD_IN_PROGRESS == "cleide_upload_in_progress"
    assert is_cleide_upload_in_progress(session_obj) is False
    mark_cleide_upload_in_progress(session_obj)
    assert is_cleide_upload_in_progress(session_obj) is True
    clear_cleide_upload_in_progress(session_obj)
    assert is_cleide_upload_in_progress(session_obj) is False
