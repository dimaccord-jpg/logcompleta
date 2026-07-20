"""
Montagem de contexto documental autorizado para o chat da Agente Compara.

Carrega registros via store Cleiton usando exclusivamente `agente_compara_doc_ids`.
Nao expoe paths internos, nao chama IA diretamente e nao altera sessao alem da leitura.
"""
from __future__ import annotations

import json
import logging

from app.agente_compara_doc_service import (
    AGENTE_COMPARA_CHAT_FLOW_TYPE,
    get_agente_compara_doc_ids,
)
from app.agente_compara_prompt import build_agente_compara_document_guidance
from app.cleiton_doc_contracts import (
    CONTEXT_KIND_GEMINI_FILE,
    CONTEXT_KIND_TEXT,
    FIELD_CONTEXT_KIND,
    FIELD_DISPLAY_NAME,
    FIELD_DOC_TYPE,
    FIELD_EXTENSION,
    FIELD_MIME_TYPE,
    FIELD_PREPARED_CONTEXT,
    FIELD_STATUS,
    FIELD_TRUNCATED,
    STATUS_ERROR,
)
from app.cleiton_doc_gemini_files import (
    build_gemini_file_part_for_generate,
    pdf_context_ready_from_record,
)
from app.cleiton_doc_store import load_document_record
from app.services.agente_compara_config_service import get_agente_compara_config
from app.services.cleiton_doc_config_service import get_cleiton_doc_config

_TRUNCATION_NOTICE = "[... contexto truncado por limite de caracteres ...]"
logger = logging.getLogger(__name__)


def _empty_chat_context(*, warnings: list[str] | None = None) -> dict:
    return {
        "context_block": "",
        "gemini_file_parts": [],
        "flow_type": AGENTE_COMPARA_CHAT_FLOW_TYPE,
        "has_documents": False,
        "meta": {
            "files_considered": 0,
            "files_total_active": 0,
            "context_truncated": False,
            "prompt_chars_used": 0,
            "pdf_files_ready": 0,
            "documents": [],
            "warnings": list(warnings or []),
        },
    }


def _safe_document_summary(record: dict) -> dict:
    return {
        "display_name": (record.get(FIELD_DISPLAY_NAME) or "documento")[:120],
        "doc_type": (record.get(FIELD_DOC_TYPE) or "")[:20],
        "context_kind": (record.get(FIELD_CONTEXT_KIND) or "")[:40],
        "status": (record.get(FIELD_STATUS) or "")[:20],
        "pdf_ready": pdf_context_ready_from_record(record),
    }


def _format_gemini_file_block(record: dict) -> str:
    display = record.get(FIELD_DISPLAY_NAME) or "documento"
    status = (record.get(FIELD_STATUS) or "").strip().lower()
    if status == STATUS_ERROR:
        return (
            f'- Observacoes: PDF "{display}" foi recebido, mas nao ficou legivel para analise nesta sessao. '
            "Informe isso com clareza e oriente reenviar em Excel, CSV ou PDF com texto selecionavel.\n"
            "- Conteudo preparado:\n"
            "  (Nenhum conteudo textual confiavel foi disponibilizado para este anexo.)\n"
        )
    if pdf_context_ready_from_record(record):
        return (
            f'- Observacoes: PDF "{display}" anexado com contexto pronto para leitura. '
            "Responda com base no documento; cite evidencias quando possivel. "
            "Se nao encontrar informacao no PDF, diga explicitamente.\n"
            "- Conteudo preparado:\n"
            "  (O conteudo do PDF segue como arquivo de contexto anexado ao modelo; nao invente trechos ausentes.)\n"
        )
    meta_lines = [
        "- Observacoes: Recebi o PDF, mas ainda nao consegui extrair conteudo legivel dele nesta sessao.",
        "- Conteudo preparado:",
        "  (Nenhum texto confiavel disponivel ate o momento.)",
    ]
    try:
        payload = json.loads(record.get(FIELD_PREPARED_CONTEXT) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if payload:
        safe_keys = (
            "strategy",
            "mime_type",
            "size_bytes",
            "page_count",
            "max_pages_configured",
            "gemini_file_ready",
            "gemini_file_error",
        )
        for key in safe_keys:
            if key in payload:
                meta_lines.append(f"  - {key}: {payload[key]}")
    return "\n".join(meta_lines) + "\n"


def _format_document_block(index: int, record: dict) -> str:
    display = record.get(FIELD_DISPLAY_NAME) or "documento"
    doc_type = record.get(FIELD_DOC_TYPE) or "desconhecido"
    extension = record.get(FIELD_EXTENSION) or ""
    mime = record.get(FIELD_MIME_TYPE) or ""
    truncated = record.get(FIELD_TRUNCATED)
    truncated_label = "truncado" if truncated else "nao truncado"
    kind = (record.get(FIELD_CONTEXT_KIND) or "").strip()

    lines = [
        f"Documento {index}:",
        f"- Nome: {display}",
        f"- Tipo: {doc_type} ({extension or mime})",
        f"- Observacoes: {truncated_label}",
    ]

    if kind == CONTEXT_KIND_GEMINI_FILE:
        lines.append(_format_gemini_file_block(record).rstrip())
    elif kind == CONTEXT_KIND_TEXT:
        content = (record.get(FIELD_PREPARED_CONTEXT) or "").strip()
        lines.append("- Conteudo preparado:")
        lines.append(content if content else "(vazio)")
    else:
        lines.append("- Conteudo preparado:")
        lines.append("(contexto indisponivel para este tipo nesta fase)")

    return "\n".join(lines) + "\n\n"


def _collect_gemini_file_parts(records: list[dict]) -> list:
    parts: list = []
    for record in records:
        if (record.get(FIELD_CONTEXT_KIND) or "").strip() != CONTEXT_KIND_GEMINI_FILE:
            continue
        if not pdf_context_ready_from_record(record):
            continue
        part = build_gemini_file_part_for_generate(record)
        if part is not None:
            parts.append(part)
    return parts


def build_agente_compara_document_context_for_chat(session_obj) -> dict:
    """
    Monta bloco de contexto documental para injecao no prompt da Agente Compara.

    Usa exclusivamente IDs em `agente_compara_doc_ids` da sessao informada.
    """
    cleiton_cfg = get_cleiton_doc_config()
    audit_cfg = get_agente_compara_config()
    doc_ids = get_agente_compara_doc_ids(session_obj)
    if not doc_ids:
        return _empty_chat_context()

    active_records: list[dict] = []
    warnings: list[str] = []

    for doc_id in doc_ids:
        record = load_document_record(doc_id, ttl_hours=cleiton_cfg.upload_ttl_hours)
        if record is None:
            warnings.append(
                f"Documento {doc_id[:12]}... expirado ou indisponivel; nao considerado no contexto."
            )
            continue
        active_records.append(record)

    if not active_records:
        return _empty_chat_context(warnings=warnings)

    max_files = max(0, int(audit_cfg.max_documents_considered))
    max_chars = max(0, int(audit_cfg.document_context_max_chars))

    considered = active_records[-max_files:] if max_files > 0 else list(active_records)
    if len(active_records) > len(considered):
        skipped = len(active_records) - len(considered)
        warnings.append(
            f"{skipped} documento(s) ativo(s) omitido(s) por limite de arquivos considerados no prompt."
        )

    guidance = build_agente_compara_document_guidance().strip()
    header = f"{guidance}\n\nContexto documental temporario da Agente Compara:\n\n"
    blocks: list[str] = []
    context_truncated = False

    for idx, record in enumerate(considered, start=1):
        blocks.append(_format_document_block(idx, record))
        status = (record.get(FIELD_STATUS) or "").strip().lower()
        if status == STATUS_ERROR:
            display = record.get(FIELD_DISPLAY_NAME) or "documento"
            warnings.append(f'Documento "{display}" registrado com erro; contexto limitado.')

    body = "".join(blocks)
    context_block = header + body
    if max_chars > 0 and len(context_block) > max_chars:
        context_block = context_block[: max_chars - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE
        context_truncated = True
        warnings.append("Contexto documental truncado por limite de caracteres configurado.")

    gemini_file_parts = _collect_gemini_file_parts(considered)
    pdf_ready_count = sum(1 for record in considered if pdf_context_ready_from_record(record))
    doc_summary = [_safe_document_summary(record) for record in considered]

    logger.info(
        "Agente Compara audit doc context: files_total_active=%s files_considered=%s pdf_files_ready=%s gemini_file_parts=%s context_truncated=%s prompt_chars_used=%s docs=%s",
        len(active_records),
        len(considered),
        pdf_ready_count,
        len(gemini_file_parts),
        context_truncated,
        len(context_block),
        doc_summary,
    )

    return {
        "context_block": context_block,
        "gemini_file_parts": gemini_file_parts,
        "flow_type": AGENTE_COMPARA_CHAT_FLOW_TYPE,
        "has_documents": True,
        "meta": {
            "files_considered": len(considered),
            "files_total_active": len(active_records),
            "context_truncated": context_truncated,
            "prompt_chars_used": len(context_block),
            "pdf_files_ready": pdf_ready_count,
            "documents": doc_summary,
            "warnings": warnings,
        },
    }
