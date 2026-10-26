"""SCRUM-260 no PostgreSQL descartável: definição persistida e isolada.

Não usa o banco operacional. Sem a URI local, o módulo inteiro é ignorado.

    AGENTE_BI_EXECUCAO_PG_URI=postgresql+psycopg2://pytest@127.0.0.1:55432/agente_bi_execucao_descartavel
"""
from __future__ import annotations

import pytest

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiDataset,
    AgenteBiDatasetBloco,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    CleitonCostConfig,
    ConfigRegras,
    Conta,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    User,
)
from app.services.agente_bi_analisar_service import analisar_dataset
from app.services.agente_bi_definir_service import CONTRATO_DEFINIR, definir_selecao
from app.services.agente_bi_descoberta_service import aprovar_selecao
from app.services.agente_bi_ingestao_service import ArquivoBruto, ingerir
from app.services.agente_bi_persistencia_service import escopo_do_usuario, obter_analise
from tests.conftest import seed_cleiton_cost_config, seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario
from tests.test_agente_bi_analisar import _movimento
from tests.test_agente_bi_integracao_postgres import _limites, _uri


@pytest.fixture
def pg_app(monkeypatch):
    uri = _uri()
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    flask_app = __import__("flask").Flask("agente-bi-definir-pg")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    tabelas = [
        Conta.__table__,
        Franquia.__table__,
        User.__table__,
        CleitonCostConfig.__table__,
        ConfigRegras.__table__,
        IaChamadaTentativa.__table__,
        IaConsumoEvento.__table__,
        IaConsumoAbatimento.__table__,
        AgenteBiDataset.__table__,
        AgenteBiDatasetBloco.__table__,
        AgenteBiAnalise.__table__,
        AgenteBiExecucao.__table__,
        AgenteBiExecucaoEvento.__table__,
    ]
    with flask_app.app_context():
        db.metadata.drop_all(bind=db.engine, tables=list(reversed(tabelas)))
        db.metadata.create_all(bind=db.engine, tables=tabelas)
        yield flask_app
        db.session.remove()
        db.metadata.drop_all(bind=db.engine, tables=list(reversed(tabelas)))
        db.session.remove()


def _base_numerico(email):
    """O CSV textual de integração não gera definição numérica. O gabarito é o movimento sintético."""
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    usuario = seed_usuario(franquia.id, conta.id, email=email)
    escopo = escopo_do_usuario(usuario)
    ingerido = ingerir(
        escopo,
        [ArquivoBruto(nome="movimento.xlsx", mime=None, conteudo=_movimento())],
        chave=f"lote-{email}",
        limites=_limites(),
    )
    return usuario, franquia, escopo, ingerido["dataset"]["id"]


def test_definicao_persiste_e_isola_no_postgres(pg_app):
    with pg_app.app_context():
        usuario, _franquia, escopo, dataset_id = _base_numerico("definir-pg@test.com")
        _outro, _outra_franquia, outro_escopo, _outro_dataset = _base_numerico("definir-pg-outro@test.com")
        colega = seed_usuario(usuario.franquia_id, usuario.conta_id, email="definir.pg.colega@test.com")
        colega_escopo = escopo_do_usuario(colega)
        dataset = AgenteBiDataset.query.filter_by(id=dataset_id).one()
        gravado = analisar_dataset(
            escopo,
            dataset_id,
            chave=dataset.chave,
            rodada=1,
            pedido={"modalidades": ["A"]},
        )
        resultado = gravado["resultado"]
        ids = [
            item["id"]
            for item in resultado["sugestoes"]
            if item["status"] == "viavel" and item["grafico"] and item["juncao"] == "nao_executada"
        ][:6]
        assert ids
        aprovada = aprovar_selecao(
            escopo,
            {
                "analise_id": gravado["analise"]["id"],
                "sugestoes": ids,
                "perguntas": [{"id": item["id"], "confirmada": True} for item in resultado["perguntas"]],
                "confirmacao": True,
            },
        )
        gravada = definir_selecao(escopo, {"analise_id": aprovada["selecao"]["id"]})
        assert gravada["definicao"]["contrato_versao"] == CONTRATO_DEFINIR
        assert gravada["definicao"]["resultado"]["chamada_anthropic"] is False
        assert gravada["definicao"]["resultado"]["execucao"] == "nao_executada"
        relida = obter_analise(escopo, gravada["definicao"]["id"])
        assert relida["usuario_id"] == escopo.usuario_id
        assert relida["resultado"]["definicoes"]
        with pytest.raises(Exception) as outro_erro:
            definir_selecao(outro_escopo, {"analise_id": aprovada["selecao"]["id"]})
        assert getattr(outro_erro.value, "codigo", "") == "nao_encontrado"
        with pytest.raises(Exception) as colega_erro:
            obter_analise(colega_escopo, gravada["definicao"]["id"])
        assert getattr(colega_erro.value, "codigo", "") == "nao_encontrado"
