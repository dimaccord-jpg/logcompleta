"""Schema estruturado da decisão semântica. O validador local continua obrigatório."""
from __future__ import annotations

from app.services.semantic_question_execution.constants import (
    CONFIDENCE_VALUES,
    DECISION_STATUSES,
    OUTPUT_SCHEMA_VERSION,
    RATIONALE_CODES,
    SemanticExecutionError,
)


def decision_response_schema():
    """Schema do SDK. Falha fechada se a versão instalada não materializar o contrato."""
    try:
        from google.genai import types
    except Exception as exc:
        raise SemanticExecutionError("response_schema_unavailable") from exc
    try:
        decision = types.Schema(
            type="OBJECT",
            additional_properties=False,
            required=[
                "schema_version",
                "question_id",
                "status",
                "selected_candidate_id",
                "evidence_refs",
                "rationale_code",
                "confidence",
            ],
            property_ordering=[
                "schema_version",
                "question_id",
                "status",
                "selected_candidate_id",
                "evidence_refs",
                "rationale_code",
                "confidence",
            ],
            properties={
                "schema_version": types.Schema(type="STRING", enum=[OUTPUT_SCHEMA_VERSION]),
                "question_id": types.Schema(type="STRING"),
                "status": types.Schema(type="STRING", enum=list(DECISION_STATUSES)),
                "selected_candidate_id": types.Schema(type="STRING", nullable=True),
                "evidence_refs": types.Schema(
                    type="ARRAY",
                    items=types.Schema(type="STRING"),
                ),
                "rationale_code": types.Schema(type="STRING", enum=list(RATIONALE_CODES)),
                "confidence": types.Schema(type="STRING", enum=list(CONFIDENCE_VALUES)),
            },
        )
        return types.Schema(
            type="OBJECT",
            additional_properties=False,
            required=["decisions"],
            properties={
                "decisions": types.Schema(
                    type="ARRAY",
                    min_items=1,
                    max_items=1,
                    items=decision,
                )
            },
        )
    except SemanticExecutionError:
        raise
    except Exception as exc:
        raise SemanticExecutionError("response_schema_unsupported") from exc
