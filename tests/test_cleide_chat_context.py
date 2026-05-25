from types import SimpleNamespace

from app.cleide_chat_context import get_cleide_chat_context


def test_chat_context_contract_and_safety_flags():
    session_obj = {
        "cleide_upload_ref": "abc-ref",
        "cleide_dataset_context": {
            "operational_context": {
                "schema_version": "cleide_contexto_operacional.v1",
                "agent": "cleide",
                "namespace": "cleide",
                "phase": "8_context_prep_no_ai",
                "generated_at": "2026-01-01T10:00:00Z",
                "dataset_summary": {"linhas_processadas": 1},
                "kpis": {"total_documentos": 1},
                "aggregate_counts": {"transportadora_stats": 1},
                "aggregate_tables": {
                    "transportadora": [{"chave": "XP", "quantidade": 1, "valor_total": 10}],
                    "pareto_fretes_zerados_uf_destino": [
                        {"chave": "RJ", "quantidade": 1, "percentual": 100, "percentual_acumulado": 100}
                    ],
                },
                "quality_flags": {"has_invalid_numeric": False},
                "filter_context": {"active_filters": {}, "filter_mode": "aggregate_approximation", "kpi_scope": "global_session"},
                "semantic_limits": {"no_row_level_intersection": True},
                "language_policy": {"allowed_language": ["concentracao"], "forbidden_language": ["erro de cobranca"]},
                "security_guards": {
                    "contains_raw_dataset": False,
                    "contains_full_rows": False,
                    "contains_roberto_payload": False,
                    "contains_ai_output": False,
                },
            }
        },
    }
    out = get_cleide_chat_context(session_obj)
    assert out["chat_context_version"] == "cleide_chat_context.v1"
    assert out["chat_ready_context"] is True
    safe = out["safe_operational_context"]
    assert safe["schema_version"] == "cleide_contexto_operacional.v1"
    assert safe["security_guards"]["contains_raw_dataset"] is False
    assert safe["security_guards"]["contains_roberto_payload"] is False
    assert safe["security_guards"]["contains_ai_output"] is False
    assert "pareto_fretes_zerados_uf_destino" in safe["aggregate_tables"]


def test_chat_context_aplica_truncamento_e_limites():
    rows = [{"chave": f"T{i}", "quantidade": i, "valor_total": i * 10} for i in range(50)]
    session_obj = {
        "cleide_upload_ref": "abc-ref",
        "cleide_dataset_context": {
            "operational_context": {
                "schema_version": "cleide_contexto_operacional.v1",
                "agent": "cleide",
                "namespace": "cleide",
                "phase": "8_context_prep_no_ai",
                "generated_at": "2026-01-01T10:00:00Z",
                "dataset_summary": {},
                "kpis": {},
                "aggregate_counts": {"transportadora_stats": 50},
                "aggregate_tables": {"transportadora": rows, "uf_origem": [], "uf_destino": [], "temporal": []},
                "quality_flags": {},
                "filter_context": {
                    "active_filters": {"transportadora": "X" * 300},
                    "filter_mode": "row_level_intersection_backend",
                    "kpi_scope": "filtered_session_intersection",
                },
                "semantic_limits": {
                    "no_row_level_intersection": True,
                    "multi_dimension_filters_are_approximate": True,
                    "kpis_are_global_session_scope": True,
                    "no_accusatory_financial_conclusion": True,
                },
                "language_policy": {
                    "allowed_language": ["a"] * 30,
                    "forbidden_language": ["b"] * 30,
                },
                "security_guards": {},
            }
        },
    }
    out = get_cleide_chat_context(session_obj, max_items_per_table=10, max_text_len=80)
    safe = out["safe_operational_context"]
    assert len(safe["aggregate_tables"]["transportadora"]) == 10
    assert len(safe["filter_context"]["active_filters"]["transportadora"]) == 80
    assert safe["filter_context"]["filter_mode"] == "row_level_intersection_backend"
    assert safe["filter_context"]["kpi_scope"] == "filtered_session_intersection"
    assert len(safe["language_policy"]["allowed_language"]) <= 12
    assert len(safe["language_policy"]["forbidden_language"]) <= 12
    assert out["exposure_controls"]["truncated"] is True


def test_chat_context_respeita_cfg_e_flags(monkeypatch):
    monkeypatch.setattr(
        "app.cleide_chat_context.get_cleide_config",
        lambda: SimpleNamespace(
            chat_context_max_items_per_table=6,
            chat_context_max_text_len=50,
            chat_context_rankings_limit=4,
            chat_response_max_chars=3000,
            chat_context_include_transportadora=1,
            chat_context_include_uf_origem=0,
            chat_context_include_uf_destino=1,
            chat_context_include_temporal=0,
            chat_context_include_paretos=0,
            chat_context_mode="executivo",
            chat_context_max_chars=6000,
        ),
    )
    session_obj = {
        "cleide_upload_ref": "abc-ref",
        "cleide_dataset_context": {
            "operational_context": {
                "schema_version": "cleide_contexto_operacional.v1",
                "aggregate_tables": {
                    "transportadora": [{"chave": f"T{i}", "quantidade": i} for i in range(10)],
                    "uf_origem": [{"chave": "SP", "quantidade": 1}],
                    "uf_destino": [{"chave": "RJ", "quantidade": 2}],
                    "temporal": [{"data": "2026-01-01", "quantidade": 3}],
                    "pareto_fretes_zerados_uf_destino": [{"chave": "RJ", "quantidade": 1}],
                },
                "filter_context": {"active_filters": {}, "filter_mode": "aggregate_approximation", "kpi_scope": "global_session"},
                "language_policy": {"allowed_language": ["concentracao"], "forbidden_language": ["erro"]},
            }
        },
    }
    out = get_cleide_chat_context(session_obj)
    safe = out["safe_operational_context"]
    assert len(safe["aggregate_tables"]["transportadora"]) == 6
    assert safe["aggregate_tables"]["uf_origem"] == []
    assert safe["aggregate_tables"]["uf_destino"]
    assert safe["aggregate_tables"]["temporal"] == []
    assert safe["aggregate_tables"]["pareto_fretes_zerados_uf_destino"] == []
    assert out["exposure_controls"]["mode"] == "executivo"


def test_chat_context_modo_conservador_reduz_blocos(monkeypatch):
    monkeypatch.setattr(
        "app.cleide_chat_context.get_cleide_config",
        lambda: SimpleNamespace(
            chat_context_max_items_per_table=10,
            chat_context_max_text_len=80,
            chat_context_rankings_limit=3,
            chat_response_max_chars=3000,
            chat_context_include_transportadora=1,
            chat_context_include_uf_origem=1,
            chat_context_include_uf_destino=1,
            chat_context_include_temporal=1,
            chat_context_include_paretos=1,
            chat_context_mode="conservador",
            chat_context_max_chars=6000,
        ),
    )
    session_obj = {
        "cleide_upload_ref": "abc-ref",
        "cleide_dataset_context": {
            "operational_context": {
                "schema_version": "cleide_contexto_operacional.v1",
                "aggregate_tables": {
                    "transportadora": [{"chave": f"T{i}", "quantidade": i} for i in range(10)],
                    "uf_origem": [{"chave": "SP", "quantidade": 1}],
                    "uf_destino": [{"chave": "RJ", "quantidade": 2}],
                    "temporal": [{"data": "2026-01-01", "quantidade": 3}],
                    "pareto_fretes_zerados_uf_destino": [{"chave": "RJ", "quantidade": 1}],
                },
                "filter_context": {"active_filters": {}, "filter_mode": "aggregate_approximation", "kpi_scope": "global_session"},
                "language_policy": {"allowed_language": ["concentracao"], "forbidden_language": ["erro"]},
            }
        },
    }
    out = get_cleide_chat_context(session_obj)
    safe = out["safe_operational_context"]
    assert len(safe["aggregate_tables"]["transportadora"]) == 3
    assert safe["aggregate_tables"]["temporal"] == []
    assert safe["aggregate_tables"]["pareto_fretes_zerados_uf_destino"] == []
    assert out["exposure_controls"]["mode"] == "conservador"


def test_chat_context_sem_operational_context_reconstroi_sem_ia():
    session_obj = {
        "cleide_upload_ref": "abc-ref",
        "cleide_dataset_context": {
            "dataset_validado": True,
            "analytics_context": {
                "analytics_ready": True,
                "dataset_summary": {"linhas_processadas": 2},
                "kpis": {"total_documentos": 2},
                "aggregate_counts": {"transportadora_stats": 0},
                "transportadora_stats": [],
                "uf_origem_stats": [],
                "uf_destino_stats": [],
                "temporal_stats": [],
            },
        },
    }
    out = get_cleide_chat_context(session_obj)
    assert out["chat_ready_context"] is True
    assert out["exposure_controls"]["source"] == "rebuilt_from_analytics_context"
    blob = str(out).lower()
    assert "openai" not in blob
    assert "gemini" not in blob
    assert "processingevent" not in blob
    assert "iaconsumoevento" not in blob
