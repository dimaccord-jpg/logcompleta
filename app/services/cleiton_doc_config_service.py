"""
Configuração documental do Cleiton (persistência em ConfigRegras).

Objetivo:
- centralizar limites administrativos do MVP documental da Júlia;
- manter defaults seguros sem hardcode espalhado;
- isolar o bloco documental de CleitonCostConfig.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

from flask import g, has_request_context

from app.extensions import db
from app.models import ConfigRegras

logger = logging.getLogger(__name__)

_BOOL_CHECKBOX_FIELDS = frozenset(
    {
        "upload_enabled",
        "cleanup_enabled",
        "pdf_enabled",
        "excel_enabled",
        "docx_enabled",
        "txt_enabled",
        "xml_enabled",
        "csv_enabled",
    }
)

_CFG_PREFIX = "cleiton_doc_"

DEFAULTS: dict[str, int] = {
    "upload_enabled": 1,
    "max_files_per_session": 5,
    "session_max_bytes": 15 * 1024 * 1024,
    "upload_ttl_hours": 48,
    "cleanup_enabled": 1,
    "prompt_context_max_chars": 24000,
    "prompt_max_files_considered": 3,
    "pdf_enabled": 1,
    "pdf_max_bytes": 5 * 1024 * 1024,
    "pdf_max_pages": 50,
    "pdf_max_chars": 120000,
    "excel_enabled": 1,
    "excel_max_bytes": 5 * 1024 * 1024,
    "excel_max_rows": 5000,
    "excel_max_columns": 80,
    "excel_max_chars": 120000,
    "docx_enabled": 1,
    "docx_max_bytes": 5 * 1024 * 1024,
    "docx_max_paragraphs": 5000,
    "docx_max_chars": 120000,
    "txt_enabled": 1,
    "txt_max_bytes": 1 * 1024 * 1024,
    "txt_max_chars": 120000,
    "xml_enabled": 1,
    "xml_max_bytes": 2 * 1024 * 1024,
    "xml_max_nodes": 20000,
    "xml_max_depth": 20,
    "xml_max_chars": 120000,
    "csv_enabled": 1,
    "csv_max_bytes": 2 * 1024 * 1024,
    "csv_max_rows": 10000,
    "csv_max_columns": 80,
    "csv_max_chars": 120000,
}

DESCRICOES: dict[str, str] = {
    "upload_enabled": "Habilita upload documental governado por Cleiton.",
    "max_files_per_session": "Máximo de arquivos documentais por sessão.",
    "session_max_bytes": "Tamanho total máximo de arquivos por sessão (bytes).",
    "upload_ttl_hours": "Tempo de vida do contexto documental temporário (horas).",
    "cleanup_enabled": "Habilita limpeza automática do contexto documental.",
    "prompt_context_max_chars": "Máximo de caracteres documentais preparados para o prompt.",
    "prompt_max_files_considered": "Máximo de arquivos considerados por resposta.",
    "pdf_enabled": "Habilita PDF.",
    "pdf_max_bytes": "Tamanho máximo de PDF (bytes).",
    "pdf_max_pages": "Máximo de páginas de PDF.",
    "pdf_max_chars": "Máximo de caracteres extraídos de PDF.",
    "excel_enabled": "Habilita Excel/XLSX.",
    "excel_max_bytes": "Tamanho máximo de Excel/XLSX (bytes).",
    "excel_max_rows": "Máximo de linhas de Excel/XLSX.",
    "excel_max_columns": "Máximo de colunas de Excel/XLSX.",
    "excel_max_chars": "Máximo de caracteres extraídos de Excel/XLSX.",
    "docx_enabled": "Habilita DOCX.",
    "docx_max_bytes": "Tamanho máximo de DOCX (bytes).",
    "docx_max_paragraphs": "Máximo de parágrafos de DOCX.",
    "docx_max_chars": "Máximo de caracteres extraídos de DOCX.",
    "txt_enabled": "Habilita TXT.",
    "txt_max_bytes": "Tamanho máximo de TXT (bytes).",
    "txt_max_chars": "Máximo de caracteres extraídos de TXT.",
    "xml_enabled": "Habilita XML.",
    "xml_max_bytes": "Tamanho máximo de XML (bytes).",
    "xml_max_nodes": "Máximo de nós de XML.",
    "xml_max_depth": "Profundidade máxima de XML.",
    "xml_max_chars": "Máximo de caracteres extraídos de XML.",
    "csv_enabled": "Habilita CSV.",
    "csv_max_bytes": "Tamanho máximo de CSV (bytes).",
    "csv_max_rows": "Máximo de linhas de CSV.",
    "csv_max_columns": "Máximo de colunas de CSV.",
    "csv_max_chars": "Máximo de caracteres extraídos de CSV.",
}


@dataclass(frozen=True)
class CleitonDocConfig:
    upload_enabled: bool
    max_files_per_session: int
    session_max_bytes: int
    upload_ttl_hours: int
    cleanup_enabled: bool
    prompt_context_max_chars: int
    prompt_max_files_considered: int
    pdf_enabled: bool
    pdf_max_bytes: int
    pdf_max_pages: int
    pdf_max_chars: int
    excel_enabled: bool
    excel_max_bytes: int
    excel_max_rows: int
    excel_max_columns: int
    excel_max_chars: int
    docx_enabled: bool
    docx_max_bytes: int
    docx_max_paragraphs: int
    docx_max_chars: int
    txt_enabled: bool
    txt_max_bytes: int
    txt_max_chars: int
    xml_enabled: bool
    xml_max_bytes: int
    xml_max_nodes: int
    xml_max_depth: int
    xml_max_chars: int
    csv_enabled: bool
    csv_max_bytes: int
    csv_max_rows: int
    csv_max_columns: int
    csv_max_chars: int


def _cfg_key(nome: str) -> str:
    return f"{_CFG_PREFIX}{nome}"


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _parse_positive_int_strict(value: Any, field_name: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError(f"{field_name} deve ser informado.")
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} deve ser um inteiro positivo.") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} deve ser maior que zero.")
    return parsed


def _parse_bounded_positive_int_strict(value: Any, field_name: str) -> int:
    parsed = _parse_positive_int_strict(value, field_name)
    bounded = _bounded(field_name, parsed)
    if bounded != parsed:
        raise ValueError(f"{field_name} fora da faixa permitida.")
    return bounded


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "on", "yes", "sim"}:
        return True
    if text in {"0", "false", "off", "no", "nao", "não"}:
        return False
    return default


def _coerce_bool_checkbox(value: Any) -> bool:
    """Checkbox HTML ausente ou sem valor explícito equivale a desligado."""
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return _coerce_bool(value, False)


def _bounded(nome: str, valor: int) -> int:
    if nome == "max_files_per_session":
        return min(max(1, valor), 20)
    if nome == "session_max_bytes":
        return min(max(256 * 1024, valor), 200 * 1024 * 1024)
    if nome == "upload_ttl_hours":
        return min(max(1, valor), 168)
    if nome == "prompt_context_max_chars":
        return min(max(2000, valor), 200000)
    if nome == "prompt_max_files_considered":
        return min(max(1, valor), 10)
    if nome.endswith("_max_bytes"):
        return min(max(32 * 1024, valor), 200 * 1024 * 1024)
    if nome.endswith("_max_chars"):
        return min(max(1000, valor), 500000)
    if nome == "pdf_max_pages":
        return min(max(1, valor), 1000)
    if nome in {"excel_max_rows", "csv_max_rows"}:
        return min(max(1, valor), 200000)
    if nome in {"excel_max_columns", "csv_max_columns"}:
        return min(max(1, valor), 1000)
    if nome == "docx_max_paragraphs":
        return min(max(1, valor), 200000)
    if nome == "xml_max_nodes":
        return min(max(1, valor), 500000)
    if nome == "xml_max_depth":
        return min(max(1, valor), 100)
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
        return default
    raw = row.valor_inteiro if row.valor_inteiro is not None else row.valor_texto
    return _bounded(nome, _coerce_positive_int(raw, default))


def _validar_relacoes(cfg: CleitonDocConfig) -> None:
    if cfg.prompt_max_files_considered > cfg.max_files_per_session:
        raise ValueError(
            "Configuração inválida: prompt_max_files_considered não pode ser maior que max_files_per_session."
        )


def _corrigir_relacoes_persistidas(cfg: CleitonDocConfig) -> CleitonDocConfig:
    if cfg.prompt_max_files_considered <= cfg.max_files_per_session:
        return cfg
    logger.warning(
        "Cleiton doc config: relação inválida no banco "
        "(prompt_max_files_considered=%s > max_files_per_session=%s); "
        "aplicando clamp explícito para %s.",
        cfg.prompt_max_files_considered,
        cfg.max_files_per_session,
        cfg.max_files_per_session,
    )
    return replace(cfg, prompt_max_files_considered=cfg.max_files_per_session)


def get_cleiton_doc_config() -> CleitonDocConfig:
    if has_request_context():
        cached = getattr(g, "_cleiton_doc_cfg", None)
        if isinstance(cached, CleitonDocConfig):
            return cached

    cfg_map = _load_cfg_map()
    cfg = CleitonDocConfig(
        upload_enabled=_parse_bool(cfg_map, "upload_enabled"),
        max_files_per_session=_parse_int(cfg_map, "max_files_per_session"),
        session_max_bytes=_parse_int(cfg_map, "session_max_bytes"),
        upload_ttl_hours=_parse_int(cfg_map, "upload_ttl_hours"),
        cleanup_enabled=_parse_bool(cfg_map, "cleanup_enabled"),
        prompt_context_max_chars=_parse_int(cfg_map, "prompt_context_max_chars"),
        prompt_max_files_considered=_parse_int(cfg_map, "prompt_max_files_considered"),
        pdf_enabled=_parse_bool(cfg_map, "pdf_enabled"),
        pdf_max_bytes=_parse_int(cfg_map, "pdf_max_bytes"),
        pdf_max_pages=_parse_int(cfg_map, "pdf_max_pages"),
        pdf_max_chars=_parse_int(cfg_map, "pdf_max_chars"),
        excel_enabled=_parse_bool(cfg_map, "excel_enabled"),
        excel_max_bytes=_parse_int(cfg_map, "excel_max_bytes"),
        excel_max_rows=_parse_int(cfg_map, "excel_max_rows"),
        excel_max_columns=_parse_int(cfg_map, "excel_max_columns"),
        excel_max_chars=_parse_int(cfg_map, "excel_max_chars"),
        docx_enabled=_parse_bool(cfg_map, "docx_enabled"),
        docx_max_bytes=_parse_int(cfg_map, "docx_max_bytes"),
        docx_max_paragraphs=_parse_int(cfg_map, "docx_max_paragraphs"),
        docx_max_chars=_parse_int(cfg_map, "docx_max_chars"),
        txt_enabled=_parse_bool(cfg_map, "txt_enabled"),
        txt_max_bytes=_parse_int(cfg_map, "txt_max_bytes"),
        txt_max_chars=_parse_int(cfg_map, "txt_max_chars"),
        xml_enabled=_parse_bool(cfg_map, "xml_enabled"),
        xml_max_bytes=_parse_int(cfg_map, "xml_max_bytes"),
        xml_max_nodes=_parse_int(cfg_map, "xml_max_nodes"),
        xml_max_depth=_parse_int(cfg_map, "xml_max_depth"),
        xml_max_chars=_parse_int(cfg_map, "xml_max_chars"),
        csv_enabled=_parse_bool(cfg_map, "csv_enabled"),
        csv_max_bytes=_parse_int(cfg_map, "csv_max_bytes"),
        csv_max_rows=_parse_int(cfg_map, "csv_max_rows"),
        csv_max_columns=_parse_int(cfg_map, "csv_max_columns"),
        csv_max_chars=_parse_int(cfg_map, "csv_max_chars"),
    )
    cfg = _corrigir_relacoes_persistidas(cfg)
    if has_request_context():
        g._cleiton_doc_cfg = cfg
    return cfg


def _parse_bool_field(name: str, raw_values: dict[str, Any], cfg_atual: CleitonDocConfig) -> bool:
    if name not in raw_values:
        return bool(getattr(cfg_atual, name))
    value = raw_values.get(name)
    if name in _BOOL_CHECKBOX_FIELDS:
        return _coerce_bool_checkbox(value)
    return _coerce_bool(value, bool(getattr(cfg_atual, name)))


def parsear_cleiton_doc_config(raw_values: dict[str, Any]) -> CleitonDocConfig:
    if not isinstance(raw_values, dict):
        raise ValueError("Campos de configuração documental inválidos.")

    cfg_atual = get_cleiton_doc_config()

    def _raw(name: str) -> Any:
        if name in raw_values:
            return raw_values.get(name)
        return getattr(cfg_atual, name)

    parsed = CleitonDocConfig(
        upload_enabled=_parse_bool_field("upload_enabled", raw_values, cfg_atual),
        max_files_per_session=_parse_bounded_positive_int_strict(_raw("max_files_per_session"), "max_files_per_session"),
        session_max_bytes=_parse_bounded_positive_int_strict(_raw("session_max_bytes"), "session_max_bytes"),
        upload_ttl_hours=_parse_bounded_positive_int_strict(_raw("upload_ttl_hours"), "upload_ttl_hours"),
        cleanup_enabled=_parse_bool_field("cleanup_enabled", raw_values, cfg_atual),
        prompt_context_max_chars=_parse_bounded_positive_int_strict(_raw("prompt_context_max_chars"), "prompt_context_max_chars"),
        prompt_max_files_considered=_parse_bounded_positive_int_strict(_raw("prompt_max_files_considered"), "prompt_max_files_considered"),
        pdf_enabled=_parse_bool_field("pdf_enabled", raw_values, cfg_atual),
        pdf_max_bytes=_parse_bounded_positive_int_strict(_raw("pdf_max_bytes"), "pdf_max_bytes"),
        pdf_max_pages=_parse_bounded_positive_int_strict(_raw("pdf_max_pages"), "pdf_max_pages"),
        pdf_max_chars=_parse_bounded_positive_int_strict(_raw("pdf_max_chars"), "pdf_max_chars"),
        excel_enabled=_parse_bool_field("excel_enabled", raw_values, cfg_atual),
        excel_max_bytes=_parse_bounded_positive_int_strict(_raw("excel_max_bytes"), "excel_max_bytes"),
        excel_max_rows=_parse_bounded_positive_int_strict(_raw("excel_max_rows"), "excel_max_rows"),
        excel_max_columns=_parse_bounded_positive_int_strict(_raw("excel_max_columns"), "excel_max_columns"),
        excel_max_chars=_parse_bounded_positive_int_strict(_raw("excel_max_chars"), "excel_max_chars"),
        docx_enabled=_parse_bool_field("docx_enabled", raw_values, cfg_atual),
        docx_max_bytes=_parse_bounded_positive_int_strict(_raw("docx_max_bytes"), "docx_max_bytes"),
        docx_max_paragraphs=_parse_bounded_positive_int_strict(_raw("docx_max_paragraphs"), "docx_max_paragraphs"),
        docx_max_chars=_parse_bounded_positive_int_strict(_raw("docx_max_chars"), "docx_max_chars"),
        txt_enabled=_parse_bool_field("txt_enabled", raw_values, cfg_atual),
        txt_max_bytes=_parse_bounded_positive_int_strict(_raw("txt_max_bytes"), "txt_max_bytes"),
        txt_max_chars=_parse_bounded_positive_int_strict(_raw("txt_max_chars"), "txt_max_chars"),
        xml_enabled=_parse_bool_field("xml_enabled", raw_values, cfg_atual),
        xml_max_bytes=_parse_bounded_positive_int_strict(_raw("xml_max_bytes"), "xml_max_bytes"),
        xml_max_nodes=_parse_bounded_positive_int_strict(_raw("xml_max_nodes"), "xml_max_nodes"),
        xml_max_depth=_parse_bounded_positive_int_strict(_raw("xml_max_depth"), "xml_max_depth"),
        xml_max_chars=_parse_bounded_positive_int_strict(_raw("xml_max_chars"), "xml_max_chars"),
        csv_enabled=_parse_bool_field("csv_enabled", raw_values, cfg_atual),
        csv_max_bytes=_parse_bounded_positive_int_strict(_raw("csv_max_bytes"), "csv_max_bytes"),
        csv_max_rows=_parse_bounded_positive_int_strict(_raw("csv_max_rows"), "csv_max_rows"),
        csv_max_columns=_parse_bounded_positive_int_strict(_raw("csv_max_columns"), "csv_max_columns"),
        csv_max_chars=_parse_bounded_positive_int_strict(_raw("csv_max_chars"), "csv_max_chars"),
    )
    _validar_relacoes(parsed)
    return parsed


def persistir_cleiton_doc_config(parsed: CleitonDocConfig, *, commit: bool = True) -> None:
    for nome in DEFAULTS.keys():
        row = ConfigRegras.query.filter_by(chave=_cfg_key(nome)).first()
        if row is None:
            row = ConfigRegras(chave=_cfg_key(nome), descricao=DESCRICOES.get(nome))
        valor = getattr(parsed, nome)
        row.valor_inteiro = 1 if isinstance(valor, bool) and valor else 0 if isinstance(valor, bool) else int(valor)
        row.valor_texto = None
        row.valor_real = None
        db.session.add(row)
    if commit:
        db.session.commit()


def salvar_cleiton_doc_config(raw_values: dict[str, Any]) -> CleitonDocConfig:
    parsed = parsear_cleiton_doc_config(raw_values)
    persistir_cleiton_doc_config(parsed, commit=True)
    if has_request_context():
        g._cleiton_doc_cfg = parsed
    return parsed


def salvar_agentes_cleiton_config(
    *,
    cost_kwargs: dict[str, Any],
    doc_campos: dict[str, Any] | None,
) -> CleitonDocConfig | None:
    """
    Persiste bloco de custo e bloco documental em uma única transação.
    Valida o documental antes de gravar qualquer alteração.
    """
    from app.services.cleiton_cost_service import save_config

    parsed_doc = parsear_cleiton_doc_config(doc_campos) if doc_campos is not None else None
    try:
        save_config(**cost_kwargs, commit=False)
        if parsed_doc is not None:
            persistir_cleiton_doc_config(parsed_doc, commit=False)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    if parsed_doc is not None and has_request_context():
        g._cleiton_doc_cfg = parsed_doc
    return parsed_doc
