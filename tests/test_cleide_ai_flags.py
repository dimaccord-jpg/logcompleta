from app.cleide_ai_flags import resolve_cleide_ai_flags


def _clear_cleide_flag_env(monkeypatch):
    monkeypatch.delenv("CLEIDE_AI_ENABLED_LOCAL", raising=False)
    monkeypatch.delenv("CLEIDE_AI_ENABLED_HOMOLOG", raising=False)
    monkeypatch.delenv("CLEIDE_AI_ENABLED_PROD", raising=False)
    monkeypatch.delenv("CLEIDE_AI_ENABLED", raising=False)


def _clear_cleide_key_env(monkeypatch):
    for var in (
        "GEMINI_API_KEY_ROBERTO",
        "GEMINI_API_KEY",
        "GEMINI_API_KEY_1",
        "GEMINI_API_KEY_2",
        "CLEIDE_GEMINI_API_KEY",
        "GEMINI_API_KEY_CLEIDE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_flags_default_false_sem_variaveis(monkeypatch):
    _clear_cleide_flag_env(monkeypatch)
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dev")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is False
    assert out.environment == "local"


def test_flags_usa_especifica_do_ambiente(monkeypatch):
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "homolog")
    monkeypatch.setenv("CLEIDE_AI_ENABLED_HOMOLOG", "true")
    monkeypatch.setenv("GEMINI_API_KEY_ROBERTO", "k")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is True
    assert out.selected_flag == "CLEIDE_AI_ENABLED_HOMOLOG"
    assert out.api_key_label == "GEMINI_API_KEY_ROBERTO"


def test_flags_fallback_global(monkeypatch):
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("CLEIDE_AI_ENABLED_PROD", raising=False)
    monkeypatch.setenv("CLEIDE_AI_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is True
    assert out.selected_flag == "CLEIDE_AI_ENABLED"
    assert out.api_key_label == "GEMINI_API_KEY"


def test_flags_valor_invalido_desliga(monkeypatch):
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "talvez")
    monkeypatch.setenv("GEMINI_API_KEY_ROBERTO", "k")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is False
    assert out.reason == "invalid_specific_flag"


def test_flags_com_flag_ativa_sem_chave_retorna_missing_api_key(monkeypatch):
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is False
    assert out.reason == "missing_api_key"
    assert out.api_key_label == ""


def test_flags_flag_ativa_aceita_gemini_api_key_1(monkeypatch):
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    monkeypatch.setenv("GEMINI_API_KEY_1", "k1")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is True
    assert out.api_key_label == "GEMINI_API_KEY_1"


def test_flags_flag_ativa_aceita_gemini_api_key_2(monkeypatch):
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    monkeypatch.setenv("GEMINI_API_KEY_2", "k2")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is True
    assert out.api_key_label == "GEMINI_API_KEY_2"


def test_flags_respeita_ordem_de_prioridade_das_chaves(monkeypatch):
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    monkeypatch.setenv("GEMINI_API_KEY_2", "k2")
    monkeypatch.setenv("GEMINI_API_KEY_1", "k1")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_API_KEY_ROBERTO", "kr")
    monkeypatch.setenv("CLEIDE_GEMINI_API_KEY", "kc")
    monkeypatch.setenv("GEMINI_API_KEY_CLEIDE", "kcc")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is True
    assert out.api_key_label == "GEMINI_API_KEY_ROBERTO"


def test_flags_chaves_legacy_ainda_funcionam_no_final_do_fallback(monkeypatch):
    _clear_cleide_key_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
    monkeypatch.setenv("CLEIDE_GEMINI_API_KEY", "kc")
    out = resolve_cleide_ai_flags()
    assert out.ai_enabled is True
    assert out.api_key_label == "CLEIDE_GEMINI_API_KEY"
