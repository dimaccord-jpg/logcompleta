"""
Contratos tecnicos minimos da Cleide (Fase 1.1).
"""

SESSION_KEY_CLEIDE_UPLOAD_REF = "cleide_upload_ref"
SESSION_KEY_CLEIDE_UPLOAD_IN_PROGRESS = "cleide_upload_in_progress"
SESSION_KEY_CLEIDE_UPLOAD_LOCK = "cleide_upload_lock"
SESSION_KEY_CLEIDE_DATASET_CONTEXT = "cleide_dataset_context"


def get_cleide_upload_ref(session_obj) -> str | None:
    raw = session_obj.get(SESSION_KEY_CLEIDE_UPLOAD_REF)
    if not isinstance(raw, str):
        return None
    ref = raw.strip()
    return ref or None


def set_cleide_upload_ref(session_obj, upload_ref: str) -> None:
    ref = (upload_ref or "").strip()
    if not ref:
        raise ValueError("cleide_upload_ref invalido.")
    session_obj[SESSION_KEY_CLEIDE_UPLOAD_REF] = ref


def clear_cleide_upload_ref(session_obj) -> None:
    session_obj.pop(SESSION_KEY_CLEIDE_UPLOAD_REF, None)


def get_cleide_dataset_context(session_obj) -> dict | None:
    raw = session_obj.get(SESSION_KEY_CLEIDE_DATASET_CONTEXT)
    if not isinstance(raw, dict):
        return None
    return raw


def set_cleide_dataset_context(session_obj, context: dict) -> None:
    if not isinstance(context, dict):
        raise ValueError("cleide_dataset_context invalido.")
    session_obj[SESSION_KEY_CLEIDE_DATASET_CONTEXT] = context


def clear_cleide_dataset_context(session_obj) -> None:
    session_obj.pop(SESSION_KEY_CLEIDE_DATASET_CONTEXT, None)


def is_cleide_upload_in_progress(session_obj) -> bool:
    return bool(session_obj.get(SESSION_KEY_CLEIDE_UPLOAD_IN_PROGRESS))


def mark_cleide_upload_in_progress(session_obj) -> None:
    session_obj[SESSION_KEY_CLEIDE_UPLOAD_IN_PROGRESS] = True


def clear_cleide_upload_in_progress(session_obj) -> None:
    session_obj.pop(SESSION_KEY_CLEIDE_UPLOAD_IN_PROGRESS, None)


def get_cleide_upload_lock(session_obj) -> str | None:
    raw = session_obj.get(SESSION_KEY_CLEIDE_UPLOAD_LOCK)
    if not isinstance(raw, str):
        return None
    lock_key = raw.strip()
    return lock_key or None


def get_or_create_cleide_upload_lock(session_obj) -> str:
    lock_key = get_cleide_upload_lock(session_obj)
    if lock_key:
        return lock_key
    from uuid import uuid4

    lock_key = f"cleide-lock-{uuid4().hex}"
    session_obj[SESSION_KEY_CLEIDE_UPLOAD_LOCK] = lock_key
    return lock_key
