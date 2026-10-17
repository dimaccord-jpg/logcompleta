"""Qualidade da resposta final do chat analítico do Agente Compara (SCRUM-247)."""
from __future__ import annotations

import inspect
import json
import time
from types import SimpleNamespace

import pytest

from app.agente_compara_chat_prompt import build_comparison_chat_user_prompt
from app.run_agente_compara_comparison_chat import chat_agente_compara_comparison_reply
from tests.cleiton_doc_fixtures import patch_cleiton_doc_store
from tests.test_agente_compara_chat_analytical_bridge import (
    _SECRET_CARRIER,
    _SECRET_DOCUMENT,
    _ready_context,
    _usuario,
)
from tests.test_agente_compara_chat_semantic_selection import _ask, _install, _query, _selection


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


def _publication(**overrides):
    payload = {
        "publication_version": "comparison_chat_publication_v1",
        "status": "ok",
        "evidence_sufficient": True,
        "publication_complete": True,
        "evidence_omitted_count": 0,
        "losses": [],
        "queries": [],
    }
    payload.update(overrides)
    return payload


def _prompt(question, publication, **extra):
    return build_comparison_chat_user_prompt(
        user_message=question,
        history_slice=[],
        context_payload={},
        scope="overview",
        evidence_publication=publication,
        confirmed_referents=extra.get("confirmed_referents", {"carrier": None, "uf": None, "ranked_rows": []}),
        authorized_tables=extra.get("authorized_tables"),
    )


def _published_from_prompt(contents):
    body = contents.split("Publicação oficial comparison_chat_publication_v1.", 1)[1]
    start = body.index("{")
    ends = [
        body.find(marker, start)
        for marker in (
            "\n\nNomes das transportadoras",
            "\n\nReferentes confirmados",
            "\n\n---\n\n",
        )
        if body.find(marker, start) != -1
    ]
    return json.loads(body[start : min(ends)])


def _summary(publication, query_type):
    query = next(item for item in publication["queries"] if item["query_type"] == query_type)
    facts = next(record["facts"] for record in query["records"] if record["record_role"] == "summary")
    return query, facts


def _component(publication, row_index, slot, code):
    query = next(item for item in publication["queries"] if item["query_type"] == "freight_components")
    for record in query["records"]:
        facts = record.get("facts") or {}
        if record.get("record_role") == "detail" and facts.get("row_index") == row_index and facts.get("carrier_slot") == slot:
            return next(item for item in facts["components"] if item["code"] == code)
    raise AssertionError(f"componente ausente {row_index} {slot} {code}")


def test_direct_question_overrides_the_five_part_report():
    publication = _publication(
        queries=[
            {
                "query_type": "official_summary",
                "evidence_sufficient": True,
                "allowed_claims": ["potential_savings", "population_count"],
                "records": [
                    {
                        "record_role": "summary",
                        "facts": {
                            "potential_savings_text": "8.00",
                            "potential_savings_state": "present",
                            "comparable_row_count": 1,
                            "savings_universe": "fully_comparable_rows",
                        },
                    }
                ],
            }
        ]
    )
    prompt = _prompt("Qual foi a economia total?", publication)
    assert prompt.index("1) conclusao principal") < prompt.index("Não use a estrutura fixa de cinco partes")
    assert "Responda primeiro ao que foi perguntado" in prompt
    assert "Não faça introdução genérica, lista longa nem relatório" in prompt
    assert "somente uma ressalva indispensável" in prompt
    assert '"potential_savings_text": "8.00"' in prompt
    assert '"comparable_row_count": 1' in prompt
    assert "Contexto comparativo oficial" not in prompt


def test_analytical_question_keeps_negotiation_distinct_from_guaranteed_savings():
    prompt = _prompt(
        "Quais diferenças merecem negociação?",
        _publication(
            queries=[
                {
                    "query_type": "carrier_comparison",
                    "allowed_claims": ["potential_savings"],
                    "evidence_sufficient": True,
                    "records": [],
                }
            ]
        ),
    )
    assert "Comece pela conclusão que a evidência sustenta" in prompt
    assert "Oportunidade de negociação não é economia garantida" in prompt
    assert "Não afirme causa definitiva sem prova na publicação" in prompt
    assert "Não recomende ação operacional que dependa de informação ausente" in prompt
    assert "Hipótese não vira causa" in prompt
    assert "fato observado, inferência sustentada e hipótese" in prompt


def test_components_keep_absence_zero_and_gris_distinct():
    publication = _publication(
        queries=[
            {
                "query_type": "freight_components",
                "allowed_claims": ["component_value", "component_provenance", "undifferentiated_bucket"],
                "evidence_sufficient": True,
                "records": [
                    {
                        "record_role": "detail",
                        "facts": {
                            "row_index": 1,
                            "carrier_slot": 1,
                            "components": [
                                {
                                    "code": "gris",
                                    "label": "GRIS",
                                    "amount_text": "0.00",
                                    "amount_state": "zero",
                                    "availability": "preserved",
                                },
                                {
                                    "code": "ad_valorem",
                                    "label": "Ad Valorem",
                                    "amount_text": "2.00",
                                    "amount_state": "present",
                                    "availability": "preserved",
                                },
                                {
                                    "code": "weight_freight",
                                    "label": "Frete peso",
                                    "amount_text": None,
                                    "amount_state": "absent",
                                    "availability": "absent",
                                },
                                {
                                    "code": "icms",
                                    "label": "ICMS",
                                    "amount_text": None,
                                    "amount_state": "not_applicable",
                                    "availability": "not_applicable",
                                },
                            ],
                            "buckets": [
                                {
                                    "code": "gris_ad_valorem_bucket",
                                    "amount_text": "7.00",
                                    "amount_state": "present",
                                    "availability": "aggregated_without_detail",
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    )
    prompt = _prompt("Como se compõe o frete?", publication)
    assert "GRIS e Ad Valorem são componentes diferentes" in prompt
    assert "gris_ad_valorem_bucket não pode ser dividido" in prompt
    assert "Não converta ausência em zero" in prompt
    assert "não force o fechamento da soma" in prompt
    assert "Não explique tributo ou adicional como se valesse para todos os fretes" in prompt
    assert '"code": "gris"' in prompt
    assert '"amount_state": "zero"' in prompt
    assert '"code": "ad_valorem"' in prompt
    assert '"amount_text": "2.00"' in prompt
    assert '"amount_state": "absent"' in prompt
    assert '"amount_state": "not_applicable"' in prompt
    assert '"code": "gris_ad_valorem_bucket"' in prompt
    assert "amount_state zero permite dizer que o valor oficial é zero" in prompt


def test_carrier_difference_does_not_confuse_savings_with_lowest_freight():
    prompt = _prompt(
        "Qual transportadora apresentou o menor frete comparável?",
        _publication(
            queries=[
                {
                    "query_type": "carrier_comparison",
                    "allowed_claims": ["potential_savings", "population_count"],
                    "evidence_sufficient": True,
                    "records": [
                        {
                            "record_role": "summary",
                            "facts": {
                                "potential_savings_text": "30.00",
                                "potential_savings_state": "present",
                                "global_superiority": False,
                                "carriers": [{"carrier_slot": 2, "comparable_freight_text": "10.00"}],
                            },
                        }
                    ],
                }
            ]
        ),
        authorized_tables=[
            {"table_id": "interno-t2", "slot_number": 2, "carrier_name": "  Beta  "},
            {"table_id": "interno-t1", "slot_number": True, "carrier_name": "Invalida"},
            {"slot_number": 1, "carrier_name": "Alpha"},
            {"slot_number": 2, "carrier_name": "Duplicada"},
        ],
    )
    assert "Maior economia não equivale automaticamente a menor frete" in prompt
    assert "Não atribua a economia agregada a uma transportadora" in prompt
    assert "ausência de bounded_best_freight impedem nomear um vencedor" in prompt
    assert "global_superiority falso não autoriza" in prompt
    assert '"potential_savings_text": "30.00"' in prompt
    assert "bounded_best_freight" not in json.dumps(_published_from_prompt(prompt)["queries"][0]["allowed_claims"])
    labels = prompt.split("Nomes das transportadoras", 1)[1].split("Referentes confirmados", 1)[0]
    assert '"carrier_name": "Beta"' in labels
    assert '"carrier_name": "Alpha"' in labels
    assert "interno-t2" not in labels
    assert "Invalida" not in labels
    assert "Duplicada" not in labels
    assert "Não são fonte de valor" in prompt


def test_diagnostics_do_not_extend_beyond_the_official_reason():
    publication = _publication(
        status="partial",
        evidence_sufficient=False,
        publication_complete=False,
        losses=[{"code": "page_incomplete", "count": 1, "blocks": ["page_ranking"]}],
        queries=[
            {
                "query_type": "non_calculation_diagnostics",
                "evidence_sufficient": False,
                "allowed_claims": ["official_diagnostic"],
                "records": [
                    {
                        "record_role": "summary",
                        "facts": {
                            "by_code": [
                                {
                                    "code": "missing_coverage_mapping",
                                    "cell_count": 4,
                                    "preserved_meaning": "destination_without_sufficient_coverage_classification",
                                    "must_not_assert": ["route_not_served"],
                                }
                            ]
                        },
                    }
                ],
            },
            {
                "query_type": "incomplete_freights",
                "evidence_sufficient": True,
                "allowed_claims": ["partial_freight"],
                "page": {"offset": 0, "limit": 1, "total": 9, "returned": 1, "sort": None},
                "records": [
                    {
                        "record_role": "detail",
                        "facts": {"row_index": 2, "cells": [{"completeness": "partial", "partial_amount_state": "partial"}]},
                    }
                ],
            },
        ],
    )
    prompt = _prompt("O que explica os fretes incompletos?", publication)
    assert "não significa rota não atendida" in prompt
    assert "Dado indisponível não é custo zero" in prompt
    assert "Frete parcial não é frete totalmente calculado" in prompt
    assert "Diagnóstico agregado não é diagnóstico de uma linha específica" in prompt
    assert "o que não pode ser afirmado" in prompt
    assert '"evidence_sufficient": false' in prompt
    assert '"status": "partial"' in prompt
    assert "route_not_served" in prompt
    assert '"completeness": "partial"' in prompt
    assert '"returned": 1' in prompt
    assert '"total": 9' in prompt
    assert "a lista não é a base inteira" in prompt
    assert "a consulta oficial não encontrou dados nesse recorte" in prompt


def test_weight_and_geography_claims_stay_inside_the_published_cut():
    prompt = _prompt(
        "Como os custos variam por faixa de peso e em São Paulo?",
        _publication(
            queries=[
                {
                    "query_type": "by_weight",
                    "allowed_claims": ["weight_band_population"],
                    "evidence_sufficient": True,
                    "records": [{"record_role": "summary", "facts": {"numeric_weight_rows": 2, "bands_version": "weight_bands_v1"}}],
                },
                {
                    "query_type": "by_uf",
                    "allowed_claims": ["geography_group"],
                    "evidence_sufficient": True,
                    "selection": {"ufs": ["SP"]},
                    "records": [
                        {
                            "record_role": "summary",
                            "facts": {"selected_ufs": ["SP"], "filtered_uf_rows": 1},
                        }
                    ],
                },
            ]
        ),
    )
    assert "weight_band_population autoriza faixas de peso publicadas" in prompt
    assert "geography_group autoriza o recorte geográfico publicado" in prompt
    assert "Não apresente subconjunto ou página como se fosse o total" in prompt
    published = _published_from_prompt(prompt)
    geography = next(item for item in published["queries"] if item["query_type"] == "by_uf")
    weight = next(item for item in published["queries"] if item["query_type"] == "by_weight")
    assert geography["records"][0]["facts"]["selected_ufs"] == ["SP"]
    assert weight["records"][0]["facts"]["numeric_weight_rows"] == 2


def test_legacy_comparison_prompt_does_not_receive_the_analytical_guidance():
    prompt = build_comparison_chat_user_prompt(
        user_message="Resuma",
        history_slice=[],
        context_payload={"comparison": {"comparison_id": "cmp"}},
        scope="overview",
        authorized_tables=[{"table_id": "segredo", "slot_number": 1, "carrier_name": "NaoEntraNoLegado"}],
    )
    assert "Contexto comparativo oficial" in prompt
    assert "Publicação oficial comparison_chat_publication_v1" not in prompt
    assert "Não use a estrutura fixa de cinco partes" not in prompt
    assert "NaoEntraNoLegado" not in prompt
    assert "referent_criterion" not in inspect.getsource(build_comparison_chat_user_prompt)


def test_runner_keeps_a_single_answer_generation_and_the_whatsapp_gate():
    import app.run_agente_compara_comparison_chat as chat

    selection = inspect.getsource(chat._reply_from_semantic_selection)
    assert selection.count("_run_one_gemini_call(") == 1
    assert selection.count("_generate_governed_text(") == 1
    assert 'billable_stage="answer_generation"' in selection
    assert 'billable_stage="semantic_selection"' in selection
    assert "answer_review" not in selection
    generation = inspect.getsource(chat._generate_governed_text)
    assert generation.count("cleiton_governed_generate_content(") == 1
    reply = inspect.getsource(chat.chat_agente_compara_comparison_reply)
    assert reply.index("responder_entrega_whatsapp_se_pedida") < reply.index("_reply_from_semantic_selection")


def test_simple_savings_answer_stays_direct(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    produced = {}

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("total", "official_summary")]))
        published = _published_from_prompt(contents)
        query, facts = _summary(published, "official_summary")
        assert "Responda primeiro ao que foi perguntado" in contents
        assert "Não use a estrutura fixa de cinco partes" in contents
        assert _SECRET_DOCUMENT not in contents
        assert _SECRET_CARRIER not in json.dumps(published, ensure_ascii=False)
        labels = contents.split("Nomes das transportadoras", 1)[1].split("Referentes confirmados", 1)[0]
        assert f"{_SECRET_CARRIER}A" in labels
        assert "table_id" not in labels
        if "potential_savings" in query["allowed_claims"] and facts.get("potential_savings_state") in {"present", "zero"}:
            text = (
                f"A economia potencial foi de R$ {facts['potential_savings_text']} "
                f"em {facts['comparable_row_count']} fretes comparáveis. "
                "É uma estimativa da comparação, não uma economia já realizada."
            )
        else:
            text = "A consulta oficial não autoriza afirmar a economia potencial desse recorte."
        produced["text"] = text
        produced["claims"] = list(query["allowed_claims"])
        produced["facts"] = facts
        return SimpleNamespace(text=text)

    models, plans, publications = _install(monkeypatch, handler)
    reply = _ask(ready, usuario, "Qual foi a economia total?", request_id="q247-economia")
    assert reply.get("error") is None
    assert reply["deterministic"] is False
    assert reply["answer"] == produced["text"]
    assert "Conclusão" not in reply["answer"]
    assert "Próximos critérios" not in reply["answer"]
    assert plans[0]["queries"][0]["type"] == "official_summary"
    assert len(models.calls) == 2
    assert "Seleção de consultas" not in models.calls[1]["contents"]
    official, official_facts = _summary(publications[0], "official_summary")
    assert produced["claims"] == official["allowed_claims"]
    assert official_facts["potential_savings_text"] in models.calls[1]["contents"]
    if "potential_savings" in official["allowed_claims"]:
        assert official_facts["potential_savings_text"] in reply["answer"]


def test_lowest_freight_is_not_answered_as_aggregate_savings(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(
                text=_selection(
                    [_query("frete", "carrier_comparison")],
                    referent_criterion="lowest_comparable_freight",
                )
            )
        published = _published_from_prompt(contents)
        query, facts = _summary(published, "carrier_comparison")
        assert "Maior economia não equivale automaticamente a menor frete" in contents
        assert "Não atribua a economia agregada a uma transportadora" in contents
        if "bounded_best_freight" in query["allowed_claims"]:
            text = "O menor frete comparável publicado está identificado na comparação oficial."
        else:
            text = (
                f"A economia potencial agregada foi de R$ {facts['potential_savings_text']}, "
                "mas a publicação não autoriza apontar o menor frete comparável nem atribuir essa economia a uma transportadora."
            )
        return SimpleNamespace(text=text)

    models, _plans, publications = _install(monkeypatch, handler)
    reply = _ask(ready, usuario, "Qual transportadora apresentou o menor frete comparável?", request_id="q247-menor")
    query, facts = _summary(publications[0], "carrier_comparison")
    assert reply["answer"]
    assert len(models.calls) == 2
    assert facts["potential_savings_text"] in models.calls[1]["contents"]
    assert "global_superiority" in models.calls[1]["contents"]
    if "bounded_best_freight" not in query["allowed_claims"]:
        assert "não autoriza apontar o menor frete" in reply["answer"]
        assert "TransportadoraSigilosa" not in reply["answer"]
    else:
        assert "menor frete comparável" in reply["answer"].lower()


def test_real_components_preserve_zero_and_absence(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("comp", "freight_components")]))
        published = _published_from_prompt(contents)
        zero = _component(published, 1, 1, "taxes")
        absent = _component(published, 1, 2, "weight_freight")
        missing = _component(published, 1, 3, "taxes")
        assert zero["amount_state"] == "zero"
        assert zero["amount_text"] == "0.00"
        assert absent["amount_text"] is None
        assert absent["amount_state"] != "zero"
        assert missing["availability"] == "absent"
        assert "GRIS e Ad Valorem são componentes diferentes" in contents
        assert "Não converta ausência em zero" in contents
        return SimpleNamespace(
            text=(
                "No frete consultado, Impostos está oficialmente em R$ 0,00. "
                "O frete peso da segunda transportadora não foi publicado, e ausência não significa zero. "
                "GRIS e Ad Valorem não aparecem decompostos nesse recorte."
            )
        )

    models, _plans, publications = _install(monkeypatch, handler)
    reply = _ask(ready, usuario, "Quais componentes de frete estão disponíveis?", request_id="q247-comp")
    assert reply["answer"].startswith("No frete consultado")
    assert "R$ 0,00" in reply["answer"]
    assert "ausência não significa zero" in reply["answer"]
    assert len(models.calls) == 2
    assert _component(publications[0], 1, 1, "taxes")["amount_state"] == "zero"
    assert '"amount_state": "zero"' in models.calls[1]["contents"]
    assert '"availability": "absent"' in models.calls[1]["contents"]


def test_incomplete_freight_and_official_diagnostic_stay_bounded(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" in contents:
            return SimpleNamespace(
                text=_selection(
                    [
                        _query("inc", "incomplete_freights"),
                        _query("diag", "non_calculation_diagnostics"),
                    ]
                )
            )
        published = _published_from_prompt(contents)
        rendered = json.dumps(published, ensure_ascii=False)
        assert "missing_coverage_mapping" in rendered
        assert "route_not_served" in rendered
        assert "não significa rota não atendida" in contents
        assert "Frete parcial não é frete totalmente calculado" in contents
        return SimpleNamespace(
            text=(
                "Há frete incompleto no recorte consultado; isso não o torna um frete totalmente calculado. "
                "O motivo oficial de não cálculo inclui ausência de mapeamento de cobertura, "
                "o que não significa que a rota não seja atendida."
            )
        )

    models, plans, publications = _install(monkeypatch, handler)
    reply = _ask(
        ready,
        usuario,
        "Quais fretes ficaram incompletos e por que não foram calculados?",
        request_id="q247-diag",
    )
    assert "não significa que a rota não seja atendida" in reply["answer"]
    assert "totalmente calculado" in reply["answer"]
    assert [item["type"] for item in plans[0]["queries"]] == ["incomplete_freights", "non_calculation_diagnostics"]
    rendered = json.dumps(publications[0], ensure_ascii=False)
    assert "missing_coverage_mapping" in rendered
    assert "route_not_served" in rendered
    assert _SECRET_DOCUMENT not in models.calls[1]["contents"]
    assert len(models.calls) == 2


def test_uf_weight_and_ranking_page_remain_the_consulted_cut(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    questions = {
        "Qual foi a economia em São Paulo?": [_query("uf", "by_uf", filters={"ufs": ["SP"]})],
        "Como os custos variam por faixa de peso?": [_query("peso", "by_weight")],
        "Mostre o frete com maior diferença.": [_query("rank", "top_differences", page={"offset": 0, "limit": 1})],
    }

    def handler(contents):
        if "Seleção de consultas" in contents:
            question = contents.rsplit("Usuario:", 1)[-1].strip()
            return SimpleNamespace(text=_selection(questions[question]))
        published = _published_from_prompt(contents)
        kinds = {item["query_type"] for item in published["queries"]}
        assert "Não apresente subconjunto ou página como se fosse o total" in contents
        if "by_uf" in kinds:
            assert "SP" in json.dumps(published, ensure_ascii=False)
            return SimpleNamespace(text="Em São Paulo, a leitura fica restrita ao recorte geográfico publicado.")
        if "by_weight" in kinds:
            assert "weight_band_population autoriza faixas de peso" in contents
            return SimpleNamespace(text="A variação por peso usa somente as faixas publicadas neste recorte.")
        page = published["queries"][0].get("page") or {}
        assert page.get("limit") == 1
        return SimpleNamespace(
            text=(
                f"A lista mostra {page.get('returned')} frete desta consulta, "
                f"de {page.get('total')} no ordenamento. Ela não representa a base inteira."
            )
        )

    models, plans, publications = _install(monkeypatch, handler)
    answers = []
    for question in questions:
        reply = _ask(ready, usuario, question, request_id=f"q247-{abs(hash(question))}")
        assert reply.get("error") is None
        answers.append(reply["answer"])
    assert answers[0] == "Em São Paulo, a leitura fica restrita ao recorte geográfico publicado."
    assert "faixas publicadas" in answers[1]
    assert "não representa a base inteira" in answers[2]
    assert [plan["queries"][0]["type"] for plan in plans] == ["by_uf", "by_weight", "top_differences"]
    assert plans[0]["queries"][0]["filters"]["ufs"] == ["SP"]
    assert plans[2]["queries"][0]["page"] == {"offset": 0, "limit": 1}
    ranking = next(item for item in publications[2]["queries"] if item["query_type"] == "top_differences")
    assert ranking["page"]["limit"] == 1
    assert '"limit": 1' in models.calls[-1]["contents"]
    assert len(models.calls) == 6


def test_follow_up_still_uses_confirmed_referents_without_a_third_generation(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()

    def handler(contents):
        if "Seleção de consultas" not in contents:
            assert "Não use a estrutura fixa de cinco partes" in contents
            assert "não comprovam valores" in contents
            return SimpleNamespace(text="Resposta fundamentada na publicação oficial.")
        question = contents.rsplit("Usuario:", 1)[-1].strip()
        if question == "Mostre o frete com maior diferença.":
            return SimpleNamespace(text=_selection([_query("rank", "top_differences", page={"offset": 0, "limit": 1})]))
        referents = json.loads(contents.split("Referentes confirmados desta comparação:\n", 1)[1].split("\n\n", 1)[0])
        ranked = referents.get("ranked_rows") or []
        if not ranked:
            return SimpleNamespace(text=_selection([], action="clarify", clarification="Qual frete da lista você quer explicar?"))
        return SimpleNamespace(text=_selection([_query("motivo", "non_calculation_diagnostics")]))

    models, plans, _publications = _install(monkeypatch, handler)
    first = _ask(ready, usuario, "Mostre o frete com maior diferença.", request_id="q247-cont-1")
    assert first["answer"] == "Resposta fundamentada na publicação oficial."
    second = _ask(ready, usuario, "Por que o primeiro não foi calculado?", request_id="q247-cont-2")
    assert second.get("error") is None
    assert plans[1]["queries"][0]["type"] == "non_calculation_diagnostics"
    answer_prompt = models.calls[-1]["contents"]
    assert "Referentes confirmados, apenas para resolver menções" in answer_prompt
    assert '"row_index":' in answer_prompt
    assert "comparison_chat_publication_v1" in answer_prompt
    assert len(models.calls) == 4


def test_whatsapp_still_precedes_the_analytical_answer(ctx, storage_env, monkeypatch):
    del ctx, storage_env
    ready = _ready_context(monkeypatch)
    usuario = _usuario()
    order = []

    def handler(contents):
        order.append("gemini")
        if "Seleção de consultas" in contents:
            return SimpleNamespace(text=_selection([_query("total", "official_summary")]))
        return SimpleNamespace(text="Resposta analítica.")

    def _entrega(*args, **kwargs):
        order.append("whatsapp")
        return {
            "answer": "Entrega encaminhada.",
            "flow_type": "entrega_whatsapp",
            "deterministic": True,
        }

    _install(monkeypatch, handler)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.responder_entrega_whatsapp_se_pedida",
        _entrega,
    )
    reply = chat_agente_compara_comparison_reply(
        "me envia essa análise pelo whatsapp",
        [],
        session_obj=ready["session"],
        usuario=usuario,
        comparison_id=ready["comparison_id"],
        request_id="q247-wa",
        entrega_ja_avaliada=False,
    )
    assert reply["answer"] == "Entrega encaminhada."
    assert order == ["whatsapp"]
