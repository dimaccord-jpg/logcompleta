import pytest
from unittest.mock import patch

from app.cleiton_doc_contracts import (
    CONTEXT_KIND_GEMINI_FILE,
    CONTEXT_KIND_TEXT,
    ERROR_CORRUPTED_FILE,
    ERROR_DISABLED_TYPE,
    ERROR_EMPTY_FILE,
    ERROR_FILE_TOO_LARGE,
    ERROR_INVALID_EXTENSION,
    ERROR_INVALID_MIME,
    ERROR_TOO_DEEP_XML,
    ERROR_TOO_MANY_CHARS,
    ERROR_TOO_MANY_COLUMNS,
    ERROR_TOO_MANY_NODES,
    ERROR_TOO_MANY_PAGES,
    ERROR_TOO_MANY_PARAGRAPHS,
    ERROR_TOO_MANY_ROWS,
    ERROR_UNSAFE_FILENAME,
    ERROR_UPLOAD_DISABLED,
    FIELD_CHAR_COUNT,
    FIELD_CONTEXT_KIND,
    FIELD_DOC_TYPE,
    FIELD_PAGE_COUNT,
    FIELD_PREPARED_CONTEXT,
    FIELD_TRUNCATED,
)
from app.cleiton_doc_prepare import prepare_document
from app.cleiton_doc_security import CleitonDocSecurityError, validate_upload_security
from tests.cleiton_doc_fixtures import (
    doc_cfg,
    make_billion_laughs_xml,
    make_corrupted_docx,
    make_corrupted_xlsx,
    make_csv,
    make_docx,
    make_invalid_pdf,
    make_minimal_pdf,
    make_nested_xml,
    make_many_nodes_xml,
    make_txt,
    make_xlsx,
    make_xml,
    make_zip_bomb_like,
    patch_cleiton_doc_cfg,
)


# --- Segurança geral ---


def test_invalid_extension(doc_cfg):
    with pytest.raises(CleitonDocSecurityError) as exc:
        validate_upload_security(
            display_name="arquivo.exe",
            file_bytes=b"data",
            extension=".exe",
        )
    assert exc.value.error_code == ERROR_INVALID_EXTENSION


def test_disabled_type_by_config(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, pdf_enabled=False)
    with pytest.raises(CleitonDocSecurityError) as exc:
        validate_upload_security(
            display_name="doc.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="application/pdf",
        )
    assert exc.value.error_code == ERROR_DISABLED_TYPE


def test_empty_file(doc_cfg):
    with pytest.raises(CleitonDocSecurityError) as exc:
        validate_upload_security(
            display_name="vazio.txt",
            file_bytes=b"",
        )
    assert exc.value.error_code == ERROR_EMPTY_FILE


def test_file_too_large_for_type(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, txt_max_bytes=10)
    with pytest.raises(CleitonDocSecurityError) as exc:
        validate_upload_security(
            display_name="grande.txt",
            file_bytes=b"x" * 20,
        )
    assert exc.value.error_code == ERROR_FILE_TOO_LARGE


def test_invalid_mime_for_extension(doc_cfg):
    with pytest.raises(CleitonDocSecurityError) as exc:
        validate_upload_security(
            display_name="doc.pdf",
            file_bytes=make_minimal_pdf(),
            mime_type="text/plain",
        )
    assert exc.value.error_code == ERROR_INVALID_MIME


def test_dangerous_filename_path_traversal(doc_cfg):
    with pytest.raises(CleitonDocSecurityError) as exc:
        validate_upload_security(
            display_name="../../etc/passwd.txt",
            file_bytes=b"ok",
        )
    assert exc.value.error_code == ERROR_UNSAFE_FILENAME


def test_corrupted_pdf_rejected(doc_cfg):
    with pytest.raises(CleitonDocSecurityError) as exc:
        validate_upload_security(
            display_name="bad.pdf",
            file_bytes=make_invalid_pdf(),
            mime_type="application/pdf",
        )
    assert exc.value.error_code == ERROR_CORRUPTED_FILE


def test_upload_disabled(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, upload_enabled=False)
    with pytest.raises(CleitonDocSecurityError) as exc:
        validate_upload_security(
            display_name="doc.txt",
            file_bytes=b"hello",
        )
    assert exc.value.error_code == ERROR_UPLOAD_DISABLED


# --- TXT ---


def test_valid_txt(doc_cfg):
    result = prepare_document(
        display_name="notas.txt",
        file_bytes=make_txt("linha 1\nlinha 2"),
        mime_type="text/plain",
    )
    assert result[FIELD_DOC_TYPE] == "txt"
    assert result[FIELD_CONTEXT_KIND] == CONTEXT_KIND_TEXT
    assert "linha 1" in result[FIELD_PREPARED_CONTEXT]
    assert result[FIELD_TRUNCATED] is False


def test_txt_truncates_above_max_chars(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, txt_max_chars=5)
    result = prepare_document(
        display_name="long.txt",
        file_bytes=make_txt("123456789"),
    )
    assert result[FIELD_TRUNCATED] is True
    assert result[FIELD_CHAR_COUNT] == 5
    assert result[FIELD_PREPARED_CONTEXT] == "12345"


def test_too_many_chars_error_reserved_truncation_used_instead(doc_cfg, monkeypatch):
    """Fase 3 trunca acima de *_max_chars; ERROR_TOO_MANY_CHARS fica reservado."""
    patch_cleiton_doc_cfg(monkeypatch, txt_max_chars=4, csv_max_chars=4)
    txt_result = prepare_document(
        display_name="long.txt",
        file_bytes=make_txt("123456"),
    )
    csv_result = prepare_document(
        display_name="long.csv",
        file_bytes=make_csv([["123456789"]]),
    )
    assert ERROR_TOO_MANY_CHARS == "cleiton_doc_too_many_chars"
    assert txt_result["error_code"] is None
    assert csv_result["error_code"] is None
    assert txt_result[FIELD_TRUNCATED] is True
    assert csv_result[FIELD_TRUNCATED] is True


def test_txt_non_utf8_uses_cp1252_fallback(doc_cfg):
    raw = b"a\xe9b"
    result = prepare_document(display_name="legacy.txt", file_bytes=raw)
    assert result[FIELD_DOC_TYPE] == "txt"
    assert result[FIELD_PREPARED_CONTEXT] == raw.decode("cp1252")
    assert len(result[FIELD_PREPARED_CONTEXT]) == 3


# --- XML ---


def test_valid_xml(doc_cfg):
    xml = make_xml('<?xml version="1.0"?><root><item>valor</item></root>')
    result = prepare_document(display_name="dados.xml", file_bytes=xml)
    assert result[FIELD_DOC_TYPE] == "xml"
    assert "item" in result[FIELD_PREPARED_CONTEXT]
    assert result[FIELD_TRUNCATED] is False


def test_xml_billion_laughs_blocked(doc_cfg):
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="attack.xml",
            file_bytes=make_billion_laughs_xml(),
        )
    assert exc.value.error_code == ERROR_CORRUPTED_FILE


def test_xml_too_many_nodes_blocked(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, xml_max_nodes=5)
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="big.xml",
            file_bytes=make_many_nodes_xml(10),
        )
    assert exc.value.error_code == ERROR_TOO_MANY_NODES


def test_xml_too_deep_blocked(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, xml_max_depth=3)
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="deep.xml",
            file_bytes=make_nested_xml(6),
        )
    assert exc.value.error_code == ERROR_TOO_DEEP_XML


def test_xml_truncates_above_max_chars(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, xml_max_chars=20)
    xml = make_xml('<?xml version="1.0"?><root>' + ("x" * 100) + "</root>")
    result = prepare_document(display_name="long.xml", file_bytes=xml)
    assert result[FIELD_TRUNCATED] is True
    assert result[FIELD_CHAR_COUNT] == 20


def test_xml_without_logistics_tags_accepted(doc_cfg):
    xml = make_xml('<?xml version="1.0"?><foo><bar>1</bar></foo>')
    result = prepare_document(display_name="generico.xml", file_bytes=xml)
    assert result[FIELD_DOC_TYPE] == "xml"
    assert "foo" in result[FIELD_PREPARED_CONTEXT]


# --- CSV ---


def test_valid_csv(doc_cfg):
    data = make_csv([["a", "b"], ["1", "2"]])
    result = prepare_document(display_name="dados.csv", file_bytes=data)
    assert result[FIELD_DOC_TYPE] == "csv"
    assert "a,b" in result[FIELD_PREPARED_CONTEXT]


def test_csv_too_many_rows_blocked(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, csv_max_rows=2)
    rows = [[str(idx)] for idx in range(5)]
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(display_name="rows.csv", file_bytes=make_csv(rows))
    assert exc.value.error_code == ERROR_TOO_MANY_ROWS


def test_csv_too_many_columns_blocked(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, csv_max_columns=2)
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="cols.csv",
            file_bytes=make_csv([["a", "b", "c", "d"]]),
        )
    assert exc.value.error_code == ERROR_TOO_MANY_COLUMNS


def test_csv_truncates_above_max_chars(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, csv_max_chars=10)
    result = prepare_document(
        display_name="long.csv",
        file_bytes=make_csv([["x" * 50]]),
    )
    assert result[FIELD_TRUNCATED] is True
    assert result[FIELD_CHAR_COUNT] == 10


def test_csv_without_column_names_accepted(doc_cfg):
    data = make_csv([["1", "2", "3"], ["4", "5", "6"]])
    result = prepare_document(display_name="sem_header.csv", file_bytes=data)
    assert result[FIELD_DOC_TYPE] == "csv"


# --- XLSX ---


def test_valid_xlsx(doc_cfg):
    data = make_xlsx([["a", "b"], ["1", "2"]])
    result = prepare_document(display_name="planilha.xlsx", file_bytes=data)
    assert result[FIELD_DOC_TYPE] == "xlsx"
    assert "a,b" in result[FIELD_PREPARED_CONTEXT]


def test_xlsx_too_many_rows_blocked(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, excel_max_rows=2)
    rows = [[str(idx)] for idx in range(5)]
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(display_name="rows.xlsx", file_bytes=make_xlsx(rows))
    assert exc.value.error_code == ERROR_TOO_MANY_ROWS


def test_xlsx_too_many_columns_blocked(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, excel_max_columns=2)
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="cols.xlsx",
            file_bytes=make_xlsx([["a", "b", "c", "d"]]),
        )
    assert exc.value.error_code == ERROR_TOO_MANY_COLUMNS


def test_xlsx_corrupted_blocked(doc_cfg):
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="bad.xlsx",
            file_bytes=make_corrupted_xlsx(),
        )
    assert exc.value.error_code == ERROR_CORRUPTED_FILE


def test_xlsx_without_header_accepted(doc_cfg):
    data = make_xlsx([["1", "2"], ["3", "4"]])
    result = prepare_document(display_name="sem_cabecalho.xlsx", file_bytes=data)
    assert result[FIELD_DOC_TYPE] == "xlsx"


# --- DOCX ---


def test_valid_docx(doc_cfg):
    data = make_docx(["Parágrafo A", "Parágrafo B"])
    result = prepare_document(display_name="texto.docx", file_bytes=data)
    assert result[FIELD_DOC_TYPE] == "docx"
    assert "Parágrafo A" in result[FIELD_PREPARED_CONTEXT]


def test_docx_too_many_paragraphs_blocked(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, docx_max_paragraphs=2)
    data = make_docx(["a", "b", "c"])
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(display_name="paragrafos.docx", file_bytes=data)
    assert exc.value.error_code == ERROR_TOO_MANY_PARAGRAPHS


def test_docx_truncates_above_max_chars(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, docx_max_chars=5)
    data = make_docx(["123456789"])
    result = prepare_document(display_name="long.docx", file_bytes=data)
    assert result[FIELD_TRUNCATED] is True
    assert result[FIELD_CHAR_COUNT] == 5


def test_docx_corrupted_blocked(doc_cfg):
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="bad.docx",
            file_bytes=make_corrupted_docx(),
        )
    assert exc.value.error_code == ERROR_CORRUPTED_FILE


def test_docx_without_contract_structure_accepted(doc_cfg):
    data = make_docx(["texto livre sem cláusulas"])
    result = prepare_document(display_name="livre.docx", file_bytes=data)
    assert result[FIELD_DOC_TYPE] == "docx"


# --- Zip bomb ---


def test_xlsx_zip_bomb_blocked_before_workbook_load(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, excel_max_bytes=4096)
    bomb = make_zip_bomb_like()
    with patch("openpyxl.load_workbook") as mock_load:
        with pytest.raises(CleitonDocSecurityError) as exc:
            prepare_document(display_name="bomb.xlsx", file_bytes=bomb)
        assert exc.value.error_code == ERROR_CORRUPTED_FILE
        mock_load.assert_not_called()


def test_docx_zip_bomb_blocked_before_document_load(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, docx_max_bytes=4096)
    bomb = make_zip_bomb_like()
    with patch("docx.Document") as mock_document:
        with pytest.raises(CleitonDocSecurityError) as exc:
            prepare_document(display_name="bomb.docx", file_bytes=bomb)
        assert exc.value.error_code == ERROR_CORRUPTED_FILE
        mock_document.assert_not_called()


# --- PDF ---


def test_valid_pdf_prepares_gemini_placeholder(doc_cfg):
    result = prepare_document(
        display_name="manual.pdf",
        file_bytes=make_minimal_pdf(pages=2),
        mime_type="application/pdf",
    )
    assert result[FIELD_DOC_TYPE] == "pdf"
    assert result[FIELD_CONTEXT_KIND] == CONTEXT_KIND_GEMINI_FILE
    assert "gemini_file_api" in result[FIELD_PREPARED_CONTEXT]
    assert result[FIELD_PAGE_COUNT] == 2


def test_pdf_too_large_blocked(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, pdf_max_bytes=10)
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="big.pdf",
            file_bytes=make_minimal_pdf(),
        )
    assert exc.value.error_code == ERROR_FILE_TOO_LARGE


def test_pdf_no_local_ocr_or_text_extraction(doc_cfg):
    result = prepare_document(
        display_name="scan.pdf",
        file_bytes=make_minimal_pdf(),
        mime_type="application/pdf",
    )
    assert "local_text_extraction" in result[FIELD_PREPARED_CONTEXT]
    assert '"local_text_extraction": false' in result[FIELD_PREPARED_CONTEXT].lower()


def test_pdf_page_count_indeterminate_documents_warning(doc_cfg):
    pdf = b"%PDF-1.4\n%%EOF\n"
    result = prepare_document(
        display_name="minimal.pdf",
        file_bytes=pdf,
        mime_type="application/pdf",
    )
    assert result[FIELD_PAGE_COUNT] is None
    assert any("page_count_indeterminate" in w for w in result["warnings"])


def test_pdf_too_many_pages_when_count_available(doc_cfg, monkeypatch):
    patch_cleiton_doc_cfg(monkeypatch, pdf_max_pages=1)
    with pytest.raises(CleitonDocSecurityError) as exc:
        prepare_document(
            display_name="multi.pdf",
            file_bytes=make_minimal_pdf(pages=3),
            mime_type="application/pdf",
        )
    assert exc.value.error_code == ERROR_TOO_MANY_PAGES
