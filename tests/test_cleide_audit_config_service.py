import importlib
import pathlib
from unittest.mock import patch

import pytest
from flask import g

from app.extensions import db
from app.models import ConfigRegras
from app.services import cleide_audit_config_service as svc
from app.services.cleiton_doc_config_service import salvar_cleiton_doc_config

SERVICE_SOURCE_PATH = pathlib.Path("app/services/cleide_audit_config_service.py")


@pytest.mark.parametrize(
    ("value", "default", "expected"),
    [
        (0, True, False),
        ("0", True, False),
        (False, True, False),
        (1, False, True),
        ("1", False, True),
        (True, False, True),
        ("false", True, False),
        ("off", True, False),
        ("", True, False),
        ("true", False, True),
        (None, True, True),
        (None, False, False),
    ],
)
def test_coerce_bool(value, default, expected):
    assert svc._coerce_bool(value, default) is expected


def test_prefixo_proprio_isolado_de_cleide_bi_e_cleiton():
    assert svc._CFG_PREFIX == "cleide_audit_cfg_"
    assert svc._CFG_PREFIX != "cleide_cfg_"
    assert svc._CFG_PREFIX != "cleiton_doc_"


def test_defaults_carregam_corretamente(ctx):
    cfg = svc.get_cleide_audit_config()

    assert cfg.chat_enabled is True
    assert cfg.upload_enabled is True
    assert cfg.chat_max_history == 10
    assert cfg.document_context_max_chars == 24000
    assert cfg.max_documents_considered == 3
    assert cfg.question_max_chars == 4000
    assert cfg.fallback_message == svc.DEFAULT_FALLBACK_MESSAGE
    assert cfg.no_documents_behavior == "allow_guided"
    assert cfg.show_documents_used is True
    assert cfg.no_hallucination_instruction_enabled is True
    assert cfg.audited_file_max_rows == svc.DEFAULT_AUDITED_FILE_MAX_ROWS


def test_gravacao_e_leitura_via_config_regras(ctx):
    saved = svc.salvar_cleide_audit_config(
        {
            "chat_enabled": "0",
            "upload_enabled": "1",
            "chat_max_history": "8",
            "document_context_max_chars": "18000",
            "max_documents_considered": "2",
            "question_max_chars": "3500",
            "fallback_message": "Falha temporária da Cleide Auditoria.",
            "no_documents_behavior": "require_documents",
            "show_documents_used": "0",
            "no_hallucination_instruction_enabled": "1",
            "audited_file_max_rows": "1500",
        }
    )

    assert saved.chat_enabled is False
    assert saved.upload_enabled is True
    assert saved.chat_max_history == 8
    assert saved.document_context_max_chars == 18000
    assert saved.max_documents_considered == 2
    assert saved.question_max_chars == 3500
    assert saved.fallback_message == "Falha temporária da Cleide Auditoria."
    assert saved.no_documents_behavior == "require_documents"
    assert saved.show_documents_used is False
    assert saved.no_hallucination_instruction_enabled is True
    assert saved.audited_file_max_rows == 1500

    rows = ConfigRegras.query.filter(ConfigRegras.chave.like("cleide_audit_cfg_%")).all()
    assert len(rows) == len(svc.DEFAULTS)

    loaded = svc.get_cleide_audit_config()
    assert loaded.chat_enabled is False
    assert loaded.question_max_chars == 3500
    assert loaded.no_documents_behavior == "require_documents"


def test_audited_file_max_rows_fora_da_faixa_rejeitado(ctx):
    with pytest.raises(ValueError, match="audited_file_max_rows"):
        svc.parsear_cleide_audit_config({"audited_file_max_rows": "0"})
    with pytest.raises(ValueError, match="audited_file_max_rows"):
        svc.parsear_cleide_audit_config({"audited_file_max_rows": "50001"})


def test_audited_file_max_rows_chave_isolada(ctx):
    saved = svc.salvar_cleide_audit_config({"audited_file_max_rows": "2500"})
    assert saved.audited_file_max_rows == 2500
    row = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_audited_file_max_rows").first()
    assert row is not None
    assert row.valor_inteiro == 2500


def test_no_documents_behavior_invalido_volta_para_default(ctx):
    cfg = svc.parsear_cleide_audit_config({"no_documents_behavior": "exigir_tudo"})
    assert cfg.no_documents_behavior == "allow_guided"


def test_limites_documentais_respeitam_teto_global_cleiton(ctx):
    salvar_cleiton_doc_config(
        {
            "prompt_context_max_chars": "12000",
            "prompt_max_files_considered": "2",
        }
    )

    saved = svc.salvar_cleide_audit_config(
        {
            "document_context_max_chars": "20000",
            "max_documents_considered": "5",
        }
    )

    assert saved.document_context_max_chars == 12000
    assert saved.max_documents_considered == 2

    loaded = svc.get_cleide_audit_config()
    assert loaded.document_context_max_chars == 12000
    assert loaded.max_documents_considered == 2


def test_booleanos_checkbox_ausente_desliga_flag(ctx):
    svc.salvar_cleide_audit_config({"chat_enabled": "1", "upload_enabled": "1"})

    saved = svc.parsear_cleide_audit_config({})
    assert saved.chat_enabled is True
    assert saved.upload_enabled is True

    saved_off = svc.parsear_cleide_audit_config({"chat_enabled": ""})
    assert saved_off.chat_enabled is False


def test_fallback_message_obrigatoria_nao_vazia(ctx):
    with pytest.raises(ValueError, match="fallback_message"):
        svc.parsear_cleide_audit_config({"fallback_message": "x" * 501})


def test_campos_inteiros_fora_da_faixa_rejeitados(ctx):
    with pytest.raises(ValueError, match="chat_max_history"):
        svc.parsear_cleide_audit_config({"chat_max_history": "0"})
    with pytest.raises(ValueError, match="question_max_chars"):
        svc.parsear_cleide_audit_config({"question_max_chars": "100"})


def test_nenhuma_chave_colide_com_cleide_bi_ou_cleiton(ctx):
    audit_keys = {svc._cfg_key(name) for name in svc.DEFAULTS}
    assert all(key.startswith("cleide_audit_cfg_") for key in audit_keys)
    assert "cleide_cfg_chat_max_history" not in audit_keys
    assert "cleiton_doc_prompt_context_max_chars" not in audit_keys


def test_cache_request_local_primeira_leitura_popula_g(app, ctx):
    with app.test_request_context("/auditoria-frete"):
        assert getattr(g, "_cleide_audit_cfg", None) is None
        cfg = svc.get_cleide_audit_config()
        assert isinstance(g._cleide_audit_cfg, svc.CleideAuditConfig)
        assert g._cleide_audit_cfg is cfg


def test_cache_request_local_segunda_leitura_reutiliza_cache(app, ctx):
    with app.test_request_context("/auditoria-frete"):
        cfg1 = svc.get_cleide_audit_config()
        with patch.object(svc, "_load_cfg_map") as mock_load:
            cfg2 = svc.get_cleide_audit_config()
            mock_load.assert_not_called()
            assert cfg2 is cfg1


def test_salvar_atualiza_cache_no_mesmo_request(app, ctx):
    with app.test_request_context("/auditoria-frete"):
        svc.get_cleide_audit_config()
        saved = svc.salvar_cleide_audit_config({"chat_max_history": "9"})
        assert g._cleide_audit_cfg is saved
        assert g._cleide_audit_cfg.chat_max_history == 9

        with patch.object(svc, "_load_cfg_map") as mock_load:
            loaded = svc.get_cleide_audit_config()
            mock_load.assert_not_called()
            assert loaded is saved
            assert loaded.chat_max_history == 9


def test_cache_nao_persiste_fora_de_flask_g(app, ctx):
    with app.test_request_context("/auditoria-frete"):
        svc.salvar_cleide_audit_config({"chat_max_history": "11"})

    with app.test_request_context("/auditoria-frete"):
        if hasattr(g, "_cleide_audit_cfg"):
            delattr(g, "_cleide_audit_cfg")
        with patch.object(svc, "_load_cfg_map", wraps=svc._load_cfg_map) as mock_load:
            cfg = svc.get_cleide_audit_config()
            mock_load.assert_called_once()
            assert cfg.chat_max_history == 11


def test_cache_nao_usa_variavel_global_no_modulo():
    source = SERVICE_SOURCE_PATH.read_text(encoding="utf-8")
    assert "_cached" not in source
    assert "lru_cache" not in source
    assert "@cache" not in source


def test_isolamento_julia_sem_referencias_estaticas():
    source = SERVICE_SOURCE_PATH.read_text(encoding="utf-8")
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.startswith("from ") or line.startswith("import ")
    ]
    imports_blob = "\n".join(import_lines).lower()

    assert "julia_chat_max_history" not in source
    assert "julia_doc_context" not in source
    assert "run_julia_chat" not in source
    assert "julia_documents" not in source
    assert "julia" not in imports_blob

    generated_keys = [svc._cfg_key(name) for name in svc.DEFAULTS]
    assert all(not key.startswith("julia_") for key in generated_keys)


def test_isolamento_julia_salvar_nao_altera_config_julia(ctx):
    db.session.add(
        ConfigRegras(chave="julia_chat_max_history", valor_inteiro=7, valor_texto=None)
    )
    db.session.commit()

    svc.salvar_cleide_audit_config(
        {
            "chat_max_history": "6",
            "show_documents_used": "0",
            "no_hallucination_instruction_enabled": "0",
        }
    )

    julia_row = ConfigRegras.query.filter_by(chave="julia_chat_max_history").first()
    assert julia_row is not None
    assert julia_row.valor_inteiro == 7

    julia_keys = ConfigRegras.query.filter(ConfigRegras.chave.like("julia_%")).all()
    assert len(julia_keys) == 1
    assert julia_keys[0].chave == "julia_chat_max_history"


def test_isolamento_julia_modulo_nao_importa_services_julia():
    module = importlib.import_module("app.services.cleide_audit_config_service")
    module_path = pathlib.Path(module.__file__).as_posix().lower()
    assert "julia" not in module_path

    source = SERVICE_SOURCE_PATH.read_text(encoding="utf-8")
    assert "from app.julia_doc_context" not in source
    assert "from app.run_julia_chat" not in source


@pytest.mark.parametrize(
    "field",
    ["show_documents_used", "no_hallucination_instruction_enabled"],
)
def test_booleanos_restantes_default_true(ctx, field):
    cfg = svc.get_cleide_audit_config()
    assert getattr(cfg, field) is True
    assert not svc._cfg_key(field).startswith("cleide_cfg_")


@pytest.mark.parametrize(
    "field",
    ["show_documents_used", "no_hallucination_instruction_enabled"],
)
def test_booleanos_restantes_salvar_false_e_true(ctx, field):
    cfg_false = svc.salvar_cleide_audit_config({field: "0"})
    assert getattr(cfg_false, field) is False

    row_false = ConfigRegras.query.filter_by(chave=svc._cfg_key(field)).first()
    assert row_false is not None
    assert row_false.valor_inteiro == 0
    assert row_false.chave.startswith("cleide_audit_cfg_")

    cfg_true = svc.salvar_cleide_audit_config({field: "1"})
    assert getattr(cfg_true, field) is True

    row_true = ConfigRegras.query.filter_by(chave=svc._cfg_key(field)).first()
    assert row_true.valor_inteiro == 1


def test_persistencia_via_config_regras_sem_migration_nova():
    source = SERVICE_SOURCE_PATH.read_text(encoding="utf-8")

    assert "from app.models import ConfigRegras" in source
    assert "ConfigRegras.query" in source
    assert "db.Model" not in source
    assert "alembic" not in source.lower()
    assert "migration" not in source.lower()

    imported_models = [
        line.strip()
        for line in source.splitlines()
        if line.startswith("from app.models import")
    ]
    assert imported_models == ["from app.models import ConfigRegras"]


def test_nenhuma_chave_gerada_usa_prefixo_cleide_cfg():
    generated_keys = [svc._cfg_key(name) for name in svc.DEFAULTS]
    assert all(key.startswith("cleide_audit_cfg_") for key in generated_keys)
    assert all(not key.startswith("cleide_cfg_") for key in generated_keys)


def test_salvar_cleide_auditoria_nao_sobrescreve_cleide_cfg_existente(ctx):
    db.session.add(ConfigRegras(chave="cleide_cfg_chat_max_history", valor_inteiro=22))
    db.session.add(ConfigRegras(chave="cleide_cfg_upload_total_max", valor_inteiro=5000))
    db.session.commit()

    svc.salvar_cleide_audit_config({"chat_max_history": "5", "upload_enabled": "0"})

    bi_history = ConfigRegras.query.filter_by(chave="cleide_cfg_chat_max_history").first()
    bi_upload = ConfigRegras.query.filter_by(chave="cleide_cfg_upload_total_max").first()
    audit_history = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_chat_max_history").first()

    assert bi_history.valor_inteiro == 22
    assert bi_upload.valor_inteiro == 5000
    assert audit_history.valor_inteiro == 5

    cleide_cfg_rows = ConfigRegras.query.filter(ConfigRegras.chave.like("cleide_cfg_%")).all()
    assert {row.chave for row in cleide_cfg_rows} == {
        "cleide_cfg_chat_max_history",
        "cleide_cfg_upload_total_max",
    }
