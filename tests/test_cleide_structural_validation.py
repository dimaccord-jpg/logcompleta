import csv
import io

import pytest

from app.cleide_structural_validation import (
    StructuralValidationError,
    analyze_structural_layout,
    canonicalize_header,
)


def _xlsx_bytes(*rows):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for ridx, row in enumerate(rows, start=1):
        for cidx, value in enumerate(row, start=1):
            ws.cell(row=ridx, column=cidx, value=value)
    stream = io.BytesIO()
    wb.save(stream)
    return stream.getvalue()


def test_canonicalize_header_normaliza_unicode_e_espacos():
    assert canonicalize_header("  Nome Transportadora  ") == "nome_transportadora"
    assert canonicalize_header("Data Emissão") == "data_emissao"


def test_csv_aliases_validos_utf8():
    payload = (
        "nome transportadora,origem_uf,destino_uf,vl_frete,peso_kg,dt_emissao\n"
        "xp,sp,rj,10,1,2026-01-01\n"
    ).encode("utf-8")
    ctx = analyze_structural_layout(
        raw_bytes=payload,
        extension=".csv",
        delimiter_default=",",
        max_rows=1000,
        max_columns=100,
    )
    assert ctx["dataset_validado"] is True
    assert ctx["linhas_detectadas"] == 1
    assert ctx["aliases_resolvidos"]["transportadora"] == "nome transportadora"


def test_csv_utf8_sig_latin1_decode():
    utf8_sig = (
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\nx,sp,rj,1,1,2026-01-01\n"
    ).encode("utf-8-sig")
    latin1 = (
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\nacao,sp,rj,1,1,2026-01-01\n"
    ).encode("latin1")
    ctx1 = analyze_structural_layout(
        raw_bytes=utf8_sig,
        extension=".csv",
        delimiter_default=",",
        max_rows=1000,
        max_columns=100,
    )
    ctx2 = analyze_structural_layout(
        raw_bytes=latin1,
        extension=".csv",
        delimiter_default=",",
        max_rows=1000,
        max_columns=100,
    )
    assert ctx1["detected_encoding"] == "utf-8-sig"
    assert ctx2["detected_encoding"] in {"latin1", "utf-8"}


def test_csv_invalido_sem_header():
    payload = b"\n\n"
    with pytest.raises(StructuralValidationError) as exc:
        analyze_structural_layout(
            raw_bytes=payload,
            extension=".csv",
            delimiter_default=",",
            max_rows=1000,
            max_columns=100,
        )
    assert exc.value.code in {"invalid_csv", "invalid_header"}


def test_csv_invalido_delimitador_ruim():
    payload = "transportadora||uf_origem\nx||sp\n".encode("utf-8")
    with pytest.raises(StructuralValidationError) as exc:
        analyze_structural_layout(
            raw_bytes=payload,
            extension=".csv",
            delimiter_default=",",
            max_rows=1000,
            max_columns=100,
        )
    assert exc.value.code == "invalid_csv"


def test_csv_colunas_duplicadas_detectadas():
    payload = (
        "transportadora,transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "x,y,sp,rj,1,1,2026-01-01\n"
    ).encode("utf-8")
    ctx = analyze_structural_layout(
        raw_bytes=payload,
        extension=".csv",
        delimiter_default=",",
        max_rows=1000,
        max_columns=100,
    )
    assert "transportadora" in ctx["colunas_duplicadas"]
    assert ctx["dataset_validado"] is False


def test_xlsx_valido_primeira_sheet():
    payload = _xlsx_bytes(
        ("transportadora", "uf_origem", "uf_destino", "valor_frete", "peso", "data_emissao"),
        ("x", "sp", "rj", 1, 1, "2026-01-01"),
    )
    ctx = analyze_structural_layout(
        raw_bytes=payload,
        extension=".xlsx",
        delimiter_default=",",
        max_rows=1000,
        max_columns=100,
    )
    assert ctx["dataset_validado"] is True
    assert ctx["sheet_detectada"] == "Sheet"


def test_xlsx_invalido_corrompido():
    with pytest.raises(StructuralValidationError) as exc:
        analyze_structural_layout(
            raw_bytes=b"not-xlsx",
            extension=".xlsx",
            delimiter_default=",",
            max_rows=1000,
            max_columns=100,
        )
    assert exc.value.code == "invalid_xlsx"


def test_xlsx_invalido_sem_header():
    payload = _xlsx_bytes(
        ("", "", ""),
    )
    with pytest.raises(StructuralValidationError) as exc:
        analyze_structural_layout(
            raw_bytes=payload,
            extension=".xlsx",
            delimiter_default=",",
            max_rows=1000,
            max_columns=100,
        )
    assert exc.value.code == "invalid_xlsx"


def test_limite_linhas_e_colunas():
    data = io.StringIO()
    writer = csv.writer(data)
    writer.writerow(["transportadora", "uf_origem", "uf_destino", "valor_frete", "peso", "data_emissao"])
    for _ in range(1002):
        writer.writerow(["x", "sp", "rj", 1, 1, "2026-01-01"])
    payload = data.getvalue().encode("utf-8")

    with pytest.raises(StructuralValidationError) as rows_exc:
        analyze_structural_layout(
            raw_bytes=payload,
            extension=".csv",
            delimiter_default=",",
            max_rows=1000,
            max_columns=100,
        )
    assert rows_exc.value.code == "layout_too_many_rows"

    many_columns = ",".join([f"c{i}" for i in range(130)]) + "\n" + ",".join(["x"] * 130) + "\n"
    with pytest.raises(StructuralValidationError) as cols_exc:
        analyze_structural_layout(
            raw_bytes=many_columns.encode("utf-8"),
            extension=".csv",
            delimiter_default=",",
            max_rows=1000,
            max_columns=50,
        )
    assert cols_exc.value.code == "layout_too_many_columns"
