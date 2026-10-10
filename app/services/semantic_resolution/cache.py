"""Cache local ao job ou à sessão. Sem tabela e sem cache global."""
from __future__ import annotations

import copy
from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint
from app.services.semantic_resolution.models import CACHE_KEY_FIELDS


def resolution_cache_key(components: dict[str, Any]) -> str:
    payload = {field: components.get(field) for field in CACHE_KEY_FIELDS}
    return local_fingerprint(payload)


class LocalResolutionCache:
    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        found = self._store.get(key)
        if found is None:
            self.misses += 1
            return None
        self.hits += 1
        return copy.deepcopy(found)

    def put(self, key: str, value: dict[str, Any]) -> None:
        self._store[key] = copy.deepcopy(value)
