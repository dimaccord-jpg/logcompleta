"""
Cleiton — governança técnica de chamadas ao SDK Gemini (fase 1).
Intercepta generate_content / generate_images, persiste evento de consumo e devolve a resposta original.
"""
from __future__ import annotations

import logging
from typing import Any

from flask import has_app_context

from app.consumo_identidade import resolve_identidade_para_persistencia
from app.extensions import db
from app.models import IaConsumoEvento, utcnow_naive

logger = logging.getLogger(__name__)

PROVIDER_GEMINI = "gemini"
OP_GENERATE_CONTENT = "generate_content"
OP_GENERATE_IMAGES = "generate_images"

STATUS_SUCCESS = "success"
STATUS_SUCCESS_NO_METRICS = "success_no_metrics"
STATUS_FAILURE = "failure"
PROVIDER_INTERNAL = "internal"


def _summarize_generate_contents(contents: Any) -> dict[str, Any]:
    if isinstance(contents, str):
        return {
            "contents_type": "str",
            "contents_items": 1,
            "item_types": ["text"],
            "file_parts": 0,
            "text_items": 1,
        }
    if not isinstance(contents, list):
        return {
            "contents_type": type(contents).__name__,
            "contents_items": 1,
            "item_types": [type(contents).__name__],
            "file_parts": 0,
            "text_items": 0,
        }
    summary = {
        "contents_type": "list",
        "contents_items": len(contents),
        "item_types": [],
        "file_parts": 0,
        "text_items": 0,
    }
    for item in contents:
        if isinstance(item, str):
            summary["item_types"].append("text")
            summary["text_items"] += 1
        elif getattr(item, "file_data", None) is not None:
            summary["item_types"].append("file_part")
            summary["file_parts"] += 1
        else:
            summary["item_types"].append(type(item).__name__)
    return summary


def _truncate_err(msg: str | None, limit: int = 2000) -> str | None:
    if msg is None:
        return None
    s = str(msg).strip()
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


def _coerce_usage_value(source: Any, *names: str) -> int | None:
    for name in names:
        value = None
        if isinstance(source, dict):
            value = source.get(name)
        else:
            value = getattr(source, name, None)
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _extract_usage_from_response(response: Any) -> tuple[int | None, int | None, int | None]:
    um = getattr(response, "usage_metadata", None)
    if um is None and isinstance(response, dict):
        um = response.get("usage_metadata") or response.get("usageMetadata")
    if um is None:
        return None, None, None
    inp_i = _coerce_usage_value(um, "prompt_token_count", "promptTokenCount", "input_tokens", "inputTokens")
    out_i = _coerce_usage_value(um, "candidates_token_count", "candidatesTokenCount", "output_tokens", "outputTokens")
    tot_i = _coerce_usage_value(um, "total_token_count", "totalTokenCount", "total_tokens", "totalTokens")
    return inp_i, out_i, tot_i


def _persist_event(
    *,
    operation: str,
    model: str,
    agent: str,
    flow_type: str,
    api_key_label: str,
    status: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    error_summary: str | None = None,
) -> None:
    if not has_app_context():
        logger.debug("Governança Gemini: sem app context; evento não persistido (%s %s).", operation, flow_type)
        return
    try:
        ident = resolve_identidade_para_persistencia()
        row = IaConsumoEvento(
            occurred_at=utcnow_naive(),
            provider=PROVIDER_GEMINI,
            operation=operation,
            model=(model or "")[:255],
            agent=(agent or "")[:80],
            flow_type=(flow_type or "")[:80],
            api_key_label=(api_key_label or "")[:80],
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            error_summary=_truncate_err(error_summary, 2000),
            conta_id=ident.get("conta_id"),
            franquia_id=ident.get("franquia_id"),
            usuario_id=ident.get("usuario_id"),
            tipo_origem=(ident.get("tipo_origem") or "")[:80] or None,
            origem_sistema=ident.get("origem_sistema"),
        )
        db.session.add(row)
        db.session.commit()
        try:
            from app.services.cleiton_franquia_operacional_service import (
                aplicar_motor_apos_ia_consumo_evento,
            )

            aplicar_motor_apos_ia_consumo_evento(row.id)
        except Exception as ex:
            logger.warning(
                "Governança Gemini: motor operacional Cleiton após evento IA falhou (id=%s): %s",
                getattr(row, "id", None),
                ex,
            )
            try:
                db.session.rollback()
            except Exception:
                pass
    except Exception as e:
        logger.warning("Governança Gemini: falha ao persistir evento (%s): %s", flow_type, e)
        try:
            db.session.rollback()
        except Exception:
            pass


def register_internal_ia_event(
    *,
    operation: str,
    agent: str,
    flow_type: str,
    status: str,
    api_key_label: str = "internal",
    model: str = "",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    error_summary: str | None = None,
) -> None:
    """
    Registra evento administrativo sem chamada direta ao SDK.
    Usado para manter observabilidade quando o fluxo responde sem usage_metadata
    ou antes de chegar ao provider externo.
    """
    if not has_app_context():
        logger.debug("Governanca Gemini: sem app context; evento interno nao persistido (%s %s).", operation, flow_type)
        return
    try:
        ident = resolve_identidade_para_persistencia()
        row = IaConsumoEvento(
            occurred_at=utcnow_naive(),
            provider=PROVIDER_INTERNAL,
            operation=operation,
            model=(model or "")[:255],
            agent=(agent or "")[:80],
            flow_type=(flow_type or "")[:80],
            api_key_label=(api_key_label or "internal")[:80],
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            error_summary=_truncate_err(error_summary, 2000),
            conta_id=ident.get("conta_id"),
            franquia_id=ident.get("franquia_id"),
            usuario_id=ident.get("usuario_id"),
            tipo_origem=(ident.get("tipo_origem") or "")[:80] or None,
            origem_sistema=ident.get("origem_sistema"),
        )
        db.session.add(row)
        db.session.commit()
    except Exception as e:
        logger.warning("Governanca Gemini: falha ao persistir evento interno (%s): %s", flow_type, e)
        try:
            db.session.rollback()
        except Exception:
            pass


def cleiton_governed_generate_content(
    client: Any,
    *,
    model: str,
    contents: Any,
    config: Any = None,
    agent: str,
    flow_type: str,
    api_key_label: str,
) -> Any:
    """
    Executa client.models.generate_content, registra evento e retorna a resposta do SDK.
    """
    contents_summary = _summarize_generate_contents(contents)
    logger.info(
        "Governanca Gemini: generate_content start model=%s agent=%s flow_type=%s contents=%s",
        model,
        agent,
        flow_type,
        contents_summary,
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        inp, out, tot = _extract_usage_from_response(response)
        logger.info(
            "Governanca Gemini: generate_content response model=%s flow_type=%s usage_metadata_present=%s input_tokens=%s output_tokens=%s total_tokens=%s",
            model,
            flow_type,
            getattr(response, "usage_metadata", None) is not None,
            inp,
            out,
            tot,
        )
        if inp is None and out is None and tot is None:
            status = STATUS_SUCCESS_NO_METRICS
        else:
            status = STATUS_SUCCESS
        _persist_event(
            operation=OP_GENERATE_CONTENT,
            model=model,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            status=status,
            input_tokens=inp,
            output_tokens=out,
            total_tokens=tot,
            error_summary=None,
        )
        return response
    except Exception as e:
        logger.warning(
            "Governanca Gemini: generate_content provider failure model=%s flow_type=%s exc_type=%s message=%s",
            model,
            flow_type,
            e.__class__.__name__,
            e,
        )
        _persist_event(
            operation=OP_GENERATE_CONTENT,
            model=model,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            status=STATUS_FAILURE,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            error_summary=str(e),
        )
        raise


def cleiton_governed_generate_images(
    client: Any,
    *,
    agent: str,
    flow_type: str,
    api_key_label: str,
    **kwargs: Any,
) -> Any:
    """
    Executa client.models.generate_images, registra evento e retorna a resposta do SDK.
    O SDK atual não expõe usage_metadata tipado em GenerateImagesResponse; tokens ficam nulos.
    """
    model = str(kwargs.get("model") or "")
    try:
        response = client.models.generate_images(**kwargs)
        _persist_event(
            operation=OP_GENERATE_IMAGES,
            model=model,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            status=STATUS_SUCCESS_NO_METRICS,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            error_summary=None,
        )
        return response
    except Exception as e:
        _persist_event(
            operation=OP_GENERATE_IMAGES,
            model=model,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            status=STATUS_FAILURE,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            error_summary=str(e),
        )
        raise
