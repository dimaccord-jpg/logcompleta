"""Testes direcionados do Lote 3C SCRUM-148: julia_chat, freight_query, cleide_audit_chat, agente_compara_chat, onboarding_discovery."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.funnel_event_service import (
    FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_COMPLETED,
    FUNNEL_EVENT_TASK_FAILED,
    FUNNEL_EVENT_TASK_PREPARATION_COMPLETED,
    FUNNEL_EVENT_TASK_PREPARATION_STARTED,
    FUNNEL_EVENT_TASK_STARTED,
    FUNNEL_SOURCE_AGENTE_COMPARA_CHAT,
    FUNNEL_SOURCE_CLEIDE_AUDIT_CHAT,
    FUNNEL_SOURCE_FREIGHT_QUERY,
    FUNNEL_SOURCE_JULIA_CHAT,
    FUNNEL_SOURCE_ONBOARDING_DISCOVERY,
    META_PIXEL_ALLOWED_EVENTS,
    TASK_TYPE_AGENTE_COMPARA_CHAT,
    TASK_TYPE_CLEIDE_AUDIT_CHAT,
    TASK_TYPE_FREIGHT_QUERY,
    TASK_TYPE_JULIA_CHAT,
    TASK_TYPE_ONBOARDING_DISCOVERY,
    is_meta_pixel_allowed,
)
from app.models import FunnelEvent
from app.services.cleiton_ai_data_governance import CleitonAiGovernanceBlockedError
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _enable_session(app):
    app.config["SECRET_KEY"] = "test-secret"
    app.config["TESTING"] = True


def _auth_user(monkeypatch, *, email: str):
    conta, franquia = seed_conta_franquia_cliente(slug=f"lote3c-{email.split('@')[0]}")
    user = seed_usuario(franquia.id, conta.id, email=email)
    fake = SimpleNamespace(
        is_authenticated=True,
        id=user.id,
        conta_id=conta.id,
        franquia_id=franquia.id,
    )
    monkeypatch.setattr("flask_login.utils._get_user", lambda: fake)
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )
    return user


def _events_for(user_id: int | None, event_name: str):
    q = FunnelEvent.query.filter_by(event_name=event_name)
    if user_id is None:
        q = q.filter(FunnelEvent.user_id.is_(None))
    else:
        q = q.filter_by(user_id=user_id)
    return q.order_by(FunnelEvent.id.asc()).all()


def test_lote3c_task_events_not_meta_authorized():
    for name in (
        FUNNEL_EVENT_TASK_PREPARATION_STARTED,
        FUNNEL_EVENT_TASK_PREPARATION_COMPLETED,
        FUNNEL_EVENT_TASK_STARTED,
        FUNNEL_EVENT_TASK_COMPLETED,
        FUNNEL_EVENT_TASK_FAILED,
    ):
        assert name not in META_PIXEL_ALLOWED_EVENTS
        assert is_meta_pixel_allowed(name) is False


# --- Júlia operacional ---


def test_julia_chat_started_completed(app, ctx, monkeypatch):
    import app.run_julia_chat as jc

    monkeypatch.setattr(jc, "_get_client", lambda: object())
    monkeypatch.setattr(jc, "_get_chat_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(jc, "should_search_web_for_question", lambda _q: False)
    monkeypatch.setattr(jc, "govern_history_messages", lambda history, **_k: history)
    monkeypatch.setattr(
        jc,
        "govern_or_raise",
        lambda content, **_k: SimpleNamespace(safe_content=content, decision="allow"),
    )
    monkeypatch.setattr(
        jc,
        "cleiton_governed_generate_content",
        lambda *a, **k: SimpleNamespace(text="Resposta operacional do AgenteFrete."),
    )

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-julia-ok@test.com")
        with app.test_request_context():
            out = jc.chat_julia_reply(
                "Como reduzir custo de frete?",
                [],
                max_history=5,
                execution_id="exec-julia-ok",
            )
        assert "AgenteFrete" in out["reply"] or "custo" in out["reply"].lower() or out["reply"]

        started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        completed = _events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)
        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].source == FUNNEL_SOURCE_JULIA_CHAT
        assert started[0].metadata_json["task_type"] == TASK_TYPE_JULIA_CHAT
        assert started[0].execution_id == "exec-julia-ok"
        assert completed[0].execution_id == "exec-julia-ok"
        assert is_meta_pixel_allowed(started[0].event_name) is False


def test_julia_chat_final_failure_emits_task_failed(app, ctx, monkeypatch):
    import app.run_julia_chat as jc

    monkeypatch.setattr(jc, "_get_client", lambda: None)

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-julia-fail@test.com")
        with app.test_request_context():
            out = jc.chat_julia_reply("Pergunta qualquer", [], execution_id="exec-julia-fail")
        assert "indisponível" in out["reply"].lower() or "indisponivel" in out["reply"].lower()

        failed = _events_for(user.id, FUNNEL_EVENT_TASK_FAILED)
        assert len(failed) == 1
        assert failed[0].metadata_json["task_type"] == TASK_TYPE_JULIA_CHAT
        assert failed[0].metadata_json["error_code"] == "julia_chat_provider_unavailable"
        assert len(_events_for(user.id, FUNNEL_EVENT_TASK_STARTED)) == 0
        assert len(_events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)) == 0


def test_julia_chat_growth_fail_open(app, ctx, monkeypatch):
    import app.run_julia_chat as jc

    monkeypatch.setattr(jc, "_get_client", lambda: object())
    monkeypatch.setattr(jc, "_get_chat_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(jc, "should_search_web_for_question", lambda _q: False)
    monkeypatch.setattr(jc, "govern_history_messages", lambda history, **_k: history)
    monkeypatch.setattr(
        jc,
        "govern_or_raise",
        lambda content, **_k: SimpleNamespace(safe_content=content, decision="allow"),
    )
    monkeypatch.setattr(
        jc,
        "cleiton_governed_generate_content",
        lambda *a, **k: SimpleNamespace(text="Resposta ok mesmo com telemetria indisponivel."),
    )
    monkeypatch.setattr(
        "app.funnel_event_service.record_funnel_event",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("analytics down")),
    )

    with app.app_context():
        _auth_user(monkeypatch, email="lote3c-julia-failopen@test.com")
        with app.test_request_context():
            out = jc.chat_julia_reply("Pergunta", [], execution_id="exec-julia-failopen")
        assert out["reply"]
        assert "telemetria" in out["reply"] or "Resposta ok" in out["reply"]


# --- Freight query ---


def test_freight_query_started_completed(app, ctx, monkeypatch):
    import app.brain as brain
    import sys

    frete = SimpleNamespace(
        valor_frete_total=100.0,
        peso_real=10.0,
        modal="rodoviario",
        data_emissao="2026-01-01",
    )
    models = SimpleNamespace(
        query=SimpleNamespace(
            filter=lambda *a, **k: SimpleNamespace(all=lambda: [frete])
        ),
        id_cidade_origem=1,
        id_cidade_destino=2,
    )
    monkeypatch.setattr("app.infra.get_id_localidade_por_chave", lambda _k: 1)
    fake_cleiton = SimpleNamespace(
        coordenar_analise_frete=lambda *_a, **_k: {
            "acuracia_percentual": "80%",
            "tendencia_macro": "Estabilidade",
            "previsao_numerica": 10.0,
            "intervalo_confianca": None,
            "metrica_erro": {},
            "explicacao_llm": "Previsão estável.",
            "insights_adicionais": None,
            "previsao_texto": "ok",
            "recado_do_roberto": "ok",
        }
    )
    monkeypatch.setitem(sys.modules, "app.run_cleiton", fake_cleiton)
    monkeypatch.setattr(
        "app.extensions.db.get_engine",
        lambda: MagicMock(
            connect=lambda: MagicMock(
                __enter__=lambda s: SimpleNamespace(
                    execute=lambda *_a, **_k: SimpleNamespace(
                        fetchone=lambda: ("Sao Paulo", "SP")
                    )
                ),
                __exit__=lambda *a: False,
            )
        ),
    )

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-freight-ok@test.com")
        with app.test_request_context():
            result, err = brain.processar_inteligencia_frete(
                "Sao Paulo", "Rio de Janeiro", "SP", "RJ", models
            )
        assert err is None
        assert result is not None
        assert result.get("media_bruta") == 10.0

        started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        completed = _events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)
        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].source == FUNNEL_SOURCE_FREIGHT_QUERY
        assert started[0].metadata_json["task_type"] == TASK_TYPE_FREIGHT_QUERY
        assert started[0].execution_id == completed[0].execution_id
        meta = started[0].metadata_json or {}
        assert "origem" not in meta
        assert "destino" not in meta
        assert "rota" not in meta


def test_freight_query_validation_before_start_not_failed(app, ctx, monkeypatch):
    import app.brain as brain

    monkeypatch.setattr("app.infra.get_id_localidade_por_chave", lambda _k: None)

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-freight-val@test.com")
        with app.test_request_context():
            result, err = brain.processar_inteligencia_frete(
                "CidadeX", "CidadeY", "XX", "YY", SimpleNamespace()
            )
        assert result is None
        assert "Localidade" in (err or "")
        assert len(_events_for(user.id, FUNNEL_EVENT_TASK_STARTED)) == 0
        assert len(_events_for(user.id, FUNNEL_EVENT_TASK_FAILED)) == 0
        assert len(_events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)) == 0


def test_freight_query_post_start_failure(app, ctx, monkeypatch):
    import app.brain as brain

    models = SimpleNamespace(
        query=SimpleNamespace(filter=lambda *a, **k: SimpleNamespace(all=lambda: [])),
        id_cidade_origem=1,
        id_cidade_destino=2,
    )
    monkeypatch.setattr("app.infra.get_id_localidade_por_chave", lambda _k: 1)
    monkeypatch.setattr(
        "app.extensions.db.get_engine",
        lambda: MagicMock(
            connect=lambda: MagicMock(
                __enter__=lambda s: SimpleNamespace(
                    execute=lambda *_a, **_k: SimpleNamespace(fetchone=lambda: None)
                ),
                __exit__=lambda *a: False,
            )
        ),
    )

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-freight-fail@test.com")
        with app.test_request_context():
            result, err = brain.processar_inteligencia_frete(
                "Sao Paulo", "Rio", "SP", "RJ", models
            )
        assert result is None
        assert "históricos" in (err or "").lower() or "historicos" in (err or "").lower()

        started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        failed = _events_for(user.id, FUNNEL_EVENT_TASK_FAILED)
        assert len(started) == 1
        assert len(failed) == 1
        assert failed[0].metadata_json["error_code"] == "freight_query_no_history"
        assert len(_events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)) == 0


def test_freight_query_growth_fail_open(app, ctx, monkeypatch):
    import app.brain as brain
    import sys

    frete = SimpleNamespace(
        valor_frete_total=50.0,
        peso_real=5.0,
        modal="rodoviario",
        data_emissao="2026-01-01",
    )
    models = SimpleNamespace(
        query=SimpleNamespace(filter=lambda *a, **k: SimpleNamespace(all=lambda: [frete])),
        id_cidade_origem=1,
        id_cidade_destino=2,
    )
    monkeypatch.setattr("app.infra.get_id_localidade_por_chave", lambda _k: 1)
    fake_cleiton = SimpleNamespace(
        coordenar_analise_frete=lambda *_a, **_k: {
            "acuracia_percentual": "70%",
            "tendencia_macro": "Alta",
            "explicacao_llm": "ok",
            "previsao_texto": "ok",
            "recado_do_roberto": "ok",
        }
    )
    monkeypatch.setitem(sys.modules, "app.run_cleiton", fake_cleiton)
    monkeypatch.setattr(
        "app.extensions.db.get_engine",
        lambda: MagicMock(
            connect=lambda: MagicMock(
                __enter__=lambda s: SimpleNamespace(
                    execute=lambda *_a, **_k: SimpleNamespace(fetchone=lambda: ("A", "SP"))
                ),
                __exit__=lambda *a: False,
            )
        ),
    )
    monkeypatch.setattr(
        "app.funnel_event_service.record_funnel_event",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("analytics down")),
    )

    with app.app_context():
        _auth_user(monkeypatch, email="lote3c-freight-failopen@test.com")
        with app.test_request_context():
            result, err = brain.processar_inteligencia_frete("A", "B", "SP", "RJ", models)
        assert err is None
        assert result is not None


# --- Chat Cleide Auditoria ---


def test_cleide_audit_chat_completed(app, ctx, monkeypatch):
    import app.run_cleide_audit_chat as chat

    monkeypatch.setattr(
        chat,
        "get_cleide_audit_config",
        lambda: SimpleNamespace(
            question_max_chars=4000,
            fallback_message="falha",
            no_hallucination_instruction_enabled=True,
            chat_max_history=10,
        ),
    )
    monkeypatch.setattr(chat, "_get_client", lambda: object())
    monkeypatch.setattr(chat, "_get_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(
        chat,
        "cleiton_governed_generate_content",
        lambda *a, **k: SimpleNamespace(text="Resposta documental da auditoria."),
    )

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-cleide-chat-ok@test.com")
        with app.test_request_context():
            out = chat.chat_cleide_audit_reply(
                "Resumo do documento",
                [],
                execution_id="exec-cleide-audit-chat-ok",
            )
        assert out.get("answer")
        assert not out.get("error")

        started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        completed = _events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)
        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].source == FUNNEL_SOURCE_CLEIDE_AUDIT_CHAT
        assert started[0].metadata_json["task_type"] == TASK_TYPE_CLEIDE_AUDIT_CHAT
        assert started[0].execution_id == "exec-cleide-audit-chat-ok"


def test_cleide_audit_chat_final_failure(app, ctx, monkeypatch):
    import app.run_cleide_audit_chat as chat

    monkeypatch.setattr(
        chat,
        "get_cleide_audit_config",
        lambda: SimpleNamespace(
            question_max_chars=4000,
            fallback_message="falha processamento",
            no_hallucination_instruction_enabled=True,
            chat_max_history=10,
        ),
    )
    monkeypatch.setattr(chat, "_get_client", lambda: object())
    monkeypatch.setattr(chat, "_get_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(
        chat,
        "cleiton_governed_generate_content",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("provider boom")),
    )

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-cleide-chat-fail@test.com")
        with app.test_request_context():
            out = chat.chat_cleide_audit_reply(
                "Pergunta",
                [],
                execution_id="exec-cleide-audit-chat-fail",
            )
        assert out.get("error") == "processing_failed"

        started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        failed = _events_for(user.id, FUNNEL_EVENT_TASK_FAILED)
        assert len(started) == 1
        assert len(failed) == 1
        assert failed[0].metadata_json["error_code"] == "cleide_audit_chat_processing_failed"
        assert len(_events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)) == 0


def test_cleide_audit_chat_growth_fail_open(app, ctx, monkeypatch):
    import app.run_cleide_audit_chat as chat

    monkeypatch.setattr(
        chat,
        "get_cleide_audit_config",
        lambda: SimpleNamespace(
            question_max_chars=4000,
            fallback_message="falha",
            no_hallucination_instruction_enabled=True,
            chat_max_history=10,
        ),
    )
    monkeypatch.setattr(chat, "_get_client", lambda: object())
    monkeypatch.setattr(chat, "_get_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(
        chat,
        "cleiton_governed_generate_content",
        lambda *a, **k: SimpleNamespace(text="Resposta ok."),
    )
    monkeypatch.setattr(
        "app.funnel_event_service.record_funnel_event",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("analytics down")),
    )

    with app.app_context():
        _auth_user(monkeypatch, email="lote3c-cleide-chat-failopen@test.com")
        with app.test_request_context():
            out = chat.chat_cleide_audit_reply("Pergunta", [], execution_id="exec-failopen")
        assert out.get("answer") == "Resposta ok."


# --- Chat Agente Compara ---


def test_agente_compara_chat_completed(app, ctx, monkeypatch):
    import app.run_agente_compara_chat as chat

    monkeypatch.setattr(
        chat,
        "get_agente_compara_config",
        lambda: SimpleNamespace(
            question_max_chars=4000,
            fallback_message="falha",
            no_hallucination_instruction_enabled=True,
            chat_max_history=10,
        ),
    )
    monkeypatch.setattr(chat, "_get_client", lambda: object())
    monkeypatch.setattr(chat, "_get_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(
        chat,
        "cleiton_governed_generate_content",
        lambda *a, **k: SimpleNamespace(text="Comparação das tabelas pronta."),
    )

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-compara-chat-ok@test.com")
        with app.test_request_context():
            out = chat.chat_agente_compara_reply(
                "Compare as tabelas",
                [],
                execution_id="exec-compara-chat-ok",
            )
        assert out.get("answer")
        assert not out.get("error")

        started = _events_for(user.id, FUNNEL_EVENT_TASK_STARTED)
        completed = _events_for(user.id, FUNNEL_EVENT_TASK_COMPLETED)
        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].source == FUNNEL_SOURCE_AGENTE_COMPARA_CHAT
        assert started[0].metadata_json["task_type"] == TASK_TYPE_AGENTE_COMPARA_CHAT


def test_agente_compara_chat_final_failure(app, ctx, monkeypatch):
    import app.run_agente_compara_chat as chat

    monkeypatch.setattr(
        chat,
        "get_agente_compara_config",
        lambda: SimpleNamespace(
            question_max_chars=4000,
            fallback_message="falha",
            no_hallucination_instruction_enabled=True,
            chat_max_history=10,
        ),
    )
    monkeypatch.setattr(chat, "_get_client", lambda: object())
    monkeypatch.setattr(chat, "_get_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(
        chat,
        "cleiton_governed_generate_content",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with app.app_context():
        user = _auth_user(monkeypatch, email="lote3c-compara-chat-fail@test.com")
        with app.test_request_context():
            out = chat.chat_agente_compara_reply(
                "Compare",
                [],
                execution_id="exec-compara-chat-fail",
            )
        assert out.get("error") == "processing_failed"
        assert len(_events_for(user.id, FUNNEL_EVENT_TASK_STARTED)) == 1
        failed = _events_for(user.id, FUNNEL_EVENT_TASK_FAILED)
        assert len(failed) == 1
        assert failed[0].metadata_json["error_code"] == "agente_compara_chat_processing_failed"


def test_agente_compara_chat_growth_fail_open(app, ctx, monkeypatch):
    import app.run_agente_compara_chat as chat

    monkeypatch.setattr(
        chat,
        "get_agente_compara_config",
        lambda: SimpleNamespace(
            question_max_chars=4000,
            fallback_message="falha",
            no_hallucination_instruction_enabled=True,
            chat_max_history=10,
        ),
    )
    monkeypatch.setattr(chat, "_get_client", lambda: object())
    monkeypatch.setattr(chat, "_get_model_candidates", lambda: ["gemini-2.5-flash"])
    monkeypatch.setattr(
        chat,
        "cleiton_governed_generate_content",
        lambda *a, **k: SimpleNamespace(text="Ok."),
    )
    monkeypatch.setattr(
        "app.funnel_event_service.record_funnel_event",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("analytics down")),
    )

    with app.app_context():
        _auth_user(monkeypatch, email="lote3c-compara-chat-failopen@test.com")
        with app.test_request_context():
            out = chat.chat_agente_compara_reply("Pergunta", [], execution_id="exec-failopen")
        assert out.get("answer") == "Ok."


# --- Onboarding Discovery ---


def test_onboarding_discovery_started_completed_anonymous(app, ctx, monkeypatch):
    import app.run_cleiton_discovery as disc

    monkeypatch.setattr(disc, "_get_client", lambda: None)
    monkeypatch.setattr(disc, "_gemini_api_key", lambda: "")
    monkeypatch.setattr(disc, "govern_history_messages", lambda history, **_k: history)
    monkeypatch.setattr(
        disc,
        "govern_or_raise",
        lambda content, **_k: SimpleNamespace(safe_content=content, decision="allow"),
    )
    monkeypatch.setattr(disc, "register_internal_ia_event", lambda **_k: None)
    monkeypatch.setattr(
        disc,
        "_local_fallback_response",
        lambda msg, reason=None: {
            "reply": "Posso ajudar a descobrir a melhor habilidade.",
            "recommended_agent": None,
            "handoff": {"url": "/auditoria-frete", "label": "Auditar"},
            "handoffs": [],
            "refinement_options": [],
            "destination_candidates": [],
            "discovery": {
                "confidence": "medium",
                "next_action": "handoff",
                "recommended_agent": "cleide",
                "reason": reason or "fallback",
                "capability_candidates": [],
                "needs_login": False,
                "pipeline": {},
            },
        },
    )
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )

    with app.app_context():
        # Anônimo: sem current_user autenticado.
        monkeypatch.setattr(
            "flask_login.utils._get_user",
            lambda: SimpleNamespace(is_authenticated=False),
        )
        with app.test_request_context():
            out = disc.cleiton_discovery_reply("Quero auditar frete", [])
        assert out.get("reply")
        assert out.get("handoff") or (out.get("discovery") or {}).get("next_action")

        started = _events_for(None, FUNNEL_EVENT_TASK_STARTED)
        completed = _events_for(None, FUNNEL_EVENT_TASK_COMPLETED)
        # Filtra só onboarding deste lote.
        started = [e for e in started if e.source == FUNNEL_SOURCE_ONBOARDING_DISCOVERY]
        completed = [e for e in completed if e.source == FUNNEL_SOURCE_ONBOARDING_DISCOVERY]
        assert len(started) == 1
        assert len(completed) == 1
        assert started[0].metadata_json["task_type"] == TASK_TYPE_ONBOARDING_DISCOVERY
        assert started[0].user_id is None
        assert FunnelEvent.query.filter_by(
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED
        ).count() == 0


def test_onboarding_discovery_failed(app, ctx, monkeypatch):
    import app.run_cleiton_discovery as disc

    monkeypatch.setattr(disc, "_get_client", lambda: object())
    monkeypatch.setattr(
        disc,
        "govern_history_messages",
        lambda *_a, **_k: (_ for _ in ()).throw(
            CleitonAiGovernanceBlockedError("blocked")
        ),
    )
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )

    with app.app_context():
        monkeypatch.setattr(
            "flask_login.utils._get_user",
            lambda: SimpleNamespace(is_authenticated=False),
        )
        with app.test_request_context():
            out = disc.cleiton_discovery_reply("mensagem suspeita", [])
        assert out.get("reply")

        started = [
            e
            for e in _events_for(None, FUNNEL_EVENT_TASK_STARTED)
            if e.source == FUNNEL_SOURCE_ONBOARDING_DISCOVERY
        ]
        failed = [
            e
            for e in _events_for(None, FUNNEL_EVENT_TASK_FAILED)
            if e.source == FUNNEL_SOURCE_ONBOARDING_DISCOVERY
        ]
        assert len(started) == 1
        assert len(failed) == 1
        assert failed[0].metadata_json["error_code"] == "onboarding_discovery_governance_blocked"
        assert FunnelEvent.query.filter_by(
            event_name=FUNNEL_EVENT_FIRST_RELEVANT_TASK_COMPLETED
        ).count() == 0


def test_onboarding_discovery_growth_fail_open(app, ctx, monkeypatch):
    import app.run_cleiton_discovery as disc

    monkeypatch.setattr(disc, "_get_client", lambda: None)
    monkeypatch.setattr(disc, "_gemini_api_key", lambda: "")
    monkeypatch.setattr(disc, "govern_history_messages", lambda history, **_k: history)
    monkeypatch.setattr(
        disc,
        "govern_or_raise",
        lambda content, **_k: SimpleNamespace(safe_content=content, decision="allow"),
    )
    monkeypatch.setattr(disc, "register_internal_ia_event", lambda **_k: None)
    monkeypatch.setattr(
        disc,
        "_local_fallback_response",
        lambda msg, reason=None: {
            "reply": "Resposta discovery ok.",
            "recommended_agent": None,
            "handoff": None,
            "handoffs": [],
            "refinement_options": [],
            "destination_candidates": [],
            "discovery": {
                "confidence": "low",
                "next_action": "converse",
                "recommended_agent": None,
                "reason": reason or "fallback",
                "capability_candidates": [],
                "needs_login": False,
                "pipeline": {},
            },
        },
    )
    monkeypatch.setattr(
        "app.funnel_event_service.record_funnel_event",
        lambda **_k: (_ for _ in ()).throw(RuntimeError("analytics down")),
    )
    monkeypatch.setattr(
        "app.funnel_event_service._is_desktop_access_admin_test_mode",
        lambda: False,
    )

    with app.app_context():
        monkeypatch.setattr(
            "flask_login.utils._get_user",
            lambda: SimpleNamespace(is_authenticated=False),
        )
        with app.test_request_context():
            out = disc.cleiton_discovery_reply("Olá", [])
        assert out.get("reply") == "Resposta discovery ok."
