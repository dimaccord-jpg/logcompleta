"""
Configuração operacional da Cleide Auditoria documental (persistência em ConfigRegras).

Fase 1:
- defaults seguros e prefixo próprio (`cleide_audit_cfg_`);
- isolamento do bloco Cleide BI (`cleide_cfg_`) e dos controles globais do Cleiton;
- limites documentais respeitam tetos globais do Cleiton na leitura efetiva.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from flask import g, has_request_context

from app.extensions import db
from app.models import ConfigRegras
from app.services.cleiton_doc_config_service import get_cleiton_doc_config

logger = logging.getLogger(__name__)

_CFG_PREFIX = "cleide_audit_cfg_"

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
    "Não foi possível obter resposta da Cleide no momento. Tente novamente em instantes."
)

DEFAULT_AUDITED_FILE_MAX_ROWS = 2000

DEFAULTS: dict[str, int | str] = {
    "chat_enabled": 1,
    "upload_enabled": 1,
    "chat_max_history": 10,
    "document_context_max_chars": 24000,
    "max_documents_considered": 3,
    "question_max_chars": 4000,
    "fallback_message": DEFAULT_FALLBACK_MESSAGE,
    "no_documents_behavior": "allow_guided",
    "show_documents_used": 1,
    "no_hallucination_instruction_enabled": 1,
    "audited_file_max_rows": DEFAULT_AUDITED_FILE_MAX_ROWS,
}

DESCRICOES: dict[str, str] = {
    "chat_enabled": "Habilita o chat IA da Cleide Auditoria em /auditoria-frete.",
    "upload_enabled": "Habilita upload documental da Cleide Auditoria.",
    "chat_max_history": "Janela de histórico (mensagens) enviada ao backend da Cleide Auditoria.",
    "document_context_max_chars": "Máximo de caracteres do contexto documental no prompt da Cleide Auditoria.",
    "max_documents_considered": "Máximo de documentos considerados por resposta da Cleide Auditoria.",
    "question_max_chars": "Máximo de caracteres aceitos por pergunta no chat da Cleide Auditoria.",
    "fallback_message": "Mensagem amigável exibida em falha de IA da Cleide Auditoria (não é resposta normal).",
    "no_documents_behavior": "Comportamento sem documentos: allow_guided ou require_documents.",
    "show_documents_used": "Exibe metadados dos documentos usados na resposta ao usuário.",
    "no_hallucination_instruction_enabled": (
        "Reforça instrução anti-alucinação no prompt da Cleide Auditoria."
    ),
    "audited_file_max_rows": (
        "Define o limite de linhas aceitas no arquivo enviado para auditoria de frete."
    ),
}


@dataclass(frozen=True)
class CleideAuditConfig:
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
    audited_file_max_rows: int


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


def _apply_global_doc_limits(cfg: CleideAuditConfig) -> CleideAuditConfig:
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
            "Cleide audit config: limites documentais ajustados ao teto global do Cleiton "
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


def get_cleide_audit_config() -> CleideAuditConfig:
    if has_request_context():
        cached = getattr(g, "_cleide_audit_cfg", None)
        if isinstance(cached, CleideAuditConfig):
            return cached

    cfg_map = _load_cfg_map()
    cfg = CleideAuditConfig(
        chat_enabled=_parse_bool(cfg_map, "chat_enabled"),
        upload_enabled=_parse_bool(cfg_map, "upload_enabled"),
        chat_max_history=_parse_int(cfg_map, "chat_max_history"),
        document_context_max_chars=_parse_int(cfg_map, "document_context_max_chars"),
        max_documents_considered=_parse_int(cfg_map, "max_documents_considered"),
        question_max_chars=_parse_int(cfg_map, "question_max_chars"),
        fallback_message=_parse_str(cfg_map, "fallback_message"),
        no_documents_behavior=_parse_str(cfg_map, "no_documents_behavior"),
        show_documents_used=_parse_bool(cfg_map, "show_documents_used"),
        no_hallucination_instruction_enabled=_parse_bool(
            cfg_map, "no_hallucination_instruction_enabled"
        ),
        audited_file_max_rows=_parse_int(cfg_map, "audited_file_max_rows"),
    )
    cfg = _apply_global_doc_limits(cfg)
    if has_request_context():
        g._cleide_audit_cfg = cfg
    return cfg


def _parse_bool_field(name: str, raw_values: dict[str, Any], cfg_atual: CleideAuditConfig) -> bool:
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


def parsear_cleide_audit_config(raw_values: dict[str, Any]) -> CleideAuditConfig:
    if not isinstance(raw_values, dict):
        raise ValueError("Campos de configuração da Cleide Auditoria inválidos.")

    cfg_atual = get_cleide_audit_config()

    def _raw(name: str) -> Any:
        if name in raw_values:
            return raw_values.get(name)
        return getattr(cfg_atual, name)

    fallback_raw = _raw("fallback_message")
    fallback = str(fallback_raw or DEFAULT_FALLBACK_MESSAGE).strip() or DEFAULT_FALLBACK_MESSAGE
    if len(fallback) > 500:
        raise ValueError("fallback_message excede o limite de 500 caracteres.")

    parsed = CleideAuditConfig(
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
        audited_file_max_rows=_parse_bounded_positive_int_strict(
            _raw("audited_file_max_rows"),
            "audited_file_max_rows",
        ),
    )
    return _apply_global_doc_limits(parsed)


def persistir_cleide_audit_config(parsed: CleideAuditConfig, *, commit: bool = True) -> None:
    for nome in DEFAULTS.keys():
        row = ConfigRegras.query.filter_by(chave=_cfg_key(nome)).first()
        if row is None:
            row = ConfigRegras(chave=_cfg_key(nome), descricao=DESCRICOES.get(nome))
        valor = getattr(parsed, nome)
        if isinstance(valor, bool):
            row.valor_inteiro = 1 if valor else 0
            row.valor_texto = None
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


def salvar_cleide_audit_config(raw_values: dict[str, Any]) -> CleideAuditConfig:
    parsed = parsear_cleide_audit_config(raw_values)
    persistir_cleide_audit_config(parsed, commit=True)
    if has_request_context():
        g._cleide_audit_cfg = parsed
    return parsed
