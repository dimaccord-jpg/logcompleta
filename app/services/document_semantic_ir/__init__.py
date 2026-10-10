"""Vertical slice do Document Semantic IR. Capacidade nova, sem cutover."""
from app.services.document_semantic_ir.dispatch import dispatch_structural_ir, serialize_safe_ir
from app.services.document_semantic_ir.extract import extract_document_semantic_ir
from app.services.document_semantic_ir.structure import (
    IR_VERSION,
    DocumentSemanticIrError,
    StructuralDispatch,
    StructuralExtraction,
)

__all__ = [
    "IR_VERSION",
    "DocumentSemanticIrError",
    "StructuralDispatch",
    "StructuralExtraction",
    "dispatch_structural_ir",
    "extract_document_semantic_ir",
    "serialize_safe_ir",
]
