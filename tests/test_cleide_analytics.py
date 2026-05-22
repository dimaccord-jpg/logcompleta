import io
from pathlib import Path

from app.cleide_analytics import build_analytics_context, build_filtered_analytics_context


def _valid_structural():
    return {
        "dataset_validado": True,
        "raw_headers": [
            "transportadora",
            "uf_origem",
            "uf_destino",
            "valor_frete",
            "peso",
            "data_emissao",
        ],
    }


def _csv(payload: str) -> bytes:
    return payload.encode("utf-8")


def test_kpis_calculo_basico():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,100,10,2026-01-01\n"
        "A,SP,RJ,50,5,2026-01-02\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    assert ctx["analytics_ready"] is True
    assert ctx["kpis"]["total_documentos"] == 2
    assert ctx["kpis"]["valor_total_frete"] == 150.0
    assert ctx["kpis"]["peso_total"] == 15.0
    assert ctx["kpis"]["ticket_medio_frete"] == 75.0
    assert ctx["kpis"]["transportadoras_unicas"] == 1


def test_dataset_vazio_retorna_fallback():
    ctx = build_analytics_context(
        raw_bytes=b"",
        extension=".csv",
        structural_context={"dataset_validado": False, "raw_headers": []},
        delimiter_default=",",
        max_rows=1000,
    )
    assert ctx["analytics_ready"] is False
    assert ctx["kpis"]["total_documentos"] == 0


def test_nan_valor_invalido_negativo_divisao_segura():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,nan,10,2026-01-01\n"
        "B,SP,RJ,-5,0,2026-01-01\n"
        "C,SP,RJ,0,0,2026-01-01\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    assert ctx["kpis"]["total_documentos"] == 3
    assert ctx["kpis"]["ticket_medio_frete"] >= 0
    assert ctx["dataset_summary"]["invalid_numeric_rows"] >= 1
    assert ctx["dataset_summary"]["negative_value_rows"] >= 1
    assert ctx["kpis"]["percentual_fretes_zerados"] >= 0


def test_agregacoes_transportadora_uf_temporal():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,10,1,2026-01-01\n"
        "A,SP,MG,20,2,2026-01-01\n"
        "B,PR,RJ,30,3,2026-01-02\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    assert len(ctx["transportadora_stats"]) >= 2
    assert len(ctx["uf_origem_stats"]) >= 2
    assert len(ctx["uf_destino_stats"]) >= 2
    assert len(ctx["temporal_stats"]) >= 2


def test_pareto_por_uf_destino_e_transportadora_calculado_corretamente():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,0,1,2026-01-01\n"
        "A,SP,RJ,0,2,2026-01-02\n"
        "B,SP,MG,0,3,2026-01-03\n"
        "C,SP,BA,10,4,2026-01-03\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    pareto_uf = ctx["pareto_fretes_zerados_uf_destino"]
    pareto_carrier = ctx["pareto_fretes_zerados_transportadora"]
    assert [row["chave"] for row in pareto_uf] == ["RJ", "MG"]
    assert [row["quantidade"] for row in pareto_uf] == [2, 1]
    assert [row["chave"] for row in pareto_carrier] == ["A", "B"]
    assert [row["quantidade"] for row in pareto_carrier] == [2, 1]
    assert pareto_uf[-1]["percentual_acumulado"] == 100.0
    assert pareto_carrier[-1]["percentual_acumulado"] == 100.0


def test_pareto_sem_fretes_zerados_retorna_lista_vazia_segura():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,10,1,2026-01-01\n"
        "B,SP,MG,20,2,2026-01-02\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    assert ctx["pareto_fretes_zerados_uf_destino"] == []
    assert ctx["pareto_fretes_zerados_transportadora"] == []


def test_datas_invalidas_e_numeros_invalidos():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,abc,1,99/99/9999\n"
        "B,SP,RJ,10,xyz,2026-01-01\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    assert ctx["dataset_summary"]["invalid_numeric_rows"] >= 1
    assert ctx["dataset_summary"]["invalid_date_rows"] >= 1
    details = ctx["dataset_summary"]["numeric_issue_details"]
    assert details["by_reason"]["invalid_format"] >= 2


def test_valor_frete_vazio_e_zero_sem_invalido_numerico():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,,10,2026-01-01\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    assert ctx["kpis"]["total_documentos"] == 1
    assert ctx["kpis"]["valor_total_frete"] == 0.0
    assert ctx["dataset_summary"]["invalid_numeric_rows"] == 0
    details = ctx["dataset_summary"]["numeric_issue_details"]
    assert details["invalid_rows_total"] == 0
    assert details["by_column"]["valor_frete"] == 0
    assert details["by_reason"]["empty"] == 0
    assert ctx["kpis"]["percentual_fretes_zerados"] == 100.0


def test_numeric_issue_details_by_column_reason_and_samples():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,abc,2,2026-01-01\n"
        "A,SP,RJ,10,,2026-01-01\n"
        "A,SP,RJ,-5,2,2026-01-01\n"
        "A,SP,RJ,abc,xyz,2026-01-01\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    summary = ctx["dataset_summary"]
    details = summary["numeric_issue_details"]
    assert details["invalid_rows_total"] == summary["invalid_numeric_rows"] + summary["negative_value_rows"]
    assert details["by_column"]["valor_frete"] >= 2
    assert details["by_column"]["peso"] >= 1
    assert details["by_column"]["both"] >= 1
    assert details["by_reason"]["empty"] >= 1
    assert details["by_reason"]["invalid_format"] >= 3
    assert details["by_reason"]["negative"] >= 1
    assert len(details["samples"]) <= 10
    assert len(details["samples"]) > 0
    for sample in details["samples"]:
        assert set(sample.keys()) == {"line", "column", "reason", "value_preview"}
        assert sample["column"] in {"valor_frete", "peso"}
        assert sample["reason"] in {"empty", "invalid_format", "negative"}
        assert isinstance(sample["line"], int)
        assert isinstance(sample["value_preview"], str)
        assert len(sample["value_preview"]) <= 32


def test_peso_vazio_continua_invalido_com_reason_empty():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,10,,2026-01-01\n"
    )
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
    )
    summary = ctx["dataset_summary"]
    details = summary["numeric_issue_details"]
    assert summary["invalid_numeric_rows"] == 1
    assert details["by_column"]["peso"] == 1
    assert details["by_reason"]["empty"] == 1
    assert all(sample["column"] == "peso" for sample in details["samples"])


def test_limite_rows_controlado():
    lines = [
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao",
    ]
    for _ in range(500):
        lines.append("A,SP,RJ,1,1,2026-01-01")
    raw = _csv("\n".join(lines) + "\n")
    ctx = build_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=100,
        max_group_items=20,
    )
    assert ctx["kpis"]["total_documentos"] == 100


def test_sem_except_exception_generico_no_modulo():
    source = Path(__file__).resolve().parents[1] / "app" / "cleide_analytics.py"
    content = source.read_text(encoding="utf-8")
    assert "except Exception" not in content


def test_cross_filter_transportadora_interseccao_real():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,0,10,2026-01-01\n"
        "A,SP,MG,100,15,2026-01-02\n"
        "B,PR,RJ,0,20,2026-01-03\n"
    )
    ctx = build_filtered_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
        filters={"transportadora": "A"},
    )
    assert ctx["kpis"]["total_documentos"] == 2
    assert {row["chave"] for row in ctx["uf_destino_stats"]} == {"RJ", "MG"}
    assert {row["data"] for row in ctx["temporal_stats"]} == {"2026-01-01", "2026-01-02"}
    assert {row["chave"] for row in ctx["pareto_fretes_zerados_transportadora"]} == {"A"}


def test_cross_filter_combinado_por_interseccao_real():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,0,10,2026-01-01\n"
        "A,SP,MG,100,15,2026-01-02\n"
        "A,PR,RJ,50,8,2026-01-03\n"
    )
    ctx = build_filtered_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
        filters={"transportadora": "A", "uf_destino": "RJ", "uf_origem": "SP"},
    )
    assert ctx["kpis"]["total_documentos"] == 1
    assert ctx["kpis"]["valor_total_frete"] == 0.0
    assert ctx["transportadora_stats"][0]["quantidade"] == 1
    assert ctx["uf_origem_stats"][0]["chave"] == "SP"
    assert ctx["uf_destino_stats"][0]["chave"] == "RJ"
    assert ctx["temporal_stats"][0]["data"] == "2026-01-01"


def test_cross_filter_limpar_restaura_visao_global():
    raw = _csv(
        "transportadora,uf_origem,uf_destino,valor_frete,peso,data_emissao\n"
        "A,SP,RJ,0,10,2026-01-01\n"
        "B,PR,MG,100,15,2026-01-02\n"
    )
    filtered = build_filtered_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
        filters={"transportadora": "A"},
    )
    restored = build_filtered_analytics_context(
        raw_bytes=raw,
        extension=".csv",
        structural_context=_valid_structural(),
        delimiter_default=",",
        max_rows=1000,
        max_group_items=20,
        filters={},
    )
    assert filtered["kpis"]["total_documentos"] == 1
    assert restored["kpis"]["total_documentos"] == 2
