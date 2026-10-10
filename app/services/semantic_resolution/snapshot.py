"""Snapshot preparado uma vez por contrato e coverage.

O resolvedor recebe este objeto e não reconverte a coverage por contexto.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint
from app.services.semantic_resolution.coverage import convert_coverage_rows


class PreparedResolutionSnapshot:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


def _human_revision(overrides: list[dict[str, Any]] | None) -> str | None:
    revisions = sorted(
        {
            str(item.get("decision_revision"))
            for item in overrides or []
            if isinstance(item, dict) and item.get("decision_revision") not in (None, "")
        }
    )
    if not revisions:
        return None
    return local_fingerprint(revisions)


def _assertions_by_subject(contract: dict[str, Any]) -> dict[Any, list[dict[str, Any]]]:
    found: dict[Any, list[dict[str, Any]]] = {}
    for assertion in contract.get("assertions") or []:
        if not isinstance(assertion, dict):
            continue
        subject = assertion.get("subject_id") or assertion.get("subject_ref")
        found.setdefault(subject, []).append(assertion)
    return found


def prepare_resolution_snapshot(
    *,
    contract: dict[str, Any],
    coverage_rows: list[dict[str, Any]] | None = None,
    catalog: Any = None,
    explicit_relations: list[dict[str, Any]] | None = None,
    human_overrides: list[dict[str, Any]] | None = None,
    policy_version: str,
    identity_policy_version: str,
) -> PreparedResolutionSnapshot:
    from app.services.semantic_resolution.resolver import (
        _content_fingerprint,
        _dimension_map,
        _lanes_for,
        contract_resolution_authorization_fingerprint,
    )

    dimension_map = _dimension_map(contract)
    lanes_by_dimension = {
        dimension_id: _lanes_for(contract, dimension)
        for dimension_id, dimension in dimension_map.items()
    }
    coverage = convert_coverage_rows(coverage_rows, contract, catalog=catalog)
    relations_by_destination: dict[Any, list[dict[str, Any]]] = {}
    for relation in coverage.get("relations") or []:
        relations_by_destination.setdefault(relation.get("destination_entity_id"), []).append(relation)
    geography_revision = None
    if catalog is not None:
        geography_revision = {
            "revision": catalog.revision,
            "fingerprint": catalog.fingerprint,
            "official": False,
        }
    return PreparedResolutionSnapshot(
        contract=contract,
        catalog=catalog,
        explicit_relations=list(explicit_relations or []),
        human_overrides=list(human_overrides or []),
        policy_version=policy_version,
        identity_policy_version=identity_policy_version,
        dimension_map=dimension_map,
        lanes_by_dimension=lanes_by_dimension,
        assertions_by_subject=_assertions_by_subject(contract),
        authorization_fingerprint=contract_resolution_authorization_fingerprint(
            contract,
            human_overrides=human_overrides,
            explicit_relations=explicit_relations,
        ),
        coverage=coverage,
        coverage_fingerprint=coverage.get("content_fingerprint"),
        relations_by_destination=relations_by_destination,
        content_fingerprint=_content_fingerprint(contract),
        geography_revision=geography_revision,
        human_revision=_human_revision(human_overrides),
        complementary_fingerprint=local_fingerprint(explicit_relations or []),
        coverage_preparations=1,
    )
