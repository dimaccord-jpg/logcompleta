"""
Governança contextual de dados antes de qualquer saída de IA externa (SCRUM-75).

Cleiton é a fachada: classifica localmente, minimiza, autoriza ou bloqueia.
Nenhuma chamada a Gemini/OpenAI/outro provedor é feita para decidir privacidade.
"""
from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.services.cleiton_ai_privacy_classifier import (
    DECISION_ALLOW,
    DECISION_BLOCK,
    DECISION_MINIMIZE,
    SUPPORTED_PURPOSES,
    ClassificationResult,
    ClassifierUnavailableError,
    classification_invariants_ok,
    classify_text,
    _is_issued_alias,
)
from app.services.cleiton_ai_safe_context import (
    CleitonAiAliasSession,
    apply_spans_to_text,
    copy_and_minimize_structured,
)

logger = logging.getLogger(__name__)

PURPOSE_DISCOVERY = "discovery"
PURPOSE_CHAT_LOGISTICO = "chat_logistico"
PURPOSE_AUDITORIA_FRETE = "auditoria_frete"
PURPOSE_EXTRACAO_TABELA = "extracao_tabela_frete"
PURPOSE_EXPLICACAO_CALCULO = "explicacao_calculo"
PURPOSE_COMPARACAO = "comparacao_fretes"
PURPOSE_INSIGHTS_AUDITORIA = "insights_auditoria"
PURPOSE_REDACAO = "redacao_editorial"
PURPOSE_IMAGEM = "geracao_imagem"
PURPOSE_BUSCA_WEB = "busca_web"

USER_SAFE_PREPARATION_FAILED = (
    "Não foi possível preparar este conteúdo com segurança para análise por IA. "
    "Revise o conteúdo e tente novamente."
)

CONTENT_TYPE_TEXT = "text"
CONTENT_TYPE_HISTORY = "history"
CONTENT_TYPE_DOCUMENT = "document"
CONTENT_TYPE_PDF = "pdf"
CONTENT_TYPE_WEB_QUERY = "web_query"
CONTENT_TYPE_IMAGE_PROMPT = "image_prompt"
CONTENT_TYPE_GENERATE_CONTENTS = "generate_contents"
CONTENT_TYPE_PROVIDER_CONFIG = "provider_config"

CLASSIFIER_NAME = "cleiton_contextual_v1"
CLASSIFIER_RUNTIME = (
    "Classificador contextual local determinístico (CPU). "
    "Sem HTTP, sem upload, sem download de modelo em runtime, sem regex. "
    "Impacto de memória: trivial (sem pesos neurais). Startup: import Python apenas. "
    "Não participa da franquia de chamadas externas."
)

_CONFIG_PASSTHROUGH_KEYS = frozenset(
    {
        "temperature",
        "top_p",
        "top_k",
        "topP",
        "topK",
        "max_output_tokens",
        "maxOutputTokens",
        "max_tokens",
        "candidate_count",
        "candidateCount",
        "stop_sequences",
        "stopSequences",
        "response_mime_type",
        "responseMimeType",
        "response_schema",
        "responseSchema",
        "safety_settings",
        "safetySettings",
        "tools",
        "tool_config",
        "toolConfig",
        "timeout",
        "http_options",
        "httpOptions",
        "seed",
        "presence_penalty",
        "presencePenalty",
        "frequency_penalty",
        "frequencyPenalty",
        "response_modalities",
        "responseModalities",
        "number_of_images",
        "numberOfImages",
        "aspect_ratio",
        "aspectRatio",
        "output_mime_type",
        "outputMimeType",
        "person_generation",
        "personGeneration",
        "thinking_config",
        "thinkingConfig",
        "speech_config",
        "speechConfig",
        "audio_timestamp",
        "audioTimestamp",
    }
)

_CONFIG_TEXT_KEYS = frozenset(
    {
        "system_instruction",
        "systemInstruction",
        "prompt",
        "negative_prompt",
        "negativePrompt",
        "instruction",
        "instructions",
        "text",
        "contents",
    }
)

_CONFIG_TEXT_ATTRS = (
    "system_instruction",
    "systemInstruction",
    "prompt",
    "negative_prompt",
    "negativePrompt",
    "instruction",
    "instructions",
)


class CleitonAiGovernanceBlockedError(RuntimeError):
    """Conteúdo não autorizado para o provedor externo."""

    def __init__(
        self,
        message: str = USER_SAFE_PREPARATION_FAILED,
        *,
        reason_codes: list[str] | None = None,
        categories_detected: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.user_message = message
        self.reason_codes = list(reason_codes or [])
        self.categories_detected = list(categories_detected or [])


@dataclass
class GovernanceResult:
    decision: str
    safe_content: Any
    aliases: dict[str, int] = field(default_factory=dict)
    categories_detected: list[str] = field(default_factory=list)
    categories_removed: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    diagnostic_metadata: dict[str, Any] = field(default_factory=dict)
    issued_by_cleiton: bool = False


def get_ai_data_protection_status() -> dict[str, Any]:
    """Fonte única de estado para a futura UI de Segurança (SCRUM-125)."""
    return {
        "data_protection_in_ai": "active",
        "contextual_minimization": "active",
        "credential_protection": "active",
        "data_isolation": "active",
        "guardrails_mandatory": True,
        "disable_protection": False,
        "local_classifier": CLASSIFIER_NAME,
        "classifier_runtime": CLASSIFIER_RUNTIME,
        "external_classifier_calls": False,
        "supported_purposes": list(SUPPORTED_PURPOSES),
    }


def purpose_from_flow_type(flow_type: str | None, agent: str | None = None) -> str:
    ft = (flow_type or "").strip().lower()
    ag = (agent or "").strip().lower()
    if "discovery" in ft or "onboarding" in ft:
        return PURPOSE_DISCOVERY
    if "imagem" in ft or "imagen" in ft:
        return PURPOSE_IMAGEM
    if "redacao" in ft:
        return PURPOSE_REDACAO
    if "web" in ft or "search" in ft:
        return PURPOSE_BUSCA_WEB
    if "compara" in ft or ag in {"agente_compara", "compara"}:
        if "insight" in ft:
            return PURPOSE_INSIGHTS_AUDITORIA
        if "temp_table" in ft or "extract" in ft:
            return PURPOSE_EXTRACAO_TABELA
        if "comparison_chat" in ft or "calculo" in ft or "calculation" in ft:
            return PURPOSE_EXPLICACAO_CALCULO
        return PURPOSE_COMPARACAO
    if "cleide" in ft or ag == "cleide":
        if "insight" in ft:
            return PURPOSE_INSIGHTS_AUDITORIA
        if "temp_table" in ft or "extract" in ft:
            return PURPOSE_EXTRACAO_TABELA
        return PURPOSE_AUDITORIA_FRETE
    return PURPOSE_CHAT_LOGISTICO


def project_safe_error(
    exc: BaseException,
    *,
    stage: str,
    provider: str,
    retry: bool | None = None,
) -> str:
    """Projeção observável do erro sem payload bruto."""
    retry_label = "yes" if retry else "no" if retry is False else "n/a"
    reason = ""
    if isinstance(exc, CleitonAiGovernanceBlockedError):
        reason = ",".join(exc.reason_codes[:8])
    class_name = exc.__class__.__name__
    parts = [
        f"type={class_name}",
        f"stage={stage}",
        f"provider={provider}",
        f"retry={retry_label}",
    ]
    if reason:
        parts.append(f"reason={reason}")
    else:
        parts.append("message=provider_or_runtime_error")
        lowered = class_name.lower()
        if "deadline" in lowered or "timeout" in lowered:
            parts.append("code=timeout")
    return "|".join(parts)


def governance_result_is_coherent(result: GovernanceResult | None, original: Any = None) -> bool:
    """Não autorizar só porque um flag diz valid=True."""
    if result is None or not isinstance(result, GovernanceResult):
        return False
    if result.decision not in {DECISION_ALLOW, DECISION_MINIMIZE, DECISION_BLOCK}:
        return False
    if result.decision == DECISION_BLOCK:
        return True
    if result.safe_content is None:
        return False
    if result.decision == DECISION_MINIMIZE:
        evidence = bool(result.categories_removed) or bool(result.categories_detected)
        evidence = evidence or "original_file_parts_stripped" in result.reason_codes
        evidence = evidence or "structured_fields_minimized" in result.reason_codes
        if not evidence:
            return False
        if original is not None and result.safe_content == original:
            if "original_file_parts_stripped" not in result.reason_codes:
                return False
    return True


def extract_authorized_upload_bytes(result: Any) -> bytes | None:
    """
    Bytes de upload só a partir de resultado emitido pela governança de Cleiton.
    Bytes autodeclarados não autorizam.
    """
    if not isinstance(result, GovernanceResult):
        return None
    if not result.issued_by_cleiton:
        return None
    if result.decision not in {DECISION_ALLOW, DECISION_MINIMIZE}:
        return None
    purpose = (result.diagnostic_metadata or {}).get("purpose")
    if purpose not in SUPPORTED_PURPOSES:
        return None
    if (result.diagnostic_metadata or {}).get("classifier") != CLASSIFIER_NAME:
        return None
    if not governance_result_is_coherent(result):
        return None
    safe = result.safe_content
    if isinstance(safe, str):
        return safe.encode("utf-8")
    if isinstance(safe, (bytes, bytearray, memoryview)):
        return bytes(safe)
    return None


def _scope_usuario_id() -> int | str | None:
    try:
        from flask import has_app_context

        if not has_app_context():
            return None
        from app.consumo_identidade import resolve_identidade_para_persistencia

        ident = resolve_identidade_para_persistencia()
        return ident.get("usuario_id")
    except Exception:
        return None


def _emit_governance_log(
    *,
    agent: str,
    purpose: str,
    content_type: str,
    result: GovernanceResult,
    provider: str,
    latency_ms: int,
    error_technical: str | None = None,
) -> None:
    logger.info(
        "Cleiton AI governance | agent=%s purpose=%s content_type=%s decision=%s "
        "categories_detected=%s categories_removed=%s provider=%s latency_ms=%s "
        "result=%s error_technical=%s classifier=%s",
        agent,
        purpose,
        content_type,
        result.decision,
        ",".join(result.categories_detected) or "-",
        ",".join(result.categories_removed) or "-",
        provider,
        latency_ms,
        "ok" if result.decision != DECISION_BLOCK else "blocked",
        error_technical or "-",
        CLASSIFIER_NAME,
    )


def _invalid_block(reason: str) -> GovernanceResult:
    return GovernanceResult(
        decision=DECISION_BLOCK,
        safe_content=None,
        reason_codes=[reason],
        diagnostic_metadata={"classifier": CLASSIFIER_NAME, "valid": False},
        issued_by_cleiton=True,
    )


def _minimize_text(text: str, session: CleitonAiAliasSession, purpose: str) -> tuple[str, ClassificationResult]:
    classified = classify_text(text, purpose=purpose, content_type="text")
    if not classification_invariants_ok(classified):
        raise ClassifierUnavailableError("inconsistent_classifier_result")
    if classified.decision == DECISION_ALLOW:
        return text, classified
    if classified.decision == DECISION_BLOCK:
        return text, classified
    safe = apply_spans_to_text(text, classified.spans, session)
    if safe == text:
        if classified.spans and all(
            _is_issued_alias(text[span.start : span.end]) for span in classified.spans
        ):
            classified.decision = DECISION_ALLOW
            classified.spans = []
            classified.categories_detected = []
            classified.reason_codes = ["already_minimized"]
            return text, classified
        raise ClassifierUnavailableError("minimize_without_transformation")
    return safe, classified


def govern_outbound_content(
    content: Any,
    *,
    purpose: str,
    content_type: str = CONTENT_TYPE_TEXT,
    context_metadata: dict[str, Any] | None = None,
    agent: str = "",
    provider: str = "external",
    alias_session: CleitonAiAliasSession | None = None,
) -> GovernanceResult:
    """
    Contrato central: purpose + content_type + content + metadata → decisão segura.
    Não registra valores sensíveis.
    """
    started = time.monotonic()
    meta = dict(context_metadata or {})
    session = alias_session or CleitonAiAliasSession(usuario_id=_scope_usuario_id())
    resolved_purpose = (purpose or "").strip()

    if resolved_purpose not in SUPPORTED_PURPOSES:
        result = _invalid_block("unsupported_purpose")
        latency_ms = int((time.monotonic() - started) * 1000)
        result.diagnostic_metadata.update(
            {
                "classifier": CLASSIFIER_NAME,
                "purpose": resolved_purpose or "-",
                "content_type": content_type,
                "latency_ms": latency_ms,
                "agent": agent,
                "provider": provider,
            }
        )
        _emit_governance_log(
            agent=agent or "unknown",
            purpose=resolved_purpose or "-",
            content_type=content_type,
            result=result,
            provider=provider,
            latency_ms=latency_ms,
            error_technical="unsupported_purpose",
        )
        return result

    try:
        result = _govern_payload(
            content,
            purpose=resolved_purpose,
            content_type=content_type,
            session=session,
        )
    except ClassifierUnavailableError:
        logger.error(
            "Cleiton AI governance: classifier failure type=%s stage=classify purpose=%s "
            "provider=%s status=blocked",
            ClassifierUnavailableError.__name__,
            resolved_purpose,
            provider,
        )
        result = _invalid_block("classifier_unavailable")
    except Exception as exc:
        logger.error(
            "Cleiton AI governance: classifier failure type=%s stage=classify purpose=%s "
            "provider=%s status=blocked",
            exc.__class__.__name__,
            resolved_purpose,
            provider,
        )
        result = _invalid_block("classifier_exception")

    if result.decision not in {DECISION_ALLOW, DECISION_MINIMIZE, DECISION_BLOCK}:
        result = _invalid_block("invalid_decision")
    if result.decision != DECISION_BLOCK and result.safe_content is None:
        result = _invalid_block("missing_safe_content")
    if not governance_result_is_coherent(result, original=content):
        result = _invalid_block("inconsistent_governance_result")

    latency_ms = int((time.monotonic() - started) * 1000)
    result.issued_by_cleiton = True
    result.diagnostic_metadata.update(
        {
            "classifier": CLASSIFIER_NAME,
            "purpose": resolved_purpose,
            "content_type": content_type,
            "latency_ms": latency_ms,
            "agent": agent,
            "provider": provider,
            "usuario_scope": session.usuario_id,
            "alias_count": session.mapping_size(),
            **{k: meta[k] for k in meta if k in {"request_id", "correlation_id", "doc_type"}},
        }
    )
    result.aliases = dict(session.aliases_issued)
    _emit_governance_log(
        agent=agent or "unknown",
        purpose=resolved_purpose,
        content_type=content_type,
        result=result,
        provider=provider,
        latency_ms=latency_ms,
        error_technical=";".join(result.reason_codes) if result.decision == DECISION_BLOCK else None,
    )
    return result


def _merge_classification(into: GovernanceResult, classified: ClassificationResult) -> None:
    if classified.decision == DECISION_BLOCK:
        into.decision = DECISION_BLOCK
        into.safe_content = None
    elif classified.decision == DECISION_MINIMIZE and into.decision == DECISION_ALLOW:
        into.decision = DECISION_MINIMIZE
    for cat in classified.categories_detected:
        if cat not in into.categories_detected:
            into.categories_detected.append(cat)
        if classified.decision == DECISION_MINIMIZE and cat not in into.categories_removed:
            into.categories_removed.append(cat)
    for code in classified.reason_codes:
        if code not in into.reason_codes:
            into.reason_codes.append(code)


def _sync_session_aliases(acc: GovernanceResult, session: CleitonAiAliasSession) -> None:
    if acc.decision == DECISION_BLOCK:
        return
    if session.mapping_size() <= 0:
        return
    if acc.decision == DECISION_ALLOW:
        acc.decision = DECISION_MINIMIZE
        if "structured_fields_minimized" not in acc.reason_codes:
            acc.reason_codes.append("structured_fields_minimized")
    for cat in session.aliases_issued:
        if cat not in acc.categories_detected:
            acc.categories_detected.append(cat)
        if cat not in acc.categories_removed:
            acc.categories_removed.append(cat)


def _govern_payload(
    content: Any,
    *,
    purpose: str,
    content_type: str,
    session: CleitonAiAliasSession,
) -> GovernanceResult:
    if content_type == CONTENT_TYPE_PROVIDER_CONFIG:
        return _govern_structured_config(content, purpose=purpose, session=session)
    if content_type == CONTENT_TYPE_GENERATE_CONTENTS or isinstance(content, list):
        return _govern_generate_contents(content, purpose=purpose, session=session)
    if isinstance(content, dict):
        return _govern_structured(content, purpose=purpose, session=session)
    if isinstance(content, (bytes, bytearray, memoryview)):
        return _invalid_block("raw_bytes_not_authorized")
    if content is None:
        return GovernanceResult(
            decision=DECISION_ALLOW,
            safe_content="",
            reason_codes=["empty_content"],
            issued_by_cleiton=True,
        )
    text = str(content)
    safe, classified = _minimize_text(text, session, purpose)
    if classified.decision == DECISION_BLOCK:
        return GovernanceResult(
            decision=DECISION_BLOCK,
            safe_content=None,
            categories_detected=list(classified.categories_detected),
            reason_codes=list(classified.reason_codes),
            issued_by_cleiton=True,
        )
    decision = classified.decision if classified.decision in {DECISION_ALLOW, DECISION_MINIMIZE} else DECISION_BLOCK
    return GovernanceResult(
        decision=decision,
        safe_content=safe,
        categories_detected=list(classified.categories_detected),
        categories_removed=list(classified.categories_detected) if decision == DECISION_MINIMIZE else [],
        reason_codes=list(classified.reason_codes),
        issued_by_cleiton=True,
    )


def _govern_structured(
    payload: dict,
    *,
    purpose: str,
    session: CleitonAiAliasSession,
) -> GovernanceResult:
    acc = GovernanceResult(decision=DECISION_ALLOW, safe_content=None, issued_by_cleiton=True)

    def _min_text(value: str, alias_session: CleitonAiAliasSession) -> str:
        safe, classified = _minimize_text(value, alias_session, purpose)
        _merge_classification(acc, classified)
        if acc.decision == DECISION_BLOCK:
            return value
        return safe

    copied = copy_and_minimize_structured(payload, session=session, text_minimizer=_min_text)
    if acc.decision == DECISION_BLOCK:
        acc.safe_content = None
        return acc
    _sync_session_aliases(acc, session)
    acc.safe_content = copied
    return acc


def _govern_generate_contents(
    contents: Any,
    *,
    purpose: str,
    session: CleitonAiAliasSession,
) -> GovernanceResult:
    if isinstance(contents, str):
        return _govern_payload(contents, purpose=purpose, content_type=CONTENT_TYPE_TEXT, session=session)
    if not isinstance(contents, list):
        return _invalid_block("unsupported_generate_contents")

    acc = GovernanceResult(decision=DECISION_ALLOW, safe_content=[], issued_by_cleiton=True)
    safe_items: list[Any] = []
    dropped_file_parts = 0
    for item in contents:
        if isinstance(item, str):
            safe, classified = _minimize_text(item, session, purpose)
            _merge_classification(acc, classified)
            if acc.decision == DECISION_BLOCK:
                acc.safe_content = None
                return acc
            safe_items.append(safe)
            continue
        dropped_file_parts += 1
    if dropped_file_parts:
        acc.reason_codes.append("original_file_parts_stripped")
        if acc.decision == DECISION_ALLOW:
            acc.decision = DECISION_MINIMIZE
    if not any(isinstance(item, str) and item.strip() for item in safe_items):
        return _invalid_block("no_authorized_text_after_file_strip")
    acc.safe_content = safe_items[0] if len(safe_items) == 1 else safe_items
    return acc


def _is_technical_scalar(value: Any) -> bool:
    return isinstance(value, (int, float, bool)) or value is None


def _govern_config_node(
    node: Any,
    *,
    purpose: str,
    session: CleitonAiAliasSession,
    acc: GovernanceResult,
    parent_key: str = "",
) -> Any:
    if _is_technical_scalar(node):
        return node
    if parent_key in _CONFIG_PASSTHROUGH_KEYS:
        return node
    if isinstance(node, dict):
        out: dict[Any, Any] = {}
        for key, value in node.items():
            key_s = key if isinstance(key, str) else ""
            if key_s in _CONFIG_PASSTHROUGH_KEYS:
                out[key] = value
                continue
            out[key] = _govern_config_node(
                value,
                purpose=purpose,
                session=session,
                acc=acc,
                parent_key=key_s,
            )
            if acc.decision == DECISION_BLOCK:
                return out
        return out
    if isinstance(node, list):
        return [
            _govern_config_node(item, purpose=purpose, session=session, acc=acc, parent_key=parent_key)
            for item in node
        ]
    if isinstance(node, tuple):
        return tuple(
            _govern_config_node(item, purpose=purpose, session=session, acc=acc, parent_key=parent_key)
            for item in node
        )
    if isinstance(node, str):
        if parent_key in _CONFIG_PASSTHROUGH_KEYS:
            return node
        if parent_key in _CONFIG_TEXT_KEYS or parent_key == "" or parent_key not in _CONFIG_PASSTHROUGH_KEYS:
            safe, classified = _minimize_text(node, session, purpose)
            _merge_classification(acc, classified)
            if acc.decision == DECISION_BLOCK:
                return node
            return safe
        return node
    return node


def _govern_structured_config(
    config: Any,
    *,
    purpose: str,
    session: CleitonAiAliasSession,
) -> GovernanceResult:
    acc = GovernanceResult(decision=DECISION_ALLOW, safe_content=None, issued_by_cleiton=True)
    if config is None:
        acc.safe_content = None
        acc.reason_codes.append("empty_config")
        return acc
    if isinstance(config, str):
        safe, classified = _minimize_text(config, session, purpose)
        _merge_classification(acc, classified)
        if acc.decision == DECISION_BLOCK:
            acc.safe_content = None
            return acc
        acc.safe_content = safe
        return acc
    if isinstance(config, dict):
        acc.safe_content = _govern_config_node(
            config, purpose=purpose, session=session, acc=acc, parent_key=""
        )
        _sync_session_aliases(acc, session)
        if acc.decision == DECISION_BLOCK:
            acc.safe_content = None
        return acc
    try:
        cloned = copy.copy(config)
    except Exception:
        return _invalid_block("config_copy_failed")
    mutated = False
    for attr in _CONFIG_TEXT_ATTRS:
        if not hasattr(cloned, attr):
            continue
        value = getattr(cloned, attr)
        if isinstance(value, str) and value:
            safe, classified = _minimize_text(value, session, purpose)
            _merge_classification(acc, classified)
            if acc.decision == DECISION_BLOCK:
                acc.safe_content = None
                return acc
            try:
                setattr(cloned, attr, safe)
                mutated = True
            except Exception:
                return _invalid_block("config_attr_locked")
        elif isinstance(value, (dict, list, tuple)):
            governed = _govern_config_node(
                value, purpose=purpose, session=session, acc=acc, parent_key=attr
            )
            if acc.decision == DECISION_BLOCK:
                acc.safe_content = None
                return acc
            try:
                setattr(cloned, attr, governed)
                mutated = True
            except Exception:
                return _invalid_block("config_attr_locked")
    _sync_session_aliases(acc, session)
    acc.safe_content = cloned if mutated or acc.decision == DECISION_ALLOW else cloned
    return acc


def govern_provider_config(
    config: Any,
    *,
    purpose: str,
    agent: str = "",
    provider: str = "external",
    alias_session: CleitonAiAliasSession | None = None,
) -> Any:
    """Governa conteúdo textual/sensível em config sem serializar parâmetros técnicos."""
    if config is None:
        return None
    result = govern_outbound_content(
        config,
        purpose=purpose,
        content_type=CONTENT_TYPE_PROVIDER_CONFIG,
        agent=agent,
        provider=provider,
        alias_session=alias_session,
    )
    if result.decision == DECISION_BLOCK:
        raise CleitonAiGovernanceBlockedError(
            USER_SAFE_PREPARATION_FAILED,
            reason_codes=result.reason_codes,
            categories_detected=result.categories_detected,
        )
    return result.safe_content


def govern_or_raise(
    content: Any,
    *,
    purpose: str,
    content_type: str = CONTENT_TYPE_TEXT,
    agent: str = "",
    provider: str = "external",
    alias_session: CleitonAiAliasSession | None = None,
    context_metadata: dict[str, Any] | None = None,
) -> GovernanceResult:
    result = govern_outbound_content(
        content,
        purpose=purpose,
        content_type=content_type,
        context_metadata=context_metadata,
        agent=agent,
        provider=provider,
        alias_session=alias_session,
    )
    if result.decision == DECISION_BLOCK:
        raise CleitonAiGovernanceBlockedError(
            USER_SAFE_PREPARATION_FAILED,
            reason_codes=result.reason_codes,
            categories_detected=result.categories_detected,
        )
    return result


def safe_outbound_document_text(
    text: str,
    *,
    purpose: str,
    agent: str,
    alias_session: CleitonAiAliasSession | None = None,
) -> tuple[str, bool]:
    """Minimiza texto documental para o provider. True = bloqueado (não usar original)."""
    raw = text or ""
    if not raw.strip():
        return raw, False
    try:
        governed = govern_or_raise(
            raw,
            purpose=purpose,
            content_type=CONTENT_TYPE_DOCUMENT,
            agent=agent,
            provider="gemini",
            alias_session=alias_session,
        )
        return str(governed.safe_content), False
    except CleitonAiGovernanceBlockedError:
        return "", True


def govern_history_messages(
    history: list | None,
    *,
    purpose: str,
    agent: str = "",
    alias_session: CleitonAiAliasSession | None = None,
) -> list[dict[str, str]]:
    """Governa cada item de histórico. Entrada não confiável."""
    session = alias_session or CleitonAiAliasSession(usuario_id=_scope_usuario_id())
    out: list[dict[str, str]] = []
    for msg in list(history or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = str(msg.get("content") or "")
        if not content.strip():
            continue
        governed = govern_or_raise(
            content,
            purpose=purpose,
            content_type=CONTENT_TYPE_HISTORY,
            agent=agent,
            provider="external",
            alias_session=session,
        )
        out.append({"role": role, "content": str(governed.safe_content)})
    return out
