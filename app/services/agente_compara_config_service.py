"""
Configuração operacional da Agente Compara documental (persistência em ConfigRegras).

Fase 1:
- defaults seguros e prefixo próprio (`agente_compara_cfg_`);
- isolamento dos blocos Cleide (`cleide_cfg_` / `cleide_audit_cfg_`) e dos controles globais do Cleiton;
- limites documentais respeitam tetos globais do Cleiton na leitura efetiva.
"""
from __future__ import annotations

import logging
import json
import re
import unicodedata
from dataclasses import dataclass, field, replace
from typing import Any

from flask import g, has_request_context

from app.extensions import db
from app.models import ConfigRegras
from app.services.cleiton_doc_config_service import get_cleiton_doc_config

logger = logging.getLogger(__name__)

_CFG_PREFIX = "agente_compara_cfg_"

_BOOL_FIELDS = frozenset(
    {
        "chat_enabled",
        "upload_enabled",
        "show_documents_used",
        "no_hallucination_instruction_enabled",
    }
)

_BOOL_CHECKBOX_FIELDS = frozenset(_BOOL_FIELDS)

_NO_DOCUMENTS_BEHAVIORS = frozenset({"allow_guided", "require_documents"})

DEFAULT_FALLBACK_MESSAGE = (
    "Não foi possível obter resposta da Agente Compara no momento. Tente novamente em instantes."
)

DEFAULT_AUDITED_FILE_MAX_ROWS = 2000

DEFAULT_CALCULATION_BASES: list[dict[str, Any]] = [
    {
        "id": "pct_nota_fiscal",
        "label": "% por nota fiscal",
        "aliases": [
            "valor da nf",
            "valor da nota fiscal",
            "valor_nf",
            "nota fiscal",
            "sobre nf",
            "sobre nota fiscal",
            "sobre o valor da nf",
            "sobre o valor da nota fiscal",
            "sobre o valor de nf",
            "sobre o valor de n fiscal",
            "sobre o valor de n.fiscal",
            "s/ valor da nf",
            "s/ valor da nota fiscal",
            "valor de n.fiscal",
            "valor da n.fiscal",
        ],
        "unit": "%",
        "calculation_type": "invoice_percentage",
        "audit_variable": "valor_nf",
        "operation": "percentage_of_variable",
        "parameters": {},
        "allows_minimum": True,
        "allows_maximum": True,
        "requires_structured_condition": False,
        "is_active": True,
        "display_order": 10,
    },
    {
        "id": "por_cte",
        "label": "por CTe",
        "aliases": ["cte", "por cte", "conhecimento", "documento"],
        "unit": "R$",
        "calculation_type": "fixed_amount",
        "audit_variable": None,
        "operation": "fixed_amount",
        "parameters": {},
        "allows_minimum": False,
        "allows_maximum": False,
        "requires_structured_condition": False,
        "is_active": True,
        "display_order": 20,
    },
    {
        "id": "por_conhecimento",
        "label": "por conhecimento",
        "aliases": ["conhecimento", "por conhecimento", "cte", "documento"],
        "unit": "R$",
        "calculation_type": "fixed_amount",
        "audit_variable": None,
        "operation": "fixed_amount",
        "parameters": {},
        "allows_minimum": False,
        "allows_maximum": False,
        "requires_structured_condition": False,
        "is_active": True,
        "display_order": 30,
    },
    {
        "id": "por_documento",
        "label": "por documento",
        "aliases": ["documento", "por documento", "doc", "por doc"],
        "unit": "R$",
        "calculation_type": "fixed_amount",
        "audit_variable": None,
        "operation": "fixed_amount",
        "parameters": {},
        "allows_minimum": False,
        "allows_maximum": False,
        "requires_structured_condition": False,
        "is_active": True,
        "display_order": 40,
    },
    {
        "id": "por_kg",
        "label": "por kg",
        "aliases": ["kg", "quilo", "quilograma", "por kg", "peso"],
        "unit": "R$",
        "calculation_type": "weight",
        "audit_variable": "peso",
        "operation": "multiply_by_variable",
        "parameters": {},
        "allows_minimum": False,
        "allows_maximum": False,
        "requires_structured_condition": False,
        "is_active": True,
        "display_order": 50,
    },
    {
        "id": "fracao_100kg",
        "label": "por fração de 100kg",
        "aliases": [
            "100kg ou fração",
            "100kg ou fracao",
            "cada 100kg",
            "para cada 100kg",
            "para cada 100kg ou fração",
            "para cada 100kg ou fracao",
            "fração de 100kg",
            "fracao de 100kg",
            "por fração de 100kg",
            "por fracao de 100kg",
        ],
        "unit": "R$",
        "calculation_type": "weight_fraction",
        "audit_variable": "peso",
        "operation": "ceil_fraction",
        "parameters": {"fraction_size": 100},
        "allows_minimum": False,
        "allows_maximum": False,
        "requires_structured_condition": False,
        "is_active": True,
        "display_order": 60,
    },
]

DEFAULTS: dict[str, Any] = {
    "chat_enabled": 1,
    "upload_enabled": 1,
    "chat_max_history": 10,
    "document_context_max_chars": 24000,
    "max_documents_considered": 3,
    "question_max_chars": 4000,
    "comparison_chat_question_max_chars": 4000,
    "comparison_chat_history_max_items": 10,
    "comparison_chat_context_max_chars": 48000,
    "comparison_chat_max_rows": 12,
    "comparison_chat_max_memories": 6,
    "comparison_chat_max_table_rules": 24,
    "comparison_chat_max_ranked_items": 8,
    "fallback_message": DEFAULT_FALLBACK_MESSAGE,
    "no_documents_behavior": "allow_guided",
    "show_documents_used": 1,
    "no_hallucination_instruction_enabled": 1,
    "audited_file_max_bytes": None,
    "audited_file_max_rows": DEFAULT_AUDITED_FILE_MAX_ROWS,
    "calculation_bases": DEFAULT_CALCULATION_BASES,
}

GENERAL_FORM_CONFIG_FIELDS: tuple[str, ...] = (
    "chat_enabled",
    "upload_enabled",
    "chat_max_history",
    "document_context_max_chars",
    "max_documents_considered",
    "question_max_chars",
    "audited_file_max_bytes",
    "audited_file_max_rows",
    "no_documents_behavior",
    "show_documents_used",
    "no_hallucination_instruction_enabled",
    "fallback_message",
)

CALCULATION_BASES_FORM_CONFIG_FIELDS: tuple[str, ...] = ("calculation_bases",)

DESCRICOES: dict[str, str] = {
    "chat_enabled": "Habilita o chat IA da Agente Compara em /agente-compara.",
    "upload_enabled": "Habilita upload documental da Agente Compara.",
    "chat_max_history": "Janela de histórico (mensagens) enviada ao backend da Agente Compara.",
    "document_context_max_chars": "Máximo de caracteres do contexto documental no prompt da Agente Compara.",
    "max_documents_considered": "Máximo de documentos considerados por resposta da Agente Compara.",
    "question_max_chars": "Máximo de caracteres aceitos por pergunta no chat da Agente Compara.",
    "comparison_chat_question_max_chars": (
        "Máximo de caracteres por pergunta no chat inteligente da comparação vigente."
    ),
    "comparison_chat_history_max_items": (
        "Janela de histórico (mensagens) do chat inteligente da comparação."
    ),
    "comparison_chat_context_max_chars": (
        "Máximo de caracteres do contexto comparativo montado para o modelo."
    ),
    "comparison_chat_max_rows": "Máximo de linhas comparativas incluídas no contexto do chat.",
    "comparison_chat_max_memories": "Máximo de memórias de cálculo incluídas no contexto do chat.",
    "comparison_chat_max_table_rules": "Máximo de regras/taxas de tabela incluídas no contexto do chat.",
    "comparison_chat_max_ranked_items": "Máximo de itens ranqueados (UFs/diffs) no contexto do chat.",
    "fallback_message": "Mensagem amigável exibida em falha de IA da Agente Compara (não é resposta normal).",
    "no_documents_behavior": "Comportamento sem documentos: allow_guided ou require_documents.",
    "show_documents_used": "Exibe metadados dos documentos usados na resposta ao usuário.",
    "no_hallucination_instruction_enabled": (
        "Reforça instrução anti-alucinação no prompt da Agente Compara."
    ),
    "audited_file_max_bytes": (
        "Limite específico opcional de bytes para o arquivo auditado da Agente Compara."
    ),
    "audited_file_max_rows": (
        "Define o limite de linhas aceitas no arquivo enviado para auditoria de frete."
    ),
    "calculation_bases": (
        "Bases de cálculo administrativas da Agente Compara para futura classificação de taxas."
    ),
}


@dataclass(frozen=True)
class AgenteComparaConfig:
    chat_enabled: bool
    upload_enabled: bool
    chat_max_history: int
    document_context_max_chars: int
    max_documents_considered: int
    question_max_chars: int
    fallback_message: str
    no_documents_behavior: str
    show_documents_used: bool
    no_hallucination_instruction_enabled: bool
    audited_file_max_bytes: int | None
    audited_file_max_rows: int
    comparison_chat_question_max_chars: int = 4000
    comparison_chat_history_max_items: int = 10
    comparison_chat_context_max_chars: int = 48000
    comparison_chat_max_rows: int = 12
    comparison_chat_max_memories: int = 6
    comparison_chat_max_table_rules: int = 24
    comparison_chat_max_ranked_items: int = 8
    calculation_bases: list[dict[str, Any]] = field(
        default_factory=lambda: json.loads(
            json.dumps(DEFAULT_CALCULATION_BASES, ensure_ascii=False)
        )
    )


def _cfg_key(nome: str) -> str:
    return f"{_CFG_PREFIX}{nome}"


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "on", "yes", "sim"}:
        return True
    if text in {"0", "false", "off", "no", "nao", "não", ""}:
        return False
    return default


def _coerce_bool_checkbox(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return _coerce_bool(value, False)


def _clone_default_calculation_bases() -> list[dict[str, Any]]:
    return json.loads(json.dumps(DEFAULT_CALCULATION_BASES, ensure_ascii=False))


def _coerce_aliases(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[;,]", value)]
    elif isinstance(value, list):
        items = [str(part or "").strip() for part in value]
    else:
        raise ValueError("aliases deve ser uma lista de textos.")
    return [item for item in items if item]


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_display_order(value: Any, fallback: int) -> int:
    if value is None or str(value).strip() == "":
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("display_order deve ser um inteiro.") from exc


def _slugify_calculation_base_id(label: str, fallback: str) -> str:
    text = unicodedata.normalize("NFKD", label)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    if not text:
        text = fallback
    if text[0].isdigit():
        text = f"base_{text}"
    return text


def _parse_fraction_size(value: Any, base_label: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"Base de cálculo '{base_label}': fraction_size deve ser informado.")
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Base de cálculo '{base_label}': fraction_size deve ser inteiro.") from exc
    if parsed <= 0:
        raise ValueError(f"Base de cálculo '{base_label}': fraction_size deve ser maior que zero.")
    return parsed


def _get_form_value(raw_values: Any, name: str, default: Any = None) -> Any:
    getter = getattr(raw_values, "get", None)
    if callable(getter):
        return getter(name, default)
    if isinstance(raw_values, dict):
        return raw_values.get(name, default)
    return default


def _get_form_list(raw_values: Any, name: str) -> list[str]:
    getlist = getattr(raw_values, "getlist", None)
    if callable(getlist):
        return [str(item) for item in getlist(name)]
    if isinstance(raw_values, dict):
        value = raw_values.get(name, [])
        if isinstance(value, list):
            return [str(item) for item in value]
        if value in (None, ""):
            return []
        return [str(value)]
    return []


def _form_has_name(raw_values: Any, name: str) -> bool:
    try:
        return name in raw_values
    except TypeError:
        return False


def parsear_calculation_bases_form(raw_values: Any) -> list[dict[str, Any]]:
    row_indices = [
        index.strip()
        for index in _get_form_list(raw_values, "calculation_base_row_index")
        if index.strip()
    ]
    bases: list[dict[str, Any]] = []
    used_ids: set[str] = set()

    for position, row_index in enumerate(row_indices):
        label = str(_get_form_value(raw_values, f"calculation_base_label_{row_index}") or "").strip()
        if not label:
            raise ValueError(f"Base de cálculo #{position + 1}: nome da base é obrigatório.")

        base_id = str(_get_form_value(raw_values, f"calculation_base_id_{row_index}") or "").strip()
        if not base_id:
            base_id = _slugify_calculation_base_id(label, f"base_{position + 1}")

        if base_id in used_ids:
            suffix = 2
            original_id = base_id
            while f"{original_id}_{suffix}" in used_ids:
                suffix += 1
            base_id = f"{original_id}_{suffix}"
        used_ids.add(base_id)

        calculation_type = str(
            _get_form_value(raw_values, f"calculation_base_calculation_type_{row_index}") or ""
        ).strip()
        operation = str(
            _get_form_value(raw_values, f"calculation_base_operation_{row_index}") or ""
        ).strip()
        parameters: dict[str, Any] = {}
        if operation == "ceil_fraction":
            parameters["fraction_size"] = _parse_fraction_size(
                _get_form_value(raw_values, f"calculation_base_fraction_size_{row_index}"),
                label,
            )

        raw_base = {
            "id": base_id,
            "label": label,
            "aliases": _get_form_value(raw_values, f"calculation_base_aliases_{row_index}") or "",
            "unit": str(_get_form_value(raw_values, f"calculation_base_unit_{row_index}") or "").strip(),
            "calculation_type": calculation_type,
            "audit_variable": _get_form_value(
                raw_values,
                f"calculation_base_audit_variable_{row_index}",
            ),
            "operation": operation,
            "parameters": parameters,
            "allows_minimum": _form_has_name(
                raw_values,
                f"calculation_base_allows_minimum_{row_index}",
            ),
            "allows_maximum": _form_has_name(
                raw_values,
                f"calculation_base_allows_maximum_{row_index}",
            ),
            "requires_structured_condition": _form_has_name(
                raw_values,
                f"calculation_base_requires_structured_condition_{row_index}",
            ),
            "is_active": _form_has_name(raw_values, f"calculation_base_is_active_{row_index}"),
            "display_order": _get_form_value(
                raw_values,
                f"calculation_base_display_order_{row_index}",
                (position + 1) * 10,
            ),
        }
        bases.append(raw_base)

    return validar_calculation_bases(bases)


def _validate_calculation_base(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"Base de cálculo #{index + 1} deve ser um objeto JSON.")

    required_fields = ("id", "label", "unit", "calculation_type", "operation", "is_active")
    missing = [field for field in required_fields if field not in raw]
    if missing:
        raise ValueError(
            f"Base de cálculo #{index + 1} sem campos obrigatórios: {', '.join(missing)}."
        )

    base_id = str(raw.get("id") or "").strip()
    label = str(raw.get("label") or "").strip()
    unit = str(raw.get("unit") or "").strip()
    calculation_type = str(raw.get("calculation_type") or "").strip()
    operation = str(raw.get("operation") or "").strip()
    if not base_id or not label or not unit or not calculation_type or not operation:
        raise ValueError(
            f"Base de cálculo #{index + 1} deve informar id, label, unit, "
            "calculation_type e operation."
        )

    parameters = raw.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError(f"Base de cálculo {base_id}: parameters deve ser um objeto JSON.")

    return {
        "id": base_id,
        "label": label,
        "aliases": _coerce_aliases(raw.get("aliases")),
        "unit": unit,
        "calculation_type": calculation_type,
        "audit_variable": _coerce_optional_str(raw.get("audit_variable")),
        "operation": operation,
        "parameters": parameters,
        "allows_minimum": _coerce_bool(raw.get("allows_minimum"), False),
        "allows_maximum": _coerce_bool(raw.get("allows_maximum"), False),
        "requires_structured_condition": _coerce_bool(
            raw.get("requires_structured_condition"),
            False,
        ),
        "is_active": _coerce_bool(raw.get("is_active"), True),
        "display_order": _coerce_display_order(raw.get("display_order"), (index + 1) * 10),
    }


def validar_calculation_bases(raw_bases: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_bases, list):
        raise ValueError("calculation_bases deve ser uma lista JSON.")
    bases = [_validate_calculation_base(raw, index) for index, raw in enumerate(raw_bases)]
    ids = [base["id"] for base in bases]
    if len(ids) != len(set(ids)):
        raise ValueError("calculation_bases não pode conter ids duplicados.")
    return sorted(bases, key=lambda base: (base["display_order"], base["label"].lower()))


def _merge_default_calculation_base_aliases(bases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    default_aliases_by_id = {
        base["id"]: list(base.get("aliases") or [])
        for base in DEFAULT_CALCULATION_BASES
        if isinstance(base, dict) and base.get("id")
    }
    merged: list[dict[str, Any]] = []
    for base in bases:
        item = dict(base)
        default_aliases = default_aliases_by_id.get(str(item.get("id") or ""))
        if default_aliases:
            aliases = list(item.get("aliases") or [])
            normalized_existing = {normalize_calculation_base_text(alias) for alias in aliases}
            for alias in default_aliases:
                normalized_alias = normalize_calculation_base_text(alias)
                if normalized_alias and normalized_alias not in normalized_existing:
                    aliases.append(alias)
                    normalized_existing.add(normalized_alias)
            item["aliases"] = aliases
        merged.append(item)
    return merged


def parsear_calculation_bases_json(raw_json: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw_json or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON inválido em calculation_bases: {exc.msg}.") from exc
    return validar_calculation_bases(payload)


def _parse_calculation_bases(cfg_map: dict[str, ConfigRegras]) -> list[dict[str, Any]]:
    row = cfg_map.get(_cfg_key("calculation_bases"))
    if row is None or not row.valor_texto:
        return _clone_default_calculation_bases()
    try:
        return _merge_default_calculation_base_aliases(parsear_calculation_bases_json(row.valor_texto))
    except ValueError:
        logger.warning(
            "Agente Compara audit config: calculation_bases inválido em ConfigRegras; usando defaults."
        )
        return _clone_default_calculation_bases()


def formatar_calculation_bases_json(bases: list[dict[str, Any]] | None = None) -> str:
    normalized = validar_calculation_bases(
        _clone_default_calculation_bases() if bases is None else bases
    )
    return json.dumps(normalized, ensure_ascii=False, indent=2)


def normalize_calculation_base_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[_\-\/]+", " ", text)
    text = re.sub(r"[^\w%$]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize_calculation_base_unit(value: Any) -> str:
    text = normalize_calculation_base_text(value)
    compact = text.replace(" ", "")
    if compact in {"%", "percent", "percentual", "porcentagem"}:
        return "%"
    if compact in {"r$", "rs", "brl", "real", "reais"}:
        return "R$"
    if compact in {"kg", "quilo", "quilos", "quilograma", "quilogramas"}:
        return "kg"
    return compact


def _calculation_base_match_tokens(base: dict[str, Any]) -> set[str]:
    tokens = {normalize_calculation_base_text(base.get("label"))}
    tokens.update(normalize_calculation_base_text(alias) for alias in base.get("aliases") or [])
    return {token for token in tokens if token}


def resolve_calculation_base_status(
    calculation_basis: Any,
    unit: Any,
    calculation_bases: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    basis_text = normalize_calculation_base_text(calculation_basis)
    if not basis_text:
        return {"status": "not_found", "base": None}

    fee_unit = normalize_calculation_base_unit(unit)
    matches: list[dict[str, Any]] = []
    for raw_base in calculation_bases or []:
        if not isinstance(raw_base, dict) or not _coerce_bool(raw_base.get("is_active"), True):
            continue
        base_unit = normalize_calculation_base_unit(raw_base.get("unit"))
        if base_unit and fee_unit != base_unit:
            continue
        if basis_text in _calculation_base_match_tokens(raw_base):
            matches.append(raw_base)

    if len(matches) == 1:
        return {"status": "matched", "base": matches[0]}
    if len(matches) > 1:
        return {"status": "ambiguous", "base": None, "matches": matches}
    return {"status": "not_found", "base": None}


def resolve_calculation_base(
    calculation_basis: Any,
    unit: Any,
    calculation_bases: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    result = resolve_calculation_base_status(calculation_basis, unit, calculation_bases)
    base = result.get("base")
    return base if result.get("status") == "matched" and isinstance(base, dict) else None


def get_active_calculation_bases(
    calculation_bases: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    bases = calculation_bases
    if bases is None:
        bases = get_agente_compara_config().calculation_bases
    active = [
        dict(base)
        for base in bases or []
        if isinstance(base, dict) and _coerce_bool(base.get("is_active"), True)
    ]
    return sorted(
        active,
        key=lambda base: (
            _coerce_display_order(base.get("display_order"), 0),
            str(base.get("label") or "").lower(),
        ),
    )


def get_active_calculation_base_by_id(
    calculation_base_id: Any,
    calculation_bases: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    wanted = str(calculation_base_id or "").strip()
    if not wanted:
        return None
    for base in get_active_calculation_bases(calculation_bases):
        if str(base.get("id") or "").strip() == wanted:
            return base
    return None


def serialize_calculation_base_for_runtime(base: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(base.get("id") or "").strip(),
        "label": str(base.get("label") or "").strip(),
        "unit": str(base.get("unit") or "").strip(),
        "aliases": list(base.get("aliases") or []),
        "calculation_type": str(base.get("calculation_type") or "").strip(),
        "audit_variable": _coerce_optional_str(base.get("audit_variable")),
        "operation": str(base.get("operation") or "").strip(),
        "parameters": dict(base.get("parameters") or {}),
    }


def get_active_calculation_bases_for_runtime(
    calculation_bases: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        serialize_calculation_base_for_runtime(base)
        for base in get_active_calculation_bases(calculation_bases)
    ]


def _coerce_no_documents_behavior(value: Any, default: str) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in _NO_DOCUMENTS_BEHAVIORS:
        return candidate
    return default


def _bounded(nome: str, valor: int) -> int:
    if nome == "chat_max_history":
        return min(max(1, valor), 100)
    if nome == "document_context_max_chars":
        return min(max(2000, valor), 200000)
    if nome == "max_documents_considered":
        return min(max(1, valor), 10)
    if nome == "question_max_chars":
        return min(max(500, valor), 12000)
    if nome == "comparison_chat_question_max_chars":
        return min(max(500, valor), 12000)
    if nome == "comparison_chat_history_max_items":
        return min(max(1, valor), 100)
    if nome == "comparison_chat_context_max_chars":
        return min(max(4000, valor), 200000)
    if nome == "comparison_chat_max_rows":
        return min(max(1, valor), 100)
    if nome == "comparison_chat_max_memories":
        return min(max(1, valor), 50)
    if nome == "comparison_chat_max_table_rules":
        return min(max(1, valor), 100)
    if nome == "comparison_chat_max_ranked_items":
        return min(max(1, valor), 50)
    if nome == "audited_file_max_bytes":
        return min(max(1, valor), 200 * 1024 * 1024)
    if nome == "audited_file_max_rows":
        return min(max(1, valor), 50000)
    return valor


def _load_cfg_map() -> dict[str, ConfigRegras]:
    keys = [_cfg_key(nome) for nome in DEFAULTS.keys()]
    rows = ConfigRegras.query.filter(ConfigRegras.chave.in_(keys)).all()
    return {row.chave: row for row in rows}


def _parse_bool(cfg_map: dict[str, ConfigRegras], nome: str) -> bool:
    default = bool(DEFAULTS[nome])
    row = cfg_map.get(_cfg_key(nome))
    if row is None:
        return default
    raw = row.valor_inteiro if row.valor_inteiro is not None else row.valor_texto
    return _coerce_bool(raw, default)


def _parse_int(cfg_map: dict[str, ConfigRegras], nome: str) -> int:
    default = int(DEFAULTS[nome])
    row = cfg_map.get(_cfg_key(nome))
    if row is None:
        return _bounded(nome, default)
    raw = row.valor_inteiro if row.valor_inteiro is not None else row.valor_texto
    return _bounded(nome, _coerce_positive_int(raw, default))


def _parse_optional_int(cfg_map: dict[str, ConfigRegras], nome: str) -> int | None:
    row = cfg_map.get(_cfg_key(nome))
    if row is None:
        return None
    raw = row.valor_inteiro if row.valor_inteiro is not None else row.valor_texto
    if raw is None or str(raw).strip() == "":
        return None
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return _bounded(nome, parsed)


def _parse_str(cfg_map: dict[str, ConfigRegras], nome: str) -> str:
    default = str(DEFAULTS[nome])
    row = cfg_map.get(_cfg_key(nome))
    if row is None:
        if nome == "no_documents_behavior":
            return _coerce_no_documents_behavior(default, default)
        return default
    raw = row.valor_texto if row.valor_texto is not None else row.valor_inteiro
    text = str(raw or default).strip() or default
    if nome == "no_documents_behavior":
        return _coerce_no_documents_behavior(text, str(DEFAULTS[nome]))
    if nome == "fallback_message":
        return text[:500] if text else default
    return text


def _global_cleiton_doc_limits() -> tuple[int, int]:
    global_cfg = get_cleiton_doc_config()
    return (
        max(0, int(global_cfg.prompt_context_max_chars)),
        max(0, int(global_cfg.prompt_max_files_considered)),
    )


def _apply_global_doc_limits(cfg: AgenteComparaConfig) -> AgenteComparaConfig:
    global_chars, global_docs = _global_cleiton_doc_limits()
    effective_chars = cfg.document_context_max_chars
    effective_docs = cfg.max_documents_considered

    if global_chars > 0:
        effective_chars = min(effective_chars, global_chars)
    if global_docs > 0:
        effective_docs = min(effective_docs, global_docs)

    if (
        effective_chars != cfg.document_context_max_chars
        or effective_docs != cfg.max_documents_considered
    ):
        logger.info(
            "Agente Compara audit config: limites documentais ajustados ao teto global do Cleiton "
            "(chars %s->%s, docs %s->%s).",
            cfg.document_context_max_chars,
            effective_chars,
            cfg.max_documents_considered,
            effective_docs,
        )
        return replace(
            cfg,
            document_context_max_chars=effective_chars,
            max_documents_considered=effective_docs,
        )
    return cfg


def resolve_audited_file_limits(
    cfg: AgenteComparaConfig | None = None,
    global_cfg: Any | None = None,
) -> dict[str, int | None | str]:
    audit_cfg = cfg or get_agente_compara_config()
    cleiton_cfg = global_cfg or get_cleiton_doc_config()

    global_max_bytes = max(0, int(cleiton_cfg.excel_max_bytes))
    global_max_rows = max(0, int(cleiton_cfg.excel_max_rows))
    specific_max_bytes = audit_cfg.audited_file_max_bytes
    specific_max_rows = max(0, int(audit_cfg.audited_file_max_rows))

    if specific_max_bytes is None:
        effective_max_bytes = global_max_bytes
        source = "global"
    else:
        effective_max_bytes = min(global_max_bytes, max(0, int(specific_max_bytes)))
        source = "specific_capped_by_global"

    return {
        "global_max_bytes": global_max_bytes,
        "specific_max_bytes": specific_max_bytes,
        "effective_max_bytes": effective_max_bytes,
        "effective_max_rows": min(global_max_rows, specific_max_rows),
        "source": source,
    }


def get_agente_compara_config() -> AgenteComparaConfig:
    if has_request_context():
        cached = getattr(g, "_agente_compara_cfg", None)
        if isinstance(cached, AgenteComparaConfig):
            return cached

    cfg_map = _load_cfg_map()
    cfg = AgenteComparaConfig(
        chat_enabled=_parse_bool(cfg_map, "chat_enabled"),
        upload_enabled=_parse_bool(cfg_map, "upload_enabled"),
        chat_max_history=_parse_int(cfg_map, "chat_max_history"),
        document_context_max_chars=_parse_int(cfg_map, "document_context_max_chars"),
        max_documents_considered=_parse_int(cfg_map, "max_documents_considered"),
        question_max_chars=_parse_int(cfg_map, "question_max_chars"),
        comparison_chat_question_max_chars=_parse_int(cfg_map, "comparison_chat_question_max_chars"),
        comparison_chat_history_max_items=_parse_int(cfg_map, "comparison_chat_history_max_items"),
        comparison_chat_context_max_chars=_parse_int(cfg_map, "comparison_chat_context_max_chars"),
        comparison_chat_max_rows=_parse_int(cfg_map, "comparison_chat_max_rows"),
        comparison_chat_max_memories=_parse_int(cfg_map, "comparison_chat_max_memories"),
        comparison_chat_max_table_rules=_parse_int(cfg_map, "comparison_chat_max_table_rules"),
        comparison_chat_max_ranked_items=_parse_int(cfg_map, "comparison_chat_max_ranked_items"),
        fallback_message=_parse_str(cfg_map, "fallback_message"),
        no_documents_behavior=_parse_str(cfg_map, "no_documents_behavior"),
        show_documents_used=_parse_bool(cfg_map, "show_documents_used"),
        no_hallucination_instruction_enabled=_parse_bool(
            cfg_map, "no_hallucination_instruction_enabled"
        ),
        audited_file_max_bytes=_parse_optional_int(cfg_map, "audited_file_max_bytes"),
        audited_file_max_rows=_parse_int(cfg_map, "audited_file_max_rows"),
        calculation_bases=_parse_calculation_bases(cfg_map),
    )
    cfg = _apply_global_doc_limits(cfg)
    if has_request_context():
        g._agente_compara_cfg = cfg
    return cfg


def _parse_bool_field(name: str, raw_values: dict[str, Any], cfg_atual: AgenteComparaConfig) -> bool:
    if name not in raw_values:
        return bool(getattr(cfg_atual, name))
    value = raw_values.get(name)
    if name in _BOOL_CHECKBOX_FIELDS:
        return _coerce_bool_checkbox(value)
    return _coerce_bool(value, bool(getattr(cfg_atual, name)))


def _parse_bounded_positive_int_strict(value: Any, field_name: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} deve ser informado.")
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} deve ser um inteiro positivo.") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} deve ser maior que zero.")
    bounded = _bounded(field_name, parsed)
    if bounded != parsed:
        raise ValueError(f"{field_name} fora da faixa permitida.")
    return bounded


def _parse_optional_bounded_positive_int_strict(value: Any, field_name: str) -> int | None:
    raw = "" if value is None else str(value).strip()
    if not raw:
        return None
    return _parse_bounded_positive_int_strict(raw, field_name)


def _validate_audited_file_max_bytes_against_global(value: int | None) -> None:
    if value is None:
        return
    global_cfg = get_cleiton_doc_config()
    global_max_bytes = max(0, int(global_cfg.excel_max_bytes))
    if value > global_max_bytes:
        raise ValueError(
            "O limite específico do arquivo auditado não pode ultrapassar o limite global "
            "de Excel definido em Cleiton."
        )


def parsear_agente_compara_config(raw_values: dict[str, Any]) -> AgenteComparaConfig:
    if not isinstance(raw_values, dict):
        raise ValueError("Campos de configuração da Agente Compara inválidos.")

    cfg_atual = get_agente_compara_config()

    def _raw(name: str) -> Any:
        if name in raw_values:
            return raw_values.get(name)
        return getattr(cfg_atual, name)

    fallback_raw = _raw("fallback_message")
    fallback = str(fallback_raw or DEFAULT_FALLBACK_MESSAGE).strip() or DEFAULT_FALLBACK_MESSAGE
    if len(fallback) > 500:
        raise ValueError("fallback_message excede o limite de 500 caracteres.")

    audited_file_max_bytes = _parse_optional_bounded_positive_int_strict(
        _raw("audited_file_max_bytes"),
        "audited_file_max_bytes",
    )
    _validate_audited_file_max_bytes_against_global(audited_file_max_bytes)

    parsed = AgenteComparaConfig(
        chat_enabled=_parse_bool_field("chat_enabled", raw_values, cfg_atual),
        upload_enabled=_parse_bool_field("upload_enabled", raw_values, cfg_atual),
        chat_max_history=_parse_bounded_positive_int_strict(_raw("chat_max_history"), "chat_max_history"),
        document_context_max_chars=_parse_bounded_positive_int_strict(
            _raw("document_context_max_chars"),
            "document_context_max_chars",
        ),
        max_documents_considered=_parse_bounded_positive_int_strict(
            _raw("max_documents_considered"),
            "max_documents_considered",
        ),
        question_max_chars=_parse_bounded_positive_int_strict(
            _raw("question_max_chars"),
            "question_max_chars",
        ),
        comparison_chat_question_max_chars=_parse_bounded_positive_int_strict(
            _raw("comparison_chat_question_max_chars")
            if "comparison_chat_question_max_chars" in raw_values
            else str(getattr(cfg_atual, "comparison_chat_question_max_chars", DEFAULTS["comparison_chat_question_max_chars"])),
            "comparison_chat_question_max_chars",
        ),
        comparison_chat_history_max_items=_parse_bounded_positive_int_strict(
            _raw("comparison_chat_history_max_items")
            if "comparison_chat_history_max_items" in raw_values
            else str(getattr(cfg_atual, "comparison_chat_history_max_items", DEFAULTS["comparison_chat_history_max_items"])),
            "comparison_chat_history_max_items",
        ),
        comparison_chat_context_max_chars=_parse_bounded_positive_int_strict(
            _raw("comparison_chat_context_max_chars")
            if "comparison_chat_context_max_chars" in raw_values
            else str(getattr(cfg_atual, "comparison_chat_context_max_chars", DEFAULTS["comparison_chat_context_max_chars"])),
            "comparison_chat_context_max_chars",
        ),
        comparison_chat_max_rows=_parse_bounded_positive_int_strict(
            _raw("comparison_chat_max_rows")
            if "comparison_chat_max_rows" in raw_values
            else str(getattr(cfg_atual, "comparison_chat_max_rows", DEFAULTS["comparison_chat_max_rows"])),
            "comparison_chat_max_rows",
        ),
        comparison_chat_max_memories=_parse_bounded_positive_int_strict(
            _raw("comparison_chat_max_memories")
            if "comparison_chat_max_memories" in raw_values
            else str(getattr(cfg_atual, "comparison_chat_max_memories", DEFAULTS["comparison_chat_max_memories"])),
            "comparison_chat_max_memories",
        ),
        comparison_chat_max_table_rules=_parse_bounded_positive_int_strict(
            _raw("comparison_chat_max_table_rules")
            if "comparison_chat_max_table_rules" in raw_values
            else str(getattr(cfg_atual, "comparison_chat_max_table_rules", DEFAULTS["comparison_chat_max_table_rules"])),
            "comparison_chat_max_table_rules",
        ),
        comparison_chat_max_ranked_items=_parse_bounded_positive_int_strict(
            _raw("comparison_chat_max_ranked_items")
            if "comparison_chat_max_ranked_items" in raw_values
            else str(getattr(cfg_atual, "comparison_chat_max_ranked_items", DEFAULTS["comparison_chat_max_ranked_items"])),
            "comparison_chat_max_ranked_items",
        ),
        fallback_message=fallback,
        no_documents_behavior=_coerce_no_documents_behavior(
            _raw("no_documents_behavior"),
            str(DEFAULTS["no_documents_behavior"]),
        ),
        show_documents_used=_parse_bool_field("show_documents_used", raw_values, cfg_atual),
        no_hallucination_instruction_enabled=_parse_bool_field(
            "no_hallucination_instruction_enabled",
            raw_values,
            cfg_atual,
        ),
        audited_file_max_bytes=audited_file_max_bytes,
        audited_file_max_rows=_parse_bounded_positive_int_strict(
            _raw("audited_file_max_rows"),
            "audited_file_max_rows",
        ),
        calculation_bases=validar_calculation_bases(_raw("calculation_bases")),
    )
    return _apply_global_doc_limits(parsed)


def _persistir_agente_compara_config_fields(
    parsed: AgenteComparaConfig,
    field_names: tuple[str, ...],
    *,
    commit: bool,
) -> None:
    for nome in field_names:
        if nome not in DEFAULTS:
            raise ValueError(f"Campo de configuração da Agente Compara inválido: {nome}.")
        row = ConfigRegras.query.filter_by(chave=_cfg_key(nome)).first()
        valor = getattr(parsed, nome)
        if valor is None:
            if row is not None:
                db.session.delete(row)
            continue
        if row is None:
            row = ConfigRegras(chave=_cfg_key(nome), descricao=DESCRICOES.get(nome))
        if isinstance(valor, bool):
            row.valor_inteiro = 1 if valor else 0
            row.valor_texto = None
        elif isinstance(valor, list):
            row.valor_texto = json.dumps(valor, ensure_ascii=False, indent=2)
            row.valor_inteiro = None
        elif isinstance(valor, str):
            row.valor_texto = valor
            row.valor_inteiro = None
        else:
            row.valor_inteiro = int(valor)
            row.valor_texto = None
        row.valor_real = None
        db.session.add(row)
    if commit:
        db.session.commit()


def persistir_agente_compara_config(parsed: AgenteComparaConfig, *, commit: bool = True) -> None:
    _persistir_agente_compara_config_fields(
        parsed,
        GENERAL_FORM_CONFIG_FIELDS,
        commit=commit,
    )


def salvar_agente_compara_config(raw_values: dict[str, Any]) -> AgenteComparaConfig:
    parsed = parsear_agente_compara_config(raw_values)
    persistir_agente_compara_config(parsed, commit=True)
    if has_request_context():
        g._agente_compara_cfg = parsed
    return parsed


def carregar_agente_compara_calculation_bases() -> list[dict[str, Any]]:
    return get_agente_compara_config().calculation_bases


def salvar_agente_compara_calculation_bases(raw_bases: Any) -> list[dict[str, Any]]:
    bases = validar_calculation_bases(raw_bases)
    cfg_atual = get_agente_compara_config()
    parsed = replace(cfg_atual, calculation_bases=bases)
    _persistir_agente_compara_config_fields(
        parsed,
        CALCULATION_BASES_FORM_CONFIG_FIELDS,
        commit=True,
    )
    if has_request_context():
        g._agente_compara_cfg = parsed
    return bases


def salvar_agente_compara_calculation_bases_json(raw_json: str) -> list[dict[str, Any]]:
    bases = parsear_calculation_bases_json(raw_json)
    return salvar_agente_compara_calculation_bases(bases)
