"""Ponte interna das consultas analíticas do Agente Compara.

Liga um plano já tipado ao resultado oficial da comparação vigente. Não escolhe
consultas, não chama modelo, não reserva crédito e não grava operação.

O status operacional devolve o resultado reidratado. Esse objeto muda células
e a versão do schema, então o checksum dele não é o checksum armazenado. Esta
ponte lê de novo o artefato compacto e a memória pelos serviços de storage,
e só entrega ao executor o par cujo checksum confere com o ponteiro da sessão.

A entrada exige ``is_authenticated is True``. IDs presentes não autenticam.
Antes de ler o artefato, o fingerprint oficial atual precisa ser calculado e
coincidir com o persistido. Falha, ausência ou divergência dessa verificação
não libera publicação: o status oficial absorve exceção nesse cálculo e
seguiria com o estado gravado.
"""
from __future__ import annotations

import copy
import logging
from typing import Any
from uuid import UUID, uuid4

from app.agente_compara_chat_context_service import (
    SNAPSHOT_MISMATCH,
    SNAPSHOT_NOT_AUTHORIZED,
    SNAPSHOT_OBSOLETE,
    SNAPSHOT_UNAVAILABLE,
    authorize_comparison_snapshot_identity,
    official_snapshot_release_error,
)
from app.agente_compara_chat_contracts import (
    AuthorizedComparisonScope,
    ChatContractError,
    canonical_comparison_id,
)
from app.agente_compara_chat_query_catalog import (
    PUBLIC_ERROR_CODES,
    QUERY_TYPES,
    QueryCatalogError,
    QueryExecutionLimits,
    artifact_checksum,
    validate_query_plan,
)
from app.agente_compara_chat_query_executor import (
    AuthorizedAnalyticalProjection,
    execute_analytical_plan,
)

logger = logging.getLogger(__name__)

_HEX = frozenset("0123456789abcdef")
_SAFE_PLAN_CODES = frozenset(
    {
        "invalid_query",
        "invalid_query_type",
        "invalid_catalog_version",
        "invalid_filter",
        "invalid_page",
        "invalid_sort",
        "invalid_limit",
        "unknown_field",
        "plan_too_large",
        "too_many_queries",
        "forbidden_expression",
    }
)


def execute_authorized_analytical_plan(
    *,
    session_obj,
    usuario,
    plan,
    comparison_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Executa o plano recebido e devolve só a publicação autorizada.

    A identidade sai do usuário do fluxo. O plano não escolhe conta, usuário,
    comparação nem arquivo.
    """

    if not _authentication_proven(usuario):
        return _error(SNAPSHOT_NOT_AUTHORIZED)
    identity = _identity_from_usuario(usuario)
    if identity is None:
        return _error(SNAPSHOT_NOT_AUTHORIZED)
    conta_id, usuario_id, franquia_id = identity
    authorization = authorize_comparison_snapshot_identity(
        session_obj=session_obj,
        conta_id=conta_id,
        usuario_id=usuario_id,
        franquia_id=franquia_id,
        comparison_id=comparison_id,
    )
    if authorization.get("error_code"):
        return _error(SNAPSHOT_NOT_AUTHORIZED)
    session_comparison = authorization.get("comparison_id")
    if not isinstance(session_comparison, str) or not session_comparison:
        return _error(SNAPSHOT_NOT_AUTHORIZED)
    resolved_operation = _canonical_operation_id(operation_id)
    if resolved_operation is None:
        return _error("invalid_operation_reference")

    try:
        from app.agente_compara_calculation_execution_service import (
            AgenteComparaCalculationExecutionError,
            get_comparison_calculation,
            get_comparison_calculation_status,
        )
        from app.agente_compara_comparison_state import get_comparison_state

        currency_error = _current_fingerprint_error(
            session_obj=session_obj,
            comparison_id=session_comparison,
        )
        if currency_error:
            return _error(currency_error)
        try:
            status_payload = get_comparison_calculation_status(
                comparison_id=session_comparison,
                session_obj=session_obj,
            )
        except AgenteComparaCalculationExecutionError:
            return _error(SNAPSHOT_NOT_AUTHORIZED)
        release_error = official_snapshot_release_error(status_payload)
        if release_error:
            return _error(release_error)
        plan_error = _plan_error(plan)
        if plan_error:
            return _error(plan_error)

        state = get_comparison_state(session_obj)
        if not isinstance(state, dict) or state.get("comparison_id") != session_comparison:
            return _error(SNAPSHOT_NOT_AUTHORIZED)
        calc = get_comparison_calculation(state)
        loaded = _load_official_snapshot(calc, comparison_id=session_comparison)
        if loaded.get("error_code"):
            return _error(str(loaded["error_code"]))
        projection = AuthorizedAnalyticalProjection(
            operation_id=resolved_operation,
            scope=loaded["scope"],
            result=loaded["result"],
            memory_payload=loaded["memory_payload"],
        )
        published = execute_analytical_plan(plan, projection, limits=QueryExecutionLimits())
    except QueryCatalogError as exc:
        allowed = _SAFE_PLAN_CODES | PUBLIC_ERROR_CODES | {
            SNAPSHOT_MISMATCH,
            "invalid_projection",
            "projection_mutated",
        }
        return _error(exc.code if exc.code in allowed else "execution_failed")
    except ChatContractError:
        return _error(SNAPSHOT_MISMATCH)
    except Exception as exc:
        logger.info(
            "comparison_chat_analytical_bridge_failed error_class=%s",
            exc.__class__.__name__,
        )
        return _error("execution_failed")
    return _publication_result(published)


def _authentication_proven(usuario) -> bool:
    """Exige o contrato do Flask-Login: só ``True`` autentica.

    Atributo ausente, exceção ao lê-lo ou qualquer outro valor não comprovam
    autenticação. A checagem não consulta sessão nem artefato.
    """

    if usuario is None:
        return False
    try:
        authenticated = usuario.is_authenticated
    except Exception:
        return False
    return authenticated is True


def _current_fingerprint_error(*, session_obj, comparison_id: str) -> str | None:
    """Exige o fingerprint oficial atual antes da leitura do resultado.

    O status oficial só entra na leitura com cálculo READY, não stale e
    faturamento applied. Nesse caminho, ausência de erro não comprova
    vigência: a função oficial precisa devolver o mesmo fingerprint
    persistido. Fora desse caminho, READY, faturamento e stale continuam
    a cargo de ``official_snapshot_release_error``.
    """

    from app.agente_compara_calculation_execution_service import (
        BILLING_STATUS_APPLIED,
        _compute_current_fingerprint_for_state,
        get_comparison_calculation,
    )
    from app.agente_compara_comparison_state import STEP_CALCULATION_READY, get_comparison_state

    state = get_comparison_state(session_obj)
    if not isinstance(state, dict) or state.get("comparison_id") != comparison_id:
        return SNAPSHOT_NOT_AUTHORIZED
    calc = get_comparison_calculation(state)
    if not isinstance(calc, dict) or bool(calc.get("stale")):
        return None
    if str(calc.get("status") or "") != STEP_CALCULATION_READY:
        return None
    if str(calc.get("billing_status") or "") != BILLING_STATUS_APPLIED:
        return None
    stored = _sha256_text(calc.get("request_fingerprint"))
    if stored is None:
        return SNAPSHOT_UNAVAILABLE
    try:
        current = _compute_current_fingerprint_for_state(state, session_obj=session_obj)
    except Exception as exc:
        logger.info(
            "comparison_chat_analytical_bridge_currency_unproven error_class=%s",
            exc.__class__.__name__,
        )
        return SNAPSHOT_OBSOLETE
    if _sha256_text(current) != stored:
        return SNAPSHOT_OBSOLETE
    return None


def _identity_from_usuario(usuario) -> tuple[int, int, int] | None:
    conta_id = getattr(usuario, "conta_id", None)
    usuario_id = getattr(usuario, "id", None)
    franquia_id = getattr(usuario, "franquia_id", None)
    if (
        type(conta_id) is not int
        or type(usuario_id) is not int
        or type(franquia_id) is not int
        or conta_id <= 0
        or usuario_id <= 0
        or franquia_id <= 0
    ):
        return None
    return conta_id, usuario_id, franquia_id


def _canonical_operation_id(value: str | None) -> str | None:
    if value is None:
        return str(uuid4())
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError, TypeError):
        return None
    if candidate.lower() not in {parsed.hex, str(parsed)}:
        return None
    return str(parsed)


def _plan_error(plan: Any) -> str | None:
    try:
        validated = validate_query_plan(plan, limits=QueryExecutionLimits())
    except QueryCatalogError as exc:
        return exc.code if exc.code in _SAFE_PLAN_CODES else "invalid_query"
    if any(query.query_type not in QUERY_TYPES for query in validated.queries):
        return "invalid_query_type"
    return None


def _load_official_snapshot(calc: dict | None, *, comparison_id: str) -> dict[str, Any]:
    """Carrega o par armazenado e recusa representação com outro checksum."""

    if not isinstance(calc, dict):
        return {"error_code": SNAPSHOT_UNAVAILABLE}
    result_key = calc.get("result_storage_key")
    fingerprint = _sha256_text(calc.get("request_fingerprint"))
    result_checksum = _sha256_text(calc.get("result_checksum"))
    result_schema = calc.get("result_schema_version")
    if (
        not isinstance(result_key, str)
        or not result_key.strip()
        or fingerprint is None
        or result_checksum is None
        or type(result_schema) is not int
        or result_schema not in {1, 2}
    ):
        return {"error_code": SNAPSHOT_UNAVAILABLE}
    memory_key = calc.get("memory_storage_key")
    memory_checksum = calc.get("memory_checksum")
    memory_required = result_schema == 2 or bool(memory_key) or memory_checksum is not None
    stored_memory_checksum = None
    if memory_required:
        if not isinstance(memory_key, str) or not memory_key.strip():
            return {"error_code": SNAPSHOT_UNAVAILABLE}
        stored_memory_checksum = _sha256_text(memory_checksum)
        if stored_memory_checksum is None:
            return {"error_code": SNAPSHOT_MISMATCH}

    from app.agente_compara_calculation_result_storage import (
        ERROR_MEMORY_CORRUPT,
        ERROR_MEMORY_MISSING,
        ERROR_RESULT_CORRUPT,
        ERROR_RESULT_IDENTITY_MISMATCH,
        ERROR_RESULT_MISSING,
        AgenteComparaCalculationResultStorageError,
        load_comparison_calculation_memory_payload,
        load_comparison_calculation_result,
    )

    try:
        result = load_comparison_calculation_result(
            storage_key=result_key.strip(),
            comparison_id=comparison_id,
            fingerprint=fingerprint,
            expected_checksum=result_checksum,
        )
        memory = None
        if memory_required:
            memory = load_comparison_calculation_memory_payload(
                storage_key=memory_key.strip(),
                comparison_id=comparison_id,
                fingerprint=fingerprint,
                expected_checksum=stored_memory_checksum,
            )
    except AgenteComparaCalculationResultStorageError as exc:
        if exc.error_code in {ERROR_RESULT_MISSING, ERROR_MEMORY_MISSING}:
            return {"error_code": SNAPSHOT_UNAVAILABLE}
        if exc.error_code in {ERROR_RESULT_CORRUPT, ERROR_MEMORY_CORRUPT, ERROR_RESULT_IDENTITY_MISMATCH}:
            return {"error_code": SNAPSHOT_MISMATCH}
        return {"error_code": SNAPSHOT_UNAVAILABLE}

    if not isinstance(result, dict):
        return {"error_code": SNAPSHOT_MISMATCH}
    try:
        if artifact_checksum(result) != result_checksum:
            return {"error_code": SNAPSHOT_MISMATCH}
        if canonical_comparison_id(result.get("comparison_id")) != canonical_comparison_id(comparison_id):
            return {"error_code": SNAPSHOT_MISMATCH}
    except (QueryCatalogError, ChatContractError):
        return {"error_code": SNAPSHOT_MISMATCH}
    if result.get("schema_version") != result_schema:
        return {"error_code": SNAPSHOT_MISMATCH}

    memory_schema = None
    if memory is not None:
        if not isinstance(memory, dict):
            return {"error_code": SNAPSHOT_MISMATCH}
        try:
            if artifact_checksum(memory) != stored_memory_checksum:
                return {"error_code": SNAPSHOT_MISMATCH}
            if canonical_comparison_id(memory.get("comparison_id")) != canonical_comparison_id(comparison_id):
                return {"error_code": SNAPSHOT_MISMATCH}
        except (QueryCatalogError, ChatContractError):
            return {"error_code": SNAPSHOT_MISMATCH}
        memory_schema = memory.get("schema_version")
        if type(memory_schema) is not int or memory_schema < 1:
            return {"error_code": SNAPSHOT_MISMATCH}
        stored_schema = calc.get("memory_schema_version")
        if stored_schema is not None and stored_schema != memory_schema:
            return {"error_code": SNAPSHOT_MISMATCH}

    try:
        scope = AuthorizedComparisonScope(
            comparison_id=comparison_id,
            result_fingerprint=fingerprint,
            result_checksum=result_checksum,
            result_schema_version=result_schema,
            memory_checksum=stored_memory_checksum,
            memory_schema_version=memory_schema,
        )
    except ChatContractError:
        return {"error_code": SNAPSHOT_MISMATCH}
    return {"error_code": None, "scope": scope, "result": result, "memory_payload": memory}


def _sha256_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        return None
    return text


def _publication_result(published: Any) -> dict[str, Any]:
    if not isinstance(published, dict) or published.get("publication_version") != "comparison_chat_publication_v1":
        return _error("execution_failed")
    delivered = copy.deepcopy(published)
    error = delivered.get("error")
    if delivered.get("status") == "error":
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, str) or not code:
            code = "execution_failed"
        return {"status": "error", "error_code": code, "publication": delivered}
    return {"status": "ok", "error_code": None, "publication": delivered}


def _error(code: str) -> dict[str, Any]:
    token = code if isinstance(code, str) and code and all(char in "abcdefghijklmnopqrstuvwxyz_" for char in code) else "execution_failed"
    return {"status": "error", "error_code": token, "publication": None}
