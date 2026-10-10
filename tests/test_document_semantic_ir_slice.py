"""SCRUM-146: vertical slice do Document Semantic IR. Sem provider real."""
from __future__ import annotations

import copy
import io
import json
import logging
import threading
import tracemalloc
import zipfile
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.cleiton_ai_privacy_classifier import ClassifierUnavailableError
from app.services.document_semantic_ir import (
    IR_VERSION,
    dispatch_structural_ir,
    extract_document_semantic_ir,
)
from app.services.document_semantic_ir.structure import (
    FORBIDDEN_SEMANTIC_KEYS,
    LIMITATION_CELL_CAP,
    LIMITATION_MERGE_CAP,
    LIMITATION_RESOURCE,
    LIMITATION_TABLE,
    LIMITATION_TEXT_CAP,
    LIMITATION_VISUAL,
    DocumentSemanticIrError,
    render_document_scalar,
)
from tests.document_semantic_ir_fixtures import (
    CACHED_FORMULA_RESULT,
    FORMULA,
    MIXED_PDF_TEXT,
    NUMERIC_BANK,
    NUMERIC_CARD,
    NUMERIC_CNPJ,
    NUMERIC_CPF,
    NUMERIC_CPF_DECIMAL,
    NUMERIC_MONEY,
    NUMERIC_WEIGHT,
    SIDE_NOTE,
    SUL_COMMENT,
    SUL_MERGE_TEXT,
    SYNTHETIC_CNPJ,
    SYNTHETIC_EMAIL,
    attach_orphan_chartsheet,
    build_aliased_merge_xlsx,
    build_entity_worksheet_xlsx,
    build_giant_merge_xlsx,
    build_grouped_hidden_xlsx,
    build_isolated_line_pdf,
    build_long_sheet_name_xlsx,
    build_many_sheets_xlsx,
    build_many_small_merges_xlsx,
    build_mixed_visual_pdf,
    build_oversized_grid_xlsx,
    build_precise_number_xlsx,
    build_precision_xlsx,
    build_repeated_number_xlsx,
    build_sensitive_numeric_xlsx,
    build_shared_text_budget_xlsx,
    build_small_merge_xlsx,
    build_sparse_corners_xlsx,
    build_tariff_pdf,
    build_tariff_xlsx,
    build_three_long_text_sheets_xlsx,
    build_traversal_worksheet_xlsx,
    build_visual_only_pdf,
    duplicate_worksheet_target,
    remove_sheet_relationship_id,
    rename_worksheet_part,
    replace_zip_member,
    retype_worksheet_relationship,
    retarget_worksheet,
    rewrite_sheet_relationship_id,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "app" / "services" / "document_semantic_ir"


class _FakeModels:
    def __init__(self) -> None:
        self.calls = []
        self.count_calls = []

    def count_tokens(self, *, model, contents, config=None):
        self.count_calls.append({"model": model})
        return SimpleNamespace(total_tokens=8, cached_content_token_count=None)

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        usage = SimpleNamespace(prompt_token_count=4, candidates_token_count=4, total_token_count=8)
        return SimpleNamespace(text="ok", usage_metadata=usage)


class _FakeClient:
    """Cliente permitido pelo gate: retry efetivo 1 e count_tokens inspecionável."""

    def __init__(self) -> None:
        self.models = _FakeModels()
        self.http_options = SimpleNamespace(retry_options=SimpleNamespace(attempts=1))


@pytest.fixture(autouse=True)
def _structural_ir_system_gate(app):
    """O dispatch passa pelo gate. O slice usa origem de sistema explícita, sem débito de cliente."""
    from tests.conftest import seed_cleiton_cost_config, seed_sistema_interno

    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        yield


def _dispatch(extraction, client=None):
    fake = client or _FakeClient()
    outcome = dispatch_structural_ir(
        fake,
        extraction.ir,
        model="structural-ir-slice-test",
        agent="agente_compara",
        flow_type="agente_compara_document_semantic_ir",
        api_key_label="slice-test",
        extraction_metrics=extraction.metrics,
        origem_sistema=True,
    )
    return fake, outcome


def _blocks(ir):
    for page in ir["pages_or_sheets"]:
        for block in page.get("blocks") or []:
            yield page, block


def _cells(ir):
    for _, block in _blocks(ir):
        for cell in block.get("cells") or []:
            yield block, cell


def _ids(ir):
    found = []
    for page in ir["pages_or_sheets"]:
        if page.get("sheet_id"):
            found.append(page["sheet_id"])
        for block in page.get("blocks") or []:
            found.append(block["id"])
            for cell in block.get("cells") or []:
                found.append(cell["id"])
    return found


def _bboxes(ir):
    found = []
    for _, block in _blocks(ir):
        found.append(block["bbox"])
        for cell in block.get("cells") or []:
            found.append(cell["bbox"])
    return found


def _sheet(ir, name):
    return next(page for page in ir["pages_or_sheets"] if page.get("sheet_name") == name)


def _cell_by_coordinate(ir, sheet_name, coordinate):
    sheet = _sheet(ir, sheet_name)
    for block in sheet["blocks"]:
        for cell in block["cells"]:
            if cell.get("coordinate") == coordinate:
                return block, cell
    raise AssertionError(coordinate)


def _pdf_ir():
    return extract_document_semantic_ir(build_tariff_pdf(), file_format="pdf")


def _xlsx_ir():
    return extract_document_semantic_ir(build_tariff_xlsx(), file_format="xlsx")


def test_pdf_ir_is_versioned_and_keeps_page_geometry():
    extraction = _pdf_ir()
    ir = extraction.ir
    assert ir["document"]["format"] == "pdf"
    assert ir["document"]["ir_version"] == IR_VERSION
    assert ir["document"]["source_fingerprint_local"].startswith("sha256:")
    assert len(ir["document"]["source_fingerprint_local"]) == 7 + 64
    page = ir["pages_or_sheets"][0]
    assert page["kind"] == "page"
    assert page["page_number"] == 1
    assert page["rotation"] == 0
    assert page["dimensions"]["width"] == 612
    assert page["dimensions"]["height"] == 792
    assert page["blocks"]
    assert all("bbox" in block for block in page["blocks"])
    assert extraction.metrics["pages_or_sheets"] == 1
    assert extraction.metrics["blocks"] >= 3
    assert extraction.metrics["elapsed_ms"] >= 0


def test_pdf_keeps_sp_above_subcolumns_and_note_independent():
    ir = _pdf_ir().ir
    page = ir["pages_or_sheets"][0]
    table = next(block for block in page["blocks"] if block["type"] == "table")
    header = next(block for block in page["blocks"] if block.get("text") == "SP")
    note = next(block for block in page["blocks"] if SIDE_NOTE in block.get("text", ""))
    texts = [cell["text"] for cell in table["cells"]]
    assert "Capital" in texts
    assert "Interior I" in texts
    assert "Interior II" in texts
    assert header["type"] == "header"
    assert header["bbox"]["y1"] <= table["bbox"]["y0"]
    assert any(rel["kind"] == "above" and rel["target_id"] == table["id"] for rel in header["relationships"])
    capital = next(cell for cell in table["cells"] if cell["text"] == "Capital")
    interior_i = next(cell for cell in table["cells"] if cell["text"] == "Interior I")
    interior_ii = next(cell for cell in table["cells"] if cell["text"] == "Interior II")
    value = next(cell for cell in table["cells"] if cell["text"] == "12,50")
    assert capital["bbox"]["x0"] < interior_i["bbox"]["x0"] < interior_ii["bbox"]["x0"]
    assert capital["column"] == value["column"] == 1
    assert capital["bbox"]["y0"] < value["bbox"]["y0"]
    assert interior_i["column"] == 2
    assert next(cell["column"] for cell in table["cells"] if cell["text"] == "18,00") == 2
    assert interior_ii["column"] == 3
    assert note["id"] != table["id"]
    assert note["type"] == "note"
    assert note["bbox"]["x0"] > table["bbox"]["x1"]
    assert any(rel["kind"] == "adjacent" and rel["target_id"] == table["id"] for rel in note["relationships"])
    assert SIDE_NOTE not in table.get("text", "")


def test_pdf_does_not_infer_commercial_rules():
    ir = _pdf_ir().ir
    blob = json.dumps(ir)
    for key in FORBIDDEN_SEMANTIC_KEYS:
        assert key not in blob
    kinds = {
        rel["kind"]
        for _, block in _blocks(ir)
        for rel in list(block.get("relationships") or []) + [rel for cell in block["cells"] for rel in cell["relationships"]]
    }
    assert kinds <= {"above", "below", "adjacent", "inside", "merged_over", "spans_width", "same_block"}
    assert "weight_rule" not in blob
    assert "pricing_dimension" not in blob


def test_pdf_without_ruled_table_does_not_invent_one():
    extraction = extract_document_semantic_ir(build_tariff_pdf(draw_lines=False), file_format="pdf")
    ir = extraction.ir
    assert all(block["type"] != "table" for _, block in _blocks(ir))
    assert LIMITATION_TABLE in ir["document"]["coverage"]["limitations"]
    assert ir["document"]["coverage"]["complete"] is False
    texts = [cell["text"] for _, cell in _cells(ir)]
    assert "Capital" in texts
    assert "SP" in texts
    assert "12,50" in texts


def test_visual_page_is_not_complete_and_skips_ocr():
    extraction = extract_document_semantic_ir(build_visual_only_pdf(), file_format="pdf")
    ir = extraction.ir
    coverage = ir["document"]["coverage"]
    assert coverage["complete"] is False
    assert coverage["visual_ununderstood_pages"] == [1]
    assert LIMITATION_VISUAL in coverage["limitations"]
    assert [block["type"] for _, block in _blocks(ir)] == ["unknown"]
    blob = json.dumps(ir).lower()
    assert "ocr" not in blob
    assert "base64" not in blob


def test_xlsx_ir_keeps_sheets_merge_comment_coordinates_and_formula():
    extraction = _xlsx_ir()
    ir = extraction.ir
    assert ir["document"]["format"] == "xlsx"
    assert ir["document"]["ir_version"] == IR_VERSION
    assert list(ir.keys()) == ["document", "pages_or_sheets"]
    names = [page["sheet_name"] for page in ir["pages_or_sheets"]]
    assert names == ["SP", "Sul"]
    assert [page["sheet_id"] for page in ir["pages_or_sheets"]] == ["s1", "s2"]
    assert "SP" not in ir
    sul = _sheet(ir, "Sul")
    assert {"range": "A1:C1", "anchor": "A1"} in sul["merges"]
    _, anchor = _cell_by_coordinate(ir, "Sul", "A1")
    assert anchor["value"] == SUL_MERGE_TEXT
    assert any(
        rel["kind"] == "merged_over" and rel["range"] == "A1:C1" and rel["anchor"] == "A1"
        for rel in anchor["relationships"]
    )
    assert any(
        rel["kind"] == "spans_width" and rel["columns"] == 3
        for block in sul["blocks"]
        for rel in block["relationships"]
    )
    _, commented = _cell_by_coordinate(ir, "Sul", "B3")
    assert commented["comment"] == SUL_COMMENT
    assert commented["coordinate"] == "B3"
    assert commented["value"] == 41
    _, formula = _cell_by_coordinate(ir, "Sul", "D3")
    assert formula["formula"] == FORMULA
    assert formula["cached_value"] == CACHED_FORMULA_RESULT
    assert formula["value"] is None
    assert formula["value_type"] == "formula"
    assert formula["formula"] != str(formula["cached_value"])
    sp = _sheet(ir, "SP")
    _, zero = _cell_by_coordinate(ir, "SP", "C3")
    assert zero["value"] == 0
    assert zero["value_type"] == "number"
    assert zero["style_ref"]["number_format"] == "0.00"
    coordinates = [cell["coordinate"] for block in sp["blocks"] for cell in block["cells"]]
    assert "A4" not in coordinates
    assert {"axis": "row", "index": 4} in sp["gaps"]
    assert 6 in sp["hidden_rows"]
    assert "E" in sp["hidden_columns"]
    assert all(cell.get("row") != 6 for block in sp["blocks"] for cell in block["cells"])
    lowered = json.dumps(ir).lower()
    assert "tarifa_invalida" not in lowered
    assert "invalida" not in lowered
    assert extraction.metrics["merges"] == 1
    assert extraction.metrics["comments"] == 1
    assert extraction.metrics["pages_or_sheets"] == 2
    assert extraction.metrics["cells"] < 40


def test_governance_minimizes_sensitive_values_and_keeps_structure():
    extraction = _xlsx_ir()
    raw = json.loads(json.dumps(extraction.ir))
    client, outcome = _dispatch(extraction)
    assert outcome.provider_called is True
    assert client.models.calls
    sent = client.models.calls[0]["contents"]
    assert isinstance(sent, str)
    parsed = json.loads(sent)
    assert SYNTHETIC_EMAIL not in sent
    assert SYNTHETIC_CNPJ not in sent
    assert "[EMAIL_" in sent
    assert "EMPRESA" in sent
    assert _ids(raw) == _ids(parsed)
    assert _bboxes(raw) == _bboxes(parsed)
    raw_sheet = _sheet(raw, "Sul")
    safe_sheet = _sheet(parsed, "Sul")
    assert raw_sheet["merges"] == safe_sheet["merges"]
    assert raw_sheet["sheet_name"] == "Sul"
    assert safe_sheet["sheet_name"] == "Sul"
    _, email_cell = _cell_by_coordinate(parsed, "SP", "A5")
    _, cnpj_cell = _cell_by_coordinate(parsed, "SP", "B5")
    assert email_cell["coordinate"] == "A5"
    assert cnpj_cell["coordinate"] == "B5"
    assert email_cell["row"] == 5
    assert email_cell["column"] == 1
    assert SYNTHETIC_EMAIL in json.dumps(extraction.ir)
    assert "operacao@sintetico.example" not in json.dumps(outcome.metrics)
    assert outcome.safe_json is not None
    assert SYNTHETIC_EMAIL not in outcome.safe_json
    assert json.loads(outcome.safe_json) == parsed


def test_alias_mapping_is_not_persisted_or_returned():
    extraction = _xlsx_ir()
    _, outcome = _dispatch(extraction)
    assert set(outcome.__dataclass_fields__) == {
        "provider_called",
        "decision",
        "safe_json",
        "metrics",
        "reason_codes",
        "response",
    }
    blob = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py"))
    assert "json.dump(" not in blob
    assert "sqlite" not in blob
    assert "pickle" not in blob
    assert ".write_text" not in blob
    assert "alias_session" not in blob
    assert SYNTHETIC_EMAIL not in (outcome.safe_json or "")
    assert all(SYNTHETIC_EMAIL not in code for code in outcome.reason_codes)


def test_classifier_failure_makes_zero_provider_calls(monkeypatch):
    def boom(*_args, **_kwargs):
        raise ClassifierUnavailableError("forced")

    monkeypatch.setattr("app.services.cleiton_ai_data_governance.classify_text", boom)
    client = _FakeClient()
    _, outcome = _dispatch(_xlsx_ir(), client)
    assert outcome.provider_called is False
    assert client.models.calls == []
    assert "classifier_unavailable" in outcome.reason_codes
    assert outcome.safe_json is None


def test_sent_payload_has_no_bytes_base64_or_path():
    extraction = _pdf_ir()
    client, outcome = _dispatch(extraction)
    sent = client.models.calls[0]["contents"]
    assert isinstance(sent, str)
    assert "%PDF" not in sent
    assert "base64" not in sent.lower()
    assert "data:image" not in sent.lower()
    assert "data:application" not in sent.lower()
    assert "file://" not in sent
    assert ":\\\\" not in sent
    assert "/Users/" not in sent
    assert "/home/" not in sent
    assert outcome.metrics["serialized_bytes"] == len(outcome.safe_json.encode("utf-8"))
    assert outcome.metrics["chars_safe"] == len(outcome.safe_json)
    assert SYNTHETIC_EMAIL not in sent
    assert SYNTHETIC_CNPJ not in sent
    parsed = json.loads(sent)
    assert parsed["document"]["ir_version"] == IR_VERSION
    assert _bboxes(extraction.ir) == _bboxes(parsed)
    assert "SP" in sent
    assert "Capital" in sent
    assert "Interior I" in sent
    assert "Interior II" in sent
    assert SIDE_NOTE in sent
    fingerprint = extraction.ir["document"]["source_fingerprint_local"]
    assert "source_fingerprint_local" not in sent
    assert fingerprint not in sent
    assert fingerprint.split(":", 1)[1] not in sent
    assert json.loads(outcome.safe_json) == parsed
    assert extraction.ir["document"]["coverage"]["complete"] is True
    assert LIMITATION_VISUAL not in extraction.ir["document"]["coverage"]["limitations"]


def test_slice_records_safe_metrics_and_approximate_memory():
    tracemalloc.start()
    extraction = _xlsx_ir()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    client, outcome = _dispatch(extraction)
    assert peak > 0
    metrics = outcome.metrics
    for key in ("pages_or_sheets", "blocks", "cells", "tables", "merges", "comments", "chars_safe", "serialized_bytes", "elapsed_ms"):
        assert key in metrics
        assert isinstance(metrics[key], int)
        assert metrics[key] >= 0
    assert SYNTHETIC_EMAIL not in json.dumps(metrics)
    assert SUL_COMMENT not in json.dumps(metrics)
    assert client.models.calls


def test_dispatch_does_not_log_document_content(caplog):
    caplog.set_level(logging.DEBUG)
    _dispatch(_xlsx_ir())
    blob = caplog.text
    assert SYNTHETIC_EMAIL not in blob
    assert SYNTHETIC_CNPJ not in blob
    assert SUL_COMMENT not in blob
    assert SIDE_NOTE not in blob


def test_forbidden_semantic_key_blocks_provider_call():
    extraction = _pdf_ir()
    tampered = json.loads(json.dumps(extraction.ir))
    tampered["pages_or_sheets"][0]["blocks"][0]["weight_rule"] = {"kg": 500}
    client = _FakeClient()
    outcome = dispatch_structural_ir(
        client,
        tampered,
        model="structural-ir-slice-test",
        extraction_metrics=extraction.metrics,
    )
    assert outcome.provider_called is False
    assert client.models.calls == []
    assert "semantic_inference" in outcome.reason_codes


def test_slice_is_isolated_from_pricing_and_rendering():
    blob = "\n".join(path.read_text(encoding="utf-8") for path in PACKAGE.glob("*.py"))
    for banned in (
        "pricing_contract",
        "_compile_pricing_contract",
        "_pricing_rule_lookup_candidates",
        "to_image",
        "camelot",
        "tabula",
        "pytesseract",
        "import fitz",
        "easyocr",
    ):
        assert banned not in blob
    dispatch_src = (PACKAGE / "dispatch.py").read_text(encoding="utf-8")
    assert "cleiton_governed_billable_ai_call" in dispatch_src
    assert "generate_content" not in dispatch_src
    assert 'vertical_strategy": "lines"' in (PACKAGE / "pdf_slice.py").read_text(encoding="utf-8")
    for relative in (
        "app/agente_compara_doc_service.py",
        "app/agente_compara_calculation_service.py",
        "app/agente_compara_routes.py",
        "app/agente_compara_api_routes.py",
    ):
        assert "document_semantic_ir" not in (ROOT / relative).read_text(encoding="utf-8")


def test_document_scalars_render_without_trailing_decimal_zero():
    assert render_document_scalar(NUMERIC_CPF) == str(NUMERIC_CPF)
    assert render_document_scalar(NUMERIC_CPF_DECIMAL) == str(int(NUMERIC_CPF_DECIMAL))
    assert render_document_scalar(NUMERIC_CPF_DECIMAL) != "52998224725.0"
    assert render_document_scalar(0) == "0"
    assert render_document_scalar(0.0) == "0"
    assert render_document_scalar(NUMERIC_MONEY) == "10.5"
    assert render_document_scalar(NUMERIC_WEIGHT) == "7.25"
    assert render_document_scalar(Decimal("10.50")) == "10.50"
    assert render_document_scalar(Decimal(str(int(NUMERIC_CPF_DECIMAL)))) == str(int(NUMERIC_CPF_DECIMAL))
    assert render_document_scalar(None) is None
    assert render_document_scalar(float("nan")) is None


def test_numeric_cell_content_stays_out_of_the_provider_payload(monkeypatch, caplog):
    extraction = extract_document_semantic_ir(build_sensitive_numeric_xlsx(), file_format="xlsx")
    raw_cpf = _cell_by_coordinate(extraction.ir, "Dados", "B1")[1]
    assert raw_cpf["value"] == NUMERIC_CPF
    assert raw_cpf["value_type"] == "number"
    assert _cell_by_coordinate(extraction.ir, "Dados", "B5")[1]["value"] == 0
    caplog.set_level(logging.DEBUG)
    client, outcome = _dispatch(extraction)
    assert outcome.provider_called is True
    assert len(client.models.calls) == 1
    sent = client.models.calls[0]["contents"]
    parsed = json.loads(sent)
    assert outcome.safe_json is not None
    assert json.loads(outcome.safe_json) == parsed
    for secret in (str(NUMERIC_CPF), str(NUMERIC_CNPJ), str(NUMERIC_CARD), str(NUMERIC_BANK), "52998224725.0"):
        assert secret not in sent
        assert secret not in caplog.text
    money = _cell_by_coordinate(parsed, "Dados", "B3")[1]
    weight = _cell_by_coordinate(parsed, "Dados", "B4")[1]
    zero = _cell_by_coordinate(parsed, "Dados", "B5")[1]
    assert money["value_type"] == "number"
    assert money["value"] == "10.5"
    assert weight["value_type"] == "number"
    assert weight["value"] == "7.25"
    assert zero["value"] == "0"
    assert zero["value_type"] == "number"
    assert isinstance(money["row"], int) and isinstance(money["bbox"]["x0"], int)
    coordinates = [cell["coordinate"] for block in _sheet(parsed, "Dados")["blocks"] for cell in block["cells"]]
    assert "A8" not in coordinates
    assert "B8" not in coordinates
    assert _cell_by_coordinate(extraction.ir, "Dados", "B1")[1]["value"] == NUMERIC_CPF

    tampered = copy.deepcopy(extraction.ir)
    _cell_by_coordinate(tampered, "Dados", "B3")[1]["value"] = NUMERIC_CPF_DECIMAL
    decimal_client = _FakeClient()
    decimal_outcome = dispatch_structural_ir(
        decimal_client,
        tampered,
        model="structural-ir-slice-test",
        extraction_metrics=extraction.metrics,
        origem_sistema=True,
    )
    assert decimal_outcome.provider_called is True
    decimal_sent = decimal_client.models.calls[0]["contents"]
    assert json.loads(decimal_sent)
    assert json.loads(decimal_outcome.safe_json) == json.loads(decimal_sent)
    assert str(int(NUMERIC_CPF_DECIMAL)) not in decimal_sent
    assert "52998224725.0" not in decimal_sent

    def boom(*_args, **_kwargs):
        raise ClassifierUnavailableError("forced")

    monkeypatch.setattr("app.services.cleiton_ai_data_governance.classify_text", boom)
    blocked_client = _FakeClient()
    blocked = dispatch_structural_ir(
        blocked_client,
        extraction.ir,
        model="structural-ir-slice-test",
        extraction_metrics=extraction.metrics,
    )
    assert blocked.provider_called is False
    assert blocked_client.models.calls == []
    assert blocked.safe_json is None
    assert "classifier_unavailable" in blocked.reason_codes


def test_sparse_xlsx_does_not_materialize_the_rectangle():
    from openpyxl.cell.cell import Cell
    from openpyxl.worksheet.worksheet import Worksheet

    data = build_sparse_corners_xlsx()
    created = {"cells": 0, "lookups": 0}
    original_init = Cell.__init__
    original_cell = Worksheet.cell

    def wrapped_init(self, *args, **kwargs):
        created["cells"] += 1
        return original_init(self, *args, **kwargs)

    def wrapped_cell(self, *args, **kwargs):
        created["lookups"] += 1
        return original_cell(self, *args, **kwargs)

    Cell.__init__ = wrapped_init
    Worksheet.cell = wrapped_cell
    try:
        tracemalloc.start()
        extraction = extract_document_semantic_ir(data, file_format="xlsx")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        Cell.__init__ = original_init
        Worksheet.cell = original_cell
    coordinates = [cell["coordinate"] for _, cell in _cells(extraction.ir)]
    assert sorted(coordinates) == ["A1", "GR200"]
    rectangle = 200 * 200
    assert created["cells"] < 100
    assert created["cells"] * 50 < rectangle
    assert created["lookups"] < 25
    assert peak < 12 * 1024 * 1024


def test_hidden_column_group_keeps_the_full_range():
    extraction = extract_document_semantic_ir(build_grouped_hidden_xlsx(), file_format="xlsx")
    sheet = _sheet(extraction.ir, "Ocultas")
    assert sheet["hidden_columns"] == ["C:F"]
    assert "D" not in sheet["hidden_columns"]
    assert "hidden" not in _cell_by_coordinate(extraction.ir, "Ocultas", "A1")[1]
    assert _cell_by_coordinate(extraction.ir, "Ocultas", "C1")[1]["hidden"] is True
    assert _cell_by_coordinate(extraction.ir, "Ocultas", "F1")[1]["hidden"] is True


def test_resource_limits_mark_coverage_incomplete(monkeypatch):
    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_STRUCTURAL_CELLS", 1)
    capped = extract_document_semantic_ir(build_tariff_xlsx(), file_format="xlsx")
    assert capped.ir["document"]["coverage"]["complete"] is False
    assert LIMITATION_CELL_CAP in capped.ir["document"]["coverage"]["limitations"]

    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_STRUCTURAL_CELLS", 8000)
    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_SERIALIZED_TEXT", 5)
    shortened = extract_document_semantic_ir(build_tariff_xlsx(), file_format="xlsx")
    assert shortened.ir["document"]["coverage"]["complete"] is False
    assert LIMITATION_TEXT_CAP in shortened.ir["document"]["coverage"]["limitations"]
    shortened_blob = json.dumps(shortened.ir)
    assert SUL_COMMENT not in shortened_blob
    assert "Capital" not in shortened_blob
    assert _documentary_chars(shortened.ir) <= 5

    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_SERIALIZED_TEXT", 4000)
    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_MERGES", 0)
    unmerged = extract_document_semantic_ir(build_tariff_xlsx(), file_format="xlsx")
    assert unmerged.ir["document"]["coverage"]["complete"] is False
    assert LIMITATION_MERGE_CAP in unmerged.ir["document"]["coverage"]["limitations"]
    assert _sheet(unmerged.ir, "Sul")["merges"] == []


def test_mixed_pdf_coverage_is_not_complete_just_because_text_exists():
    extraction = extract_document_semantic_ir(build_mixed_visual_pdf(), file_format="pdf")
    coverage = extraction.ir["document"]["coverage"]
    assert coverage["complete"] is False
    assert coverage["visual_ununderstood_pages"] == [1]
    assert LIMITATION_VISUAL in coverage["limitations"]
    extracted = " ".join(cell["text"] for _, cell in _cells(extraction.ir))
    assert MIXED_PDF_TEXT in extracted
    assert "ocr" not in json.dumps(extraction.ir).lower()


def test_pdf_extractor_logs_do_not_include_document_text(caplog, monkeypatch):
    import pdfplumber

    real_open = pdfplumber.open

    def wrapped(*args, **kwargs):
        logging.getLogger("pdfminer.pdfinterp").debug("conteudo %s", SYNTHETIC_EMAIL)
        logging.getLogger("pdfplumber").error("falha %s", SYNTHETIC_CNPJ)
        return real_open(*args, **kwargs)

    monkeypatch.setattr("app.services.document_semantic_ir.pdf_slice.pdfplumber.open", wrapped)
    caplog.set_level(logging.DEBUG)
    extraction = extract_document_semantic_ir(build_tariff_pdf(), file_format="pdf")
    assert extraction.ir["pages_or_sheets"]
    assert SYNTHETIC_EMAIL not in caplog.text
    assert SYNTHETIC_CNPJ not in caplog.text
    assert "pdf_extract level=DEBUG" in caplog.text
    assert "pdf_extract level=ERROR" in caplog.text


def test_technical_fields_reject_document_payloads():
    pdf = _pdf_ir()
    xlsx = _xlsx_ir()

    def blocked(ir, metrics, code):
        client = _FakeClient()
        outcome = dispatch_structural_ir(
            client,
            ir,
            model="structural-ir-slice-test",
            extraction_metrics=metrics,
        )
        assert outcome.provider_called is False
        assert client.models.calls == []
        assert code in outcome.reason_codes

    bbox = copy.deepcopy(pdf.ir)
    bbox["pages_or_sheets"][0]["blocks"][0]["bbox"]["x0"] = "39053344705"
    blocked(bbox, pdf.metrics, "bbox")

    nan_box = copy.deepcopy(pdf.ir)
    nan_box["pages_or_sheets"][0]["blocks"][0]["bbox"]["y0"] = float("nan")
    blocked(nan_box, pdf.metrics, "bbox")

    page_number = copy.deepcopy(pdf.ir)
    page_number["pages_or_sheets"][0]["page_number"] = 0
    blocked(page_number, pdf.metrics, "page_number")

    page_text = copy.deepcopy(pdf.ir)
    page_text["pages_or_sheets"][0]["page_number"] = "1"
    blocked(page_text, pdf.metrics, "page_number")

    row = copy.deepcopy(xlsx.ir)
    _cell_by_coordinate(row, "SP", "A1")[1]["row"] = "1"
    blocked(row, xlsx.metrics, "row_column")

    coordinate = copy.deepcopy(xlsx.ir)
    _cell_by_coordinate(coordinate, "SP", "A1")[1]["coordinate"] = "CPF 390"
    blocked(coordinate, xlsx.metrics, "coordinate")

    hidden = copy.deepcopy(xlsx.ir)
    _sheet(hidden, "SP")["hidden_columns"] = ["39053344705"]
    blocked(hidden, xlsx.metrics, "hidden_columns")

    span = copy.deepcopy(xlsx.ir)
    _sheet(span, "Sul")["merges"][0]["range"] = "A1:C1 CPF"
    blocked(span, xlsx.metrics, "range")

    relation = copy.deepcopy(pdf.ir)
    relation["pages_or_sheets"][0]["blocks"][0]["relationships"] = [{"kind": "secret_link", "target_id": "p1-b1"}]
    blocked(relation, pdf.metrics, "relationship_kind")

    technical_id = copy.deepcopy(pdf.ir)
    technical_id["pages_or_sheets"][0]["blocks"][0]["id"] = "operacao@sintetico.example"
    blocked(technical_id, pdf.metrics, "technical_id")


def _logging_snapshot():
    root = logging.getLogger()
    pdf_loggers = []
    for name, logger in logging.Logger.manager.loggerDict.items():
        if not isinstance(logger, logging.Logger):
            continue
        if name in {"pdfminer", "pdfplumber"} or name.startswith("pdfminer.") or name.startswith("pdfplumber."):
            pdf_loggers.append(
                (
                    name,
                    logger.level,
                    logger.propagate,
                    tuple(logger.handlers),
                    tuple(id(item) for item in logger.filters),
                )
            )
    return (
        logging.Logger.handle,
        logging.getLoggerClass(),
        root.level,
        root.propagate,
        tuple(root.handlers),
        tuple(sorted(pdf_loggers)),
    )


def test_concurrent_pdf_extractions_keep_log_redaction_and_logging_config(caplog, monkeypatch):
    import pdfplumber

    logging.getLogger("pdfminer.fresh.child")
    secrets = (
        ("alpha@sintetico.example", "11.111.111/0001-91"),
        ("beta@sintetico.example", "22.222.222/0001-82"),
    )
    local = threading.local()
    state = {"barrier": None}
    real_open = pdfplumber.open

    def wrapped(*args, **kwargs):
        state["barrier"].wait(timeout=10)
        email = local.email
        cnpj = local.cnpj
        logging.getLogger("pdfminer.pdfinterp").debug("conteudo %s", email)
        logging.getLogger("pdfminer.fresh.child").error("novo %s", email)
        try:
            raise RuntimeError(f"{cnpj} {email}")
        except RuntimeError:
            logging.getLogger("pdfplumber").exception("falha %s", cnpj)
        return real_open(*args, **kwargs)

    monkeypatch.setattr("app.services.document_semantic_ir.pdf_slice.pdfplumber.open", wrapped)
    caplog.set_level(logging.DEBUG)
    before = _logging_snapshot()
    assert logging.Logger.handle.__name__ == "handle"
    source = (PACKAGE / "pdf_slice.py").read_text(encoding="utf-8")
    assert "Logger.handle =" not in source
    for _round in range(4):
        caplog.clear()
        state["barrier"] = threading.Barrier(2)
        errors = []

        def worker(email, cnpj):
            local.email = email
            local.cnpj = cnpj
            try:
                extraction = extract_document_semantic_ir(build_tariff_pdf(), file_format="pdf")
                if not extraction.ir["pages_or_sheets"]:
                    errors.append("empty")
            except Exception as exc:  # noqa: BLE001 - a falha da thread precisa voltar para a asserção
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=pair) for pair in secrets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
            assert not thread.is_alive()
        assert errors == []
        blob = caplog.text
        for email, cnpj in secrets:
            assert email not in blob
            assert cnpj not in blob
        assert "pdf_extract level=" in blob
        for record in caplog.records:
            rendered = record.getMessage()
            if record.exc_text:
                rendered = f"{rendered}\n{record.exc_text}"
            for email, cnpj in secrets:
                assert email not in rendered
                assert cnpj not in rendered
            if record.name == "pdfminer" or record.name.startswith("pdfminer.") or record.name == "pdfplumber" or record.name.startswith("pdfplumber."):
                assert record.exc_info is None
                assert not record.exc_text
        assert _logging_snapshot() == before


def test_closed_schema_rejects_document_numbers_in_technical_fields():
    pdf = _pdf_ir()
    xlsx = _xlsx_ir()
    cpf = NUMERIC_CPF

    def blocked(ir, metrics, code):
        client = _FakeClient()
        outcome = dispatch_structural_ir(
            client,
            ir,
            model="structural-ir-slice-test",
            extraction_metrics=metrics,
        )
        assert outcome.provider_called is False
        assert client.models.calls == []
        assert outcome.safe_json is None
        assert code in outcome.reason_codes

    row = copy.deepcopy(xlsx.ir)
    _cell_by_coordinate(row, "SP", "A1")[1]["row"] = cpf
    blocked(row, xlsx.metrics, "row_column")

    column = copy.deepcopy(xlsx.ir)
    _cell_by_coordinate(column, "SP", "A1")[1]["column"] = cpf
    blocked(column, xlsx.metrics, "row_column")

    rotation = copy.deepcopy(pdf.ir)
    rotation["pages_or_sheets"][0]["rotation"] = cpf
    blocked(rotation, pdf.metrics, "rotation")

    bbox = copy.deepcopy(pdf.ir)
    bbox["pages_or_sheets"][0]["blocks"][0]["bbox"]["x0"] = cpf
    blocked(bbox, pdf.metrics, "bbox")

    dimensions = copy.deepcopy(pdf.ir)
    dimensions["pages_or_sheets"][0]["dimensions"]["width"] = cpf
    blocked(dimensions, pdf.metrics, "dimensions")

    extra_number = copy.deepcopy(pdf.ir)
    extra_number["pages_or_sheets"][0]["blocks"][0]["carrier"] = cpf
    blocked(extra_number, pdf.metrics, "unknown_field")

    extra_text = copy.deepcopy(pdf.ir)
    extra_text["pages_or_sheets"][0]["nota_oculta"] = "documento secreto"
    blocked(extra_text, pdf.metrics, "unknown_field")

    nested = copy.deepcopy(xlsx.ir)
    style = _cell_by_coordinate(nested, "SP", "C3")[1]["style_ref"]
    style["segredo"] = cpf
    blocked(nested, xlsx.metrics, "unknown_field")

    for extraction in (pdf, xlsx, _pdf_ir(), _xlsx_ir()):
        client, outcome = _dispatch(extraction)
        assert outcome.provider_called is True
        contents = client.models.calls[0]["contents"]
        assert isinstance(contents, str)
        parsed = json.loads(contents)
        assert json.loads(outcome.safe_json) == parsed


def test_xlsx_budgets_are_global_and_preflight_blocks_giant_merges():
    from openpyxl.cell.cell import Cell, MergedCell

    oversized = extract_document_semantic_ir(build_oversized_grid_xlsx(), file_format="xlsx")
    oversized_cells = sum(1 for _block, _cell in _cells(oversized.ir))
    assert oversized_cells <= 8000
    assert oversized_cells != 12003
    assert oversized.ir["document"]["coverage"]["complete"] is False
    assert LIMITATION_CELL_CAP in oversized.ir["document"]["coverage"]["limitations"]

    created = {"merged": 0, "cells": 0}
    original_merged = MergedCell.__init__
    original_cell = Cell.__init__

    def count_merged(self, *args, **kwargs):
        created["merged"] += 1
        return original_merged(self, *args, **kwargs)

    def count_cell(self, *args, **kwargs):
        created["cells"] += 1
        return original_cell(self, *args, **kwargs)

    MergedCell.__init__ = count_merged
    Cell.__init__ = count_cell
    try:
        tracemalloc.start()
        giant = extract_document_semantic_ir(build_giant_merge_xlsx(), file_format="xlsx")
        _current, giant_peak = tracemalloc.get_traced_memory()
        created_after_giant = dict(created)
        many = extract_document_semantic_ir(build_many_small_merges_xlsx(), file_format="xlsx")
        _current, many_peak = tracemalloc.get_traced_memory()
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        MergedCell.__init__ = original_merged
        Cell.__init__ = original_cell

    assert giant.ir["document"]["coverage"]["complete"] is False
    assert LIMITATION_MERGE_CAP in giant.ir["document"]["coverage"]["limitations"]
    assert created_after_giant["merged"] < 1000
    assert created_after_giant["cells"] < 1000
    assert giant_peak < 32 * 1024 * 1024
    many_merges = sum(len(sheet.get("merges") or []) for sheet in many.ir["pages_or_sheets"])
    assert many_merges <= 2000
    assert many_merges < 3000
    assert many.ir["document"]["coverage"]["complete"] is False
    assert LIMITATION_MERGE_CAP in many.ir["document"]["coverage"]["limitations"]
    assert many_peak < 48 * 1024 * 1024
    normal = _xlsx_ir()
    assert normal.ir["document"]["coverage"]["limitations"] == []
    assert _sheet(normal.ir, "Sul")["merges"]


def test_float_textualization_preserves_parser_precision():
    expected = {
        0: "0",
        10.5: "10.5",
        7.25: "7.25",
        0.0000001: format(Decimal(str(0.0000001)), "f"),
        1.23456789: format(Decimal(str(1.23456789)), "f"),
        -0.0000001: format(Decimal(str(-0.0000001)), "f"),
        123456789.123456: format(Decimal(str(123456789.123456)), "f"),
        NUMERIC_CPF_DECIMAL: str(int(NUMERIC_CPF_DECIMAL)),
    }
    assert expected[0.0000001] != "0"
    assert expected[1.23456789] != "1.234568"
    assert expected[-0.0000001] not in {"0", "-0"}
    assert render_document_scalar(0) == "0"
    assert render_document_scalar(0.0) == "0"
    for value, text in expected.items():
        assert render_document_scalar(value) == text
        if isinstance(value, float) and not value.is_integer():
            assert float(text) == value

    extraction = extract_document_semantic_ir(build_precision_xlsx(), file_format="xlsx")
    client, outcome = _dispatch(extraction)
    assert outcome.provider_called is True
    sent = client.models.calls[0]["contents"]
    parsed = json.loads(sent)
    assert json.loads(outcome.safe_json) == parsed
    by_label = {}
    for block in _sheet(parsed, "Num")["blocks"]:
        for cell in block["cells"]:
            if cell.get("column") == 1:
                by_label[cell["value"]] = cell["row"]
    values = {cell["row"]: cell for _block, cell in _cells(parsed) if cell.get("column") == 2}
    checks = {
        "zero": "0",
        "meio": "10.5",
        "peso": "7.25",
        "pequeno": expected[0.0000001],
        "fracao": expected[1.23456789],
        "negativo": expected[-0.0000001],
        "grande": expected[123456789.123456],
    }
    for label, text in checks.items():
        cell = values[by_label[label]]
        assert cell["value"] == text
        assert cell["value_type"] == "number"
        assert text in sent
    cpf_cell = values[by_label["CPF"]]
    assert cpf_cell["value_type"] == "number"
    assert str(int(NUMERIC_CPF_DECIMAL)) not in sent
    assert "52998224725.0" not in sent
    assert "1.234568" not in sent


def test_nested_fingerprint_does_not_reach_the_provider():
    from app.services.document_semantic_ir.dispatch import _strip_reserved_fingerprints

    extraction = _xlsx_ir()
    fingerprint = extraction.ir["document"]["source_fingerprint_local"]
    sha = fingerprint.split(":", 1)[1]
    generic = {"fingerprint": sha, "source_fingerprint_local": fingerprint, "child": {"fingerprint": sha}}
    _strip_reserved_fingerprints(generic)
    assert generic["fingerprint"] == sha
    assert generic["child"]["fingerprint"] == sha
    assert "source_fingerprint_local" not in generic

    tampered = copy.deepcopy(extraction.ir)
    tampered["pages_or_sheets"][0]["source_fingerprint_local"] = fingerprint
    block = tampered["pages_or_sheets"][0]["blocks"][0]
    block["provenance"]["source_fingerprint_local"] = fingerprint
    cell = block["cells"][0]
    cell["source_fingerprint_local"] = {"nested": fingerprint, "source_fingerprint_local": sha}
    if cell["relationships"]:
        cell["relationships"][0]["source_fingerprint_local"] = fingerprint
    holder = SimpleNamespace(ir=tampered, metrics=extraction.metrics)
    client, outcome = _dispatch(holder)
    assert outcome.provider_called is True
    sent = client.models.calls[0]["contents"]
    parsed = json.loads(sent)
    assert json.loads(outcome.safe_json) == parsed
    assert "source_fingerprint_local" not in sent
    assert fingerprint not in sent
    assert sha not in sent


def test_isolated_graphic_line_marks_structural_limitation():
    extraction = extract_document_semantic_ir(build_isolated_line_pdf(), file_format="pdf")
    coverage = extraction.ir["document"]["coverage"]
    assert coverage["complete"] is False
    assert LIMITATION_VISUAL in coverage["limitations"]
    assert "Trecho com linha" in " ".join(cell["text"] for _block, cell in _cells(extraction.ir))


def _documentary_chars(ir) -> int:
    total = 0
    for sheet in ir["pages_or_sheets"]:
        total += len(sheet.get("sheet_name") or "")
        for block in sheet.get("blocks") or []:
            for cell in block.get("cells") or []:
                for key in ("value", "cached_value", "formula", "comment"):
                    total += _documentary_piece_len(cell.get(key))
                style = cell.get("style_ref") or {}
                for key in ("number_format", "fill_pattern"):
                    raw = style.get(key)
                    if isinstance(raw, str):
                        total += len(raw)
    return total


def _documentary_piece_len(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return len("true" if value else "false")
    if isinstance(value, str):
        return len(value)
    rendered = render_document_scalar(value)
    return len(rendered or "")


def _load_calls(monkeypatch):
    import openpyxl

    calls = {"load": 0}
    original = openpyxl.load_workbook

    def wrapped(*args, **kwargs):
        calls["load"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.openpyxl.load_workbook", wrapped)
    return calls


def _assert_preflight_stop(data, monkeypatch, limitation):
    calls = _load_calls(monkeypatch)
    extraction = extract_document_semantic_ir(data, file_format="xlsx")
    coverage = extraction.ir["document"]["coverage"]
    assert calls["load"] == 0
    assert extraction.ir["pages_or_sheets"] == []
    assert coverage["complete"] is False
    assert limitation in coverage["limitations"]
    return extraction


def _assert_unreadable(data, monkeypatch):
    calls = _load_calls(monkeypatch)
    with pytest.raises(DocumentSemanticIrError, match="xlsx_unreadable"):
        extract_document_semantic_ir(data, file_format="xlsx")
    assert calls["load"] == 0


def test_preflight_counts_cells_merges_area_and_xml_for_referenced_worksheets(monkeypatch):
    conventional = build_small_merge_xlsx()
    renamed = rename_worksheet_part(conventional)
    relative = retarget_worksheet(conventional, "worksheets/sheet1.xml")
    for data in (conventional, renamed, relative):
        extraction = extract_document_semantic_ir(data, file_format="xlsx")
        coverage = extraction.ir["document"]["coverage"]
        assert coverage["complete"] is True
        assert coverage["limitations"] == []
        coordinates = sorted(cell["coordinate"] for _block, cell in _cells(extraction.ir))
        assert coordinates == ["A1", "GR200"]
        assert extraction.ir["pages_or_sheets"][0]["merges"] == [{"range": "A1:B1", "anchor": "A1"}]

    for data, part in (
        (conventional, "xl/worksheets/sheet1.xml"),
        (renamed, "xl/worksheets/custom.xml"),
    ):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            workbook_size = archive.getinfo("xl/workbook.xml").file_size
            worksheet_size = archive.getinfo(part).file_size
        monkeypatch.setattr(
            "app.services.document_semantic_ir.xlsx_slice.MAX_XML_UNCOMPRESSED",
            workbook_size + worksheet_size - 1,
        )
        _assert_preflight_stop(data, monkeypatch, LIMITATION_RESOURCE)
        monkeypatch.setattr(
            "app.services.document_semantic_ir.xlsx_slice.MAX_XML_UNCOMPRESSED",
            workbook_size + worksheet_size,
        )
        allowed = extract_document_semantic_ir(data, file_format="xlsx")
        assert LIMITATION_RESOURCE not in allowed.ir["document"]["coverage"]["limitations"]
        assert [cell["coordinate"] for _block, cell in _cells(allowed.ir)]

    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_XML_UNCOMPRESSED", 4 * 1024 * 1024)
    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_STRUCTURAL_CELLS", 1)
    for data in (conventional, renamed):
        _assert_preflight_stop(data, monkeypatch, LIMITATION_CELL_CAP)


def test_renamed_worksheet_giant_merge_is_blocked_before_load(monkeypatch):
    from openpyxl.cell.cell import MergedCell

    created = {"merged": 0}
    original = MergedCell.__init__

    def count_merged(self, *args, **kwargs):
        created["merged"] += 1
        return original(self, *args, **kwargs)

    MergedCell.__init__ = count_merged
    calls = _load_calls(monkeypatch)
    try:
        for data in (build_giant_merge_xlsx(), rename_worksheet_part(build_giant_merge_xlsx())):
            created["merged"] = 0
            calls["load"] = 0
            extraction = extract_document_semantic_ir(data, file_format="xlsx")
            coverage = extraction.ir["document"]["coverage"]
            assert calls["load"] == 0
            assert created["merged"] == 0
            assert extraction.ir["pages_or_sheets"] == []
            assert coverage["complete"] is False
            assert LIMITATION_MERGE_CAP in coverage["limitations"]
    finally:
        MergedCell.__init__ = original


def test_workbook_relationship_attacks_fail_closed(monkeypatch):
    base = build_sparse_corners_xlsx()
    _assert_unreadable(build_traversal_worksheet_xlsx(), monkeypatch)
    _assert_unreadable(
        retarget_worksheet(base, "https://evil.example/sheet.xml", target_mode="External"),
        monkeypatch,
    )
    _assert_unreadable(retarget_worksheet(base, "/xl/worksheets/sheet1.xml", duplicate=True), monkeypatch)
    _assert_unreadable(retarget_worksheet(base, "/xl/worksheets/missing.xml"), monkeypatch)
    _assert_unreadable(build_entity_worksheet_xlsx(), monkeypatch)
    _assert_unreadable(build_entity_worksheet_xlsx(renamed=True), monkeypatch)

    calls = _load_calls(monkeypatch)
    duplicated = duplicate_worksheet_target(build_sparse_corners_xlsx())
    extraction = extract_document_semantic_ir(duplicated, file_format="xlsx")
    names = [sheet["sheet_name"] for sheet in extraction.ir["pages_or_sheets"]]
    assert calls["load"] == 2
    assert names == ["Raro", "Copia"]
    assert extraction.ir["document"]["coverage"]["complete"] is True


def test_duplicate_worksheet_target_multiplies_budget_before_load(monkeypatch):
    from openpyxl.cell.cell import MergedCell

    from app.services.document_semantic_ir import xlsx_slice

    single = build_aliased_merge_xlsx(sheets=1, merges=100)
    single_budget = xlsx_slice._logical_sheet_budget(single)
    assert single_budget["sheets"] == 1
    assert single_budget["merges"] == 100
    assert xlsx_slice._preflight_limitations(single) == []

    data = build_aliased_merge_xlsx(sheets=51, merges=100)
    budget = xlsx_slice._logical_sheet_budget(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        workbook_size = archive.getinfo("xl/workbook.xml").file_size
        part_size = archive.getinfo("xl/worksheets/sheet1.xml").file_size
    assert budget["limitation"] is None
    assert budget["sheets"] == 51
    assert budget["merges"] == 5100
    assert budget["merge_area"] == 10200
    assert budget["cells"] == 102
    assert budget["xml_bytes"] == workbook_size + (51 * part_size)

    created = {"merged": 0}
    original = MergedCell.__init__

    def count_merged(self, *args, **kwargs):
        created["merged"] += 1
        return original(self, *args, **kwargs)

    MergedCell.__init__ = count_merged
    calls = _load_calls(monkeypatch)
    try:
        extraction = extract_document_semantic_ir(data, file_format="xlsx")
        coverage = extraction.ir["document"]["coverage"]
        assert calls["load"] == 0
        assert created["merged"] == 0
        assert extraction.ir["pages_or_sheets"] == []
        assert coverage["complete"] is False
        assert LIMITATION_RESOURCE in coverage["limitations"]

        monkeypatch.setattr(xlsx_slice, "MAX_SHEETS", 80)
        monkeypatch.setattr(xlsx_slice, "MAX_MERGE_AREA_TOTAL", 10**9)
        monkeypatch.setattr(xlsx_slice, "MAX_XML_UNCOMPRESSED", 10**9)
        calls["load"] = 0
        created["merged"] = 0
        blocked = extract_document_semantic_ir(data, file_format="xlsx")
        assert calls["load"] == 0
        assert created["merged"] == 0
        assert blocked.ir["pages_or_sheets"] == []
        assert blocked.ir["document"]["coverage"]["complete"] is False
        assert LIMITATION_MERGE_CAP in blocked.ir["document"]["coverage"]["limitations"]

        monkeypatch.setattr(xlsx_slice, "PREFLIGHT_MAX_MERGES", 5099)
        assert xlsx_slice._preflight_limitations(data) == [LIMITATION_MERGE_CAP]
        monkeypatch.setattr(xlsx_slice, "PREFLIGHT_MAX_MERGES", 5100)
        assert xlsx_slice._preflight_limitations(data) == []
        assert calls["load"] == 0
        assert created["merged"] == 0
    finally:
        MergedCell.__init__ = original


def test_duplicate_worksheet_target_below_limit_counts_both_sheets(monkeypatch):
    from app.services.document_semantic_ir import xlsx_slice

    data = duplicate_worksheet_target(build_small_merge_xlsx())
    budget = xlsx_slice._logical_sheet_budget(data)
    assert budget["sheets"] == 2
    assert budget["merges"] == 2
    assert budget["cells"] == 4
    assert budget["limitation"] is None
    assert xlsx_slice._preflight_limitations(data) == []

    extraction = extract_document_semantic_ir(data, file_format="xlsx")
    coverage = extraction.ir["document"]["coverage"]
    names = [sheet["sheet_name"] for sheet in extraction.ir["pages_or_sheets"]]
    assert coverage["complete"] is True
    assert coverage["limitations"] == []
    assert names == ["Raro", "Copia"]
    assert sum(len(sheet["merges"]) for sheet in extraction.ir["pages_or_sheets"]) == 2

    monkeypatch.setattr(xlsx_slice, "MAX_STRUCTURAL_CELLS", 3)
    _assert_preflight_stop(data, monkeypatch, LIMITATION_CELL_CAP)
    assert xlsx_slice._preflight_limitations(build_small_merge_xlsx()) == []

    monkeypatch.setattr(xlsx_slice, "MAX_STRUCTURAL_CELLS", 8000)
    monkeypatch.setattr(xlsx_slice, "PREFLIGHT_MAX_MERGES", 1)
    _assert_preflight_stop(data, monkeypatch, LIMITATION_MERGE_CAP)
    assert xlsx_slice._preflight_limitations(build_small_merge_xlsx()) == []


def test_sheet_pointing_at_styles_is_unreadable_before_load(monkeypatch):
    data = retarget_worksheet(build_sparse_corners_xlsx(), "/xl/styles.xml")
    _assert_unreadable(data, monkeypatch)
    mixed = duplicate_worksheet_target(build_sparse_corners_xlsx())
    mixed = retarget_worksheet(mixed, "/xl/styles.xml", old_target="/xl/worksheets/sheet1.xml")
    # O helper troca só a primeira ocorrência. A cópia continua válida e não pode sair completa.
    _assert_unreadable(mixed, monkeypatch)


def test_sheet_without_relationship_id_is_unreadable_before_load(monkeypatch):
    _assert_unreadable(remove_sheet_relationship_id(build_sparse_corners_xlsx()), monkeypatch)
    aliased = duplicate_worksheet_target(build_sparse_corners_xlsx())
    _assert_unreadable(remove_sheet_relationship_id(aliased, "rId9"), monkeypatch)


def test_sheet_relationship_id_without_target_is_unreadable_before_load(monkeypatch):
    _assert_unreadable(rewrite_sheet_relationship_id(build_sparse_corners_xlsx(), "rId1", "rIdMissing"), monkeypatch)
    aliased = duplicate_worksheet_target(build_sparse_corners_xlsx())
    _assert_unreadable(rewrite_sheet_relationship_id(aliased, "rId9", "rIdMissing"), monkeypatch)


def test_chartsheet_relationship_is_not_a_worksheet(monkeypatch):
    chart_type = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/chartsheet"
    _assert_unreadable(retype_worksheet_relationship(build_sparse_corners_xlsx(), chart_type), monkeypatch)

    kept = extract_document_semantic_ir(attach_orphan_chartsheet(build_sparse_corners_xlsx()), file_format="xlsx")
    coverage = kept.ir["document"]["coverage"]
    assert coverage["complete"] is True
    assert coverage["limitations"] == []
    assert [sheet["sheet_name"] for sheet in kept.ir["pages_or_sheets"]] == ["Raro"]
    assert sorted(cell["coordinate"] for _block, cell in _cells(kept.ir)) == ["A1", "GR200"]


@pytest.mark.parametrize(
    "rel_type",
    [
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/externalLink",
    ],
)
def test_non_worksheet_relationship_type_is_unreadable_before_load(rel_type, monkeypatch):
    _assert_unreadable(retype_worksheet_relationship(build_sparse_corners_xlsx(), rel_type), monkeypatch)


def test_preflight_resource_guards_still_stop_before_load(monkeypatch):
    _assert_preflight_stop(build_many_sheets_xlsx(), monkeypatch, LIMITATION_RESOURCE)

    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_ZIP_DECLARED_UNCOMPRESSED", 10)
    _assert_unreadable(build_sparse_corners_xlsx(), monkeypatch)

    def banned(*_args, **_kwargs):
        raise AssertionError("zip_extraido")

    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_ZIP_DECLARED_UNCOMPRESSED", 32 * 1024 * 1024)
    monkeypatch.setattr(zipfile.ZipFile, "extract", banned)
    monkeypatch.setattr(zipfile.ZipFile, "extractall", banned)
    kept = extract_document_semantic_ir(build_sparse_corners_xlsx(), file_format="xlsx")
    assert kept.ir["document"]["coverage"]["complete"] is True
    assert sorted(cell["coordinate"] for _block, cell in _cells(kept.ir)) == ["A1", "GR200"]


def test_global_text_budget_stops_long_cell_values(monkeypatch):
    from app.services.document_semantic_ir import xlsx_slice

    extraction = extract_document_semantic_ir(build_three_long_text_sheets_xlsx(), file_format="xlsx")
    coverage = extraction.ir["document"]["coverage"]
    blob = json.dumps(extraction.ir)
    names = [sheet["sheet_name"] for sheet in extraction.ir["pages_or_sheets"]]
    assert coverage["complete"] is False
    assert LIMITATION_TEXT_CAP in coverage["limitations"]
    assert names[0] == "Sa"
    assert "Sc" not in names
    assert blob.count("alfa " * 600) == 1
    assert "beta " * 600 not in blob
    assert "gama " * 600 not in blob
    assert _documentary_chars(extraction.ir) <= xlsx_slice.MAX_SERIALIZED_TEXT
    assert _documentary_chars(extraction.ir) < 9000
    _client, outcome = _dispatch(extraction)
    assert outcome.safe_json is None or json.loads(outcome.safe_json)["document"]["coverage"]["complete"] is False
    if outcome.provider_called:
        assert "beta " * 600 not in outcome.safe_json
        assert "gama " * 600 not in outcome.safe_json
        assert _documentary_chars(json.loads(outcome.safe_json)) <= xlsx_slice.MAX_SERIALIZED_TEXT


def test_text_budget_is_shared_by_value_comment_formula_and_cache(monkeypatch):
    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_SERIALIZED_TEXT", 30)
    extraction = extract_document_semantic_ir(build_shared_text_budget_xlsx(), file_format="xlsx")
    coverage = extraction.ir["document"]["coverage"]
    blob = json.dumps(extraction.ir)
    assert coverage["complete"] is False
    assert LIMITATION_TEXT_CAP in coverage["limitations"]
    assert "V" * 12 in blob
    assert "K" * 12 in blob
    assert "F" * 10 not in blob
    assert "123456789" not in blob
    assert _documentary_chars(extraction.ir) <= 30
    _client, outcome = _dispatch(extraction)
    if outcome.safe_json:
        assert json.loads(outcome.safe_json)["document"]["coverage"]["complete"] is False


def test_numeric_textualization_participates_in_the_text_budget(monkeypatch):
    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_SERIALIZED_TEXT", 15)
    repeated = extract_document_semantic_ir(build_repeated_number_xlsx(), file_format="xlsx")
    assert repeated.ir["document"]["coverage"]["complete"] is False
    assert LIMITATION_TEXT_CAP in repeated.ir["document"]["coverage"]["limitations"]
    numbers = [
        cell["value"]
        for _block, cell in _cells(repeated.ir)
        if cell.get("value") == 123456789
    ]
    assert numbers == [123456789]
    assert _documentary_chars(repeated.ir) <= 15

    precise = render_document_scalar(1.23456789)
    assert precise == "1.23456789"
    assert len("N") + len("1.23") <= 6 < len("N") + len(precise)
    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_SERIALIZED_TEXT", 6)
    single = extract_document_semantic_ir(build_precise_number_xlsx(), file_format="xlsx")
    assert single.ir["document"]["coverage"]["complete"] is False
    assert LIMITATION_TEXT_CAP in single.ir["document"]["coverage"]["limitations"]
    assert precise not in json.dumps(single.ir)
    assert _documentary_chars(single.ir) <= 6


def test_sheet_name_participates_in_the_text_budget(monkeypatch):
    monkeypatch.setattr("app.services.document_semantic_ir.xlsx_slice.MAX_SERIALIZED_TEXT", 36)
    extraction = extract_document_semantic_ir(build_long_sheet_name_xlsx(), file_format="xlsx")
    coverage = extraction.ir["document"]["coverage"]
    assert coverage["complete"] is False
    assert LIMITATION_TEXT_CAP in coverage["limitations"]
    assert extraction.ir["pages_or_sheets"][0]["sheet_name"] == "N" * 31
    assert "C" * 20 not in json.dumps(extraction.ir)
    assert _documentary_chars(extraction.ir) <= 36


def test_small_xlsx_stays_complete_under_the_text_budget():
    extraction = extract_document_semantic_ir(build_sparse_corners_xlsx(), file_format="xlsx")
    assert extraction.ir["document"]["coverage"]["complete"] is True
    assert extraction.ir["document"]["coverage"]["limitations"] == []
    assert sorted(cell["coordinate"] for _block, cell in _cells(extraction.ir)) == ["A1", "GR200"]
    client, outcome = _dispatch(extraction)
    assert outcome.provider_called is True
    assert client.models.calls
    assert json.loads(outcome.safe_json)["document"]["coverage"]["complete"] is True
