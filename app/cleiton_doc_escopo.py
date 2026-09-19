"""
Escopo operacional de documentos temporários (Conta/Franquia/User).

Camada A: registros JSON carimbados no momento do registro são revalidados
na leitura. Não inventa coluna SQL; usa os campos de identidade já existentes
no User (`conta_id`, `franquia_id`, `id`).

Camada B: após transferência Multiuser, referências documentais e caches
operacionais da sessão HTTP anterior são invalidados. A segurança não
depende só desta limpeza: a leitura recusa documento/cache fora do escopo.
"""
from __future__ import annotations

from app.cleiton_doc_contracts import (
    FIELD_CONTA_ID,
    FIELD_FRANQUIA_ID,
    FIELD_USUARIO_ID,
    SESSION_KEY_CLEITON_DOC_IDS,
    SESSION_KEY_CLEITON_DOC_LOCK,
)

# Inventário central das chaves operacionais/documentais. A invalidação
# pós-transferência usa exclusivamente esta lista (não duplicar por serviço).
OPERATIONAL_SESSION_KEYS: tuple[str, ...] = (
    SESSION_KEY_CLEITON_DOC_IDS,
    SESSION_KEY_CLEITON_DOC_LOCK,
    "cleide_audit_doc_ids",
    "cleide_audit_doc_context",
    "cleide_audit_chat_history",
    "cleide_audit_temp_table_id",
    "cleide_audit_temp_table_source_doc_ids",
    "cleide_audit_upload_lock",
    "cleide_audit_last_request_id",
    "cleide_audit_upload_in_progress",
    "cleide_audit_chat_idempotency_cache",
    "cleide_audit_insights_chat_idempotency_cache",
    "cleide_audit_insights_chat_unlock",
    "cleide_audit_insights_conversation_focus",
    "cleide_audit_correction_previews",
    "cleide_audit_temp_table_extraction_cache",
    "cleide_upload_ref",
    "cleide_upload_in_progress",
    "cleide_upload_lock",
    "cleide_dataset_context",
    "agente_compara_doc_ids",
    "agente_compara_doc_context",
    "agente_compara_chat_history",
    "agente_compara_comparison_state",
    "agente_compara_temp_table_id",
    "agente_compara_temp_table_source_doc_ids",
    "agente_compara_upload_lock",
    "agente_compara_last_request_id",
    "agente_compara_upload_in_progress",
    "agente_compara_chat_idempotency_cache",
    "agente_compara_insights_chat_idempotency_cache",
    "agente_compara_comparison_chat_idempotency_cache",
    "agente_compara_temp_table_save_idempotency_cache",
    "agente_compara_temp_table_extraction_cache",
    "agente_compara_correction_previews",
    "agente_compara_insights_chat_unlock",
    "agente_compara_insights_conversation_focus",
    "roberto_upload_ref",
    "onboarding_julia_context",
)

# Idempotency keys harvested on invalidation so replay pós-transferência
# não reutiliza resposta antiga nem exige nova chamada de LLM.
REJECTED_OPERATIONAL_CACHE_KEYS = "_operational_rejected_idempotency_keys"

_SESSION_DOC_REF_KEYS = OPERATIONAL_SESSION_KEYS


def _as_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def current_operational_scope() -> dict[str, int] | None:
    """Conta/Franquia/User do operador autenticado, se houver contexto HTTP."""
    from flask import has_request_context

    if not has_request_context():
        return None
    try:
        from flask_login import current_user
    except Exception:
        return None
    try:
        if not getattr(current_user, "is_authenticated", False):
            return None
    except Exception:
        return None
    conta_id = _as_int(getattr(current_user, "conta_id", None))
    franquia_id = _as_int(getattr(current_user, "franquia_id", None))
    if conta_id is None or franquia_id is None:
        return None
    usuario_id = _as_int(getattr(current_user, "id", None))
    scope: dict[str, int] = {"conta_id": conta_id, "franquia_id": franquia_id}
    if usuario_id is not None:
        scope["usuario_id"] = usuario_id
    return scope


def stamp_document_operational_scope(record: dict) -> dict:
    """Grava identidade operacional atual no JSON temporário, quando autenticado."""
    if not isinstance(record, dict):
        return record
    scope = current_operational_scope()
    if not scope:
        return record
    record[FIELD_CONTA_ID] = scope["conta_id"]
    record[FIELD_FRANQUIA_ID] = scope["franquia_id"]
    if "usuario_id" in scope:
        record[FIELD_USUARIO_ID] = scope["usuario_id"]
    return record


def record_matches_explicit_scope(record: dict | None, scope: dict[str, int] | None) -> bool:
    """
    Autorização de leitura operacional com escopo explícito.

    Ausência de `conta_id`/`franquia_id` persistidos NÃO é autorização.
    Mesma Conta não autoriza. Se `usuario_id` estiver carimbado, o User
    dono também precisa coincidir (Contratante/admin não herdam o artefato).
    Sem operador (scope=None) não há contexto operacional a exigir
    (testes unitários do store sem login).
    """
    if not isinstance(record, dict):
        return False
    if scope is None:
        return True
    rec_conta = _as_int(record.get(FIELD_CONTA_ID))
    rec_fr = _as_int(record.get(FIELD_FRANQUIA_ID))
    if rec_conta is None or rec_fr is None:
        return False
    if rec_conta != scope.get("conta_id"):
        return False
    if rec_fr != scope.get("franquia_id"):
        return False
    rec_user = _as_int(record.get(FIELD_USUARIO_ID))
    scope_user = _as_int(scope.get("usuario_id"))
    if rec_user is not None and scope_user is not None and rec_user != scope_user:
        return False
    return True


def document_record_matches_operational_scope(record: dict | None) -> bool:
    return record_matches_explicit_scope(record, current_operational_scope())


def stamp_operational_cache_payload(payload: dict) -> dict:
    """Carimba Conta/Franquia/User atuais em entrada de cache operacional."""
    if not isinstance(payload, dict):
        return payload
    scope = current_operational_scope()
    if not scope:
        return payload
    payload[FIELD_CONTA_ID] = scope["conta_id"]
    payload[FIELD_FRANQUIA_ID] = scope["franquia_id"]
    if "usuario_id" in scope:
        payload[FIELD_USUARIO_ID] = scope["usuario_id"]
    return payload


def operational_cache_payload_is_current(payload) -> bool:
    """
    Cache sem metadata de escopo, ou com escopo diferente do atual, é MISS.

    Sem operador autenticado: não há contexto operacional (testes unitários).
    """
    scope = current_operational_scope()
    if scope is None:
        return True
    if not isinstance(payload, dict):
        return False
    return record_matches_explicit_scope(payload, scope)


def request_id_was_operationally_invalidated(session_obj, idempotency_key: str) -> bool:
    """Replay de request_id invalidado na transferência não reutiliza cache antigo."""
    if session_obj is None:
        return False
    ref = str(idempotency_key or "").strip()
    if not ref:
        return False
    try:
        raw = session_obj.get(REJECTED_OPERATIONAL_CACHE_KEYS)
    except Exception:
        return False
    if not isinstance(raw, list):
        return False
    return ref in {str(item) for item in raw if item}


def invalidate_session_document_refs(session_obj) -> None:
    """Remove IDs/estado documental e caches operacionais da sessão HTTP (Camada B)."""
    if session_obj is None:
        return
    changed = False
    rejected: list[str] = []
    existing = None
    try:
        existing = session_obj.get(REJECTED_OPERATIONAL_CACHE_KEYS)
    except Exception:
        existing = None
    if isinstance(existing, list):
        rejected.extend(str(item) for item in existing if item)
    for key in OPERATIONAL_SESSION_KEYS:
        try:
            if key not in session_obj:
                continue
            value = session_obj.get(key)
            if isinstance(value, dict):
                rejected.extend(str(item) for item in value.keys() if item)
            session_obj.pop(key, None)
            changed = True
        except Exception:
            continue
    if rejected:
        try:
            session_obj[REJECTED_OPERATIONAL_CACHE_KEYS] = rejected
            changed = True
        except Exception:
            pass
    if changed:
        try:
            session_obj.modified = True
        except Exception:
            pass


def invalidar_sessao_documental_pos_transferencia() -> None:
    from flask import has_request_context, session as flask_session

    if not has_request_context():
        return
    invalidate_session_document_refs(flask_session)
