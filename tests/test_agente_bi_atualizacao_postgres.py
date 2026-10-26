"""SCRUM-264 no PostgreSQL descartável: versão nova, recálculo e isolamento.

Não usa o banco operacional. Sem a URI local, o módulo inteiro é ignorado.
"""
from __future__ import annotations

import pytest

from app.extensions import db
from app.models import (
    AgenteBiDashboard,
    AgenteBiDashboardDataset,
    AgenteBiDataset,
    CleitonCostConfig,
    Conta,
    IaChamadaTentativa,
    IaConsumoEvento,
)
from app.services.agente_bi_atualizacao_service import atualizar_dashboard
from app.services.agente_bi_dashboard_service import consultar_dashboard
from app.services.agente_bi_persistencia_service import AgenteBiErro, escopo_do_usuario
from tests.conftest import seed_cleiton_cost_config, seed_sistema_interno, seed_usuario
from tests.test_agente_bi_atualizacao import _arquivos, _compativel, _painel, _soma
from tests.test_agente_bi_ingestao import _par

pytest_plugins = ["tests.test_agente_bi_definir_postgres"]


def _garantir_tabelas():
    AgenteBiDashboard.__table__.create(db.engine, checkfirst=True)
    AgenteBiDashboardDataset.__table__.create(db.engine, checkfirst=True)
    from app.agente_bi_integridade import instalar_protecao_agente_bi

    with db.engine.begin() as conexao:
        instalar_protecao_agente_bi(conexao)


def test_substituicao_recalcula_isola_e_preserva_historico_no_postgres(pg_app, monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)
    with pg_app.app_context():
        _garantir_tabelas()
        try:
            if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
                seed_sistema_interno()
            if db.session.get(CleitonCostConfig, 1) is None:
                seed_cleiton_cost_config()
            _usuario, franquia, escopo, _definicao, calculo, painel = _painel(
                "atualizacao.pg@test.com",
                "atualizacao-pg",
            )
            _outro, _outra_franquia, outro = _par("atualizacao-pg-outro", "atualizacao.pg.outro@test.com")
            outro_escopo = escopo_do_usuario(outro)
            colega = seed_usuario(franquia.id, _usuario.conta_id, email="atualizacao.pg.colega@test.com")
            colega_escopo = escopo_do_usuario(colega)
            dataset = AgenteBiDataset.query.filter_by(id=calculo["calculo"]["resultado"]["dataset_id"]).one()
            if "@" in dataset.chave:
                pytest.skip("chave do dataset de prova contém caractere fora do contrato de dashboard")
            antes_tentativas = IaChamadaTentativa.query.count()
            antes_consumo = IaConsumoEvento.query.count()
            antigo_id = painel["dashboard"]["id"]
            impressao = dataset.impressao
            with pytest.raises(AgenteBiErro) as erro:
                atualizar_dashboard(
                    outro_escopo,
                    antigo_id,
                    modo="substituir",
                    arquivos=_arquivos(_compativel()),
                )
            assert erro.value.codigo == "nao_encontrado"
            resultado = atualizar_dashboard(
                escopo,
                antigo_id,
                modo="substituir",
                arquivos=_arquivos(_compativel()),
            )
            assert resultado["recalculado"] is True
            assert resultado["chamada_anthropic"] is False
            assert resultado["versao"] == 2
            assert resultado["impressao"] != impressao
            assert _soma(resultado["consulta"]) == "29.5"
            db.session.remove()
            assert _soma(consultar_dashboard(escopo, antigo_id)["consulta"]) == "25.25"
            assert _soma(consultar_dashboard(escopo, resultado["dashboard"]["id"])["consulta"]) == "29.5"
            with pytest.raises(AgenteBiErro) as colega_erro:
                consultar_dashboard(colega_escopo, resultado["dashboard"]["id"])
            assert colega_erro.value.codigo == "nao_encontrado"
            assert IaChamadaTentativa.query.count() == antes_tentativas
            assert IaConsumoEvento.query.count() == antes_consumo
            novo = AgenteBiDataset.query.filter_by(id=resultado["dataset_id"]).one()
            assert novo.versao_anterior_id == dataset.id
            assert novo.usuario_id == escopo.usuario_id
            assert novo.conta_id == escopo.conta_id
        finally:
            AgenteBiDashboardDataset.__table__.drop(db.engine, checkfirst=True)
            AgenteBiDashboard.__table__.drop(db.engine, checkfirst=True)
