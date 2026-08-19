"""
Montagem de contexto documental autorizado para o chat operacional da Júlia (Fase 4).

Carrega registros via Cleiton (store temporário), respeita limites administrativos
e produz bloco interno de prompt. Não expõe prepared_context ao frontend.
"""
from __future__ import annotations

import json
import logging

from flask import has_request_context, session

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
    FLOW_TYPE_JULIA_CHAT,
    FLOW_TYPE_JULIA_CHAT_DOCUMENTAL,
    STATUS_ERROR,
    get_cleiton_doc_ids,
)
from app.cleiton_doc_gemini_files import (
    build_gemini_file_part_for_generate,
    pdf_context_ready_from_record,
)
from app.cleiton_doc_service import (
    cleanup_expired_documents_for_session,
    maybe_cleanup_expired_cleiton_docs,
)
from app.cleiton_doc_store import load_document_record
from app.prompts import JULIA_CHAT_DOCUMENTAL_GUIDANCE
from app.services.cleiton_doc_config_service import get_cleiton_doc_config
from app.services.external_ai_masking import (
    ExternalAiMaskingSession,
    mask_structured_for_external_ai,
)

_TRUNCATION_NOTICE = "[... contexto truncado por limite de caracteres ...]"
logger = logging.getLogger(__name__)


def _empty_chat_context() -> dict:
    return {
        "context_block": "",
        "gemini_file_parts": [],
        "flow_type": FLOW_TYPE_JULIA_CHAT,
        "meta": {
            "files_considered": 0,
            "files_total_active": 0,
            "context_truncated": False,
            "prompt_chars_used": 0,
            "pdf_files_ready": 0,
        },
    }


def _format_gemini_file_block(record: dict) -> str:
    display = record.get(FIELD_DISPLAY_NAME) or "documento"
    status = (record.get(FIELD_STATUS) or "").strip().lower()
    if status == STATUS_ERROR:
        return (
            f"- Observações: PDF \"{display}\" não pôde ser preparado para leitura pela IA. "
            "Informe ao usuário que o arquivo precisa ser reenviado ou substituído.\n"
            "- Conteúdo preparado:\n"
            "  (Leitura via Gemini File API indisponível para este anexo.)\n"
        )
    if pdf_context_ready_from_record(record):
        return (
            f"- Observações: PDF \"{display}\" anexado e disponível como contexto multimodal "
            "via Gemini File API. Responda com base no documento; cite evidências quando possível. "
            "Se não encontrar informação no PDF, diga explicitamente.\n"
            "- Conteúdo preparado:\n"
            "  (Conteúdo do PDF enviado ao modelo como parte de arquivo — não repetir como texto aqui.)\n"
        )
    meta_lines = [
        "- Observações: PDF ainda não está pronto para leitura pela IA nesta sessão.",
        "- Conteúdo preparado:",
        "  (Aguardando preparação via Gemini File API.)",
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
    truncated_label = "truncado" if truncated else "não truncado"
    kind = (record.get(FIELD_CONTEXT_KIND) or "").strip()

    lines = [
        f"Documento {index}:",
        f"- Nome: {display}",
        f"- Tipo: {doc_type} ({extension or mime})",
        f"- Observações: {truncated_label}",
    ]

    if kind == CONTEXT_KIND_GEMINI_FILE:
        lines.append(_format_gemini_file_block(record).rstrip())
    elif kind == CONTEXT_KIND_TEXT:
        content = (record.get(FIELD_PREPARED_CONTEXT) or "").strip()
        lines.append("- Conteúdo preparado:")
        lines.append(content if content else "(vazio)")
    else:
        lines.append("- Conteúdo preparado:")
        lines.append("(contexto indisponível para este tipo nesta fase)")

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


def build_julia_document_context_for_chat() -> dict:
    """
    Monta bloco de contexto documental para injeção no prompt da Júlia.

    Retorna dict com context_block (str), gemini_file_parts, flow_type e meta técnica.
    """
    if not has_request_context():
        return _empty_chat_context()

    maybe_cleanup_expired_cleiton_docs()
    cleanup_expired_documents_for_session()

    cfg = get_cleiton_doc_config()
    doc_ids = get_cleiton_doc_ids(session)
    if not doc_ids:
        return _empty_chat_context()

    active_records: list[dict] = []
    for doc_id in doc_ids:
        record = load_document_record(doc_id, ttl_hours=cfg.upload_ttl_hours)
        if record is not None:
            active_records.append(record)

    if not active_records:
        return _empty_chat_context()

    max_files = max(0, int(cfg.prompt_max_files_considered))
    max_chars = max(0, int(cfg.prompt_context_max_chars))

    if max_files > 0:
        considered = active_records[-max_files:]
    else:
        considered = list(active_records)

    guidance = JULIA_CHAT_DOCUMENTAL_GUIDANCE.strip()
    header = f"{guidance}\n\nContexto documental temporário autorizado por Cleiton:\n\n"
    blocks: list[str] = []
    context_truncated = False

    outbound_session = ExternalAiMaskingSession()
    for idx, record in enumerate(considered, start=1):
        outbound_record = mask_structured_for_external_ai(
            record, session=outbound_session
        )
        blocks.append(_format_document_block(idx, outbound_record))

    body = "".join(blocks)
    context_block = header + body

    if max_chars > 0 and len(context_block) > max_chars:
        context_block = context_block[: max_chars - len(_TRUNCATION_NOTICE)] + _TRUNCATION_NOTICE
        context_truncated = True

    gemini_file_parts = _collect_gemini_file_parts(considered)
    pdf_ready_count = sum(1 for r in considered if pdf_context_ready_from_record(r))
    doc_summary = []
    for record in considered:
        doc_summary.append(
            {
                "display_name": (record.get(FIELD_DISPLAY_NAME) or "documento")[:120],
                "doc_type": (record.get(FIELD_DOC_TYPE) or "")[:20],
                "context_kind": (record.get(FIELD_CONTEXT_KIND) or "")[:40],
                "status": (record.get(FIELD_STATUS) or "")[:20],
                "pdf_ready": pdf_context_ready_from_record(record),
            }
        )
    logger.info(
        "Julia doc context: files_total_active=%s files_considered=%s pdf_files_ready=%s gemini_file_parts=%s context_truncated=%s prompt_chars_used=%s docs=%s",
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
        "flow_type": FLOW_TYPE_JULIA_CHAT_DOCUMENTAL,
        "meta": {
            "files_considered": len(considered),
            "files_total_active": len(active_records),
            "context_truncated": context_truncated,
            "prompt_chars_used": len(context_block),
            "pdf_files_ready": pdf_ready_count,
        },
    }
