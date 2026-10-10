"""SCRUM-146 lote 5: batch semântico, perguntas e fila local, sem IA."""
from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path

import pytest

from app.agente_compara_doc_service import (
    _calculate_expected_freight_row,
    build_coverage_index,
    build_freight_pricing_index,
    calculate_weight_freight,
)
from app.services.semantic_freight_contract.models import TECHNICAL_JSON_KEY as CONTRACT_KEY
from app.services.semantic_resolution import (
    TECHNICAL_JSON_KEY,
    AuxiliaryGeographyCatalog,
    LocalResolutionCache,
    attach_semantic_resolution_queue,
    preserve_semantic_resolution_queue,
    resolve_batch,
    resolve_context,
)
from app.services.semantic_resolution.coverage import coverage_content_fingerprint
from app.services.semantic_resolution.human_cache import HumanDecisionCache
from app.services.semantic_resolution.identity_memo import IdentityMemo
from app.services.semantic_resolution.limits import BatchLimitExceeded, BatchLimits
from app.services.semantic_resolution.models import IDENTITY_POLICY_VERSION, QUESTION_TYPES, QUEUE_JSON_KEY
from app.services.semantic_resolution.questions import validate_question
from app.services.semantic_resolution.queue import validate_queue

CURITIBA = "sem:BR:municipality:PR:CURITIBA"
CAMPINAS = "sem:BR:municipality:SP:CAMPINAS"
CAMPINAS_MG = "sem:BR:municipality:MG:CAMPINAS"
SAO_PAULO = "sem:BR:municipality:SP:SAO_PAULO"
RIO = "sem:BR:municipality:RJ:RIO_DE_JANEIRO"
PACKAGE = Path("app/services/semantic_resolution")
BANNED_CALLS = (
    "cleiton_governed_billable_ai_call",
    "cleiton_billable_ai_call",
    "generate_content",
    "generativeai",
    "openai",
    "Gemini",
    "IaConsumoEvento",
    "IaChamadaTentativa",
)


def _lane(lane_id, dimension_id, *, origins=None, service=None, entity_id=None):
    origin = (
        {"scope": "single", "kind": "municipality", "refs": list(origins)}
        if origins
        else {"scope": "unspecified", "kind": None, "refs": []}
    )
    return {
        "lane_id": lane_id,
        "origin": origin,
        "service_ref": service,
        "destination": {"kind": "pricing_dimension", "dimension_id": dimension_id, "entity_id": entity_id},
    }


def _contract(*dimensions, lanes=None, assertions=None, review="accepted"):
    built = list(lanes or [])
    if not built:
        built = [
            _lane(f"lane-{item['dimension_id']}", item["dimension_id"], entity_id=item.get("destination_entity_id"))
            for item in dimensions
        ]
    return {
        "semantic_contract_version": "1",
        "contract_id": "sfc:test",
        "revision": 1,
        "content_fingerprint": "sha256:test",
        "review_state": review,
        "source_bindings": {"fingerprint_match": True, "source_fingerprint_local": "sha256:test"},
        "entities": [],
        "pricing_dimensions": list(dimensions),
        "lanes": built,
        "price_rules": [],
        "assertions": list(assertions or []),
        "validation": {"status": "valid", "findings": []},
    }


def _municipality(dimension_id, entity_id, label):
    return {
        "dimension_id": dimension_id,
        "kind": "municipality",
        "destination_entity_id": entity_id,
        "source_label": label,
        "meaning_status": "explicit",
        "state": entity_id.split(":")[3],
    }


def _region(dimension_id, label):
    return {
        "dimension_id": dimension_id,
        "kind": "carrier_defined_region",
        "source_label": label,
        "meaning_status": "explicit",
        "state": None,
        "destination_entity_id": None,
    }


def _catalog():
    return AuxiliaryGeographyCatalog(
        [
            {"city": "Campinas", "state": "SP"},
            {"city": "Campinas", "state": "MG"},
            {"city": "Curitiba", "state": "PR"},
            {"city": "São Paulo", "state": "SP"},
            {"city": "Rio de Janeiro", "state": "RJ"},
        ],
        revision="geo-test",
    )


def _ambiguous_row(**extra):
    row = {
        "text": "Campinas",
        "candidate_evidence": [
            {"candidate_id": CAMPINAS, "material": "rodovia SP-340"},
            {"candidate_id": CAMPINAS_MG, "material": "rodovia MG-050"},
        ],
    }
    row.update(extra)
    return row


def _questions(preview):
    return preview["queue"]["questions"]


def _one(preview):
    questions = _questions(preview)
    assert len(questions) == 1
    return questions[0]


def test_unresolved_ids_distinguish_malformed_subjects_without_using_row_number():
    contract = _contract()
    foo = resolve_context(contract=contract, destination={"city": "Foo", "state": "ZZ", "row_ref": "1"})
    bar = resolve_context(contract=contract, destination={"city": "Bar", "state": "ZZ", "row_ref": "1"})
    again = resolve_context(contract=contract, destination={"city": "Foo", "state": "ZZ", "row_ref": "99"})
    assert foo["destination_entity"] is None
    assert bar["destination_entity"] is None
    assert foo["resolution_id"] != bar["resolution_id"]
    assert foo["resolution_id"] == again["resolution_id"]


def test_duplicate_external_row_id_keeps_both_internal_refs():
    preview = resolve_batch(
        [
            {"row_id": "dup", "city": "Curitiba", "state": "PR"},
            {"row_id": "dup", "city": "Campinas", "state": "SP"},
        ],
        contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
    )
    records = preview["row_records"]
    assert len(records) == 2
    assert records[0]["row_ref"] != records[1]["row_ref"]
    assert records[0]["external_row_id"] == records[1]["external_row_id"] == "dup"
    assert records[0]["resolution_id"]
    assert records[1]["resolution_id"]
    assert records[0]["resolution_id"] != records[1]["resolution_id"]
    assert "dup" not in preview["row_refs"]


def test_invalid_rows_are_explained():
    preview = resolve_batch(
        ["bad", {"row_id": "empty"}, {"row_id": "ok", "city": "Curitiba", "state": "PR"}],
        contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
    )
    assert preview["stats"]["row_count"] == 3
    assert preview["row_records"][0]["status"] == "failed"
    assert preview["row_records"][0]["reason_codes"] == ["invalid_row"]
    assert preview["row_records"][1]["reason_codes"] == ["missing_minimum_identity"]
    assert preview["row_records"][2]["status"] == "resolved"
    assert preview["row_records"][2]["resolution_id"]


def test_identity_is_memoized_inside_the_batch():
    memo = IdentityMemo(catalog=None, identity_policy_version=IDENTITY_POLICY_VERSION)
    rows = [{"row_id": str(index), "city": "Curitiba", "state": "PR", "origin_city": "São Paulo", "origin_state": "SP"} for index in range(30)]
    preview = resolve_batch(
        rows,
        contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
        identity_memo=memo,
    )
    assert memo.evaluations == 2
    assert preview["stats"]["identity_evaluations"] == 2
    assert preview["stats"]["resolution_count"] == 1
    assert memo.hits > memo.evaluations


def test_cache_hit_uses_the_new_row_provenance():
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"))
    cache = LocalResolutionCache()
    resolve_batch(
        [{"row_id": "row-a", "city": "Curitiba", "state": "PR", "job_ref": "job-a"}],
        contract=contract,
        cache=cache,
        tenant_scope="tenant-a",
    )
    second = resolve_batch(
        [{"row_id": "row-b", "city": "Curitiba", "state": "PR", "job_ref": "job-b"}],
        contract=contract,
        cache=cache,
        tenant_scope="tenant-a",
    )
    assert cache.hits == 1
    blob = json.dumps(second["resolutions"])
    assert "row-a" not in blob
    assert "job-a" not in blob
    operational = json.dumps(second["row_records"][0]["operational_evidence"])
    assert "row-b" in operational
    assert "job-b" in operational
    assert "row-a" not in operational
    assert second["row_records"][0]["resolution_source"] == "cache"


def test_explicit_origin_contradicting_text_is_preserved():
    preview = resolve_batch(
        [
            {
                "row_id": "1",
                "city": "Curitiba",
                "state": "PR",
                "origin_entity_id": SAO_PAULO,
                "origin_city": "Rio de Janeiro",
                "origin_state": "RJ",
            }
        ],
        contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
    )
    resolution = preview["resolutions"][0]
    assert "structured_text_conflict" in resolution["reason_codes"]
    assert resolution["context"]["origin_entity_id"] == SAO_PAULO
    assert resolution["context"]["origin_city"] == "Rio de Janeiro"
    assert _one(preview)["eligibility"] == "human_only"
    assert _one(preview)["question_type"] is None


def test_coverage_is_prepared_once_for_many_contexts(monkeypatch):
    calls = {"count": 0}
    from app.services.semantic_resolution import snapshot as snapshot_module

    original = snapshot_module.convert_coverage_rows

    def wrapped(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(snapshot_module, "convert_coverage_rows", wrapped)
    coverage = [
        {"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior I"},
        {"destination_city": "Curitiba", "destination_uf": "PR", "freight_region": "Capital"},
        {"destination_city": "Santos", "destination_uf": "SP", "freight_region": "Litoral"},
    ]
    contract = _contract(
        _region("dim-interior-i", "Interior I"),
        _region("dim-capital", "Capital"),
        _region("dim-litoral", "Litoral"),
    )
    preview = resolve_batch(
        [
            {"row_id": "1", "city": "Campinas", "state": "SP"},
            {"row_id": "2", "city": "Curitiba", "state": "PR"},
            {"row_id": "3", "city": "Santos", "state": "SP"},
        ],
        contract=contract,
        coverage_rows=coverage,
    )
    assert calls["count"] == 1
    assert preview["stats"]["coverage_preparations"] == 1
    assert preview["stats"]["resolution_count"] == 3


def test_resolution_keeps_only_relevant_coverage_evidence():
    coverage = [
        {"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior I", "row_index": 1},
        {"destination_city": "Santos", "destination_uf": "SP", "freight_region": "Litoral", "row_index": 2},
    ]
    preview = resolve_batch(
        [{"row_id": "1", "city": "Campinas", "state": "SP"}],
        contract=_contract(_region("dim-interior-i", "Interior I"), _region("dim-litoral", "Litoral")),
        coverage_rows=coverage,
    )
    resolution = preview["resolutions"][0]
    attached = [item for item in resolution["evidence"] if item["source_type"] == "coverage"]
    assert attached
    assert all(item.get("original_destination_city") != "Santos" for item in attached)
    shared_cities = {item.get("original_destination_city") for item in preview["queue"]["shared_evidence"]}
    assert "Santos" in shared_cities
    assert "Campinas" in shared_cities
    assert len(preview["queue"]["shared_evidence"]) > len(attached)


def test_country_changes_the_coverage_fingerprint():
    base = {
        "destination_city": "Campinas",
        "destination_uf": "SP",
        "destination_country": "BR",
        "origin_city": "São Paulo",
        "origin_uf": "SP",
        "origin_country": "BR",
        "freight_region": "Interior I",
        "service_ref": "express",
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
    }
    assert coverage_content_fingerprint([base]) != coverage_content_fingerprint([dict(base, destination_country="AR")])
    assert coverage_content_fingerprint([base]) != coverage_content_fingerprint([dict(base, origin_country="AR")])
    assert coverage_content_fingerprint([base]) == coverage_content_fingerprint([dict(base, destination_country="Brasil")])
    visual = dict(base, row_index=9, source_file_name="capa.xlsx")
    assert coverage_content_fingerprint([base]) == coverage_content_fingerprint([visual])
    assert coverage_content_fingerprint([base, dict(base, destination_city="Santos")]) == coverage_content_fingerprint(
        [dict(base, destination_city="Santos"), base]
    )


def test_question_schema_and_allowed_types():
    preview = resolve_batch([_ambiguous_row(row_id="1")], contract=_contract(), catalog=_catalog())
    question = _one(preview)
    assert validate_question(question)["ok"] is True
    assert validate_queue(preview["queue"])["ok"] is True
    assert question["question_type"] == "municipality_text_disambiguation"
    assert set(QUESTION_TYPES) == {
        "municipality_text_disambiguation",
        "dimension_label_disambiguation",
        "explicit_documentary_relation_extraction",
    }
    assert question["ai_execution"]["model_id"] is None
    assert question["ai_execution"]["prompt_version"] is None
    assert question["attempt_refs"] == []
    assert preview["queue"]["attempts"] == []


def test_dimension_label_can_be_ai_eligible_and_same_evidence_is_human_only():
    contract = _contract(_region("dim-a", "Interior I"), _region("dim-b", "Interior I"))
    coverage = [{"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior I"}]
    distinguished = resolve_batch(
        [
            {
                "row_id": "1",
                "city": "Campinas",
                "state": "SP",
                "candidate_evidence": [
                    {"candidate_id": "dim-a", "material": "tabela A"},
                    {"candidate_id": "dim-b", "material": "tabela B"},
                ],
            }
        ],
        contract=contract,
        coverage_rows=coverage,
    )
    question = _one(distinguished)
    assert question["question_type"] == "dimension_label_disambiguation"
    assert question["eligibility"] == "ai_eligible"
    same = resolve_batch(
        [
            {
                "row_id": "1",
                "city": "Campinas",
                "state": "SP",
                "candidate_evidence": [
                    {"candidate_id": "dim-a", "material": "mesma linha"},
                    {"candidate_id": "dim-b", "material": "mesma linha"},
                ],
            }
        ],
        contract=contract,
        coverage_rows=coverage,
    )
    assert _one(same)["eligibility"] == "human_only"
    assert _one(same)["question_type"] is None


def test_documentary_relation_uses_existing_dimensions_only():
    zones = [
        {
            "dimension_id": "dim-interior-i",
            "kind": "zone",
            "source_label": "Interior I",
            "meaning_status": "explicit",
            "destination_entity_id": None,
        },
        {
            "dimension_id": "dim-interior-ii",
            "kind": "zone",
            "source_label": "Interior II",
            "meaning_status": "explicit",
            "destination_entity_id": None,
        },
    ]
    contract = _contract(*zones)
    preview = resolve_batch(
        [
            {
                "row_id": "1",
                "city": "Campinas",
                "state": "SP",
                "documentary_excerpt": "clausula distingue Interior I e Interior II",
                "documentary_candidate_ids": ["dim-interior-i", "dim-interior-ii"],
                "documentary_evidence": [
                    {"candidate_id": "dim-interior-i", "material": "clausula 4.1"},
                    {"candidate_id": "dim-interior-ii", "material": "clausula 4.2"},
                ],
            }
        ],
        contract=contract,
    )
    question = _one(preview)
    assert question["question_type"] == "explicit_documentary_relation_extraction"
    assert question["eligibility"] == "ai_eligible"
    blocked = resolve_batch(
        [
            {
                "row_id": "1",
                "city": "Campinas",
                "state": "SP",
                "documentary_excerpt": "criar regiao nova",
                "documentary_candidate_ids": ["dim-nova"],
                "documentary_evidence": [
                    {"candidate_id": "dim-nova", "material": "clausula nova"},
                    {"candidate_id": "dim-interior-i", "material": "clausula antiga"},
                ],
            }
        ],
        contract=contract,
    )
    assert _one(blocked)["eligibility"] == "human_only"
    assert _one(blocked)["question_type"] is None


def test_benchmark_classifications_and_dedup():
    catalog = _catalog()
    eligible = resolve_batch([_ambiguous_row(row_id="a")], contract=_contract(), catalog=catalog)
    assert _one(eligible)["eligibility"] == "ai_eligible"
    assert _one(eligible)["status"] == "pending"

    interior = resolve_batch(
        [{"row_id": "b", "city": "Campinas", "state": "SP"}],
        contract=_contract(_region("dim-interior-i", "Interior I")),
    )
    assert _one(interior)["eligibility"] == "human_only"
    assert "carrier_region_membership_missing" in _one(interior)["reason_codes"]

    conflict = resolve_batch(
        [{"row_id": "c", "city": "Campinas", "state": "SP"}],
        contract=_contract(_region("dim-i", "Interior I"), _region("dim-ii", "Interior II")),
        coverage_rows=[
            {"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior I"},
            {"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior II"},
        ],
    )
    assert _one(conflict)["eligibility"] == "conflicting_authoritative_evidence"
    assert _one(conflict)["question_type"] is None
    assert _one(conflict)["status"] == "blocked"

    missing = resolve_batch(
        [{"row_id": "d", "text": "Campinas"}],
        contract=_contract(),
        catalog=catalog,
    )
    assert _one(missing)["eligibility"] == "unresolved_missing_evidence"
    assert _one(missing)["question_type"] is None

    repeated = resolve_batch(
        [_ambiguous_row(row_id=f"r{index}") for index in range(100)],
        contract=_contract(),
        catalog=catalog,
    )
    question = _one(repeated)
    assert len(question["consumer_refs"]) == 100
    assert len(question["context_refs"]) == 1
    assert repeated["stats"]["question_count"] == 1
    assert repeated["stats"]["consumer_count"] == 100


def test_same_question_can_serve_more_than_one_context():
    catalog = _catalog()
    preview = resolve_batch(
        [
            _ambiguous_row(row_id="1", service_ref="express"),
            _ambiguous_row(row_id="2", service_ref="express"),
        ],
        contract=_contract(),
        catalog=catalog,
    )
    question = _one(preview)
    assert len(question["consumer_refs"]) == 2
    assert len(question["context_refs"]) == 1
    assert set(preview["queue"]["context_question_refs"]) == set(question["context_refs"])


def test_reordering_does_not_change_question_key_or_dependency_fingerprint():
    contract = _contract(_region("dim-a", "Interior I"), _region("dim-b", "Interior I"))
    coverage = [
        {"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior I", "row_index": 1},
        {"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior I", "row_index": 2},
    ]
    evidence = [
        {"candidate_id": "dim-a", "material": "tabela A"},
        {"candidate_id": "dim-b", "material": "tabela B"},
    ]
    forward = resolve_batch(
        [{"row_id": "1", "city": "Campinas", "state": "SP", "candidate_evidence": evidence}],
        contract=contract,
        coverage_rows=coverage,
    )
    backward = resolve_batch(
        [{"row_id": "9", "city": "Campinas", "state": "SP", "candidate_evidence": list(reversed(evidence))}],
        contract=contract,
        coverage_rows=list(reversed(coverage)),
    )
    left = _one(forward)
    right = _one(backward)
    assert left["semantic_question_key"] == right["semantic_question_key"]
    assert left["dependency_fingerprints"]["decision"] == right["dependency_fingerprints"]["decision"]
    assert left["candidate_ids"] == ["dim-a", "dim-b"]


def test_human_acceptance_is_reused_and_rejection_is_not_asked_again():
    catalog = _catalog()
    rows = [_ambiguous_row(row_id="1")]
    cache = HumanDecisionCache()
    first = resolve_batch(rows, contract=_contract(), catalog=catalog, human_decision_cache=cache)
    question = _one(first)
    assert question["eligibility"] == "ai_eligible"
    cache.put(
        question["semantic_question_key"],
        question["dependency_fingerprints"]["decision"],
        {"review_state": "rejected", "decision_ref": "dec-reject", "decision_revision": "hr-1"},
    )
    rejected = resolve_batch(rows, contract=_contract(), catalog=catalog, human_decision_cache=cache)
    blocked = _one(rejected)
    assert blocked["eligibility"] == "human_only"
    assert blocked["status"] == "blocked"
    assert blocked["review_state"] == "rejected"
    assert blocked["resolution_source"] == "cache"
    assert blocked["question_type"] is None
    assert blocked["eligibility"] != "ai_eligible"

    changed = resolve_batch(
        [
            {
                "row_id": "1",
                "text": "Campinas",
                "candidate_evidence": [
                    {"candidate_id": CAMPINAS, "material": "nova evidencia SP"},
                    {"candidate_id": CAMPINAS_MG, "material": "nova evidencia MG"},
                ],
            }
        ],
        contract=_contract(),
        catalog=catalog,
        human_decision_cache=cache,
    )
    assert _one(changed)["eligibility"] == "ai_eligible"
    assert _one(changed)["semantic_question_key"] != question["semantic_question_key"]

    accepted_cache = HumanDecisionCache()
    current = resolve_batch(rows, contract=_contract(), catalog=catalog, human_decision_cache=accepted_cache)
    current_question = _one(current)
    accepted_cache.put(
        current_question["semantic_question_key"],
        current_question["dependency_fingerprints"]["decision"],
        {"review_state": "accepted", "decision_ref": "dec-accept", "chosen_candidate_id": CAMPINAS},
    )
    reused = resolve_batch(rows, contract=_contract(), catalog=catalog, human_decision_cache=accepted_cache)
    reused_question = _one(reused)
    assert reused_question["status"] == "resolved"
    assert reused_question["review_state"] == "accepted"
    assert reused_question["resolution_source"] == "cache"
    assert reused_question["decision_ref"] == "dec-accept"
    assert reused_question["eligibility"] == "deterministically_resolvable"


def test_queue_persistence_preserves_and_blocks_stale_or_expired_records():
    preview = resolve_batch(
        [{"row_id": "1", "city": "Curitiba", "state": "PR"}],
        contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
        job_ref="job-1",
        tenant_scope="tenant-a",
    )
    queue = preview["queue"]
    record = {
        "status": "needs_review",
        "expires_at": "2026-10-10T00:00:00",
        CONTRACT_KEY: {"contract_id": "sfc:test"},
        "pricing_contract": {"rules": []},
        TECHNICAL_JSON_KEY: {"resolutions": []},
    }
    attached = attach_semantic_resolution_queue(record, {**queue, "activated": True, "operational": True})
    stored = attached[QUEUE_JSON_KEY]
    assert stored["activated"] is False
    assert stored["operational"] is False
    assert stored["preview_only"] is True
    assert stored["dependency_status"] == "current"
    assert QUEUE_JSON_KEY not in attached[CONTRACT_KEY]
    assert QUEUE_JSON_KEY not in attached["pricing_contract"]
    assert attached[TECHNICAL_JSON_KEY] == {"resolutions": []}

    updated = dict(attached)
    updated[QUEUE_JSON_KEY] = {"activated": True, "questions": []}
    preserve_semantic_resolution_queue(
        stored=attached,
        updated=updated,
        payload={QUEUE_JSON_KEY: {"questions": [{"injected": True}]}},
    )
    assert updated[QUEUE_JSON_KEY]["questions"] == stored["questions"]
    assert updated[QUEUE_JSON_KEY]["activated"] is False

    erased = {"freight_tables": []}
    preserve_semantic_resolution_queue(
        stored={},
        updated=erased,
        payload={QUEUE_JSON_KEY: {"questions": [{"injected": True}]}},
    )
    assert QUEUE_JSON_KEY not in erased

    stale = attach_semantic_resolution_queue(
        record,
        queue,
        current_dependencies={
            "coverage_fingerprint": "sha256:other",
            "authorization_fingerprint": queue["dependency_snapshot"]["authorization_fingerprint"],
            "contract_content_fingerprint": queue["dependency_snapshot"]["contract_content_fingerprint"],
        },
    )
    assert stale[QUEUE_JSON_KEY]["dependency_status"] == "stale"
    assert stale[QUEUE_JSON_KEY]["activated"] is False
    with pytest.raises(ValueError):
        attach_semantic_resolution_queue({"status": "expired"}, queue)
    with pytest.raises(ValueError):
        attach_semantic_resolution_queue(record, {"schema_version": "9"})


def test_batch_limits_fail_closed():
    contract = _contract()
    with pytest.raises(BatchLimitExceeded) as exceeded:
        resolve_batch(
            [{"city": "Curitiba", "state": "PR"}, {"city": "Campinas", "state": "SP"}, {"text": "Santos"}],
            contract=contract,
            limits=BatchLimits(max_rows=2),
        )
    assert exceeded.value.reason_code == "max_rows"
    with pytest.raises(BatchLimitExceeded) as identities:
        resolve_batch(
            [
                {"city": "Curitiba", "state": "PR"},
                {"city": "Campinas", "state": "SP"},
            ],
            contract=contract,
            limits=BatchLimits(max_unique_identities=1),
        )
    assert identities.value.reason_code == "max_unique_identities"
    with pytest.raises(BatchLimitExceeded):
        BatchLimits(max_questions=None)
    with pytest.raises(BatchLimitExceeded) as questions:
        resolve_batch(
            [
                _ambiguous_row(row_id="1", text="Campinas"),
                _ambiguous_row(row_id="2", text="Curitiba", candidate_evidence=[
                    {"candidate_id": "sem:BR:municipality:PR:CURITIBA", "material": "uma pista"},
                    {"candidate_id": "sem:BR:municipality:RJ:CURITIBA", "material": "outra pista"},
                ]),
            ],
            contract=contract,
            catalog=_catalog(),
            limits=BatchLimits(max_questions=1),
        )
    assert questions.value.reason_code == "max_questions"
    with pytest.raises(BatchLimitExceeded) as size:
        resolve_batch(
            [{"city": "Curitiba", "state": "PR"}],
            contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
            limits=BatchLimits(max_queue_serialized_bytes=1),
        )
    assert size.value.reason_code == "max_queue_serialized_bytes"


def test_one_context_failure_does_not_drop_the_batch_and_a_bug_aborts_it(monkeypatch):
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"))
    preview = resolve_batch(
        [
            {"row_id": "bad", "city": ["Campinas"], "state": "SP"},
            {"row_id": "ok", "city": "Curitiba", "state": "PR"},
        ],
        contract=contract,
    )
    assert preview["row_records"][0]["status"] == "failed"
    assert preview["row_records"][0]["reason_codes"] == ["invalid_context_value"]
    assert preview["row_records"][1]["status"] == "resolved"

    def explode(**_kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr("app.services.semantic_resolution.batch.resolve_context", explode)
    with pytest.raises(RuntimeError, match="bug"):
        resolve_batch([{"row_id": "ok", "city": "Curitiba", "state": "PR"}], contract=contract)


def test_ten_thousand_repeated_rows_stay_on_unique_identity_and_context():
    rows = [
        {
            "row_id": f"r{index}",
            "city": "Curitiba",
            "state": "PR",
            "origin_city": "São Paulo",
            "origin_state": "SP",
        }
        for index in range(10_000)
    ]
    dimension = _municipality("dim-curitiba", CURITIBA, "Curitiba/PR")
    preview = resolve_batch(
        rows,
        contract=_contract(dimension, lanes=[_lane("lane-sp", "dim-curitiba", origins=[SAO_PAULO], entity_id=CURITIBA)]),
    )
    assert preview["stats"]["row_count"] == 10_000
    assert preview["stats"]["identity_evaluations"] == 2
    assert preview["stats"]["identity_count"] == 2
    assert preview["stats"]["resolution_count"] == 1
    assert preview["stats"]["question_count"] == 0
    assert preview["stats"]["ai_eligible_count"] == 0
    assert preview["stats"]["shared_evidence_count"] == 0
    assert len(preview["row_records"]) == 10_000


def _comp03_contract():
    path = Path("tests/test_scrum146_semantic_resolution.py")
    spec = importlib.util.spec_from_file_location("scrum146_lot4_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._comp03_contract()


def test_comp03_stays_deterministic_without_questions():
    preview = resolve_batch(
        [{"row_id": "comp03", "city": "Curitiba", "state": "PR", "origin_city": "São Paulo", "origin_state": "SP"}],
        contract=_comp03_contract(),
    )
    resolution = preview["resolutions"][0]
    assert resolution["resolution_status"] == "resolved"
    assert resolution["resolution_basis"] == "exact_entity"
    assert resolution["destination_entity"]["entity_id"] == CURITIBA
    assert preview["queue"]["questions"] == []
    assert preview["stats"]["ai_eligible_count"] == 0
    assert preview["queue"]["attempts"] == []
    assert preview["activated"] is False
    assert preview["operational"] is False


def test_unresolved_destination_keeps_distinct_explicit_origins():
    contract = _contract()
    scope = {
        "service_ref": "express",
        "carrier_scope_ref": "carrier-a",
        "effective_date": "2026-01-01",
    }
    from_sp = resolve_context(
        contract=contract,
        destination={"city": "Foo", "state": "ZZ", "row_ref": "1"},
        context={**scope, "origin_entity_id": SAO_PAULO},
    )
    from_rj = resolve_context(
        contract=contract,
        destination={"city": "Foo", "state": "ZZ", "row_ref": "2"},
        context={**scope, "origin_entity_id": RIO},
    )
    assert from_sp["destination_entity"] is None
    assert from_rj["destination_entity"] is None
    assert from_sp["resolution_id"] != from_rj["resolution_id"]
    assert from_sp["context"]["origin_entity_id"] == SAO_PAULO
    assert from_rj["context"]["origin_entity_id"] == RIO
    assert from_sp["context"]["service_ref"] == "express"
    assert from_sp["context"]["carrier_scope_ref"] == "carrier-a"
    assert from_sp["context"]["effective_date"] == "2026-01-01"
    assert from_sp["contract_ref"]["contract_id"] == "sfc:test"


def test_same_unresolved_pendency_on_two_rows_keeps_one_resolution_id():
    preview = resolve_batch(
        [
            {"row_id": "a", "city": "Foo", "state": "ZZ", "origin_entity_id": SAO_PAULO},
            {"row_id": "b", "city": "Foo", "state": "ZZ", "origin_entity_id": SAO_PAULO},
        ],
        contract=_contract(),
    )
    records = preview["row_records"]
    assert records[0]["row_ref"] != records[1]["row_ref"]
    assert records[0]["resolution_id"] == records[1]["resolution_id"]
    assert records[0]["resolution_id"]


def test_invalid_destination_preserves_conflicting_origin_context():
    preview = resolve_batch(
        [
            {
                "row_id": "1",
                "city": "Foo",
                "state": "ZZ",
                "origin_entity_id": SAO_PAULO,
                "origin_city": "Rio de Janeiro",
                "origin_state": "RJ",
                "service_ref": "express",
                "carrier_scope_ref": "carrier-a",
                "effective_date": "2026-01-01",
            }
        ],
        contract=_contract(),
        catalog=_catalog(),
    )
    resolution = preview["resolutions"][0]
    assert resolution["destination_entity"] is None
    assert "invalid_state" in resolution["reason_codes"]
    assert "structured_text_conflict" in resolution["reason_codes"]
    assert resolution["context"]["origin_entity_id"] == SAO_PAULO
    assert resolution["context"]["origin_city"] == "Rio de Janeiro"
    assert resolution["context"]["origin_state"] == "RJ"
    assert resolution["context"]["service_ref"] == "express"
    assert resolution["context"]["carrier_scope_ref"] == "carrier-a"
    assert resolution["context"]["effective_date"] == "2026-01-01"


def _campinas_row(row_id, service, evidence):
    return {
        "row_id": row_id,
        "text": "Campinas",
        "service_ref": service,
        "candidate_evidence": evidence,
    }


def _express_economy_rows():
    express = _campinas_row(
        "express",
        "express",
        [
            {"candidate_id": CAMPINAS, "material": "rodovia SP-340"},
            {"candidate_id": CAMPINAS_MG, "material": "nao atende expresso"},
        ],
    )
    economy = _campinas_row(
        "economy",
        "economy",
        [
            {"candidate_id": CAMPINAS, "material": "nao atende economico"},
            {"candidate_id": CAMPINAS_MG, "material": "rodovia MG-050"},
        ],
    )
    return express, economy


def _question_by_service(preview):
    return {question["constraints"]["service_ref"]: question for question in _questions(preview)}


def _decision_signature(preview):
    refs = {record["external_row_id"]: record["context_ref"] for record in preview["row_records"]}
    signature = []
    for service in ("economy", "express"):
        question = _question_by_service(preview)[service]
        signature.append(
            (
                service,
                question["semantic_question_key"],
                question["dependency_fingerprints"]["decision"],
                question["eligibility"],
                question["status"],
                question["review_state"],
                question["resolution_source"],
                question["decision_ref"],
                tuple(question["reason_codes"]),
                tuple(question["context_refs"]),
                tuple(question["context_refs"]) == (refs[service],),
            )
        )
    return tuple(signature)


def test_express_and_economy_with_different_evidence_are_two_questions():
    express, economy = _express_economy_rows()
    catalog = _catalog()
    contract = _contract()
    forward = resolve_batch([express, economy], contract=contract, catalog=catalog)
    backward = resolve_batch([economy, express], contract=contract, catalog=catalog)
    assert len(_questions(forward)) == 2
    assert _decision_signature(forward) == _decision_signature(backward)
    services = _question_by_service(forward)
    assert services["express"]["semantic_question_key"] != services["economy"]["semantic_question_key"]
    assert services["express"]["candidate_ids"] == services["economy"]["candidate_ids"] == [CAMPINAS_MG, CAMPINAS]
    assert services["express"]["eligibility"] == "ai_eligible"
    assert services["economy"]["eligibility"] == "ai_eligible"
    assert set(services["express"]["context_refs"]).isdisjoint(services["economy"]["context_refs"])


def test_same_service_evidence_and_candidates_collapse_to_one_question():
    evidence = [
        {"candidate_id": CAMPINAS, "material": "rodovia SP-340"},
        {"candidate_id": CAMPINAS_MG, "material": "rodovia MG-050"},
    ]
    preview = resolve_batch(
        [_campinas_row(f"r{index}", "express", evidence) for index in range(4)],
        contract=_contract(),
        catalog=_catalog(),
    )
    question = _one(preview)
    assert question["constraints"]["service_ref"] == "express"
    assert question["candidate_ids"] == [CAMPINAS_MG, CAMPINAS]
    assert len(question["consumer_refs"]) == 4
    assert len(question["context_refs"]) == 1


def test_same_subject_and_candidates_do_not_merge_when_dependency_fingerprint_differs():
    evidence = [
        {"candidate_id": CAMPINAS, "material": "rodovia SP-340"},
        {"candidate_id": CAMPINAS_MG, "material": "rodovia MG-050"},
    ]
    preview = resolve_batch(
        [
            _campinas_row("express", "express", evidence),
            _campinas_row("economy", "economy", evidence),
        ],
        contract=_contract(),
        catalog=_catalog(),
    )
    questions = _questions(preview)
    assert len(questions) == 2
    services = _question_by_service(preview)
    assert services["express"]["subject"]["text"] == services["economy"]["subject"]["text"] == "CAMPINAS"
    assert services["express"]["candidate_ids"] == services["economy"]["candidate_ids"]
    assert services["express"]["dependency_fingerprints"]["decision"] != services["economy"]["dependency_fingerprints"]["decision"]
    assert services["express"]["semantic_question_key"] != services["economy"]["semantic_question_key"]
    assert _decision_signature(preview) == _decision_signature(
        resolve_batch(
            [
                _campinas_row("economy", "economy", evidence),
                _campinas_row("express", "express", evidence),
            ],
            contract=_contract(),
            catalog=_catalog(),
        )
    )


def _isolated_human_batch(review_state, decision_ref):
    express, economy = _express_economy_rows()
    catalog = _catalog()
    contract = _contract()
    cache = HumanDecisionCache()
    seeded = resolve_batch([express], contract=contract, catalog=catalog, human_decision_cache=cache)
    question = _one(seeded)
    cache.put(
        question["semantic_question_key"],
        question["dependency_fingerprints"]["decision"],
        {"review_state": review_state, "decision_ref": decision_ref, "decision_revision": "hr-express"},
    )
    forward = resolve_batch([express, economy], contract=contract, catalog=catalog, human_decision_cache=cache)
    backward = resolve_batch([economy, express], contract=contract, catalog=catalog, human_decision_cache=cache)
    return forward, backward


def test_human_rejection_of_express_does_not_follow_economy_in_either_order():
    forward, backward = _isolated_human_batch("rejected", "dec-express-reject")
    assert _decision_signature(forward) == _decision_signature(backward)
    for preview in (forward, backward):
        services = _question_by_service(preview)
        express = services["express"]
        economy = services["economy"]
        assert express["eligibility"] == "human_only"
        assert express["status"] == "blocked"
        assert express["review_state"] == "rejected"
        assert express["resolution_source"] == "cache"
        assert express["eligibility"] != "ai_eligible"
        assert "human_decision_rejected" in express["reason_codes"]
        assert economy["eligibility"] == "ai_eligible"
        assert economy["status"] == "pending"
        assert economy["review_state"] == "requires_review"
        assert economy["resolution_source"] is None
        assert economy["decision_ref"] is None
        assert "human_decision_rejected" not in economy["reason_codes"]
        assert "human_decision_reused" not in economy["reason_codes"]


def test_human_acceptance_of_express_does_not_authorize_economy_in_either_order():
    forward, backward = _isolated_human_batch("accepted", "dec-express-accept")
    assert _decision_signature(forward) == _decision_signature(backward)
    for preview in (forward, backward):
        services = _question_by_service(preview)
        express = services["express"]
        economy = services["economy"]
        assert express["eligibility"] == "deterministically_resolvable"
        assert express["status"] == "resolved"
        assert express["review_state"] == "accepted"
        assert express["resolution_source"] == "cache"
        assert express["decision_ref"] == "dec-express-accept"
        assert express["eligibility"] != "ai_eligible"
        assert economy["eligibility"] == "ai_eligible"
        assert economy["status"] == "pending"
        assert economy["review_state"] == "requires_review"
        assert economy["resolution_source"] is None
        assert economy["decision_ref"] is None
        assert economy["eligibility"] != "deterministically_resolvable"


def test_queue_material_dependency_changes_are_stale_and_reorder_stays_current():
    preview = resolve_batch(
        [{"row_id": "1", "city": "Curitiba", "state": "PR"}],
        contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
        catalog=_catalog(),
        job_ref="job-deps",
        tenant_scope="tenant-a",
    )
    queue = preview["queue"]
    snapshot = queue["dependency_snapshot"]
    record = {"status": "needs_review"}
    unchanged = attach_semantic_resolution_queue(record, queue, current_dependencies=dict(snapshot))
    assert unchanged[QUEUE_JSON_KEY]["dependency_status"] == "current"
    assert unchanged[QUEUE_JSON_KEY]["questions"] == queue["questions"]

    reordered = {key: snapshot[key] for key in reversed(tuple(snapshot))}
    geography = reordered.get("geography_revision")
    if isinstance(geography, dict):
        reordered["geography_revision"] = {key: geography[key] for key in reversed(tuple(geography))}
    assert attach_semantic_resolution_queue(record, queue, current_dependencies=reordered)[QUEUE_JSON_KEY]["dependency_status"] == "current"

    mutations = {
        "policy_version": "sr-policy-v2",
        "geography_revision": {"revision": "geo-other", "fingerprint": "sha256:other", "official": False},
        "human_decision_revision": "hr-other",
        "revision": 99,
        "authorization_fingerprint": "sha256:auth-other",
        "coverage_fingerprint": "sha256:cov-other",
    }
    for field, value in mutations.items():
        current = dict(snapshot)
        assert current["contract_content_fingerprint"] == snapshot["contract_content_fingerprint"]
        current[field] = value
        attached = attach_semantic_resolution_queue(record, queue, current_dependencies=current)
        stored = attached[QUEUE_JSON_KEY]
        assert stored["dependency_status"] == "stale", field
        assert stored["activated"] is False
        assert stored["operational"] is False
        assert stored["questions"] == queue["questions"]
        assert stored["decisions"] == queue["decisions"]


def test_lot5_does_not_call_ai_billing_or_runtime_cutover():
    for path in PACKAGE.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in BANNED_CALLS:
            assert token not in source, f"{path.name} contém {token}"
    preview = resolve_batch([_ambiguous_row(row_id="1")], contract=_contract(), catalog=_catalog())
    assert preview["queue"]["attempts"] == []
    assert _one(preview)["eligibility"] == "ai_eligible"
    assert _one(preview)["resolution_source"] is None
    for function in (
        build_freight_pricing_index,
        build_coverage_index,
        calculate_weight_freight,
        _calculate_expected_freight_row,
    ):
        assert "semantic_resolution_queue" not in inspect.getsource(function)
    assert preview["queue"]["activated"] is False
    assert preview["queue"]["operational"] is False
