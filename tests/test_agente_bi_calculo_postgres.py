"""SCRUM-261 no PostgreSQL descartável: memória de cálculo persistida e isolada.

Não usa o banco operacional. Sem a URI local, o módulo inteiro é ignorado.
"""
from __future__ import annotations

import pytest

from app.services.agente_bi_calculo_service import CONTRATO_CALCULO, calcular_definicao
from app.services.agente_bi_definir_service import definir_selecao
from app.services.agente_bi_persistencia_service import obter_analise
from tests.conftest import seed_usuario
from tests.test_agente_bi_definir_postgres import _base_numerico

pytest_plugins = ["tests.test_agente_bi_definir_postgres"]


def test_memoria_de_calculo_persiste_e_isola_no_postgres(pg_app):
    with pg_app.app_context():
        usuario, _franquia, escopo, dataset_id = _base_numerico("calculo-pg@test.com")
        _outro, _outra_franquia, outro_escopo, _outro_dataset = _base_numerico("calculo-pg-outro@test.com")
        colega = seed_usuario(usuario.franquia_id, usuario.conta_id, email="calculo.pg.colega@test.com")
        from app.services.agente_bi_persistencia_service import escopo_do_usuario
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
        soma = next(
            item
            for item in calculo["calculo"]["resultado"]["indicadores"]
            if item["operacao"] == "soma" and item["grafico"] == "indicador" and item["status"] == "calculado"
        )
        assert soma["resultado"] == "25.25"
        assert calculo["calculo"]["resultado"]["ia_calculo"] is False
        relida = obter_analise(escopo, calculo["calculo"]["id"])
        assert relida["contrato_versao"] == CONTRATO_CALCULO
        assert relida["resultado"]["indicadores"] == calculo["calculo"]["resultado"]["indicadores"]
        with pytest.raises(Exception) as erro:
            calcular_definicao(outro_escopo, {"analise_id": definicao["definicao"]["id"]})
        assert getattr(erro.value, "codigo", "") == "nao_encontrado"
        with pytest.raises(Exception) as colega_erro:
            obter_analise(escopo_do_usuario(colega), calculo["calculo"]["id"])
        assert getattr(colega_erro.value, "codigo", "") == "nao_encontrado"
