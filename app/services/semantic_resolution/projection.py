"""Adaptador experimental entre resolução aceita e o preview do Lote 3.

Não calcula frete. A chave legada só aparece neste retorno, nunca como
mecanismo de matching do resolver. available só fica verdadeiro depois de
validar o preview, a unicidade da regra semântica e a referência executável.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_freight_contract.compiler import validate_compilation_preview


def _legacy_lookup(destination: dict[str, Any] | None) -> str | None:
    if not isinstance(destination, dict):
        return None
    from app.agente_compara_doc_service import _coverage_lookup_key

    if destination.get("kind") == "municipality":
        return _coverage_lookup_key(destination.get("state"), destination.get("name"))
    if destination.get("kind") == "state":
        state = destination.get("state")
        return str(state) if state else None
    return None


def _unavailable(result: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    result["reason_codes"] = list(dict.fromkeys(reasons))
    return result


def _preview_block_reasons(preview: Any) -> list[str]:
    """Valida o preview inteiro antes de procurar regra."""
    if not isinstance(preview, dict) or not preview:
        return ["preview_invalid"]
    reasons: list[str] = []
    body = preview.get("pricing_contract_preview") if isinstance(preview.get("pricing_contract_preview"), dict) else {}
    if preview.get("activated") is not False:
        reasons.append("preview_activated")
    if preview.get("operational") is True or body.get("operational") is not False:
        reasons.append("preview_operational")
    if body.get("preview_only") is not True or preview.get("preview_only") is False:
        reasons.append("preview_not_experimental")
    validation = validate_compilation_preview(preview)
    if not validation.get("ok"):
        for finding in validation.get("findings") or []:
            code = finding.get("code") if isinstance(finding, dict) else None
            if isinstance(code, str) and code not in reasons:
                reasons.append(code)
        if not reasons:
            reasons.append("preview_invalid")
    return reasons


def _applicable_semantic_rules(
    contract: dict[str, Any],
    *,
    matched_lanes: set[Any],
    dimension_id: Any,
) -> list[dict[str, Any]]:
    """Regras cujo escopo é a dimensão e a lane resolvidas, tenham ou não projeção."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rule in contract.get("price_rules") or []:
        if not isinstance(rule, dict):
            continue
        rule_id = rule.get("rule_id")
        if not isinstance(rule_id, str) or rule_id in seen:
            continue
        if rule.get("lane_id") not in matched_lanes:
            continue
        rule_dimension = rule.get("dimension_id")
        if rule_dimension not in (None, dimension_id):
            continue
        seen.add(rule_id)
        found.append(rule)
    return found


def _projection_for(preview: dict[str, Any], rule_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in (preview.get("rule_projection") or [])
        if isinstance(item, dict) and item.get("semantic_rule_id") == rule_id
    ]


def _preview_rule_at(rules: list[Any], ref: Any) -> dict[str, Any] | None:
    if not isinstance(ref, str) or not ref.startswith("preview:"):
        return None
    index_text = ref[len("preview:") :]
    if not index_text.isdigit():
        return None
    index = int(index_text)
    if index >= len(rules):
        return None
    rule = rules[index]
    if not isinstance(rule, dict):
        return None
    return rule


def experimental_projection_ref(resolution: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "available": False,
        "preview_only": True,
        "activated": False,
        "operational": False,
        "semantic_rule_id": None,
        "executable_rule_ref": None,
        "legacy_lookup_key": None,
        "reason_codes": [],
    }
    if resolution.get("resolution_status") != "resolved" or resolution.get("review_state") != "accepted":
        return result
    preview = contract.get("compilation_preview") if isinstance(contract.get("compilation_preview"), dict) else {}
    blocked = _preview_block_reasons(preview)
    if blocked:
        return _unavailable(result, blocked)

    matched_lanes = set(resolution.get("matched_lane_ids") or [])
    dimension_id = resolution.get("matched_pricing_dimension_id")
    applicable = _applicable_semantic_rules(
        contract,
        matched_lanes=matched_lanes,
        dimension_id=dimension_id,
    )
    if len(applicable) != 1:
        reason = "ambiguous_rule_projection" if len(applicable) > 1 else "no_applicable_semantic_rule"
        return _unavailable(result, [reason])

    rule_id = applicable[0]["rule_id"]
    projections = _projection_for(preview, rule_id)
    if len(projections) != 1:
        reason = "ambiguous_rule_projection" if len(projections) > 1 else "executable_ref_missing"
        return _unavailable(result, [reason])
    executable_ref = projections[0].get("executable_rule_ref")
    if not executable_ref:
        return _unavailable(result, ["executable_ref_missing"])

    body = preview.get("pricing_contract_preview") if isinstance(preview.get("pricing_contract_preview"), dict) else {}
    rules = body.get("rules") if isinstance(body.get("rules"), list) else []
    preview_rule = _preview_rule_at(rules, executable_ref)
    if preview_rule is None:
        return _unavailable(result, ["executable_ref_not_found"])
    if preview_rule.get("semantic_rule_id") != rule_id or projections[0].get("semantic_rule_id") != rule_id:
        return _unavailable(result, ["executable_ref_incoherent"])

    result["available"] = True
    result["semantic_rule_id"] = rule_id
    result["executable_rule_ref"] = executable_ref
    result["legacy_lookup_key"] = _legacy_lookup(resolution.get("destination_entity"))
    return result
