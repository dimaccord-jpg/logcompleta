"""Testes do chat analítico pós-BI da Cleide Auditoria."""
from __future__ import annotations

import importlib
import io
import os
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.cleide_audit_doc_service as audit_doc_service
import app.run_cleide_audit_chat as audit_chat
import app.run_cleide_audit_insights_chat as insights_chat
from app.cleide_audit_doc_service import (
    CLEIDE_AUDIT_CHAT_FLOW_TYPE,
    CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
)
from app.cleide_audit_insights_context import (
    ERROR_INSIGHTS_BATCH_NO_RESULTS,
    ERROR_INSIGHTS_CHAT_LOCKED,
    get_conversation_focus,
    has_minimally_valid_audit_results,
    insights_batch_scope,
    is_minimally_valid_audit_result,
    mark_insights_chat_unlocked,
    set_conversation_focus,
)
from app.cleide_audit_insights_query import (
    INTENT_ACTION_PLAN,
    INTENT_AMBIGUOUS,
    INTENT_CARRIER_NEGOTIATION_BRIEF,
    INTENT_CHARGE_VALIDITY,
    INTENT_DOCUMENT_FOLLOWUP,
    INTENT_EXECUTIVE_SUMMARY,
    INTENT_EXPLAIN_BUSINESS_IMPACT,
    INTENT_EXPLAIN_CALCULATION,
    INTENT_EXPLAIN_UNCALCULATED_REASONS,
    INTENT_MANAGEMENT_EMAIL_DRAFT,
    INTENT_OUT_OF_SCOPE,
    INTENT_OVERCHARGED,
    INTENT_PRIORITIZATION,
    INTENT_ROOT_CAUSE_HYPOTHESES,
    INTENT_SEND_EMAIL_BLOCKED,
    INTENT_TOP_DIVERGENCES,
    INTENT_UNCALCULATED_CITIES,
    INTENT_UNCALCULATED_DOCUMENTS,
    INTENT_UNDERCHARGED,
    build_analytical_package,
    build_compact_context_for_gemini,
    build_ranking,
    classify_intent,
    extract_top_n,
    format_batch_summary,
    format_calculation_explanation,
    format_chart_explanation,
    format_duplicate_document_options,
    format_executive_summary,
    format_management_email_draft,
    format_managerial_fallback,
    format_out_of_scope,
    format_ranking_response,
    format_brl,
    is_uncalculated_row,
    resolve_document_target,
    try_deterministic_response,
)
from app.services.cleide_audit_config_service import CleideAuditConfig, DEFAULT_FALLBACK_MESSAGE
from tests.cleiton_doc_fixtures import make_txt, patch_cleiton_doc_cfg, patch_cleiton_doc_store


def _default_audit_cfg(**overrides):
    defaults = {
        "chat_enabled": True,
        "upload_enabled": True,
        "chat_max_history": 10,
        "document_context_max_chars": 24000,
        "max_documents_considered": 3,
        "question_max_chars": 4000,
        "fallback_message": DEFAULT_FALLBACK_MESSAGE,
        "no_documents_behavior": "allow_guided",
        "show_documents_used": True,
        "no_hallucination_instruction_enabled": True,
        "audited_file_max_bytes": None,
        "audited_file_max_rows": 2000,
    }
    defaults.update(overrides)
    return CleideAuditConfig(**defaults)


def _patch_audit_cfg(monkeypatch, **overrides):
    cfg = _default_audit_cfg(**overrides)
    targets = [
        "app.cleide_audit_routes.get_cleide_audit_config",
        "app.cleide_audit_doc_context.get_cleide_audit_config",
        "app.run_cleide_audit_chat.get_cleide_audit_config",
        "app.run_cleide_audit_insights_chat.get_cleide_audit_config",
    ]
    for target in targets:
        monkeypatch.setattr(target, lambda _cfg=cfg: _cfg)
    return cfg


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _setup_doc_env(monkeypatch, tmp_path, **cfg_overrides):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    cfg = patch_cleiton_doc_cfg(monkeypatch, **cfg_overrides)
    monkeypatch.setattr("app.cleide_audit_doc_service.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_routes.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_doc_context.get_cleiton_doc_config", lambda: cfg)
    monkeypatch.setattr("app.cleide_audit_insights_context.get_cleiton_doc_config", lambda: cfg)
    _patch_audit_cfg(monkeypatch)
    return cfg


def _authorized(monkeypatch, web, *, authz=None):
    fake_user = SimpleNamespace(is_authenticated=True, conta_id=1, franquia_id=1, id=42)
    monkeypatch.setattr(web, "current_user", fake_user)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", fake_user)
    authz_payload = authz or {"permitido": True, "modo_operacao": "normal"}
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )
    monkeypatch.setattr(
        "app.cleide_audit_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: authz_payload,
    )


def _audit_chat(client, payload: dict):
    return client.post(
        "/api/cleide-auditoria/audit-chat",
        json=payload,
        content_type="application/json",
    )


def _document_chat(client, payload: dict):
    return client.post(
        "/api/cleide-auditoria/chat",
        json=payload,
        content_type="application/json",
    )


def _upload(client, filename: str, content: bytes, mime: str = "text/plain"):
    return client.post(
        "/api/cleide-auditoria/documents/upload",
        data={"file": (io.BytesIO(content), filename, mime)},
        content_type="multipart/form-data",
    )


def _bind_processed_batch_to_session(app, web_client, tmp_path, *, results_override=None):
    record = _base_processed_record()
    if results_override is not None:
        record["audit_batch"]["results"] = results_override
    with app.app_context():
        path = audit_doc_service._temp_table_path(record["temp_table_id"])
        audit_doc_service._write_temp_table_atomic(path, record)
    with web_client.session_transaction() as sess:
        sess[audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_ID_SESSION_KEY] = record["temp_table_id"]
        sess[audit_doc_service.CLEIDE_AUDIT_TEMP_TABLE_SOURCE_DOCS_SESSION_KEY] = ["doc-a"]
        sess[audit_doc_service.CLEIDE_AUDIT_DOC_IDS_SESSION_KEY] = ["doc-a"]
    return record


def _audit_bi_rows_from_merged(merged_rows: list[dict]) -> dict:
    return {
        "ready": True,
        "row_count": len(merged_rows),
        "rows": [
            {
                "row_index": row.get("row_index"),
                "carrier": row.get("carrier"),
                "origin_uf": row.get("origin_uf"),
                "destination_uf": row.get("destination_uf"),
                "issue_date": row.get("issue_date"),
                "charged_freight": row.get("charged_freight"),
                "expected_freight": row.get("expected_freight"),
                "divergence_value": row.get("divergence_value"),
                "status": row.get("status"),
            }
            for row in merged_rows
        ],
    }


def _unlock_processed_batch(web_client):
    return web_client.post(
        "/api/cleide-auditoria/audit-chat/unlock",
        json={},
        content_type="application/json",
    )


def _make_insights_bundle(record=None, *, merged_rows_override=None):
    from app.cleide_audit_insights_context import build_merged_rows

    record = record or _base_processed_record()
    audit_batch = record["audit_batch"]
    merged_rows = merged_rows_override if merged_rows_override is not None else build_merged_rows(audit_batch)
    return {
        "temp_table_id": record.get("temp_table_id") or "tt-insights-test",
        "audit_batch_id": audit_batch.get("audit_batch_id"),
        "processed_at": audit_batch.get("processed_at"),
        "source_file_name": audit_batch.get("source_file_name"),
        "merged_rows": merged_rows,
        "audit_bi": _audit_bi_rows_from_merged(merged_rows),
        "summary": audit_batch.get("summary"),
        "audit_diagnostics": audit_batch.get("audit_diagnostics"),
        "coverage_summary": {"row_count": 0, "sample_regions": ["SC1"], "sample_cities": ["Joinville"]},
        "needs_reprocess": False,
        "stale_reason": None,
    }


def _insights_reply_with_bundle(
    message: str,
    *,
    request_id: str = "ins-svc",
    monkeypatch=None,
    session_obj=None,
    bundle=None,
    history=None,
    cfg_overrides=None,
):
    bundle = bundle or _make_insights_bundle()

    class _FakeSession(dict):
        modified = False

    session_obj = session_obj if session_obj is not None else _FakeSession()
    mark_insights_chat_unlocked(session_obj, bundle)

    if monkeypatch is not None:
        _patch_audit_cfg(monkeypatch, **(cfg_overrides or {}))

    def _fake_load(_session, require_unlock=True):
        from app.cleide_audit_insights_context import is_insights_chat_unlocked

        if require_unlock and not is_insights_chat_unlocked(_session, bundle):
            return {
                "ok": False,
                "error_code": ERROR_INSIGHTS_CHAT_LOCKED,
                "message": "locked",
            }
        return {"ok": True, "bundle": bundle}

    import app.run_cleide_audit_insights_chat as insights_chat_module

    if monkeypatch is not None:
        monkeypatch.setattr(insights_chat_module, "load_audit_insights_bundle", _fake_load)
    else:
        insights_chat_module.load_audit_insights_bundle = _fake_load

    result = insights_chat_module.chat_cleide_audit_insights_reply(
        message,
        history or [],
        session_obj=session_obj,
        request_id=request_id,
    )
    result["_session"] = session_obj
    result["_bundle"] = bundle
    return result


def _audit_row(row_index: int, *, document_number: str, charged: str, expected: float, divergence: float) -> dict:
    return {
        "row_index": row_index,
        "document_number": document_number,
        "destination_uf": "SP",
        "destination_city": "Campinas",
        "audited_weight": "48",
        "charged_freight": charged,
        "carrier": f"Transp {row_index}",
        "origin_uf": "PR",
        "issue_date": "2026-07-07",
    }


def _result_row(row_index: int, *, document_number: str, charged: float, expected: float, divergence: float) -> dict:
    return {
        "row_index": row_index,
        "numero_documento": document_number,
        "destination_uf": "SP",
        "destination_city": "Campinas",
        "charged_freight": charged,
        "expected_freight": expected,
        "divergence_value": divergence,
        "status": "divergent" if abs(divergence) > 0.004 else "ok",
        "weight_freight": expected,
        "calculation_components": {
            "weight_freight": {"amount": expected},
            "subtotal_before_taxes": expected,
        },
        "calculation_details": "Memória simulada",
    }


def _base_processed_record() -> dict:
    normalized_rows = [
        _audit_row(1, document_number="7400455", charged="120.00", expected=100.0, divergence=20.0),
        _audit_row(2, document_number="7400455", charged="99.00", expected=100.0, divergence=-1.0),
        _audit_row(3, document_number="888", charged="50.00", expected=50.0, divergence=0.0),
        _audit_row(4, document_number="777", charged="200.00", expected=150.0, divergence=50.0),
    ]
    results = [
        _result_row(1, document_number="7400455", charged=120.0, expected=100.0, divergence=20.0),
        _result_row(2, document_number="7400455", charged=99.0, expected=100.0, divergence=-1.0),
        _result_row(3, document_number="888", charged=50.0, expected=50.0, divergence=0.0),
        _result_row(4, document_number="777", charged=200.0, expected=150.0, divergence=50.0),
    ]
    return {
        "temp_table_id": "tt-insights-test",
        "status": "needs_review",
        "version_marker": audit_doc_service.TEMP_TABLE_VERSION_MARKER,
        "edit_version": 0,
        "created_at": "2026-07-07T19:00:00+00:00",
        "updated_at": "2026-07-07T19:00:00+00:00",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "source_documents": ["doc-a"],
        "freight_tables": [],
        "freight_routes": [],
        "accessorial_fees": [],
        "coverage_table": {"rows": []},
        "audit_batch": {
            "audit_batch_id": "batch-insights",
            "temp_table_id": "tt-insights-test",
            "status": "processed",
            "source_file_name": "auditado.xlsx",
            "normalized_rows": normalized_rows,
            "results": results,
            "summary": {"total_rows": 4, "divergent": 3, "processed_rows": 4},
            "audit_diagnostics": {"groups": [{"label": "Tabela divergente", "count": 2}]},
            "processed_at": "2026-07-07T20:00:00+00:00",
        },
    }


def _sample_bundle(**overrides):
    rows = [
        {
            "row_index": 1,
            "document_number": "7400455",
            "carrier": "Transp A",
            "destination_uf": "SP",
            "charged_freight": 120.0,
            "expected_freight": 100.0,
            "divergence_value": 20.0,
            "status": "divergent",
            "calculation_components": {
                "weight_freight": {"amount": 90.0},
                "subtotal_before_taxes": 100.0,
                "tax_total": 0.0,
            },
            "weight_freight": 90.0,
            "freight_value_amount": 10.0,
            "route_toll_amount": 0.0,
            "accessorial_fees_amount": 0.0,
            "calculation_details": "Faixa SP-Interior 1",
        },
        {
            "row_index": 2,
            "document_number": "7400455",
            "carrier": "Transp B",
            "destination_uf": "RJ",
            "charged_freight": 99.0,
            "expected_freight": 100.0,
            "divergence_value": -1.0,
            "status": "divergent",
            "calculation_components": {},
        },
        {
            "row_index": 3,
            "document_number": "888",
            "carrier": "Transp C",
            "destination_uf": "MG",
            "charged_freight": 50.0,
            "expected_freight": 50.0,
            "divergence_value": 0.0,
            "status": "ok",
            "calculation_components": {},
        },
        {
            "row_index": 4,
            "document_number": "777",
            "carrier": "Transp D",
            "destination_uf": "PR",
            "charged_freight": 200.0,
            "expected_freight": 150.0,
            "divergence_value": 50.0,
            "status": "divergent",
            "calculation_components": {},
        },
    ]
    bundle = {
        "source_file_name": "auditado.xlsx",
        "merged_rows": rows,
        "summary": {"total_rows": 4, "divergent": 3},
        "audit_diagnostics": {"groups": [{"label": "Tabela divergente", "count": 2}]},
        "needs_reprocess": False,
        "stale_reason": None,
    }
    bundle.update(overrides)
    return bundle


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    web.app.config["TESTING"] = True
    client = web.app.test_client()
    client._flask_app = web.app
    client._ctx = ctx
    client._tmp_path = tmp_path
    return client


def test_insights_endpoint_registered(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/cleide-auditoria/audit-chat" in rules
    assert "/api/cleide-auditoria/audit-chat/unlock" in rules


def test_audit_chat_rejects_processed_batch_without_backend_unlock(web_client):
    _bind_processed_batch_to_session(web_client._flask_app, web_client, web_client._tmp_path)
    resp = _audit_chat(web_client, {"message": "maiores divergências", "history": [], "request_id": "ins-lock-1"})
    assert resp.status_code == 403
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error_code"] == ERROR_INSIGHTS_CHAT_LOCKED


def test_audit_chat_works_after_backend_unlock(web_client):
    _bind_processed_batch_to_session(web_client._flask_app, web_client, web_client._tmp_path)
    unlock_resp = _unlock_processed_batch(web_client)
    assert unlock_resp.status_code == 200
    assert unlock_resp.get_json()["unlocked"] is True
    resp = _audit_chat(web_client, {"message": "resumo do lote", "history": [], "request_id": "ins-lock-2"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_audit_chat_without_processed_batch_is_rejected(web_client):
    resp = _audit_chat(web_client, {"message": "maiores divergências", "history": [], "request_id": "ins-1"})
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["ok"] is False
    assert "lote" in body["message"].lower() or "auditoria" in body["message"].lower()


def test_audit_chat_anonymous_receives_401(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    anon = SimpleNamespace(is_authenticated=False)
    monkeypatch.setattr(web, "current_user", anon)
    monkeypatch.setattr("app.cleide_audit_routes.current_user", anon)
    resp = _audit_chat(web.app.test_client(), {"message": "resumo", "history": [], "request_id": "ins-2"})
    assert resp.status_code == 401


def test_document_calculation_explanation_uses_persisted_components():
    bundle = _sample_bundle()
    target = resolve_document_target(bundle, "CT-e 7400455")
    assert target["kind"] == "duplicate"
    answer, _, deterministic = try_deterministic_response(
        bundle,
        INTENT_EXPLAIN_CALCULATION,
        "linha 1",
        visual_focus=None,
    )
    assert deterministic is True
    assert answer is not None
    assert "R$" in answer
    assert "90" in answer or "100" in answer
    assert "regra de ouro" not in answer.lower()


def test_document_not_found_does_not_hallucinate():
    bundle = _sample_bundle()
    answer, _, deterministic = try_deterministic_response(
        bundle,
        INTENT_EXPLAIN_CALCULATION,
        "documento 000999",
        visual_focus=None,
    )
    assert deterministic is True
    assert "Não encontrei" in answer


def test_duplicate_document_requests_clarification():
    bundle = _sample_bundle()
    answer = format_duplicate_document_options(bundle, bundle["merged_rows"][:2], "7400455")
    assert "2 linhas" in answer
    assert "Transp A" in answer
    assert "Transp B" in answer
    assert "esclareça" in answer.lower()


def test_top_n_variable_quantity():
    assert extract_top_n("liste 6 maiores divergências") == 6
    assert extract_top_n("maiores divergências") == 5
    assert extract_top_n("Liste as três maiores divergencia a maior") == 3
    assert extract_top_n("quero as 3 maiores divergências") == 3
    assert extract_top_n("top 12 cobranças a mais") == 12
    assert extract_top_n("me mostre 4") == 4


def test_ranking_top_divergences_uses_absolute_value():
    bundle = _sample_bundle()
    ranked = build_ranking(bundle, INTENT_TOP_DIVERGENCES, limit=3)
    divergences = [abs(row["divergence_value"]) for row in ranked]
    assert divergences == sorted(divergences, reverse=True)
    assert ranked[0]["document_number"] == "777"


def test_overcharged_filters_positive_only():
    bundle = _sample_bundle()
    ranked = build_ranking(bundle, INTENT_OVERCHARGED, limit=10)
    assert all(row["divergence_value"] > 0 for row in ranked)
    assert len(ranked) == 2


def test_undercharged_filters_negative_only():
    bundle = _sample_bundle()
    ranked = build_ranking(bundle, INTENT_UNDERCHARGED, limit=10)
    assert all(row["divergence_value"] < 0 for row in ranked)
    answer, _, deterministic = try_deterministic_response(
        bundle,
        INTENT_UNDERCHARGED,
        "cobranças a menor",
        visual_focus=None,
    )
    assert deterministic is True
    assert "cobrado a menor" in answer.lower()


def test_smallest_divergences_excludes_zero_by_default():
    bundle = _sample_bundle()
    ranked = build_ranking(bundle, "menores_divergencias", limit=10)
    assert all(abs(row["divergence_value"]) > 0.004 for row in ranked)
    assert all(row["document_number"] != "888" for row in ranked)


def test_chart_explanation_carrier(monkeypatch):
    result = _insights_reply_with_bundle(
        "explique o gráfico por transportadora",
        request_id="ins-chart-1",
        monkeypatch=monkeypatch,
    )
    assert result.get("error") is None
    assert result["flow_type"] == CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE
    assert "transportadora" in result["answer"].lower()


def test_chart_explanation_uf(monkeypatch):
    result = _insights_reply_with_bundle(
        "explique impacto por UF destino",
        request_id="ins-chart-2",
        monkeypatch=monkeypatch,
    )
    assert result.get("error") is None
    assert "uf" in result["answer"].lower()


def test_objective_answers_omit_golden_rule_label():
    bundle = _sample_bundle()
    summary = format_batch_summary(bundle)
    ranking = format_ranking_response(
        bundle,
        INTENT_TOP_DIVERGENCES,
        build_ranking(bundle, INTENT_TOP_DIVERGENCES, limit=3),
        limit=3,
    )
    chart = format_chart_explanation(
        bundle,
        message="explique o gráfico por transportadora",
        visual_focus=None,
    )
    calc = format_calculation_explanation(bundle, bundle["merged_rows"][0])
    for answer in (summary, ranking, chart, calc):
        assert "regra de ouro" not in answer.lower()
        assert "regra de ouro:" not in answer.lower()


def test_out_of_scope_is_refused():
    bundle = _sample_bundle()
    assert classify_intent("cancelar assinatura stripe") == "fora_de_escopo"
    answer = format_out_of_scope(bundle)
    lowered = answer.lower()
    assert "não consigo executar essa ação pela plataforma" in lowered
    assert "texto" in lowered or "análise" in lowered or "analise" in lowered or "plano" in lowered


def test_gemini_flow_type_for_causes(monkeypatch):
    capture: dict = {}

    class _Resp:
        text = "Hipótese: diferença de tabela. A decisão final é sua."

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["agent"] = agent
        capture["flow_type"] = flow_type
        return _Resp()

    monkeypatch.setattr(insights_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(insights_chat, "_get_client", lambda: object())
    result = _insights_reply_with_bundle(
        "quais possíveis causas de divergência?",
        request_id="ins-causes",
        monkeypatch=monkeypatch,
    )
    assert result.get("error") is None
    assert capture["agent"] == "cleide"
    assert capture["flow_type"] == CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE


def test_deterministic_response_does_not_debit_operational_lines(monkeypatch):
    billing_mock = MagicMock()
    monkeypatch.setattr(
        "app.services.cleiton_upload_billing_service.apropriar_billing_cleide_operational_flow",
        billing_mock,
    )
    result = _insights_reply_with_bundle(
        "maiores divergências",
        request_id="ins-det",
        monkeypatch=monkeypatch,
    )
    assert result.get("error") is None
    assert result["deterministic"] is True
    billing_mock.assert_not_called()


def test_documental_chat_still_works(web_client, monkeypatch):
    capture: dict = {}

    class _Resp:
        text = "Resposta documental simulada."

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["flow_type"] = flow_type
        return _Resp()

    monkeypatch.setattr(audit_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(audit_chat, "_get_client", lambda: object())
    _upload(web_client, "nota.txt", make_txt("evidencia documental"))
    resp = _document_chat(
        web_client,
        {"message": "analise o documento", "history": [], "request_id": "doc-chat-1"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["flow_type"] == CLEIDE_AUDIT_CHAT_FLOW_TYPE
    assert capture["flow_type"] == CLEIDE_AUDIT_CHAT_FLOW_TYPE


def test_julia_route_unaffected(app, ctx, monkeypatch, tmp_path):
    with app.app_context():
        _setup_doc_env(monkeypatch, tmp_path)
    web = _load_web_module()
    rules = {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/julia/documents/upload" in rules
    assert "/api/cleide-auditoria/audit-chat" in rules


def test_insights_idempotency_scoped_by_batch():
    class _FakeSession(dict):
        modified = False

    session_obj = _FakeSession()
    payload_a = {
        "answer": "Resumo lote A.",
        "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
        "deterministic": True,
    }
    payload_b = {
        "answer": "Resumo lote B.",
        "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
        "deterministic": True,
    }
    insights_chat.cache_insights_chat_response(session_obj, "ins-idem-1", payload_a, batch_scope="scope-a")
    insights_chat.cache_insights_chat_response(session_obj, "ins-idem-1", payload_b, batch_scope="scope-b")
    cached_a = insights_chat.get_cached_insights_chat_response(session_obj, "ins-idem-1", batch_scope="scope-a")
    cached_b = insights_chat.get_cached_insights_chat_response(session_obj, "ins-idem-1", batch_scope="scope-b")
    assert cached_a["answer"] == "Resumo lote A."
    assert cached_b["answer"] == "Resumo lote B."
    assert insights_chat.get_cached_insights_chat_response(session_obj, "ins-idem-1", batch_scope="scope-c") is None


def test_processed_batch_route_returns_ranking(monkeypatch):
    result = _insights_reply_with_bundle(
        "liste 2 maiores divergências",
        request_id="ins-rank",
        monkeypatch=monkeypatch,
    )
    assert result.get("error") is None
    answer = result["answer"]
    assert "1." in answer
    assert "2." in answer
    assert "3." not in answer


def test_chart_explanation_uses_audit_bi_rows(monkeypatch):
    from app.cleide_audit_insights_bi import bi_rows_from_bundle, build_financial_metrics
    from app.cleide_audit_insights_query import format_chart_explanation

    merged = _sample_bundle()["merged_rows"]
    bundle = _sample_bundle(
        audit_bi=_audit_bi_rows_from_merged(merged),
        temp_table_id="tt-1",
        audit_batch_id="b-1",
        processed_at="2026-07-07T20:00:00+00:00",
    )
    rows, from_bi = bi_rows_from_bundle(bundle)
    assert from_bi is True
    answer = format_chart_explanation(bundle, message="explique gráfico por transportadora", visual_focus=None)
    metrics = build_financial_metrics(rows)
    assert format_brl(metrics["overcharged"]).replace(" ", "") in answer.replace(" ", "")


def test_temporal_chart_is_chronological(monkeypatch):
    from app.cleide_audit_insights_bi import aggregate_by_date_chronological, bi_rows_from_bundle

    rows = [
        {
            "row_index": 1,
            "issue_date": "2026-07-10",
            "charged_freight": 100.0,
            "expected_freight": 90.0,
            "divergence_value": 10.0,
        },
        {
            "row_index": 2,
            "issue_date": "2026-07-01",
            "charged_freight": 50.0,
            "expected_freight": 40.0,
            "divergence_value": 10.0,
        },
    ]
    bundle = {"audit_bi": {"ready": True, "rows": rows}, "merged_rows": rows}
    bi_rows, _ = bi_rows_from_bundle(bundle)
    ordered = aggregate_by_date_chronological(bi_rows)
    assert [item["data"] for item in ordered] == ["2026-07-01", "2026-07-10"]


def test_pareto_matches_bi_overcharge_only(monkeypatch):
    from app.cleide_audit_insights_bi import build_overcharge_pareto

    rows = [
        {"carrier": "A", "charged_freight": 120.0, "expected_freight": 100.0, "divergence_value": 20.0},
        {"carrier": "B", "charged_freight": 90.0, "expected_freight": 100.0, "divergence_value": -10.0},
        {"carrier": "C", "charged_freight": 110.0, "expected_freight": 100.0, "divergence_value": 10.0},
    ]
    pareto = build_overcharge_pareto(rows, "carrier")
    assert len(pareto) == 2
    assert pareto[0]["chave"] == "A"
    assert pareto[0]["percentual_acumulado"] < pareto[1]["percentual_acumulado"]
    assert abs(pareto[1]["percentual_acumulado"] - 100.0) < 0.01


def test_calculation_explanation_includes_gris_excess_and_ignored_fees():
    from app.cleide_audit_insights_query import format_calculation_explanation

    row = {
        "row_index": 1,
        "document_number": "0012345",
        "weight_freight": 80.0,
        "freight_value_amount": 5.0,
        "expected_freight": 100.0,
        "charged_freight": 120.0,
        "divergence_value": 20.0,
        "status": "divergent",
        "calculation_components": {
            "weight_freight": {"amount": 80.0, "basis": "48 kg", "details": "Faixa até 50 kg + excedente"},
            "excess": {"amount": 10.0, "details": "2 kg excedentes"},
            "accessorial_fees": [
                {
                    "name": "GRIS",
                    "amount": 3.0,
                    "details": "0,3% sobre NF",
                    "minimum_applied": True,
                    "minimum_amount": 3.0,
                }
            ],
            "ignored_accessorial_fees": [
                {"name": "Taxa X", "reason_code": "missing_invoice_value", "ignored_reason": "valor NF ausente"}
            ],
            "subtotal_before_taxes": 93.0,
            "tax_components": [{"tax_type": "ICMS", "amount": 7.0}],
            "tax_total": 7.0,
        },
    }
    answer = format_calculation_explanation(_sample_bundle(), row)
    assert "GRIS" in answer
    assert "excedente" in answer.lower()
    assert "Taxa X" in answer
    assert "ICMS" in answer


def test_charge_validity_question_is_prudent_not_out_of_scope():
    bundle = _sample_bundle()
    assert classify_intent("Essa cobrança está errada?") == INTENT_CHARGE_VALIDITY
    answer, _, deterministic = try_deterministic_response(
        bundle,
        INTENT_CHARGE_VALIDITY,
        "Essa cobrança está errada no documento 777",
        visual_focus=None,
    )
    assert deterministic is True
    assert "não equivale automaticamente" in answer.lower()
    assert "regra de ouro" not in answer.lower()
    assert "validação" in answer.lower() or "confirmação final" in answer.lower()
    assert "indevid" not in answer.lower() or "não equivale" in answer.lower()


def test_soften_gemini_output_rewrites_categorical_language():
    raw = "A transportadora cobrou indevidamente. Está errado. A decisão é cancelar."
    softened = insights_chat.soften_insights_gemini_output(raw)
    assert "indevidamente" not in softened.lower() or "acima do esperado" in softened.lower()
    assert "regra de ouro" not in softened.lower()
    assert (
        "análise preliminar" in softened.lower()
        or "validação" in softened.lower()
        or "recomendo validar" in softened.lower()
    )


def test_document_number_preserves_leading_zeros():
    bundle = _sample_bundle()
    bundle["merged_rows"].append(
        {
            "row_index": 9,
            "document_number": "000123",
            "carrier": "Transp Z",
            "destination_uf": "SP",
            "charged_freight": 10.0,
            "expected_freight": 10.0,
            "divergence_value": 0.0,
            "status": "ok",
            "calculation_components": {},
        }
    )
    target = resolve_document_target(bundle, "documento 000123")
    assert target["kind"] == "single"
    assert target["rows"][0]["document_number"] == "000123"


def test_ranking_respects_top_50_cap():
    bundle = _sample_bundle()
    extra_rows = []
    for index in range(60):
        extra_rows.append(
            {
                "row_index": 100 + index,
                "document_number": str(1000 + index),
                "carrier": "Bulk",
                "destination_uf": "SP",
                "charged_freight": 100.0 + index,
                "expected_freight": 50.0,
                "divergence_value": 50.0 + index,
                "status": "divergent",
                "calculation_components": {},
            }
        )
    bundle["merged_rows"].extend(extra_rows)
    ranked = build_ranking(bundle, INTENT_TOP_DIVERGENCES, limit=50)
    assert len(ranked) == 50


def test_is_minimally_valid_audit_result_rejects_malformed():
    assert is_minimally_valid_audit_result(None) is False
    assert is_minimally_valid_audit_result("texto") is False
    assert is_minimally_valid_audit_result({}) is False
    assert has_minimally_valid_audit_results([None]) is False
    assert has_minimally_valid_audit_results(["texto"]) is False
    assert has_minimally_valid_audit_results([{}]) is False
    assert has_minimally_valid_audit_results([None, {}]) is False


def test_unlock_rejects_results_with_none(web_client):
    _bind_processed_batch_to_session(
        web_client._flask_app,
        web_client,
        web_client._tmp_path,
        results_override=[None],
    )
    resp = _unlock_processed_batch(web_client)
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error_code"] == ERROR_INSIGHTS_BATCH_NO_RESULTS


def test_unlock_rejects_malformed_result(web_client):
    _bind_processed_batch_to_session(
        web_client._flask_app,
        web_client,
        web_client._tmp_path,
        results_override=[{"foo": "bar"}],
    )
    resp = _unlock_processed_batch(web_client)
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == ERROR_INSIGHTS_BATCH_NO_RESULTS


def test_unlock_accepts_minimally_valid_result(web_client):
    _bind_processed_batch_to_session(
        web_client._flask_app,
        web_client,
        web_client._tmp_path,
        results_override=[{"row_index": 1, "charged_freight": 100.0, "expected_freight": 90.0, "status": "divergent"}],
    )
    resp = _unlock_processed_batch(web_client)
    assert resp.status_code == 200
    assert resp.get_json()["unlocked"] is True


def test_audit_chat_rejects_malformed_results_without_gemini(web_client, monkeypatch):
    gemini_mock = MagicMock()
    monkeypatch.setattr(insights_chat, "cleiton_governed_generate_content", gemini_mock)
    _bind_processed_batch_to_session(
        web_client._flask_app,
        web_client,
        web_client._tmp_path,
        results_override=[None],
    )
    unlock_resp = _unlock_processed_batch(web_client)
    assert unlock_resp.status_code == 409
    resp = _audit_chat(web_client, {"message": "resumo do lote", "history": [], "request_id": "ins-bad-results"})
    assert resp.status_code == 409
    assert resp.get_json()["error_code"] == ERROR_INSIGHTS_BATCH_NO_RESULTS
    gemini_mock.assert_not_called()


def test_temporal_chart_groups_iso_timestamp_to_date():
    from app.cleide_audit_insights_bi import aggregate_by_date_chronological, normalize_issue_date

    assert normalize_issue_date("2026-07-01") == "2026-07-01"
    assert normalize_issue_date("2026-07-01T23:00:00+00:00") == "2026-07-01"
    assert normalize_issue_date("invalid-date") == ""

    rows = [
        {
            "row_index": 1,
            "issue_date": "2026-07-01T23:00:00+00:00",
            "charged_freight": 100.0,
            "expected_freight": 90.0,
            "divergence_value": 10.0,
        },
        {
            "row_index": 2,
            "issue_date": "2026-07-02T01:00:00+00:00",
            "charged_freight": 50.0,
            "expected_freight": 40.0,
            "divergence_value": 10.0,
        },
    ]
    ordered = aggregate_by_date_chronological(rows)
    assert [item["data"] for item in ordered] == ["2026-07-01", "2026-07-02"]


def test_soften_gemini_output_rewrites_extended_categorical_language():
    samples = [
        "A transportadora fraudou neste lote.",
        "A cobrança é ilícita e deve ser cancelada.",
        "A empresa é culpada pela divergência.",
        "Confirmo que está errado e a decisão é cancelar.",
    ]
    for raw in samples:
        softened = insights_chat.soften_insights_gemini_output(raw)
        lowered = softened.lower()
        assert "regra de ouro" not in lowered
        assert "análise preliminar" in lowered or "indício" in lowered or "recomendo validar" in lowered
        assert "fraudou" not in lowered
        assert "ilícita" not in lowered
        assert "culpada" not in lowered

    leaked = insights_chat.soften_insights_gemini_output(
        "Há divergência no lote.\n\n---\n*Regra de ouro: eu apresento fatos, mas a decisão final é sua.*"
    )
    assert "regra de ouro" not in leaked.lower()
    assert "divergência" in leaked.lower()


def test_calculation_explanation_extracts_excess_from_weight_freight_details():
    from app.cleide_audit_insights_query import format_calculation_explanation

    row = {
        "row_index": 1,
        "document_number": "0012345",
        "expected_freight": 240.76,
        "charged_freight": 250.0,
        "divergence_value": 9.24,
        "status": "divergent",
        "calculation_components": {
            "weight_freight": {
                "amount": 240.76,
                "basis": "range_plus_excess_per_kg",
                "details": "Faixa até 100 kg + excedente por kg · 406,88 kg excedentes · R$ 0,45/kg",
            },
            "subtotal_before_taxes": 240.76,
        },
    }
    answer = format_calculation_explanation(_sample_bundle(), row)
    assert "Excedente de peso" in answer
    assert "100 kg" in answer
    assert "406,88 kg" in answer or "406.88 kg" in answer
    assert "0,45/kg" in answer or "0.45/kg" in answer
    assert "range_plus_excess_per_kg" not in answer
    assert "faixa fixa + excedente por kg" in answer.lower()


def test_insights_cache_respects_max_entries_and_isolates_documental():
    from app.run_cleide_audit_chat import CHAT_IDEMPOTENCY_CACHE_SESSION_KEY, cache_chat_response

    class _FakeSession(dict):
        modified = False

    session_obj = _FakeSession()
    max_entries = insights_chat.INSIGHTS_CHAT_CACHE_MAX_ENTRIES
    payload = {
        "answer": "Resposta cacheada.",
        "flow_type": CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE,
        "deterministic": True,
    }
    for index in range(max_entries + 5):
        insights_chat.cache_insights_chat_response(
            session_obj,
            f"ins-req-{index}",
            payload,
            batch_scope=f"scope-{index}",
        )

    cache = session_obj[insights_chat.INSIGHTS_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY]
    assert len(cache) == max_entries
    assert insights_chat.get_cached_insights_chat_response(session_obj, "ins-req-0", batch_scope="scope-0") is None
    assert (
        insights_chat.get_cached_insights_chat_response(
            session_obj,
            f"ins-req-{max_entries + 4}",
            batch_scope=f"scope-{max_entries + 4}",
        )
        is not None
    )

    cache_chat_response(
        session_obj,
        "doc-req-1",
        {"answer": "Resposta documental.", "flow_type": CLEIDE_AUDIT_CHAT_FLOW_TYPE},
    )
    assert CHAT_IDEMPOTENCY_CACHE_SESSION_KEY in session_obj
    assert len(session_obj[CHAT_IDEMPOTENCY_CACHE_SESSION_KEY]) == 1
    assert len(session_obj[insights_chat.INSIGHTS_CHAT_IDEMPOTENCY_CACHE_SESSION_KEY]) == max_entries


def test_prepare_email_classifies_as_management_email_draft():
    assert classify_intent("Prepare um e-mail para meus chefes") == INTENT_MANAGEMENT_EMAIL_DRAFT


def test_send_email_remains_out_of_scope():
    assert classify_intent("Envie um e-mail") == INTENT_SEND_EMAIL_BLOCKED
    assert classify_intent("Prepare e envie um e-mail para a diretoria") == INTENT_SEND_EMAIL_BLOCKED
    assert classify_intent("Prepare e envie") == INTENT_OUT_OF_SCOPE


def test_management_email_draft_is_minuta_not_send(monkeypatch):
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Prepare um e-mail para meus chefes",
        request_id="ins-email-1",
        monkeypatch=monkeypatch,
    )
    answer = (result.get("answer") or "").lower()
    assert result.get("error") is None
    assert "sugestão" in answer or "minuta" in answer or "claro" in answer
    assert "e-mail enviado" not in answer
    assert "enviei o e-mail com sucesso" not in answer
    assert "não consigo enviar o e-mail pela plataforma" not in answer


def test_executive_summary_interprets_not_just_kpis(monkeypatch):
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Faça um resumo executivo",
        request_id="ins-exec-1",
        monkeypatch=monkeypatch,
    )
    answer = (result.get("answer") or "").lower()
    assert classify_intent("Faça um resumo executivo") == INTENT_EXECUTIVE_SUMMARY
    assert "leitura gerencial" in answer or "concentra" in answer
    assert "confiança" in answer or "confianca" in answer
    assert "priorit" in answer or "próximos passos" in answer or "proximos passos" in answer
    assert "impacto" in answer


def test_action_plan_allowed_and_prioritizes_by_impact(monkeypatch):
    assert classify_intent("Monte um plano de ação") == INTENT_ACTION_PLAN
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Monte um plano de ação",
        request_id="ins-plan-1",
        monkeypatch=monkeypatch,
    )
    answer = (result.get("answer") or "").lower()
    assert result.get("error") is None
    assert "plano de ação" in answer or "prioriz" in answer
    assert "impacto" in answer
    assert "777" in result["answer"]  # maior divergência absoluta no fixture


def test_carrier_negotiation_brief(monkeypatch):
    assert classify_intent("O que devo negociar com a transportadora?") == INTENT_CARRIER_NEGOTIATION_BRIEF
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "O que devo negociar com a transportadora?",
        request_id="ins-neg-1",
        monkeypatch=monkeypatch,
    )
    answer = (result.get("answer") or "").lower()
    assert "briefing" in answer or "negocia" in answer
    assert "memória de cálculo" in answer or "memoria de calculo" in answer or "cobrado" in answer


def test_business_impact_plain_language_allowed(monkeypatch):
    assert classify_intent("Explique o impacto financeiro em linguagem simples") == INTENT_EXPLAIN_BUSINESS_IMPACT
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Explique o impacto financeiro em linguagem simples",
        request_id="ins-impact-1",
        monkeypatch=monkeypatch,
    )
    answer = (result.get("answer") or "").lower()
    assert result.get("error") is None
    assert "impacto" in answer
    assert "r$" in answer


def test_urgent_documents_prioritization_uses_real_docs(monkeypatch):
    assert classify_intent("Quais documentos merecem revisão urgente?") == INTENT_PRIORITIZATION
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Quais documentos merecem revisão urgente?",
        request_id="ins-prio-1",
        monkeypatch=monkeypatch,
    )
    answer = result.get("answer") or ""
    assert "777" in answer
    assert "7400455" in answer


def test_hypotheses_use_only_present_signals(monkeypatch):
    assert classify_intent("Quais hipóteses são mais prováveis?") == INTENT_ROOT_CAUSE_HYPOTHESES
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Quais hipóteses são mais prováveis?",
        request_id="ins-hyp-1",
        monkeypatch=monkeypatch,
    )
    answer = (result.get("answer") or "").lower()
    assert "hipótese" in answer or "hipotese" in answer
    assert "tabela divergente" in answer  # diagnóstico do fixture
    assert "fraude" not in answer
    assert "culp" not in answer
    assert "ilícit" not in answer and "ilicit" not in answer


def test_managerial_answers_avoid_categorical_guilt():
    bundle = _sample_bundle()
    package = build_analytical_package(bundle)
    forbidden = (
        r"\bé fraude\b",
        r"\bhouve fraude\b",
        r"\bculpada\b",
        r"\bilicitude\b",
        r"\bcom certeza (?:é|esta|está) indevid",
        r"\bconfirmo (?:a )?cobrança indevida\b",
    )
    import re

    for intent in (
        INTENT_EXECUTIVE_SUMMARY,
        INTENT_MANAGEMENT_EMAIL_DRAFT,
        INTENT_ACTION_PLAN,
        INTENT_ROOT_CAUSE_HYPOTHESES,
        INTENT_CARRIER_NEGOTIATION_BRIEF,
    ):
        answer = format_managerial_fallback(bundle, intent, package=package).lower()
        assert "fraude" not in answer
        assert "culpada" not in answer
        assert "ilicitude" not in answer
        for pattern in forbidden:
            assert re.search(pattern, answer) is None, (intent, pattern)


def test_gemini_context_excludes_all_rows():
    bundle = _sample_bundle()
    context = build_compact_context_for_gemini(bundle, INTENT_EXECUTIVE_SUMMARY)
    serialized = str(context)
    assert context["safety"]["includes_all_rows"] is False
    assert context["safety"]["includes_full_spreadsheet"] is False
    assert "merged_rows" not in context
    assert "analytical_package" in context
    package = context["analytical_package"]
    evidence_docs = package.get("top_documents_by_absolute_impact") or []
    assert len(evidence_docs) <= 5
    assert package.get("full_row_count_excluded") == len(bundle["merged_rows"])
    assert "calculation_components" not in serialized or "component_keys" in serialized


def test_new_gemini_intents_use_insights_flow_type(monkeypatch):
    capture: dict = {}

    class _Resp:
        text = (
            "Sugestão de minuta — revise antes do envio.\n"
            "Há indícios de divergência que merecem validação."
        )

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["flow_type"] = flow_type
        capture["agent"] = agent
        capture["has_all_rows"] = "merged_rows" in contents or '"row_index": 3' in contents and contents.count("row_index") > 10
        return _Resp()

    monkeypatch.setattr(insights_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(insights_chat, "_get_client", lambda: object())
    result = _insights_reply_with_bundle(
        "Prepare um e-mail para meus chefes",
        request_id="ins-email-gemini",
        monkeypatch=monkeypatch,
    )
    assert result.get("error") is None
    assert capture["agent"] == "cleide"
    assert capture["flow_type"] == CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE
    assert result["deterministic"] is False


def test_managerial_path_does_not_debit_operational_lines(monkeypatch):
    billing_mock = MagicMock()
    monkeypatch.setattr(
        "app.services.cleiton_upload_billing_service.apropriar_billing_cleide_operational_flow",
        billing_mock,
    )
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Faça um resumo executivo",
        request_id="ins-exec-billing",
        monkeypatch=monkeypatch,
    )
    assert result.get("error") is None
    billing_mock.assert_not_called()


def test_existing_rankings_and_filters_still_work():
    bundle = _sample_bundle()
    assert classify_intent("maiores divergências") == INTENT_TOP_DIVERGENCES
    ranked = build_ranking(bundle, INTENT_TOP_DIVERGENCES, limit=3)
    answer, rows, deterministic = try_deterministic_response(
        bundle,
        INTENT_TOP_DIVERGENCES,
        "maiores divergências",
    )
    assert deterministic is True
    assert rows is not None
    assert abs(_row_div(ranked[0])) >= abs(_row_div(ranked[-1]))
    assert "R$" in (answer or "")


def _row_div(row: dict) -> float:
    return float(row.get("divergence_value") or 0)


def test_upload_reprocess_subscription_and_send_still_blocked():
    hard_blocked = [
        "faça upload da planilha",
        "reprocesse a auditoria",
        "contratar plano",
        "alterar assinatura",
        "cancelar plano",
        "Prepare e envie",
    ]
    for message in hard_blocked:
        assert classify_intent(message) in {INTENT_OUT_OF_SCOPE, INTENT_SEND_EMAIL_BLOCKED}, message
    assert classify_intent("dispare o comunicado") == INTENT_SEND_EMAIL_BLOCKED
    assert classify_intent("Envie um e-mail") == INTENT_SEND_EMAIL_BLOCKED
    bundle = _sample_bundle()
    answer = format_out_of_scope(bundle).lower()
    assert "não consigo executar" in answer or "nao consigo executar" in answer


def test_executive_summary_formatter_has_interpretation():
    bundle = _sample_bundle()
    answer = format_executive_summary(bundle).lower()
    assert "leitura gerencial" in answer
    assert "concentra" in answer
    assert "confiança" in answer or "confianca" in answer
    assert "próximos passos" in answer or "proximos passos" in answer


def test_email_formatter_uses_minuta_title():
    bundle = _sample_bundle()
    answer = format_management_email_draft(bundle, user_message="Prepare um e-mail executivo")
    lowered = answer.lower()
    assert "minuta" in lowered or "sugestão" in lowered or "sugestao" in lowered
    assert "assunto:" in lowered
    assert "claro" in lowered


def test_executive_email_does_not_end_with_cleide_identity(monkeypatch):
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Prepare um e-mail executivo para eu enviar à diretoria.",
        request_id="sig-det-1",
        monkeypatch=monkeypatch,
    )
    answer = result["answer"].rstrip()
    tail = answer[-220:].lower()
    assert not re.search(r"(?m)^\s*cleide\s*$", answer, flags=re.IGNORECASE)
    assert "analista de frete experiente" not in tail
    assert not re.search(r"(?m)^\s*agentefrete\s*$", answer, flags=re.IGNORECASE)
    assert "[seu nome]" in answer.lower()


def test_sanitize_cleide_sender_signature_full_block():
    raw = (
        "Claro. Segue a minuta.\n\n"
        "**Assunto:** Auditoria\n\n"
        "Prezados,\n\n"
        "Segue o resumo.\n\n"
        "Atenciosamente,\n\n"
        "Cleide\n"
        "Analista de Frete Experiente\n"
        "AgenteFrete"
    )
    cleaned = insights_chat.sanitize_cleide_sender_signature(raw)
    assert cleaned.rstrip().endswith("[Seu nome]")
    assert not re.search(r"(?m)^\s*Cleide\s*$", cleaned)
    assert "Analista de Frete Experiente" not in cleaned
    assert not re.search(r"(?m)^\s*AgenteFrete\s*$", cleaned)
    assert "Segue o resumo." in cleaned


def test_sanitize_cleide_sender_signature_markdown():
    raw = (
        "Minuta pronta.\n\n"
        "Atenciosamente,\n\n"
        "**Cleide**\n"
        "**Analista de Frete Experiente**\n"
        "**AgenteFrete**"
    )
    cleaned = insights_chat.sanitize_cleide_sender_signature(raw)
    assert "[Seu nome]" in cleaned
    assert "**Cleide**" not in cleaned
    assert "Analista de Frete Experiente" not in cleaned


def test_sanitize_preserves_body_mentions_of_cleide():
    raw = (
        "A Cleide analisou o lote da AgenteFrete e priorizou as divergências.\n\n"
        "Próximo passo: revisar o top 3."
    )
    cleaned = insights_chat.sanitize_cleide_sender_signature(raw)
    assert "A Cleide analisou" in cleaned
    assert "AgenteFrete" in cleaned
    assert cleaned == raw


def test_mock_gemini_signature_is_sanitized_and_cached(monkeypatch):
    class _Resp:
        text = (
            "Claro. Segue uma sugestão de minuta executiva:\n\n"
            "**Assunto:** Auditoria de frete\n\n"
            "Prezados,\n\n"
            "Segue análise do lote.\n\n"
            "Atenciosamente,\n\n"
            "Cleide\n"
            "Analista de Frete Experiente\n"
            "AgenteFrete"
        )

    monkeypatch.setattr(
        insights_chat,
        "cleiton_governed_generate_content",
        lambda *args, **kwargs: _Resp(),
    )
    monkeypatch.setattr(insights_chat, "_get_client", lambda: object())

    class _FakeSession(dict):
        modified = False

    session = _FakeSession()
    result = _insights_reply_with_bundle(
        "Prepare um e-mail executivo para minha equipe",
        request_id="sig-gemini-1",
        monkeypatch=monkeypatch,
        session_obj=session,
    )
    answer = result["answer"]
    assert "[Seu nome]" in answer
    assert not re.search(r"(?m)^\s*Cleide\s*$", answer)
    assert "Analista de Frete Experiente" not in answer
    assert result.get("deterministic") is False

    cached = insights_chat.get_cached_insights_chat_response(
        session,
        "sig-gemini-1",
        batch_scope=insights_batch_scope(result["_bundle"]),
    )
    assert cached is not None
    assert cached["answer"] == answer
    assert "[Seu nome]" in cached["answer"]
    assert not re.search(r"(?m)^\s*Cleide\s*$", cached["answer"])


def test_prompt_forbids_cleide_sender_signature():
    from app.cleide_audit_insights_prompt import build_cleide_audit_insights_system_prompt

    prompt = build_cleide_audit_insights_system_prompt().lower()
    assert "nunca assine como cleide" in prompt
    assert "[seu nome]" in prompt
    assert "agentefrete" in prompt


def _bundle_with_doc_82986_and_uncalculated():
    rows = [
        {
            "row_index": 1,
            "document_number": "82986",
            "carrier": "Transp Alpha",
            "destination_city": "Curitiba",
            "destination_uf": "PR",
            "audited_weight": "120.5",
            "charged_freight": 2267.16,
            "expected_freight": 1153.31,
            "divergence_value": 1113.85,
            "status": "divergent",
            "reason_code": "divergent",
            "freight_region": "PR1",
            "calculation_components": {
                "weight_freight": {"amount": 1000.0},
                "tax_components": [{"tax_type": "ICMS", "amount": 153.31}],
                "tax_total": 153.31,
                "subtotal_before_taxes": 1000.0,
            },
        },
        {
            "row_index": 2,
            "document_number": "90001",
            "carrier": "Transp Beta",
            "destination_city": "Joinville",
            "destination_uf": "SC",
            "audited_weight": "40",
            "charged_freight": 200.0,
            "expected_freight": None,
            "divergence_value": None,
            "status": "missing_freight_rule",
            "reason_code": "missing_freight_rule",
            "freight_region": "SC1",
            "diagnostic": {
                "failure_stage": "pricing_rule_match",
                "message": "Região SC1 sem regra tarifária compatível.",
                "attempted_keys": ["SC1"],
            },
            "calculation_components": {},
        },
        {
            "row_index": 3,
            "document_number": "90002",
            "carrier": "Transp Beta",
            "destination_city": "Joinville",
            "destination_uf": "SC",
            "charged_freight": 180.0,
            "expected_freight": None,
            "divergence_value": None,
            "status": "missing_freight_rule",
            "reason_code": "missing_freight_rule",
            "freight_region": "SC1",
            "diagnostic": {
                "failure_stage": "pricing_rule_match",
                "message": "Região SC1 sem regra tarifária compatível.",
                "attempted_keys": ["SC1"],
            },
            "calculation_components": {},
        },
        {
            "row_index": 4,
            "document_number": "90003",
            "carrier": "Transp Gama",
            "destination_city": "Campo Limpo Paulista",
            "destination_uf": "SP",
            "charged_freight": 90.0,
            "expected_freight": None,
            "divergence_value": None,
            "status": "missing_coverage_mapping",
            "reason_code": "missing_coverage_mapping",
            "freight_region": None,
            "diagnostic": {
                "failure_stage": "coverage_mapping",
                "message": "Cidade sem classificação de cobertura.",
                "attempted_keys": ["Campo Limpo Paulista/SP"],
            },
            "calculation_components": {},
        },
    ]
    for index in range(5, 10):
        rows.append(
            {
                "row_index": index,
                "document_number": f"9001{index}",
                "carrier": "Transp Beta",
                "destination_city": "Joinville",
                "destination_uf": "SC",
                "charged_freight": 150.0,
                "expected_freight": None,
                "divergence_value": None,
                "status": "missing_freight_rule",
                "reason_code": "missing_freight_rule",
                "freight_region": "SC1",
                "diagnostic": {
                    "failure_stage": "pricing_rule_match",
                    "message": "Região SC1 sem regra tarifária compatível.",
                    "attempted_keys": ["SC1"],
                },
                "calculation_components": {},
            }
        )
    return {
        "source_file_name": "auditado.xlsx",
        "merged_rows": rows,
        "summary": {"total_rows": len(rows)},
        "audit_diagnostics": {
            "groups": [
                {
                    "title": "Dimensão tarifária incompatível",
                    "affected_rows": 7,
                    "failure_stage": "pricing_rule_match",
                    "message": "Regiões sem regra compatível.",
                }
            ]
        },
        "coverage_summary": {
            "row_count": 3,
            "sample_regions": ["SC1", "PR1"],
            "sample_cities": ["Joinville", "Campo Limpo Paulista"],
        },
        "needs_reprocess": False,
        "stale_reason": None,
        "temp_table_id": "tt-focus",
        "audit_batch_id": "batch-focus",
        "processed_at": "2026-07-07T20:00:00+00:00",
    }


def test_document_focus_sequence_divergence_followup(monkeypatch):
    bundle = _bundle_with_doc_82986_and_uncalculated()

    class _FakeSession(dict):
        modified = False

    session = _FakeSession()
    first = _insights_reply_with_bundle(
        "Me explique o cálculo do documento 82986",
        request_id="focus-1",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    assert first.get("error") is None
    assert "82986" in first["answer"]
    focus = get_conversation_focus(session, "tt-focus:batch-focus:2026-07-07T20:00:00+00:00")
    assert focus is not None
    assert str(focus.get("document_number")) == "82986"

    second = _insights_reply_with_bundle(
        "de quanto foi a divergência?",
        request_id="focus-2",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    answer = second["answer"].lower()
    assert "82986" in answer
    assert "1.113,85" in second["answer"]
    assert "2.267,16" in second["answer"]
    assert "1.153,31" in second["answer"]
    assert classify_intent("de quanto foi a divergência?", conversation_focus=focus) == INTENT_DOCUMENT_FOLLOWUP


def test_document_focus_anaphora_keeps_document(monkeypatch):
    bundle = _bundle_with_doc_82986_and_uncalculated()

    class _FakeSession(dict):
        modified = False

    session = _FakeSession()
    _insights_reply_with_bundle(
        "Me explique o cálculo do documento 82986",
        request_id="focus-a1",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    result = _insights_reply_with_bundle(
        "estou falando daquele documento específico",
        request_id="focus-a2",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    answer = result["answer"].lower()
    assert "entendi" in answer
    assert "82986" in answer
    assert "diverg" in answer


def test_document_focus_tax_and_weight(monkeypatch):
    bundle = _bundle_with_doc_82986_and_uncalculated()

    class _FakeSession(dict):
        modified = False

    session = _FakeSession()
    _insights_reply_with_bundle(
        "Me explique o cálculo do documento 82986",
        request_id="focus-t1",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    tax = _insights_reply_with_bundle(
        "e o imposto?",
        request_id="focus-t2",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    assert "82986" in tax["answer"]
    assert "icms" in tax["answer"].lower() or "153,31" in tax["answer"]

    weight = _insights_reply_with_bundle(
        "qual foi o peso?",
        request_id="focus-t3",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    assert "82986" in weight["answer"]
    assert "120.5" in weight["answer"] or "120,5" in weight["answer"]


def test_focus_invalidated_when_batch_scope_changes():
    class _FakeSession(dict):
        modified = False

    session = _FakeSession()
    set_conversation_focus(
        session,
        batch_scope="old-scope",
        document_number="82986",
        row_index=1,
        last_intent=INTENT_EXPLAIN_CALCULATION,
    )
    assert get_conversation_focus(session, "old-scope") is not None
    assert get_conversation_focus(session, "new-scope") is None


def test_duplicate_document_does_not_set_ambiguous_focus(monkeypatch):
    bundle = _make_insights_bundle()

    class _FakeSession(dict):
        modified = False

    session = _FakeSession()
    result = _insights_reply_with_bundle(
        "Me explique o cálculo do documento 7400455",
        request_id="dup-1",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    assert "esclareça" in result["answer"].lower() or "linhas" in result["answer"].lower()
    scope = f"{bundle['temp_table_id']}:{bundle['audit_batch_id']}:{bundle['processed_at']}"
    assert get_conversation_focus(session, scope) is None


def test_uncalculated_cities_lists_cities_and_reasons(monkeypatch):
    assert classify_intent("Quais cidades estão sem frete calculado?") == INTENT_UNCALCULATED_CITIES
    bundle = _bundle_with_doc_82986_and_uncalculated()
    result = _insights_reply_with_bundle(
        "Quais cidades estão sem frete calculado?",
        request_id="unc-city",
        monkeypatch=monkeypatch,
        bundle=bundle,
    )
    answer = result["answer"].lower()
    assert "joinville" in answer
    assert "campo limpo paulista" in answer
    assert "sc1" in answer or "regra" in answer


def test_uncalculated_documents_lists_rows(monkeypatch):
    assert classify_intent("Quais documentos ficaram sem frete calculado?") == INTENT_UNCALCULATED_DOCUMENTS
    bundle = _bundle_with_doc_82986_and_uncalculated()
    result = _insights_reply_with_bundle(
        "Quais documentos ficaram sem frete calculado?",
        request_id="unc-doc",
        monkeypatch=monkeypatch,
        bundle=bundle,
    )
    assert "90001" in result["answer"]
    assert "linha" in result["answer"].lower()


def test_uncalculated_reasons_explain_codes(monkeypatch):
    assert classify_intent("Por que essas cidades não calcularam?") == INTENT_EXPLAIN_UNCALCULATED_REASONS
    bundle = _bundle_with_doc_82986_and_uncalculated()
    result = _insights_reply_with_bundle(
        "Por que essas cidades não calcularam?",
        request_id="unc-reason",
        monkeypatch=monkeypatch,
        bundle=bundle,
    )
    answer = result["answer"].lower()
    assert "missing_freight_rule" in answer or "regra de frete" in answer
    assert "missing_coverage_mapping" in answer or "cobertura" in answer


def test_expected_freight_none_excluded_from_financial_ranking():
    bundle = _bundle_with_doc_82986_and_uncalculated()
    ranked = build_ranking(bundle, INTENT_TOP_DIVERGENCES, limit=20)
    assert all(not is_uncalculated_row(row) for row in ranked)
    assert all(row.get("expected_freight") is not None for row in ranked)
    assert all(str(row.get("document_number")) != "90001" for row in ranked)
    assert any(str(row.get("document_number")) == "82986" for row in ranked)


def test_faca_email_executivo_generates_draft(monkeypatch):
    assert classify_intent("Faça um e-mail executivo com a análise da auditoria") == INTENT_MANAGEMENT_EMAIL_DRAFT
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(
        "Faça um e-mail executivo com a análise da auditoria",
        request_id="email-exec",
        monkeypatch=monkeypatch,
    )
    answer = result["answer"].lower()
    assert "minuta" in answer or "sugestão" in answer or "sugestao" in answer or "claro" in answer
    assert "assunto:" in answer


def test_envie_esse_email_blocked_with_offer(monkeypatch):
    assert classify_intent("Envie esse e-mail") == INTENT_SEND_EMAIL_BLOCKED
    result = _insights_reply_with_bundle(
        "Envie esse e-mail",
        request_id="email-send-block",
        monkeypatch=monkeypatch,
    )
    answer = result["answer"].lower()
    assert "não consigo envi" in answer or "nao consigo envi" in answer
    assert "minuta" in answer


def test_faca_upload_still_blocked():
    assert classify_intent("Faça upload") == INTENT_OUT_OF_SCOPE


def test_monte_plano_acao_not_blocked_by_plano():
    assert classify_intent("Monte um plano de ação") == INTENT_ACTION_PLAN


def test_out_of_scope_fallback_only_for_external_topics():
    assert classify_intent("contratar plano stripe") == INTENT_OUT_OF_SCOPE
    assert classify_intent("de quanto foi a divergência?") != INTENT_OUT_OF_SCOPE
    bundle = _sample_bundle()
    answer = format_out_of_scope(bundle).lower()
    assert "regra de ouro" not in answer


def test_answers_omit_golden_rule_footer(monkeypatch):
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    for message in (
        "maiores divergências",
        "Faça um resumo executivo",
        "Quais cidades estão sem frete calculado?",
    ):
        result = _insights_reply_with_bundle(
            message,
            request_id=f"gold-{abs(hash(message)) % 10000}",
            monkeypatch=monkeypatch,
            bundle=_bundle_with_doc_82986_and_uncalculated() if "cidades" in message else None,
        )
        assert "regra de ouro" not in result["answer"].lower()


def test_memory_respects_chat_max_history_from_adm(monkeypatch):
    captured = {}

    def _fake_sanitize(history, max_history=10):
        captured["max_history"] = max_history
        return []

    monkeypatch.setattr(insights_chat, "sanitize_chat_history", _fake_sanitize)
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    _insights_reply_with_bundle(
        "Faça um resumo executivo",
        request_id="hist-adm",
        monkeypatch=monkeypatch,
        history=[{"role": "user", "content": "oi"}] * 10,
        cfg_overrides={"chat_max_history": 3},
    )
    assert captured["max_history"] == 3


def test_no_persona_adm_field_in_cleide_audit_config():
    cfg = _default_audit_cfg()
    assert hasattr(cfg, "chat_max_history")
    assert not hasattr(cfg, "persona")
    assert not hasattr(cfg, "custom_persona")
    assert not hasattr(cfg, "system_prompt_override")


def test_gemini_failure_keeps_useful_deterministic_fallback(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("gemini down")

    monkeypatch.setattr(insights_chat, "_get_client", lambda: object())
    monkeypatch.setattr(insights_chat, "cleiton_governed_generate_content", _boom)
    result = _insights_reply_with_bundle(
        "Faça um resumo executivo",
        request_id="gemini-fail",
        monkeypatch=monkeypatch,
    )
    assert result.get("deterministic") is True
    assert "impacto" in result["answer"].lower()


def _ranking_fixture_bundle():
    """6 positivas, 3 negativas, 1 zerada — para testes de quantidade/direção."""
    rows = []
    for index, (doc, charged, expected) in enumerate(
        [
            ("P1", 200, 100),
            ("P2", 180, 100),
            ("P3", 160, 100),
            ("P4", 140, 100),
            ("P5", 130, 100),
            ("P6", 120, 100),
            ("N1", 80, 100),
            ("N2", 70, 100),
            ("N3", 60, 100),
            ("Z1", 100, 100),
        ],
        start=1,
    ):
        divergence = charged - expected
        rows.append(
            {
                "row_index": index,
                "document_number": doc,
                "carrier": f"Transp {index}",
                "destination_uf": "SP",
                "destination_city": "Campinas",
                "charged_freight": float(charged),
                "expected_freight": float(expected),
                "divergence_value": float(divergence),
                "status": "ok" if abs(divergence) <= 0.004 else "divergent",
                "calculation_components": {},
            }
        )
    return {
        "source_file_name": "ranking.xlsx",
        "merged_rows": rows,
        "summary": {"total_rows": len(rows)},
        "audit_diagnostics": {"groups": []},
        "needs_reprocess": False,
        "stale_reason": None,
        "temp_table_id": "tt-rank",
        "audit_batch_id": "batch-rank",
        "processed_at": "2026-07-07T20:00:00+00:00",
    }


def _count_ranked_items(answer: str) -> int:
    return len(re.findall(r"(?m)^\d+\.\s+Linha\s+", answer or ""))


def test_tres_maiores_divergencia_a_maior_returns_exactly_3_positives():
    bundle = _ranking_fixture_bundle()
    assert classify_intent("Liste as três maiores divergencia a maior.") == INTENT_OVERCHARGED
    answer, rows, deterministic = try_deterministic_response(
        bundle,
        INTENT_OVERCHARGED,
        "Liste as três maiores divergencia a maior.",
    )
    assert deterministic is True
    assert rows is not None
    assert len(rows) == 3
    assert all((_row_div(row) or 0) > 0 for row in rows)
    assert "cobranças a maior (top 3)" in answer.lower() or "cobrancas a maior (top 3)" in answer.lower()
    assert "cobrado a maior" in answer.lower()
    assert _count_ranked_items(answer) == 3


def test_3_maiores_divergencias_a_maior_digit_form():
    bundle = _ranking_fixture_bundle()
    assert classify_intent("Liste as 3 maiores divergências a maior") == INTENT_OVERCHARGED
    answer, rows, _ = try_deterministic_response(
        bundle,
        INTENT_OVERCHARGED,
        "Liste as 3 maiores divergências a maior",
    )
    assert len(rows or []) == 3
    assert all((_row_div(row) or 0) > 0 for row in rows)
    assert _count_ranked_items(answer) == 3


def test_3_maiores_divergencias_a_menor_returns_exactly_3_negatives():
    bundle = _ranking_fixture_bundle()
    message = "Quero as 3 maiores divergências cobradas a menor que o nosso cálculo."
    assert classify_intent(message) == INTENT_UNDERCHARGED
    answer, rows, _ = try_deterministic_response(bundle, INTENT_UNDERCHARGED, message)
    assert len(rows or []) == 3
    assert all((_row_div(row) or 0) < 0 for row in rows)
    assert "cobranças a menor (top 3)" in answer.lower() or "cobrancas a menor (top 3)" in answer.lower()
    assert "cobrado a menor" in answer.lower()
    assert _count_ranked_items(answer) == 3


def test_pedir_3_a_menor_com_apenas_2_informa_limitacao():
    rows = [
        {
            "row_index": 1,
            "document_number": "N1",
            "carrier": "A",
            "destination_uf": "SP",
            "charged_freight": 80.0,
            "expected_freight": 100.0,
            "divergence_value": -20.0,
            "status": "divergent",
            "calculation_components": {},
        },
        {
            "row_index": 2,
            "document_number": "N2",
            "carrier": "B",
            "destination_uf": "RJ",
            "charged_freight": 70.0,
            "expected_freight": 100.0,
            "divergence_value": -30.0,
            "status": "divergent",
            "calculation_components": {},
        },
        {
            "row_index": 3,
            "document_number": "P1",
            "carrier": "C",
            "destination_uf": "MG",
            "charged_freight": 150.0,
            "expected_freight": 100.0,
            "divergence_value": 50.0,
            "status": "divergent",
            "calculation_components": {},
        },
    ]
    bundle = {"merged_rows": rows, "needs_reprocess": False, "stale_reason": None}
    answer, ranked, _ = try_deterministic_response(
        bundle,
        INTENT_UNDERCHARGED,
        "Quero as 3 maiores divergências cobradas a menor",
    )
    assert len(ranked or []) == 2
    assert "encontrei 2 de 3" in answer.lower() or "apenas 2" in answer.lower()
    assert _count_ranked_items(answer) == 2


def test_liste_6_maiores_divergencias_uses_absolute():
    bundle = _ranking_fixture_bundle()
    message = "Liste as 6 maiores divergências"
    assert classify_intent(message) == INTENT_TOP_DIVERGENCES
    answer, rows, _ = try_deterministic_response(bundle, INTENT_TOP_DIVERGENCES, message)
    assert len(rows or []) == 6
    assert "absolutas (top 6)" in answer.lower()
    assert _count_ranked_items(answer) == 6


def test_ranking_without_quantity_defaults_to_top_5():
    bundle = _ranking_fixture_bundle()
    assert extract_top_n("maiores divergências") == 5
    answer, rows, _ = try_deterministic_response(
        bundle,
        INTENT_TOP_DIVERGENCES,
        "maiores divergências",
    )
    assert len(rows or []) == 5
    assert "top 5" in answer.lower()


def test_top_12_overcharges_respects_quantity_or_cap():
    from app.cleide_audit_insights_query import RANKING_MAX_ITEMS

    bundle = _ranking_fixture_bundle()
    message = "top 12 cobranças a mais"
    assert classify_intent(message) == INTENT_OVERCHARGED
    assert extract_top_n(message) == min(12, RANKING_MAX_ITEMS)
    answer, rows, _ = try_deterministic_response(bundle, INTENT_OVERCHARGED, message)
    # Fixture tem 6 positivas; pede 12 → informa limitação
    assert len(rows or []) == 6
    assert "encontrei 6 de 12" in answer.lower() or "apenas 6" in answer.lower()


def test_ranking_titles_reflect_quantity_and_criteria():
    bundle = _ranking_fixture_bundle()
    over = format_ranking_response(
        bundle,
        INTENT_OVERCHARGED,
        build_ranking(bundle, INTENT_OVERCHARGED, limit=3),
        limit=3,
    ).lower()
    under = format_ranking_response(
        bundle,
        INTENT_UNDERCHARGED,
        build_ranking(bundle, INTENT_UNDERCHARGED, limit=3),
        limit=3,
    ).lower()
    absolute = format_ranking_response(
        bundle,
        INTENT_TOP_DIVERGENCES,
        build_ranking(bundle, INTENT_TOP_DIVERGENCES, limit=3),
        limit=3,
    ).lower()
    assert "cobranças a maior (top 3)" in over or "cobrancas a maior (top 3)" in over
    assert "cobranças a menor (top 3)" in under or "cobrancas a menor (top 3)" in under
    assert "maiores divergências absolutas (top 3)" in absolute or "absolutas (top 3)" in absolute


def test_followup_e_a_menor_reuses_previous_ranking_quantity(monkeypatch):
    bundle = _ranking_fixture_bundle()

    class _FakeSession(dict):
        modified = False

    session = _FakeSession()
    first = _insights_reply_with_bundle(
        "Liste as 3 maiores divergências a maior",
        request_id="rank-f1",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    assert "top 3" in first["answer"].lower()
    scope = f"{bundle['temp_table_id']}:{bundle['audit_batch_id']}:{bundle['processed_at']}"
    focus = get_conversation_focus(session, scope)
    assert focus is not None
    assert int(focus.get("last_ranking_limit") or 0) == 3

    second = _insights_reply_with_bundle(
        "e a menor?",
        request_id="rank-f2",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    assert classify_intent("e a menor?", conversation_focus=focus) == INTENT_UNDERCHARGED
    assert "cobranças a menor (top 3)" in second["answer"].lower() or "cobrancas a menor (top 3)" in second["answer"].lower()
    assert _count_ranked_items(second["answer"]) == 3


def test_followup_e_a_menor_without_ranking_memory_asks_clarification():
    assert classify_intent("e a menor?") == INTENT_AMBIGUOUS
    bundle = _ranking_fixture_bundle()
    answer, _, deterministic = try_deterministic_response(
        bundle,
        INTENT_AMBIGUOUS,
        "e a menor?",
        conversation_focus=None,
    )
    assert deterministic is True
    assert "esclarecer" in answer.lower() or "quantidade" in answer.lower()


def test_typo_email_execituvo_para_eu_enviar_generates_minuta(monkeypatch):
    message = "Prepare um e-ma execituvo para eu enviar a minha equipe."
    assert classify_intent(message) == INTENT_MANAGEMENT_EMAIL_DRAFT
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(message, request_id="typo-email-1", monkeypatch=monkeypatch)
    answer = result["answer"].lower()
    assert "minuta" in answer or "sugestão" in answer or "sugestao" in answer
    assert "claro" in answer
    assert "não consigo enviar o e-mail pela plataforma" not in answer
    assert "fora do que consigo" not in answer


def test_email_para_eu_enviar_diretoria_generates_minuta(monkeypatch):
    message = "Prepare um email executivo para eu enviar à diretoria."
    assert classify_intent(message) == INTENT_MANAGEMENT_EMAIL_DRAFT
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(message, request_id="typo-email-2", monkeypatch=monkeypatch)
    assert "assunto:" in result["answer"].lower()
    assert "não consigo enviar o e-mail pela plataforma" not in result["answer"].lower()


def test_typo_resumo_execultivo_generates_executive_summary(monkeypatch):
    message = "faça um resumo execultivo da auditoria"
    # Aceita "Faz" / "faça" após normalização
    assert classify_intent(message) == INTENT_EXECUTIVE_SUMMARY
    assert classify_intent("Faz um resumo execultivo da auditoria.") == INTENT_EXECUTIVE_SUMMARY
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    result = _insights_reply_with_bundle(message, request_id="typo-exec", monkeypatch=monkeypatch)
    answer = result["answer"].lower()
    assert "impacto" in answer
    assert "fora do que consigo" not in answer


def test_typo_explica_calculo_documento():
    assert classify_intent("me explica o calculo do documento 82986") == INTENT_EXPLAIN_CALCULATION


def test_typo_cidades_estao_sem_frete():
    assert classify_intent("quais cidades estao sem frete calculado") == INTENT_UNCALCULATED_CITIES


def test_para_eu_enviar_is_not_blocked():
    assert classify_intent("Prepare um e-mail para eu enviar a minha equipe") == INTENT_MANAGEMENT_EMAIL_DRAFT
    assert classify_intent("redija uma mensagem para eu mandar à diretoria") == INTENT_MANAGEMENT_EMAIL_DRAFT


def test_envie_and_mande_remain_blocked():
    assert classify_intent("Envie o e-mail") == INTENT_SEND_EMAIL_BLOCKED
    assert classify_intent("mande para minha equipe") == INTENT_SEND_EMAIL_BLOCKED


def test_typo_requests_do_not_hit_generic_out_of_scope_fallback(monkeypatch):
    monkeypatch.setattr(insights_chat, "_get_client", lambda: None)
    for message in (
        "Prepare um e-ma execituvo para eu enviar a minha equipe.",
        "Faz um resumo execultivo da auditoria.",
        "quais cidades estao sem frete calculado?",
    ):
        result = _insights_reply_with_bundle(
            message,
            request_id=f"typo-fb-{abs(hash(message)) % 10000}",
            monkeypatch=monkeypatch,
            bundle=_bundle_with_doc_82986_and_uncalculated() if "cidades" in message else None,
        )
        lowered = result["answer"].lower()
        assert "não consigo executar essa ação pela plataforma" not in lowered
        assert classify_intent(message) != INTENT_OUT_OF_SCOPE


def test_textual_gemini_uses_insights_flow_type_for_typo_email(monkeypatch):
    capture: dict = {}

    class _Resp:
        text = (
            "Claro. Segue uma sugestão de minuta executiva para você revisar e enviar à sua equipe:\n"
            "**Assunto:** Auditoria de frete"
        )

    def _fake(client, model, contents, agent, flow_type, api_key_label):
        capture["flow_type"] = flow_type
        capture["agent"] = agent
        return _Resp()

    monkeypatch.setattr(insights_chat, "cleiton_governed_generate_content", _fake)
    monkeypatch.setattr(insights_chat, "_get_client", lambda: object())
    billing_mock = MagicMock()
    monkeypatch.setattr(
        "app.services.cleiton_upload_billing_service.apropriar_billing_cleide_operational_flow",
        billing_mock,
    )
    result = _insights_reply_with_bundle(
        "Prepare um e-ma execituvo para eu enviar a minha equipe.",
        request_id="typo-gemini",
        monkeypatch=monkeypatch,
    )
    assert result.get("error") is None
    assert capture["flow_type"] == CLEIDE_AUDIT_INSIGHTS_CHAT_FLOW_TYPE
    assert capture["agent"] == "cleide"
    assert result["deterministic"] is False
    billing_mock.assert_not_called()


def test_ranking_and_document_focus_still_work_after_typo_fixes(monkeypatch):
    assert classify_intent("Liste as 3 maiores divergências a maior") == INTENT_OVERCHARGED
    bundle = _bundle_with_doc_82986_and_uncalculated()

    class _FakeSession(dict):
        modified = False

    session = _FakeSession()
    first = _insights_reply_with_bundle(
        "Me explica o calculo do documento 82986",
        request_id="typo-focus-1",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    assert "82986" in first["answer"]
    second = _insights_reply_with_bundle(
        "de quanto foi a divergência?",
        request_id="typo-focus-2",
        monkeypatch=monkeypatch,
        session_obj=session,
        bundle=bundle,
    )
    assert "82986" in second["answer"]
