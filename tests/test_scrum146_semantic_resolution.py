"""SCRUM-146 lote 4: Semantic Resolution Preview, sem cutover."""
from __future__ import annotations

import copy
import inspect
import json
import re
from pathlib import Path

import pytest

from app.agente_compara_doc_service import (
    _pricing_rule_lookup_candidates,
    _resolve_region_without_coverage,
    build_coverage_index,
    build_freight_pricing_index,
    build_pricing_contract,
    calculate_weight_freight,
)
from app.services.semantic_freight_contract.compiler import compile_pricing_preview
from app.services.semantic_freight_contract.models import TECHNICAL_JSON_KEY as CONTRACT_KEY
from app.services.semantic_freight_contract.validator import assign_canonical_ids
from app.services.semantic_resolution import (
    RESOLUTION_SCHEMA,
    TECHNICAL_JSON_KEY,
    AuxiliaryGeographyCatalog,
    LocalResolutionCache,
    attach_semantic_resolution_preview,
    capital_interior_membership,
    catalog_from_locality_rows,
    experimental_projection_ref,
    identify_place,
    municipality_entity_id,
    place_candidates,
    contract_resolution_authorization_fingerprint,
    preserve_semantic_resolution_preview,
    resolution_cache_key,
    resolve_batch,
    resolve_context,
)
from app.services.semantic_resolution.coverage import convert_coverage_rows, coverage_content_fingerprint
from app.services.semantic_resolution.models import CACHE_KEY_FIELDS, CAPITAL_INTERIOR_REASONS, RESERVED_INACTIVE_BASES
from app.services.semantic_resolution.validator import validate_resolution

CURITIBA = "sem:BR:municipality:PR:CURITIBA"
CAMPINAS = "sem:BR:municipality:SP:CAMPINAS"
SAO_PAULO = "sem:BR:municipality:SP:SAO_PAULO"
RIO = "sem:BR:municipality:RJ:RIO_DE_JANEIRO"
PACKAGE = Path("app/services/semantic_resolution")


def _lane(lane_id: str, dimension_id: str, *, origins: list[str] | None = None, service: str | None = None, entity_id: str | None = None) -> dict:
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


def _contract(*dimensions: dict, lanes: list[dict] | None = None, assertions: list[dict] | None = None, review: str = "accepted") -> dict:
    built_lanes = list(lanes or [])
    if not built_lanes:
        built_lanes = [_lane(f"lane-{item['dimension_id']}", item["dimension_id"], entity_id=item.get("destination_entity_id")) for item in dimensions]
    return {
        "semantic_contract_version": "1",
        "contract_id": "sfc:test",
        "revision": 1,
        "content_fingerprint": "sha256:test",
        "review_state": review,
        "source_bindings": {"fingerprint_match": True, "source_fingerprint_local": "sha256:test"},
        "entities": [],
        "pricing_dimensions": list(dimensions),
        "lanes": built_lanes,
        "price_rules": [],
        "assertions": list(assertions or []),
        "validation": {"status": "valid", "findings": []},
    }


def _municipality(dimension_id: str, entity_id: str, label: str) -> dict:
    return {
        "dimension_id": dimension_id,
        "kind": "municipality",
        "destination_entity_id": entity_id,
        "source_label": label,
        "meaning_status": "explicit",
        "state": entity_id.split(":")[3],
    }


def _state(dimension_id: str, state: str, label: str) -> dict:
    return {
        "dimension_id": dimension_id,
        "kind": "state",
        "state": state,
        "source_label": label,
        "meaning_status": "explicit",
        "destination_entity_id": f"geo:BR:state:{state}",
    }


def _region(dimension_id: str, label: str, *, kind: str = "carrier_defined_region", state: str | None = None) -> dict:
    return {
        "dimension_id": dimension_id,
        "kind": kind,
        "source_label": label,
        "meaning_status": "explicit",
        "state": state,
        "destination_entity_id": None,
    }


def _decision(resolution: dict) -> tuple:
    return (
        resolution["resolution_status"],
        resolution["review_state"],
        resolution["resolution_basis"],
        resolution["matched_pricing_dimension_id"],
        tuple(resolution["candidate_dimension_ids"]),
        tuple(resolution["reason_codes"]),
        tuple(resolution["matched_lane_ids"]),
    )


def _catalog() -> AuxiliaryGeographyCatalog:
    return AuxiliaryGeographyCatalog(
        [
            {"city": "Curitiba", "state": "PR", "id_cidade": 1},
            {"city": "Campinas", "state": "SP", "id_cidade": 2},
            {"city": "São Paulo", "state": "SP", "id_cidade": 3},
        ],
        revision=None,
    )


def test_curitiba_exact_entity_uses_semantic_id_not_legacy_key():
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"))
    resolution = resolve_context(contract=contract, destination={"city": "Curitiba", "state": "PR"})
    assert resolution["destination_entity"]["entity_id"] == CURITIBA
    assert resolution["destination_entity"]["kind"] == "municipality"
    assert resolution["destination_entity"]["name"] == "CURITIBA"
    assert resolution["resolution_basis"] == "exact_entity"
    assert resolution["resolution_status"] == "resolved"
    assert resolution["review_state"] == "accepted"
    assert resolution["matched_pricing_dimension_id"] == "dim-curitiba"
    assert resolution["matched_lane_ids"] == ["lane-dim-curitiba"]
    assert "exact_entity_match" in resolution["reason_codes"]
    assert "PR|CURITIBA" not in json.dumps(resolution)
    assert "lookup_key" not in json.dumps(resolution)
    assert validate_resolution(resolution)["ok"] is True


def test_heterogeneous_labels_share_one_identity_without_delimiter_parser():
    catalog = _catalog()
    labels = ["Curitiba/PR", "Curitiba - PR", "PR Curitiba", "Cidade: Curitiba | UF: PR"]
    assert {tuple(place_candidates(label)) for label in labels} == {(("CURITIBA", "PR"),)}
    identities = [
        identify_place(text=label, catalog=catalog)["entity"]["entity_id"]
        for label in labels
    ]
    assert identities == [CURITIBA, CURITIBA, CURITIBA, CURITIBA]
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "qualquer rótulo"))
    resolutions = [
        resolve_context(contract=contract, destination={"text": label}, catalog=catalog)
        for label in labels
    ]
    assert {item["destination_entity"]["entity_id"] for item in resolutions} == {CURITIBA}
    assert {item["resolution_basis"] for item in resolutions} == {"exact_entity"}
    source = (PACKAGE / "identity.py").read_text(encoding="utf-8")
    assert 'split("/")' not in source
    assert "split('-')" not in source


def test_structured_field_contradicting_text_requires_review():
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"))
    resolution = resolve_context(
        contract=contract,
        destination={"city": "Curitiba", "state": "PR", "text": "Campinas/SP"},
        catalog=_catalog(),
    )
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["review_state"] == "requires_review"
    assert "structured_text_conflict" in resolution["reason_codes"]
    assert resolution["matched_pricing_dimension_id"] is None
    assert resolution["resolution_basis"] == "unresolved"


def test_structured_state_rejects_text_from_another_uf():
    catalog = _catalog()
    identified = identify_place(state="PR", text="Campinas/SP", catalog=catalog)
    assert identified["ok"] is False
    assert identified["entity"] is None
    assert identified["reason_codes"] == ["structured_text_conflict"]
    resolution = resolve_context(
        contract=_contract(
            _municipality("dim-campinas", CAMPINAS, "Campinas/SP"),
            _state("dim-sp", "SP", "SAO PAULO"),
        ),
        destination={"state": "PR", "text": "Campinas/SP"},
        catalog=catalog,
    )
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["review_state"] == "requires_review"
    assert resolution["reason_codes"] == ["structured_text_conflict"]
    assert resolution["destination_entity"] is None
    assert resolution["matched_pricing_dimension_id"] is None


def test_missing_city_does_not_let_text_replace_structured_state():
    identified = identify_place(city=None, state="PR", text="Campinas/SP", catalog=_catalog())
    assert identified["ok"] is False
    assert identified["entity"] is None
    assert "structured_text_conflict" in identified["reason_codes"]
    assert identified["reason_codes"] != ["ambiguous_municipality"]
    resolution = resolve_context(
        contract=_contract(_state("dim-sp", "SP", "SAO PAULO"), _state("dim-pr", "PR", "PARANA")),
        destination={"state": "PR", "text": "Campinas/SP"},
        catalog=_catalog(),
    )
    assert resolution["destination_entity"] is None
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["matched_pricing_dimension_id"] is None
    assert "structured_text_conflict" in resolution["reason_codes"]


def test_structured_city_and_contradicting_text_uf_conflict():
    identified = identify_place(city="Campinas", state="PR", text="Campinas/SP", catalog=_catalog())
    assert identified["ok"] is False
    assert identified["entity"] is None
    assert "structured_text_conflict" in identified["reason_codes"]


def test_text_can_complete_missing_state_when_catalog_confirms_one_pair():
    without_catalog = identify_place(city="Campinas", text="Campinas/SP")
    assert without_catalog["ok"] is False
    assert without_catalog["entity"] is None
    identified = identify_place(city="Campinas", text="Campinas/SP", catalog=_catalog())
    assert identified["ok"] is True
    assert identified["entity"]["entity_id"] == CAMPINAS
    assert identified["entity"]["state"] == "SP"
    assert identified["entity"]["name"] == "CAMPINAS"
    resolution = resolve_context(
        contract=_contract(_state("dim-sp", "SP", "SAO PAULO")),
        destination={"city": "Campinas", "text": "Campinas/SP"},
        catalog=_catalog(),
    )
    assert resolution["destination_entity"]["entity_id"] == CAMPINAS
    assert resolution["destination_entity"]["state"] == "SP"
    assert resolution["resolution_status"] == "resolved"
    assert resolution["review_state"] == "accepted"
    assert resolution["resolution_basis"] == "canonical_geography"
    assert resolution["matched_pricing_dimension_id"] == "dim-sp"


def test_matching_text_can_complete_city_inside_structured_state():
    londrina = municipality_entity_id("PR", "Londrina")
    identified = identify_place(state="PR", text="Londrina/PR")
    assert identified["ok"] is True
    assert identified["entity"]["entity_id"] == londrina
    assert identified["entity"]["state"] == "PR"
    assert identified["entity"]["name"] == "LONDRINA"
    catalog = AuxiliaryGeographyCatalog(
        [
            {"city": "Londrina", "state": "PR"},
            {"city": "Campinas", "state": "SP"},
        ]
    )
    confirmed = identify_place(state="PR", text="Londrina/PR", catalog=catalog)
    assert confirmed["entity"]["entity_id"] == londrina
    resolution = resolve_context(
        contract=_contract(_state("dim-pr", "PR", "PARANA"), _state("dim-sp", "SP", "SAO PAULO")),
        destination={"state": "PR", "text": "Londrina/PR"},
        catalog=catalog,
    )
    assert resolution["destination_entity"]["entity_id"] == londrina
    assert resolution["destination_entity"]["state"] == "PR"
    assert resolution["resolution_status"] == "resolved"
    assert resolution["matched_pricing_dimension_id"] == "dim-pr"
    assert "state_dimension_match" in resolution["reason_codes"]


def test_bare_city_text_stays_inside_structured_state():
    catalog = AuxiliaryGeographyCatalog(
        [
            {"city": "Campinas", "state": "SP"},
            {"city": "Londrina", "state": "PR"},
        ]
    )
    missing = identify_place(state="PR", text="Campinas", catalog=catalog)
    assert missing["ok"] is False
    assert missing["entity"] is None
    assert "structured_text_conflict" not in missing["reason_codes"]
    resolution = resolve_context(
        contract=_contract(
            _municipality("dim-campinas", CAMPINAS, "Campinas/SP"),
            _state("dim-sp", "SP", "SAO PAULO"),
            _state("dim-pr", "PR", "PARANA"),
        ),
        destination={"state": "PR", "text": "Campinas"},
        catalog=catalog,
    )
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["destination_entity"] is None
    assert resolution["matched_pricing_dimension_id"] is None
    present = AuxiliaryGeographyCatalog(
        [
            {"city": "Campinas", "state": "PR"},
            {"city": "Campinas", "state": "SP"},
        ]
    )
    accepted = identify_place(state="PR", text="Campinas", catalog=present)
    assert accepted["ok"] is True
    assert accepted["entity"]["state"] == "PR"
    assert accepted["entity"]["name"] == "CAMPINAS"
    assert accepted["entity"]["entity_id"] == municipality_entity_id("PR", "Campinas")


def test_coherent_structured_city_and_state_stay_valid_with_matching_text():
    identified = identify_place(city="Campinas", state="SP", text="Campinas/SP", catalog=_catalog())
    assert identified["ok"] is True
    assert identified["entity"]["entity_id"] == CAMPINAS
    assert identified["source"] == "structured"
    resolution = resolve_context(
        contract=_contract(_municipality("dim-campinas", CAMPINAS, "Campinas/SP")),
        destination={"city": "Campinas", "state": "SP", "text": "Campinas/SP"},
        catalog=_catalog(),
    )
    assert resolution["resolution_status"] == "resolved"
    assert resolution["review_state"] == "accepted"
    assert resolution["resolution_basis"] == "exact_entity"
    assert resolution["destination_entity"]["entity_id"] == CAMPINAS
    assert resolution["matched_pricing_dimension_id"] == "dim-campinas"


def test_municipality_without_uf_is_ambiguous_when_catalog_has_many_states():
    catalog = AuxiliaryGeographyCatalog(
        [
            {"city": "Curitiba", "state": "PR"},
            {"city": "Curitiba", "state": "GO"},
        ]
    )
    resolution = resolve_context(
        contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
        destination={"city": "Curitiba"},
        catalog=catalog,
    )
    assert resolution["resolution_status"] == "unresolved"
    assert "ambiguous_municipality" in resolution["reason_codes"]
    assert resolution["destination_entity"] is None


def test_invalid_uf_is_rejected():
    resolution = resolve_context(
        contract=_contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR")),
        destination={"city": "Curitiba", "state": "XX"},
    )
    assert resolution["resolution_status"] == "unresolved"
    assert "invalid_state" in resolution["reason_codes"]
    assert resolution["destination_entity"] is None


def test_campinas_resolves_to_state_dimension_by_structured_uf_not_label():
    state = _state("dim-sp", "SP", "ESTADO DE SAO PAULO")
    decoy = _region("dim-label-sp", "SP", kind="zone")
    contract = _contract(decoy, state)
    resolution = resolve_context(contract=contract, destination={"city": "Campinas", "state": "SP"})
    assert resolution["resolution_status"] == "resolved"
    assert resolution["review_state"] == "accepted"
    assert resolution["resolution_basis"] == "canonical_geography"
    assert resolution["matched_pricing_dimension_id"] == "dim-sp"
    assert "state_dimension_match" in resolution["reason_codes"]
    geography = next(item for item in resolution["evidence"] if item.get("method") == "structured_state_component")
    assert geography["relation"] == {"from_entity_id": CAMPINAS, "to_state": "SP"}
    assert geography["official_authority"] is False
    dumped = json.dumps(resolution)
    assert "dim-label-sp" not in dumped


def test_campinas_does_not_resolve_sp_interior_without_capital_authority():
    interior = _region("dim-interior", "SP Interior", kind="state_interior", state="SP")
    resolution = resolve_context(
        contract=_contract(interior),
        destination={"city": "Campinas", "state": "SP"},
    )
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["review_state"] == "requires_review"
    assert resolution["matched_pricing_dimension_id"] is None
    assert set(CAPITAL_INTERIOR_REASONS).issubset(resolution["reason_codes"])
    assert capital_interior_membership()["resolved"] is False


def test_sao_paulo_does_not_resolve_sp_capital_without_capital_authority():
    capital = _region("dim-capital", "SP Capital", kind="state_capital", state="SP")
    resolution = resolve_context(
        contract=_contract(capital),
        destination={"city": "São Paulo", "state": "SP"},
    )
    assert resolution["destination_entity"]["entity_id"] == SAO_PAULO
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["review_state"] == "requires_review"
    assert resolution["matched_pricing_dimension_id"] is None
    assert "missing_capital_authority" in resolution["reason_codes"]


def test_interior_i_resolves_only_through_coverage_ids():
    region = _region("dim-interior-i", "Interior I")
    rows = [
        {
            "destination_city": "Campinas",
            "destination_uf": "SP",
            "freight_region": "Interior I",
            "artifact_id": "coverage-1",
            "artifact_revision": 2,
            "row_index": 4,
            "source_file_name": "cobertura.xlsx",
        }
    ]
    resolution = resolve_context(
        contract=_contract(region),
        destination={"city": "Campinas", "state": "SP"},
        coverage_rows=rows,
    )
    assert resolution["resolution_basis"] == "coverage"
    assert resolution["resolution_status"] == "resolved"
    assert resolution["review_state"] == "accepted"
    assert resolution["matched_pricing_dimension_id"] == "dim-interior-i"
    assert "coverage_match" in resolution["reason_codes"]
    coverage = [item for item in resolution["evidence"] if item["source_type"] == "coverage"]
    assert coverage[0]["row_index"] == 4
    assert coverage[0]["artifact_id"] == "coverage-1"
    assert "row_index" not in coverage[0]["row_identity"]


def test_interior_i_without_coverage_stays_unresolved():
    region = _region("dim-interior-i", "Interior I")
    resolution = resolve_context(
        contract=_contract(region),
        destination={"city": "Campinas", "state": "SP"},
        coverage_rows=[],
    )
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["review_state"] == "requires_review"
    assert resolution["matched_pricing_dimension_id"] is None
    assert "carrier_region_membership_missing" in resolution["reason_codes"]


def test_conflicting_coverage_is_identical_in_both_row_orders():
    first = _region("dim-interior-i", "Interior I")
    second = _region("dim-interior-ii", "Interior II")
    contract = _contract(first, second)
    rows = [
        {"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior I", "row_index": 1},
        {"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior II", "row_index": 2},
    ]
    forward = resolve_context(contract=contract, destination={"city": "Campinas", "state": "SP"}, coverage_rows=rows)
    backward = resolve_context(
        contract=contract,
        destination={"city": "Campinas", "state": "SP"},
        coverage_rows=list(reversed(rows)),
    )
    assert _decision(forward) == _decision(backward)
    assert forward["resolution_status"] == "conflicting"
    assert forward["review_state"] == "requires_review"
    assert forward["candidate_dimension_ids"] == ["dim-interior-i", "dim-interior-ii"]
    assert forward["matched_pricing_dimension_id"] is None
    assert "conflicting_coverage" in forward["reason_codes"]
    assert validate_resolution(forward)["ok"] is True


def test_equivalent_coverage_dedupes_relation_and_keeps_both_evidences():
    region = _region("dim-interior-i", "Interior I")
    rows = [
        {
            "destination_city": "Campinas",
            "destination_uf": "SP",
            "freight_region": "Interior I",
            "row_index": 1,
            "source_file_name": "a.xlsx",
        },
        {
            "destination_city": "Campinas",
            "destination_uf": "SP",
            "freight_region": "Interior I",
            "row_index": 8,
            "source_file_name": "b.xlsx",
        },
    ]
    converted = convert_coverage_rows(rows, _contract(region))
    assert len(converted["relations"]) == 1
    assert converted["relations"][0]["pricing_dimension_id"] == "dim-interior-i"
    assert len(converted["relations"][0]["evidence"]) == 2
    assert coverage_content_fingerprint(rows) == coverage_content_fingerprint(list(reversed(rows)))
    changed_index = [dict(rows[0], row_index=99, source_file_name="c.xlsx")]
    assert coverage_content_fingerprint(changed_index) == coverage_content_fingerprint([rows[0]])
    changed_region = [dict(rows[0], freight_region="Interior II")]
    assert coverage_content_fingerprint(changed_region) != coverage_content_fingerprint([rows[0]])
    resolution = resolve_context(
        contract=_contract(region),
        destination={"city": "Campinas", "state": "SP"},
        coverage_rows=rows,
    )
    evidences = [item for item in resolution["evidence"] if item["source_type"] == "coverage"]
    assert resolution["resolution_status"] == "resolved"
    assert len(evidences) == 2
    assert {item["row_index"] for item in evidences} == {1, 8}
    assert len({item["row_identity"] for item in evidences}) == 1


def test_same_label_on_two_dimensions_does_not_pick_the_first():
    first = _region("dim-a", "Interior I")
    second = _region("dim-b", "Interior I")
    rows = [{"destination_city": "Campinas", "destination_uf": "SP", "freight_region": "Interior I", "row_index": 1}]
    forward = resolve_context(contract=_contract(first, second), destination={"city": "Campinas", "state": "SP"}, coverage_rows=rows)
    backward = resolve_context(contract=_contract(second, first), destination={"city": "Campinas", "state": "SP"}, coverage_rows=rows)
    assert forward["resolution_status"] == "conflicting"
    assert "ambiguous_coverage" in forward["reason_codes"]
    assert forward["candidate_dimension_ids"] == backward["candidate_dimension_ids"] == ["dim-a", "dim-b"]
    assert forward["matched_pricing_dimension_id"] is None


def test_exact_entity_does_not_win_before_a_conflicting_coverage_candidate():
    municipality = _municipality("dim-curitiba", CURITIBA, "Curitiba/PR")
    region = _region("dim-interior-i", "Interior I")
    rows = [{"destination_city": "Curitiba", "destination_uf": "PR", "freight_region": "Interior I"}]
    resolution = resolve_context(
        contract=_contract(municipality, region),
        destination={"city": "Curitiba", "state": "PR"},
        coverage_rows=rows,
    )
    assert resolution["resolution_status"] == "conflicting"
    assert resolution["resolution_basis"] == "unresolved"
    assert resolution["matched_pricing_dimension_id"] is None
    assert resolution["candidate_dimension_ids"] == ["dim-curitiba", "dim-interior-i"]


def test_multiple_origins_keep_distinct_contexts_and_missing_origin_blocks():
    dimension = _municipality("dim-curitiba", CURITIBA, "Curitiba/PR")
    lanes = [
        _lane("lane-sp", "dim-curitiba", origins=[SAO_PAULO], entity_id=CURITIBA),
        _lane("lane-rj", "dim-curitiba", origins=[RIO], entity_id=CURITIBA),
    ]
    contract = _contract(dimension, lanes=lanes)
    from_sp = resolve_context(
        contract=contract,
        destination={"city": "Curitiba", "state": "PR"},
        context={"origin_city": "São Paulo", "origin_state": "SP"},
    )
    from_rj = resolve_context(
        contract=contract,
        destination={"city": "Curitiba", "state": "PR"},
        context={"origin_city": "Rio de Janeiro", "origin_state": "RJ"},
    )
    missing = resolve_context(contract=contract, destination={"city": "Curitiba", "state": "PR"})
    assert from_sp["resolution_status"] == "resolved"
    assert from_sp["matched_lane_ids"] == ["lane-sp"]
    assert from_rj["matched_lane_ids"] == ["lane-rj"]
    assert from_sp["resolution_id"] != from_rj["resolution_id"]
    assert missing["resolution_status"] == "unresolved"
    assert "origin_required" in missing["reason_codes"]
    assert missing["matched_pricing_dimension_id"] is None
    assert missing["matched_lane_ids"] == []


def test_ambiguous_lanes_are_not_collapsed():
    dimension = _municipality("dim-curitiba", CURITIBA, "Curitiba/PR")
    lanes = [
        _lane("lane-express", "dim-curitiba", service="express", entity_id=CURITIBA),
        _lane("lane-standard", "dim-curitiba", service="standard", entity_id=CURITIBA),
    ]
    resolution = resolve_context(
        contract=_contract(dimension, lanes=lanes),
        destination={"city": "Curitiba", "state": "PR"},
    )
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["review_state"] == "requires_review"
    assert "lane_ambiguous" in resolution["reason_codes"]
    assert resolution["matched_lane_ids"] == []
    assert resolution["matched_pricing_dimension_id"] is None


def test_pending_assertion_blocks_even_with_high_confidence():
    dimension = _municipality("dim-curitiba", CURITIBA, "Curitiba/PR")
    assertion = {
        "assertion_id": "assertion-1",
        "subject_id": "dim-curitiba",
        "review_state": "requires_review",
        "confidence": "high",
        "statement": "pendente",
    }
    resolution = resolve_context(
        contract=_contract(dimension, assertions=[assertion]),
        destination={"city": "Curitiba", "state": "PR"},
    )
    assert assertion["confidence"] == "high"
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["review_state"] == "requires_review"
    assert "dimension_not_accepted" in resolution["reason_codes"]
    assert resolution["matched_pricing_dimension_id"] is None


def test_rejected_assertion_does_not_authorize_resolution():
    dimension = _municipality("dim-curitiba", CURITIBA, "Curitiba/PR")
    assertion = {
        "assertion_id": "assertion-1",
        "subject_id": "dim-curitiba",
        "review_state": "rejected",
        "confidence": "high",
        "statement": "rejeitada",
    }
    resolution = resolve_context(
        contract=_contract(dimension, assertions=[assertion]),
        destination={"city": "Curitiba", "state": "PR"},
    )
    assert resolution["resolution_status"] == "unresolved"
    assert resolution["review_state"] == "rejected"
    assert "dimension_not_accepted" in resolution["reason_codes"]


def test_overlapping_dimensions_without_precedence_conflict_independent_of_order():
    municipality = _municipality("dim-curitiba", CURITIBA, "Curitiba/PR")
    state = _state("dim-pr", "PR", "PARANA")
    forward = resolve_context(contract=_contract(municipality, state), destination={"city": "Curitiba", "state": "PR"})
    backward = resolve_context(contract=_contract(state, municipality), destination={"city": "Curitiba", "state": "PR"})
    assert _decision(forward) == _decision(backward)
    assert forward["resolution_status"] == "conflicting"
    assert forward["review_state"] == "requires_review"
    assert "overlapping_dimensions" in forward["reason_codes"]
    assert forward["candidate_dimension_ids"] == ["dim-curitiba", "dim-pr"]
    assert forward["matched_pricing_dimension_id"] is None


def test_explicit_human_precedence_is_used_only_when_represented():
    municipality = _municipality("dim-curitiba", CURITIBA, "Curitiba/PR")
    state = _state("dim-pr", "PR", "PARANA")
    contract = _contract(municipality, state)
    silent = {
        "destination_entity_id": CURITIBA,
        "origin_entity_id": None,
        "dimension_id": "dim-curitiba",
        "review_state": "accepted",
        "decision_revision": "hr-1",
        "service_ref": None,
    }
    without = resolve_context(
        contract=contract,
        destination={"city": "Curitiba", "state": "PR"},
        human_overrides=[silent],
    )
    with_precedence = resolve_context(
        contract=contract,
        destination={"city": "Curitiba", "state": "PR"},
        human_overrides=[{**silent, "precedence": "overrides_applicable"}],
    )
    assert without["resolution_status"] == "conflicting"
    assert with_precedence["resolution_status"] == "resolved"
    assert with_precedence["resolution_basis"] == "human_override"
    assert with_precedence["acceptance_source"] == "human"
    assert with_precedence["matched_pricing_dimension_id"] == "dim-curitiba"


def test_batch_dedupes_by_context_not_by_destination_entity_count():
    rows = []
    for index in range(3):
        rows.append({"row_id": f"c-sp-{index}", "city": "Campinas", "state": "SP", "origin_city": "São Paulo", "origin_state": "SP"})
    rows.append({"row_id": "c-cwb", "city": "Campinas", "state": "SP", "origin_city": "Curitiba", "origin_state": "PR"})
    rows.append({"row_id": "k-sp", "city": "Curitiba", "state": "PR", "origin_city": "São Paulo", "origin_state": "SP"})
    rows.append({"row_id": "k-cps", "city": "Curitiba", "state": "PR", "origin_city": "Campinas", "origin_state": "SP"})
    preview = resolve_batch(rows, contract=_contract())
    assert preview["stats"]["row_count"] == 6
    assert preview["stats"]["identity_count"] == 3
    assert preview["stats"]["resolution_count"] == 4
    assert preview["stats"]["resolution_count"] != preview["stats"]["identity_count"]
    assert preview["row_refs"]["c-sp-0"] == preview["row_refs"]["c-sp-1"] == preview["row_refs"]["c-sp-2"]
    assert len(set(preview["row_refs"].values())) == 4
    assert preview["preview_only"] is True
    assert preview["activated"] is False
    assert preview["operational"] is False


def test_cache_hits_the_same_dependencies_and_misses_when_one_changes():
    rows = [{"row_id": "1", "city": "Curitiba", "state": "PR"}]
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"))
    cache = LocalResolutionCache()
    first = resolve_batch(rows, contract=contract, cache=cache, tenant_scope="tenant-a")
    assert cache.misses == 1 and cache.hits == 0
    second = resolve_batch(rows, contract=contract, cache=cache, tenant_scope="tenant-a")
    assert cache.hits == 1
    assert second["resolutions"] == first["resolutions"]
    revised = copy.deepcopy(contract)
    revised["revision"] = 2
    resolve_batch(rows, contract=revised, cache=cache, tenant_scope="tenant-a")
    assert cache.misses == 2


@pytest.mark.parametrize(
    ("field", "left", "right"),
    [
        ("contract_revision", 1, 2),
        ("contract_content_fingerprint", "sha256:a", "sha256:b"),
        ("contract_authorization_fingerprint", "sha256:auth-a", "sha256:auth-b"),
        ("destination_identity", CURITIBA, CAMPINAS),
        ("origin_identity", SAO_PAULO, RIO),
        ("service_scope", "express", "standard"),
        ("coverage_content_fingerprint", "sha256:cov-a", "sha256:cov-b"),
        ("human_decision_revision", "hr-1", "hr-2"),
        ("resolution_policy_version", "sr-policy-v1", "sr-policy-v2"),
        ("identity_policy_version", "sr-identity-v1", "sr-identity-v2"),
        ("geography_revision", {"revision": "g1", "fingerprint": "f1"}, {"revision": "g2", "fingerprint": "f1"}),
        ("tenant_scope", "tenant-a", "tenant-b"),
        ("carrier_scope", "carrier-a", "carrier-b"),
        ("effective_date", "2026-01-01", "2026-02-01"),
        ("complementary_evidence_fingerprint", "sha256:e1", "sha256:e2"),
    ],
)
def test_cache_key_changes_when_a_single_dependency_changes(field, left, right):
    base = {name: None for name in CACHE_KEY_FIELDS}
    base.update(
        {
            "tenant_scope": "tenant-a",
            "contract_id": "sfc:test",
            "contract_revision": 1,
            "contract_content_fingerprint": "sha256:test",
            "destination_identity": CURITIBA,
            "origin_identity": None,
            "resolution_policy_version": "sr-policy-v1",
            "identity_policy_version": "sr-identity-v1",
        }
    )
    first = dict(base)
    second = dict(base)
    first[field] = left
    second[field] = right
    assert resolution_cache_key(first) != resolution_cache_key(second)
    assert "contract_authorization_fingerprint" in CACHE_KEY_FIELDS


def _curitiba_batch_rows() -> list[dict]:
    return [{"row_id": "1", "city": "Curitiba", "state": "PR"}]


def _assert_authorization_miss(cache: LocalResolutionCache, revised: dict) -> dict:
    before_misses = cache.misses
    before_hits = cache.hits
    second = resolve_batch(_curitiba_batch_rows(), contract=revised, cache=cache, tenant_scope="tenant-a")
    assert cache.misses == before_misses + 1
    assert cache.hits == before_hits
    resolution = second["resolutions"][0]
    assert resolution["review_state"] != "accepted"
    assert resolution["resolution_status"] == "unresolved"
    return resolution


def test_cache_misses_when_review_state_changes_without_content_fingerprint():
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"))
    cache = LocalResolutionCache()
    first = resolve_batch(_curitiba_batch_rows(), contract=contract, cache=cache, tenant_scope="tenant-a")
    assert cache.misses == 1 and cache.hits == 0
    assert first["resolutions"][0]["review_state"] == "accepted"
    revised = copy.deepcopy(contract)
    revised["review_state"] = "requires_review"
    assert revised["revision"] == contract["revision"]
    assert revised["content_fingerprint"] == contract["content_fingerprint"]
    assert contract_resolution_authorization_fingerprint(revised) != contract_resolution_authorization_fingerprint(contract)
    resolution = _assert_authorization_miss(cache, revised)
    assert resolution["review_state"] == "requires_review"
    assert "dimension_not_accepted" in resolution["reason_codes"]


def test_cache_misses_when_assertion_becomes_requires_review_without_content_change():
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"))
    cache = LocalResolutionCache()
    first = resolve_batch(_curitiba_batch_rows(), contract=contract, cache=cache, tenant_scope="tenant-a")
    assert first["resolutions"][0]["review_state"] == "accepted"
    revised = copy.deepcopy(contract)
    revised["assertions"] = [
        {
            "assertion_id": "assertion-1",
            "subject_id": "dim-curitiba",
            "review_state": "requires_review",
            "acceptance_source": None,
            "statement": "pendente",
        }
    ]
    assert revised["revision"] == contract["revision"]
    assert revised["content_fingerprint"] == contract["content_fingerprint"]
    resolution = _assert_authorization_miss(cache, revised)
    assert resolution["review_state"] == "requires_review"


def test_cache_misses_when_accepted_assertion_becomes_rejected():
    accepted = {
        "assertion_id": "assertion-1",
        "subject_id": "dim-curitiba",
        "review_state": "accepted",
        "acceptance_source": "deterministic_policy",
        "statement": "aceita",
    }
    contract = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"), assertions=[accepted])
    cache = LocalResolutionCache()
    first = resolve_batch(_curitiba_batch_rows(), contract=contract, cache=cache, tenant_scope="tenant-a")
    assert first["resolutions"][0]["review_state"] == "accepted"
    assert first["resolutions"][0]["resolution_status"] == "resolved"
    revised = copy.deepcopy(contract)
    revised["assertions"][0]["review_state"] = "rejected"
    assert revised["revision"] == contract["revision"]
    assert revised["content_fingerprint"] == contract["content_fingerprint"]
    resolution = _assert_authorization_miss(cache, revised)
    assert resolution["review_state"] == "rejected"
    assert resolution["acceptance_source"] is None


def test_presentation_metadata_does_not_change_authorization_fingerprint():
    contract = _contract(
        _municipality("dim-curitiba", CURITIBA, "Curitiba/PR"),
        assertions=[
            {
                "assertion_id": "assertion-1",
                "subject_id": "dim-curitiba",
                "review_state": "accepted",
                "acceptance_source": "deterministic_policy",
                "statement": "rótulo original",
                "confidence": "high",
            }
        ],
    )
    revised = copy.deepcopy(contract)
    revised["pricing_dimensions"][0]["source_label"] = "Rótulo só de apresentação"
    revised["assertions"][0]["statement"] = "outro texto de apresentação"
    revised["assertions"][0]["confidence"] = "low"
    revised["updated_at"] = "2026-10-09T12:00:00"
    assert contract_resolution_authorization_fingerprint(revised) == contract_resolution_authorization_fingerprint(contract)
    cache = LocalResolutionCache()
    first = resolve_batch(_curitiba_batch_rows(), contract=contract, cache=cache, tenant_scope="tenant-a")
    second = resolve_batch(_curitiba_batch_rows(), contract=revised, cache=cache, tenant_scope="tenant-a")
    assert cache.misses == 1 and cache.hits == 1
    assert second["resolutions"] == first["resolutions"]
    assert second["resolutions"][0]["review_state"] == "accepted"


def test_assertion_order_does_not_change_authorization_fingerprint():
    assertions = [
        {
            "assertion_id": "assertion-b",
            "subject_id": "dim-curitiba",
            "review_state": "accepted",
            "acceptance_source": "human",
            "statement": "segunda",
        },
        {
            "assertion_id": "assertion-a",
            "subject_id": "dim-curitiba",
            "review_state": "accepted",
            "acceptance_source": "deterministic_policy",
            "statement": "primeira",
        },
    ]
    forward = _contract(_municipality("dim-curitiba", CURITIBA, "Curitiba/PR"), assertions=assertions)
    backward = _contract(
        _municipality("dim-curitiba", CURITIBA, "Curitiba/PR"),
        assertions=list(reversed(assertions)),
    )
    assert contract_resolution_authorization_fingerprint(forward) == contract_resolution_authorization_fingerprint(backward)
    cache = LocalResolutionCache()
    first = resolve_batch(_curitiba_batch_rows(), contract=forward, cache=cache, tenant_scope="tenant-a")
    second = resolve_batch(_curitiba_batch_rows(), contract=backward, cache=cache, tenant_scope="tenant-a")
    assert cache.misses == 1 and cache.hits == 1
    assert second["resolutions"] == first["resolutions"]


def test_label_is_not_the_primary_match_and_legacy_key_stays_in_the_adapter(monkeypatch):
    def explode(*_args, **_kwargs):
        raise AssertionError("calculate_weight_freight")

    monkeypatch.setattr("app.agente_compara_doc_service.calculate_weight_freight", explode)
    zone = _region("dim-zone", "CAMPINAS", kind="zone")
    labeled = _region("dim-sp-label", "SP", kind="zone")
    unresolved = resolve_context(
        contract=_contract(zone, labeled),
        destination={"city": "Campinas", "state": "SP"},
    )
    assert unresolved["resolution_status"] == "unresolved"
    assert unresolved["matched_pricing_dimension_id"] is None
    assert "dim-zone" not in json.dumps(unresolved["matched_lane_ids"])

    contract = _comp03_contract()
    origin = municipality_entity_id("SP", "São Paulo")
    resolution = resolve_context(
        contract=contract,
        destination={"city": "Curitiba", "state": "PR"},
        context={"origin_city": "São Paulo", "origin_state": "SP"},
    )
    projected = experimental_projection_ref(resolution, contract)
    assert resolution["destination_entity"]["entity_id"] == CURITIBA
    assert resolution["resolution_basis"] == "exact_entity"
    assert resolution["matched_pricing_dimension_id"]
    assert resolution["matched_lane_ids"]
    assert "PR|CURITIBA" not in json.dumps(resolution)
    assert projected["available"] is True
    assert projected["semantic_rule_id"]
    assert projected["executable_rule_ref"]
    assert projected["legacy_lookup_key"] == "PR|CURITIBA"
    assert projected["preview_only"] is True
    assert origin == SAO_PAULO
    resolver_source = (PACKAGE / "resolver.py").read_text(encoding="utf-8")
    assert "_coverage_lookup_key" not in resolver_source
    assert "calculate_weight_freight" not in resolver_source


def test_preview_is_isolated_from_the_freight_contract_and_runtime():
    contract = {"contract_id": "sfc:test", "compilation_preview": {"activated": False}}
    record = {
        "status": "needs_review",
        "expires_at": "2026-10-10T00:00:00",
        CONTRACT_KEY: contract,
        "pricing_contract": {"rules": [{"sentinel": "operational"}]},
    }
    preview = {
        "schema_version": "1",
        "preview_only": False,
        "activated": True,
        "operational": True,
        "resolutions": [],
    }
    attached = attach_semantic_resolution_preview(record, preview)
    assert attached[TECHNICAL_JSON_KEY]["preview_only"] is True
    assert attached[TECHNICAL_JSON_KEY]["activated"] is False
    assert attached[TECHNICAL_JSON_KEY]["operational"] is False
    assert attached[CONTRACT_KEY] == contract
    assert TECHNICAL_JSON_KEY not in attached[CONTRACT_KEY]
    assert record[CONTRACT_KEY] == contract
    stored = copy.deepcopy(attached)
    updated = dict(stored)
    updated[TECHNICAL_JSON_KEY] = {"activated": True}
    preserve_semantic_resolution_preview(stored=stored, updated=updated, payload={TECHNICAL_JSON_KEY: {"injected": True}})
    assert updated[TECHNICAL_JSON_KEY] == stored[TECHNICAL_JSON_KEY]
    assert updated[TECHNICAL_JSON_KEY]["activated"] is False
    with pytest.raises(ValueError):
        attach_semantic_resolution_preview({"status": "expired"}, preview)

    table = {"freight_tables": [{"columns": ["Destino"], "rows": [{"Destino": "Curitiba/PR"}]}], "freight_routes": []}
    operational = build_pricing_contract(table)
    mutated = dict(table)
    mutated[TECHNICAL_JSON_KEY] = {"resolutions": [{"matched_pricing_dimension_id": "dim-x"}], "activated": True}
    assert build_pricing_contract(mutated) == operational
    indexed = dict(mutated)
    indexed["pricing_contract"] = operational
    assert build_freight_pricing_index(indexed) == build_freight_pricing_index(
        {"freight_tables": table["freight_tables"], "freight_routes": [], "pricing_contract": operational}
    )
    assert build_coverage_index([{"destination_uf": "SP", "destination_city": "Campinas", "freight_region": "Interior I"}])
    for function in (
        build_freight_pricing_index,
        build_coverage_index,
        _pricing_rule_lookup_candidates,
        _resolve_region_without_coverage,
        calculate_weight_freight,
        build_pricing_contract,
    ):
        assert "semantic_resolution" not in inspect.getsource(function)
    assert RESOLUTION_SCHEMA["preview_only"] is True
    assert "unresolved" not in RESOLUTION_SCHEMA["review_state"]
    assert set(RESERVED_INACTIVE_BASES).isdisjoint(RESOLUTION_SCHEMA["resolution_basis"])


def test_lot4_does_not_call_ai_or_use_reserved_bases():
    banned = (
        "cleiton_governed_billable_ai_call",
        "cleiton_billable_ai_call",
        "generate_content",
        "calculate_weight_freight",
        "build_freight_pricing_index",
        "build_coverage_index",
        "_pricing_rule_lookup_candidates",
        "_resolve_region_without_coverage",
        "_calculate_expected_freight_row",
        "openai",
        "generativeai",
    )
    for path in PACKAGE.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in banned:
            assert token not in source, f"{path.name} contém {token}"
        for basis in RESERVED_INACTIVE_BASES:
            if path.name == "models.py":
                continue
            if path.name in {"schema.py", "__init__.py"}:
                continue
            assert re.search(rf"(?<![A-Za-z0-9_]){re.escape(basis)}(?![A-Za-z0-9_])", source) is None, path.name
    unresolved = resolve_context(contract=_contract(), destination={"city": "Campinas", "state": "SP"})
    assert unresolved["resolution_status"] == "unresolved"
    assert unresolved["resolution_basis"] == "unresolved"


def test_auxiliary_catalog_is_not_an_official_municipality_or_capital_authority():
    first = catalog_from_locality_rows([{"cidade_nome": "Curitiba", "uf_nome": "PR", "id_cidade": 10}])
    second = catalog_from_locality_rows([{"cidade_nome": "Curitiba", "uf_nome": "pr", "id_cidade": 999}])
    assert first.fingerprint == second.fingerprint
    identified = identify_place(city="Curitiba", state="PR", catalog=first)
    assert identified["entity"]["entity_id"] == CURITIBA
    assert "10" not in identified["entity"]["entity_id"]
    assert "999" not in identified["entity"]["entity_id"]
    limitation = AuxiliaryGeographyCatalog.LIMITATION.lower()
    assert "id_cidade" in limitation
    assert "canônico" in limitation or "canonico" in limitation
    assert "capital" in limitation
    assert "interior" in limitation
    geography = next(item for item in identified["evidence"] if item["source_type"] == "geography")
    assert geography["official_authority"] is False
    assert geography["geography_revision_unavailable"] is True
    assert geography["source_revision"] is None


def _comp03_contract() -> dict:
    payload = {
        "entities": [
            {
                "ref": "e-origin",
                "kind": "municipality",
                "country": "BR",
                "state": "SP",
                "name": "São Paulo",
                "labels": ["São Paulo/SP"],
                "aliases": [],
            },
            {
                "ref": "e-dest",
                "kind": "municipality",
                "country": "BR",
                "state": "PR",
                "name": "Curitiba",
                "labels": ["Curitiba/PR"],
                "aliases": [],
            },
        ],
        "pricing_dimensions": [
            {
                "ref": "d-dest",
                "kind": "municipality",
                "destination_entity_ref": "e-dest",
                "source_label": "Curitiba/PR",
                "meaning_status": "explicit",
                "membership_definition": None,
                "state": "PR",
                "declared_min": None,
                "declared_max": None,
                "unit": None,
            }
        ],
        "lanes": [
            {
                "ref": "l1",
                "origin": {"scope": "single", "kind": "municipality", "refs": ["e-origin"]},
                "destination": {"kind": "municipality", "entity_ref": "e-dest", "dimension_ref": "d-dest"},
            }
        ],
        "price_rules": [
            {
                "ref": "r1",
                "lane_ref": "l1",
                "dimension_ref": "d-dest",
                "pricing_type": "fixed_range",
                "currency": "BRL",
                "unit": "kg",
                "declared_intervals": [{"declared_min": "0", "declared_max": "10", "amount": "32"}],
                "rate_amount": None,
                "excess_rate_per_kg": None,
                "confidence": "high",
            }
        ],
        "assertions": [],
        "evidence": [],
        "commercial_conditions": [],
    }
    assign_canonical_ids(payload, contract_id="sfc:comp03")
    contract = {
        "semantic_contract_version": "1",
        "contract_id": "sfc:comp03",
        "revision": 1,
        "content_fingerprint": "sha256:comp03",
        "review_state": "accepted",
        "source_bindings": {"source_fingerprint_local": "sha256:comp03", "fingerprint_match": True},
        "entities": payload["entities"],
        "pricing_dimensions": payload["pricing_dimensions"],
        "lanes": payload["lanes"],
        "price_rules": payload["price_rules"],
        "assertions": payload["assertions"],
        "validation": {"status": "valid", "findings": []},
    }
    contract["compilation_preview"] = compile_pricing_preview(contract)
    return contract


def _projection_resolution() -> dict:
    return {
        "resolution_status": "resolved",
        "review_state": "accepted",
        "matched_pricing_dimension_id": "dim-1",
        "matched_lane_ids": ["lane-1"],
        "destination_entity": {"kind": "municipality", "state": "PR", "name": "CURITIBA"},
    }


def _projection_contract(
    rules: list[dict],
    projections: list[dict],
    *,
    preview_rules: list[dict] | None = None,
    operational: bool = False,
) -> dict:
    if preview_rules is None:
        preview_rules = [
            {
                "pricing_type": "fixed_range",
                "preview_only": True,
                "semantic_rule_id": rules[0]["rule_id"],
                "brackets": [],
            }
        ]
    return {
        "price_rules": rules,
        "compilation_preview": {
            "activated": False,
            "status": "complete",
            "pricing_contract_preview": {
                "schema_version": 1,
                "preview_only": True,
                "operational": operational,
                "rules": preview_rules,
            },
            "compatibility_snapshot": {
                "kind": "semantic_freight_preview_snapshot",
                "isolated": True,
                "operational": False,
                "compiler_version": "test",
                "rules_signature": "sha256:test",
            },
            "rule_projection": projections,
        },
    }


def _semantic_rule(rule_id: str) -> dict:
    return {"rule_id": rule_id, "lane_id": "lane-1", "dimension_id": "dim-1"}


def test_projection_is_unavailable_when_preview_is_operational():
    contract = _projection_contract(
        [_semantic_rule("rule-1")],
        [{"semantic_rule_id": "rule-1", "executable_rule_ref": "preview:0", "status": "projected"}],
        operational=True,
    )
    projected = experimental_projection_ref(_projection_resolution(), contract)
    assert projected["available"] is False
    assert projected["operational"] is False
    assert projected["activated"] is False
    assert projected["preview_only"] is True
    assert projected["executable_rule_ref"] is None
    assert "preview_operational" in projected["reason_codes"]


def test_projection_is_unavailable_when_executable_ref_points_at_missing_rule():
    contract = _projection_contract(
        [_semantic_rule("rule-1")],
        [{"semantic_rule_id": "rule-1", "executable_rule_ref": "preview:0", "status": "projected"}],
        preview_rules=[],
    )
    projected = experimental_projection_ref(_projection_resolution(), contract)
    assert projected["available"] is False
    assert projected["executable_rule_ref"] is None
    assert projected["semantic_rule_id"] is None
    assert "executable_ref_not_found" in projected["reason_codes"]


def test_projection_is_unavailable_when_two_semantic_rules_share_a_lane_and_only_one_is_projected():
    contract = _projection_contract(
        [_semantic_rule("rule-1"), _semantic_rule("rule-2")],
        [{"semantic_rule_id": "rule-1", "executable_rule_ref": "preview:0", "status": "projected"}],
    )
    projected = experimental_projection_ref(_projection_resolution(), contract)
    assert projected["available"] is False
    assert projected["semantic_rule_id"] is None
    assert projected["executable_rule_ref"] is None
    assert projected["legacy_lookup_key"] is None
    assert projected["reason_codes"] == ["ambiguous_rule_projection"]


def test_projection_is_available_only_for_one_valid_semantic_rule():
    contract = _projection_contract(
        [_semantic_rule("rule-1")],
        [{"semantic_rule_id": "rule-1", "executable_rule_ref": "preview:0", "status": "projected"}],
    )
    projected = experimental_projection_ref(_projection_resolution(), contract)
    assert projected["available"] is True
    assert projected["semantic_rule_id"] == "rule-1"
    assert projected["executable_rule_ref"] == "preview:0"
    assert projected["legacy_lookup_key"] == "PR|CURITIBA"
    assert projected["preview_only"] is True
    assert projected["activated"] is False
    assert projected["operational"] is False
    assert projected["reason_codes"] == []
    compiled = experimental_projection_ref(
        resolve_context(
            contract=_comp03_contract(),
            destination={"city": "Curitiba", "state": "PR"},
            context={"origin_city": "São Paulo", "origin_state": "SP"},
        ),
        _comp03_contract(),
    )
    assert compiled["available"] is True
    assert compiled["executable_rule_ref"]
    assert compiled["operational"] is False
    assert compiled["activated"] is False
    assert compiled["preview_only"] is True


def test_projection_is_unavailable_when_executable_ref_is_null():
    contract = _projection_contract(
        [_semantic_rule("rule-1")],
        [{"semantic_rule_id": "rule-1", "executable_rule_ref": None, "status": "blocked"}],
    )
    projected = experimental_projection_ref(_projection_resolution(), contract)
    assert projected["available"] is False
    assert projected["executable_rule_ref"] is None
    assert "executable_ref_missing" in projected["reason_codes"]


def test_projection_is_unavailable_when_executable_ref_is_out_of_range():
    contract = _projection_contract(
        [_semantic_rule("rule-1")],
        [{"semantic_rule_id": "rule-1", "executable_rule_ref": "preview:1", "status": "projected"}],
    )
    projected = experimental_projection_ref(_projection_resolution(), contract)
    assert projected["available"] is False
    assert projected["executable_rule_ref"] is None
    assert projected["semantic_rule_id"] is None
    assert "executable_ref_not_found" in projected["reason_codes"]
