"""Adaptador da fachada billable. Não chama o provider direto."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.semantic_question_execution.constants import (
    AGENT_SEMANTIC_QUESTION_EXECUTION,
    FLOW_TYPE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION,
    PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION,
    REAL_MAX_OUTPUT_TOKENS,
)


def call_municipality_disambiguation(
    client: Any,
    *,
    model: str,
    contents: Any,
    config: Any,
    attempt_key: str,
    usuario: Any,
    max_reserved_credits: Decimal,
    max_output_tokens: int = REAL_MAX_OUTPUT_TOKENS,
) -> Any:
    from app.services.cleiton_billable_ai_call import cleiton_governed_billable_ai_call

    return cleiton_governed_billable_ai_call(
        client,
        model=model,
        contents=contents,
        config=config,
        agent=AGENT_SEMANTIC_QUESTION_EXECUTION,
        flow_type=FLOW_TYPE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION,
        api_key_label="semantic_question_execution",
        provider="gemini",
        usuario=usuario,
        attempt_key=attempt_key,
        purpose=PURPOSE_SEMANTIC_MUNICIPALITY_DISAMBIGUATION,
        max_output_tokens=max_output_tokens,
        max_reserved_credits=max_reserved_credits,
    )
