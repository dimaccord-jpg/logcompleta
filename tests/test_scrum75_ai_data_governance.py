"""SCRUM-75: governança contextual local antes de qualquer saída de IA externa."""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.cleiton_doc_gemini_files as gemini_files
import app.cleiton_doc_service as julia_doc_svc
import app.run_julia_chat as julia_chat
import app.services.cleiton_ai_data_governance as gov
import app.services.cleiton_ai_privacy_classifier as classifier
import app.services.cleiton_ai_safe_context as safe_ctx
import app.services.julia_web_search_service as web_search
from app.cleiton_doc_contracts import CONTEXT_KIND_TEXT, FIELD_CONTEXT_KIND, FIELD_PREPARED_CONTEXT
from app.cleiton_doc_store import peek_document_record
from app.julia_doc_context import build_julia_document_context_for_chat
from app.run_cleiton_gemini_governance import (
    cleiton_governed_generate_content,
    cleiton_governed_generate_images,
)
from app.services.cleiton_ai_data_governance import (
    USER_SAFE_PREPARATION_FAILED,
    CleitonAiGovernanceBlockedError,
    GovernanceResult,
    extract_authorized_upload_bytes,
    get_ai_data_protection_status,
    govern_or_raise,
    govern_outbound_content,
    project_safe_error,
)
from app.services.cleiton_ai_privacy_classifier import (
    ClassificationResult,
    DECISION_MINIMIZE,
)
from app.services.cleiton_ai_safe_context import CleitonAiAliasSession
from app.services.external_ai_masking import mask_structured_for_external_ai
from tests.cleiton_doc_fixtures import (
    make_csv,
    make_docx,
    make_text_pdf,
    make_txt,
    make_xlsx,
    make_xml,
    patch_cleiton_doc_cfg,
    patch_cleiton_doc_store,
)

CPF = "529.982.247-25"
CNPJ = "04.252.011/0001-10"
FISCAL_KEY = "35240112345678901234567890123456789012345678"
EMAIL = "ana.scrum75@cliente.com"
PHONE = "11999990000"
TOKEN = "ghp_" + ("x" * 36)
API_KEY = "sk_test_" + ("x" * 24)
BANK = "agencia 1234 conta 12345-6"

LOGISTICS = (
    "São Paulo → Curitiba\n"
    "Peso: 500 kg\n"
    "Frete: R$ 2.430\n"
    "GRIS: 0,30%\n"
    "Pedágio: R$ 87\n"
    "CIF/FOB"
)


def _mixed_text() -> str:
    return (
        f"{LOGISTICS}\n"
        f"CPF {CPF}\n"
        f"CNPJ {CNPJ}\n"
        f"chave NF-e {FISCAL_KEY}\n"
        f"email {EMAIL}\n"
        f"telefone {PHONE}\n"
        f"token {TOKEN}\n"
        f"api_key {API_KEY}\n"
        f"banco {BANK}\n"
    )


class _FakeModels:
    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def generate_content(self, *, model, contents, config=None):
        self._captured["contents"] = contents
        self._captured["config"] = config
        self._captured["model"] = model
        self._captured["calls"] = self._captured.get("calls", 0) + 1
        return SimpleNamespace(text="ok", usage_metadata=None)

    def generate_images(self, **kwargs):
        self._captured["prompt"] = kwargs.get("prompt")
        self._captured["config"] = kwargs.get("config")
        self._captured.update({k: v for k, v in kwargs.items() if k not in self._captured})
        self._captured["calls"] = self._captured.get("calls", 0) + 1
        return SimpleNamespace(generated_images=[])


def _fake_client(captured: dict) -> SimpleNamespace:
    return SimpleNamespace(models=_FakeModels(captured))


@pytest.fixture
def session_app(app, tmp_path, monkeypatch, ctx):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    patch_cleiton_doc_cfg(monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    return app


def test_classifier_and_governance_do_not_use_regex():
    for module in (classifier, gov, safe_ctx):
        source = inspect.getsource(module)
        assert "import re\n" not in source
        assert "import re " not in source
        assert "from re " not in source
        assert "re.compile" not in source
        assert "re.search" not in source
        assert r"\d{3}" not in source
        assert r"\d{11}" not in source


def test_protection_status_is_mandatory():
    status = get_ai_data_protection_status()
    assert status["data_protection_in_ai"] == "active"
    assert status["contextual_minimization"] == "active"
    assert status["credential_protection"] == "active"
    assert status["data_isolation"] == "active"
    assert status["guardrails_mandatory"] is True
    assert status["disable_protection"] is False
    assert status["external_classifier_calls"] is False


def test_free_text_critical_data_does_not_reach_sdk_and_logistics_remain(ctx):
    captured = {}
    client = _fake_client(captured)
    cleiton_governed_generate_content(
        client,
        model="gemini-test",
        contents=_mixed_text(),
        agent="julia",
        flow_type="julia_chat",
        api_key_label="test",
    )
    payload = captured["contents"]
    assert captured["calls"] == 1
    assert CPF not in payload
    assert CNPJ not in payload
    assert FISCAL_KEY not in payload
    assert EMAIL not in payload
    assert PHONE not in payload
    assert TOKEN not in payload
    assert API_KEY not in payload
    assert "12345-6" not in payload
    assert "São Paulo" in payload
    assert "Curitiba" in payload
    assert "500 kg" in payload
    assert "2.430" in payload
    assert "0,30%" in payload
    assert "87" in payload
    assert "CIF/FOB" in payload


def test_history_and_previous_reply_are_governed_before_next_payload(monkeypatch):
    captured = {}
    history = [
        {"role": "user", "content": f"cpf antigo {CPF}"},
        {"role": "model", "content": f"segredo anterior {API_KEY}"},
    ]
    monkeypatch.setattr(julia_chat, "_get_client", lambda: _fake_client(captured))

    def _governed(client, **kwargs):
        return client.models.generate_content(model=kwargs["model"], contents=kwargs["contents"])

    monkeypatch.setattr(julia_chat, "cleiton_governed_generate_content", _governed)
    julia_chat.chat_julia_reply(f"{LOGISTICS} sem dado novo", history)
    payload = captured["contents"]
    assert CPF not in payload
    assert API_KEY not in payload
    assert "São Paulo" in payload


def test_nested_unknown_keys_are_minimized():
    payload = {
        "notes": f"contato {EMAIL} cpf {CPF}",
        "carrier": "GBEX",
        "diagnostic": f"token {TOKEN}",
        "observações": f"chave {FISCAL_KEY}",
        "campo_novo": f"api_key {API_KEY}",
        "cidade": "Campinas",
    }
    result = govern_or_raise(
        payload,
        purpose="auditoria_frete",
        content_type="document",
        agent="cleide",
    )
    safe = result.safe_content
    assert safe["cidade"] == "Campinas"
    assert safe["carrier"] == "GBEX"
    assert EMAIL not in safe["notes"]
    assert CPF not in safe["notes"]
    assert TOKEN not in safe["diagnostic"]
    assert FISCAL_KEY not in safe["observações"]
    assert API_KEY not in safe["campo_novo"]


def test_alias_is_stable_in_operation_and_not_global():
    session = CleitonAiAliasSession(usuario_id=1)
    first = govern_or_raise(
        f"email {EMAIL}",
        purpose="chat_logistico",
        alias_session=session,
        agent="julia",
    )
    second = govern_or_raise(
        f"email {EMAIL} novamente",
        purpose="chat_logistico",
        alias_session=session,
        agent="julia",
    )
    other = CleitonAiAliasSession(usuario_id=2)
    third = govern_or_raise(
        f"email {EMAIL}",
        purpose="chat_logistico",
        alias_session=other,
        agent="julia",
    )
    assert first.safe_content.count("[EMAIL_1]") == 1
    assert "[EMAIL_1]" in second.safe_content
    assert "[EMAIL_2]" not in second.safe_content
    assert third.safe_content == first.safe_content
    assert session.mapping_size() >= 1
    assert other.mapping_size() >= 1
    assert session._tokens is not other._tokens


def test_pdf_original_bytes_are_not_uploaded(session_app, monkeypatch):
    captured = {}
    marker = f"CRITICO-{CPF}-{API_KEY}"
    pdf_bytes = make_text_pdf(f"{LOGISTICS}\n{marker}")

    def _upload(*, file, config):
        captured["bytes"] = file.getvalue()
        captured["display_name"] = config["display_name"]
        raise AssertionError("files.upload não deveria receber PDF original")

    client = MagicMock()
    client.files.upload.side_effect = _upload
    monkeypatch.setattr(gemini_files, "get_cleiton_gemini_client", lambda: client)

    with session_app.test_request_context("/"):
        public = julia_doc_svc.prepare_and_register_document(
            display_name="contrato_joao.pdf",
            file_bytes=pdf_bytes,
            mime_type="application/pdf",
        )
        record = peek_document_record(public["doc_id"])
        doc_ctx = build_julia_document_context_for_chat()

    assert public[FIELD_CONTEXT_KIND] == CONTEXT_KIND_TEXT
    client.files.upload.assert_not_called()
    assert captured == {}
    assert record[FIELD_PREPARED_CONTEXT]  # original local extraído permanece
    assert marker in (record.get(FIELD_PREPARED_CONTEXT) or "")
    assert marker not in doc_ctx["context_block"]
    assert "São Paulo" in doc_ctx["context_block"]
    assert doc_ctx["gemini_file_parts"] == []
    assert record["display_name"] == "contrato_joao.pdf"


@pytest.mark.parametrize(
    ("builder", "name", "mime"),
    [
        (lambda: make_txt(_mixed_text()), "dados.txt", "text/plain"),
        (lambda: make_xml(f"<root><n>{_mixed_text()}</n></root>"), "dados.xml", "application/xml"),
        (lambda: make_csv([["cidade", "cpf"], ["Campinas", CPF]]), "dados.csv", "text/csv"),
        (lambda: make_xlsx([["cidade", "email"], ["Santos", EMAIL]]), "dados.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        (lambda: make_docx([LOGISTICS, f"cpf {CPF}"]), "dados.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ],
)
def test_converted_formats_are_governed_before_outbound(session_app, builder, name, mime):
    with session_app.test_request_context("/"):
        public = julia_doc_svc.prepare_and_register_document(
            display_name=name,
            file_bytes=builder(),
            mime_type=mime,
        )
        stored = peek_document_record(public["doc_id"])
        doc_ctx = build_julia_document_context_for_chat()
    assert stored[FIELD_CONTEXT_KIND] == CONTEXT_KIND_TEXT
    block = doc_ctx["context_block"]
    assert CPF not in block
    assert EMAIL not in block
    assert "Campinas" in block or "Santos" in block or "São Paulo" in block


def test_duckduckgo_query_does_not_receive_critical_data(monkeypatch):
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        text = "<html></html>"

    def _get(url, params=None, timeout=None, headers=None):
        seen["url"] = url
        seen["q"] = (params or {}).get("q")
        return _Resp()

    monkeypatch.setattr(web_search.requests, "get", _get)
    web_search.search_web_links(f"cotacao atual {LOGISTICS} cpf {CPF} {EMAIL}")
    query = seen.get("q") or ""
    assert CPF not in query
    assert EMAIL not in query
    assert "cotacao" in query.lower() or "São Paulo" in query


def test_image_prompt_is_governed(ctx):
    captured = {}
    client = _fake_client(captured)
    cleiton_governed_generate_images(
        client,
        agent="julia",
        flow_type="julia_imagem_imagen",
        api_key_label="test",
        model="imagen-3.0-generate-002",
        prompt=f"editorial logistics {EMAIL} {API_KEY}",
    )
    assert EMAIL not in (captured.get("prompt") or "")
    assert API_KEY not in (captured.get("prompt") or "")
    assert captured["calls"] == 1


def test_retry_uses_already_authorized_content(ctx):
    captured = {"payloads": []}

    class _Models:
        def generate_content(self, *, model, contents, config=None):
            captured["payloads"].append(contents)
            if len(captured["payloads"]) == 1:
                raise RuntimeError(f"provider boom {CPF}")
            return SimpleNamespace(text="ok", usage_metadata=None)

    client = SimpleNamespace(models=_Models())
    authorized = govern_or_raise(_mixed_text(), purpose="chat_logistico", agent="julia").safe_content
    with pytest.raises(RuntimeError):
        cleiton_governed_generate_content(
            client,
            model="m1",
            contents=authorized,
            agent="julia",
            flow_type="julia_chat",
            api_key_label="test",
        )
    cleiton_governed_generate_content(
        client,
        model="m2",
        contents=authorized,
        agent="julia",
        flow_type="julia_chat",
        api_key_label="test",
    )
    for payload in captured["payloads"]:
        assert CPF not in payload
        assert "São Paulo" in payload


def test_classifier_failure_is_fail_closed_zero_provider_calls(ctx, monkeypatch):
    captured = {"calls": 0}

    class _Models:
        def generate_content(self, *, model, contents, config=None):
            captured["calls"] += 1
            return SimpleNamespace(text="leak", usage_metadata=None)

    monkeypatch.setattr(
        "app.services.cleiton_ai_data_governance.classify_text",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("classifier down")),
    )
    with pytest.raises(CleitonAiGovernanceBlockedError) as exc:
        cleiton_governed_generate_content(
            SimpleNamespace(models=_Models()),
            model="m",
            contents=_mixed_text(),
            agent="julia",
            flow_type="julia_chat",
            api_key_label="test",
        )
    assert captured["calls"] == 0
    assert USER_SAFE_PREPARATION_FAILED in str(exc.value)


def test_exception_projection_strips_sensitive_marker():
    marker = f"SECRET_MARKER_{API_KEY}"
    summary = project_safe_error(
        RuntimeError(f"upstream dump {marker}"),
        stage="generate_content",
        provider="gemini",
        retry=False,
    )
    assert marker not in summary
    assert API_KEY not in summary
    assert "RuntimeError" in summary
    assert "generate_content" in summary


def test_governance_event_does_not_persist_exception_payload(app, ctx, caplog):
    marker = f"SECRET_MARKER_{CPF}"
    with caplog.at_level("INFO"):
        summary = project_safe_error(
            RuntimeError(marker),
            stage="generate_content",
            provider="gemini",
        )
        from app.run_cleiton_gemini_governance import register_internal_ia_event, STATUS_FAILURE

        register_internal_ia_event(
            operation="ai_data_governance_block",
            agent="julia",
            flow_type="julia_chat",
            status=STATUS_FAILURE,
            error_summary=summary,
        )
    from app.models import IaConsumoEvento

    rows = IaConsumoEvento.query.all()
    blob = " ".join((row.error_summary or "") for row in rows)
    assert marker not in blob
    assert CPF not in blob
    assert marker not in caplog.text


def test_multiuser_alias_and_document_isolation(session_app):
    with session_app.test_request_context("/"):
        public_a = julia_doc_svc.prepare_and_register_document(
            display_name="privado-a.txt",
            file_bytes=make_txt(f"segredo do A {EMAIL}"),
            mime_type="text/plain",
        )
        record_a = peek_document_record(public_a["doc_id"])
    session_b = CleitonAiAliasSession(usuario_id="B")
    session_a = CleitonAiAliasSession(usuario_id="A")
    gov_a = govern_or_raise(
        record_a[FIELD_PREPARED_CONTEXT],
        purpose="chat_logistico",
        alias_session=session_a,
        agent="julia",
    )
    gov_b = govern_or_raise(
        "mensagem do B sem o arquivo de A",
        purpose="chat_logistico",
        alias_session=session_b,
        agent="julia",
    )
    assert EMAIL not in gov_a.safe_content
    assert EMAIL not in gov_b.safe_content
    assert "privado-a.txt" not in gov_b.safe_content
    assert record_a[FIELD_PREPARED_CONTEXT] != gov_a.safe_content


def test_logout_does_not_reuse_alias_mapping():
    session = CleitonAiAliasSession(usuario_id=9)
    govern_or_raise(f"email {EMAIL}", purpose="chat_logistico", alias_session=session)
    fresh = CleitonAiAliasSession(usuario_id=9)
    assert fresh.mapping_size() == 0


def test_original_structured_payload_is_not_mutated():
    original = {"notes": f"cpf {CPF}", "cidade": "Santos"}
    snapshot = dict(original)
    govern_or_raise(original, purpose="auditoria_frete", agent="cleide")
    assert original == snapshot
    masked_field = mask_structured_for_external_ai({"email": EMAIL, "cidade": "Santos"})
    assert masked_field["cidade"] == "Santos"
    assert masked_field["email"] != EMAIL


CODEX_CRITICAL_SNIPPETS = (
    "telefone (11) 99999-0000",
    "email ana@example.com.",
    "frete 100 reais; banco agencia 1234 conta 12345-6",
    "frete 100; cartao 4111 1111 1111 1111",
    "senha: abc",
    "senha de acesso: minhaSenha123",
    "Authorization: Basic dXNlcjpwYXNz",
    "Cookie: sid=abcdef; session=ghijkl",
    "usuario_id=918273 documento_id=privado-987",
)

CODEX_MARKERS = (
    "(11) 99999-0000",
    "ana@example.com",
    "12345-6",
    "4111 1111 1111 1111",
    "minhaSenha123",
    "dXNlcjpwYXNz",
    "sid=abcdef",
    "session=ghijkl",
    "918273",
    "privado-987",
)


def _assert_critical_markers_absent(payload: str) -> None:
    blob = payload or ""
    for marker in CODEX_MARKERS:
        assert marker not in blob
    compact = blob.replace(" ", "")
    assert "ana@example.com" not in blob
    assert "4111111111111111" not in compact
    assert "senha: abc" not in blob.lower()


def test_codex_false_negatives_do_not_reach_gemini(ctx):
    captured = {}
    client = _fake_client(captured)
    contents = "\n".join(CODEX_CRITICAL_SNIPPETS)
    cleiton_governed_generate_content(
        client,
        model="gemini-test",
        contents=contents,
        agent="julia",
        flow_type="julia_chat",
        api_key_label="test",
    )
    payload = captured["contents"]
    assert captured["calls"] == 1
    _assert_critical_markers_absent(payload)
    assert "abc" not in payload
    assert "minhaSenha123" not in payload


def test_codex_bank_and_credentials_do_not_reach_duckduckgo(monkeypatch):
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        text = "<html></html>"

    def _get(url, params=None, timeout=None, headers=None):
        seen["url"] = url
        seen["q"] = (params or {}).get("q")
        seen["calls"] = seen.get("calls", 0) + 1
        return _Resp()

    monkeypatch.setattr(web_search.requests, "get", _get)
    query = (
        "cotacao atual "
        + " ".join(CODEX_CRITICAL_SNIPPETS)
    )
    web_search.search_web_links(query)
    sent = seen.get("q") or ""
    _assert_critical_markers_absent(sent)
    assert "abc" not in sent
    assert "minhaSenha123" not in sent
    assert "1234" not in sent
    assert "cotacao" in sent.lower()


def test_codex_critical_content_does_not_reach_imagen_prompt_or_config(ctx):
    captured = {}
    client = _fake_client(captured)
    prompt = "editorial logistics " + " ".join(CODEX_CRITICAL_SNIPPETS)
    config = {
        "number_of_images": 1,
        "system_instruction": "Cookie: sid=abcdef; session=ghijkl",
    }
    cleiton_governed_generate_images(
        client,
        agent="julia",
        flow_type="julia_imagem_imagen",
        api_key_label="test",
        model="imagen-3.0-generate-002",
        prompt=prompt,
        config=config,
    )
    assert captured["calls"] == 1
    _assert_critical_markers_absent(captured.get("prompt") or "")
    cfg = captured.get("config") or {}
    assert cfg.get("number_of_images") == 1
    _assert_critical_markers_absent(str(cfg))
    assert "sid=abcdef" not in str(cfg)
    assert "session=ghijkl" not in str(cfg)


def test_config_system_instruction_critical_marker_does_not_reach_sdk(ctx):
    captured = {}
    client = _fake_client(captured)
    marker = "Authorization: Basic dXNlcjpwYXNz"
    cleiton_governed_generate_content(
        client,
        model="gemini-test",
        contents="origem São Paulo destino Curitiba peso 500 kg",
        config={
            "system_instruction": marker,
            "temperature": 0.2,
            "max_output_tokens": 128,
        },
        agent="julia",
        flow_type="julia_chat",
        api_key_label="test",
    )
    assert captured["calls"] == 1
    cfg = captured["config"]
    assert cfg["temperature"] == 0.2
    assert cfg["max_output_tokens"] == 128
    assert marker not in str(cfg)
    assert "dXNlcjpwYXNz" not in str(cfg)
    assert "São Paulo" in captured["contents"]
    assert "Curitiba" in captured["contents"]


def test_operational_logistics_entities_are_preserved_for_auditoria():
    text = (
        "Nome: Transportadora Santos destino Curitiba peso 500 kg "
        "São Paulo GRIS pedágio CIF/FOB tarifas"
    )
    result = govern_or_raise(text, purpose="auditoria_frete", agent="cleide")
    safe = result.safe_content
    assert "Transportadora Santos" in safe
    assert "Curitiba" in safe
    assert "500 kg" in safe
    assert "São Paulo" in safe
    assert "GRIS" in safe
    assert "pedágio" in safe
    assert "CIF/FOB" in safe
    assert "tarifas" in safe
    assert "Pessoa 1" not in safe


def test_purpose_changes_necessity_and_invalid_purpose_is_fail_closed(ctx):
    text = (
        "Nome: Transportadora Santos destino Curitiba peso 500 kg "
        "senha: abc cartao 4111 1111 1111 1111"
    )
    audit = govern_or_raise(text, purpose="auditoria_frete", agent="cleide")
    assert "Transportadora Santos" in audit.safe_content
    assert "Curitiba" in audit.safe_content
    assert "500 kg" in audit.safe_content
    assert "abc" not in audit.safe_content
    assert "4111 1111 1111 1111" not in audit.safe_content

    image = govern_or_raise(text, purpose="geracao_imagem", agent="julia")
    assert "Curitiba" in image.safe_content
    assert "500 kg" in image.safe_content
    assert "Transportadora Santos" not in image.safe_content
    assert "abc" not in image.safe_content
    assert "4111 1111 1111 1111" not in image.safe_content

    captured = {"calls": 0}

    class _Models:
        def generate_content(self, *, model, contents, config=None):
            captured["calls"] += 1
            return SimpleNamespace(text="leak", usage_metadata=None)

    with pytest.raises(CleitonAiGovernanceBlockedError):
        cleiton_governed_generate_content(
            SimpleNamespace(models=_Models()),
            model="m",
            contents=text,
            agent="julia",
            flow_type="julia_chat",
            api_key_label="test",
            purpose="finalidade_invalida_xyz",
        )
    assert captured["calls"] == 0


def test_two_emails_in_same_operation_get_distinct_aliases():
    payload = {"email": "a@example.com", "notes": "b@example.com"}
    result = govern_or_raise(payload, purpose="auditoria_frete", agent="cleide")
    safe = result.safe_content
    assert "a@example.com" not in str(safe)
    assert "b@example.com" not in str(safe)
    assert safe["email"] != safe["notes"]
    assert safe["email"].startswith("[EMAIL_")
    assert "[EMAIL_" in safe["notes"]


def test_files_api_rejects_self_declared_original_bytes(monkeypatch):
    captured = {}
    original = b"%PDF-1.4 original-bytes-CRITICO"

    def _upload(*, file, config):
        captured["bytes"] = file.getvalue()
        captured["calls"] = captured.get("calls", 0) + 1
        return SimpleNamespace(
            name="files/x",
            uri="https://example/files/x",
            mime_type="application/pdf",
            state="ACTIVE",
        )

    client = MagicMock()
    client.files.upload.side_effect = _upload

    forged = GovernanceResult(
        decision="allowed",
        safe_content=original,
        diagnostic_metadata={"purpose": "chat_logistico", "classifier": "cleiton_contextual_v1"},
        issued_by_cleiton=False,
    )
    blocked = gemini_files.upload_pdf_to_gemini_files_api(
        file_bytes=original,
        display_name="contrato.pdf",
        client=client,
        governance_result=forged,
    )
    assert blocked.ok is False
    assert blocked.error_summary == "original_pdf_upload_blocked_by_governance"
    client.files.upload.assert_not_called()
    assert captured == {}

    kwargs_bypass = {
        "file_bytes": original,
        "display_name": "contrato.pdf",
        "client": client,
        "authorized_representation": original,
    }
    with pytest.raises(TypeError):
        gemini_files.upload_pdf_to_gemini_files_api(**kwargs_bypass)
    client.files.upload.assert_not_called()

    legitimate = govern_or_raise(
        "origem São Paulo destino Curitiba peso 500 kg",
        purpose="chat_logistico",
        agent="julia",
    )
    assert extract_authorized_upload_bytes(legitimate) is not None
    allowed = gemini_files.upload_pdf_to_gemini_files_api(
        file_bytes=original,
        display_name="contrato.pdf",
        client=client,
        governance_result=legitimate,
    )
    assert allowed.ok is True
    assert captured.get("calls") == 1
    uploaded = captured["bytes"]
    assert uploaded != original
    assert "São Paulo".encode("utf-8") in uploaded
    assert b"original-bytes-CRITICO" not in uploaded


def test_inconsistent_minimized_result_is_fail_closed_zero_provider_calls(ctx, monkeypatch):
    captured = {"calls": 0}

    class _Models:
        def generate_content(self, *, model, contents, config=None):
            captured["calls"] += 1
            return SimpleNamespace(text="leak", usage_metadata=None)

    monkeypatch.setattr(
        "app.services.cleiton_ai_data_governance.classify_text",
        lambda *a, **k: ClassificationResult(
            decision=DECISION_MINIMIZE,
            spans=[],
            categories_detected=[],
            reason_codes=[],
            valid=True,
        ),
    )
    with pytest.raises(CleitonAiGovernanceBlockedError):
        cleiton_governed_generate_content(
            SimpleNamespace(models=_Models()),
            model="m",
            contents="origem São Paulo destino Curitiba",
            agent="julia",
            flow_type="julia_chat",
            api_key_label="test",
        )
    assert captured["calls"] == 0


def test_classifier_exception_log_omits_sensitive_marker(caplog, monkeypatch):
    marker = "SEGREDO-SINTETICO-123"

    def _boom(*a, **k):
        raise RuntimeError(marker)

    monkeypatch.setattr("app.services.cleiton_ai_data_governance.classify_text", _boom)
    with caplog.at_level("ERROR"):
        result = govern_outbound_content("hello", purpose="chat_logistico", provider="gemini")
    assert result.decision == "blocked"
    assert marker not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "classify" in caplog.text
    assert "chat_logistico" in caplog.text
