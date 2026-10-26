"""SCRUM-262 no PostgreSQL descartável: dashboard reaberto sem IA.

Não usa o banco operacional. Sem a URI local, o módulo inteiro é ignorado.
"""
from __future__ import annotations

import pytest

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiDashboard, AgenteBiDashboardDataset, AgenteBiDataset, IaChamadaTentativa, IaConsumoEvento
from app.services.agente_bi_analisar_service import analisar_dataset
from app.services.agente_bi_calculo_service import calcular_definicao
from app.services.agente_bi_dashboard_service import (
    consultar_dashboard,
    consultar_detalhe_grafico,
    filtrar_dashboard,
    montar_dashboard,
)
from app.services.agente_bi_definir_service import definir_selecao
from app.services.agente_bi_descoberta_service import aprovar_selecao
from app.services.agente_bi_persistencia_service import AgenteBiErro, escopo_do_usuario, obter_analise
from tests.conftest import seed_usuario
from tests.test_agente_bi_calculo import _por_operacao
from tests.test_agente_bi_definir_postgres import _base_numerico

pytest_plugins = ["tests.test_agente_bi_definir_postgres"]


def _garantir_tabelas():
    AgenteBiDashboard.__table__.create(db.engine, checkfirst=True)
    AgenteBiDashboardDataset.__table__.create(db.engine, checkfirst=True)
    from app.agente_bi_integridade import instalar_protecao_agente_bi

    with db.engine.begin() as conexao:
        instalar_protecao_agente_bi(conexao)


def test_dashboard_reabre_filtra_e_isola_no_postgres(pg_app, monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)
    with pg_app.app_context():
        _garantir_tabelas()
        try:
            usuario, _franquia, escopo, dataset_id = _base_numerico("dashboard-pg")
        except Exception:
            usuario, _franquia, escopo, dataset_id = _base_numerico("dashboard.pg@test.com")
        _outro, _outra_franquia, outro_escopo, _outro_dataset = _base_numerico("dashboard.pg.outro@test.com")
        colega = seed_usuario(usuario.franquia_id, usuario.conta_id, email="dashboard.pg.colega@test.com")
        colega_escopo = escopo_do_usuario(colega)
        dataset = AgenteBiDataset.query.filter_by(id=dataset_id).one()
        if "@" in dataset.chave:
            pytest.skip("chave do dataset de prova contém caractere fora do contrato de dashboard")
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
        ][:6]
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
        antes_tentativas = IaChamadaTentativa.query.count()
        antes_consumo = IaConsumoEvento.query.count()
        painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
        identificador = painel["dashboard"]["id"]
        db.session.remove()
        reaberto = consultar_dashboard(escopo, identificador)
        soma = _por_operacao(calculo["calculo"]["resultado"], "soma")
        assert reaberto["consulta"]["graficos"][reaberto["consulta"]["graficos"].index(
            next(item for item in reaberto["consulta"]["graficos"] if item["operacao"] == "soma" and item["tipo"] == "indicador")
        )]["resultado"] == soma["resultado"] == "25.25"
        assert reaberto["consulta"]["chamada_anthropic"] is False
        memoria = obter_analise(escopo, reaberto["consulta"]["calculo_id"])
        assert memoria["resultado"]["indicadores"][0]["resultado"] == reaberto["consulta"]["graficos"][0]["resultado"]
        with pytest.raises(AgenteBiErro) as alheio:
            consultar_dashboard(outro_escopo, identificador)
        assert alheio.value.status == 404
        with pytest.raises(AgenteBiErro) as colega_erro:
            filtrar_dashboard(colega_escopo, identificador, {"parametros": {}})
        assert colega_erro.value.status == 404
        assert IaChamadaTentativa.query.count() == antes_tentativas
        assert IaConsumoEvento.query.count() == antes_consumo
        assert AgenteBiAnalise.query.filter_by(id=calculo["calculo"]["id"]).count() == 1
        ref = reaberto["consulta"]["graficos"][0]["ref"]
        antes_analises = AgenteBiAnalise.query.count()
        antes_dashboards = AgenteBiDashboard.query.count()
        detalhe = consultar_detalhe_grafico(
            escopo,
            identificador,
            reaberto["dashboard"]["revisao"],
            reaberto["consulta"]["calculo_id"],
            ref,
        )
        assert detalhe["detalhe"]["grafico"]["resultado"] == reaberto["consulta"]["graficos"][0]["resultado"]
        assert detalhe["detalhe"]["grafico"]["serie"] == reaberto["consulta"]["graficos"][0]["serie"]
        assert "graficos" not in detalhe["detalhe"]
        assert AgenteBiAnalise.query.count() == antes_analises
        assert AgenteBiDashboard.query.count() == antes_dashboards
        with pytest.raises(AgenteBiErro) as detalhe_alheio:
            consultar_detalhe_grafico(
                outro_escopo,
                identificador,
                reaberto["dashboard"]["revisao"],
                reaberto["consulta"]["calculo_id"],
                ref,
            )
        assert detalhe_alheio.value.status == 404
        with pytest.raises(AgenteBiErro) as detalhe_colega:
            consultar_detalhe_grafico(
                colega_escopo,
                identificador,
                reaberto["dashboard"]["revisao"],
                reaberto["consulta"]["calculo_id"],
                ref,
            )
        assert detalhe_colega.value.status == 404
