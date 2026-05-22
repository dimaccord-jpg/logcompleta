"""
Configuracao operacional da Cleide (persistencia em ConfigRegras).

Fase 1:
- defaults seguros;
- prefixo proprio para isolamento de dominio;
- cache request-local dedicado.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import g, has_request_context
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.extensions import db
from app.models import ConfigRegras
import logging

_CFG_PREFIX = "cleide_cfg_"

DEFAULTS: dict[str, int | str] = {
    "upload_total_max": 10000,
    "upload_ttl_minutes": 30,
    "upload_max_file_size_bytes": 8 * 1024 * 1024,
    "chat_max_history": 10,
    "csv_delimiter_default": ",",
    "layout_version": 1,
    "wordcloud_min_term_freq": 2,
    "structural_max_rows": 50000,
    "structural_max_columns": 120,
    "analytics_max_rows": 50000,
    "analytics_group_limit": 25,
    "chat_context_max_items_per_table": 10,
    "chat_context_max_text_len": 80,
    "chat_context_rankings_limit": 12,
    "chat_context_include_transportadora": 1,
    "chat_context_include_uf_origem": 1,
    "chat_context_include_uf_destino": 1,
    "chat_context_include_temporal": 1,
    "chat_context_include_paretos": 1,
    "chat_context_mode": "executivo",
    "chat_context_max_chars": 6000,
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CleideConfig:
    upload_total_max: int
    upload_ttl_minutes: int
    upload_max_file_size_bytes: int
    chat_max_history: int
    csv_delimiter_default: str
    layout_version: int
    wordcloud_min_term_freq: int
    structural_max_rows: int
    structural_max_columns: int
    analytics_max_rows: int
    analytics_group_limit: int
    chat_context_max_items_per_table: int
    chat_context_max_text_len: int
    chat_context_rankings_limit: int
    chat_context_include_transportadora: int
    chat_context_include_uf_origem: int
    chat_context_include_uf_destino: int
    chat_context_include_temporal: int
    chat_context_include_paretos: int
    chat_context_mode: str
    chat_context_max_chars: int


def _coerce_mode(value: Any, default: str) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in {"executivo", "conservador"}:
        return candidate
    return default


def _cfg_key(nome: str) -> str:
    return f"{_CFG_PREFIX}{nome}"


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _coerce_non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def _coerce_delimiter(value: Any, default: str) -> str:
    candidate = str(value or "").strip()
    if candidate in {",", ";", "\t", "|"}:
        return candidate
    return default


def _load_cfg_map() -> dict[str, ConfigRegras]:
    keys = [_cfg_key(nome) for nome in DEFAULTS.keys()]
    try:
        rows = ConfigRegras.query.filter(ConfigRegras.chave.in_(keys)).all()
    except (OperationalError, SQLAlchemyError, UnicodeDecodeError) as exc:
        if _allow_defaults_without_db():
            logger.warning(
                "Cleide config fallback ativado por contexto seguro de teste: %s",
                exc.__class__.__name__,
            )
            return {}
        raise
    return {row.chave: row for row in rows}


def _allow_defaults_without_db() -> bool:
    if not has_request_context():
        return False
    try:
        return bool(getattr(g, "cleide_allow_config_fallback", False))
    except RuntimeError:
        return False


def _parse_int(cfg_map: dict[str, ConfigRegras], nome: str, *, allow_zero: bool = False) -> int:
    default = int(DEFAULTS[nome])
    row = cfg_map.get(_cfg_key(nome))
    if row is None:
        return default
    raw = row.valor_inteiro if row.valor_inteiro is not None else row.valor_texto
    if allow_zero:
        return _coerce_non_negative_int(raw, default)
    return _coerce_positive_int(raw, default)


def _parse_str(cfg_map: dict[str, ConfigRegras], nome: str) -> str:
    default = str(DEFAULTS[nome])
    row = cfg_map.get(_cfg_key(nome))
    if row is None:
        return default
    raw = row.valor_texto if row.valor_texto is not None else row.valor_inteiro
    if nome == "csv_delimiter_default":
        return _coerce_delimiter(raw, default)
    if nome == "chat_context_mode":
        return _coerce_mode(raw, default)
    return str(raw or default).strip() or default


def get_cleide_config() -> CleideConfig:
    if has_request_context():
        cached = getattr(g, "_cleide_cfg", None)
        if isinstance(cached, CleideConfig):
            return cached

    cfg_map = _load_cfg_map()
    cfg = CleideConfig(
        upload_total_max=min(max(100, _parse_int(cfg_map, "upload_total_max")), 200000),
        upload_ttl_minutes=min(max(5, _parse_int(cfg_map, "upload_ttl_minutes")), 240),
        upload_max_file_size_bytes=min(
            max(1 * 1024 * 1024, _parse_int(cfg_map, "upload_max_file_size_bytes")),
            64 * 1024 * 1024,
        ),
        chat_max_history=min(max(1, _parse_int(cfg_map, "chat_max_history")), 100),
        csv_delimiter_default=_parse_str(cfg_map, "csv_delimiter_default"),
        layout_version=min(max(1, _parse_int(cfg_map, "layout_version")), 10),
        wordcloud_min_term_freq=min(max(1, _parse_int(cfg_map, "wordcloud_min_term_freq")), 50),
        structural_max_rows=min(max(1000, _parse_int(cfg_map, "structural_max_rows")), 200000),
        structural_max_columns=min(max(10, _parse_int(cfg_map, "structural_max_columns")), 500),
        analytics_max_rows=min(max(1000, _parse_int(cfg_map, "analytics_max_rows")), 200000),
        analytics_group_limit=min(max(5, _parse_int(cfg_map, "analytics_group_limit")), 200),
        chat_context_max_items_per_table=min(max(3, _parse_int(cfg_map, "chat_context_max_items_per_table")), 30),
        chat_context_max_text_len=min(max(32, _parse_int(cfg_map, "chat_context_max_text_len")), 200),
        chat_context_rankings_limit=min(max(3, _parse_int(cfg_map, "chat_context_rankings_limit")), 25),
        chat_context_include_transportadora=1 if _parse_int(cfg_map, "chat_context_include_transportadora", allow_zero=True) > 0 else 0,
        chat_context_include_uf_origem=1 if _parse_int(cfg_map, "chat_context_include_uf_origem", allow_zero=True) > 0 else 0,
        chat_context_include_uf_destino=1 if _parse_int(cfg_map, "chat_context_include_uf_destino", allow_zero=True) > 0 else 0,
        chat_context_include_temporal=1 if _parse_int(cfg_map, "chat_context_include_temporal", allow_zero=True) > 0 else 0,
        chat_context_include_paretos=1 if _parse_int(cfg_map, "chat_context_include_paretos", allow_zero=True) > 0 else 0,
        chat_context_mode=_parse_str(cfg_map, "chat_context_mode"),
        chat_context_max_chars=min(max(1500, _parse_int(cfg_map, "chat_context_max_chars")), 12000),
    )
    if has_request_context():
        g._cleide_cfg = cfg
    return cfg


def salvar_cleide_config(campos: dict[str, Any]) -> None:
    if not isinstance(campos, dict):
        raise ValueError("Campos de configuração inválidos.")

    cfg_atual = get_cleide_config()

    def _raw(name: str) -> Any:
        value = campos.get(name, getattr(cfg_atual, name))
        if isinstance(value, str):
            return value.strip()
        return value

    parsed = CleideConfig(
        upload_total_max=min(max(100, _coerce_positive_int(_raw("upload_total_max"), cfg_atual.upload_total_max)), 200000),
        upload_ttl_minutes=min(max(5, _coerce_positive_int(_raw("upload_ttl_minutes"), cfg_atual.upload_ttl_minutes)), 240),
        upload_max_file_size_bytes=min(
            max(1 * 1024 * 1024, _coerce_positive_int(_raw("upload_max_file_size_bytes"), cfg_atual.upload_max_file_size_bytes)),
            64 * 1024 * 1024,
        ),
        chat_max_history=min(max(1, _coerce_positive_int(_raw("chat_max_history"), cfg_atual.chat_max_history)), 100),
        csv_delimiter_default=_coerce_delimiter(_raw("csv_delimiter_default"), cfg_atual.csv_delimiter_default),
        layout_version=min(max(1, _coerce_positive_int(_raw("layout_version"), cfg_atual.layout_version)), 10),
        wordcloud_min_term_freq=min(
            max(1, _coerce_positive_int(_raw("wordcloud_min_term_freq"), cfg_atual.wordcloud_min_term_freq)),
            50,
        ),
        structural_max_rows=min(max(1000, _coerce_positive_int(_raw("structural_max_rows"), cfg_atual.structural_max_rows)), 200000),
        structural_max_columns=min(
            max(10, _coerce_positive_int(_raw("structural_max_columns"), cfg_atual.structural_max_columns)),
            500,
        ),
        analytics_max_rows=min(max(1000, _coerce_positive_int(_raw("analytics_max_rows"), cfg_atual.analytics_max_rows)), 200000),
        analytics_group_limit=min(
            max(5, _coerce_positive_int(_raw("analytics_group_limit"), cfg_atual.analytics_group_limit)),
            200,
        ),
        chat_context_max_items_per_table=min(
            max(3, _coerce_positive_int(_raw("chat_context_max_items_per_table"), cfg_atual.chat_context_max_items_per_table)),
            30,
        ),
        chat_context_max_text_len=min(
            max(32, _coerce_positive_int(_raw("chat_context_max_text_len"), cfg_atual.chat_context_max_text_len)),
            200,
        ),
        chat_context_rankings_limit=min(
            max(3, _coerce_positive_int(_raw("chat_context_rankings_limit"), cfg_atual.chat_context_rankings_limit)),
            25,
        ),
        chat_context_include_transportadora=1 if _coerce_non_negative_int(_raw("chat_context_include_transportadora"), cfg_atual.chat_context_include_transportadora) > 0 else 0,
        chat_context_include_uf_origem=1 if _coerce_non_negative_int(_raw("chat_context_include_uf_origem"), cfg_atual.chat_context_include_uf_origem) > 0 else 0,
        chat_context_include_uf_destino=1 if _coerce_non_negative_int(_raw("chat_context_include_uf_destino"), cfg_atual.chat_context_include_uf_destino) > 0 else 0,
        chat_context_include_temporal=1 if _coerce_non_negative_int(_raw("chat_context_include_temporal"), cfg_atual.chat_context_include_temporal) > 0 else 0,
        chat_context_include_paretos=1 if _coerce_non_negative_int(_raw("chat_context_include_paretos"), cfg_atual.chat_context_include_paretos) > 0 else 0,
        chat_context_mode=_coerce_mode(_raw("chat_context_mode"), cfg_atual.chat_context_mode),
        chat_context_max_chars=min(
            max(1500, _coerce_positive_int(_raw("chat_context_max_chars"), cfg_atual.chat_context_max_chars)),
            12000,
        ),
    )

    for nome in DEFAULTS.keys():
        chave = _cfg_key(nome)
        atual = ConfigRegras.query.filter_by(chave=chave).first()
        valor = getattr(parsed, nome)
        if isinstance(valor, str):
            if atual is None:
                atual = ConfigRegras(chave=chave)
            atual.valor_texto = valor
            atual.valor_inteiro = None
            atual.valor_real = None
            db.session.add(atual)
            continue
        if atual is None:
            atual = ConfigRegras(chave=chave)
        atual.valor_inteiro = int(valor)
        atual.valor_texto = None
        atual.valor_real = None
        db.session.add(atual)

    db.session.commit()
    if has_request_context():
        g._cleide_cfg = parsed
