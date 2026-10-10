"""Converte coverage atual em relações semânticas.

O rótulo de freight_region só serve para achar a pricing_dimension
dentro do contrato fixado. Depois da conversão a relação usa IDs.
Origem, serviço e vigência entram só quando a linha os traz.
Origem ausente não significa todas as origens.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint, normalize_identity_text
from app.services.semantic_resolution.evidence import typed_evidence
from app.services.semantic_resolution.identity import identify_place

_RELATION_FIELDS = (
    "destination_entity_id",
    "origin_entity_id",
    "service_ref",
    "carrier_scope_ref",
    "valid_from",
    "valid_to",
    "contract_id",
    "revision",
)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _country_token(value: Any) -> str | None:
    token = normalize_identity_text(str(value or ""))
    if not token:
        return None
    if token in {"BR", "BRA", "BRASIL"}:
        return "BR"
    return token


def _state_token(*values: Any) -> str | None:
    for value in values:
        text = _text(value)
        if text:
            return text.upper()
    return None


def _service_token(row: dict[str, Any]) -> str | None:
    return _text(row.get("service_ref") or row.get("service"))


def coverage_content_fingerprint(rows: list[dict[str, Any]] | None) -> str:
    """Fingerprint material da conversão.

    País, UF, cidade, região, serviço, vigência e escopo entram.
    row_index e nome de arquivo não entram. A ordem das linhas não entra.
    """
    payloads: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        payloads.append(
            {
                "destination_city": normalize_identity_text(str(row.get("destination_city") or "")) or None,
                "destination_state": _state_token(row.get("destination_uf"), row.get("destination_state")),
                "destination_country": _country_token(row.get("destination_country")),
                "origin_city": normalize_identity_text(str(row.get("origin_city") or "")) or None,
                "origin_state": _state_token(row.get("origin_uf"), row.get("origin_state")),
                "origin_country": _country_token(row.get("origin_country")),
                "freight_region": normalize_identity_text(str(row.get("freight_region") or "")) or None,
                "origin_entity_id": _text(row.get("origin_entity_id")),
                "service_ref": _service_token(row),
                "carrier_scope_ref": _text(row.get("carrier_scope_ref")),
                "valid_from": _text(row.get("valid_from")),
                "valid_to": _text(row.get("valid_to")),
                "artifact_id": _text(row.get("artifact_id")),
                "artifact_revision": row.get("artifact_revision"),
                "artifact_fingerprint": _text(row.get("content_fingerprint")),
            }
        )
    payloads.sort(key=lambda item: local_fingerprint(item))
    return local_fingerprint(payloads)


def _dimension_label_index(contract: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for dimension in contract.get("pricing_dimensions") or []:
        if not isinstance(dimension, dict):
            continue
        label = normalize_identity_text(str(dimension.get("source_label") or ""))
        dimension_id = dimension.get("dimension_id")
        if not label or not isinstance(dimension_id, str) or not dimension_id:
            continue
        bucket = found.setdefault(label, [])
        if dimension_id not in bucket:
            bucket.append(dimension_id)
    for bucket in found.values():
        bucket.sort()
    return found


def _origin_id(row: dict[str, Any], catalog: Any) -> str | None:
    explicit = _text(row.get("origin_entity_id"))
    if explicit:
        return explicit
    origin_state = _state_token(row.get("origin_uf"), row.get("origin_state"))
    if not _text(row.get("origin_city")) or not origin_state:
        return None
    identified = identify_place(
        city=_text(row.get("origin_city")),
        state=origin_state,
        country=_country_token(row.get("origin_country")),
        catalog=catalog,
        field="coverage_origin",
    )
    entity = identified.get("entity") if identified.get("ok") else None
    if isinstance(entity, dict):
        return entity.get("entity_id")
    return None


def _scope_key(scope: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(scope.get(field) for field in _RELATION_FIELDS)


def convert_coverage_rows(
    rows: list[dict[str, Any]] | None,
    contract: dict[str, Any],
    *,
    catalog: Any = None,
) -> dict[str, Any]:
    """Agrupa linhas equivalentes e preserva o conflito quando os destinos divergem."""
    labels = _dimension_label_index(contract)
    contract_id = contract.get("contract_id")
    revision = contract.get("revision")
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        identified = identify_place(
            city=_text(row.get("destination_city")),
            state=_state_token(row.get("destination_uf"), row.get("destination_state")),
            country=_country_token(row.get("destination_country")),
            catalog=catalog,
            row_ref=_text(row.get("row_ref")),
            field="coverage_destination",
        )
        entity = identified.get("entity") if identified.get("ok") else None
        if not isinstance(entity, dict):
            continue
        region = normalize_identity_text(str(row.get("freight_region") or ""))
        if not region:
            continue
        dimension_ids = list(labels.get(region) or [])
        scope = {
            "destination_entity_id": entity["entity_id"],
            "origin_entity_id": _origin_id(row, catalog),
            "service_ref": _service_token(row),
            "carrier_scope_ref": _text(row.get("carrier_scope_ref")),
            "valid_from": _text(row.get("valid_from")),
            "valid_to": _text(row.get("valid_to")),
            "contract_id": contract_id,
            "revision": revision,
        }
        # Identidade da relação: não usa source_file_name nem row_index.
        relation_payload = {**scope, "dimension_ids": dimension_ids, "region": region}
        evidence = typed_evidence(
            "coverage",
            {
                "artifact_id": _text(row.get("artifact_id")),
                "artifact_revision": row.get("artifact_revision"),
                "content_fingerprint": _text(row.get("content_fingerprint")) or coverage_content_fingerprint([row]),
                "row_identity": local_fingerprint(relation_payload),
                "field_identity": {
                    "destination_city": "destination_city",
                    "destination_uf": "destination_uf",
                    "freight_region": "freight_region",
                },
                "row_index": row.get("row_index"),
                "source_file_name": _text(row.get("source_file_name")),
                "original_destination_city": row.get("destination_city"),
                "original_destination_uf": row.get("destination_uf"),
                "original_freight_region": row.get("freight_region"),
                "destination_entity_id": entity["entity_id"],
                "dimension_ids": dimension_ids,
            },
        )
        key = _scope_key(scope)
        group = groups.get(key)
        if group is None:
            group = {
                "scope": scope,
                "dimension_ids": set(),
                "labels": {},
                "evidence": [],
                "relation_fingerprints": set(),
            }
            groups[key] = group
        group["evidence"].append(evidence)
        group["relation_fingerprints"].add(evidence["row_identity"])
        group["labels"].setdefault(region, set()).update(dimension_ids)
        group["dimension_ids"].update(dimension_ids)

    relations: list[dict[str, Any]] = []
    for group in groups.values():
        dimension_ids = sorted(group["dimension_ids"])
        ambiguous = any(len(ids) > 1 for ids in group["labels"].values())
        distinct_labels = [label for label, ids in group["labels"].items() if ids]
        reasons: list[str] = []
        status = "unique"
        if ambiguous:
            status = "ambiguous"
            reasons.append("ambiguous_coverage")
        if len(dimension_ids) > 1 and (len(distinct_labels) > 1 or not ambiguous):
            status = "conflicting"
            reasons.append("conflicting_coverage")
        if len(dimension_ids) > 1 and ambiguous and len(distinct_labels) > 1:
            status = "conflicting"
        if not dimension_ids:
            status = "unbound"
            reasons.append("dimension_not_found")
        relation_id = local_fingerprint(
            {
                "scope": group["scope"],
                "dimension_ids": dimension_ids,
                "status": status,
            }
        )
        relations.append(
            {
                "relation_id": relation_id,
                "status": status,
                "reason_codes": reasons,
                "pricing_dimension_id": dimension_ids[0] if status == "unique" and len(dimension_ids) == 1 else None,
                "candidate_dimension_ids": dimension_ids,
                "evidence": sorted(group["evidence"], key=lambda item: item["evidence_id"]),
                "evidence_refs": sorted(item["evidence_id"] for item in group["evidence"]),
                **group["scope"],
            }
        )
    relations.sort(key=lambda item: item["relation_id"])
    return {
        "relations": relations,
        "content_fingerprint": coverage_content_fingerprint(rows),
    }
