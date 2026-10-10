"""Batch semântico: identidades memoizadas, contextos únicos e fila local.

Não chama modelo, não calcula frete e não faz cutover operacional.
"""
from __future__ import annotations

import copy
from collections import Counter
from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint, normalize_identity_text
from app.services.semantic_resolution.cache import LocalResolutionCache, resolution_cache_key
from app.services.semantic_resolution.evidence import typed_evidence
from app.services.semantic_resolution.identity_memo import IdentityMemo
from app.services.semantic_resolution.limits import BatchLimitExceeded, BatchLimits, BatchStructuralError
from app.services.semantic_resolution.models import (
    IDENTITY_POLICY_VERSION,
    RESOLUTION_POLICY_VERSION,
    SCHEMA_VERSION,
)
from app.services.semantic_resolution.questions import build_questions
from app.services.semantic_resolution.queue import build_resolution_queue, queue_serialized_size, shared_coverage_evidence
from app.services.semantic_resolution.resolver import ContextResolutionFailure, resolve_context
from app.services.semantic_resolution.snapshot import prepare_resolution_snapshot

_PLACE_FIELDS = (
    "city",
    "state",
    "country",
    "text",
    "origin_city",
    "origin_state",
    "origin_country",
    "origin_text",
    "origin_entity_id",
)
_TEXT_FIELDS = (
    "city",
    "state",
    "country",
    "text",
    "origin_city",
    "origin_state",
    "origin_country",
    "origin_text",
    "origin_entity_id",
    "service_ref",
    "carrier_scope_ref",
    "effective_date",
    "row_id",
    "document_ref",
    "job_ref",
)


def _require_optional_str(value: Any, reason_code: str) -> None:
    if value is None or isinstance(value, str):
        return
    raise ContextResolutionFailure(reason_code)


def _identity_token(place: dict[str, Any], raw: dict[str, Any]) -> str:
    entity = place.get("entity") if place.get("ok") else None
    if isinstance(entity, dict) and entity.get("entity_id"):
        return str(entity["entity_id"])
    return "unresolved:" + local_fingerprint(
        {
            "reasons": sorted(str(code) for code in (place.get("reason_codes") or [])),
            "city": normalize_identity_text(str(raw.get("city") or "")),
            "state": str(raw.get("state") or "").strip().upper(),
            "country": normalize_identity_text(str(raw.get("country") or "")),
            "text": normalize_identity_text(str(raw.get("text") or "")),
        }
    )


def _has_minimum_identity(row: dict[str, Any]) -> bool:
    return any(row.get(field) not in (None, "") for field in _PLACE_FIELDS)


def _context_ref(material: dict[str, Any]) -> str:
    return "ctx:" + local_fingerprint(material).split(":", 1)[1][:24]


def strip_operational_evidence(resolution: dict[str, Any]) -> dict[str, Any]:
    """A resolução cacheável fica só com evidência material reutilizável."""
    cleaned = copy.deepcopy(resolution)
    evidence = [
        item
        for item in cleaned.get("evidence") or []
        if not (isinstance(item, dict) and item.get("source_type") == "operational")
    ]
    cleaned["evidence"] = evidence
    cleaned["evidence_refs"] = [item["evidence_id"] for item in evidence if isinstance(item.get("evidence_id"), str)]
    return cleaned


def _row_operational_evidence(record: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    return typed_evidence(
        "operational",
        {
            "row_ref": record["row_ref"],
            "external_row_id": record["external_row_id"],
            "document_ref": record["document_ref"],
            "job_ref": record["job_ref"],
            "field": "row",
            "original_value": {
                "city": row.get("city"),
                "state": row.get("state"),
                "country": row.get("country"),
                "text": row.get("text"),
                "origin_city": row.get("origin_city"),
                "origin_state": row.get("origin_state"),
                "origin_text": row.get("origin_text"),
                "origin_entity_id": row.get("origin_entity_id"),
            },
        },
    )


def _public_row(record: dict[str, Any]) -> dict[str, Any]:
    hidden = {"source_row"}
    return {key: value for key, value in record.items() if key not in hidden}


def resolve_batch(
    rows: list[dict[str, Any]],
    *,
    contract: dict[str, Any],
    coverage_rows: list[dict[str, Any]] | None = None,
    catalog: Any = None,
    cache: LocalResolutionCache | None = None,
    explicit_relations: list[dict[str, Any]] | None = None,
    human_overrides: list[dict[str, Any]] | None = None,
    tenant_scope: str | None = None,
    policy_version: str | None = None,
    identity_policy_version: str | None = None,
    human_decision_cache: Any = None,
    identity_memo: IdentityMemo | None = None,
    limits: BatchLimits | None = None,
    job_ref: str | None = None,
) -> dict[str, Any]:
    if not isinstance(rows, list):
        raise BatchStructuralError("rows_invalid")
    if not isinstance(contract, dict):
        raise BatchStructuralError("contract_invalid")
    bounds = limits or BatchLimits()
    if len(rows) > bounds.max_rows:
        raise BatchLimitExceeded("max_rows")

    policy = policy_version or RESOLUTION_POLICY_VERSION
    identity_policy = identity_policy_version or IDENTITY_POLICY_VERSION
    snapshot = prepare_resolution_snapshot(
        contract=contract,
        coverage_rows=coverage_rows,
        catalog=catalog,
        explicit_relations=explicit_relations,
        human_overrides=human_overrides,
        policy_version=policy,
        identity_policy_version=identity_policy,
    )
    memo = identity_memo or IdentityMemo(catalog=catalog, identity_policy_version=identity_policy)
    contexts: dict[tuple[Any, ...], dict[str, Any]] = {}
    identity_ids: set[str] = set()
    row_records: list[dict[str, Any]] = []

    for index, row in enumerate(rows):
        row_ref = f"rr:{index}"
        if not isinstance(row, dict):
            row_records.append(
                {
                    "row_ref": row_ref,
                    "external_row_id": None,
                    "job_ref": job_ref,
                    "document_ref": None,
                    "status": "failed",
                    "reason_codes": ["invalid_row"],
                    "resolution_id": None,
                    "context_ref": None,
                    "resolution_source": None,
                    "operational_evidence": None,
                    "source_row": None,
                }
            )
            continue
        external = row.get("row_id")
        external_id = None if external in (None, "") else str(external)
        record = {
            "row_ref": row_ref,
            "external_row_id": external_id,
            "job_ref": row.get("job_ref") or job_ref,
            "document_ref": row.get("document_ref"),
            "status": "failed",
            "reason_codes": [],
            "resolution_id": None,
            "context_ref": None,
            "resolution_source": None,
            "operational_evidence": None,
            "source_row": row,
        }
        record["operational_evidence"] = _row_operational_evidence(record, row)
        try:
            for field in _TEXT_FIELDS:
                _require_optional_str(row.get(field), "invalid_context_value")
        except ContextResolutionFailure as exc:
            record["reason_codes"] = [exc.reason_code]
            row_records.append(record)
            continue
        if not _has_minimum_identity(row):
            record["reason_codes"] = ["missing_minimum_identity"]
            row_records.append(record)
            continue

        destination_raw = {
            "city": row.get("city"),
            "state": row.get("state"),
            "country": row.get("country"),
            "text": row.get("text"),
        }
        destination = memo.identify(
            city=row.get("city"),
            state=row.get("state"),
            country=row.get("country"),
            text=row.get("text"),
            catalog=catalog,
            row_ref=row_ref,
            external_row_id=external_id,
            document_ref=row.get("document_ref"),
            job_ref=record["job_ref"],
            field="destination",
            identity_policy_version=identity_policy,
        )
        origin = None
        origin_raw = {
            "city": row.get("origin_city"),
            "state": row.get("origin_state"),
            "country": row.get("origin_country"),
            "text": row.get("origin_text"),
        }
        if any(row.get(key) not in (None, "") for key in ("origin_city", "origin_state", "origin_text")):
            origin = memo.identify(
                city=row.get("origin_city"),
                state=row.get("origin_state"),
                country=row.get("origin_country"),
                text=row.get("origin_text"),
                catalog=catalog,
                row_ref=row_ref,
                external_row_id=external_id,
                document_ref=row.get("document_ref"),
                job_ref=record["job_ref"],
                field="origin",
                identity_policy_version=identity_policy,
            )
        if len(memo._store) > bounds.max_unique_identities:
            raise BatchLimitExceeded("max_unique_identities")
        destination_token = _identity_token(destination, destination_raw)
        if destination.get("ok"):
            identity_ids.add(destination["entity"]["entity_id"])
        explicit_origin = row.get("origin_entity_id")
        explicit_origin = None if explicit_origin in (None, "") else str(explicit_origin)
        if origin is not None:
            origin_token = origin["entity"]["entity_id"] if origin.get("ok") else _identity_token(origin, origin_raw)
            if origin.get("ok"):
                identity_ids.add(origin["entity"]["entity_id"])
        else:
            origin_token = explicit_origin
        if origin is not None and explicit_origin is not None:
            origin_identity = local_fingerprint({"explicit": explicit_origin, "identified": origin_token})
        else:
            origin_identity = origin_token
        context_material = {
            "destination": destination_token,
            "origin": origin_identity,
            "explicit_origin": explicit_origin,
            "contract_id": contract.get("contract_id"),
            "revision": contract.get("revision"),
            "content": snapshot.content_fingerprint,
            "service_ref": row.get("service_ref"),
            "effective_date": row.get("effective_date"),
            "carrier_scope_ref": row.get("carrier_scope_ref"),
        }
        context_key = tuple(context_material.values())
        bucket = contexts.get(context_key)
        if bucket is None:
            if len(contexts) + 1 > bounds.max_resolution_contexts:
                raise BatchLimitExceeded("max_resolution_contexts")
            bucket = {
                "context_ref": _context_ref(context_material),
                "rows": [],
                "destination": {
                    "city": row.get("city"),
                    "state": row.get("state"),
                    "country": row.get("country"),
                    "text": row.get("text"),
                    "row_ref": row_ref,
                    "external_row_id": external_id,
                    "document_ref": row.get("document_ref"),
                    "job_ref": record["job_ref"],
                },
                "context": {
                    "origin_entity_id": explicit_origin,
                    "origin_city": row.get("origin_city"),
                    "origin_state": row.get("origin_state"),
                    "origin_country": row.get("origin_country"),
                    "origin_text": row.get("origin_text"),
                    "carrier_scope_ref": row.get("carrier_scope_ref"),
                    "service_ref": row.get("service_ref"),
                    "effective_date": row.get("effective_date"),
                    "row_ref": row_ref,
                    "external_row_id": external_id,
                    "document_ref": row.get("document_ref"),
                    "job_ref": record["job_ref"],
                },
                "cache_key": resolution_cache_key(
                    {
                        "tenant_scope": tenant_scope,
                        "contract_id": contract.get("contract_id"),
                        "contract_revision": contract.get("revision"),
                        "contract_content_fingerprint": snapshot.content_fingerprint,
                        "contract_authorization_fingerprint": snapshot.authorization_fingerprint,
                        "destination_identity": destination_token,
                        "origin_identity": origin_identity,
                        "carrier_scope": row.get("carrier_scope_ref"),
                        "service_scope": row.get("service_ref"),
                        "effective_date": row.get("effective_date"),
                        "coverage_content_fingerprint": snapshot.coverage_fingerprint,
                        "geography_revision": snapshot.geography_revision,
                        "complementary_evidence_fingerprint": snapshot.complementary_fingerprint,
                        "human_decision_revision": snapshot.human_revision,
                        "identity_policy_version": identity_policy,
                        "resolution_policy_version": policy,
                    }
                ),
                "resolution": None,
                "resolution_source": None,
            }
            contexts[context_key] = bucket
        record["context_ref"] = bucket["context_ref"]
        bucket["rows"].append(record)
        row_records.append(record)

    resolutions: list[dict[str, Any]] = []
    context_failures: list[dict[str, Any]] = []
    for bucket in contexts.values():
        cached = cache.get(bucket["cache_key"]) if cache is not None else None
        source = "cache" if cached is not None else "deterministic"
        try:
            if cached is None:
                resolved = resolve_context(
                    contract=contract,
                    destination=bucket["destination"],
                    context=bucket["context"],
                    catalog=catalog,
                    explicit_relations=explicit_relations,
                    human_overrides=human_overrides,
                    policy_version=policy,
                    identity_policy_version=identity_policy,
                    snapshot=snapshot,
                    identity_memo=memo,
                )
                resolved = strip_operational_evidence(resolved)
                if cache is not None:
                    cache.put(bucket["cache_key"], resolved)
            else:
                resolved = strip_operational_evidence(cached)
        except ContextResolutionFailure as exc:
            context_failures.append(
                {
                    "context_ref": bucket["context_ref"],
                    "status": "failed",
                    "reason_codes": [exc.reason_code],
                }
            )
            for record in bucket["rows"]:
                record["status"] = "failed"
                record["reason_codes"] = [exc.reason_code]
                record["resolution_source"] = None
            continue
        bucket["resolution"] = resolved
        bucket["resolution_source"] = source
        resolutions.append(resolved)
        for record in bucket["rows"]:
            record["resolution_id"] = resolved["resolution_id"]
            record["status"] = resolved["resolution_status"]
            record["reason_codes"] = list(resolved["reason_codes"])
            record["resolution_source"] = source

    built = build_questions(
        [
            {
                "context_ref": bucket["context_ref"],
                "resolution": bucket.get("resolution"),
                "rows": bucket["rows"],
            }
            for bucket in contexts.values()
            if bucket.get("resolution") is not None
        ],
        contract=contract,
        catalog=catalog,
        tenant_scope=tenant_scope,
        authorization_fingerprint=snapshot.authorization_fingerprint,
        coverage_fingerprint=snapshot.coverage_fingerprint,
        human_revision=snapshot.human_revision,
        human_cache=human_decision_cache,
        policy_version=policy,
        identity_policy_version=identity_policy,
    )
    if len(built["questions"]) > bounds.max_questions:
        raise BatchLimitExceeded("max_questions")
    shared = shared_coverage_evidence(snapshot)
    if len(shared) > bounds.max_shared_evidence_items:
        raise BatchLimitExceeded("max_shared_evidence_items")

    resolutions.sort(key=lambda item: item["resolution_id"])
    external_counts = Counter(record["external_row_id"] for record in row_records if record["external_row_id"])
    row_refs: dict[str, str] = {}
    for record in row_records:
        external_id = record["external_row_id"]
        resolution_id = record["resolution_id"]
        if external_id and external_counts[external_id] == 1 and resolution_id:
            row_refs[external_id] = resolution_id
    consumer_count = sum(len(refs) for refs in built["question_consumers"].values())
    stats = {
        "row_count": len(rows),
        "invalid_row_count": sum(1 for record in row_records if record["status"] == "failed"),
        "identity_count": len(identity_ids),
        "identity_evaluations": memo.evaluations,
        "resolution_count": len(resolutions),
        "question_count": len(built["questions"]),
        "ai_eligible_count": sum(1 for item in built["questions"] if item.get("eligibility") == "ai_eligible"),
        "consumer_count": consumer_count,
        "shared_evidence_count": len(shared),
        "failed_context_count": len(context_failures),
        "coverage_preparations": snapshot.coverage_preparations,
    }
    queue = build_resolution_queue(
        job_ref=job_ref,
        tenant_scope=tenant_scope,
        snapshot=snapshot,
        built=built,
        stats=stats,
        context_failures=context_failures,
    )
    if queue_serialized_size(queue) > bounds.max_queue_serialized_bytes:
        raise BatchLimitExceeded("max_queue_serialized_bytes")
    stats["queue_serialized_bytes"] = queue_serialized_size(queue)
    queue["stats"] = dict(stats)
    queue["stats"]["context_failures"] = list(context_failures)
    return {
        "schema_version": SCHEMA_VERSION,
        "preview_only": True,
        "activated": False,
        "operational": False,
        "policy_version": policy,
        "identity_policy_version": identity_policy,
        "resolutions": resolutions,
        "row_refs": row_refs,
        "row_records": [_public_row(record) for record in row_records],
        "queue": queue,
        "stats": stats,
    }
