import re

from app.cleide_operational_context import build_cleide_operational_context


def test_operational_context_schema_minimo():
    ctx = build_cleide_operational_context(
        upload_ref="abc",
        dataset_validado=True,
        analytics_ready=True,
        stale_upload=False,
        dataset_summary={"linhas_processadas": 10},
        kpis={"total_documentos": 10, "valor_total_frete": 100.0},
        aggregate_tables={"transportadora": [{"chave": "XP", "quantidade": 10, "valor_total": 100.0}]},
        aggregate_counts={
            "transportadora_stats": 1,
            "pareto_fretes_zerados_uf_destino": 1,
            "pareto_fretes_zerados_transportadora": 1,
        },
        active_filters={"transportadora": "XP"},
    )
    assert ctx["schema_version"] == "cleide_contexto_operacional.v1"
    assert ctx["agent"] == "cleide"
    assert ctx["namespace"] == "cleide"
    assert ctx["phase"] == "8_context_prep_no_ai"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ctx["generated_at"])
    assert isinstance(ctx["dataset_summary"], dict)
    assert isinstance(ctx["kpis"], dict)
    assert isinstance(ctx["aggregate_tables"], dict)
    assert isinstance(ctx["aggregate_counts"], dict)
    assert "pareto_fretes_zerados_uf_destino" in ctx["aggregate_counts"]
    assert "pareto_fretes_zerados_transportadora" in ctx["aggregate_counts"]
    assert isinstance(ctx["quality_flags"], dict)
    assert isinstance(ctx["filter_context"], dict)
    assert isinstance(ctx["semantic_limits"], dict)
    assert isinstance(ctx["language_policy"], dict)
    assert isinstance(ctx["security_guards"], dict)


def test_operational_context_sem_dataset_bruto_ou_roberto():
    ctx = build_cleide_operational_context(
        upload_ref=None,
        dataset_validado=False,
        analytics_ready=False,
        stale_upload=False,
    )
    blob = str(ctx).lower()
    forbidden = [
        "dataset_raw",
        "dataframe",
        "raw_bytes",
        "roberto_snapshot",
        "roberto_prompt",
        "processingevent",
        "iaconsumoevento",
        "billing",
        "consumo",
    ]
    for token in forbidden:
        assert token not in blob
    assert ctx["security_guards"]["contains_raw_dataset"] is False
    assert ctx["security_guards"]["contains_full_rows"] is False
    assert ctx["security_guards"]["contains_roberto_payload"] is False
    assert ctx["security_guards"]["contains_ai_output"] is False


def test_operational_context_semantica_e_linguagem():
    ctx = build_cleide_operational_context(
        upload_ref="abc",
        dataset_validado=True,
        analytics_ready=True,
        stale_upload=True,
        active_filters={"transportadora": "XP", "data_inicio": "2026-01-01"},
    )
    assert ctx["filter_context"]["filter_mode"] == "aggregate_approximation"
    assert ctx["filter_context"]["kpi_scope"] == "global_session"
    limits = ctx["semantic_limits"]
    assert limits["no_row_level_intersection"] is True
    assert limits["multi_dimension_filters_are_approximate"] is True
    assert limits["kpis_are_global_session_scope"] is True
    assert limits["no_accusatory_financial_conclusion"] is True
    policy = ctx["language_policy"]
    assert "concentração operacional" in policy["allowed_language"]
    assert "comportamento atípico" in policy["allowed_language"]
    assert "variação relevante" in policy["allowed_language"]
    assert "oportunidade de investigação" in policy["allowed_language"]
    assert "dados insuficientes" in policy["allowed_language"]
    assert "tendência operacional" in policy["allowed_language"]
    assert "participação relevante" in policy["allowed_language"]
    assert "erro de cobrança" in policy["forbidden_language"]
    assert "cobrança incorreta" in policy["forbidden_language"]
    assert "transportadora errada" in policy["forbidden_language"]
    assert "valor incorreto" in policy["forbidden_language"]
    assert "divergência contratual" in policy["forbidden_language"]
    assert "conclusão financeira acusatória" in policy["forbidden_language"]
    assert "fraude" in policy["forbidden_language"]
    assert "superfaturamento" in policy["forbidden_language"]
    assert "responsabilidade financeira" in policy["forbidden_language"]


def test_operational_context_permite_modo_intersecao_real():
    ctx = build_cleide_operational_context(
        upload_ref="abc",
        dataset_validado=True,
        analytics_ready=True,
        filter_mode="row_level_intersection_backend",
        kpi_scope="filtered_session_intersection",
        no_row_level_intersection=False,
        multi_dimension_filters_are_approximate=False,
        kpis_are_global_session_scope=False,
    )
    assert ctx["filter_context"]["filter_mode"] == "row_level_intersection_backend"
    assert ctx["filter_context"]["kpi_scope"] == "filtered_session_intersection"
    assert ctx["semantic_limits"]["no_row_level_intersection"] is False
    assert ctx["semantic_limits"]["multi_dimension_filters_are_approximate"] is False
    assert ctx["semantic_limits"]["kpis_are_global_session_scope"] is False
