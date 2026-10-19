"""Seleção semântica e continuidade do chat do Agente Compara (SCRUM-246)."""
from __future__ import annotations

import inspect
import json
import logging
import time
from types import SimpleNamespace

import pytest

from app.agente_compara_chat_query_catalog import CATALOG_VERSION, PUBLICATION_VERSION
from app.agente_compara_chat_prompt import (
    build_comparison_chat_selection_prompt,
    build_comparison_chat_user_prompt,
)
from app.agente_compara_chat_semantic_selection import (
    MSG_COMPARISON_NOT_AUTHORIZED,
    MSG_REFERENT_CLARIFICATION,
    MSG_SELECTION_MALFORMED,
    REFERENTS_SESSION_KEY,
    SELECTION_VERSION,
    load_confirmed_referents,
    referents_confirmed_by_publication,
    resolve_semantic_selection,
    selection_catalog_document,
    selection_response_json_schema,
)
from app.agente_compara_comparison_state import AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY
from app.run_agente_compara_comparison_chat import (
    chat_agente_compara_comparison_reply,
    responder_entrega_whatsapp_se_pedida,
)
from app.services.cleiton_billable_ai_call import BillableAiAdmissionBlocked
from tests.cleiton_doc_fixtures import patch_cleiton_doc_store
from tests.test_agente_compara_chat_analytical_bridge import (
    _SECRET_CARRIER,
    _SECRET_DOCUMENT,
    _ready_context,
    _usuario,
)

_OTHER_CONTA = 44
_OTHER_USUARIO = 55
_OTHER_FRANQUIA = 66


@pytest.fixture
def storage_env(tmp_path, monkeypatch):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_calculation_result_storage.get_cleiton_doc_tmp_dir",
        lambda: str(tmp_path),
    )
    import app.agente_compara_calculation_result_storage as storage_mod

    monkeypatch.setattr(storage_mod, "_calc_storage_sweep_monotonic", time.monotonic())
    return tmp_path


class _Models:
    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._handler(contents)


def _cfg():
    return SimpleNamespace(question_max_chars=4000, chat_max_history=10)


_UNSET = object()


def _selection(
    queries,
    *,
    action="query",
    clarification=None,
    version=SELECTION_VERSION,
    referent_criterion=_UNSET,
):
    payload = {
        "selection_version": version,
        "action": action,
        "clarification": clarification,
        "catalog_version": CATALOG_VERSION,
        "queries": queries,
    }
    if referent_criterion is not _UNSET:
        payload["referent_criterion"] = referent_criterion
    return json.dumps(payload, ensure_ascii=False)


def _query(query_id, query_type, **extra):
    item = {"query_id": query_id, "type": query_type}
    item.update(extra)
    return item


def _install(monkeypatch, handler):
    models = _Models(handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.get_agente_compara_config",
        _cfg,
    )
    monkeypatch.setattr(
        "app.agente_compara_doc_service.load_temp_table_record",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat._get_client",
        lambda: SimpleNamespace(models=models),
    )
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        lambda client_arg, **kwargs: client_arg.models.generate_content(
            model=kwargs["model"],
            contents=kwargs["contents"],
            config=kwargs.get("config"),
        ),
    )
    import app.run_agente_compara_comparison_chat as chat_mod

    plans = []
    publications = []
    real = chat_mod.execute_comparison_chat_analytical_plan

    def _wrapped(**kwargs):
        plans.append(json.loads(json.dumps(kwargs.get("plan"))))
        result = real(**kwargs)
        publications.append(result.get("publication"))
        return result

    monkeypatch.setattr(chat_mod, "execute_comparison_chat_analytical_plan", _wrapped)
    return models, plans, publications


def _ask(ready, usuario, question, *, history=None, request_id=None, entrega_ja_avaliada=True):
    return chat_agente_compara_comparison_reply(
        question,
        history or [],
        session_obj=ready["session"],
        usuario=usuario,
        comparison_id=ready["comparison_id"],
        request_id=request_id,
        entrega_ja_avaliada=entrega_ja_avaliada,
    )


def _component_states(publication):
    states = set()
    for query in publication.get("queries") or []:
        for record in query.get("records") or []:
            facts = record.get("facts") if isinstance(record, dict) else None
            if not isinstance(facts, dict):
                continue
            for component in facts.get("components") or []:
                if isinstance(component, dict) and component.get("amount_state"):
                    states.add(component["amount_state"])
    return states


def test_simple_savings_question_selects_carrier_comparison(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("economia", "carrier_comparison")]))
        return SimpleNamespace(text="A publicação mostra a economia no universo comparável.")

    models, plans, publications = _install(monkeypatch, handler)
    reply = _ask(ready, usuario, "Qual transportadora teve a maior economia?", request_id="eco-1")
    assert reply.get("error") is None
    assert "publicação" in (reply.get("answer") or "").lower()
    assert len(models.calls) == 2
    assert plans[0]["queries"][0]["type"] == "carrier_comparison"
    assert publications[0]["publication_version"] == "comparison_chat_publication_v1"
    assert "Publicação oficial comparison_chat_publication_v1" in models.calls[1]["contents"]
    assert "Contexto comparativo oficial" not in models.calls[1]["contents"]
    assert _SECRET_DOCUMENT not in models.calls[1]["contents"]


def test_carrier_components_uf_ranking_and_diagnostics(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    questions = {
        "Compare as transportadoras.": [_query("cmp", "carrier_comparison")],
        "Quais componentes de frete estão disponíveis?": [_query("comp", "freight_components")],
        "Qual a economia em São Paulo?": [_query("uf", "by_uf", filters={"ufs": ["SP"]})],
        "Mostre os três fretes com maior diferença.": [
            _query("rank", "top_differences", page={"offset": 0, "limit": 3})
        ],
        "Quais fretes ficaram incompletos e por que não foram calculados?": [
            _query("inc", "incomplete_freights"),
            _query("diag", "non_calculation_diagnostics"),
        ],
    }

    def handler(contents):
        if "Seleção de consultas" in contents:
            question = contents.rsplit("Usuario:", 1)[-1].strip()
            return SimpleNamespace(text=_selection(questions[question]))
        return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")

    models, plans, publications = _install(monkeypatch, handler)
    for question in questions:
        reply = _ask(ready, usuario, question, request_id=f"req-{abs(hash(question))}")
        assert reply.get("error") is None
        assert reply.get("answer")
    assert [plan["queries"][0]["type"] for plan in plans] == [
        "carrier_comparison",
        "freight_components",
        "by_uf",
        "top_differences",
        "incomplete_freights",
    ]
    assert plans[3]["queries"][0]["page"] == {"offset": 0, "limit": 3}
    assert plans[4]["queries"][1]["type"] == "non_calculation_diagnostics"
    assert {item["query_type"] for item in publications[2]["queries"]} == {"by_uf"}
    assert len(models.calls) == 10


def test_sequential_referents_and_ambiguous_follow_up(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" not in contents:
            return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")
        question = contents.rsplit("Usuario:", 1)[-1].strip()
        if question.startswith("Explique os custos da primeira"):
            return SimpleNamespace(
                text=_selection([_query("custos", "freight_components", filters={"table_ids": ["t1"]})])
            )
        if question == "E em São Paulo?":
            return SimpleNamespace(text=_selection([_query("uf", "by_uf", filters={"ufs": ["SP"]})]))
        if question == "Explique os custos dessa transportadora.":
            carrier = json.loads(contents.split("Referentes confirmados desta comparação:\n", 1)[1].split("\n\n", 1)[0])
            table_id = (carrier.get("carrier") or {}).get("table_id")
            if not table_id:
                return SimpleNamespace(
                    text=_selection([], action="clarify", clarification="De qual transportadora você fala?")
                )
            return SimpleNamespace(
                text=_selection(
                    [_query("dessa", "freight_components", filters={"table_ids": [table_id]})]
                )
            )
        if question == "Mostre os três fretes com maior diferença.":
            return SimpleNamespace(
                text=_selection([_query("rank", "top_differences", page={"offset": 0, "limit": 3})])
            )
        if question == "Por que o primeiro não foi calculado?":
            referents = json.loads(
                contents.split("Referentes confirmados desta comparação:\n", 1)[1].split("\n\n", 1)[0]
            )
            ranked = referents.get("ranked_rows") or []
            if not ranked:
                return SimpleNamespace(
                    text=_selection([], action="clarify", clarification="Qual frete da lista você quer explicar?")
                )
            return SimpleNamespace(text=_selection([_query("motivo", "non_calculation_diagnostics")]))
        return SimpleNamespace(
            text=_selection([], action="clarify", clarification="Preciso que você indique o recorte.")
        )

    models, plans, _publications = _install(monkeypatch, handler)
    first = _ask(ready, usuario, "Explique os custos da primeira transportadora.", request_id="seq-1")
    assert first.get("error") is None
    stored = load_confirmed_referents(ready["session"], ready["comparison_id"])
    assert stored["carrier"]["table_id"] == "t1"
    second = _ask(ready, usuario, "E em São Paulo?", request_id="seq-2")
    assert second.get("error") is None
    assert plans[1]["queries"][0]["filters"]["ufs"] == ["SP"]
    assert load_confirmed_referents(ready["session"], ready["comparison_id"])["uf"] == "SP"
    third = _ask(ready, usuario, "Explique os custos dessa transportadora.", request_id="seq-3")
    assert third.get("error") is None
    assert plans[2]["queries"][0]["filters"]["table_ids"] == ["t1"]
    fourth = _ask(ready, usuario, "Mostre os três fretes com maior diferença.", request_id="seq-4")
    assert fourth.get("error") is None
    ranked = load_confirmed_referents(ready["session"], ready["comparison_id"])["ranked_rows"]
    assert ranked and ranked[0]["rank"] == 1
    fifth = _ask(ready, usuario, "Por que o primeiro não foi calculado?", request_id="seq-5")
    assert fifth.get("error") is None
    assert plans[4]["queries"][0]["type"] == "non_calculation_diagnostics"
    selection_prompt = models.calls[-2]["contents"]
    answer_prompt = models.calls[-1]["contents"]
    assert f'"row_index": {ranked[0]["row_index"]}' in selection_prompt
    assert "Referentes confirmados, apenas para resolver menções" in answer_prompt
    assert f'"row_index": {ranked[0]["row_index"]}' in answer_prompt
    assert "comparison_chat_publication_v1" in answer_prompt
    assert len(models.calls) == 10


def test_ambiguous_referent_asks_without_executing(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        assert "Seleção de consultas" in contents
        return SimpleNamespace(
            text=_selection([], action="clarify", clarification="De qual transportadora você fala?")
        )

    models, plans, _publications = _install(monkeypatch, handler)
    reply = _ask(ready, usuario, "Explique os custos dessa transportadora.", request_id="amb-1")
    assert reply.get("error") is None
    assert reply["answer"] == "De qual transportadora você fala?"
    assert reply["deterministic"] is True
    assert plans == []
    assert len(models.calls) == 1


def test_comparison_switch_drops_previous_referents(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    first = _ready_context(monkeypatch)
    second = _ready_context(monkeypatch)
    usuario = _usuario()
    seen = []

    def handler(contents):
        if "Seleção de consultas" not in contents:
            return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")
        seen.append(contents)
        if len(seen) == 1:
            return SimpleNamespace(
                text=_selection([_query("custos", "freight_components", filters={"table_ids": ["t1"]})])
            )
        return SimpleNamespace(
            text=_selection([], action="clarify", clarification="De qual transportadora você fala?")
        )

    _models, plans, _publications = _install(monkeypatch, handler)
    import app.agente_compara_calculation_execution_service as execution

    def _fingerprint(state, session_obj=None, ttl_hours=None):
        calc = (state or {}).get("comparison_calculation") or {}
        return calc.get("request_fingerprint")

    monkeypatch.setattr(execution, "_compute_current_fingerprint_for_state", _fingerprint)
    _ask(first, usuario, "Explique os custos da primeira transportadora.", request_id="troca-1")
    assert load_confirmed_referents(first["session"], first["comparison_id"])["carrier"]["table_id"] == "t1"
    first["session"][AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = second["session"][
        AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY
    ]
    reply = chat_agente_compara_comparison_reply(
        "Explique os custos dessa transportadora.",
        [],
        session_obj=first["session"],
        usuario=usuario,
        comparison_id=second["comparison_id"],
        request_id="troca-2",
        entrega_ja_avaliada=True,
    )
    assert reply["answer"] == "De qual transportadora você fala?"
    assert '"carrier": null' in seen[1]
    referent_block = seen[1].split("Referentes confirmados desta comparação:\n", 1)[1].split("\n\n", 1)[0]
    assert first["comparison_id"] not in referent_block
    assert load_confirmed_referents(first["session"], second["comparison_id"])["carrier"] is None
    stored = first["session"].get(REFERENTS_SESSION_KEY)
    assert stored is None or stored.get("comparison_id") != first["comparison_id"]
    assert len(plans) == 1


def test_invalid_plan_and_malformed_selection_do_not_retry(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    responses = [
        _selection([_query(f"q{index}", "official_summary") for index in range(5)]),
        "isto não é json",
        _selection([_query("q1", "official_summary")], version="outra-versao"),
        json.dumps(
            {
                "selection_version": SELECTION_VERSION,
                "action": "query",
                "catalog_version": CATALOG_VERSION,
                "queries": [{"query_id": "q1", "type": "official_summary", "sql": "select 1"}],
            }
        ),
    ]

    def handler(contents):
        assert "Seleção de consultas" in contents
        return SimpleNamespace(text=responses.pop(0))

    models, plans, _publications = _install(monkeypatch, handler)
    for request_id in ("inv-1", "inv-2", "inv-3", "inv-4"):
        reply = _ask(ready, usuario, "Qual transportadora teve a maior economia?", request_id=request_id)
        assert reply["deterministic"] is True
        assert reply["answer"] == MSG_SELECTION_MALFORMED
        assert "too_many" not in reply["answer"]
        assert "sql" not in reply["answer"].lower()
    assert plans == []
    assert len(models.calls) == 4


def test_unavailable_table_reference_asks_clarification(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        return SimpleNamespace(
            text=_selection([_query("outra", "freight_components", filters={"table_ids": ["t-alheia"]})])
        )

    models, plans, _publications = _install(monkeypatch, handler)
    reply = _ask(ready, usuario, "Explique os custos dessa transportadora.", request_id="ref-1")
    assert reply["answer"] == MSG_REFERENT_CLARIFICATION
    assert plans == []
    assert len(models.calls) == 1


def test_empty_slice_zero_versus_absence_and_four_query_limit(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    scripts = [
        _selection([_query("vazia", "by_uf", filters={"ufs": ["AC"]})]),
        _selection([_query("partes", "freight_components", filters={"table_ids": ["t1"]})]),
        _selection([_query(f"q{index}", "official_summary") for index in range(4)]),
    ]

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=scripts.pop(0))
        return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")

    models, plans, publications = _install(monkeypatch, handler)
    empty = _ask(ready, usuario, "Qual a economia no Acre?", request_id="vazio-1")
    assert empty.get("error") is None
    assert "não encontrou dados nesse recorte" in models.calls[1]["contents"]
    components = _ask(ready, usuario, "Quais componentes de frete estão disponíveis?", request_id="zero-1")
    assert components.get("error") is None
    assert "Não converta ausência em zero" in models.calls[3]["contents"]
    assert "amount_state zero permite dizer que o valor oficial é zero" in models.calls[3]["contents"]
    states = _component_states(publications[1])
    assert "zero" in states
    assert "absent" in states
    four = _ask(ready, usuario, "Resumo, comparação, UF e peso.", request_id="limite-1")
    assert four.get("error") is None
    assert len(plans[2]["queries"]) == 4
    assert len(models.calls) == 6


def test_partial_publication_still_reaches_the_answer_generation(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("resumo", "official_summary")]))
        return SimpleNamespace(text="Resposta com a limitação declarada.")

    models, _plans, _publications = _install(monkeypatch, handler)
    import app.run_agente_compara_comparison_chat as chat_mod

    def _partial(**kwargs):
        return {
            "status": "ok",
            "error_code": None,
            "publication": {
                "publication_version": "comparison_chat_publication_v1",
                "status": "partial",
                "evidence_sufficient": False,
                "allowed_claims": [],
                "queries": [],
                "losses": [{"code": "page_incomplete", "count": 1}],
            },
        }

    monkeypatch.setattr(chat_mod, "execute_comparison_chat_analytical_plan", _partial)
    reply = _ask(ready, usuario, "Qual transportadora teve a maior economia?", request_id="parcial-1")
    assert reply["answer"] == "Resposta com a limitação declarada."
    assert '"evidence_sufficient": false' in models.calls[1]["contents"]
    assert '"status": "partial"' in models.calls[1]["contents"]
    assert len(models.calls) == 2


def test_user_isolation_and_authorized_plan_only(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    owner = _usuario()
    intruder = _usuario(conta_id=_OTHER_CONTA, usuario_id=_OTHER_USUARIO, franquia_id=_OTHER_FRANQUIA)

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("resumo", "official_summary")]))
        return SimpleNamespace(text="Resposta do dono.")

    models, plans, _publications = _install(monkeypatch, handler)
    denied = _ask(ready, intruder, "Qual transportadora teve a maior economia?", request_id="iso-1")
    assert denied["answer"] == MSG_COMPARISON_NOT_AUTHORIZED
    assert _SECRET_DOCUMENT not in denied["answer"]
    assert _SECRET_CARRIER not in denied["answer"]
    assert models.calls == []
    assert plans == []
    allowed = _ask(ready, owner, "Qual transportadora teve a maior economia?", request_id="iso-2")
    assert allowed["answer"] == "Resposta do dono."
    assert plans[0]["catalog_version"] == CATALOG_VERSION
    assert set(plans[0]) == {"catalog_version", "queries"}
    assert "sql" not in json.dumps(plans[0])


def test_financial_blocks_and_selection_failure_do_not_spawn_a_third_call(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    mode = {"phase": "selection_block"}

    def handler(contents):
        phase = mode["phase"]
        if phase == "selection_block":
            raise BillableAiAdmissionBlocked(
                "bloqueio",
                motivo="franquia",
                mensagem_usuario="Franquia esgotada.",
            )
        if phase == "answer_block" and "Seleção de consultas" not in contents:
            raise BillableAiAdmissionBlocked(
                "bloqueio",
                motivo="franquia",
                mensagem_usuario="Franquia esgotada na resposta.",
            )
        if phase == "answer_fail" and "Seleção de consultas" not in contents:
            raise RuntimeError("provider-down")
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("resumo", "official_summary")]))
        return SimpleNamespace(text="não deveria chegar")

    models, plans, _publications = _install(monkeypatch, handler)
    blocked = _ask(ready, usuario, "Qual transportadora teve a maior economia?", request_id="fin-1")
    assert blocked["http_status"] == 403
    assert blocked["error"] == "provider_request_failed"
    assert plans == []
    assert len(models.calls) == 1

    mode["phase"] = "answer_block"
    answer_blocked = _ask(ready, usuario, "Qual transportadora teve a maior economia?", request_id="fin-2")
    assert answer_blocked["http_status"] == 403
    assert len(plans) == 1
    assert len(models.calls) == 3

    mode["phase"] = "answer_fail"
    failed = _ask(ready, usuario, "Qual transportadora teve a maior economia?", request_id="fin-3")
    assert failed["error"] == "provider_request_failed"
    assert failed.get("answer") == ""
    assert len(plans) == 2
    assert len(models.calls) == 5


def test_valid_cache_does_not_generate_again(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("resumo", "official_summary")]))
        return SimpleNamespace(text="Resposta em cache.")

    models, plans, _publications = _install(monkeypatch, handler)
    first = _ask(ready, usuario, "Qual transportadora teve a maior economia?", request_id="cache-246")
    second = _ask(ready, usuario, "Qual transportadora teve a maior economia?", request_id="cache-246")
    assert first["answer"] == "Resposta em cache."
    assert second.get("cached") is True
    assert second["answer"] == first["answer"]
    assert len(models.calls) == 2
    assert len(plans) == 1


def test_whatsapp_gate_stays_ahead_of_selection(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    order = []

    def handler(contents):
        order.append("gemini")
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("resumo", "official_summary")]))
        return SimpleNamespace(text="Resposta depois do WhatsApp.")

    models, plans, _publications = _install(monkeypatch, handler)

    def _entrega(*args, **kwargs):
        order.append("whatsapp")
        return {
            "answer": "Envio registrado.",
            "flow_type": "agente_compara_comparison_chat",
            "deterministic": True,
            "chat_available": True,
        }

    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.responder_entrega_whatsapp_se_pedida",
        _entrega,
    )
    sent = _ask(
        ready,
        usuario,
        "me envia essa análise pelo whatsapp",
        request_id="wa-1",
        entrega_ja_avaliada=False,
    )
    assert sent["answer"] == "Envio registrado."
    assert order == ["whatsapp"]
    assert plans == []

    def _continua(*args, **kwargs):
        order.append("whatsapp")
        return None

    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.responder_entrega_whatsapp_se_pedida",
        _continua,
    )
    continued = _ask(
        ready,
        usuario,
        "Qual transportadora teve a maior economia?",
        request_id="wa-2",
        entrega_ja_avaliada=False,
    )
    assert continued["answer"] == "Resposta depois do WhatsApp."
    assert order[0] == "whatsapp"
    assert order[1:] == ["whatsapp", "gemini", "gemini"]
    assert len(models.calls) == 2
    source = inspect.getsource(responder_entrega_whatsapp_se_pedida)
    assert "execute_comparison_chat_analytical_plan" not in source
    assert "semantic_selection" not in source


def test_selection_module_does_not_touch_frozen_batch():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    selection = (root / "app" / "agente_compara_chat_semantic_selection.py").read_text(encoding="utf-8")
    runner = (root / "app" / "run_agente_compara_comparison_chat.py").read_text(encoding="utf-8")
    for forbidden in (
        "agente_compara_chat_sqlalchemy_store",
        "agente_compara_chat_financial_read",
        "agente_compara_chat_operation_store",
        "calculate_comparison_in_memory",
    ):
        assert forbidden not in selection
        assert forbidden not in runner


_TABLES = [
    {"table_id": "t1", "slot_number": 1, "carrier_name": "Alpha"},
    {"table_id": "t2", "slot_number": 2, "carrier_name": "Beta"},
    {"table_id": "t3", "slot_number": 3, "carrier_name": "Gama"},
]
_SLOT_TABLE = {1: "t1", 2: "t2", 3: "t3"}


def _carrier_fact(slot, freight, wins):
    return {
        "carrier_slot": slot,
        "comparable_freight_text": freight,
        "comparable_freight_state": "zero" if freight == "0.00" else "present",
        "wins": wins,
    }


def _comparison_publication(
    carriers,
    *,
    claim=True,
    tie_rule="unique_lowest_amount",
    universe="fully_comparable_rows",
    comparable=2,
    decisive=2,
    ties=0,
    superiority=False,
    extra_facts=None,
):
    facts = {
        "universe": universe,
        "indicator_source": "official_analytics",
        "tie_rule": tie_rule,
        "global_superiority": superiority,
        "comparable_row_count": comparable,
        "decisive_row_count": decisive,
        "tie_count": ties,
        "carriers": carriers,
    }
    if extra_facts:
        facts.update(extra_facts)
    return {
        "publication_version": PUBLICATION_VERSION,
        "status": "ok",
        "queries": [
            {
                "query_type": "carrier_comparison",
                "status": "ok",
                "allowed_claims": ["bounded_best_freight"] if claim else ["population_count"],
                "selection": {},
                "records": [{"record_role": "summary", "facts": facts}],
            }
        ],
    }


def _ranking_publication(row_indexes, *, offset, returned, total, detail_offset=None):
    page = {
        "offset": offset,
        "limit": 50,
        "total": total,
        "returned": returned,
        "sort": "absolute_gap_desc_row_index_asc",
    }
    records = [{"record_role": "summary", "facts": {"ranked_rows": total}, "page": dict(page)}]
    for row_index in row_indexes:
        item_page = dict(page)
        if detail_offset is not None:
            item_page["offset"] = detail_offset
        records.append(
            {
                "record_role": "detail",
                "subject": {"kind": "row", "row_index": row_index},
                "facts": {"row_index": row_index},
                "page": item_page,
            }
        )
    return {
        "publication_version": PUBLICATION_VERSION,
        "status": "ok",
        "queries": [
            {
                "query_type": "top_differences",
                "status": "ok",
                "page": page,
                "selection": {},
                "records": records,
            }
        ],
    }


def _confirmed(publication, previous=None, *, referent_criterion=None):
    return referents_confirmed_by_publication(
        publication,
        _TABLES,
        previous,
        referent_criterion=referent_criterion,
    )


def _referent_block(contents):
    return contents.split("Referentes confirmados desta comparação:\n", 1)[1].split("\n\n", 1)[0]


def test_unique_carrier_uses_published_comparable_freight_not_list_order():
    publication = _comparison_publication(
        [_carrier_fact(1, "30.00", 0), _carrier_fact(2, "10.00", 3), _carrier_fact(3, "18.00", 0)]
    )
    confirmed = _confirmed(publication, referent_criterion="lowest_comparable_freight")
    assert confirmed["carrier"]["table_id"] == "t2"
    assert confirmed["carrier"]["slot_number"] == 2
    assert confirmed["carrier"]["carrier_name"] == "Beta"
    facts = publication["queries"][0]["records"][0]["facts"]
    assert facts["carriers"][0]["carrier_slot"] == 1
    assert "lead_carrier_slot" not in facts


def test_tie_missing_claim_and_lead_slot_do_not_invent_a_carrier():
    criterion = "lowest_comparable_freight"
    tied = _comparison_publication(
        [_carrier_fact(1, "10.00", 1), _carrier_fact(2, "10.00", 1)],
        extra_facts={"lead_carrier_slot": 1},
    )
    assert _confirmed(tied, referent_criterion=criterion)["carrier"] is None
    narrated = _comparison_publication(
        [_carrier_fact(1, "30.00", 0), _carrier_fact(2, "10.00", 2)],
        claim=False,
        extra_facts={"lead_carrier_slot": 1},
    )
    assert _confirmed(
        narrated,
        {"carrier": {"table_id": "t1", "slot_number": 1}, "uf": None, "ranked_rows": []},
        referent_criterion=criterion,
    )["carrier"] is None
    assert _confirmed(
        _comparison_publication([_carrier_fact(1, "10.00", 1)]),
        referent_criterion=criterion,
    )["carrier"] is None
    assert _confirmed(
        _comparison_publication([_carrier_fact(1, "30.00", 4), _carrier_fact(2, "10.00", 1)]),
        referent_criterion=criterion,
    )["carrier"] is None
    without_rule = _comparison_publication(
        [_carrier_fact(1, "30.00", 0), _carrier_fact(2, "10.00", 2)],
        tie_rule=None,
    )
    assert _confirmed(without_rule, referent_criterion=criterion)["carrier"] is None


def test_uf_follow_up_keeps_the_carrier_identified_by_the_comparison():
    identified = _confirmed(
        _comparison_publication([_carrier_fact(1, "30.00", 0), _carrier_fact(2, "10.00", 2)]),
        referent_criterion="lowest_comparable_freight",
    )
    followed = referents_confirmed_by_publication(
        {
            "publication_version": PUBLICATION_VERSION,
            "status": "ok",
            "queries": [
                {
                    "query_type": "by_uf",
                    "status": "ok",
                    "selection": {"ufs": ["SP"]},
                    "records": [],
                }
            ],
        },
        _TABLES,
        identified,
    )
    assert followed["uf"] == "SP"
    assert followed["carrier"]["table_id"] == "t2"
    assert followed["carrier"]["slot_number"] == 2


def test_empty_ranking_page_drops_positional_referent_and_keeps_carrier():
    first = _confirmed(_ranking_publication([4, 9], offset=0, returned=2, total=3))
    assert first["ranked_rows"] == [{"rank": 1, "row_index": 4}, {"rank": 2, "row_index": 9}]
    identified = {
        "carrier": {"table_id": "t2", "slot_number": 2, "carrier_name": "Beta"},
        "uf": "SP",
        "ranked_rows": first["ranked_rows"],
    }
    empty = _confirmed(_ranking_publication([], offset=50, returned=0, total=3), identified)
    assert empty["ranked_rows"] == []
    assert empty["carrier"]["table_id"] == "t2"
    assert empty["uf"] == "SP"
    stray = _confirmed(
        _ranking_publication([4], offset=50, returned=0, total=3),
        identified,
    )
    assert stray["ranked_rows"] == []
    replaced = _confirmed(_ranking_publication([20], offset=50, returned=1, total=3), identified)
    assert replaced["ranked_rows"] == [{"rank": 1, "row_index": 20}]
    foreign = _confirmed(
        _ranking_publication([4], offset=50, returned=1, total=3, detail_offset=0),
        identified,
    )
    assert foreign["ranked_rows"] == []


def test_selection_envelope_accepts_query_and_clarify_only_when_complete():
    valid = resolve_semantic_selection(
        _selection([_query("q1", "official_summary")]),
        authorized_tables=_TABLES,
    )
    assert valid.action == "query"
    assert valid.referent_criterion is None
    assert valid.plan == {
        "catalog_version": CATALOG_VERSION,
        "queries": [_query("q1", "official_summary")],
    }
    lowest = resolve_semantic_selection(
        _selection(
            [_query("q1", "carrier_comparison")],
            referent_criterion="lowest_comparable_freight",
        ),
        authorized_tables=_TABLES,
    )
    assert lowest.action == "query"
    assert lowest.referent_criterion == "lowest_comparable_freight"
    assert "referent_criterion" not in lowest.plan
    absent_null = resolve_semantic_selection(
        _selection([_query("q1", "official_summary")], referent_criterion=None),
        authorized_tables=_TABLES,
    )
    assert absent_null.action == "query"
    assert absent_null.referent_criterion is None
    assert "referent_criterion" not in absent_null.plan
    clarified = resolve_semantic_selection(
        _selection([], action="clarify", clarification="De qual transportadora você fala?"),
        authorized_tables=_TABLES,
    )
    assert clarified.action == "clarify"
    assert clarified.plan is None
    assert clarified.clarification == "De qual transportadora você fala?"

    missing_version = json.loads(_selection([_query("q1", "official_summary")]))
    del missing_version["selection_version"]
    missing_action = json.loads(_selection([_query("q1", "official_summary")]))
    del missing_action["action"]
    incompatible = _selection([_query("q1", "official_summary")], version="outra-versao")
    bare = json.dumps(
        {"catalog_version": CATALOG_VERSION, "queries": [_query("q1", "official_summary")]},
        ensure_ascii=False,
    )
    unknown = json.loads(_selection([_query("q1", "official_summary")]))
    unknown["sql"] = "select 1"
    wrong_catalog = json.loads(_selection([_query("q1", "official_summary")]))
    wrong_catalog["catalog_version"] = "outro"
    incoherent = json.loads(
        _selection([_query("q1", "official_summary")], action="clarify", clarification="Qual recorte?")
    )
    invalid_criterion = json.loads(_selection([_query("q1", "official_summary")]))
    invalid_criterion["referent_criterion"] = "minimum_freight"
    for payload in (
        json.dumps(missing_version),
        json.dumps(missing_action),
        incompatible,
        bare,
        json.dumps(unknown),
        json.dumps(wrong_catalog),
        json.dumps(incoherent),
        json.dumps(invalid_criterion),
    ):
        refused = resolve_semantic_selection(payload, authorized_tables=_TABLES)
        assert refused.action == "malformed"
        assert refused.plan is None
        assert refused.clarification == MSG_SELECTION_MALFORMED


def test_invalid_plan_inside_envelope_is_refused_before_execution(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    responses = [
        json.dumps(
            {
                "catalog_version": CATALOG_VERSION,
                "queries": [_query("economia", "carrier_comparison")],
            }
        ),
        json.dumps(
            {
                "selection_version": SELECTION_VERSION,
                "action": "query",
                "catalog_version": CATALOG_VERSION,
                "queries": [{"query_id": "q1", "type": "official_summary", "sql": "select 1"}],
            }
        ),
    ]

    def handler(contents):
        assert "Seleção de consultas" in contents
        return SimpleNamespace(text=responses.pop(0))

    models, plans, _publications = _install(monkeypatch, handler)
    bare = _ask(ready, usuario, "Qual transportadora apresentou maior economia?", request_id="bare-246")
    assert bare["deterministic"] is True
    assert bare["answer"] == MSG_SELECTION_MALFORMED
    assert plans == []
    assert len(models.calls) == 1
    invalid = _ask(ready, usuario, "Qual transportadora apresentou maior economia?", request_id="plan-246")
    assert invalid["deterministic"] is True
    assert invalid["answer"] == MSG_SELECTION_MALFORMED
    assert plans == []
    assert len(models.calls) == 2


def test_carrier_comparison_survives_uf_follow_up(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" not in contents:
            return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")
        question = contents.rsplit("Usuario:", 1)[-1].strip()
        if question == "Qual transportadora tem o menor frete comparável?":
            return SimpleNamespace(
                text=_selection(
                    [_query("frete", "carrier_comparison")],
                    referent_criterion="lowest_comparable_freight",
                )
            )
        if question == "E em São Paulo?":
            return SimpleNamespace(text=_selection([_query("uf", "by_uf", filters={"ufs": ["SP"]})]))
        if question == "Explique os custos dessa transportadora.":
            carrier = json.loads(_referent_block(contents))
            table_id = (carrier.get("carrier") or {}).get("table_id")
            if not table_id:
                return SimpleNamespace(
                    text=_selection([], action="clarify", clarification="De qual transportadora você fala?")
                )
            return SimpleNamespace(
                text=_selection([_query("dessa", "freight_components", filters={"table_ids": [table_id]})])
            )
        return SimpleNamespace(text=_selection([], action="clarify", clarification="Preciso que você indique o recorte."))

    models, plans, publications = _install(monkeypatch, handler)
    first = _ask(ready, usuario, "Qual transportadora tem o menor frete comparável?", request_id="car-1")
    assert first.get("error") is None
    comparison = next(item for item in publications[0]["queries"] if item["query_type"] == "carrier_comparison")
    facts = next(record["facts"] for record in comparison["records"] if record["record_role"] == "summary")
    assert "carriers" in facts
    assert "lead_carrier_slot" not in facts
    assert "bounded_best_freight" in comparison["allowed_claims"]
    priced = []
    for item in facts["carriers"]:
        whole, frac = item["comparable_freight_text"].split(".", 1)
        priced.append((int(whole) * 100 + int(frac), item["carrier_slot"]))
    priced.sort()
    assert priced[0][0] != priced[1][0]
    winner_slot = priced[0][1]
    stored = load_confirmed_referents(ready["session"], ready["comparison_id"])
    assert stored["carrier"]["slot_number"] == winner_slot
    assert stored["carrier"]["table_id"] == _SLOT_TABLE[winner_slot]
    assert "referent_criterion" not in plans[0]
    assert "referent_criterion" not in json.dumps(plans[0])
    session_referents = ready["session"][REFERENTS_SESSION_KEY]
    assert "referent_criterion" not in session_referents
    assert set(session_referents) == {"comparison_id", "carrier", "uf", "ranked_rows"}
    second = _ask(ready, usuario, "E em São Paulo?", request_id="car-2")
    assert second.get("error") is None
    after_uf = load_confirmed_referents(ready["session"], ready["comparison_id"])
    assert after_uf["uf"] == "SP"
    assert after_uf["carrier"]["table_id"] == stored["carrier"]["table_id"]
    third = _ask(ready, usuario, "Explique os custos dessa transportadora.", request_id="car-3")
    assert third.get("error") is None
    assert plans[2]["queries"][0]["filters"]["table_ids"] == [stored["carrier"]["table_id"]]
    assert len(plans) == 3
    assert len(models.calls) == 6


def test_empty_next_page_does_not_keep_the_previous_first_row(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" not in contents:
            return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")
        question = contents.rsplit("Usuario:", 1)[-1].strip()
        if question == "Explique os custos da primeira transportadora.":
            return SimpleNamespace(
                text=_selection([_query("custos", "freight_components", filters={"table_ids": ["t1"]})])
            )
        if question == "Mostre os fretes com maior diferença.":
            return SimpleNamespace(
                text=_selection([_query("rank", "top_differences", page={"offset": 0, "limit": 50})])
            )
        if question == "Mostre a página seguinte.":
            return SimpleNamespace(
                text=_selection([_query("next", "top_differences", page={"offset": 50, "limit": 50})])
            )
        if question == "Por que o primeiro não foi calculado?":
            referents = json.loads(_referent_block(contents))
            if not referents.get("ranked_rows"):
                return SimpleNamespace(
                    text=_selection([], action="clarify", clarification="Qual frete da lista você quer explicar?")
                )
            return SimpleNamespace(text=_selection([_query("motivo", "non_calculation_diagnostics")]))
        return SimpleNamespace(text=_selection([], action="clarify", clarification="Preciso que você indique o recorte."))

    models, plans, publications = _install(monkeypatch, handler)
    named = _ask(ready, usuario, "Explique os custos da primeira transportadora.", request_id="rank-1")
    assert named.get("error") is None
    assert load_confirmed_referents(ready["session"], ready["comparison_id"])["carrier"]["table_id"] == "t1"
    first = _ask(ready, usuario, "Mostre os fretes com maior diferença.", request_id="rank-2")
    assert first.get("error") is None
    ranking = next(item for item in publications[1]["queries"] if item["query_type"] == "top_differences")
    assert ranking["page"]["offset"] == 0
    assert ranking["page"]["returned"] >= 1
    stored = load_confirmed_referents(ready["session"], ready["comparison_id"])
    old_row = stored["ranked_rows"][0]["row_index"]
    assert stored["ranked_rows"][0]["rank"] == 1
    assert stored["carrier"]["table_id"] == "t1"
    detail = next(record for record in ranking["records"] if record["record_role"] == "detail")
    assert detail["subject"]["row_index"] == old_row
    second = _ask(ready, usuario, "Mostre a página seguinte.", request_id="rank-3")
    assert second.get("error") is None
    nxt = next(item for item in publications[2]["queries"] if item["query_type"] == "top_differences")
    assert nxt["page"]["offset"] == 50
    assert nxt["page"]["returned"] == 0
    assert not any(record.get("record_role") == "detail" for record in nxt["records"])
    cleared = load_confirmed_referents(ready["session"], ready["comparison_id"])
    assert cleared["ranked_rows"] == []
    assert cleared["carrier"]["table_id"] == "t1"
    third = _ask(ready, usuario, "Por que o primeiro não foi calculado?", request_id="rank-4")
    assert third["deterministic"] is True
    assert third["answer"] == "Qual frete da lista você quer explicar?"
    assert len(plans) == 3
    block = _referent_block(models.calls[-1]["contents"])
    assert f'"row_index": {old_row}' not in block
    assert '"ranked_rows": []' in block
    assert len(models.calls) == 7


def _alpha_beta_gama_publication():
    return _comparison_publication(
        [
            _carrier_fact(1, "30.00", 0),
            _carrier_fact(2, "10.00", 3),
            _carrier_fact(3, "18.00", 0),
        ]
    )


def _previous_alpha():
    return {
        "carrier": {"table_id": "t1", "slot_number": 1, "carrier_name": "Alpha"},
        "uf": "SP",
        "ranked_rows": [{"rank": 1, "row_index": 4}],
    }


def test_alpha_beta_gama_separates_lowest_freight_from_highest_savings():
    publication = _alpha_beta_gama_publication()
    facts = publication["queries"][0]["records"][0]["facts"]
    assert [item["carrier_slot"] for item in facts["carriers"]] == [1, 2, 3]

    lowest = _confirmed(publication, referent_criterion="lowest_comparable_freight")
    assert lowest["carrier"] == {
        "table_id": "t2",
        "slot_number": 2,
        "carrier_name": "Beta",
    }

    savings = _confirmed(publication, _previous_alpha(), referent_criterion="highest_savings")
    assert savings["carrier"] is None
    assert savings["uf"] == "SP"
    assert savings["ranked_rows"] == [{"rank": 1, "row_index": 4}]

    absent = _confirmed(publication, _previous_alpha())
    assert absent["carrier"] is None
    assert absent["uf"] == "SP"

    null_criterion = _confirmed(publication, _previous_alpha(), referent_criterion=None)
    assert null_criterion["carrier"] is None

    conflict = _comparison_publication(
        [
            _carrier_fact(1, "30.00", 9),
            _carrier_fact(2, "10.00", 1),
            _carrier_fact(3, "18.00", 0),
        ]
    )
    assert _confirmed(conflict, referent_criterion="highest_savings")["carrier"] is None
    assert _confirmed(conflict, referent_criterion="lowest_comparable_freight")["carrier"] is None


def test_explicit_freight_component_carrier_survives_savings_criterion():
    publication = {
        "publication_version": PUBLICATION_VERSION,
        "status": "ok",
        "queries": [
            {
                "query_type": "freight_components",
                "status": "ok",
                "selection": {"carrier_slots": [3]},
                "records": [],
            }
        ],
    }
    confirmed = _confirmed(
        publication,
        {"carrier": {"table_id": "t2", "slot_number": 2, "carrier_name": "Beta"}, "uf": None, "ranked_rows": []},
        referent_criterion="highest_savings",
    )
    assert confirmed["carrier"]["carrier_name"] == "Gama"
    assert confirmed["carrier"]["table_id"] == "t3"
    assert confirmed["carrier"]["slot_number"] == 3


def test_selection_catalog_and_prompt_allow_only_the_two_criteria():
    document = selection_catalog_document()
    assert document["selection_version"] == SELECTION_VERSION
    assert document["referent_criteria"] == ["highest_savings", "lowest_comparable_freight"]
    prompt = build_comparison_chat_selection_prompt(
        user_message="Qual transportadora teve a maior economia?",
        history_slice=[],
        referents={"carrier": None, "uf": None, "ranked_rows": []},
        authorized_tables=_TABLES,
        catalog=document,
    )
    assert "referent_criterion" in prompt
    assert "lowest_comparable_freight" in prompt
    assert "highest_savings" in prompt
    assert "menor frete comparável" in prompt
    assert "maior economia" in prompt
    answer_source = inspect.getsource(build_comparison_chat_user_prompt)
    assert "referent_criterion" not in answer_source


def test_invalid_referent_criterion_is_rejected_before_validation(monkeypatch):
    import app.agente_compara_chat_semantic_selection as selection_mod

    seen = []
    real = selection_mod.validate_query_plan

    def _capture(plan, **kwargs):
        seen.append(plan)
        return real(plan, **kwargs)

    monkeypatch.setattr(selection_mod, "validate_query_plan", _capture)
    for value in ("cheapest", "minimum_freight", "", 1, ["lowest_comparable_freight"], True):
        payload = json.loads(_selection([_query("q1", "carrier_comparison")]))
        payload["referent_criterion"] = value
        refused = resolve_semantic_selection(json.dumps(payload), authorized_tables=_TABLES)
        assert refused.action == "malformed"
        assert refused.plan is None
        assert refused.referent_criterion is None
        assert refused.clarification == MSG_SELECTION_MALFORMED
    assert seen == []


def test_invalid_referent_criterion_never_reaches_the_bridge(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    payload = json.loads(_selection([_query("economia", "carrier_comparison")]))
    payload["referent_criterion"] = "maior_economia"

    def handler(contents):
        assert "Seleção de consultas" in contents
        return SimpleNamespace(text=json.dumps(payload))

    models, plans, _publications = _install(monkeypatch, handler)
    reply = _ask(ready, usuario, "Qual transportadora apresentou maior economia?", request_id="crit-246")
    assert reply["deterministic"] is True
    assert reply["answer"] == MSG_SELECTION_MALFORMED
    assert plans == []
    assert len(models.calls) == 1


def test_highest_savings_then_uf_asks_which_carrier(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" not in contents:
            return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")
        question = contents.rsplit("Usuario:", 1)[-1].strip()
        if question == "Qual transportadora apresentou maior economia?":
            return SimpleNamespace(
                text=_selection(
                    [_query("economia", "carrier_comparison")],
                    referent_criterion="highest_savings",
                )
            )
        if question == "E em São Paulo?":
            return SimpleNamespace(text=_selection([_query("uf", "by_uf", filters={"ufs": ["SP"]})]))
        if question == "Explique os custos dessa transportadora.":
            carrier = json.loads(_referent_block(contents))
            if (carrier.get("carrier") or {}).get("table_id"):
                return SimpleNamespace(text=_selection([_query("dessa", "freight_components")]))
            return SimpleNamespace(
                text=_selection([], action="clarify", clarification="De qual transportadora você fala?")
            )
        return SimpleNamespace(text=_selection([], action="clarify", clarification="Preciso que você indique o recorte."))

    models, plans, publications = _install(monkeypatch, handler)
    first = _ask(ready, usuario, "Qual transportadora apresentou maior economia?", request_id="sav-1")
    assert first.get("error") is None
    comparison = next(item for item in publications[0]["queries"] if item["query_type"] == "carrier_comparison")
    facts = next(record["facts"] for record in comparison["records"] if record["record_role"] == "summary")
    priced = []
    for item in facts["carriers"]:
        whole, frac = item["comparable_freight_text"].split(".", 1)
        priced.append((int(whole) * 100 + int(frac), item["carrier_slot"]))
    priced.sort()
    assert priced[0][0] != priced[1][0]
    assert "referent_criterion" not in plans[0]
    stored = load_confirmed_referents(ready["session"], ready["comparison_id"])
    assert stored["carrier"] is None
    assert "referent_criterion" not in ready["session"][REFERENTS_SESSION_KEY]
    second = _ask(ready, usuario, "E em São Paulo?", request_id="sav-2")
    assert second.get("error") is None
    after_uf = load_confirmed_referents(ready["session"], ready["comparison_id"])
    assert after_uf["uf"] == "SP"
    assert after_uf["carrier"] is None
    third = _ask(ready, usuario, "Explique os custos dessa transportadora.", request_id="sav-3")
    assert third["deterministic"] is True
    assert third["answer"] == "De qual transportadora você fala?"
    assert len(plans) == 2
    assert len(models.calls) == 5


def test_absent_criterion_clears_the_previous_carrier(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" not in contents:
            return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")
        question = contents.rsplit("Usuario:", 1)[-1].strip()
        if question == "Explique os custos da transportadora selecionada.":
            return SimpleNamespace(
                text=_selection([_query("custos", "freight_components", filters={"table_ids": ["t1"]})])
            )
        if question == "Compare as transportadoras.":
            return SimpleNamespace(text=_selection([_query("cmp", "carrier_comparison")]))
        return SimpleNamespace(text=_selection([], action="clarify", clarification="Preciso que você indique o recorte."))

    models, plans, publications = _install(monkeypatch, handler)
    named = _ask(ready, usuario, "Explique os custos da transportadora selecionada.", request_id="abs-1")
    assert named.get("error") is None
    assert load_confirmed_referents(ready["session"], ready["comparison_id"])["carrier"]["table_id"] == "t1"
    compared = _ask(ready, usuario, "Compare as transportadoras.", request_id="abs-2")
    assert compared.get("error") is None
    comparison = next(item for item in publications[1]["queries"] if item["query_type"] == "carrier_comparison")
    facts = next(record["facts"] for record in comparison["records"] if record["record_role"] == "summary")
    priced = []
    for item in facts["carriers"]:
        whole, frac = item["comparable_freight_text"].split(".", 1)
        priced.append((int(whole) * 100 + int(frac), item["carrier_slot"]))
    priced.sort()
    assert priced[0][0] != priced[1][0]
    cleared = load_confirmed_referents(ready["session"], ready["comparison_id"])
    assert cleared["carrier"] is None
    assert "referent_criterion" not in plans[1]
    assert "referent_criterion" not in ready["session"][REFERENTS_SESSION_KEY]
    assert len(plans) == 2
    assert len(models.calls) == 4


_SNAKE_SCHEMA_KEYS = {"additional_properties", "property_ordering", "min_items", "max_items"}
_SECRET_SELECTION = "VALOR_SIGILOSO_238"


def _schema_nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _schema_nodes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _schema_nodes(item)


def _assert_provider_schema(schema):
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["selection_version", "action", "catalog_version", "queries"]
    assert set(schema["properties"]) == {
        "selection_version",
        "action",
        "clarification",
        "catalog_version",
        "queries",
        "referent_criterion",
    }
    assert schema["properties"]["selection_version"]["enum"] == [SELECTION_VERSION]
    assert schema["properties"]["action"]["enum"] == ["clarify", "query"]
    assert schema["properties"]["catalog_version"]["enum"] == [CATALOG_VERSION]
    queries = schema["properties"]["queries"]
    assert queries["type"] == "array"
    assert queries["minItems"] == 0
    assert queries["maxItems"] == 4
    item = queries["items"]
    assert "anyOf" not in item
    assert item["type"] == "object"
    assert item["additionalProperties"] is False
    assert item["required"] == ["query_id", "type"]
    assert set(item["properties"]) == {"query_id", "type", "filters", "fields", "page", "sort"}
    assert item["properties"]["query_id"]["type"] == "string"
    assert item["properties"]["type"]["type"] == "string"
    assert "enum" not in item["properties"]["type"]
    filters = item["properties"]["filters"]
    assert filters["type"] == "object"
    assert filters["additionalProperties"] is False
    assert "filters" not in item["required"]
    from app.agente_compara_chat_query_catalog import FILTER_FIELDS, PAGE_FIELDS

    assert set(filters["properties"]) == set().union(*FILTER_FIELDS.values())
    assert item["properties"]["fields"]["type"] == "array"
    assert item["properties"]["fields"]["items"] == {"type": "string"}
    page = item["properties"]["page"]
    assert page["type"] == "object"
    assert page["additionalProperties"] is False
    assert set(page["properties"]) == set(PAGE_FIELDS)
    assert item["properties"]["sort"]["type"] == "string"
    criterion = schema["properties"]["referent_criterion"]["anyOf"][0]
    assert criterion["enum"] == ["highest_savings", "lowest_comparable_freight"]
    any_of_nodes = [node for node in _schema_nodes(schema) if "anyOf" in node]
    assert len(any_of_nodes) == 3
    assert all(len(node["anyOf"]) == 2 for node in any_of_nodes)
    enum_values = [value for node in _schema_nodes(schema) if "enum" in node for value in node["enum"]]
    assert enum_values == [
        SELECTION_VERSION,
        "clarify",
        "query",
        CATALOG_VERSION,
        "highest_savings",
        "lowest_comparable_freight",
    ]
    encoded = json.dumps(schema)
    assert len(encoded) < 4500
    assert "publication_version" not in encoded
    assert '"sorts"' not in encoded
    assert '"paginated"' not in encoded
    for node in _schema_nodes(schema):
        assert _SNAKE_SCHEMA_KEYS.isdisjoint(node)


def _rejection_lines(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("comparison_chat_selection_rejected")
    ]


def test_total_savings_question_reaches_the_analytical_bridge(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    plan = [_query("economia_total", "official_summary")]

    def handler(contents):
        if "Seleção de consultas" in contents:
            assert contents.rstrip().endswith("Qual foi a economia total?")
            return SimpleNamespace(
                text=_selection(plan),
                candidates=[SimpleNamespace(finish_reason="STOP")],
            )
        return SimpleNamespace(text="A economia potencial está no resumo oficial.")

    models, plans, publications = _install(monkeypatch, handler)
    reply = _ask(ready, usuario, "Qual foi a economia total?", request_id="scrum238-economia")
    assert reply.get("error") is None
    assert reply["deterministic"] is False
    assert plans == [{"catalog_version": CATALOG_VERSION, "queries": plan}]
    assert publications[0]["queries"][0]["query_type"] == "official_summary"
    assert len(models.calls) == 2
    selection_config = models.calls[0]["config"]
    dumped = selection_config.model_dump(exclude_none=True)
    wire = selection_config.model_dump(exclude_none=True, by_alias=True)
    assert dumped["response_mime_type"] == "application/json"
    assert "response_schema" not in dumped
    assert dumped["response_json_schema"] == selection_response_json_schema()
    assert dumped["thinking_config"]["thinking_budget"] == 0
    assert wire["responseJsonSchema"]["additionalProperties"] is False
    assert "responseSchema" not in wire
    assert wire["thinkingConfig"]["thinkingBudget"] == 0
    assert models.calls[1]["config"] is None
    _assert_provider_schema(dumped["response_json_schema"])


def test_catalog_document_is_not_accepted_as_a_plan(ctx, storage_env, monkeypatch, caplog):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    catalog = selection_catalog_document()

    def handler(contents):
        assert "Seleção de consultas" in contents
        return SimpleNamespace(text=json.dumps(catalog))

    models, plans, _publications = _install(monkeypatch, handler)
    import app.agente_compara_chat_semantic_selection as selection_mod

    with caplog.at_level(logging.INFO, logger=selection_mod.logger.name):
        reply = _ask(ready, usuario, "Qual foi a economia total?", request_id="scrum238-catalogo")
    assert reply["deterministic"] is True
    assert reply["answer"] == MSG_SELECTION_MALFORMED
    assert plans == []
    assert len(models.calls) == 1
    lines = _rejection_lines(caplog)
    assert lines == ["comparison_chat_selection_rejected code=extra_field"]
    assert "Qual foi a economia total?" not in lines[0]
    assert json.dumps(catalog) not in lines[0]


def test_invalid_json_and_extra_fields_stay_blocked(caplog):
    import app.agente_compara_chat_semantic_selection as selection_mod

    secret_plan = json.loads(_selection([_query("q1", "official_summary")]))
    secret_plan["nota"] = _SECRET_SELECTION
    missing = json.loads(_selection([_query("q1", "official_summary")]))
    del missing["action"]
    wrong_action = json.loads(_selection([_query("q1", "official_summary")]))
    wrong_action["action"] = "execute"
    filters_as_list = json.loads(_selection([_query("q1", "carrier_comparison")]))
    filters_as_list["queries"][0]["filters"] = ["table_ids"]
    missing_query_id = json.loads(_selection([_query("q1", "official_summary")]))
    del missing_query_id["queries"][0]["query_id"]
    unauthorized_type = _selection([_query("q1", "semantic_choice")])
    foreign_filter = json.loads(_selection([_query("q1", "official_summary", filters={"ufs": ["SP"]})]))
    page_over_limit = json.loads(
        _selection([_query("q1", "by_uf", page={"offset": 0, "limit": 51})])
    )
    unauthorized_sort = json.loads(_selection([_query("q1", "official_summary", sort="uf_asc")]))
    cases = [
        ("{", "invalid_json", "MAX_TOKENS"),
        (json.dumps(missing), "missing_field", None),
        (json.dumps(secret_plan), "extra_field", None),
        (_selection([_query("q1", "official_summary")], version="outra-versao"), "wrong_version", None),
        (json.dumps(wrong_action), "wrong_action", None),
        (
            _selection([_query("q1", "official_summary")], action="clarify", clarification="Qual recorte?"),
            "clarify_incoherent",
            None,
        ),
        (json.dumps(missing_query_id), "invalid_query", None),
        (json.dumps(filters_as_list), "invalid_filter", None),
        (unauthorized_type, "invalid_query", None),
        (json.dumps(foreign_filter), "extra_field", None),
        (json.dumps(page_over_limit), "invalid_query", None),
        (json.dumps(unauthorized_sort), "invalid_query", None),
    ]
    with caplog.at_level(logging.INFO, logger=selection_mod.logger.name):
        for payload, code, finish in cases:
            refused = resolve_semantic_selection(
                payload,
                authorized_tables=_TABLES,
                finish_reason=finish if finish else payload,
            )
            assert refused.action == "malformed"
            assert refused.plan is None
            assert refused.clarification == MSG_SELECTION_MALFORMED
            lines = _rejection_lines(caplog)
            expected = f"comparison_chat_selection_rejected code={code}"
            if finish:
                expected += f" finish_reason={finish}"
            assert lines[-1] == expected
            assert _SECRET_SELECTION not in lines[-1]
            assert payload not in lines[-1]
            caplog.clear()


def test_referent_criterion_schema_stays_closed(caplog):
    schema = selection_response_json_schema()
    criterion = schema["properties"]["referent_criterion"]["anyOf"][0]["enum"]
    assert criterion == ["highest_savings", "lowest_comparable_freight"]
    payload = json.loads(_selection([_query("q1", "carrier_comparison")]))
    payload["referent_criterion"] = "minimum_freight"
    import app.agente_compara_chat_semantic_selection as selection_mod

    with caplog.at_level(logging.INFO, logger=selection_mod.logger.name):
        refused = resolve_semantic_selection(
            json.dumps(payload),
            authorized_tables=_TABLES,
            finish_reason="STOP",
        )
    assert refused.action == "malformed"
    assert refused.referent_criterion is None
    assert _rejection_lines(caplog) == [
        "comparison_chat_selection_rejected code=invalid_referent_criterion finish_reason=STOP"
    ]
    assert "minimum_freight" not in _rejection_lines(caplog)[0]


def test_selection_generation_does_not_add_a_third_call_or_change_cleiton():
    import app.run_agente_compara_comparison_chat as chat

    selection = inspect.getsource(chat._reply_from_semantic_selection)
    assert selection.count("_generate_governed_text(") == 1
    assert selection.count("_run_one_gemini_call(") == 1
    governed = inspect.getsource(chat._generate_governed_text)
    assert governed.count("cleiton_governed_generate_content(") == 1
    answer = inspect.getsource(chat._run_one_gemini_call)
    assert "thinking_budget" not in answer
    assert "response_json_schema" not in answer
    assert "thinking_budget=0" in inspect.getsource(chat._selection_response_config)
    runner = inspect.getsource(chat.chat_agente_compara_comparison_reply)
    assert "thinking_budget" not in runner
    assert "response_json_schema" not in runner
    module = inspect.getsource(chat)
    assert ".generate_content(" not in module
    assert "economia total" not in module
    selection_module = inspect.getsource(
        __import__("app.agente_compara_chat_semantic_selection", fromlist=["selection_response_json_schema"])
    )
    assert "economia total" not in selection_module
