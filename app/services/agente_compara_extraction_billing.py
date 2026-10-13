"""
Cobrança da extração de tabela do AgenteCompara.

O débito acontece depois da entrega: a tabela precisa ter valor de frete e
contexto tarifário suficiente para uso, estar persistida e ficar disponível
para revisão. O status needs_review sozinho não comprova utilidade. Tokens de
tentativas fracassadas continuam no evento de consumo; a reserva dessas
tentativas é liberada sem débito.
"""
from __future__ import annotations

import hashlib
from uuid import uuid4

_STATUS_REVISAVEL = frozenset({"needs_review", "awaiting_validation", "validated"})
_PREFIXO_ENTREGA = "ac-tt:"


def digest_da_entrega(
    source_doc_ids: list[str],
    *,
    comparison_id: str | None,
    table_id: str | None,
) -> str:
    from app.agente_compara_doc_service import agente_compara_temp_table_extraction_idempotency_key

    bruto = agente_compara_temp_table_extraction_idempotency_key(
        source_doc_ids,
        comparison_id=comparison_id,
        table_id=table_id,
    )
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()[:32]


def attempt_key_da_tentativa(digest: str, model: str) -> str:
    modelo = (model or "modelo").strip()[:40] or "modelo"
    return f"{_PREFIXO_ENTREGA}{digest}:{modelo}:{uuid4().hex[:12]}"[:160]


_DEFEITOS_SEM_FRETE_UTIL = frozenset(
    {
        "no_executable_rule",
        "missing_lookup_key",
        "unresolved_pricing_type",
        "invalid_tariff",
        "invalid_range",
        "invalid_excess",
        "missing_unit",
        "freight_value_not_executable",
    }
)


def _contexto_tarifario_suficiente(rule: dict) -> bool:
    """Destino, praça ou rota. Rótulo de coluna de valor, como Frete, não localiza a tarifa."""
    from app.agente_compara_doc_service import _is_value_column

    keys = [
        key.strip()
        for key in (rule.get("lookup_keys") or [])
        if isinstance(key, str) and key.strip()
    ]
    if not keys:
        return False
    return any(not _is_value_column(key) for key in keys)


def _valor_de_frete_executavel(rule: dict) -> bool:
    from app.agente_compara_doc_service import _is_finite_number

    pricing_type = rule.get("pricing_type")
    if pricing_type == "direct_weight_rate":
        unit = str(rule.get("unit") or "").strip().lower()
        amount = rule.get("value_per_ton") if unit == "ton" else rule.get("value_per_kg")
        return _is_finite_number(amount) and float(amount) >= 0
    if pricing_type in {"fixed_range", "range_plus_excess_per_kg"}:
        brackets = rule.get("brackets") if isinstance(rule.get("brackets"), list) else []
        return any(
            isinstance(item, dict)
            and _is_finite_number(item.get("value"))
            and float(item.get("value")) >= 0
            and _is_finite_number(item.get("max_kg"))
            for item in brackets
        )
    return False


def _regra_comercialmente_aplicavel(rule: dict) -> bool:
    from app.agente_compara_temp_table_validation_service import pricing_contract_structure_findings

    if not isinstance(rule, dict):
        return False
    if not _contexto_tarifario_suficiente(rule) or not _valor_de_frete_executavel(rule):
        return False
    try:
        findings = pricing_contract_structure_findings({"rules": [rule]})
    except Exception:
        return False
    return not any(
        isinstance(item, dict) and item.get("kind") in _DEFEITOS_SEM_FRETE_UTIL
        for item in findings
    )


def estrutura_tarifaria_aproveitavel(registro: dict) -> bool:
    """Valor de frete executável com contexto suficiente para uso.

    Destino isolado, rota sem tarifa, rótulo Frete, faixa sem preço, pedágio
    ou acessório isolado e JSON sem regra utilizável não autorizam débito.
    """
    from app.agente_compara_doc_service import _compile_pricing_contract

    if not isinstance(registro, dict):
        return False
    try:
        contract, _findings = _compile_pricing_contract(registro)
    except Exception:
        return False
    rules = contract.get("rules") if isinstance(contract, dict) else None
    if not isinstance(rules, list):
        return False
    return any(_regra_comercialmente_aplicavel(rule) for rule in rules if isinstance(rule, dict))


def entrega_faturavel(registro: dict | None) -> bool:
    """Tabela persistida, revisável e com estrutura tarifária aproveitável."""
    if not isinstance(registro, dict):
        return False
    identificador = registro.get("temp_table_id")
    if not isinstance(identificador, str) or not identificador.strip():
        return False
    status = registro.get("status")
    if not isinstance(status, str) or status.strip().lower() not in _STATUS_REVISAVEL:
        return False
    return estrutura_tarifaria_aproveitavel(registro)


def liquidar_ou_dispensar_entrega(attempt_id: int, digest: str):
    """Decide o débito da entrega e a liquidação na mesma transação exclusiva."""
    from app.services.cleiton_billable_ai_call import liquidar_entrega_agente_compara

    return liquidar_entrega_agente_compara(
        int(attempt_id),
        prefixo=f"{_PREFIXO_ENTREGA}{digest}:",
    )
