"""SCRUM-269 etapa 3.4.1: persistência da aplicação v2.

Prepara a identidade do plano e o vínculo dos artefatos. A conclusão só
aceita cálculo da definição final e do plano governado. Não publica
dashboard, não calcula e não chama a Anthropic.

Inserção direta por ORM ou SQL pode gravar revisão ou cálculo divergentes
do JSON persistido. Os gatilhos não interpretam esse JSON; a limitação é
do banco, e o serviço é quem recusa a divergência.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.agente_bi_aplicacao_integridade import (
    CONTRATO_CALCULO_APLICACAO,
    CONTRATO_DEFINICAO_APLICACAO,
    CONTRATO_PLANO_APLICACAO,
    CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO,
    nomes_gatilhos_aplicacao,
)
from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiAplicacao,
    AgenteBiDashboard,
    AgenteBiExecucao,
    utcnow_naive,
)
from app.services.agente_bi_aplicacao_service import (
    AgenteBiErro,
    concluir_aplicacao,
    obter_aplicacao,
    obter_aplicacao_do_plano,
    preparar_aplicacao,
)
from app.services.agente_bi_calculo_service import CONTRATO_CALCULO_V2
from app.services.agente_bi_contrato_v2_service import CONTRATO_DEFINIR_V2, CONTRATO_PATCH_V2
from app.services.agente_bi_definir_service import CONTRATO_DEFINIR
from app.services.agente_bi_persistencia_service import (
    criar_analise,
    criar_dashboard,
    criar_dataset,
    escopo_do_usuario,
    obter_analise,
    obter_dashboard,
)
from tests.test_agente_bi_execucao_v2 import _base, _contagem, _op, _persistir_plano, _plano
from tests.test_agente_bi_fundacao import _dataset, _par, _run

ROOT = Path(__file__).resolve().parents[1]
FONTE_SERVICO = (ROOT / "app" / "services" / "agente_bi_aplicacao_service.py").read_text(encoding="utf-8")
FONTE_CALCULO = (ROOT / "app" / "services" / "agente_bi_calculo_service.py").read_text(encoding="utf-8")
FONTE_ROTAS = (ROOT / "app" / "agente_bi_api_routes.py").read_text(encoding="utf-8")
_MIGRATION = ROOT / "migrations" / "versions" / "n2o3p4q5r6s7_agente_bi_aplicacao_v2.py"
_CHAVES = frozenset(
    {
        "id",
        "conta_id",
        "franquia_id",
        "usuario_id",
        "plano_id",
        "plano_contrato",
        "dataset_id",
        "versao",
        "impressao",
        "dashboard_id",
        "dashboard_chave",
        "revisao_base",
        "definicao_anterior_id",
        "definicao_anterior_contrato",
        "estado",
        "definicao_id",
        "definicao_contrato",
        "calculo_id",
        "calculo_contrato",
        "dashboard_publicado_id",
        "revisao_publicada",
        "vinculos",
        "preparada_em",
        "concluida_em",
    }
)
_ERRO_INTEGRIDADE = (IntegrityError, OperationalError)


@pytest.fixture(autouse=True)
def _sem_anthropic(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido, raising=False)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido, raising=False)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido, raising=False)


def _cenario(email, slug):
    base = _base(email, slug, lambda modelo, colunas: [_contagem(modelo, colunas)])
    from app.services.agente_bi_evolucao_service import _carregar
    from app.services.agente_bi_interpretacao_v2_service import _graficos

    graficos = _graficos(_carregar(base["escopo"], base["dashboard_id"]))
    assert graficos
    plano = _persistir_plano(base, _plano([_op(acao="consultar", grafico=graficos[0])]))
    return base, plano


def _proxima_rodada(escopo, chave: str) -> int:
    from sqlalchemy import func

    atual = (
        db.session.query(func.max(AgenteBiAnalise.rodada))
        .filter_by(
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
            chave=chave,
        )
        .scalar()
    )
    return 1 if atual is None else int(atual) + 1


def _definicao_final(base):
    escopo = base["escopo"]
    painel = obter_dashboard(escopo, base["dashboard_id"])
    return criar_analise(
        escopo,
        {
            "chave": painel["chave"],
            "rodada": _proxima_rodada(escopo, painel["chave"]),
            "dataset_id": base["dataset_id"],
            "versao": base["versao"],
            "impressao": base["impressao"],
            "etapa": "definicao",
            "estado": "concluida",
            "contexto": {"contrato": "contexto.definicao.v2"},
            "regras_negocio": {"contrato": "definir.v2"},
            "esclarecimentos": {},
            "contrato_versao": "definir.v2",
            "resultado": {"contrato": "definir.v2"},
        },
    )


def _proxima_revisao(escopo, chave: str) -> int:
    from sqlalchemy import func

    atual = (
        db.session.query(func.max(AgenteBiDashboard.revisao))
        .filter_by(
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
            chave=chave,
        )
        .scalar()
    )
    return 1 if atual is None else int(atual) + 1


def _memoria_publicada(base, contexto, resultado):
    escopo = base["escopo"]
    painel = obter_dashboard(escopo, base["dashboard_id"])
    chave = painel["chave"]
    calculo = criar_analise(
        escopo,
        {
            "chave": chave,
            "rodada": _proxima_rodada(escopo, chave),
            "dataset_id": base["dataset_id"],
            "versao": base["versao"],
            "impressao": base["impressao"],
            "etapa": "execucao",
            "estado": "concluida",
            "contexto": contexto,
            "regras_negocio": {"contrato": "calculo.v2"},
            "esclarecimentos": {},
            "contrato_versao": "calculo.v2",
            "resultado": resultado,
        },
    )
    publicado = criar_dashboard(
        escopo,
        {
            "chave": chave,
            "revisao": _proxima_revisao(escopo, chave),
            "estado_publicacao": "publicado",
            "contrato_versao": "definir.v2",
            "executor_versao": "executor.deterministico.v1",
            "definicoes_graficos": [
                {
                    "ref": "g1",
                    "titulo": "Serie sintetica",
                    "tipo": "indicador",
                    "definicao": {"medida": "valor"},
                }
            ],
            "datasets": [
                {
                    "dataset_id": base["dataset_id"],
                    "versao": base["versao"],
                    "impressao": base["impressao"],
                }
            ],
            "analise_id": calculo["id"],
        },
    )
    return calculo, publicado


def _artefatos(base, plano):
    definicao = _definicao_final(base)
    calculo, publicado = _memoria_publicada(
        base,
        {
            "contrato": "contexto.calculo.v2",
            "definicao_id": definicao["id"],
            "plano_id": plano["id"],
        },
        {"contrato": "calculo.v2", "origem_definicao_id": definicao["id"]},
    )
    return definicao, calculo, publicado


def _recusar_conclusao(escopo, aplicacao_id, **kwargs):
    with pytest.raises(AgenteBiErro) as exc:
        concluir_aplicacao(escopo, aplicacao_id, **kwargs)
    assert exc.value.codigo == "identidade_invalida"
    assert exc.value.status == 409


def _permanece_preparada(escopo, aplicacao_id):
    atual = obter_aplicacao(escopo, aplicacao_id)
    assert atual["estado"] == "preparada"
    assert atual["definicao_id"] is None
    assert atual["calculo_id"] is None
    assert atual["dashboard_publicado_id"] is None
    assert atual["concluida_em"] is None


def _manual(escopo, plano, painel, **trocas):
    agora = utcnow_naive()
    dados = {
        "conta_id": escopo.conta_id,
        "franquia_id": escopo.franquia_id,
        "usuario_id": escopo.usuario_id,
        "plano_id": plano["id"],
        "plano_contrato": CONTRATO_PLANO_APLICACAO,
        "dataset_id": plano["dataset_id"],
        "dataset_versao": plano["versao"],
        "dataset_impressao": plano["impressao"],
        "dashboard_id": painel["id"],
        "dashboard_chave": painel["chave"],
        "revisao_base": painel["revisao"],
        "definicao_anterior_id": plano["contexto"]["interno"]["definicao_anterior_id"],
        "definicao_anterior_contrato": CONTRATO_DEFINIR,
        "estado": "preparada",
        "preparada_em": agora,
        "created_at": agora,
        "updated_at": agora,
    }
    dados.update(trocas)
    return AgenteBiAplicacao(**dados)


def _rejeita_insercao(linha) -> None:
    db.session.add(linha)
    with pytest.raises(_ERRO_INTEGRIDADE):
        db.session.flush()
    db.session.rollback()


def test_criar_identidade_unica_do_plano(ctx):
    base, plano = _cenario("aplicacao.identidade@test.com", "aplicacao-identidade")
    antes = AgenteBiDashboard.query.count()
    criada = preparar_aplicacao(base["escopo"], plano["id"])
    assert set(criada) == _CHAVES
    assert criada["estado"] == "preparada"
    assert criada["plano_id"] == plano["id"]
    assert criada["definicao_id"] is None
    assert criada["calculo_id"] is None
    assert criada["dashboard_publicado_id"] is None
    assert criada["vinculos"] == []
    repetida = preparar_aplicacao(base["escopo"], plano["id"])
    assert repetida["id"] == criada["id"]
    assert AgenteBiAplicacao.query.filter_by(plano_id=plano["id"]).count() == 1
    assert AgenteBiDashboard.query.count() == antes
    db.session.rollback()
    assert AgenteBiAplicacao.query.count() == 0


def test_rejeita_duplicacao(ctx):
    base, plano = _cenario("aplicacao.duplicada@test.com", "aplicacao-duplicada")
    criada = preparar_aplicacao(base["escopo"], plano["id"])
    db.session.commit()
    painel = obter_dashboard(base["escopo"], base["dashboard_id"])
    _rejeita_insercao(_manual(base["escopo"], plano, painel))
    assert AgenteBiAplicacao.query.filter_by(plano_id=plano["id"]).count() == 1
    assert obter_aplicacao_do_plano(base["escopo"], plano["id"])["id"] == criada["id"]


def test_vinculo_ao_plano_persistido(ctx):
    base, plano = _cenario("aplicacao.plano@test.com", "aplicacao-plano")
    registro = obter_analise(base["escopo"], plano["id"])
    painel = obter_dashboard(base["escopo"], base["dashboard_id"])
    criada = preparar_aplicacao(base["escopo"], plano["id"])
    assert criada["plano_id"] == registro["id"]
    assert criada["plano_contrato"] == registro["contrato_versao"] == CONTRATO_PLANO_APLICACAO
    assert criada["dashboard_id"] == painel["id"] == base["dashboard_id"]
    assert criada["revisao_base"] == painel["revisao"] == registro["contexto"]["interno"]["revisao_base"]
    assert criada["definicao_anterior_id"] == registro["contexto"]["interno"]["definicao_anterior_id"]
    assert "texto" not in criada
    assert "resultado" not in criada
    incompleto = _persistir_plano(
        base,
        plano["resultado"],
        estado="preparada",
        texto="plano ainda aberto",
    )
    with pytest.raises(AgenteBiErro) as exc:
        preparar_aplicacao(base["escopo"], incompleto["id"])
    assert exc.value.codigo == "analise_indisponivel"
    assert AgenteBiAplicacao.query.filter_by(plano_id=incompleto["id"]).count() == 0


def test_separa_usuario_conta_e_franquia(ctx):
    base, plano = _cenario("aplicacao.escopo.a@test.com", "aplicacao-escopo-a")
    criada = preparar_aplicacao(base["escopo"], plano["id"])
    db.session.commit()
    _conta, _franquia, outro = _par("aplicacao-escopo-b", "aplicacao.escopo.b@test.com")
    escopo_b = escopo_do_usuario(outro)
    assert criada["usuario_id"] != escopo_b.usuario_id
    assert criada["conta_id"] != escopo_b.conta_id
    assert criada["franquia_id"] != escopo_b.franquia_id
    with pytest.raises(AgenteBiErro) as preparar_alheio:
        preparar_aplicacao(escopo_b, plano["id"])
    assert preparar_alheio.value.codigo == "nao_encontrado"
    with pytest.raises(AgenteBiErro) as ler_alheio:
        obter_aplicacao(escopo_b, criada["id"])
    assert ler_alheio.value.codigo == "nao_encontrado"
    with pytest.raises(AgenteBiErro) as plano_alheio:
        obter_aplicacao_do_plano(escopo_b, plano["id"])
    assert plano_alheio.value.codigo == "nao_encontrado"
    painel = obter_dashboard(base["escopo"], base["dashboard_id"])
    _rejeita_insercao(
        _manual(
            base["escopo"],
            plano,
            painel,
            conta_id=escopo_b.conta_id,
            franquia_id=escopo_b.franquia_id,
            usuario_id=escopo_b.usuario_id,
            plano_id=plano["id"],
        )
    )
    assert AgenteBiAplicacao.query.filter_by(usuario_id=escopo_b.usuario_id).count() == 0


def test_identidade_completa_do_dataset(ctx):
    base, plano = _cenario("aplicacao.dataset@test.com", "aplicacao-dataset")
    criada = preparar_aplicacao(base["escopo"], plano["id"])
    assert criada["dataset_id"] == base["dataset_id"] == plano["dataset_id"]
    assert criada["versao"] == base["versao"] == plano["versao"]
    assert criada["impressao"] == base["impressao"] == plano["impressao"]
    assert len(criada["impressao"]) == 64
    db.session.commit()
    outro = _persistir_plano(base, plano["resultado"], texto="outro dataset do mesmo painel")
    alheio = criar_dataset(
        base["escopo"],
        _dataset(chave="outro-dataset", origem_ref="sintetico-02"),
    )
    painel = obter_dashboard(base["escopo"], base["dashboard_id"])
    _rejeita_insercao(
        _manual(
            base["escopo"],
            outro,
            painel,
            dataset_id=alheio["id"],
            dataset_versao=alheio["versao"],
            dataset_impressao=alheio["impressao"],
        )
    )
    assert AgenteBiAplicacao.query.filter_by(plano_id=outro["id"]).count() == 0
    assert obter_aplicacao(base["escopo"], criada["id"])["impressao"] == base["impressao"]


def test_revisao_base_consistente(ctx):
    base, plano = _cenario("aplicacao.revisao@test.com", "aplicacao-revisao")
    painel = obter_dashboard(base["escopo"], base["dashboard_id"])
    criada = preparar_aplicacao(base["escopo"], plano["id"])
    assert criada["dashboard_id"] == painel["id"]
    assert criada["dashboard_chave"] == painel["chave"]
    assert criada["revisao_base"] == painel["revisao"]
    db.session.commit()
    outro = _persistir_plano(base, plano["resultado"], texto="revisao trocada")
    _rejeita_insercao(_manual(base["escopo"], outro, painel, revisao_base=int(painel["revisao"]) + 3))
    assert AgenteBiAplicacao.query.filter_by(plano_id=outro["id"]).count() == 0


def test_estado_preparado_e_concluido(ctx):
    base, plano = _cenario("aplicacao.estado@test.com", "aplicacao-estado")
    preparada = preparar_aplicacao(base["escopo"], plano["id"])
    assert preparada["estado"] == "preparada"
    assert preparada["concluida_em"] is None
    db.session.commit()
    definicao, calculo, publicado = _artefatos(base, plano)
    memoria = obter_analise(base["escopo"], calculo["id"])
    assert memoria["contexto"]["definicao_id"] == definicao["id"]
    assert memoria["contexto"]["plano_id"] == plano["id"]
    assert memoria["resultado"]["origem_definicao_id"] == definicao["id"]
    concluida = concluir_aplicacao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=calculo["id"],
        dashboard_publicado_id=publicado["id"],
    )
    assert concluida["estado"] == "concluida"
    assert concluida["concluida_em"] is not None
    assert concluida["revisao_publicada"] == publicado["revisao"]
    assert concluida["revisao_publicada"] > concluida["revisao_base"]
    db.session.rollback()
    desfeita = obter_aplicacao(base["escopo"], preparada["id"])
    assert desfeita["estado"] == "preparada"
    assert desfeita["definicao_id"] is None
    assert desfeita["calculo_id"] is None
    assert desfeita["dashboard_publicado_id"] is None
    assert desfeita["concluida_em"] is None
    de_novo = concluir_aplicacao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=calculo["id"],
        dashboard_publicado_id=publicado["id"],
    )
    db.session.commit()
    assert de_novo["estado"] == "concluida"
    repetida = concluir_aplicacao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=calculo["id"],
        dashboard_publicado_id=publicado["id"],
    )
    assert repetida["id"] == de_novo["id"]
    linha = db.session.get(AgenteBiAplicacao, de_novo["id"])
    linha.updated_at = utcnow_naive()
    with pytest.raises(ValueError, match="imutavel"):
        db.session.flush()
    db.session.rollback()


def test_referencias_recuperaveis(ctx):
    base, plano = _cenario("aplicacao.recuperacao@test.com", "aplicacao-recuperacao")
    preparada = preparar_aplicacao(base["escopo"], plano["id"])
    db.session.commit()
    definicao, calculo, publicado = _artefatos(base, plano)
    concluida = concluir_aplicacao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=calculo["id"],
        dashboard_publicado_id=publicado["id"],
    )
    db.session.commit()
    db.session.expire_all()
    relida = obter_aplicacao_do_plano(base["escopo"], plano["id"])
    assert relida["id"] == concluida["id"]
    assert relida["definicao_id"] == definicao["id"]
    assert relida["calculo_id"] == calculo["id"]
    assert relida["dashboard_publicado_id"] == publicado["id"]
    assert relida["revisao_publicada"] == publicado["revisao"]
    assert {
        "dataset_id": base["dataset_id"],
        "versao": base["versao"],
        "impressao": base["impressao"],
    } in relida["vinculos"]
    painel = db.session.get(AgenteBiDashboard, relida["dashboard_publicado_id"])
    assert painel.analise_id == relida["calculo_id"]
    assert AgenteBiAplicacao.query.count() == 1
    assert AgenteBiDashboard.query.filter_by(chave=relida["dashboard_chave"]).count() == 2


def test_rejeita_referencias_cruzadas(ctx):
    base, plano = _cenario("aplicacao.cruzada@test.com", "aplicacao-cruzada")
    preparada = preparar_aplicacao(base["escopo"], plano["id"])
    db.session.commit()
    definicao, calculo, publicado = _artefatos(base, plano)
    painel = obter_dashboard(base["escopo"], base["dashboard_id"])
    outro = criar_dashboard(
        base["escopo"],
        {
            "chave": "outro-painel-sintetico",
            "revisao": 1,
            "estado_publicacao": "publicado",
            "contrato_versao": "definir.v2",
            "executor_versao": "executor.deterministico.v1",
            "definicoes_graficos": [
                {
                    "ref": "g9",
                    "titulo": "Outra serie",
                    "tipo": "tabela",
                    "definicao": {"medida": "valor"},
                }
            ],
            "datasets": [
                {
                    "dataset_id": base["dataset_id"],
                    "versao": base["versao"],
                    "impressao": base["impressao"],
                }
            ],
            "analise_id": calculo["id"],
        },
    )
    for nome, kwargs in (
        ("definicao", {"definicao_id": plano["id"], "calculo_id": calculo["id"], "dashboard_publicado_id": publicado["id"]}),
        ("calculo", {"definicao_id": definicao["id"], "calculo_id": base["calculo_id"], "dashboard_publicado_id": publicado["id"]}),
        ("painel", {"definicao_id": definicao["id"], "calculo_id": calculo["id"], "dashboard_publicado_id": painel["id"]}),
        ("linhagem", {"definicao_id": definicao["id"], "calculo_id": calculo["id"], "dashboard_publicado_id": outro["id"]}),
        ("anterior", {"definicao_id": base["definicao_id"], "calculo_id": calculo["id"], "dashboard_publicado_id": publicado["id"]}),
    ):
        with pytest.raises(AgenteBiErro) as exc:
            concluir_aplicacao(base["escopo"], preparada["id"], **kwargs)
        assert exc.value.codigo == "identidade_invalida", nome
    assert obter_aplicacao(base["escopo"], preparada["id"])["estado"] == "preparada"
    _conta, _franquia, alheio = _par("aplicacao-cruzada-b", "aplicacao.cruzada.b@test.com")
    escopo_b = escopo_do_usuario(alheio)
    with pytest.raises(AgenteBiErro) as exc:
        concluir_aplicacao(
            escopo_b,
            preparada["id"],
            definicao_id=definicao["id"],
            calculo_id=calculo["id"],
            dashboard_publicado_id=publicado["id"],
        )
    assert exc.value.codigo == "nao_encontrado"


def test_rejeita_calculo_de_outra_definicao(ctx):
    base, plano = _cenario("aplicacao.calculo.definicao@test.com", "aplicacao-calculo-definicao")
    preparada = preparar_aplicacao(base["escopo"], plano["id"])
    db.session.commit()
    definicao = _definicao_final(base)
    alheia = _definicao_final(base)
    calculo, publicado = _memoria_publicada(
        base,
        {
            "contrato": "contexto.calculo.v2",
            "definicao_id": alheia["id"],
            "plano_id": plano["id"],
        },
        {"contrato": "calculo.v2", "origem_definicao_id": alheia["id"]},
    )
    memoria = obter_analise(base["escopo"], calculo["id"])
    assert memoria["chave"] == definicao["chave"]
    assert memoria["dataset_id"] == preparada["dataset_id"]
    assert memoria["versao"] == preparada["versao"]
    assert memoria["impressao"] == preparada["impressao"]
    _recusar_conclusao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=calculo["id"],
        dashboard_publicado_id=publicado["id"],
    )
    _permanece_preparada(base["escopo"], preparada["id"])
    origem, painel_origem = _memoria_publicada(
        base,
        {
            "contrato": "contexto.calculo.v2",
            "definicao_id": definicao["id"],
            "plano_id": plano["id"],
        },
        {"contrato": "calculo.v2", "origem_definicao_id": alheia["id"]},
    )
    _recusar_conclusao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=origem["id"],
        dashboard_publicado_id=painel_origem["id"],
    )
    _permanece_preparada(base["escopo"], preparada["id"])


def test_rejeita_calculo_de_outro_plano(ctx):
    base, plano = _cenario("aplicacao.calculo.plano@test.com", "aplicacao-calculo-plano")
    preparada = preparar_aplicacao(base["escopo"], plano["id"])
    db.session.commit()
    outro = _persistir_plano(base, plano["resultado"], texto="outro plano do mesmo dataset")
    definicao = _definicao_final(base)
    calculo, publicado = _memoria_publicada(
        base,
        {
            "contrato": "contexto.calculo.v2",
            "definicao_id": definicao["id"],
            "plano_id": outro["id"],
        },
        {"contrato": "calculo.v2", "origem_definicao_id": definicao["id"]},
    )
    memoria = obter_analise(base["escopo"], calculo["id"])
    assert memoria["chave"] == definicao["chave"]
    assert memoria["dataset_id"] == preparada["dataset_id"]
    assert memoria["impressao"] == preparada["impressao"]
    _recusar_conclusao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=calculo["id"],
        dashboard_publicado_id=publicado["id"],
    )
    _permanece_preparada(base["escopo"], preparada["id"])


def test_rejeita_calculo_sem_referencia_obrigatoria(ctx):
    base, plano = _cenario("aplicacao.calculo.referencia@test.com", "aplicacao-calculo-referencia")
    preparada = preparar_aplicacao(base["escopo"], plano["id"])
    db.session.commit()
    definicao = _definicao_final(base)
    casos = (
        {"contrato": "contexto.calculo.v2", "plano_id": plano["id"]},
        {"contrato": "contexto.calculo.v2", "definicao_id": definicao["id"]},
        {"contrato": "contexto.calculo.v2"},
        {
            "contrato": "contexto.calculo.v2",
            "definicao_id": str(definicao["id"]),
            "plano_id": plano["id"],
        },
        {
            "contrato": "contexto.calculo.v2",
            "definicao_id": definicao["id"],
            "plano_id": str(plano["id"]),
        },
    )
    for contexto in casos:
        calculo, publicado = _memoria_publicada(
            base,
            contexto,
            {"contrato": "calculo.v2", "origem_definicao_id": definicao["id"]},
        )
        memoria = obter_analise(base["escopo"], calculo["id"])
        assert memoria["chave"] == definicao["chave"]
        assert memoria["dataset_id"] == preparada["dataset_id"]
        assert memoria["impressao"] == preparada["impressao"]
        _recusar_conclusao(
            base["escopo"],
            preparada["id"],
            definicao_id=definicao["id"],
            calculo_id=calculo["id"],
            dashboard_publicado_id=publicado["id"],
        )
        _permanece_preparada(base["escopo"], preparada["id"])
    calculo, publicado = _memoria_publicada(
        base,
        {
            "contrato": "contexto.calculo.v2",
            "definicao_id": definicao["id"],
            "plano_id": plano["id"],
        },
        {"contrato": "calculo.v2"},
    )
    concluida = concluir_aplicacao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=calculo["id"],
        dashboard_publicado_id=publicado["id"],
    )
    assert concluida["estado"] == "concluida"
    assert concluida["definicao_id"] == definicao["id"]
    assert concluida["calculo_id"] == calculo["id"]
    assert "origem_definicao_id" not in obter_analise(base["escopo"], calculo["id"])["resultado"]
    db.session.commit()
    repetida = concluir_aplicacao(
        base["escopo"],
        preparada["id"],
        definicao_id=definicao["id"],
        calculo_id=calculo["id"],
        dashboard_publicado_id=publicado["id"],
    )
    assert repetida["id"] == concluida["id"]
    assert repetida["estado"] == "concluida"
    assert repetida["concluida_em"] == concluida["concluida_em"]


def test_rejeita_base_divergente_com_revisao_posterior(ctx):
    """Revisão 2 existente não substitui a base gravada no plano.

    O serviço relê o plano. Inserção direta da aplicação, com a revisão
    posterior nas colunas quando elas batem com a linha do dashboard,
    continua possível: os gatilhos não leem o JSON do plano.
    """
    base, plano = _cenario("aplicacao.base.divergente@test.com", "aplicacao-base-divergente")
    painel = obter_dashboard(base["escopo"], base["dashboard_id"])
    assert painel["revisao"] == 1
    posterior = criar_dashboard(
        base["escopo"],
        {
            "chave": painel["chave"],
            "revisao": 2,
            "estado_publicacao": "publicado",
            "contrato_versao": "definir.v2",
            "executor_versao": "executor.deterministico.v1",
            "definicoes_graficos": [
                {
                    "ref": "g1",
                    "titulo": "Serie sintetica",
                    "tipo": "indicador",
                    "definicao": {"medida": "valor"},
                }
            ],
            "datasets": [
                {
                    "dataset_id": base["dataset_id"],
                    "versao": base["versao"],
                    "impressao": base["impressao"],
                }
            ],
            "analise_id": base["calculo_id"],
        },
    )
    assert posterior["id"] != painel["id"]
    assert posterior["revisao"] == 2
    assert posterior["chave"] == painel["chave"]
    criada = preparar_aplicacao(base["escopo"], plano["id"])
    assert criada["revisao_base"] == 1
    assert criada["dashboard_id"] == painel["id"]
    assert criada["dashboard_id"] != posterior["id"]
    registro = obter_analise(base["escopo"], plano["id"])
    contexto = copy.deepcopy(registro["contexto"])
    identidade = "plano.baserevisao1divergente"
    contexto["interno"]["dashboard_id"] = posterior["id"]
    contexto["interno"]["identidade"] = identidade
    assert contexto["interno"]["revisao_base"] == 1
    divergente = criar_analise(
        base["escopo"],
        {
            "chave": registro["chave"],
            "rodada": _proxima_rodada(base["escopo"], registro["chave"]),
            "dataset_id": registro["dataset_id"],
            "versao": registro["versao"],
            "impressao": registro["impressao"],
            "etapa": registro["etapa"],
            "estado": "concluida",
            "contexto": contexto,
            "regras_negocio": copy.deepcopy(registro["regras_negocio"]),
            "esclarecimentos": copy.deepcopy(registro["esclarecimentos"]),
            "contrato_versao": registro["contrato_versao"],
            "resultado": copy.deepcopy(registro["resultado"]),
            "chave_recuperacao": identidade,
        },
    )
    with pytest.raises(AgenteBiErro) as exc:
        preparar_aplicacao(base["escopo"], divergente["id"])
    assert exc.value.codigo == "identidade_inconsistente"
    assert exc.value.status == 409
    assert AgenteBiAplicacao.query.filter_by(plano_id=divergente["id"]).count() == 0
    preservada = obter_aplicacao(base["escopo"], criada["id"])
    assert preservada["estado"] == "preparada"
    assert preservada["revisao_base"] == 1
    assert preservada["dashboard_id"] == painel["id"]


def test_modelos_existentes_seguem_compativeis(ctx):
    assert CONTRATO_PLANO_APLICACAO == CONTRATO_PATCH_V2
    assert CONTRATO_DEFINICAO_APLICACAO == CONTRATO_DEFINIR_V2
    assert set(CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO) == {CONTRATO_DEFINIR, CONTRATO_DEFINIR_V2}
    assert CONTRATO_CALCULO_APLICACAO == CONTRATO_CALCULO_V2
    colunas = {coluna.name for coluna in AgenteBiAplicacao.__table__.columns}
    assert not any("json" in nome or nome in {"texto", "filtro", "parametro", "resultado", "prompt"} for nome in colunas)
    base, _plano_gravado = _cenario("aplicacao.compat@test.com", "aplicacao-compat")
    assert AgenteBiAplicacao.query.count() == 0
    assert AgenteBiDashboard.query.filter_by(id=base["dashboard_id"]).count() == 1
    assert AgenteBiAnalise.query.filter_by(id=base["definicao_id"]).count() == 1
    assert AgenteBiExecucao.__table__.c.analise_id is not None
    painel = db.session.get(AgenteBiDashboard, base["dashboard_id"])
    painel.estado_publicacao = "arquivado"
    with pytest.raises(ValueError, match="imutavel"):
        db.session.flush()
    db.session.rollback()
    assert "AgenteBiAplicacao" not in FONTE_CALCULO
    assert "Não grava análise, revisão ou dashboard." in FONTE_CALCULO
    assert "preparar_aplicacao" not in FONTE_ROTAS
    assert "criar_dashboard" not in FONTE_SERVICO
    assert "calcular_definicao" not in FONTE_SERVICO
    assert ".commit(" not in FONTE_SERVICO
    assert "re.compile" not in FONTE_SERVICO
    assert "anthropic" not in FONTE_SERVICO.lower()


def test_migration_reversivel():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    revisao = script.get_revision("n2o3p4q5r6s7")
    assert revisao is not None
    assert revisao.down_revision == "m1n2o3p4q5r6"
    assert script.get_heads() == ["o3p4q5r6s7t8"]
    spec = importlib.util.spec_from_file_location("mig_aplicacao_v2", _MIGRATION)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)
    engine = create_engine("sqlite://")
    AgenteBiAplicacao.metadata.create_all(engine)
    assert "agente_bi_aplicacao" in inspect(engine).get_table_names()

    def _nomes():
        return set(inspect(engine).get_table_names())

    _run(engine, mig, mig.downgrade)
    assert "agente_bi_aplicacao" not in _nomes()
    assert "agente_bi_analise" in _nomes()
    assert "agente_bi_dashboard" in _nomes()
    assert "agente_bi_execucao" in _nomes()
    _run(engine, mig, mig.upgrade)
    inspetor = inspect(engine)
    assert {coluna["name"] for coluna in inspetor.get_columns("agente_bi_aplicacao")} == {
        coluna.name for coluna in AgenteBiAplicacao.__table__.columns
    }
    with engine.connect() as conn:
        gatilhos = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert set(nomes_gatilhos_aplicacao("sqlite")) <= gatilhos
    _run(engine, mig, mig.downgrade)
    assert "agente_bi_aplicacao" not in _nomes()
    assert "agente_bi_analise" in _nomes()
    with engine.connect() as conn:
        gatilhos = {row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='trigger'"))}
    assert not (set(nomes_gatilhos_aplicacao("sqlite")) & gatilhos)
