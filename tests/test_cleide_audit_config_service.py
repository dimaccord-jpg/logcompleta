import importlib
import inspect
import json
import pathlib
from dataclasses import replace
from unittest.mock import patch

import pytest
from flask import g
from werkzeug.datastructures import MultiDict

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
    assert cfg.audited_file_max_bytes is None
    assert cfg.audited_file_max_rows == svc.DEFAULT_AUDITED_FILE_MAX_ROWS
    assert cfg.calculation_bases == svc.DEFAULT_CALCULATION_BASES
    assert [base["label"] for base in cfg.calculation_bases] == [
        "% por nota fiscal",
        "por CTe",
        "por conhecimento",
        "por documento",
        "por kg",
        "por fração de 100kg",
    ]


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
            "audited_file_max_bytes": "1048576",
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
    assert saved.audited_file_max_bytes == 1048576
    assert saved.audited_file_max_rows == 1500

    rows = ConfigRegras.query.filter(ConfigRegras.chave.like("cleide_audit_cfg_%")).all()
    keys = {row.chave for row in rows}
    assert keys == {svc._cfg_key(name) for name in svc.GENERAL_FORM_CONFIG_FIELDS}
    assert "cleide_audit_cfg_calculation_bases" not in keys

    loaded = svc.get_cleide_audit_config()
    assert loaded.chat_enabled is False
    assert loaded.question_max_chars == 3500
    assert loaded.no_documents_behavior == "require_documents"
    assert loaded.audited_file_max_bytes == 1048576
    assert loaded.calculation_bases == svc.DEFAULT_CALCULATION_BASES


def test_salvar_calculation_bases_validas(ctx):
    bases = [
        svc.DEFAULT_CALCULATION_BASES[0],
        svc.DEFAULT_CALCULATION_BASES[1],
        svc.DEFAULT_CALCULATION_BASES[5],
    ]

    saved = svc.salvar_cleide_audit_calculation_bases(bases)

    assert [base["label"] for base in saved] == [
        "% por nota fiscal",
        "por CTe",
        "por fração de 100kg",
    ]
    row = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_calculation_bases").first()
    assert row is not None
    assert row.valor_texto
    persisted = json.loads(row.valor_texto)
    assert persisted[0]["id"] == "pct_nota_fiscal"
    assert persisted[2]["parameters"] == {"fraction_size": 100}
    assert svc.carregar_cleide_audit_calculation_bases() == saved


def test_parsear_calculation_bases_form_aliases_fracao_e_resolvedor(ctx):
    form = MultiDict(
        [
            ("calculation_base_row_index", "0"),
            ("calculation_base_id_0", "pct_nota_fiscal"),
            ("calculation_base_label_0", "% por nota fiscal"),
            ("calculation_base_unit_0", "%"),
            ("calculation_base_calculation_type_0", "invoice_percentage"),
            ("calculation_base_operation_0", "percentage_of_variable"),
            ("calculation_base_audit_variable_0", "valor_nf"),
            ("calculation_base_aliases_0", "valor da nf; sobre nf, nota fiscal"),
            ("calculation_base_is_active_0", "1"),
            ("calculation_base_allows_minimum_0", "1"),
            ("calculation_base_allows_maximum_0", "1"),
            ("calculation_base_display_order_0", "10"),
            ("calculation_base_row_index", "1"),
            ("calculation_base_id_1", "fracao_100kg"),
            ("calculation_base_label_1", "por fração de 100kg"),
            ("calculation_base_unit_1", "R$"),
            ("calculation_base_calculation_type_1", "weight_fraction"),
            ("calculation_base_operation_1", "ceil_fraction"),
            ("calculation_base_audit_variable_1", "peso"),
            ("calculation_base_aliases_1", "100kg ou fração"),
            ("calculation_base_fraction_size_1", "100"),
            ("calculation_base_is_active_1", "1"),
            ("calculation_base_display_order_1", "20"),
        ]
    )

    bases = svc.parsear_calculation_bases_form(form)
    saved = svc.salvar_cleide_audit_calculation_bases(bases)

    assert saved[0]["aliases"] == ["valor da nf", "sobre nf", "nota fiscal"]
    assert saved[1]["parameters"] == {"fraction_size": 100}
    assert svc.resolve_calculation_base("sobre nf", "%", saved)["id"] == "pct_nota_fiscal"
    assert svc.resolve_calculation_base("100kg ou fração", "R$", saved)["id"] == "fracao_100kg"


def test_parsear_calculation_bases_form_gera_id_para_nova_base():
    form = MultiDict(
        [
            ("calculation_base_row_index", "0"),
            ("calculation_base_label_0", "Taxa administrativa"),
            ("calculation_base_unit_0", "R$"),
            ("calculation_base_calculation_type_0", "fixed_amount"),
            ("calculation_base_operation_0", "fixed_amount"),
            ("calculation_base_aliases_0", "taxa adm"),
            ("calculation_base_is_active_0", "1"),
            ("calculation_base_display_order_0", "10"),
        ]
    )

    bases = svc.parsear_calculation_bases_form(form)

    assert bases[0]["id"] == "taxa_administrativa"
    assert bases[0]["parameters"] == {}


def test_salvar_calculation_bases_json_invalido_nao_apaga_config_anterior(ctx):
    original = [svc.DEFAULT_CALCULATION_BASES[1]]
    svc.salvar_cleide_audit_calculation_bases(original)
    before = ConfigRegras.query.filter_by(
        chave="cleide_audit_cfg_calculation_bases"
    ).first().valor_texto

    with pytest.raises(ValueError, match="JSON inválido"):
        svc.salvar_cleide_audit_calculation_bases_json("{invalid")

    after = ConfigRegras.query.filter_by(
        chave="cleide_audit_cfg_calculation_bases"
    ).first().valor_texto
    assert after == before
    assert svc.carregar_cleide_audit_calculation_bases()[0]["id"] == "por_cte"


def test_config_antiga_sem_calculation_bases_recebe_default(ctx):
    svc.salvar_cleide_audit_config({"chat_max_history": "7"})
    row = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_calculation_bases").first()
    assert row is None

    if hasattr(g, "_cleide_audit_cfg"):
        delattr(g, "_cleide_audit_cfg")

    loaded = svc.get_cleide_audit_config()
    assert loaded.chat_max_history == 7
    assert loaded.calculation_bases == svc.DEFAULT_CALCULATION_BASES


def test_salvar_calculation_bases_preserva_outras_configs_auditoria(ctx):
    svc.salvar_cleide_audit_config(
        {
            "chat_max_history": "6",
            "fallback_message": "fallback preservado",
            "upload_enabled": "0",
        }
    )

    svc.salvar_cleide_audit_calculation_bases([svc.DEFAULT_CALCULATION_BASES[0]])

    loaded = svc.get_cleide_audit_config()
    assert loaded.chat_max_history == 6
    assert loaded.fallback_message == "fallback preservado"
    assert loaded.upload_enabled is False
    assert [base["id"] for base in loaded.calculation_bases] == ["pct_nota_fiscal"]


@pytest.mark.parametrize(
    ("basis", "unit", "expected_id"),
    [
        ("valor da nota fiscal", "%", "pct_nota_fiscal"),
        ("por Cte", "R$", "por_cte"),
        ("por conhecimento", "R$", "por_conhecimento"),
        ("100Kg ou fração", "R$", "fracao_100kg"),
        ("sobre NF", "%", "pct_nota_fiscal"),
        ("sobre o valor da Nota Fiscal", "%", "pct_nota_fiscal"),
        ("sobre o valor da NF", "%", "pct_nota_fiscal"),
        ("sobre o valor de N.Fiscal", "%", "pct_nota_fiscal"),
        ("S/ Valor da Nota Fiscal", "%", "pct_nota_fiscal"),
        ("sobre nota fiscal", "%", "pct_nota_fiscal"),
        ("para cada 100Kg ou fração", "R$", "fracao_100kg"),
    ],
)
def test_resolve_calculation_base_defaults_por_basis_e_unit(basis, unit, expected_id):
    match = svc.resolve_calculation_base(basis, unit, svc.DEFAULT_CALCULATION_BASES)
    assert match is not None
    assert match["id"] == expected_id


def test_parse_calculation_bases_persistida_antiga_recebe_aliases_default(ctx):
    old_pct_base = dict(svc.DEFAULT_CALCULATION_BASES[0], aliases=["valor da nota fiscal"])
    svc.salvar_cleide_audit_calculation_bases([old_pct_base])

    if hasattr(g, "_cleide_audit_cfg"):
        delattr(g, "_cleide_audit_cfg")

    loaded = svc.get_cleide_audit_config()
    assert svc.resolve_calculation_base(
        "sobre o valor de N.Fiscal",
        "%",
        loaded.calculation_bases,
    )["id"] == "pct_nota_fiscal"


def test_resolve_calculation_base_exige_unidade_compativel():
    match = svc.resolve_calculation_base(
        "valor da nota fiscal",
        "R$",
        svc.DEFAULT_CALCULATION_BASES,
    )
    assert match is None


def test_resolve_calculation_base_ignora_inativas():
    bases = [dict(svc.DEFAULT_CALCULATION_BASES[0], is_active=False)]
    match = svc.resolve_calculation_base("valor da nota fiscal", "%", bases)
    assert match is None


def test_resolve_calculation_base_ambigua_nao_escolhe():
    bases = [
        dict(svc.DEFAULT_CALCULATION_BASES[0]),
        dict(
            svc.DEFAULT_CALCULATION_BASES[0],
            id="pct_nota_fiscal_2",
            label="% por NF duplicada",
            aliases=["valor da nota fiscal"],
        ),
    ]

    result = svc.resolve_calculation_base_status("valor da nota fiscal", "%", bases)

    assert result["status"] == "ambiguous"
    assert result["base"] is None
    assert svc.resolve_calculation_base("valor da nota fiscal", "%", bases) is None


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


def test_salvar_form_geral_chat_enabled_isolado_nao_persiste_bases(ctx):
    saved = svc.salvar_cleide_audit_config({"chat_enabled": "0"})

    assert saved.chat_enabled is False
    row = ConfigRegras.query.filter_by(chave="cleide_audit_cfg_chat_enabled").first()
    assert row is not None
    assert row.valor_inteiro == 0
    assert (
        ConfigRegras.query.filter_by(chave="cleide_audit_cfg_calculation_bases").first()
        is None
    )


def test_salvar_form_geral_audited_file_max_rows_isolado_nao_persiste_bases(ctx):
    saved = svc.salvar_cleide_audit_config({"audited_file_max_rows": "2500"})

    assert saved.audited_file_max_rows == 2500
    assert (
        ConfigRegras.query.filter_by(chave="cleide_audit_cfg_calculation_bases").first()
        is None
    )


def test_audited_file_max_bytes_opcional_herda_global_cleiton(ctx):
    salvar_cleiton_doc_config(
        {
            "excel_max_bytes": str(4 * 1024 * 1024),
            "excel_max_rows": "3500",
        }
    )
    cfg = svc.salvar_cleide_audit_config({"audited_file_max_rows": "2000"})

    limits = svc.resolve_audited_file_limits(cfg)

    assert cfg.audited_file_max_bytes is None
    assert limits == {
        "global_max_bytes": 4 * 1024 * 1024,
        "specific_max_bytes": None,
        "effective_max_bytes": 4 * 1024 * 1024,
        "effective_max_rows": 2000,
        "source": "global",
    }
    row = ConfigRegras.query.filter_by(
        chave="cleide_audit_cfg_audited_file_max_bytes"
    ).first()
    assert row is None


def test_audited_file_limits_especifico_mais_restritivo(ctx):
    salvar_cleiton_doc_config(
        {
            "excel_max_bytes": str(4 * 1024 * 1024),
            "excel_max_rows": "3500",
        }
    )
    cfg = svc.salvar_cleide_audit_config(
        {
            "audited_file_max_bytes": str(2 * 1024 * 1024),
            "audited_file_max_rows": "2500",
        }
    )

    limits = svc.resolve_audited_file_limits(cfg)

    assert limits["global_max_bytes"] == 4 * 1024 * 1024
    assert limits["specific_max_bytes"] == 2 * 1024 * 1024
    assert limits["effective_max_bytes"] == 2 * 1024 * 1024
    assert limits["effective_max_rows"] == 2500
    assert limits["source"] == "specific_capped_by_global"


def test_audited_file_limits_runtime_capado_pelo_global(ctx):
    salvar_cleiton_doc_config(
        {
            "excel_max_bytes": str(2 * 1024 * 1024),
            "excel_max_rows": "1000",
        }
    )
    cfg = replace(
        svc.get_cleide_audit_config(),
        audited_file_max_bytes=4 * 1024 * 1024,
        audited_file_max_rows=2500,
    )

    limits = svc.resolve_audited_file_limits(cfg)

    assert limits["global_max_bytes"] == 2 * 1024 * 1024
    assert limits["specific_max_bytes"] == 4 * 1024 * 1024
    assert limits["effective_max_bytes"] == 2 * 1024 * 1024
    assert limits["effective_max_rows"] == 1000
    assert limits["source"] == "specific_capped_by_global"


def test_audited_file_max_bytes_acima_do_global_rejeitado(ctx):
    salvar_cleiton_doc_config({"excel_max_bytes": str(2 * 1024 * 1024)})

    with pytest.raises(ValueError, match="não pode ultrapassar o limite global"):
        svc.salvar_cleide_audit_config(
            {"audited_file_max_bytes": str(4 * 1024 * 1024)}
        )


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_audited_file_max_bytes_invalido_rejeitado(ctx, value):
    with pytest.raises(ValueError, match="audited_file_max_bytes"):
        svc.salvar_cleide_audit_config({"audited_file_max_bytes": value})


def test_audited_file_max_bytes_em_branco_remove_especifico(ctx):
    svc.salvar_cleide_audit_config({"audited_file_max_bytes": "1048576"})

    saved = svc.salvar_cleide_audit_config({"audited_file_max_bytes": ""})

    assert saved.audited_file_max_bytes is None
    row = ConfigRegras.query.filter_by(
        chave="cleide_audit_cfg_audited_file_max_bytes"
    ).first()
    assert row is None


def test_salvar_form_geral_audited_file_max_bytes_vazio_herda_global(ctx):
    svc.salvar_cleide_audit_config({"audited_file_max_bytes": "1048576"})

    saved = svc.salvar_cleide_audit_config({"audited_file_max_bytes": ""})

    assert saved.audited_file_max_bytes is None
    assert svc.resolve_audited_file_limits(saved)["source"] == "global"
    assert (
        ConfigRegras.query.filter_by(chave="cleide_audit_cfg_calculation_bases").first()
        is None
    )


def test_salvar_form_geral_nao_persiste_string_maior_que_valor_texto(ctx, monkeypatch):
    original_add = db.session.add

    def _assert_no_long_text(row):
        text = getattr(row, "valor_texto", None)
        assert text is None or len(text) <= 500
        return original_add(row)

    monkeypatch.setattr(db.session, "add", _assert_no_long_text)

    saved = svc.salvar_cleide_audit_config({"chat_enabled": "0"})

    assert saved.chat_enabled is False
    assert (
        ConfigRegras.query.filter_by(chave="cleide_audit_cfg_calculation_bases").first()
        is None
    )


def test_persistencia_form_geral_nao_percorre_defaults():
    source = inspect.getsource(svc.persistir_cleide_audit_config)
    assert "for nome in DEFAULTS.keys()" not in source
    assert "GENERAL_FORM_CONFIG_FIELDS" in source


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
