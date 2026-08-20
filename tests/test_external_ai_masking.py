"""Testes do mascaramento field-aware no outbound Gemini (AI-MASK-R1)."""
from __future__ import annotations

import copy
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.cleiton_doc_gemini_files as gemini_files
import app.cleiton_doc_service as julia_doc_svc
import app.run_julia_chat as julia_chat
import app.services.external_ai_masking as masking_mod
from app.cleiton_doc_store import peek_document_record
from app.julia_doc_context import build_julia_document_context_for_chat
from app.run_julia_chat import chat_julia_reply
from app.services.external_ai_masking import (
    ExternalAiMaskingSession,
    MASKABLE_FIELD_KEYS,
    mask_structured_for_external_ai,
)
from tests.cleiton_doc_fixtures import (
    make_minimal_pdf,
    make_txt,
    patch_cleiton_doc_cfg,
    patch_cleiton_doc_store,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _sample_payload() -> dict:
    return {
        "cidade": "Campinas",
        "UF": "SP",
        "transportadora": "GBEX",
        "cnpj": "12.345.678/0001-90",
        "preco": 150.75,
        "tarifa": 12.3,
        "email": "ana@cliente.com",
        "customer_email": "ana@cliente.com",
        "phone": "11999990000",
        "telefone": "1133334444",
        "cpf": "123.456.789-00",
        "filename": "contrato_joao.pdf",
        "source_file_name": "tabela_cliente.xlsx",
        "display_name": "nota_fiscal.pdf",
        "name": "João da Silva",
        "nome": "João da Silva",
        "tomador": "ACME Logística",
        "remetente": "Remetente LTDA",
        "destinatario": "Destino SA",
        "chave_cte": "35240112345678901234567890123456789012345678",
        "placa": "ABC1D23",
        "notes": "contato ana@cliente.com cpf 123.456.789-00",
        "prepared_context": "email ana@cliente.com cpf 999.888.777-66",
        "nested": {
            "cidade": "Santos",
            "email": "outro@cliente.com",
            "rows": [
                {
                    "destination_city": "Campinas",
                    "destination_uf": "SP",
                    "carrier": "GBEX",
                    "charged_freight": 99.9,
                    "email": "ana@cliente.com",
                    "filename": "contrato_joao.pdf",
                }
            ],
        },
    }


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    patch_cleiton_doc_cfg(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def test_original_dict_is_not_mutated():
    original = _sample_payload()
    snapshot = copy.deepcopy(original)
    masked = mask_structured_for_external_ai(original)
    assert original == snapshot
    assert masked is not original
    assert masked["nested"] is not original["nested"]
    assert masked["nested"]["rows"] is not original["nested"]["rows"]


def test_schema_remains_identical():
    original = _sample_payload()
    masked = mask_structured_for_external_ai(original)
    assert set(masked.keys()) == set(original.keys())
    assert set(masked["nested"].keys()) == set(original["nested"].keys())
    assert len(masked["nested"]["rows"]) == len(original["nested"]["rows"])
    assert set(masked["nested"]["rows"][0].keys()) == set(original["nested"]["rows"][0].keys())


def test_logistics_and_commercial_fields_stay_intact():
    original = _sample_payload()
    masked = mask_structured_for_external_ai(original)
    assert masked["cidade"] == "Campinas"
    assert masked["UF"] == "SP"
    assert masked["transportadora"] == "GBEX"
    assert masked["cnpj"] == "12.345.678/0001-90"
    assert masked["preco"] == 150.75
    assert masked["tarifa"] == 12.3
    assert masked["nested"]["cidade"] == "Santos"
    assert masked["nested"]["rows"][0]["destination_city"] == "Campinas"
    assert masked["nested"]["rows"][0]["destination_uf"] == "SP"
    assert masked["nested"]["rows"][0]["carrier"] == "GBEX"
    assert masked["nested"]["rows"][0]["charged_freight"] == 99.9


def test_structured_email_is_masked_stably():
    original = _sample_payload()
    masked = mask_structured_for_external_ai(original)
    assert masked["email"] == "[EMAIL_1]"
    assert masked["customer_email"] == "[EMAIL_1]"
    assert masked["nested"]["rows"][0]["email"] == "[EMAIL_1]"
    assert masked["nested"]["email"] == "[EMAIL_2]"
    assert masked["email"] != masked["nested"]["email"]


def test_structured_phone_and_cpf_are_masked():
    masked = mask_structured_for_external_ai(_sample_payload())
    assert masked["phone"] == "[TEL_1]"
    assert masked["telefone"] == "[TEL_2]"
    assert masked["cpf"] == "[CPF_1]"


def test_free_text_email_and_cpf_are_not_changed():
    original = _sample_payload()
    masked = mask_structured_for_external_ai(original)
    assert masked["notes"] == original["notes"]
    assert masked["prepared_context"] == original["prepared_context"]
    assert mask_structured_for_external_ai("email ana@cliente.com cpf 123.456.789-00") == (
        "email ana@cliente.com cpf 123.456.789-00"
    )


def test_ambiguous_identity_fields_are_not_masked():
    masked = mask_structured_for_external_ai(_sample_payload())
    assert masked["name"] == "João da Silva"
    assert masked["nome"] == "João da Silva"
    assert masked["tomador"] == "ACME Logística"
    assert masked["remetente"] == "Remetente LTDA"
    assert masked["destinatario"] == "Destino SA"
    assert masked["chave_cte"] == "35240112345678901234567890123456789012345678"
    assert masked["placa"] == "ABC1D23"


def test_filenames_are_neutralized_and_extensions_preserved():
    masked = mask_structured_for_external_ai(_sample_payload())
    assert masked["filename"] == "[ARQUIVO_1].pdf"
    assert masked["nested"]["rows"][0]["filename"] == "[ARQUIVO_1].pdf"
    assert masked["display_name"].startswith("[ARQUIVO_")
    assert masked["display_name"].endswith(".pdf")
    assert masked["source_file_name"].startswith("[ARQUIVO_")
    assert masked["source_file_name"].endswith(".xlsx")
    assert masked["display_name"] != masked["filename"]
    assert masked["source_file_name"] != masked["filename"]
    assert "joao" not in masked["filename"].lower()
    assert "cliente" not in masked["source_file_name"].lower()


def test_nested_types_and_structure_are_preserved():
    original = _sample_payload()
    original["flags"] = (True, None, 0)
    masked = mask_structured_for_external_ai(original)
    assert isinstance(masked, dict)
    assert isinstance(masked["nested"]["rows"], list)
    assert isinstance(masked["preco"], float)
    assert isinstance(masked["tarifa"], float)
    assert masked["flags"] == (True, None, 0)
    assert type(masked["flags"]) is tuple


def test_mapping_is_not_persisted_between_operations():
    first = mask_structured_for_external_ai({"email": "ana@cliente.com"})
    second = mask_structured_for_external_ai({"email": "bruno@cliente.com"})
    assert first["email"] == "[EMAIL_1]"
    assert second["email"] == "[EMAIL_1]"
    session = ExternalAiMaskingSession()
    mask_structured_for_external_ai({"email": "ana@cliente.com"}, session=session)
    assert not any(
        isinstance(value, dict)
        for name, value in vars(masking_mod).items()
        if not name.startswith("__")
    )
    assert MASKABLE_FIELD_KEYS == {
        "display_name",
        "source_file_name",
        "filename",
        "email",
        "customer_email",
        "phone",
        "telefone",
        "cpf",
    }


def test_helper_is_field_aware_not_content_aware():
    source = inspect.getsource(masking_mod)
    assert "import re" not in source
    assert "re.compile" not in source
    assert "re.search" not in source
    assert '"@" in' not in source
    assert "contains(" not in source
    assert "NER" not in source
    assert "openai" not in source.lower()
    assert r"\d{3}" not in source
    assert r"\d{11}" not in source


def test_pdf_bytes_remain_identical_and_gemini_display_name_is_neutral(monkeypatch):
    captured = {}
    original_bytes = make_minimal_pdf()

    def _upload(*, file, config):
        captured["bytes"] = file.getvalue()
        captured["display_name"] = config["display_name"]
        return SimpleNamespace(
            name="files/test-pdf-abc",
            uri="https://generativelanguage.googleapis.com/v1beta/files/test-pdf-abc",
            mime_type="application/pdf",
            state="ACTIVE",
        )

    client = MagicMock()
    client.files.upload.side_effect = _upload
    result = gemini_files.upload_pdf_to_gemini_files_api(
        file_bytes=original_bytes,
        display_name="contrato_joao.pdf",
        client=client,
    )
    assert result.ok is True
    assert captured["bytes"] == original_bytes
    assert captured["display_name"] == "[ARQUIVO_1].pdf"
    assert "joao" not in captured["display_name"].lower()
    assert original_bytes == make_minimal_pdf()


def test_stored_document_keeps_original_display_name(session_app, monkeypatch):
    captured = {}
    client = MagicMock()
    uploaded = SimpleNamespace(
        name="files/test-pdf-abc",
        uri="https://generativelanguage.googleapis.com/v1beta/files/test-pdf-abc",
        mime_type="application/pdf",
        state="ACTIVE",
    )

    def _upload(*, file, config):
        captured["display_name"] = config["display_name"]
        captured["bytes"] = file.getvalue()
        return uploaded

    client.files.upload.return_value = uploaded
    client.files.upload.side_effect = _upload
    client.files.get.return_value = uploaded
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)
    original_bytes = make_minimal_pdf()
    with session_app.test_request_context("/"):
        public = julia_doc_svc.prepare_and_register_document(
            display_name="contrato_joao.pdf",
            file_bytes=original_bytes,
            mime_type="application/pdf",
        )
    record = peek_document_record(public["doc_id"])
    assert record["display_name"] == "contrato_joao.pdf"
    assert captured["display_name"] == "[ARQUIVO_1].pdf"
    assert captured["bytes"] == original_bytes


def test_julia_keeps_message_history_and_prepared_text(session_app, monkeypatch):
    capture = {}

    class _Resp:
        text = "resposta simulada"

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["contents"] = contents
        capture["flow_type"] = flow_type
        return _Resp()

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(julia_chat, "_get_client", lambda: object())
    with session_app.test_request_context("/"):
        registered = julia_doc_svc.prepare_and_register_document(
            display_name="contrato_joao.txt",
            file_bytes=make_txt("cidade Campinas email hidden@x.com"),
            mime_type="text/plain",
        )
        doc_ctx = build_julia_document_context_for_chat()
        chat_julia_reply(
            "meu email e user@test.com",
            [{"role": "user", "content": "cpf 999.888.777-66 na conversa"}],
            document_context_block=doc_ctx["context_block"],
            flow_type=doc_ctx["flow_type"],
        )
        stored = peek_document_record(registered["doc_id"])
    contents = capture["contents"]
    assert "meu email e user@test.com" in contents
    assert "cpf 999.888.777-66 na conversa" in contents
    assert "cidade Campinas email hidden@x.com" in contents
    assert "contrato_joao.txt" not in contents
    assert "[ARQUIVO_1].txt" in contents
    assert stored["display_name"] == "contrato_joao.txt"
    assert doc_ctx["context_block"].count("contrato_joao.txt") == 0


def test_cleide_keeps_logistics_fields_on_structured_payload():
    payload = {
        "cidade": "Campinas",
        "UF": "SP",
        "transportadora": "GBEX",
        "chave_cte": "352401ABCDEF",
        "tomador": "ACME",
        "remetente": "Casa A",
        "destinatario": "Casa B",
        "placa": "ABC1D23",
        "source_file_name": "cte_joao.xlsx",
        "email": "ana@cliente.com",
        "focused_rows": [
            {
                "destination_city": "Campinas",
                "destination_uf": "SP",
                "carrier": "GBEX",
                "charged_freight": 88.1,
            }
        ],
    }
    masked = mask_structured_for_external_ai(payload)
    assert masked["cidade"] == "Campinas"
    assert masked["UF"] == "SP"
    assert masked["transportadora"] == "GBEX"
    assert masked["chave_cte"] == "352401ABCDEF"
    assert masked["tomador"] == "ACME"
    assert masked["remetente"] == "Casa A"
    assert masked["destinatario"] == "Casa B"
    assert masked["placa"] == "ABC1D23"
    assert masked["focused_rows"][0]["charged_freight"] == 88.1
    assert masked["source_file_name"] == "[ARQUIVO_1].xlsx"
    assert masked["email"] == "[EMAIL_1]"
    cleide_ctx_src = (REPO_ROOT / "app" / "cleide_audit_doc_context.py").read_text(
        encoding="utf-8"
    )
    insights_src = (REPO_ROOT / "app" / "run_cleide_audit_insights_chat.py").read_text(
        encoding="utf-8"
    )
    assert "mask_structured_for_external_ai" in cleide_ctx_src
    assert "mask_structured_for_external_ai(compact)" in insights_src


def test_agente_compara_keeps_comparative_fields_on_structured_payload():
    payload = {
        "carrier": "GBEX",
        "UF": "SP",
        "tarifa": 10.5,
        "generalidades": [{"code": "GRIS", "value": 0.3}],
        "regras": ["pedagio incluso"],
        "focused_rows": [
            {
                "destination_city": "Santos",
                "destination_uf": "SP",
                "charged_freight": 40.0,
                "expected_freight": 38.0,
            }
        ],
        "source_file_name": "tabela_operacional.xlsx",
        "display_name": "tabela_operacional.xlsx",
    }
    masked = mask_structured_for_external_ai(payload)
    assert masked["carrier"] == "GBEX"
    assert masked["UF"] == "SP"
    assert masked["tarifa"] == 10.5
    assert masked["generalidades"] == [{"code": "GRIS", "value": 0.3}]
    assert masked["regras"] == ["pedagio incluso"]
    assert masked["focused_rows"][0]["expected_freight"] == 38.0
    assert masked["source_file_name"] == "[ARQUIVO_1].xlsx"
    assert masked["display_name"] == "[ARQUIVO_1].xlsx"
    compara_ctx_src = (REPO_ROOT / "app" / "agente_compara_doc_context.py").read_text(
        encoding="utf-8"
    )
    insights_src = (
        REPO_ROOT / "app" / "run_agente_compara_insights_chat.py"
    ).read_text(encoding="utf-8")
    assert "mask_structured_for_external_ai" in compara_ctx_src
    assert "mask_structured_for_external_ai(compact)" in insights_src


def test_roberto_does_not_use_the_helper():
    roberto_files = [
        REPO_ROOT / "app" / "run_roberto.py",
        REPO_ROOT / "app" / "run_roberto_chat.py",
        REPO_ROOT / "app" / "roberto_bi.py",
        REPO_ROOT / "app" / "roberto_custo.py",
        REPO_ROOT / "app" / "roberto_modelo.py",
        REPO_ROOT / "app" / "roberto_recomendacoes.py",
        REPO_ROOT / "app" / "roberto_qualidade_base.py",
        REPO_ROOT / "app" / "roberto_upload_store.py",
        REPO_ROOT / "app" / "services" / "roberto_config_service.py",
    ]
    for path in roberto_files:
        source = path.read_text(encoding="utf-8")
        assert "external_ai_masking" not in source
        assert "mask_structured_for_external_ai" not in source
