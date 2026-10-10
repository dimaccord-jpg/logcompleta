"""SCRUM-146 lote 6C: dispatch real atrás da fachada, com transporte simulado."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models import (
    IaChamadaTentativa,
    IaConsumoEvento,
    SemanticAiJobBudget,
    SemanticQuestionJournal,
)
from app.services.cleiton_ai_data_governance import govern_outbound_content, purpose_from_flow_type
from app.services.cleiton_ai_privacy_classifier import (
    DECISION_ALLOW,
    DECISION_BLOCK,
    DECISION_MINIMIZE,
    _OPERATIONAL_PURPOSES,
    classify_text,
)
from app.services.semantic_question_execution.constants import (
    CONFIDENCE_VALUES,
    DECISION_STATUSES,
    OUTPUT_SCHEMA_VERSION,
    PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION,
    RATIONALE_CODES,
    REAL_RESPONSE_SCHEMA_TRANSPORT,
    SEMANTIC_AI_REAL_DISPATCH_ENABLED,
    real_config_material,
    semantic_ai_real_dispatch_enabled,
)
from app.services.semantic_question_execution.keys import ai_execution_fingerprint
from app.services.semantic_question_execution.dispatcher import (
    build_generation_config,
    dispatch_real_question,
    open_real_generation,
    with_real_execution_config,
)
from app.services.semantic_question_execution.service import (
    claim_execution,
    configure_job_budget,
    load_execution,
    prepare_execution,
    recover_execution,
    reject_human_review,
)
from app.services.semantic_freight_contract.evidence import local_fingerprint
from tests.conftest import seed_cleiton_cost_config, seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

CAMPINAS = "sem:BR:municipality:SP:CAMPINAS"
CAMPINAS_MG = "sem:BR:municipality:MG:CAMPINAS"
DEPENDENCY = local_fingerprint({"kind": "dependency", "place": "campinas-real"})
AUTHORIZATION = local_fingerprint({"auth": "real-v1"})
CPF = "529.982.247-25"
EMAIL = "ana@example.com"
DECISION = json.dumps(
    {
        "decisions": [
            {
                "schema_version": "1",
                "question_id": "q1",
                "status": "selected",
                "selected_candidate_id": "c2",
                "evidence_refs": ["e2"],
                "rationale_code": "evidence_selects_candidate",
                "confidence": "high",
            }
        ]
    },
    ensure_ascii=False,
)


def _enable(flask_app, monkeypatch, env="homolog"):
    flask_app.config["SEMANTIC_AI_REAL_DISPATCH_ENABLED"] = True
    monkeypatch.setenv("APP_ENV", env)


def _seed(email="real@test.com", *, limite="100", bloqueio=False):
    seed_sistema_interno()
    seed_cleiton_cost_config()
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = Decimal(limite)
    franquia.bloqueio_manual = bloqueio
    from app.models import utcnow_naive
    from datetime import timedelta

    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    return seed_usuario(franquia.id, conta.id, email=email), franquia


def _spec(**overrides):
    base = {
        "tenant_scope": "tenant-a",
        "job_ref": "job-real",
        "batch_id": "batch-real",
        "question_id": "sq-campinas",
        "semantic_question_key": "sqk:campinas-real",
        "question_type": "municipality_text_disambiguation",
        "eligibility": "ai_eligible",
        "queue_status": "current",
        "candidate_ids": [CAMPINAS, CAMPINAS_MG],
        "candidate_public": {
            CAMPINAS: {"municipality": "Campinas", "state": "SP"},
            CAMPINAS_MG: {"municipality": "Campinas", "state": "MG"},
        },
        "ambiguous_text": "Campinas",
        "evidence_items": [
            {
                "evidence_id": "ev-mg",
                "candidate_id": CAMPINAS_MG,
                "material": "rodovia MG-050",
                "source_kind": "excerpt",
                "scope": "destination",
            },
            {
                "evidence_id": "ev-sp",
                "candidate_id": CAMPINAS,
                "material": "rodovia SP-340",
                "source_kind": "excerpt",
                "scope": "destination",
            },
        ],
        "constraints": {
            "country": "BR",
            "state": None,
            "origin_entity_id": "internal-origin",
            "service_ref": "internal-service",
        },
        "contract_snapshot": {"contract_id": "sfc:hidden", "revision": 1, "content_fingerprint": "sha256:hidden"},
        "decision_dependency_fingerprint": DEPENDENCY,
        "authorization_fingerprint": AUTHORIZATION,
        "generation": 1,
    }
    base.update(overrides)
    return with_real_execution_config(base)


def _budget(spec, credits="5"):
    return configure_job_budget(
        spec["tenant_scope"],
        spec["job_ref"],
        max_requests=4,
        max_billable_credits=Decimal(credits),
    )


def _loader(execution_id, counter=None):
    def load():
        if counter is not None:
            counter["n"] += 1
        current = load_execution(execution_id)
        snapshot = current["snapshot"]
        return {
            "queue_status": "current",
            "eligibility": "ai_eligible",
            "question_type": current["question_type"],
            "candidate_ids": list(snapshot["candidate_ids"]),
            "evidence_fingerprint": snapshot["evidence_fingerprint"],
            "dependency_fingerprint": current["decision_dependency_fingerprint"],
            "authorization_fingerprint": snapshot["authorization_fingerprint"],
            "generation": current["generation"],
        }

    return load


def _sdk(capturados, text, *, usage=True):
    import httpx
    from google import genai

    def handler(request: httpx.Request) -> httpx.Response:
        capturados.append(
            {
                "url": str(request.url),
                "body": request.content.decode("utf-8") if request.content else "",
            }
        )
        url = str(request.url)
        if "countTokens" in url:
            return httpx.Response(200, json={"totalTokens": 8})
        if "generateContent" in url:
            body = {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ]
            }
            if usage:
                body["usageMetadata"] = {
                    "promptTokenCount": 8,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 12,
                }
            return httpx.Response(200, json=body)
        return httpx.Response(500, json={"error": {"message": "inesperado"}})

    client = genai.Client(api_key="teste")
    client._api_client._httpx_client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _budget_row(spec):
    return SemanticAiJobBudget.query.filter_by(tenant_scope=spec["tenant_scope"], job_ref=spec["job_ref"]).one()


def _journal(execution_id, checkpoint):
    row = (
        SemanticQuestionJournal.query.filter_by(execution_id=execution_id, checkpoint=checkpoint)
        .order_by(SemanticQuestionJournal.id.desc())
        .first()
    )
    assert row is not None
    return json.loads(row.payload), row.response_digest


def _dispatch(execution_id, usuario, client, credits, **extra):
    return dispatch_real_question(
        execution_id,
        observation_loader=_loader(execution_id, extra.pop("counter", None)),
        client=client,
        usuario=usuario,
        max_reserved_credits=Decimal(credits),
        **extra,
    )


def test_flag_default_is_false(monkeypatch):
    monkeypatch.delenv("SEMANTIC_AI_REAL_DISPATCH_ENABLED", raising=False)
    assert SEMANTIC_AI_REAL_DISPATCH_ENABLED is False
    assert semantic_ai_real_dispatch_enabled() is False


def test_generation_config_is_explicit_and_has_zero_tools():
    config = build_generation_config()
    dumped = config.model_dump(exclude_none=True)
    assert dumped["temperature"] == 0
    assert dumped["candidate_count"] == 1
    assert dumped["max_output_tokens"] == 512
    assert dumped["thinking_config"]["thinking_budget"] == 0
    assert dumped["response_mime_type"] == "application/json"
    assert dumped["automatic_function_calling"]["disable"] is True
    assert dumped.get("tools") in ([], None)
    assert "tools" in config.model_fields_set
    assert config.tools == []
    assert "tool_config" not in dumped
    assert "cached_content" not in dumped
    assert "response_schema" not in dumped
    schema = dumped["response_json_schema"]
    _assert_decision_json_schema(schema)


def test_real_material_fingerprint_includes_json_schema_transport():
    material = real_config_material()
    assert material["response_schema_transport"] == REAL_RESPONSE_SCHEMA_TRANSPORT
    assert material["response_schema_transport"] == "response_json_schema"
    common = dict(
        decision_dependency_fingerprint="sha256:dep",
        prompt_version="sq-prompt-v1",
        output_schema_version="1",
        provider_id="gemini",
        model_id="gemini-2.5-flash",
        policy_version="sq-ai-policy-v1",
        minimization_policy_version="sq-min-v1",
    )
    current = ai_execution_fingerprint(config_material=material, **common)
    previous = dict(material)
    previous.pop("response_schema_transport")
    assert current != ai_execution_fingerprint(config_material=previous, **common)


def test_purpose_is_explicit_and_unknown_stays_closed():
    assert purpose_from_flow_type("semantic_municipality_disambiguation") == PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION
    assert purpose_from_flow_type("semantic_municipality_disambiguation", "semantic_question_execution") == (
        PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION
    )
    assert PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION not in _OPERATIONAL_PURPOSES
    kept = classify_text("nome: João Silva frete destino Campinas", purpose="chat_logistico")
    minimized = classify_text(
        "nome: João Silva frete destino Campinas",
        purpose=PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION,
    )
    assert kept.decision == DECISION_ALLOW
    assert minimized.decision == DECISION_MINIMIZE
    blocked = govern_outbound_content("Campinas SP", purpose="finalidade_inexistente", agent="semantic_question_execution")
    assert blocked.decision == DECISION_BLOCK
    assert "unsupported_purpose" in blocked.reason_codes


def test_flag_false_does_not_call_provider(app, monkeypatch):
    monkeypatch.delenv("SEMANTIC_AI_REAL_DISPATCH_ENABLED", raising=False)
    monkeypatch.setenv("APP_ENV", "homolog")
    with app.app_context():
        usuario, _franquia = _seed()
        spec = _spec()
        _budget(spec)
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert result["dispatched"] is False
        assert result["reason"] == "semantic_ai_real_dispatch_disabled"
        assert capturados == []
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 0
        row = _budget_row(spec)
        assert int(row.reserved_requests) == 0
        assert row.settled_credits == 0
        assert row.uncertain_credits == 0


@pytest.mark.parametrize("env", ["prod", "dev", "local"])
def test_non_homolog_env_blocks_even_with_flag(app, monkeypatch, env):
    _enable(app, monkeypatch, env)
    with app.app_context():
        usuario, _franquia = _seed(email=f"{env}@test.com")
        spec = _spec(job_ref=f"job-{env}", semantic_question_key=f"sqk-{env}", question_id=f"sq-{env}")
        _budget(spec)
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert result["dispatched"] is False
        assert result["reason"] == "app_env_blocked"
        assert capturados == []
        assert IaChamadaTentativa.query.count() == 0


@pytest.mark.parametrize(
    "question_type",
    ["dimension_label_disambiguation", "explicit_documentary_relation_extraction"],
)
def test_other_question_types_do_not_dispatch(app, monkeypatch, question_type):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, _franquia = _seed(email=f"{question_type}@test.com")
        spec = _spec(
            job_ref=f"job-{question_type}",
            semantic_question_key=f"sqk-{question_type}",
            question_id=f"sq-{question_type}",
            question_type=question_type,
        )
        _budget(spec)
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert result["reason"] == "question_type_disabled"
        assert capturados == []
        assert IaChamadaTentativa.query.count() == 0


def test_simulated_success_journals_requires_review_and_real_cost(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, franquia = _seed()
        spec = _spec()
        _budget(spec, credits="5")
        view = prepare_execution(spec)
        counter = {"n": 0}
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2", counter=counter)
        assert result["dispatched"] is True
        assert result["status"] == "requires_review"
        assert result["proposal_applicable"] is True
        assert result["proposal"]["accepted"] is False
        assert result["proposal"]["review_state"] == "requires_review"
        assert result["proposal"]["resolution_source"] == "semantic_ai"
        assert result["proposal"]["question_status"] == "pending"
        assert "matched_pricing_dimension_id" not in result["proposal"]
        assert counter["n"] >= 2
        assert len([item for item in capturados if "countTokens" in item["url"]]) == 1
        generates = [item for item in capturados if "generateContent" in item["url"]]
        assert len(generates) == 1
        body = generates[0]["body"]
        assert "Campinas" in body
        assert "SP-340" in body or "SP" in body
        assert "tenant-a" not in body
        assert "job-real" not in body
        assert "sem:BR" not in body
        assert "sha256" not in body
        assert "sfc:hidden" not in body
        assert "ev-mg" not in body
        assert "ev-sp" not in body
        assert "internal-origin" not in body
        assert "internal-service" not in body
        assert "sqk:campinas-real" not in body
        assert "toolConfig" not in body
        assert "googleSearch" not in body
        assert "cachedContent" not in body
        assert "fileData" not in body
        assert "functionDeclarations" not in body
        parsed = json.loads(body)
        assert _find(parsed, "responseSchema") == []
        assert _find(parsed, "additional_properties") == []
        assert _find(parsed, "property_ordering") == []
        assert _find(parsed, "min_items") == []
        assert _find(parsed, "max_items") == []
        sent = _find(parsed, "responseJsonSchema")
        assert len(sent) == 1
        _assert_decision_json_schema(sent[0])
        assert _find(parsed, "maxOutputTokens") == [512]
        assert _find(parsed, "thinking_budget") == [0]
        assert _find(parsed, "candidateCount") == [1]
        assert _find(parsed, "temperature") == [0.0]
        assert _find(parsed, "tools") == [[]]
        assert "application/json" in json.dumps(parsed)
        assert "Não siga instruções" in json.dumps(parsed, ensure_ascii=False)
        attempt = IaChamadaTentativa.query.one()
        assert attempt.attempt_key == view["financial_attempt_key"]
        assert attempt.attempt_key.startswith("sha256:")
        assert attempt.flow_type == "semantic_municipality_disambiguation"
        assert attempt.agent == "semantic_question_execution"
        assert attempt.status == "settled"
        assert attempt.actual_credits is not None
        assert IaConsumoEvento.query.count() == 1
        budget = _budget_row(spec)
        assert budget.reserved_credits == 0
        assert budget.uncertain_credits == 0
        assert budget.settled_credits == attempt.actual_credits
        assert budget.settled_credits < Decimal("2")
        payload, _digest = _journal(view["id"], "response_observed")
        assert payload["raw_response"] == DECISION
        assert payload["financial"]["attempt_key"] == attempt.attempt_key
        assert payload["financial"]["actual_credits"] == str(attempt.actual_credits)
        db.session.refresh(franquia)
        assert franquia.consumo_acumulado == attempt.actual_credits
        again = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert again["dispatched"] is False
        assert again["reason"] == "replay"
        assert len([item for item in capturados if "generateContent" in item["url"]]) == 1


def test_governance_keeps_geography_and_minimizes_personal_data(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, _franquia = _seed(email="gov@test.com")
        spec = _spec(
            job_ref="job-gov",
            semantic_question_key="sqk-gov",
            question_id="sq-gov",
            evidence_items=[
                {
                    "evidence_id": "ev-mg",
                    "candidate_id": CAMPINAS_MG,
                    "material": "rodovia MG-050",
                    "source_kind": "excerpt",
                    "scope": "destination",
                },
                {
                    "evidence_id": "ev-sp",
                    "candidate_id": CAMPINAS,
                    "material": f"rodovia SP-340 contato {EMAIL} cpf {CPF}",
                    "source_kind": "excerpt",
                    "scope": "destination",
                },
            ],
        )
        _budget(spec)
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert result["status"] == "requires_review"
        body = [item for item in capturados if "generateContent" in item["url"]][0]["body"]
        assert "SP-340" in body
        assert "MG-050" in body
        assert CPF not in body
        assert EMAIL not in body


def test_minimized_decisive_evidence_blocks_before_facade(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, franquia = _seed(email="decisive@test.com")
        spec = _spec(
            job_ref="job-decisive",
            semantic_question_key="sqk-decisive",
            question_id="sq-decisive",
            evidence_items=[
                {
                    "evidence_id": "ev-mg",
                    "candidate_id": CAMPINAS_MG,
                    "material": EMAIL,
                    "source_kind": "excerpt",
                    "scope": "destination",
                },
                {
                    "evidence_id": "ev-sp",
                    "candidate_id": CAMPINAS,
                    "material": CPF,
                    "source_kind": "excerpt",
                    "scope": "destination",
                },
            ],
        )
        _budget(spec)
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert result["dispatched"] is False
        assert result["reason"] == "decisive_evidence_minimized"
        assert capturados == []
        assert IaChamadaTentativa.query.count() == 0
        assert franquia.reserva_pendente == 0
        budget = _budget_row(spec)
        assert int(budget.reserved_requests) == 0
        assert budget.settled_credits == 0
        assert budget.uncertain_credits == 0


def test_max_reserved_blocks_franchise_and_releases_job(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, franquia = _seed(email="cap@test.com")
        spec = _spec(job_ref="job-cap", semantic_question_key="sqk-cap", question_id="sq-cap")
        _budget(spec, credits="0.001")
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "0.001")
        assert result["dispatched"] is False
        assert result["reason"] == "teto_excede_reserva_job"
        assert not any("generateContent" in item["url"] for item in capturados)
        db.session.refresh(franquia)
        assert franquia.reserva_pendente == 0
        assert franquia.consumo_acumulado == 0
        budget = _budget_row(spec)
        assert int(budget.reserved_requests) == 0
        assert budget.settled_credits == 0
        assert budget.uncertain_credits == 0


def test_franchise_denial_releases_job_budget(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, franquia = _seed(email="denied@test.com", bloqueio=True)
        spec = _spec(job_ref="job-denied", semantic_question_key="sqk-denied", question_id="sq-denied")
        _budget(spec)
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert result["dispatched"] is False
        assert result["status"] == "blocked"
        assert not any("generateContent" in item["url"] for item in capturados)
        db.session.refresh(franquia)
        assert franquia.consumo_acumulado == 0
        budget = _budget_row(spec)
        assert int(budget.reserved_requests) == 0
        assert budget.settled_credits == 0
        assert budget.uncertain_credits == 0


def test_malformed_response_keeps_real_settlement(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, _franquia = _seed(email="bad@test.com")
        spec = _spec(job_ref="job-bad", semantic_question_key="sqk-bad", question_id="sq-bad")
        _budget(spec)
        view = prepare_execution(spec)
        raw = '{"decisions":[{"foo":1}]}'
        result = _dispatch(view["id"], usuario, _sdk([], raw), "2")
        assert result["status"] == "failed"
        assert result["proposal_applicable"] is False
        attempt = IaChamadaTentativa.query.one()
        assert attempt.status == "settled"
        budget = _budget_row(spec)
        assert budget.settled_credits == attempt.actual_credits
        assert budget.uncertain_credits == 0
        payload, _digest = _journal(view["id"], "response_observed")
        assert payload["raw_response"] == raw
        assert result["proposal"] is None


def test_duplicate_keys_stay_in_the_journal(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, _franquia = _seed(email="dup@test.com")
        spec = _spec(job_ref="job-dup", semantic_question_key="sqk-dup", question_id="sq-dup")
        _budget(spec)
        view = prepare_execution(spec)
        raw = '{"decisions":[],"decisions":[]}'
        result = _dispatch(view["id"], usuario, _sdk([], raw), "2")
        assert result["status"] == "failed"
        assert "duplicate_key" in result["validation_findings"]
        payload, _digest = _journal(view["id"], "response_observed")
        assert payload["raw_response"] == raw


def test_uncertain_blocks_retry_and_new_generation(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, _franquia = _seed(email="unsure@test.com")
        spec = _spec(job_ref="job-unsure", semantic_question_key="sqk-unsure", question_id="sq-unsure")
        _budget(spec)
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION, usage=False), "2")
        assert result["status"] == "uncertain"
        assert result["proposal_applicable"] is False
        attempt = IaChamadaTentativa.query.one()
        assert attempt.status == "uncertain"
        assert attempt.attempt_key == view["financial_attempt_key"]
        budget = _budget_row(spec)
        assert budget.settled_credits == 0
        assert budget.uncertain_credits > 0
        assert budget.reserved_credits == 0
        again = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert again["dispatched"] is False
        assert len([item for item in capturados if "generateContent" in item["url"]]) == 1
        changed = _spec(
            job_ref="job-unsure",
            semantic_question_key="sqk-unsure",
            question_id="sq-unsure",
            model_id="gemini-2.5-pro",
        )
        with pytest.raises(Exception) as caught:
            open_real_generation({**changed, "explicit_retry": True})
        assert getattr(caught.value, "code", "") == "unreconciled_financial_attempt"


def test_crash_after_journal_recovers_without_second_generation(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, _franquia = _seed(email="crash@test.com")
        spec = _spec(job_ref="job-crash", semantic_question_key="sqk-crash", question_id="sq-crash")
        _budget(spec)
        view = prepare_execution(spec)
        capturados = []

        def boom():
            raise RuntimeError("crash-after-journal")

        with pytest.raises(RuntimeError, match="crash-after-journal"):
            _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2", after_journal=boom)
        payload, _digest = _journal(view["id"], "response_observed")
        assert payload["raw_response"] == DECISION
        assert payload["financial"]["attempt_id"]
        recovered = recover_execution(view["id"])
        assert recovered["status"] == "requires_review"
        assert recovered["proposal"]["accepted"] is False
        again = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "2")
        assert again["dispatched"] is False
        assert len([item for item in capturados if "generateContent" in item["url"]]) == 1


def test_human_decision_during_call_prevails(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, _franquia = _seed(email="human@test.com")
        spec = _spec(job_ref="job-human", semantic_question_key="sqk-human", question_id="sq-human")
        _budget(spec)
        view = prepare_execution(spec)
        claimed = claim_execution(view["id"])
        assert claimed["won"] is True

        def handler_factory(capturados):
            import httpx
            from google import genai

            def handler(request: httpx.Request) -> httpx.Response:
                capturados.append({"url": str(request.url), "body": request.content.decode() if request.content else ""})
                if "countTokens" in str(request.url):
                    return httpx.Response(200, json={"totalTokens": 8})
                reject_human_review(spec, reviewer_ref="ana", execution_id=view["id"])
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {"role": "model", "parts": [{"text": DECISION}]},
                                "finishReason": "STOP",
                            }
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 8,
                            "candidatesTokenCount": 4,
                            "totalTokenCount": 12,
                        },
                    },
                )

            client = genai.Client(api_key="teste")
            client._api_client._httpx_client = httpx.Client(transport=httpx.MockTransport(handler))
            return client

        result = _dispatch(view["id"], usuario, handler_factory([]), "2")
        assert result["status"] == "rejected"
        assert result["proposal_applicable"] is False
        attempt = IaChamadaTentativa.query.one()
        budget = _budget_row(spec)
        assert budget.settled_credits == attempt.actual_credits
        assert result.get("proposal") is None or result["proposal_applicable"] is False


def test_provider_exception_with_usage_settles_actual_cost(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        usuario, _franquia = _seed(email="boom@test.com")
        spec = _spec(job_ref="job-boom", semantic_question_key="sqk-boom", question_id="sq-boom")
        _budget(spec)
        view = prepare_execution(spec)

        class Boom(RuntimeError):
            def __init__(self):
                super().__init__("falhou")
                self.usage_metadata = SimpleNamespace(
                    prompt_token_count=8,
                    candidates_token_count=4,
                    total_token_count=12,
                )

        class _Models:
            def __init__(self):
                self.calls = []
                self.count_calls = []

            def count_tokens(self, *, model, contents, config=None):
                self.count_calls.append(model)
                return SimpleNamespace(total_tokens=8, cached_content_token_count=None)

            def generate_content(self, *, model, contents, config=None):
                self.calls.append(model)
                raise Boom()

        client = SimpleNamespace(
            models=_Models(),
            http_options=SimpleNamespace(retry_options=SimpleNamespace(attempts=1)),
        )
        result = _dispatch(view["id"], usuario, client, "2")
        assert result["status"] == "failed"
        assert result["reason"] == "provider_failed"
        attempt = IaChamadaTentativa.query.one()
        assert attempt.status == "provider_failed"
        assert attempt.actual_credits is not None
        assert attempt.actual_credits > 0
        budget = _budget_row(spec)
        assert budget.settled_credits == attempt.actual_credits
        assert budget.uncertain_credits == 0
        assert budget.reserved_credits == 0


def test_regua_invalida_libera_job_budget_sem_geracao(app, monkeypatch):
    _enable(app, monkeypatch)
    with app.app_context():
        from app.services.cleiton_cost_service import get_or_create_config

        usuario, franquia = _seed(email="regua-job@test.com")
        cfg = get_or_create_config()
        cfg.credit_tokens_per_credit = 0
        db.session.commit()
        spec = _spec(job_ref="job-regua", semantic_question_key="sqk-regua", question_id="sq-regua")
        _budget(spec, credits="0.001")
        view = prepare_execution(spec)
        capturados = []
        result = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "0.000001")
        assert result["dispatched"] is False
        assert result["status"] == "blocked"
        assert result["reason"] == "teto_creditos_nao_verificavel"
        assert len([item for item in capturados if "countTokens" in item["url"]]) == 1
        assert len([item for item in capturados if "generateContent" in item["url"]]) == 0
        attempt = IaChamadaTentativa.query.one()
        assert attempt.status == "blocked"
        assert attempt.actual_credits is None
        assert attempt.status != "uncertain"
        db.session.refresh(franquia)
        assert franquia.reserva_pendente == 0
        assert franquia.consumo_acumulado == 0
        budget = _budget_row(spec)
        assert int(budget.reserved_requests) == 0
        assert budget.reserved_credits == 0
        assert budget.settled_credits == 0
        assert budget.uncertain_credits == 0
        again = _dispatch(view["id"], usuario, _sdk(capturados, DECISION), "0.000001")
        assert again["dispatched"] is False
        assert again["reason"] == "replay"
        assert len([item for item in capturados if "generateContent" in item["url"]]) == 0
        assert len([item for item in capturados if "countTokens" in item["url"]]) == 1
        budget = _budget_row(spec)
        assert budget.settled_credits == 0
        assert budget.uncertain_credits == 0


def test_dispatcher_does_not_apply_pricing():
    source = Path("app/services/semantic_question_execution/dispatcher.py").read_text(encoding="utf-8")
    for token in (
        "matched_pricing_dimension_id",
        "executable_rule_ref",
        "calculate_weight_freight",
        "build_freight_pricing_index",
    ):
        assert token not in source


def _assert_decision_json_schema(schema):
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["decisions"]
    decisions = schema["properties"]["decisions"]
    assert decisions["type"] == "array"
    assert decisions["minItems"] == 1
    assert decisions["maxItems"] == 1
    decision = decisions["items"]
    assert decision["type"] == "object"
    assert decision["additionalProperties"] is False
    fields = [
        "schema_version",
        "question_id",
        "status",
        "selected_candidate_id",
        "evidence_refs",
        "rationale_code",
        "confidence",
    ]
    assert decision["required"] == fields
    assert decision["propertyOrdering"] == fields
    props = decision["properties"]
    assert props["schema_version"] == {"type": "string", "enum": [OUTPUT_SCHEMA_VERSION]}
    assert props["question_id"] == {"type": "string"}
    assert props["status"]["enum"] == list(DECISION_STATUSES)
    assert props["selected_candidate_id"]["anyOf"] == [{"type": "string"}, {"type": "null"}]
    assert props["evidence_refs"] == {"type": "array", "items": {"type": "string"}}
    assert props["rationale_code"]["enum"] == list(RATIONALE_CODES)
    assert props["confidence"]["enum"] == list(CONFIDENCE_VALUES)
    encoded = json.dumps(schema)
    assert "additional_properties" not in encoded
    assert "property_ordering" not in encoded
    assert "min_items" not in encoded
    assert "max_items" not in encoded
    assert "nullable" not in encoded


def _find(body, key):
    found = []
    if isinstance(body, dict):
        for name, value in body.items():
            if name == key:
                found.append(value)
            found.extend(_find(value, key))
    elif isinstance(body, list):
        for item in body:
            found.extend(_find(item, key))
    return found
