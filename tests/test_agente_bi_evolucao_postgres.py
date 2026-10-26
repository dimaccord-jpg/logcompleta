"""SCRUM-265 no PostgreSQL descartável: revisão nova e revisão anterior.

Não usa o banco operacional. Sem a URI local, o módulo inteiro é ignorado.
"""
from __future__ import annotations

import pytest

from app.extensions import db
from app.models import AgenteBiDashboard, AgenteBiDashboardDataset, IaChamadaTentativa, IaConsumoEvento
from app.services.agente_bi_calculo_service import calcular_definicao
from app.services.agente_bi_dashboard_service import consultar_dashboard, montar_dashboard
from app.services.agente_bi_definir_service import definir_selecao
from app.services.agente_bi_evolucao_service import evoluir_dashboard
from app.services.agente_bi_persistencia_service import obter_analise
from tests.test_agente_bi_definir_postgres import _base_numerico

pytest_plugins = ["tests.test_agente_bi_definir_postgres"]


def _garantir_tabelas():
    AgenteBiDashboard.__table__.create(db.engine, checkfirst=True)
    AgenteBiDashboardDataset.__table__.create(db.engine, checkfirst=True)


def test_filtro_textual_preserva_revisao_no_postgres(pg_app, monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)
    with pg_app.app_context():
        _garantir_tabelas()
        _usuario, _franquia, escopo, dataset_id = _base_numerico("evolucao-pg")
        from app.models import AgenteBiDataset
        from app.services.agente_bi_analisar_service import analisar_dataset
        from app.services.agente_bi_descoberta_service import aprovar_selecao

        dataset = AgenteBiDataset.query.filter_by(id=dataset_id).one()
        gravado = analisar_dataset(
            escopo,
            dataset_id,
            chave=dataset.chave,
            rodada=1,
            pedido={"modalidades": ["A"]},
        )
        ids = [
            item["id"]
            for item in gravado["resultado"]["sugestoes"]
            if item["status"] == "viavel" and item["grafico"] and item["juncao"] == "nao_executada"
        ][:1]
        assert ids
        aprovada = aprovar_selecao(
            escopo,
            {
                "analise_id": gravado["analise"]["id"],
                "sugestoes": ids,
                "perguntas": [{"id": item["id"], "confirmada": True} for item in gravado["resultado"]["perguntas"]],
                "confirmacao": True,
            },
        )
        definicao = definir_selecao(escopo, {"analise_id": aprovada["selecao"]["id"]})
        calculo = calcular_definicao(escopo, {"analise_id": definicao["definicao"]["id"]})
        painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
        antes_tentativas = IaChamadaTentativa.query.count()
        antes_consumo = IaConsumoEvento.query.count()
        identificador = painel["dashboard"]["id"]
        original = painel["consulta"]["graficos"][0]["resultado"]
        evolucao = evoluir_dashboard(escopo, identificador, {"texto": "filtre beta igual a sul"})
        assert evolucao["situacao"] == "aplicada"
        assert evolucao["via"] == "deterministica"
        assert evolucao["chamada_anthropic"] is False
        assert evolucao["cobranca"] is False
        assert evolucao["dashboard"]["revisao"] == 2
        assert evolucao["dashboard"]["id"] != identificador
        memoria = obter_analise(escopo, evolucao["consulta"]["calculo_id"])["resultado"]
        assert evolucao["consulta"]["graficos"][0]["resultado"] == memoria["indicadores"][0]["resultado"]
        assert evolucao["consulta"]["dataset_id"] == painel["consulta"]["dataset_id"]
        assert evolucao["consulta"]["versao"] == painel["consulta"]["versao"]
        reaberto = consultar_dashboard(escopo, identificador)
        assert reaberto["dashboard"]["revisao"] == 1
        assert reaberto["consulta"]["graficos"][0]["resultado"] == original
        assert IaChamadaTentativa.query.count() == antes_tentativas
        assert IaConsumoEvento.query.count() == antes_consumo
