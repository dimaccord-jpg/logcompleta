"""Seleção tipada das consultas analíticas do chat do Agente Compara.

O modelo escolhe um plano do catálogo fechado. Este módulo valida o plano,
recusa identificadores fora da comparação autorizada e guarda só os referentes
confirmados pela publicação, na sessão já usada pelo chat.

O critério do referente acompanha a seleção até essa confirmação. Não entra
no plano, no executor nem na sessão.

Não chama Gemini, não executa a ponte, não lê armazenamento e não interpreta
a pergunta por palavras-chave.
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from typing import Any

from app.agente_compara_chat_query_catalog import (
    BODY_FIELDS,
    CATALOG_VERSION,
    FILTER_FIELDS,
    MAX_PAGE_SIZE,
    MAX_QUERIES_PER_PLAN,
    PAGINATED_QUERY_TYPES,
    PUBLIC_UNIVERSES,
    PUBLICATION_VERSION,
    QUERY_TYPES,
    SORTS_BY_TYPE,
    QueryCatalogError,
    validate_query_plan,
)

logger = logging.getLogger(__name__)

SELECTION_VERSION = "comparison_chat_selection_v1"
REFERENT_CRITERION_LOWEST_COMPARABLE_FREIGHT = "lowest_comparable_freight"
REFERENT_CRITERION_HIGHEST_SAVINGS = "highest_savings"
REFERENT_CRITERIA = frozenset(
    {
        REFERENT_CRITERION_LOWEST_COMPARABLE_FREIGHT,
        REFERENT_CRITERION_HIGHEST_SAVINGS,
    }
)
REFERENTS_SESSION_KEY = "agente_compara_comparison_chat_referents"
_MAX_CLARIFICATION_CHARS = 400
_MAX_RANKED_ROWS = 8
_SELECTION_FIELDS = frozenset(
    {
        "selection_version",
        "action",
        "clarification",
        "catalog_version",
        "queries",
        "referent_criterion",
    }
)
_SELECTION_ACTIONS = frozenset({"query", "clarify"})
_REQUIRED_SELECTION_FIELDS = frozenset(
    {"selection_version", "action", "catalog_version", "queries"}
)
_TIE_RULE_UNIQUE_LOWEST = "unique_lowest_amount"
_BOUNDED_BEST_CLAIM = "bounded_best_freight"
_READY_MONEY_STATES = frozenset({"present", "zero"})
_UNSAFE_CLARIFICATION_FRAGMENTS = (
    "catalog_version",
    "query_id",
    "checksum",
    "storage_key",
    "storage_path",
    "select ",
    "insert ",
    "update ",
    "delete ",
    "drop ",
    "eval(",
    "exec(",
    "__import__",
    "comparison_chat_",
    "publication_version",
    "evidence_digest",
    "operation_id",
    "```",
)

MSG_SELECTION_MALFORMED = (
    "Não consegui organizar uma consulta segura sobre essa comparação. "
    "Reformule indicando economia, componentes, UF, ranking ou fretes não calculados."
)
MSG_REFERENT_CLARIFICATION = (
    "Preciso que você indique a transportadora, a UF ou o frete a que se refere."
)
MSG_COMPARISON_UNAVAILABLE = (
    "O resultado oficial desta comparação não está disponível para esta consulta."
)
MSG_COMPARISON_NOT_AUTHORIZED = (
    "Não foi possível consultar a comparação autorizada nesta conta."
)

_SELECTION_REJECTION_CODES = frozenset(
    {
        "invalid_json",
        "missing_field",
        "extra_field",
        "wrong_version",
        "wrong_action",
        "clarify_incoherent",
        "invalid_query",
        "invalid_filter",
        "invalid_referent_criterion",
    }
)
_CATALOG_REJECTION_CODES = {
    "unknown_field": "extra_field",
    "invalid_catalog_version": "wrong_version",
    "invalid_filter": "invalid_filter",
    "invalid_uf": "invalid_filter",
    "invalid_row_index": "invalid_filter",
    "invalid_weight_band": "invalid_filter",
    "invalid_table_reference": "invalid_filter",
    "invalid_field_selection": "invalid_query",
    "invalid_page": "invalid_query",
    "invalid_sort": "invalid_query",
    "invalid_query": "invalid_query",
    "invalid_query_type": "invalid_query",
    "plan_too_large": "invalid_query",
    "too_many_queries": "invalid_query",
    "forbidden_expression": "invalid_query",
    "invalid_limit": "invalid_query",
}
_QUERY_PROPERTY_ORDER = ("query_id", "type", "filters", "fields", "page", "sort")
_FILTER_PROPERTY_ORDER = (
    "table_ids",
    "row_indexes",
    "component_codes",
    "codes",
    "ufs",
    "include_absent",
    "include_unusable",
    "minimum_gap",
    "bands_version",
)
_SELECTION_PROPERTY_ORDER = (
    "selection_version",
    "action",
    "clarification",
    "catalog_version",
    "queries",
    "referent_criterion",
)

_BRIDGE_UNAVAILABLE = frozenset(
    {
        "snapshot_unavailable",
        "snapshot_obsolete",
        "snapshot_mismatch",
        "invalid_operation_reference",
        "execution_failed",
        "invalid_query",
        "invalid_query_type",
        "invalid_catalog_version",
        "invalid_filter",
        "invalid_page",
        "invalid_sort",
        "invalid_limit",
        "unknown_field",
        "plan_too_large",
        "too_many_queries",
        "forbidden_expression",
        "population_limit_exceeded",
        "processing_limit_exceeded",
        "official_analytics_unavailable",
        "context_limit_exceeded",
        "invalid_evidence_binding",
    }
)


@dataclass(frozen=True)
class SemanticSelection:
    action: str
    plan: dict[str, Any] | None = None
    clarification: str | None = None
    referent_criterion: str | None = None


def selective_chat_required(usuario) -> bool:
    """O percurso de duas gerações vale para o usuário operacional autenticado.

    Ausência de usuário preserva o chat anterior, que não lê o artefato da ponte.
    ``is_authenticated is True`` é o mesmo contrato da ponte.
    """

    if usuario is None:
        return False
    try:
        authenticated = usuario.is_authenticated
    except Exception:
        return False
    return authenticated is True


def operational_identity(usuario) -> tuple[int, int, int] | None:
    if not selective_chat_required(usuario):
        return None
    conta_id = getattr(usuario, "conta_id", None)
    usuario_id = getattr(usuario, "id", None)
    franquia_id = getattr(usuario, "franquia_id", None)
    if (
        type(conta_id) is not int
        or type(usuario_id) is not int
        or type(franquia_id) is not int
        or conta_id <= 0
        or usuario_id <= 0
        or franquia_id <= 0
    ):
        return None
    return conta_id, usuario_id, franquia_id


def selection_catalog_document() -> dict[str, Any]:
    queries = []
    for query_type in sorted(QUERY_TYPES):
        queries.append(
            {
                "type": query_type,
                "filters": sorted(FILTER_FIELDS[query_type]),
                "fields": sorted(BODY_FIELDS[query_type]),
                "sorts": sorted(SORTS_BY_TYPE[query_type]),
                "paginated": query_type in PAGINATED_QUERY_TYPES,
            }
        )
    return {
        "selection_version": SELECTION_VERSION,
        "catalog_version": CATALOG_VERSION,
        "publication_version": PUBLICATION_VERSION,
        "referent_criteria": sorted(REFERENT_CRITERIA),
        "max_queries": MAX_QUERIES_PER_PLAN,
        "max_page_size": MAX_PAGE_SIZE,
        "queries": queries,
    }


def authorized_tables_from_context(context: dict | None) -> list[dict[str, Any]]:
    tables = (context or {}).get("tables") if isinstance(context, dict) else None
    if not isinstance(tables, list):
        return []
    authorized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tables:
        if not isinstance(item, dict):
            continue
        table_id = item.get("table_id")
        if not isinstance(table_id, str) or not table_id or table_id in seen:
            continue
        seen.add(table_id)
        entry: dict[str, Any] = {"table_id": table_id}
        carrier_name = item.get("carrier_name")
        if isinstance(carrier_name, str) and carrier_name.strip():
            entry["carrier_name"] = carrier_name.strip()
        slot_number = item.get("slot_number")
        if type(slot_number) is int:
            entry["slot_number"] = slot_number
        authorized.append(entry)
    return authorized


def empty_referents() -> dict[str, Any]:
    return {"carrier": None, "uf": None, "ranked_rows": []}


def load_confirmed_referents(session_obj, comparison_id: str | None) -> dict[str, Any]:
    """Referentes da comparação atual. Outra comparação apaga o estado anterior."""

    current = (comparison_id or "").strip()
    stored = session_obj.get(REFERENTS_SESSION_KEY) if session_obj is not None else None
    if not isinstance(stored, dict) or stored.get("comparison_id") != current or not current:
        _clear_referents(session_obj)
        return empty_referents()
    return _normalize_referents(stored)


def referents_for_prompt(referents: dict[str, Any] | None) -> dict[str, Any]:
    return _normalize_referents(referents or empty_referents())


def remember_confirmed_referents(
    session_obj,
    comparison_id: str | None,
    publication: dict[str, Any] | None,
    authorized_tables: list[dict[str, Any]],
    previous: dict[str, Any] | None = None,
    *,
    referent_criterion: str | None = None,
) -> dict[str, Any]:
    """Grava transportadora, UF e ranking. O critério da seleção não entra na sessão."""

    current = (comparison_id or "").strip()
    if not current or session_obj is None:
        return empty_referents()
    confirmed = referents_confirmed_by_publication(
        publication,
        authorized_tables,
        previous or load_confirmed_referents(session_obj, current),
        referent_criterion=referent_criterion,
    )
    payload = {"comparison_id": current, **confirmed}
    session_obj[REFERENTS_SESSION_KEY] = payload
    _mark_session_modified(session_obj)
    return confirmed


def referents_confirmed_by_publication(
    publication: dict[str, Any] | None,
    authorized_tables: list[dict[str, Any]],
    previous: dict[str, Any] | None,
    *,
    referent_criterion: str | None = None,
) -> dict[str, Any]:
    current = _normalize_referents(previous or empty_referents())
    if not isinstance(publication, dict) or publication.get("status") not in {"ok", "partial"}:
        return current
    if publication.get("publication_version") != PUBLICATION_VERSION:
        return current
    by_slot = _tables_by_slot(authorized_tables)
    queries = publication.get("queries")
    if not isinstance(queries, list):
        return current
    for query in queries:
        if not isinstance(query, dict) or query.get("status") != "ok":
            continue
        query_type = query.get("query_type")
        selection = query.get("selection") if isinstance(query.get("selection"), dict) else {}
        if query_type == "by_uf":
            ufs = [item for item in (selection.get("ufs") or []) if isinstance(item, str)]
            if len(ufs) == 1:
                current["uf"] = ufs[0]
            elif len(ufs) > 1:
                current["uf"] = None
        elif query_type == "freight_components":
            carrier = _single_slot_carrier(selection.get("carrier_slots"), by_slot)
            if carrier is not None:
                current["carrier"] = carrier
        elif query_type == "carrier_comparison":
            current["carrier"] = _carrier_for_referent_criterion(
                query,
                by_slot,
                referent_criterion,
            )
        elif query_type == "top_differences":
            current["ranked_rows"] = _positional_ranked_rows(query)
    return current


def selection_response_json_schema() -> dict[str, Any]:
    """JSON Schema do envelope, com as chaves aceitas em responseJsonSchema.

    O transporte é response_json_schema. O caminho response_schema do SDK
    projeta additional_properties, property_ordering e min_items em snake_case,
    e a API rejeita esse corpo. O item de queries é um único objeto: um anyOf
    por tipo estoura o limite de estados da API. Este documento só orienta a
    geração. O validador local continua fechado e o catálogo não vira plano.
    """

    queries = {
        "type": "array",
        "minItems": 0,
        "maxItems": MAX_QUERIES_PER_PLAN,
        "description": (
            "Lista de consultas. Cada item exige query_id e type do catálogo. "
            "filters, quando presente, é objeto. A lista fica vazia somente em clarify."
        ),
        "items": _query_item_response_schema(),
    }
    properties = {
        "selection_version": {
            "type": "string",
            "enum": [SELECTION_VERSION],
            "description": "Valor fixo comparison_chat_selection_v1.",
        },
        "action": {
            "type": "string",
            "enum": sorted(_SELECTION_ACTIONS),
            "description": "query executa o plano. clarify pede esclarecimento e não traz consultas.",
        },
        "clarification": _nullable({"type": "string"}),
        "catalog_version": {
            "type": "string",
            "enum": [CATALOG_VERSION],
            "description": "Valor fixo comparison_chat_query_catalog_v1.",
        },
        "queries": queries,
        "referent_criterion": _nullable(
            {
                "type": "string",
                "enum": sorted(REFERENT_CRITERIA),
                "description": (
                    "Somente lowest_comparable_freight ou highest_savings. "
                    "Null quando a pergunta não definir o critério."
                ),
            }
        ),
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "propertyOrdering": list(_SELECTION_PROPERTY_ORDER),
        "required": ["selection_version", "action", "catalog_version", "queries"],
        "description": (
            "Envelope comparison_chat_selection_v1. "
            "Não devolva o documento do catálogo, SQL nem campos fora deste objeto."
        ),
        "properties": properties,
    }


def resolve_semantic_selection(
    text: str,
    *,
    authorized_tables: list[dict[str, Any]],
    finish_reason: str | None = None,
) -> SemanticSelection:
    """Aceita só o envelope comparison_chat_selection_v1 completo."""

    document = _json_object(text)
    if not isinstance(document, dict):
        _log_selection_rejection("invalid_json", finish_reason)
        return SemanticSelection("malformed", clarification=MSG_SELECTION_MALFORMED)
    rejection = _selection_rejection_code(document)
    if rejection:
        _log_selection_rejection(rejection, finish_reason)
        return SemanticSelection("malformed", clarification=MSG_SELECTION_MALFORMED)
    criterion = _optional_referent_criterion(document)[1]
    if document.get("action") == "clarify":
        if document.get("queries") != []:
            _log_selection_rejection("clarify_incoherent", finish_reason)
            return SemanticSelection("malformed", clarification=MSG_SELECTION_MALFORMED)
        return SemanticSelection("clarify", clarification=_safe_clarification(document.get("clarification")))
    plan = {"catalog_version": document.get("catalog_version"), "queries": document.get("queries")}
    try:
        validate_query_plan(plan)
    except QueryCatalogError as exc:
        _log_selection_rejection(_CATALOG_REJECTION_CODES.get(exc.code, "invalid_query"), finish_reason)
        return SemanticSelection("malformed", clarification=MSG_SELECTION_MALFORMED)
    if _plan_leaves_authorized_tables(plan, authorized_tables):
        return SemanticSelection("clarify", clarification=MSG_REFERENT_CLARIFICATION)
    return SemanticSelection("query", plan=plan, referent_criterion=criterion)


def user_message_for_bridge_error(error_code: str | None) -> str:
    if error_code == "comparison_not_authorized":
        return MSG_COMPARISON_NOT_AUTHORIZED
    if error_code == "invalid_table_reference":
        return MSG_REFERENT_CLARIFICATION
    if error_code in _BRIDGE_UNAVAILABLE or not error_code:
        return MSG_COMPARISON_UNAVAILABLE
    return MSG_COMPARISON_UNAVAILABLE


def _selection_envelope(document: dict[str, Any] | None) -> bool:
    """Exige versão, ação, catálogo e coerência com as consultas. Não repara o JSON."""

    return _selection_rejection_code(document) is None


def _selection_rejection_code(document: dict[str, Any] | None) -> str | None:
    """Um código estável, na primeira falha. Não descreve o conteúdo rejeitado."""

    if not isinstance(document, dict):
        return "invalid_json"
    keys = set(document)
    if keys - _SELECTION_FIELDS:
        return "extra_field"
    if not _REQUIRED_SELECTION_FIELDS <= keys:
        return "missing_field"
    if document.get("selection_version") != SELECTION_VERSION:
        return "wrong_version"
    if document.get("action") not in _SELECTION_ACTIONS:
        return "wrong_action"
    if document.get("catalog_version") != CATALOG_VERSION:
        return "wrong_version"
    if not isinstance(document.get("queries"), list):
        return "invalid_query"
    clarification = document.get("clarification")
    if "clarification" in document and clarification is not None and not isinstance(clarification, str):
        return "invalid_query"
    accepted, _criterion = _optional_referent_criterion(document)
    if not accepted:
        return "invalid_referent_criterion"
    return None


def _log_selection_rejection(code: str, finish_reason: str | None) -> None:
    stable = code if code in _SELECTION_REJECTION_CODES else "invalid_query"
    reason = _public_finish_reason(finish_reason)
    if reason:
        logger.info("comparison_chat_selection_rejected code=%s finish_reason=%s", stable, reason)
        return
    logger.info("comparison_chat_selection_rejected code=%s", stable)


def _public_finish_reason(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > 64 or any(char.isspace() for char in token):
        return None
    if not all(char.isalnum() or char == "_" for char in token):
        return None
    return token


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _query_item_response_schema() -> dict[str, Any]:
    """Um objeto só. Tipo, filtros, campos, página e ordenação ficam no validador."""

    return {
        "type": "object",
        "additionalProperties": False,
        "propertyOrdering": list(_QUERY_PROPERTY_ORDER),
        "required": ["query_id", "type"],
        "properties": {
            "query_id": {
                "type": "string",
                "description": "Identificador obrigatório da consulta.",
            },
            "type": {
                "type": "string",
                "description": "Tipo pertencente ao catálogo fechado.",
            },
            "filters": _query_filter_response_schema(),
            "fields": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "Campos opcionais já aceitos pelo catálogo para o tipo.",
            },
            "page": {
                "type": "object",
                "additionalProperties": False,
                "propertyOrdering": ["offset", "limit"],
                "properties": {
                    "offset": {"type": "integer"},
                    "limit": {"type": "integer"},
                },
            },
            "sort": {
                "type": "string",
                "description": "Ordenação opcional já aceita pelo catálogo para o tipo.",
            },
        },
    }


def _query_filter_response_schema() -> dict[str, Any]:
    """União dos filtros do catálogo, sem uma alternativa por tipo."""

    properties: dict[str, Any] = {
        "table_ids": {"type": "array", "items": {"type": "string"}},
        "row_indexes": {"type": "array", "items": {"type": "integer"}},
        "component_codes": {"type": "array", "items": {"type": "string"}},
        "codes": {"type": "array", "items": {"type": "string"}},
        "ufs": {"type": "array", "items": {"type": "string"}},
        "include_absent": {"type": "boolean"},
        "include_unusable": {"type": "boolean"},
        "minimum_gap": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        "bands_version": {"type": "string"},
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "propertyOrdering": list(_FILTER_PROPERTY_ORDER),
        "description": "Objeto opcional de filtros. Não é a lista de nomes do catálogo.",
        "properties": properties,
    }


def _optional_referent_criterion(document: dict[str, Any]) -> tuple[bool, str | None]:
    """Ausência e null são válidos. Qualquer outro valor fora do conjunto é recusado."""

    if "referent_criterion" not in document:
        return True, None
    value = document.get("referent_criterion")
    if value is None:
        return True, None
    if isinstance(value, str) and value in REFERENT_CRITERIA:
        return True, value
    return False, None


def _plan_leaves_authorized_tables(plan: dict[str, Any], authorized_tables: list[dict[str, Any]]) -> bool:
    allowed = {item["table_id"] for item in authorized_tables if isinstance(item.get("table_id"), str)}
    for query in plan.get("queries") or []:
        if not isinstance(query, dict):
            return True
        filters = query.get("filters") if isinstance(query.get("filters"), dict) else {}
        table_ids = filters.get("table_ids") or []
        if not isinstance(table_ids, list):
            return True
        for table_id in table_ids:
            if table_id not in allowed:
                return True
    return False


def _json_object(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw or raw.startswith("```"):
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _safe_clarification(value: Any) -> str:
    if not isinstance(value, str):
        return MSG_REFERENT_CLARIFICATION
    text = " ".join(value.split())
    if not text or len(text) > _MAX_CLARIFICATION_CHARS:
        return MSG_REFERENT_CLARIFICATION
    lowered = text.lower()
    if any(fragment in lowered for fragment in _UNSAFE_CLARIFICATION_FRAGMENTS):
        return MSG_REFERENT_CLARIFICATION
    return text


def _mark_session_modified(session_obj) -> None:
    if hasattr(session_obj, "modified"):
        session_obj.modified = True


def _clear_referents(session_obj) -> None:
    if session_obj is None:
        return
    if REFERENTS_SESSION_KEY not in session_obj:
        return
    session_obj.pop(REFERENTS_SESSION_KEY, None)
    _mark_session_modified(session_obj)


def _normalize_referents(stored: dict[str, Any]) -> dict[str, Any]:
    carrier = stored.get("carrier")
    normalized_carrier = None
    if isinstance(carrier, dict) and isinstance(carrier.get("table_id"), str) and carrier.get("table_id"):
        normalized_carrier = {"table_id": carrier["table_id"]}
        if type(carrier.get("slot_number")) is int:
            normalized_carrier["slot_number"] = carrier["slot_number"]
        if isinstance(carrier.get("carrier_name"), str) and carrier["carrier_name"].strip():
            normalized_carrier["carrier_name"] = carrier["carrier_name"].strip()
    uf = stored.get("uf") if isinstance(stored.get("uf"), str) and stored.get("uf") else None
    rows = []
    for item in stored.get("ranked_rows") or []:
        if not isinstance(item, dict) or type(item.get("rank")) is not int or type(item.get("row_index")) is not int:
            continue
        if item["rank"] < 1 or item["row_index"] < 1:
            continue
        rows.append({"rank": item["rank"], "row_index": item["row_index"]})
        if len(rows) >= _MAX_RANKED_ROWS:
            break
    return {"carrier": normalized_carrier, "uf": uf, "ranked_rows": rows}


def _tables_by_slot(authorized_tables: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_slot: dict[int, dict[str, Any]] = {}
    ambiguous: set[int] = set()
    for table in authorized_tables:
        slot = table.get("slot_number")
        if type(slot) is not int:
            continue
        previous = by_slot.get(slot)
        if previous is not None and previous.get("table_id") != table.get("table_id"):
            ambiguous.add(slot)
            continue
        by_slot[slot] = table
    for slot in ambiguous:
        by_slot.pop(slot, None)
    return by_slot


def _single_slot_carrier(slots: Any, by_slot: dict[int, dict[str, Any]]) -> dict[str, Any] | None:
    if not isinstance(slots, list) or len(slots) != 1 or type(slots[0]) is not int:
        return None
    table = by_slot.get(slots[0])
    if table is None:
        return None
    return _carrier_ref(table)


def _carrier_ref(table: dict[str, Any]) -> dict[str, Any]:
    ref: dict[str, Any] = {"table_id": table["table_id"]}
    if type(table.get("slot_number")) is int:
        ref["slot_number"] = table["slot_number"]
    if isinstance(table.get("carrier_name"), str):
        ref["carrier_name"] = table["carrier_name"]
    return ref


def _carrier_for_referent_criterion(
    query: dict[str, Any],
    by_slot: dict[int, dict[str, Any]],
    criterion: str | None,
) -> dict[str, Any] | None:
    """Só o menor frete comparável pode confirmar transportadora.

    Maior economia, critério ausente ou nulo deixam o referente indeterminado.
    A publicação não atribui economia por transportadora, e frete mínimo,
    vitórias ou posição da lista não substituem esse critério.
    """

    if criterion != REFERENT_CRITERION_LOWEST_COMPARABLE_FREIGHT:
        return None
    return _carrier_confirmed_by_comparison(query, by_slot)


def _carrier_confirmed_by_comparison(
    query: dict[str, Any],
    by_slot: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """Transportadora do universo publicado, só com vencedor único em facts.carriers.

    O critério exposto é tie_rule unique_lowest_amount sobre o frete comparável.
    A ordem da lista é o slot, não um ranking. Empate, contradição ou campo
    ausente não escolhem a primeira transportadora.
    """

    claims = query.get("allowed_claims")
    if not isinstance(claims, list) or _BOUNDED_BEST_CLAIM not in claims:
        return None
    summary = _summary_facts(query.get("records"))
    if summary is None or summary.get("global_superiority") is not False:
        return None
    if summary.get("tie_rule") != _TIE_RULE_UNIQUE_LOWEST:
        return None
    if summary.get("universe") not in PUBLIC_UNIVERSES or not _decision_counts(summary):
        return None
    slot = _unique_lowest_slot(summary.get("carriers"))
    if slot is None or not _wins_agree(summary.get("carriers"), slot):
        return None
    table = by_slot.get(slot)
    if table is None:
        return None
    return _carrier_ref(table)


def _summary_facts(records: Any) -> dict[str, Any] | None:
    if not isinstance(records, list):
        return None
    for record in records:
        if not isinstance(record, dict) or record.get("record_role") != "summary":
            continue
        facts = record.get("facts")
        if isinstance(facts, dict):
            return facts
    return None


def _decision_counts(summary: dict[str, Any]) -> bool:
    comparable = summary.get("comparable_row_count")
    decisive = summary.get("decisive_row_count")
    ties = summary.get("tie_count")
    return (
        type(comparable) is int
        and type(decisive) is int
        and type(ties) is int
        and comparable > 0
        and decisive > 0
        and decisive + ties == comparable
    )


def _money_cents(text: Any, state: Any) -> int | None:
    if state not in _READY_MONEY_STATES or not isinstance(text, str):
        return None
    if not 4 <= len(text) <= 24 or text.count(".") != 1:
        return None
    whole, frac = text.split(".", 1)
    if not whole or not whole.isdigit() or len(frac) != 2 or not frac.isdigit():
        return None
    cents = int(whole) * 100 + int(frac)
    if (state == "zero") != (cents == 0):
        return None
    return cents


def _unique_lowest_slot(carriers: Any) -> int | None:
    if not isinstance(carriers, list) or len(carriers) < 2:
        return None
    priced: list[tuple[int, int]] = []
    seen: set[int] = set()
    for item in carriers:
        if not isinstance(item, dict):
            return None
        slot = item.get("carrier_slot")
        cents = _money_cents(item.get("comparable_freight_text"), item.get("comparable_freight_state"))
        if type(slot) is not int or cents is None or slot in seen:
            return None
        seen.add(slot)
        priced.append((cents, slot))
    priced.sort()
    if priced[0][0] == priced[1][0]:
        return None
    return priced[0][1]


def _wins_agree(carriers: Any, winner: int) -> bool:
    if not isinstance(carriers, list) or len(carriers) < 2:
        return False
    values = [item.get("wins") if isinstance(item, dict) else None for item in carriers]
    if all(value is None for value in values):
        return True
    if any(type(value) is not int for value in values):
        return False
    ranked = sorted(
        ((int(item["wins"]), item["carrier_slot"]) for item in carriers if isinstance(item, dict)),
        key=lambda item: (-item[0], item[1]),
    )
    if len(ranked) < 2 or ranked[0][0] <= 0 or ranked[0][0] == ranked[1][0]:
        return True
    return ranked[0][1] == winner


def _positional_ranked_rows(query: dict[str, Any]) -> list[dict[str, int]]:
    """Posições da página publicada. Página vazia ou de outro recorte não herda a anterior."""

    page = query.get("page") if isinstance(query.get("page"), dict) else None
    returned = page.get("returned") if isinstance(page, dict) else None
    offset = page.get("offset") if isinstance(page, dict) else None
    if type(returned) is int and returned <= 0:
        return []
    records = query.get("records")
    if not isinstance(records, list):
        return []
    rows: list[dict[str, int]] = []
    for record in records:
        if not isinstance(record, dict) or record.get("record_role") != "detail":
            continue
        record_page = record.get("page")
        if isinstance(page, dict) and isinstance(record_page, dict):
            if record_page.get("offset") != offset or record_page.get("returned") != returned:
                return []
        subject = record.get("subject")
        if not isinstance(subject, dict) or subject.get("kind") != "row":
            continue
        row_index = subject.get("row_index")
        if type(row_index) is not int or row_index < 1:
            continue
        rows.append({"rank": len(rows) + 1, "row_index": row_index})
        if len(rows) >= _MAX_RANKED_ROWS:
            break
    if type(returned) is int and len(rows) != min(returned, _MAX_RANKED_ROWS):
        return []
    return rows


def publication_is_answerable(publication: Any) -> bool:
    if not isinstance(publication, dict):
        return False
    if publication.get("publication_version") != PUBLICATION_VERSION:
        return False
    return publication.get("status") in {"ok", "partial"}


def copy_publication(publication: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(publication)
