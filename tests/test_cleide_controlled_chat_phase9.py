import pytest

import app.cleide_controlled_chat as controlled_chat
from app.cleide_formatters import (
    format_brl,
    format_date_ptbr,
    format_integer,
    format_percent,
    format_weight,
)
from app.cleide_controlled_chat import (
    CONVERSATIONAL_CONTRACT_VERSION,
    CONVERSATIONAL_PHASE,
    MAX_QUESTION_LEN,
    MAX_RESPONSE_LEN,
    POLICY_SAFE_FALLBACK_REPLY,
    TRANSITION_AUDIT_MARKER,
    run_cleide_controlled_chat,
)
from app.cleide_language_policy import CLEIDE_ALLOWED_LANGUAGE, CLEIDE_FORBIDDEN_LANGUAGE


def _chat_context(*, total_docs=100, dataset_validado=True):
    return {
        "chat_context_version": "cleide_chat_context.v1",
        "chat_ready_context": True,
        "safe_operational_context": {
            "schema_version": "cleide_contexto_operacional.v1",
            "session_scope": {"dataset_validado": dataset_validado},
            "kpis": {
                "total_documentos": total_docs,
                "valor_total_frete": 12345.67,
                "peso_total": 54321.0,
                "ticket_medio_frete": 123.45,
                "percentual_fretes_zerados": 4.5,
                "periodo_dataset": {"inicio": "2026-01-01", "fim": "2026-01-31"},
            },
            "aggregate_tables": {
                "transportadora": [
                    {"chave": "XP", "quantidade": 80},
                    {"chave": "YZ", "quantidade": 20},
                ],
                "uf_origem": [{"chave": "SP", "quantidade": 70}],
                "uf_destino": [{"chave": "RJ", "quantidade": 60}],
                "temporal": [{"data": "2026-01", "quantidade": 100}],
            },
            "dataset_summary": {
                "linhas_processadas": 100,
                "invalid_numeric_rows": 2,
                "invalid_date_rows": 1,
                "negative_value_rows": 0,
            },
            "quality_flags": {
                "has_invalid_numeric": True,
                "has_invalid_date": True,
                "has_negative_values": False,
                "has_sparse_aggregates": False,
            },
            "filter_context": {
                "active_filters": {},
                "filter_mode": "row_level_intersection_backend",
                "kpi_scope": "filtered_session_intersection",
            },
            "semantic_limits": {
                "no_row_level_intersection": False,
                "multi_dimension_filters_are_approximate": False,
                "kpis_are_global_session_scope": False,
                "no_accusatory_financial_conclusion": True,
            },
            "language_policy": {
                "allowed_language": [
                    "concentracao operacional",
                    "comportamento atipico",
                    "variacao relevante",
                    "oportunidade de investigacao",
                    "dados insuficientes",
                    "tendencia operacional",
                    "participacao relevante",
                ],
                "forbidden_language": [
                    "erro de cobranca",
                    "cobranca incorreta",
                    "transportadora errada",
                    "valor incorreto",
                    "divergencia contratual",
                    "fraude",
                    "superfaturamento",
                    "responsabilidade financeira",
                    "conclusao financeira acusatoria",
                ],
            },
            "security_guards": {
                "contains_raw_dataset": False,
                "contains_full_rows": False,
                "contains_roberto_payload": False,
                "contains_ai_output": False,
            },
        },
        "exposure_controls": {"max_items_per_table": 10, "max_text_len": 80, "truncated": False},
    }


def _patch_context(monkeypatch, chat_ctx):
    monkeypatch.setattr("app.cleide_controlled_chat.get_cleide_chat_context", lambda _session: chat_ctx)


def _norm(text: str) -> str:
    return controlled_chat._normalize_for_match(text)  # noqa: SLF001 - helper de teste


@pytest.fixture(autouse=True)
def _reset_cleide_breaker_state():
    controlled_chat._circuit_breaker_state["open_until_monotonic"] = 0.0  # noqa: SLF001
    controlled_chat._circuit_breaker_state["reason"] = ""  # noqa: SLF001
    controlled_chat._circuit_breaker_state["policy_blocked_streak"] = 0  # noqa: SLF001
    yield
    controlled_chat._circuit_breaker_state["open_until_monotonic"] = 0.0  # noqa: SLF001
    controlled_chat._circuit_breaker_state["reason"] = ""  # noqa: SLF001
    controlled_chat._circuit_breaker_state["policy_blocked_streak"] = 0  # noqa: SLF001


def _ai_flag_env(monkeypatch, enabled=True):
    if enabled:
        monkeypatch.setenv("CLEIDE_AI_ENABLED_LOCAL", "true")
        monkeypatch.setenv("GEMINI_API_KEY_ROBERTO", "test-cleide-key")
    else:
        monkeypatch.delenv("CLEIDE_AI_ENABLED_LOCAL", raising=False)
        monkeypatch.delenv("CLEIDE_AI_ENABLED", raising=False)
        monkeypatch.delenv("GEMINI_API_KEY_ROBERTO", raising=False)


def test_intents_operacionais_controladas(monkeypatch):
    _ai_flag_env(monkeypatch, enabled=False)
    _patch_context(monkeypatch, _chat_context())
    prompts = {
        "resumo operacional": "resumo_operacional",
        "top transportadoras": "top_transportadoras",
        "UF origem": "uf_origem",
        "UF destino": "uf_destino",
        "periodo dataset": "periodo_dataset",
        "qualidade dataset": "qualidade_dataset",
        "quantidade documentos": "quantidade_documentos",
        "ticket medio": "ticket_medio",
        "peso total": "peso_total",
        "fretes zerados": "fretes_zerados",
    }
    for question, expected_intent in prompts.items():
        body, status = run_cleide_controlled_chat(question=question, session_obj={})
        assert status == 200
        assert body["intent"] == expected_intent
        assert body["ai_enabled"] is False
        assert body["mode"] == "controlled_templates_no_ai_phase_9"
        assert body["reply"]


def test_formatters_ptbr_basicos():
    assert format_brl(141030.83) == "R$ 141.030,83"
    assert format_percent(18.53) == "18,53%"
    assert format_integer(8575) == "8.575"
    assert format_weight(1350.25) == "1.350,25 kg"
    assert format_date_ptbr("2026-05-18") == "18/05/2026"


def test_fallback_intent_desconhecida(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(question="me fale de contratos juridicos", session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] in {"fallback_fora_de_escopo", "fallback_intent_desconhecida"}


def test_bloqueio_linguagem_proibida(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(
        question="isso parece fraude e superfaturamento",
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] == "fallback_bloqueio_semantico"


def test_sem_narrativa_acusatoria_nem_inferencia_financeira(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(
        question="responsabilidade financeira da transportadora?",
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] in {"fallback_bloqueio_semantico", "fallback_fora_de_escopo"}


def test_sem_inventar_numeros_em_dataset_insuficiente(monkeypatch):
    _patch_context(monkeypatch, _chat_context(total_docs=0, dataset_validado=False))
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["intent"] == "dados_insuficientes"
    assert "Dados insuficientes" in body["reply"]


def test_sem_referencia_roberto_e_sem_ia_real(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    blob = str(body).lower()
    assert "roberto" not in blob
    assert "openai" not in blob
    assert "flow_type_roberto" not in blob
    assert "llm" not in blob
    assert "processingevent" not in blob
    assert "iaconsumoevento" not in blob


def test_limites_pergunta_vazia_e_longa(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body_empty, status_empty = run_cleide_controlled_chat(question="   ", session_obj={})
    assert status_empty == 200
    assert body_empty["fallback_code"] == "fallback_pergunta_invalida"

    body_long, status_long = run_cleide_controlled_chat(question="x" * (MAX_QUESTION_LEN + 1), session_obj={})
    assert status_long == 200
    assert body_long["fallback_code"] == "fallback_pergunta_muito_longa"


def test_truncamento_de_resposta(monkeypatch):
    ctx = _chat_context()
    ctx["safe_operational_context"]["aggregate_tables"]["transportadora"] = [
        {"chave": "transportadora-super-longa-" + ("x" * 100), "quantidade": 9999},
        {"chave": "outra-super-longa-" + ("y" * 100), "quantidade": 8888},
        {"chave": "mais-uma-super-longa-" + ("z" * 100), "quantidade": 7777},
    ]
    _patch_context(monkeypatch, ctx)
    body, status = run_cleide_controlled_chat(question="top transportadoras", session_obj={})
    assert status == 200
    assert len(body["reply"]) <= MAX_RESPONSE_LEN
    assert isinstance(body["response_truncated"], bool)


def test_fallback_contexto_indisponivel(monkeypatch):
    _patch_context(
        monkeypatch,
        {
            "chat_context_version": "cleide_chat_context.v1",
            "chat_ready_context": False,
            "safe_operational_context": {},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] == "fallback_contexto_indisponivel"
    assert body["fallback_used"] is True
    assert body["ai_used"] is False
    assert body["error_code"] == "unsafe_context"


def test_semantic_limits_e_filtros_preservados(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["semantic_limits"]["no_row_level_intersection"] is False
    assert body["semantic_limits"]["kpis_are_global_session_scope"] is False
    assert body["filter_mode"] == "row_level_intersection_backend"
    assert body["kpi_scope"] == "filtered_session_intersection"
    assert body["view_scope"] == "global"
    assert body["active_filters"] == {}


def test_pergunta_roberto_permanece_bloqueada_sem_acionar_ia(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    called = {"count": 0}

    def _fake_ai(**_kwargs):
        called["count"] += 1
        return {"ok": True, "reply": "concentracao operacional"}

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(
        question="O Roberto validou esse frete?",
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] in {"fallback_fora_de_escopo", "fallback_intent_desconhecida"}
    assert body["fallback_used"] is True
    assert called["count"] == 0


def test_pergunta_julia_permanece_bloqueada_sem_acionar_ia(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    called = {"count": 0}

    def _fake_ai(**_kwargs):
        called["count"] += 1
        return {"ok": True, "reply": "concentracao operacional"}

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(
        question="A Julia consegue explicar esse caso?",
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] in {"fallback_fora_de_escopo", "fallback_intent_desconhecida"}
    assert body["fallback_used"] is True
    assert called["count"] == 0


def test_resposta_com_numeros_mantem_formatacao_ptbr(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(question="ticket medio", session_obj={})
    assert status == 200
    assert body["intent"] == "ticket_medio"
    assert "R$" in body["reply"]
    assert "123,45" in body["reply"]
    assert body["context_status"] == "ready"
    assert body["ai_flow_type"] in {"", "cleide_chat_auditoria_frete_ai"}


def test_chat_scope_filtered_quando_filtros_ativos(monkeypatch):
    ctx = _chat_context()
    ctx["safe_operational_context"]["filter_context"]["active_filters"] = {
        "transportadora": "XP",
        "data_inicio": "2026-01-01",
    }
    _patch_context(monkeypatch, ctx)
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["view_scope"] == "filtered"
    assert body["active_filters"]["transportadora"] == "XP"
    assert body["active_filters"]["data_inicio"] == "2026-01-01"
    assert body["context_status"] == "ready"


def test_chat_context_status_insufficient_sem_dataset(monkeypatch):
    _patch_context(monkeypatch, _chat_context(total_docs=0, dataset_validado=False))
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["context_status"] == "insufficient"


def test_chat_context_status_stale_upload(monkeypatch):
    ctx = _chat_context()
    ctx["safe_operational_context"]["session_scope"]["stale_upload"] = True
    _patch_context(monkeypatch, ctx)
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["context_status"] == "stale"


def test_governanca_semantica_sem_drift_allowed_forbidden(monkeypatch):
    _ai_flag_env(monkeypatch, enabled=False)
    ctx = _chat_context()
    assert set(ctx["safe_operational_context"]["language_policy"]["allowed_language"]) == set(CLEIDE_ALLOWED_LANGUAGE)
    assert set(ctx["safe_operational_context"]["language_policy"]["forbidden_language"]) == set(CLEIDE_FORBIDDEN_LANGUAGE)
    _patch_context(monkeypatch, ctx)
    body, status = run_cleide_controlled_chat(question="top transportadoras", session_obj={})
    assert status == 200
    reply_norm = _norm(body["reply"])
    assert "participacao relevante" in reply_norm
    assert "variacao relevante" in reply_norm


def test_contract_metadata_e_auditoria_transicao_presentes(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["contract_version"] == CONVERSATIONAL_CONTRACT_VERSION
    assert body["phase"] == CONVERSATIONAL_PHASE
    assert body["audit_transition_marker"] == TRANSITION_AUDIT_MARKER
    assert body["audit_notes"]["legacy_chat_status"] == 501
    assert body["audit_notes"]["current_chat_status"] == 200
    assert body["audit_notes"]["policy_source"] == "app.cleide_language_policy"


def test_fallback_tambem_tem_metadata_auditavel(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(question="", session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["contract_version"] == CONVERSATIONAL_CONTRACT_VERSION
    assert body["phase"] == CONVERSATIONAL_PHASE
    assert body["audit_transition_marker"] == TRANSITION_AUDIT_MARKER
    assert body["audit_notes"]["legacy_chat_status"] == 501
    assert body["audit_notes"]["current_chat_status"] == 200
    assert body["audit_notes"]["policy_source"] == "app.cleide_language_policy"


def test_nao_existe_lista_semantica_hardcoded_paralela():
    assert not hasattr(controlled_chat, "ENGINE_SEMANTIC_TERMS")


def test_fallbacks_alinhados_a_allowed_policy(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    cases = [
        "",
        "x" * (MAX_QUESTION_LEN + 1),
        "isso e fraude",
        "tema juridico fora do escopo",
        "algo totalmente desconhecido sem intent",
    ]
    for question in cases:
        body, status = run_cleide_controlled_chat(question=question, session_obj={})
        assert status == 200
        assert body["audit_notes"]["legacy_chat_status"] == 501
        assert body["audit_notes"]["current_chat_status"] == 200
        reply = body.get("reply") or ""
        assert reply.strip()
        assert body.get("policy_block_reason_code", "") == ""


def test_runtime_enforcement_ignora_policy_mutada_no_safe_context(monkeypatch):
    ctx = _chat_context()
    ctx["safe_operational_context"]["language_policy"]["allowed_language"] = ["termo inventado"]
    ctx["safe_operational_context"]["language_policy"]["forbidden_language"] = []
    _patch_context(monkeypatch, ctx)
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["intent"] == "resumo_operacional"
    # Se runtime lesse safe_context mutado, este termo apareceria.
    assert "termo inventado" not in (body.get("reply") or "").lower()


def test_runtime_enforcement_bloqueia_forbidden_mesmo_com_payload_permissivo(monkeypatch):
    ctx = _chat_context()
    ctx["safe_operational_context"]["language_policy"]["forbidden_language"] = []
    _patch_context(monkeypatch, ctx)
    body, status = run_cleide_controlled_chat(question="fraude operacional", session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] == "fallback_bloqueio_semantico"


def test_semantic_enforcement_rejeita_resposta_hibrida():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001 - teste de hardening interno
        "concentracao operacional e fraude",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is False


def test_semantic_enforcement_aprova_linguagem_executiva_natural_segura():
    reply = (
        "A maior concentracao operacional esta nas UFs com maior volume de frete. "
        "Priorize a verificacao dos rankings de UF destino e transportadora para identificar oportunidades de auditoria."
    )
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        controlled_chat._normalize_for_match(reply),  # noqa: SLF001
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
        out_of_scope_terms=controlled_chat.normalized_out_of_scope_language(),
        safe_operational_context=_chat_context()["safe_operational_context"],
    )
    assert ok is True


def test_semantic_enforcement_rejeita_referencia_roberto_julia():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "concentracao operacional com analise roberto",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
        out_of_scope_terms=controlled_chat.normalized_out_of_scope_language(),
        safe_operational_context=_chat_context()["safe_operational_context"],
    )
    assert ok is False


def test_semantic_enforcement_rejeita_fora_dominio():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "recomendacao de futebol para o fim de semana",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
        out_of_scope_terms=controlled_chat.normalized_out_of_scope_language(),
        safe_operational_context=_chat_context()["safe_operational_context"],
    )
    assert ok is False


def test_semantic_enforcement_rejeita_fallback_hibrido(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    body, status = run_cleide_controlled_chat(
        question="",
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    # fallback final deve respeitar policy oficial
    reply = _norm(body.get("reply") or "")
    assert "qualidade do dataset" not in reply
    assert any(_norm(term) in reply for term in CLEIDE_ALLOWED_LANGUAGE)


def test_semantic_enforcement_rejeita_permitido_mais_invalido():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "participacao relevante com erro de cobranca",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is False


def test_semantic_enforcement_rejeita_sem_termo_permitido():
    eval_data = controlled_chat._evaluate_reply_policy(  # noqa: SLF001
        controlled_chat._normalize_for_match("resposta neutra sem termo semantico oficial"),  # noqa: SLF001
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
        out_of_scope_terms=controlled_chat.normalized_out_of_scope_language(),
        safe_operational_context=_chat_context()["safe_operational_context"],
    )
    assert eval_data["ok"] is True
    assert eval_data["reason_code"] == ""
    assert eval_data["warning_reason_code"] == "domain_signal_missing"


def test_phase_9_1_1_policy_whitelist_permitido_puro_aprovado():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "concentracao operacional",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is True


def test_phase_9_1_1_policy_whitelist_proibido_oficial_rejeitado():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "fraude",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is False


def test_phase_9_1_1_policy_whitelist_permitido_mais_proibido_rejeitado():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "concentracao operacional fraude",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is False


def test_phase_9_1_1_policy_whitelist_permitido_mais_desconhecido_rejeitado():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "concentracao operacional alfa",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is True


def test_phase_9_1_1_policy_whitelist_fallback_mais_desconhecido_rejeitado():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "dados insuficientes beta",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is True


def test_phase_9_1_1_policy_whitelist_template_hibrido_rejeitado():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "participacao relevante 100 xyz",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is True


def test_phase_9_1_1_policy_whitelist_drift_semantico_arbitrario_rejeitado():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "tendencia operacional narrativa livre fora da policy",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is True


def test_phase_9_1_1_policy_whitelist_sem_permitido_oficial_rejeitado():
    eval_data = controlled_chat._evaluate_reply_policy(  # noqa: SLF001
        controlled_chat._normalize_for_match("narrativa sem composicao oficial"),  # noqa: SLF001
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
        out_of_scope_terms=controlled_chat.normalized_out_of_scope_language(),
        safe_operational_context=_chat_context()["safe_operational_context"],
    )
    assert eval_data["ok"] is True
    assert eval_data["warning_reason_code"] == "domain_signal_missing"


def test_phase_9_1_1_policy_whitelist_totalmente_aderente_aprovado():
    ok = controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        "participacao relevante 100 20 30 variacao relevante concentracao operacional oportunidade de investigacao",
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )
    assert ok is True


def test_phase_9_1_1_e2e_dataset_insuficiente_sem_bypass(monkeypatch):
    _patch_context(monkeypatch, _chat_context(total_docs=0, dataset_validado=False))
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["intent"] == "dados_insuficientes"
    assert "dados insuficientes" in body["reply"].lower()
    assert controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        controlled_chat._normalize_for_match(body["reply"]),  # noqa: SLF001
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )


def test_phase_9_1_1_e2e_intent_sem_resposta_sem_bypass(monkeypatch):
    _ai_flag_env(monkeypatch, enabled=False)
    _patch_context(monkeypatch, _chat_context())
    monkeypatch.setattr("app.cleide_controlled_chat._reply_for_intent", lambda _intent, _ctx: "")
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["intent"] == "dados_insuficientes"
    assert "dados insuficientes" in body["reply"].lower()
    assert controlled_chat._reply_respects_allowed_policy(  # noqa: SLF001
        controlled_chat._normalize_for_match(body["reply"]),  # noqa: SLF001
        allowed_terms=controlled_chat.normalized_allowed_language(),
        forbidden_terms=controlled_chat.normalized_forbidden_language(),
    )


def test_phase_9_1_1_build_success_blindagem_runtime_total():
    body = controlled_chat._build_success(  # noqa: SLF001
        intent="resumo_operacional",
        reply="texto fora da policy com drift contaminado",
        chat_ctx=_chat_context(),
    )
    assert body["reply"] == "texto fora da policy com drift contaminado"


def test_phase_9_1_1_runtime_sem_escape_textual_em_sucesso(monkeypatch):
    _ai_flag_env(monkeypatch, enabled=False)
    _patch_context(monkeypatch, _chat_context())
    monkeypatch.setattr(
        "app.cleide_controlled_chat._build_top_reply",
        lambda **_kwargs: "contaminacao semantica fora da policy",
    )
    body, status = run_cleide_controlled_chat(question="top transportadoras", session_obj={})
    assert status == 200
    assert body["intent"] == "top_transportadoras"
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False
    assert body["policy_warning_reason_code"] == "domain_signal_missing"


def test_ai_desligada_mantem_fluxo_atual(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["ai_enabled"] is False
    assert body["ai_used"] is False
    assert body["policy_block_reason_code"] == ""
    assert body["policy_warning_reason_code"] == ""
    assert body["mode"] == "controlled_templates_no_ai_phase_9"


def test_ai_ligada_chama_adapter(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)

    called = {"count": 0}

    def _fake_ai(*, question, safe_operational_context):
        called["count"] += 1
        assert question == "resumo operacional"
        assert "dataset_summary" in safe_operational_context
        return {
            "ok": True,
            "reply": "concentracao operacional variacao relevante tendencia operacional oportunidade de investigacao",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 13,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        }

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert called["count"] == 1
    assert body["ai_enabled"] is True
    assert body["ai_used"] is True
    assert body["provider"] == "gemini"
    assert body["mode"] == "controlled_templates_gemini_supervised_phase_12"
    assert body["token_usage"]["total_tokens"] == 15
    assert body["flow_type"] == "cleide_chat_auditoria_frete"
    assert body["ai_flow_type"] == "cleide_chat_auditoria_frete_ai"


def test_unknown_operacional_uf_aciona_ia_supervisionada(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    called = {"count": 0}

    def _fake_ai(*, question, safe_operational_context):
        called["count"] += 1
        assert question == "Quais UFs possuem frete relacionado?"
        assert "aggregate_tables" in safe_operational_context
        return {
            "ok": True,
            "reply": "A concentracao operacional de frete nas UFs indica variacao relevante e oportunidade de investigacao.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 11,
            "usage": {"input_tokens": 9, "output_tokens": 8, "total_tokens": 17},
        }

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(
        question="Quais UFs possuem frete relacionado?",
        session_obj={},
    )
    assert status == 200
    assert called["count"] == 1
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["intent"] == "unknown"


def test_unknown_operacional_concentracao_aciona_ia_supervisionada(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    called = {"count": 0}

    def _fake_ai(**_kwargs):
        called["count"] += 1
        return {
            "ok": True,
            "reply": "Existe concentracao operacional com variacao relevante e oportunidade de investigacao.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 10,
            "usage": {"input_tokens": 7, "output_tokens": 7, "total_tokens": 14},
        }

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(
        question="Onde existe maior concentracao operacional?",
        session_obj={},
    )
    assert status == 200
    assert called["count"] == 1
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["intent"] == "unknown"


def test_unknown_operacional_transportadora_aciona_ia_supervisionada(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    called = {"count": 0}

    def _fake_ai(**_kwargs):
        called["count"] += 1
        return {
            "ok": True,
            "reply": "A participacao relevante no ranking de transportadoras mostra concentracao operacional e oportunidade de investigacao.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 12,
            "usage": {"input_tokens": 8, "output_tokens": 9, "total_tokens": 17},
        }

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(
        question="Qual transportadora merece atencao?",
        session_obj={},
    )
    assert status == 200
    assert called["count"] == 1
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["intent"] == "unknown"


def test_memoria_transportadora_lidera_terceira_posicao_aciona_ia(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    captured = {}

    def _fake_ai(**kwargs):
        captured["history"] = kwargs.get("history")
        return {
            "ok": True,
            "reply": "A terceira posicao no ranking de transportadoras e da transportadora YZ.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 9,
            "usage": {"input_tokens": 8, "output_tokens": 8, "total_tokens": 16},
        }

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(
        question="Quem esta em terceiro?",
        history=[
            {"role": "user", "content": "Qual transportadora lidera?"},
            {"role": "assistant", "content": "XP lidera o ranking de transportadoras."},
        ],
        session_obj={},
    )
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False
    assert isinstance(captured.get("history"), list)
    assert len(captured["history"]) == 2


def test_memoria_uf_lidera_segunda_aciona_ia(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "A segunda UF no ranking atual e SP.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 9,
            "usage": {"input_tokens": 8, "output_tokens": 8, "total_tokens": 16},
        },
    )
    body, status = run_cleide_controlled_chat(
        question="E a segunda?",
        history=[
            {"role": "user", "content": "Qual UF lidera?"},
            {"role": "assistant", "content": "RJ lidera na visao atual."},
        ],
        session_obj={},
    )
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False


def test_memoria_maior_custo_menor_aciona_ia(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "O menor custo observado no recorte atual e da UF RJ.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 9,
            "usage": {"input_tokens": 8, "output_tokens": 8, "total_tokens": 16},
        },
    )
    body, status = run_cleide_controlled_chat(
        question="E o menor?",
        history=[
            {"role": "user", "content": "Qual maior custo?"},
            {"role": "assistant", "content": "SP aparece com maior custo no ranking."},
        ],
        session_obj={},
    )
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False


def test_memoria_isolada_quem_terceiro_sem_historico_permanece_unknown(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    called = {"count": 0}

    def _fake_ai(**kwargs):
        called["count"] += 1
        return {"ok": True, "reply": "nao deveria acionar"}

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(question="Quem esta em terceiro?", session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] == "fallback_intent_desconhecida"
    assert called["count"] == 0


def test_unknown_operacional_com_ia_desligada_permanece_fallback(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(question="Quais UFs possuem frete relacionado?", session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] == "fallback_intent_desconhecida"
    assert body["ai_used"] is False
    assert body["fallback_used"] is True


def test_unknown_fora_dominio_permanece_bloqueado(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    called = {"count": 0}

    def _fake_ai(**_kwargs):
        called["count"] += 1
        return {"ok": True, "reply": "concentracao operacional"}

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(question="como fazer imposto de renda", session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] in {"fallback_fora_de_escopo", "fallback_intent_desconhecida"}
    assert called["count"] == 0


def test_ai_sem_franquia_nao_chama_adapter(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat._resolve_authz",
        lambda: {"permitido": False, "motivo": "blocked"},
    )
    called = {"count": 0}

    def _fake_ai(**kwargs):
        called["count"] += 1
        return {"ok": False}

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert called["count"] == 0
    assert body["ai_used"] is False
    assert body["reason"] in {"deterministic_success", "deterministic_intent_without_reply"}


def test_erro_gemini_cai_no_fallback_deterministico(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": False,
            "error_code": "provider_error",
            "reason": "upstream",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 50,
            "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is True
    assert body["error_code"] == "provider_error"
    assert body["policy_block_reason_code"] == ""
    assert body["policy_warning_reason_code"] == ""
    assert body["reply"]
    assert body["mode"] == "controlled_templates_no_ai_phase_9"


def test_erro_gemini_unknown_operacional_cai_em_fallback_governado(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **_kwargs: {
            "ok": False,
            "error_code": "provider_error",
            "reason": "upstream",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 44,
            "usage": {"input_tokens": 3, "output_tokens": 0, "total_tokens": 3},
        },
    )
    body, status = run_cleide_controlled_chat(
        question="Quais UFs possuem frete relacionado?",
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] == "fallback_intent_desconhecida"
    assert body["ai_used"] is True
    assert body["fallback_used"] is True
    assert body["error_code"] == "provider_error"


def test_resposta_ai_contaminada_bloqueia_com_policy(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "fraude e erro de cobranca confirmados",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 8,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["ai_used"] is True
    assert body["policy_blocked"] is True
    assert body["policy_block_reason_code"] == "forbidden_terms"
    assert body["policy_warning_reason_code"] == ""
    assert body["fallback_used"] is True


def test_ai_resposta_executiva_natural_aprovada_sem_policy_blocked(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": (
                "A maior concentracao operacional esta nas UFs com maior volume de frete. "
                "Priorize a verificacao dos rankings de UF destino e transportadora para identificar oportunidades de auditoria."
            ),
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 9,
            "usage": {"input_tokens": 12, "output_tokens": 9, "total_tokens": 21},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False
    assert body["policy_block_reason_code"] == ""
    assert body["policy_warning_reason_code"] == ""
    assert "maior concentracao operacional" in body["reply"].lower()


def test_ai_resposta_fora_dominio_bloqueada(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "O melhor filme da semana eh uma comedia romantica.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 7,
            "usage": {"input_tokens": 8, "output_tokens": 8, "total_tokens": 16},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["policy_blocked"] is True
    assert body["fallback_used"] is True
    assert body["error_code"] == "policy_blocked"


def test_ai_resposta_com_referencia_roberto_bloqueada(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "Concentracao operacional. Roberto confirma este resultado.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 7,
            "usage": {"input_tokens": 8, "output_tokens": 8, "total_tokens": 16},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["policy_blocked"] is True
    assert body["policy_block_reason_code"] == "roberto_julia"
    assert body["policy_warning_reason_code"] == ""
    assert body["fallback_used"] is True
    assert body["error_code"] == "policy_blocked"


def test_ai_resposta_com_entidade_desconhecida_aprovada_com_warning(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": (
                "Concentracao operacional na transportadora desconhecida com variacao relevante "
                "e oportunidade de investigacao."
            ),
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 9,
            "usage": {"input_tokens": 8, "output_tokens": 9, "total_tokens": 17},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False
    assert body["policy_block_reason_code"] == ""
    assert body["policy_warning_reason_code"] == "entity_unknown"
    assert body["error_code"] == ""


def test_ai_resposta_dados_insuficientes_aprovada(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "Dados insuficientes para leitura conclusiva neste momento.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 6,
            "usage": {"input_tokens": 5, "output_tokens": 6, "total_tokens": 11},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False
    assert body["policy_block_reason_code"] == ""
    assert body["policy_warning_reason_code"] == ""
    assert "dados insuficientes" in body["reply"].lower()


def test_ai_resposta_com_sinal_operacional_fraco_aprovada_com_warning(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "Analise executiva sintetica para priorizacao estrategica.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 10,
            "usage": {"input_tokens": 7, "output_tokens": 7, "total_tokens": 14},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False
    assert body["policy_block_reason_code"] == ""
    assert body["policy_warning_reason_code"] == "domain_signal_missing"


def test_ai_resposta_com_dataset_bruto_bloqueada(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "Linha 12345 com CPF 12345678901 e chave de acesso XML.",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 7,
            "usage": {"input_tokens": 8, "output_tokens": 8, "total_tokens": 16},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["policy_blocked"] is True
    assert body["policy_block_reason_code"] == "raw_dataset_or_row_pattern"
    assert body["policy_warning_reason_code"] == ""
    assert body["error_code"] == "policy_blocked"


def test_ai_resposta_transportadora_conhecida_ou_desconhecida_sem_risco_passa(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": (
                "A transportadora xp e a transportadora desconhecida concentram variacao operacional relevante, "
                "com oportunidade de investigacao."
            ),
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 9,
            "usage": {"input_tokens": 9, "output_tokens": 10, "total_tokens": 19},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["ai_used"] is True
    assert body["fallback_used"] is False
    assert body["policy_blocked"] is False
    assert body["policy_block_reason_code"] == ""
    assert body["policy_warning_reason_code"] == "entity_unknown"


def test_observabilidade_metadados_preenchidos(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "concentracao operacional variacao relevante oportunidade de investigacao",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 21,
            "usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        },
    )
    body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
    assert status == 200
    assert body["provider"] == "gemini"
    assert body["model"] == "gemini-2.5-flash"
    assert body["latency_ms"] == 21
    assert body["token_usage"]["input_tokens"] == 11
    assert body["error_code"] == ""


def test_circuit_breaker_abre_em_policy_blocked_repetido(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=True)
    controlled_chat._circuit_breaker_state["open_until_monotonic"] = 0.0
    controlled_chat._circuit_breaker_state["policy_blocked_streak"] = 0

    monkeypatch.setattr(
        "app.cleide_controlled_chat.generate_cleide_ai_reply",
        lambda **kwargs: {
            "ok": True,
            "reply": "fraude",
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "latency_ms": 8,
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        },
    )
    for _ in range(controlled_chat.POLICY_BLOCKED_BREAKER_THRESHOLD):
        body, status = run_cleide_controlled_chat(question="resumo operacional", session_obj={})
        assert status == 200
        assert body["policy_blocked"] is True

    assert controlled_chat._circuit_breaker_state["open_until_monotonic"] > 0.0


@pytest.mark.parametrize(
    ("question", "history", "expected_intent"),
    [
        ("E UF?", [{"role": "user", "content": "Qual UF origem mais aparece?"}], "uf_origem"),
        ("E origem?", [{"role": "user", "content": "Qual UF destino lidera?"}], "uf_origem"),
        ("E destino?", [{"role": "user", "content": "Qual UF origem lidera?"}], "uf_destino"),
        ("E a segunda?", [{"role": "user", "content": "Qual transportadora lidera?"}], "top_transportadoras"),
        ("E terceira?", [{"role": "user", "content": "Qual UF destino lidera?"}], "dados_insuficientes"),
        ("E transportadora?", [{"role": "user", "content": "Qual UF destino lidera?"}], "top_transportadoras"),
        ("E modal?", [{"role": "user", "content": "Qual transportadora lidera?"}], "modal_operacional"),
        ("E período?", [{"role": "user", "content": "Qual modal lidera?"}], "periodo_dataset"),
    ],
)
def test_followup_contextual_curto_resolve_sem_fallback(monkeypatch, question, history, expected_intent):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(
        question=question,
        history=history,
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == expected_intent
    assert body["fallback_used"] is False


@pytest.mark.parametrize(
    ("question", "history", "expected_intent"),
    [
        (
            "E UF?",
            [{"role": "user", "content": "Qual cidade possui maior volume de frete?"}],
            "uf_origem",
        ),
        (
            "E UF?",
            [{"role": "user", "content": "Qual cidade possui maior volume de embarque?"}],
            "uf_origem",
        ),
        (
            "E UF?",
            [{"role": "user", "content": "Qual cidade de destino possui maior volume de frete?"}],
            "uf_destino",
        ),
    ],
)
def test_followup_contextual_cidade_para_uf(monkeypatch, question, history, expected_intent):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(
        question=question,
        history=history,
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == expected_intent
    assert body["fallback_used"] is False


def test_followup_contextual_curto_filtro(monkeypatch):
    ctx = _chat_context()
    ctx["safe_operational_context"]["filter_context"]["active_filters"] = {"uf_origem": "SP", "transportadora": "XP"}
    _patch_context(monkeypatch, ctx)
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(
        question="E filtro?",
        history=[{"role": "user", "content": "Qual UF destino lidera?"}],
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "filtro_operacional"
    assert body["fallback_used"] is False
    assert "filtros ativos" in body["reply"].lower()


def test_followup_contextual_curto_sem_historico_permanece_fallback(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(question="E UF?", history=[], session_obj={})
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] == "fallback_intent_desconhecida"


def test_followup_contextual_curto_contexto_insufficient_permanece_fallback(monkeypatch):
    _patch_context(monkeypatch, _chat_context(total_docs=0, dataset_validado=False))
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(
        question="E UF?",
        history=[{"role": "user", "content": "Qual UF origem mais aparece?"}],
        session_obj={},
    )
    assert status == 200
    assert body["intent"] in {"fallback_seguro", "dados_insuficientes"}
    if body["intent"] == "fallback_seguro":
        assert body["fallback_code"] == "fallback_intent_desconhecida"


def test_followup_contextual_curto_contexto_stale_resolve(monkeypatch):
    ctx = _chat_context()
    ctx["safe_operational_context"]["session_scope"]["stale_upload"] = True
    _patch_context(monkeypatch, ctx)
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(
        question="E origem?",
        history=[{"role": "user", "content": "Qual UF destino lidera?"}],
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "uf_origem"
    assert body["context_status"] == "stale"
    assert body["fallback_used"] is False


@pytest.mark.parametrize("ai_enabled", [False, True])
def test_followup_contextual_curto_independe_de_ia(monkeypatch, ai_enabled):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=ai_enabled)
    called = {"count": 0}

    def _fake_ai(**_kwargs):
        called["count"] += 1
        return {"ok": True, "reply": "concentracao operacional"}

    monkeypatch.setattr("app.cleide_controlled_chat.generate_cleide_ai_reply", _fake_ai)
    body, status = run_cleide_controlled_chat(
        question="E a segunda?",
        history=[{"role": "user", "content": "Qual transportadora lidera?"}],
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "top_transportadoras"
    assert body["fallback_used"] is False
    assert called["count"] == 0


def test_followup_contextual_curto_desconhecido_permanece_fallback(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(
        question="E planeta marte?",
        history=[{"role": "user", "content": "Qual UF origem mais aparece?"}],
        session_obj={},
    )
    assert status == 200
    assert body["intent"] == "fallback_seguro"
    assert body["fallback_code"] == "fallback_intent_desconhecida"


def test_followup_transportadora_destino_sem_quebra_intent(monkeypatch):
    _patch_context(monkeypatch, _chat_context())
    _ai_flag_env(monkeypatch, enabled=False)
    body, status = run_cleide_controlled_chat(
        question="E destino?",
        history=[{"role": "user", "content": "Qual transportadora lidera?"}],
        session_obj={},
    )
    assert status == 200
    assert body["intent"] in {"uf_destino", "fallback_seguro"}
    if body["intent"] == "fallback_seguro":
        assert body["fallback_code"] in {"fallback_intent_desconhecida", "fallback_fora_de_escopo"}
