"""Memo local de identidade, válido só durante um batch.

A chave ignora row_ref, job e documento. O resultado guardado não carrega
proveniência operacional. Cada chamada devolve a evidência da linha atual.
"""
from __future__ import annotations

import copy
from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint, normalize_identity_text
from app.services.semantic_resolution.evidence import typed_evidence
from app.services.semantic_resolution.identity import identify_place


def _normalize_state(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip().upper()


def _strip_operational(result: dict[str, Any]) -> dict[str, Any]:
    stored = copy.deepcopy(result)
    stored["evidence"] = [
        item
        for item in stored.get("evidence") or []
        if not (isinstance(item, dict) and item.get("source_type") == "operational")
    ]
    return stored


def _operational_evidence(kwargs: dict[str, Any]) -> dict[str, Any]:
    originals = {
        "city": kwargs.get("city"),
        "state": kwargs.get("state"),
        "country": kwargs.get("country"),
        "text": kwargs.get("text"),
    }
    text = kwargs.get("text")
    return typed_evidence(
        "operational",
        {
            "row_ref": kwargs.get("row_ref"),
            "external_row_id": kwargs.get("external_row_id"),
            "document_ref": kwargs.get("document_ref"),
            "job_ref": kwargs.get("job_ref"),
            "field": kwargs.get("field") or "destination",
            "original_value": text if text not in (None, "") else {"city": kwargs.get("city"), "state": kwargs.get("state")},
            "originals": originals,
        },
    )


class IdentityMemo:
    def __init__(self, *, catalog: Any = None, identity_policy_version: str) -> None:
        self.catalog = catalog
        self.identity_policy_version = identity_policy_version
        self.evaluations = 0
        self.hits = 0
        self._store: dict[str, dict[str, Any]] = {}

    def identity_key(self, **kwargs: Any) -> str:
        catalog = kwargs.get("catalog", self.catalog)
        return local_fingerprint(
            {
                "city": normalize_identity_text(str(kwargs.get("city") or "")),
                "state": _normalize_state(kwargs.get("state")),
                "country": normalize_identity_text(str(kwargs.get("country") or "")),
                "text": normalize_identity_text(str(kwargs.get("text") or "")),
                "catalog_fingerprint": None if catalog is None else catalog.fingerprint,
                "catalog_revision": None if catalog is None else catalog.revision,
                "identity_policy_version": kwargs.get("identity_policy_version") or self.identity_policy_version,
            }
        )

    def identify(self, **kwargs: Any) -> dict[str, Any]:
        key = self.identity_key(**kwargs)
        stored = self._store.get(key)
        if stored is None:
            self.evaluations += 1
            raw = identify_place(
                city=kwargs.get("city"),
                state=kwargs.get("state"),
                country=kwargs.get("country"),
                text=kwargs.get("text"),
                catalog=kwargs.get("catalog", self.catalog),
                field=kwargs.get("field") or "destination",
            )
            stored = _strip_operational(raw)
            self._store[key] = stored
        else:
            self.hits += 1
        result = copy.deepcopy(stored)
        operational = _operational_evidence(kwargs)
        result["evidence"] = [operational, *result["evidence"]]
        return result
