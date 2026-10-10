"""Semantic Resolution Preview v1. Camada isolada, sem cutover."""
from app.services.semantic_resolution.batch import resolve_batch
from app.services.semantic_resolution.cache import LocalResolutionCache, resolution_cache_key
from app.services.semantic_resolution.identity import (
    AuxiliaryGeographyCatalog,
    catalog_from_locality_rows,
    identify_place,
    municipality_entity_id,
    place_candidates,
)
from app.services.semantic_resolution.models import (
    RESOLUTION_POLICY_VERSION,
    SCHEMA_VERSION,
    TECHNICAL_JSON_KEY,
)
from app.services.semantic_resolution.persistence import (
    attach_semantic_resolution_preview,
    attach_semantic_resolution_queue,
    preserve_semantic_resolution_preview,
    preserve_semantic_resolution_queue,
)
from app.services.semantic_resolution.projection import experimental_projection_ref
from app.services.semantic_resolution.resolver import (
    capital_interior_membership,
    contract_resolution_authorization_fingerprint,
    resolve_context,
)
from app.services.semantic_resolution.schema import RESOLUTION_SCHEMA

__all__ = [
    "RESOLUTION_POLICY_VERSION",
    "RESOLUTION_SCHEMA",
    "SCHEMA_VERSION",
    "TECHNICAL_JSON_KEY",
    "AuxiliaryGeographyCatalog",
    "LocalResolutionCache",
    "attach_semantic_resolution_preview",
    "attach_semantic_resolution_queue",
    "capital_interior_membership",
    "catalog_from_locality_rows",
    "contract_resolution_authorization_fingerprint",
    "experimental_projection_ref",
    "identify_place",
    "municipality_entity_id",
    "place_candidates",
    "preserve_semantic_resolution_preview",
    "preserve_semantic_resolution_queue",
    "resolution_cache_key",
    "resolve_batch",
    "resolve_context",
]
