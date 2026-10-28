"""SCRUM-269 etapa 3.3: execução determinística de definir.v2.

Reusa o agregador de definir.v1. Não publica dashboard, não grava revisão
e não chama a Anthropic.
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiDashboard, IaChamadaTentativa
from app.services.agente_bi_analisar_service import montar_contexto
from app.services.agente_bi_calculo_service import (
    CONTRATO_CALCULO_V2,
    EXECUTOR,
    CalculoErro,
    calcular_definicao,
    calcular_definicao_v2,
)
from app.services.agente_bi_composicao_service import compor_definicao
from app.services.agente_bi_contrato_v2_service import CONTRATO_DEFINIR_V2, CONTRATO_PATCH_V2
from app.services.agente_bi_dashboard_service import montar_dashboard
from app.services.agente_bi_definir_service import CONTRATO_DEFINIR, definir
from app.services.agente_bi_leitura_service import CONTRATO_CONTEXTO_DEFINICAO_V2
from app.services.agente_bi_persistencia_service import criar_analise, escopo_do_usuario
from tests.conftest import seed_usuario
from tests.test_agente_bi_calculo import _gravar
from tests.test_agente_bi_definir import _jornada
from tests.test_agente_bi_ingestao import _ingerir, _par, _xlsx

PRECISAO = Context(prec=28, rounding=ROUND_HALF_EVEN)
ROOT = Path(__file__).resolve().parents[1]
FONTE_ROTAS = (ROOT / "app" / "agente_bi_api_routes.py").read_text(encoding="utf-8")
FONTE_CALCULO = (ROOT / "app" / "services" / "agente_bi_calculo_service.py").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _sem_anthropic(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido, raising=False)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido, raising=False)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido, raising=False)


def _planilha():
    return _xlsx(
        {
            "movimento": [
                ["categoria", "parte", "todo", "quando"],
                ["norte", 2, 4, date(2024, 1, 10)],
                ["norte", 2, 4, date(2024, 1, 20)],
                ["sul", 1, 2, date(2024, 2, 1)],
            ]
        }
    )


def _texto(valor: Decimal) -> str:
    texto = format(valor, "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto or "0"


def _dividir(numerador, denominador) -> str:
    return _texto(PRECISAO.divide(Decimal(numerador), Decimal(denominador)))


def _ref(coluna) -> dict:
    return {
        "arquivo": coluna["arquivo"],
        "aba": coluna["aba"],
        "regiao": coluna["regiao"],
        "indice": coluna["indice"],
    }


def _por_indice(perfil, indice):
    return next(coluna for coluna in perfil["colunas"] if coluna["aba"] == 0 and coluna["indice"] == indice)


def _grafico(modelo, identificador, *, operacao, grafico, leitura, medida, agrupamento=None, segunda=None):
    item = copy.deepcopy(modelo)
    item["id"] = identificador
    item["status"] = "viavel"
    item["operacao"] = operacao
    item["grafico"] = grafico
    item["leitura"] = leitura
    item["medida"] = _ref(medida)
    item["agrupamento"] = None if agrupamento is None else _ref(agrupamento)
    item["segunda_medida"] = None if segunda is None else _ref(segunda)
    item["regiao"] = {
        "arquivo": item["medida"]["arquivo"],
        "aba": item["medida"]["aba"],
        "regiao": item["medida"]["regiao"],
    }
    item["juncao"] = "nao_executada"
    return item


def _op(**extras):
    base = {
        "id": "o1",
        "acao": "consultar",
        "grafico": "g1",
        "temporario": None,
        "dependencias": [],
        "coluna": None,
        "segunda_coluna": None,
        "operacao": None,
        "apresentacao": None,
        "operador": None,
        "parametros": [],
        "esclarecimento": None,
    }
    base.update(extras)
    return base


def _plano(operacoes):
    return {"contrato": CONTRATO_PATCH_V2, "versao": 2, "situacao": "proposto", "operacoes": operacoes}


def _contagens():
    return (
        AgenteBiAnalise.query.count(),
        AgenteBiDashboard.query.count(),
        IaChamadaTentativa.query.count(),
    )


def _vigiar(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("persistencia")

    monkeypatch.setattr("app.services.agente_bi_calculo_service.criar_analise", proibido)
    monkeypatch.setattr("app.services.agente_bi_dashboard_service.montar_dashboard", proibido)
    monkeypatch.setattr("app.services.agente_bi_persistencia_service.criar_dashboard", proibido)


def _base(email, slug, graficos):
    _usuario, _franquia, escopo, perfil, pacote, _aprovada = _jornada(email, slug, _planilha())
    colunas = {
        "categoria": _por_indice(perfil, 0),
        "parte": _por_indice(perfil, 1),
        "todo": _por_indice(perfil, 2),
        "quando": _por_indice(perfil, 3),
    }
    assert colunas["parte"]["dimensao_principal"] == colunas["todo"]["dimensao_principal"]
    assert colunas["parte"]["dimensao_principal"] in {"quantidade", "valor"}
    pacote = copy.deepcopy(pacote)
    pacote["graficos"] = graficos(pacote["graficos"][0], colunas)
    documento = definir(perfil, pacote)
    salva = _gravar(escopo, pacote["id"], documento)
    calculado = calcular_definicao(escopo, {"analise_id": salva["id"]})
    painel = montar_dashboard(escopo, {"analise_id": calculado["calculo"]["id"]})
    tecnico = montar_contexto(perfil, {"modalidades": ["A"]})
    return {
        "escopo": escopo,
        "perfil": perfil,
        "colunas": colunas,
        "documento": documento,
        "definicao_id": salva["id"],
        "calculo_id": calculado["calculo"]["id"],
        "memoria_v1": calculado["calculo"]["resultado"],
        "revisao": painel["dashboard"]["revisao"],
        "dashboard_id": painel["dashboard"]["id"],
        "chave": salva["chave"],
        "dataset_id": salva["dataset_id"],
        "versao": salva["versao"],
        "impressao": salva["impressao"],
        "contexto": {chave: tecnico[chave] for chave in ("colunas", "regioes", "relacoes", "qualidade")},
        "salva": salva,
    }


def _contagem(modelo, colunas, identificador="s1"):
    return _grafico(
        modelo,
        identificador,
        operacao="contagem",
        grafico="indicador",
        leitura="contagem_de_registros",
        medida=colunas["parte"],
    )


def _soma(modelo, colunas, identificador="s1"):
    return _grafico(
        modelo,
        identificador,
        operacao="soma",
        grafico="indicador",
        leitura="indicador_de_medida",
        medida=colunas["parte"],
    )


def _participacao(modelo, colunas, identificador="s2"):
    return _grafico(
        modelo,
        identificador,
        operacao="participacao",
        grafico="indicador",
        leitura="indicador_de_medida",
        medida=colunas["parte"],
    )


def _compor(base, operacoes):
    ident = {
        "definicao_anterior_id": base["definicao_id"],
        "revisao_base": base["revisao"],
        "origem_contrato": CONTRATO_DEFINIR,
    }
    anterior = {
        "contrato": CONTRATO_DEFINIR,
        "analise_id": base["definicao_id"],
        "definicoes": base["documento"]["definicoes"],
    }
    saida = compor_definicao(_plano(operacoes), anterior, base["contexto"], [], ident)
    assert saida.get("contrato") == CONTRATO_DEFINIR_V2, saida
    return saida


def _chamar(base, definicao, alvos, parametros=None, *, monkeypatch, plano_id=None, plano=None, sem_plano=False):
    if not sem_plano and plano_id is None:
        plano_id = _persistir_plano(base, definicao["plano"] if plano is None else plano)["id"]
    _vigiar(monkeypatch)
    antes = _contagens()
    corpo = {"definicao": definicao, "alvos": list(alvos)}
    if parametros is not None:
        corpo["parametros"] = parametros
    if plano_id is not None:
        corpo["plano_id"] = plano_id
    try:
        return calcular_definicao_v2(base["escopo"], corpo)
    finally:
        assert _contagens() == antes


def _por_id(memoria, identificador):
    return next(item for item in memoria["indicadores"] if item["id"] == identificador)


def _criar(identificador, temporario, coluna, operacao, apresentacao, **extras):
    return _op(
        id=identificador,
        acao="criar_grafico",
        grafico=None,
        temporario=temporario,
        coluna=_ref(coluna),
        operacao=operacao,
        apresentacao=apresentacao,
        **extras,
    )


def _persistir_plano(base, plano, *, estado="concluida", texto=None):
    from app.services.agente_bi_evolucao_service import _carregar
    from app.services.agente_bi_interpretacao_v2_service import (
        CONTRATO_CONTEXTO_PLANO,
        _associacoes_comprovadas,
        _colunas,
        _contexto_interno,
        _graficos,
        _identidade,
        _parametros_opacos,
    )

    if texto is None:
        texto = "plano governado " + uuid4().hex
    carregado = _carregar(base["escopo"], base["dashboard_id"])
    identidade = _identidade(base["escopo"], carregado, texto)
    perfil = carregado.get("perfil") if isinstance(carregado.get("perfil"), dict) else {}
    atual = (
        db.session.query(func.max(AgenteBiAnalise.rodada))
        .filter_by(
            conta_id=base["escopo"].conta_id,
            franquia_id=base["escopo"].franquia_id,
            usuario_id=base["escopo"].usuario_id,
            chave=base["chave"],
        )
        .scalar()
    )
    corpo = {
        "chave": base["chave"],
        "rodada": 1 if atual is None else int(atual) + 1,
        "dataset_id": base["dataset_id"],
        "versao": base["versao"],
        "impressao": base["impressao"],
        "etapa": "definicao",
        "estado": estado,
        "contexto": {
            "contrato": CONTRATO_CONTEXTO_PLANO,
            "vinculo_preparacao": perfil.get("vinculo_preparacao"),
            "interno": _contexto_interno(base["escopo"], carregado, identidade),
        },
        "regras_negocio": {
            "contrato": CONTRATO_PATCH_V2,
            "vinculo": {"graficos": _graficos(carregado), "colunas": _colunas(carregado)},
            "parametros": _parametros_opacos(carregado),
            "associacoes": _associacoes_comprovadas(carregado),
            "texto": texto,
        },
        "esclarecimentos": {},
        "contrato_versao": CONTRATO_PATCH_V2,
        "chave_recuperacao": identidade,
    }
    if estado == "concluida":
        corpo["resultado"] = plano
    return criar_analise(base["escopo"], corpo)


def _recusar_calculo(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("calculou")

    monkeypatch.setattr("app.services.agente_bi_calculo_service._calcular_um", proibido)


def _persistir_v2(base, definicao):
    atual = (
        db.session.query(func.max(AgenteBiAnalise.rodada))
        .filter_by(
            conta_id=base["escopo"].conta_id,
            franquia_id=base["escopo"].franquia_id,
            usuario_id=base["escopo"].usuario_id,
            chave=base["chave"],
        )
        .scalar()
    )
    vinculo = {
        "definicao_anterior_id": base["definicao_id"],
        "revisao_base": base["revisao"],
        "origem_contrato": CONTRATO_DEFINIR,
        "definicoes": [
            {"id": item["id"], "sugestao_id": item["sugestao_id"], "origem": item["origem"]}
            for item in base["documento"]["definicoes"]
        ],
        "contexto": base["contexto"],
        "plano": definicao["plano"],
    }
    return criar_analise(
        base["escopo"],
        {
            "chave": base["chave"],
            "rodada": int(atual) + 1,
            "dataset_id": base["dataset_id"],
            "versao": base["versao"],
            "impressao": base["impressao"],
            "etapa": "definicao",
            "estado": "concluida",
            "contexto": {
                "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
                "vinculo_preparacao": base["perfil"]["vinculo_preparacao"],
                "vinculo": vinculo,
            },
            "regras_negocio": {"contrato": CONTRATO_DEFINIR_V2},
            "esclarecimentos": {},
            "contrato_versao": CONTRATO_DEFINIR_V2,
            "resultado": definicao,
        },
    )


def test_soma_media_contagem_e_agrupamento_batem_com_o_gabarito(ctx, monkeypatch):
    base = _base(
        "execucao.v2.agregacao@test.com",
        "execucao-v2-agregacao",
        lambda modelo, colunas: [_contagem(modelo, colunas)],
    )
    parte = base["colunas"]["parte"]
    categoria = base["colunas"]["categoria"]
    definicao = _compor(
        base,
        [
            _criar("o1", "t1", parte, "soma", "indicador"),
            _criar("o2", "t2", parte, "media", "indicador"),
            _criar("o3", "t3", parte, "soma", "barra"),
            _op(
                id="o4",
                acao="alterar_agrupamento",
                grafico=None,
                temporario="t3",
                coluna=_ref(categoria),
                dependencias=["o3"],
            ),
        ],
    )
    congelada = copy.deepcopy(definicao)
    memoria = _chamar(base, definicao, ["g2", "g3", "g4"], monkeypatch=monkeypatch)["calculo"]
    assert definicao == congelada
    assert memoria["contrato"] == CONTRATO_CALCULO_V2
    assert memoria["executor"] == EXECUTOR
    assert memoria["ia_calculo"] is False
    assert memoria["chamada_anthropic"] is False
    assert memoria["persistido"] is False
    assert memoria["publicado"] is False
    assert memoria["dataset_id"] == base["perfil"]["dataset"]["id"]
    assert memoria["dataset_versao"] == base["perfil"]["dataset"]["versao"]
    assert memoria["dataset_impressao"] == base["perfil"]["dataset"]["impressao"]
    assert memoria["vinculo_preparacao"] == base["perfil"]["vinculo_preparacao"]
    assert memoria["alvos_obrigatorios"] == ["g2", "g3", "g4"]

    preservada = _por_id(memoria, "g1")
    assert preservada["papel"] == "preservado"
    assert preservada["status"] == "calculado"
    assert preservada["operacao"] == "contagem"
    assert preservada["resultado"] == "3"
    assert preservada["definicao_aplicada"]["id"] == "g1"

    soma = _por_id(memoria, "g2")
    assert soma["papel"] == "novo"
    assert soma["status"] == "calculado"
    assert soma["agregacao"] == "soma"
    assert soma["resultado"] == "5"
    assert soma["elegiveis"] == 3
    assert soma["unidade"] == parte["dimensao_principal"]
    assert soma["definicao_aplicada"]["metrica"]["operacao"] == "soma"
    assert soma["definicao_aplicada"]["metrica"]["coluna"] == _ref(parte)

    media = _por_id(memoria, "g3")
    assert media["resultado"] == _dividir("5", "3")
    assert media["elegiveis"] == 3
    assert media["agregacao"] == "media"
    assert media["dimensoes"] == []

    agrupado = _por_id(memoria, "g4")
    assert agrupado["resultado"] is None
    assert agrupado["total"] == "5"
    assert agrupado["serie"] == [
        {"chave": "norte", "resultado": "4", "elegiveis": 2},
        {"chave": "sul", "resultado": "1", "elegiveis": 1},
    ]
    assert agrupado["dimensoes"][0]["coluna"] == _ref(categoria)
    assert {item["id"] for item in memoria["indicadores"] if item["papel"] == "novo"} == {"g2", "g3", "g4"}


def test_alteracao_de_medida(ctx, monkeypatch):
    base = _base(
        "execucao.v2.medida@test.com",
        "execucao-v2-medida",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    todo = base["colunas"]["todo"]
    definicao = _compor(
        base,
        [_op(acao="alterar_indicador", operacao="soma", coluna=_ref(todo))],
    )
    memoria = _chamar(base, definicao, ["g1"], monkeypatch=monkeypatch)["calculo"]
    alterado = _por_id(memoria, "g1")
    assert alterado["papel"] == "alterado"
    assert alterado["resultado"] == "10"
    assert alterado["elegiveis"] == 3
    assert alterado["definicao_aplicada"]["metrica"]["coluna"] == _ref(todo)
    assert alterado["definicao_aplicada"]["metrica"]["operacao"] == "soma"
    assert base["memoria_v1"]["indicadores"][0]["resultado"] == "5"


def test_adicao_de_grafico(ctx, monkeypatch):
    base = _base(
        "execucao.v2.adicao@test.com",
        "execucao-v2-adicao",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(
        base,
        [_criar("o1", "t1", base["colunas"]["parte"], "contagem", "indicador")],
    )
    memoria = _chamar(base, definicao, ["g2"], monkeypatch=monkeypatch)["calculo"]
    assert _por_id(memoria, "g1")["papel"] == "preservado"
    assert _por_id(memoria, "g1")["resultado"] == "5"
    novo = _por_id(memoria, "g2")
    assert novo["papel"] == "novo"
    assert novo["resultado"] == "3"
    assert novo["operacao"] == "contagem"
    assert memoria["proveniencia"]["graficos_novos"] == [{"id": "g2", "temporario": "t1"}]


def test_pedido_composto_calcula_todos_os_alvos(ctx, monkeypatch):
    base = _base(
        "execucao.v2.composto@test.com",
        "execucao-v2-composto",
        lambda modelo, colunas: [_soma(modelo, colunas, "s1"), _contagem(modelo, colunas, "s2")],
    )
    parte = base["colunas"]["parte"]
    categoria = base["colunas"]["categoria"]
    definicao = _compor(
        base,
        [
            _op(id="o1", acao="alterar_indicador", grafico="g1", operacao="media"),
            _criar("o2", "t1", parte, "soma", "barra"),
            _op(
                id="o3",
                acao="alterar_agrupamento",
                grafico=None,
                temporario="t1",
                coluna=_ref(categoria),
                dependencias=["o2"],
            ),
        ],
    )
    memoria = _chamar(base, definicao, ["g1", "g3"], monkeypatch=monkeypatch)["calculo"]
    assert memoria["alvos_obrigatorios"] == ["g1", "g3"]
    assert _por_id(memoria, "g1")["status"] == "calculado"
    assert _por_id(memoria, "g1")["resultado"] == _dividir("5", "3")
    assert _por_id(memoria, "g1")["papel"] == "alterado"
    assert _por_id(memoria, "g3")["status"] == "calculado"
    assert _por_id(memoria, "g3")["total"] == "5"
    assert _por_id(memoria, "g3")["papel"] == "novo"
    preservado = _por_id(memoria, "g2")
    assert preservado["papel"] == "preservado"
    assert preservado["resultado"] == "3"
    assert preservado["status"] == "calculado"
    assert [item["id"] for item in memoria["indicadores"] if item["id"] in {"g1", "g3"}] == ["g1", "g3"]


def test_razao_e_par_numerico(ctx, monkeypatch):
    base = _base(
        "execucao.v2.razao@test.com",
        "execucao-v2-razao",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    parte = base["colunas"]["parte"]
    todo = base["colunas"]["todo"]
    definicao = _compor(
        base,
        [
            _criar(
                "o1",
                "t1",
                parte,
                "razao",
                "indicador",
                segunda_coluna=_ref(todo),
            ),
            _criar(
                "o2",
                "t2",
                parte,
                "par_numerico",
                "dispersao",
                segunda_coluna=_ref(todo),
            ),
        ],
    )
    memoria = _chamar(base, definicao, ["g2", "g3"], monkeypatch=monkeypatch)["calculo"]
    razao = _por_id(memoria, "g2")
    assert razao["resultado"] == "0.5"
    assert razao["unidade"] == "adimensional"
    assert razao["elegiveis"] == 3
    assert razao["definicao_aplicada"]["metrica"]["operacao"] == "razao"
    assert [item["papel"] for item in razao["definicao_aplicada"]["dimensoes"]] == ["segunda_medida"]
    par = _por_id(memoria, "g3")
    assert par["resultado"] == "3"
    assert par["operacao"] == "par_numerico"
    assert par["elegiveis"] == 3
    assert "pares_completos" in par["avisos"]
    assert par["definicao_aplicada"]["grafico"] == "dispersao"


def test_filtro_com_parametro_autorizado(ctx, monkeypatch):
    base = _base(
        "execucao.v2.filtro@test.com",
        "execucao-v2-filtro",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(
        base,
        [
            _op(
                acao="definir_filtro",
                coluna=_ref(base["colunas"]["categoria"]),
                operador="igual",
                parametros=[{"forma": "tipado", "nome": "faixa", "tipo": "texto"}],
            )
        ],
    )
    resposta = _chamar(base, definicao, ["g1"], parametros={"faixa": "norte"}, monkeypatch=monkeypatch)
    memoria = resposta["calculo"]
    indicador = _por_id(memoria, "g1")
    assert indicador["resultado"] == "4"
    assert indicador["elegiveis"] == 2
    assert indicador["parametros_efetivos"] == [{"nome": "faixa", "tipo": "texto", "disponivel": True}]
    assert memoria["parametros_efetivos"] == [{"nome": "faixa", "tipo": "texto"}]
    assert resposta["entrada_efetiva"]["g1"] == [{"nome": "faixa", "tipo": "texto", "valor": "norte"}]
    assert "norte" not in json.dumps(indicador["filtros"])
    assert "norte" not in json.dumps(memoria["parametros_efetivos"])
    assert "norte" not in json.dumps(memoria)
    assert indicador["definicao_aplicada"]["filtros"][0]["parametro"] == "faixa"
    assert indicador["definicao_aplicada"]["filtros"][0]["operador"] == "igual"


def test_ausencia_de_parametro_obrigatorio_esclarece_sem_inventar(ctx, monkeypatch):
    base = _base(
        "execucao.v2.periodo@test.com",
        "execucao-v2-periodo",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )

    def proibido(*_args, **_kwargs):
        raise AssertionError("calculou")

    monkeypatch.setattr("app.services.agente_bi_calculo_service._calcular_um", proibido)
    definicao = _compor(
        base,
        [
            _op(
                acao="alterar_periodo",
                coluna=_ref(base["colunas"]["quando"]),
                operador="entre",
                parametros=[
                    {"forma": "tipado", "nome": "inicio", "tipo": "data"},
                    {"forma": "tipado", "nome": "fim", "tipo": "data"},
                ],
            )
        ],
    )
    resposta = _chamar(base, definicao, ["g1"], monkeypatch=monkeypatch)
    assert resposta["ok"] is False
    assert resposta["situacao"] == "esclarecimento"
    assert resposta["motivo"] == "informar_parametro"
    assert resposta["publicado"] is False
    assert resposta["persistido"] is False
    assert "calculo" not in resposta
    assert resposta["esclarecimentos"] == [
        {"id": "g1", "motivo": "informar_parametro", "parametro": "inicio"},
        {"id": "g1", "motivo": "informar_parametro", "parametro": "fim"},
    ]
    assert "2024" not in json.dumps(resposta)


def test_alvo_obrigatorio_bloqueado_nao_e_sucesso(ctx, monkeypatch):
    base = _base(
        "execucao.v2.bloqueado@test.com",
        "execucao-v2-bloqueado",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(
        base,
        [_criar("o1", "t1", base["colunas"]["parte"], "participacao", "indicador")],
    )
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, definicao, ["g2"], monkeypatch=monkeypatch)
    assert erro.value.codigo == "alvo_bloqueado"
    assert erro.value.status == 422


def test_alvo_obrigatorio_ausente(ctx, monkeypatch):
    base = _base(
        "execucao.v2.ausente@test.com",
        "execucao-v2-ausente",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(
        base,
        [_criar("o1", "t1", base["colunas"]["parte"], "contagem", "indicador")],
    )
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, definicao, [], monkeypatch=monkeypatch)
    assert erro.value.codigo == "alvo_ausente"

    from app.services.agente_bi_calculo_service import _calcular_um as real

    def troca(definicao_grafico, *args, **kwargs):
        indicador = real(definicao_grafico, *args, **kwargs)
        if definicao_grafico["id"] == "g2":
            indicador = dict(indicador)
            indicador["id"] = "g0"
            indicador["definicao_id"] = "g0"
        return indicador

    monkeypatch.setattr("app.services.agente_bi_calculo_service._calcular_um", troca)
    with pytest.raises(CalculoErro) as calculado:
        _chamar(base, definicao, ["g2"], monkeypatch=monkeypatch)
    assert calculado.value.codigo == "alvo_ausente"


def test_alvo_obrigatorio_substituido(ctx, monkeypatch):
    base = _base(
        "execucao.v2.substituido@test.com",
        "execucao-v2-substituido",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    parte = base["colunas"]["parte"]
    definicao = _compor(
        base,
        [
            _criar("o1", "t1", parte, "soma", "indicador"),
            _criar("o2", "t2", parte, "contagem", "indicador"),
        ],
    )

    from app.services.agente_bi_calculo_service import _calcular_um as real

    def troca(definicao_grafico, *args, **kwargs):
        indicador = real(definicao_grafico, *args, **kwargs)
        if definicao_grafico["id"] == "g2":
            indicador = dict(indicador)
            indicador["id"] = "g3"
            indicador["definicao_id"] = "g3"
        elif definicao_grafico["id"] == "g3":
            indicador = dict(indicador)
            indicador["id"] = "g2"
            indicador["definicao_id"] = "g2"
        return indicador

    monkeypatch.setattr("app.services.agente_bi_calculo_service._calcular_um", troca)
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, definicao, ["g2", "g3"], monkeypatch=monkeypatch)
    assert erro.value.codigo == "alvo_substituido"


def test_falha_do_ultimo_grafico_nao_publica_o_resto(ctx, monkeypatch):
    base = _base(
        "execucao.v2.ultimo@test.com",
        "execucao-v2-ultimo",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    parte = base["colunas"]["parte"]
    definicao = _compor(
        base,
        [
            _criar("o1", "t1", parte, "soma", "indicador"),
            _criar("o2", "t2", parte, "contagem", "indicador"),
            _criar("o3", "t3", parte, "participacao", "indicador"),
        ],
    )
    chamadas = []
    from app.services.agente_bi_calculo_service import _calcular_um as real

    def conta(definicao_grafico, *args, **kwargs):
        chamadas.append(definicao_grafico["id"])
        return real(definicao_grafico, *args, **kwargs)

    monkeypatch.setattr("app.services.agente_bi_calculo_service._calcular_um", conta)
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, definicao, ["g2", "g3", "g4"], monkeypatch=monkeypatch)
    assert erro.value.codigo == "alvo_bloqueado"
    assert chamadas == ["g1", "g2", "g3", "g4"]
    assert not hasattr(erro.value, "calculo")


def test_preservado_calculado_nao_regride(ctx, monkeypatch):
    base = _base(
        "execucao.v2.preservado@test.com",
        "execucao-v2-preservado",
        lambda modelo, colunas: [_soma(modelo, colunas, "s1"), _contagem(modelo, colunas, "s2")],
    )
    definicao = _compor(
        base,
        [_op(acao="alterar_indicador", grafico="g1", operacao="media")],
    )
    memoria = _chamar(base, definicao, ["g1"], monkeypatch=monkeypatch)["calculo"]
    assert _por_id(memoria, "g2")["resultado"] == "3"
    assert _por_id(memoria, "g2")["status"] == "calculado"
    assert _por_id(memoria, "g2")["papel"] == "preservado"
    assert _por_id(memoria, "g1")["resultado"] == _dividir("5", "3")

    from app.services.agente_bi_calculo_service import _calcular_um as real

    def regride(definicao_grafico, *args, **kwargs):
        indicador = real(definicao_grafico, *args, **kwargs)
        if definicao_grafico["id"] == "g2":
            indicador = dict(indicador)
            indicador["status"] = "bloqueado"
            indicador["motivo"] = "sem_registros_elegiveis"
            indicador["resultado"] = None
            indicador["total"] = None
            indicador["serie"] = []
        return indicador

    monkeypatch.setattr("app.services.agente_bi_calculo_service._calcular_um", regride)
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, definicao, ["g1"], monkeypatch=monkeypatch)
    assert erro.value.codigo == "preservado_inconsistente"


def test_preservado_bloqueado_permanece_quando_equivalente(ctx, monkeypatch):
    base = _base(
        "execucao.v2.bloqueio.anterior@test.com",
        "execucao-v2-bloqueio-anterior",
        lambda modelo, colunas: [_soma(modelo, colunas, "s1"), _participacao(modelo, colunas, "s2")],
    )
    anterior = next(item for item in base["memoria_v1"]["indicadores"] if item["id"] == "g2")
    assert anterior["status"] == "bloqueado"
    definicao = _compor(
        base,
        [_op(acao="alterar_indicador", grafico="g1", operacao="media")],
    )
    memoria = _chamar(base, definicao, ["g1"], monkeypatch=monkeypatch)["calculo"]
    preservado = _por_id(memoria, "g2")
    assert preservado["papel"] == "preservado"
    assert preservado["status"] == "bloqueado"
    assert preservado["motivo"] == anterior["motivo"] == "informar_parametro"
    assert preservado["operacao"] == anterior["operacao"]
    assert preservado["definicao_aplicada"] == definicao["definicoes"][1]
    assert _por_id(memoria, "g1")["status"] == "calculado"
    assert _por_id(memoria, "g1")["resultado"] == _dividir("5", "3")


def test_determinismo(ctx, monkeypatch):
    base = _base(
        "execucao.v2.determinismo@test.com",
        "execucao-v2-determinismo",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(
        base,
        [_op(acao="alterar_indicador", operacao="media")],
    )
    primeira = _chamar(base, definicao, ["g1"], monkeypatch=monkeypatch)
    segunda = _chamar(base, copy.deepcopy(definicao), ["g1"], monkeypatch=monkeypatch)
    assert primeira == segunda
    assert json.dumps(primeira, sort_keys=True) == json.dumps(segunda, sort_keys=True)


def test_compatibilidade_v1_e_ausencia_de_publicacao(ctx, monkeypatch):
    _usuario, _franquia, escopo, perfil, pacote, _aprovada = _jornada(
        "execucao.v2.compat@test.com",
        "execucao-v2-compat",
        _planilha(),
    )
    documento = definir(perfil, pacote)
    salva = _gravar(escopo, pacote["id"], documento)
    primeira = calcular_definicao(escopo, {"analise_id": salva["id"]})
    painel = montar_dashboard(escopo, {"analise_id": primeira["calculo"]["id"]})
    repetida = calcular_definicao(escopo, {"analise_id": salva["id"]})
    assert repetida["calculo"]["resultado"]["contrato"] == "calculo.v1"
    assert repetida["calculo"]["resultado"]["executor"] == EXECUTOR
    assert [
        (item["id"], item["status"], item["resultado"], item["total"], item["serie"])
        for item in repetida["calculo"]["resultado"]["indicadores"]
    ] == [
        (item["id"], item["status"], item["resultado"], item["total"], item["serie"])
        for item in primeira["calculo"]["resultado"]["indicadores"]
    ]
    tecnico = montar_contexto(perfil, {"modalidades": ["A"]})
    base = {
        "escopo": escopo,
        "perfil": perfil,
        "documento": documento,
        "definicao_id": salva["id"],
        "calculo_id": primeira["calculo"]["id"],
        "memoria_v1": primeira["calculo"]["resultado"],
        "revisao": painel["dashboard"]["revisao"],
        "dashboard_id": painel["dashboard"]["id"],
        "chave": salva["chave"],
        "dataset_id": salva["dataset_id"],
        "versao": salva["versao"],
        "impressao": salva["impressao"],
        "contexto": {chave: tecnico[chave] for chave in ("colunas", "regioes", "relacoes", "qualidade")},
        "salva": salva,
    }
    definicao = _compor(base, [_op(acao="consultar", grafico="g1")])
    assert "dataset_id" not in definicao
    assert "usuario_id" not in definicao
    assert "dashboard_id" not in definicao
    memoria = _chamar(base, definicao, [], monkeypatch=monkeypatch)["calculo"]
    assert memoria["origem_contrato"] == CONTRATO_DEFINIR_V2
    assert memoria["executor"] == EXECUTOR
    assert memoria["alvos_obrigatorios"] == []
    assert [
        (item["id"], item["status"], item["resultado"], item["total"], item["serie"])
        for item in memoria["indicadores"]
    ] == [
        (item["id"], item["status"], item["resultado"], item["total"], item["serie"])
        for item in primeira["calculo"]["resultado"]["indicadores"]
    ]
    relida = json.loads(db.session.get(AgenteBiAnalise, base["calculo_id"]).resultado_json)
    assert relida["indicadores"] == primeira["calculo"]["resultado"]["indicadores"]

    proibida = copy.deepcopy(definicao)
    proibida["dataset_id"] = base["dataset_id"]
    with pytest.raises(CalculoErro) as indevida:
        _chamar(base, proibida, [], monkeypatch=monkeypatch)
    assert indevida.value.codigo == "autorizacao_indevida"
    assert indevida.value.status == 403

    gravada = _persistir_v2(base, definicao)
    with pytest.raises(CalculoErro) as recusa:
        calcular_definicao(escopo, {"analise_id": gravada["id"]})
    assert recusa.value.codigo == "analise_indisponivel"
    assert "calcular_definicao_v2" not in FONTE_ROTAS
    assert "def calcular_definicao_v2" in FONTE_CALCULO
    assert "def calcular_definicao(" in FONTE_CALCULO


def _filtro(coluna, identificador="o1", grafico="g1"):
    return _op(
        id=identificador,
        acao="definir_filtro",
        grafico=grafico,
        coluna=_ref(coluna),
        operador="igual",
        parametros=[{"forma": "tipado", "nome": "faixa", "tipo": "texto"}],
    )


def test_apresentacao_com_definicao_adulterada_recusa(ctx, monkeypatch):
    base = _base(
        "execucao.v2.adulterada@test.com",
        "execucao-v2-adulterada",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_op(acao="alterar_apresentacao", apresentacao="tabela")])
    adulterada = copy.deepcopy(definicao)
    adulterada["definicoes"][0]["metrica"]["operacao"] = "media"
    _recusar_calculo(monkeypatch)
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, adulterada, ["g1"], monkeypatch=monkeypatch)
    assert erro.value.codigo == "proveniencia_inconsistente"


def test_filtro_do_plano_ausente_na_definicao_recusa(ctx, monkeypatch):
    base = _base(
        "execucao.v2.sem.filtro@test.com",
        "execucao-v2-sem-filtro",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_filtro(base["colunas"]["categoria"])])
    adulterada = copy.deepcopy(definicao)
    adulterada["definicoes"][0]["filtros"] = []
    adulterada["definicoes"][0]["parametros"] = []
    _recusar_calculo(monkeypatch)
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, adulterada, ["g1"], parametros={"faixa": "norte"}, monkeypatch=monkeypatch)
    assert erro.value.codigo == "proveniencia_inconsistente"


def test_plano_persistido_igual_a_composicao_calcula(ctx, monkeypatch):
    base = _base(
        "execucao.v2.igual@test.com",
        "execucao-v2-igual",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_op(acao="alterar_indicador", operacao="media")])
    vistos = []
    from app.services.agente_bi_calculo_service import _calcular_um as real

    def espia(grafico, *args, **kwargs):
        vistos.append(copy.deepcopy(grafico))
        return real(grafico, *args, **kwargs)

    monkeypatch.setattr("app.services.agente_bi_calculo_service._calcular_um", espia)
    resposta = _chamar(base, definicao, ["g1"], monkeypatch=monkeypatch)
    assert vistos == definicao["definicoes"]
    assert resposta["calculo"]["persistido"] is False
    assert resposta["calculo"]["publicado"] is False
    assert _por_id(resposta["calculo"], "g1")["resultado"] == _dividir("5", "3")
    assert resposta["entrada_efetiva"]["g1"] == []
    assert "entrada_efetiva" not in resposta["calculo"]


def test_plano_inexistente_recusa(ctx, monkeypatch):
    base = _base(
        "execucao.v2.ausente.plano@test.com",
        "execucao-v2-ausente-plano",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_op(acao="consultar")])
    _recusar_calculo(monkeypatch)
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, definicao, [], plano_id=9_999_999, monkeypatch=monkeypatch)
    assert erro.value.codigo == "nao_encontrado"
    assert erro.value.status == 404


def test_plano_de_outro_escopo_recusa(ctx, monkeypatch):
    base = _base(
        "execucao.v2.escopo@test.com",
        "execucao-v2-escopo",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_op(acao="consultar")])
    colega = seed_usuario(
        base["escopo"].franquia_id,
        base["escopo"].conta_id,
        email="execucao.v2.colega@test.com",
    )
    _conta, _franquia, outro = _par("execucao-v2-outra-conta", "execucao.v2.outra@test.com")
    _recusar_calculo(monkeypatch)
    for usuario, slug in ((colega, "execucao-v2-colega"), (outro, "execucao-v2-outra-conta")):
        alheia = _analise_fora(usuario, slug)
        with pytest.raises(CalculoErro) as erro:
            _chamar(base, definicao, [], plano_id=alheia["id"], monkeypatch=monkeypatch)
        assert erro.value.codigo == "nao_encontrado"
        assert erro.value.status == 404


def test_plano_incompleto_ou_em_esclarecimento_recusa(ctx, monkeypatch):
    base = _base(
        "execucao.v2.incompleto@test.com",
        "execucao-v2-incompleto",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_op(acao="consultar")])
    preparado = _persistir_plano(base, _plano([_op()]), estado="preparada")
    esclarecido = _plano([_op(esclarecimento={"motivo": "confirmar_rotulos"})])
    esclarecido["situacao"] = "esclarecimento"
    gravado = _persistir_plano(base, esclarecido)
    _recusar_calculo(monkeypatch)
    for plano_id in (preparado["id"], gravado["id"]):
        with pytest.raises(CalculoErro) as erro:
            _chamar(base, definicao, [], plano_id=plano_id, monkeypatch=monkeypatch)
        assert erro.value.codigo == "analise_indisponivel"


def test_plano_de_outra_definicao_ou_revisao_recusa(ctx, monkeypatch):
    base = _base(
        "execucao.v2.outra.base@test.com",
        "execucao-v2-outra-base",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_op(acao="alterar_apresentacao", apresentacao="tabela")])
    revisada = copy.deepcopy(definicao)
    revisada["revisao_base"] = definicao["revisao_base"] + 1
    _recusar_calculo(monkeypatch)
    with pytest.raises(CalculoErro) as revisao:
        _chamar(base, revisada, ["g1"], monkeypatch=monkeypatch)
    assert revisao.value.codigo == "proveniencia_inconsistente"

    outra = _persistir_v2(base, definicao)
    trocada = copy.deepcopy(definicao)
    trocada["definicao_anterior_id"] = outra["id"]
    with pytest.raises(CalculoErro) as anterior:
        _chamar(base, trocada, ["g1"], monkeypatch=monkeypatch)
    assert anterior.value.codigo == "proveniencia_inconsistente"


def test_plano_declarado_divergente_do_persistido_recusa(ctx, monkeypatch):
    base = _base(
        "execucao.v2.divergente@test.com",
        "execucao-v2-divergente",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_op(acao="alterar_apresentacao", apresentacao="tabela")])
    _recusar_calculo(monkeypatch)
    with pytest.raises(CalculoErro) as erro:
        _chamar(base, definicao, ["g1"], plano=_plano([_op(acao="consultar")]), monkeypatch=monkeypatch)
    assert erro.value.codigo == "proveniencia_inconsistente"


def test_ausencia_de_plano_confiavel_recusa(ctx, monkeypatch):
    base = _base(
        "execucao.v2.sem.plano@test.com",
        "execucao-v2-sem-plano",
        lambda modelo, colunas: [_soma(modelo, colunas)],
    )
    definicao = _compor(base, [_op(acao="consultar")])
    _recusar_calculo(monkeypatch)
    with pytest.raises(CalculoErro) as ausente:
        _chamar(base, definicao, [], sem_plano=True, monkeypatch=monkeypatch)
    assert ausente.value.codigo == "payload_invalido"
    with pytest.raises(CalculoErro) as definicao_comum:
        _chamar(base, definicao, [], plano_id=base["definicao_id"], monkeypatch=monkeypatch)
    assert definicao_comum.value.codigo == "contrato_saida_invalido"


def test_entrada_efetiva_por_grafico_separa_valor_da_memoria_publica(ctx, monkeypatch, caplog):
    base = _base(
        "execucao.v2.entrada@test.com",
        "execucao-v2-entrada",
        lambda modelo, colunas: [_soma(modelo, colunas, "s1"), _contagem(modelo, colunas, "s2")],
    )
    categoria = base["colunas"]["categoria"]
    definicao = _compor(base, [_filtro(categoria, "o1", "g1"), _filtro(categoria, "o2", "g2")])
    caplog.set_level(logging.INFO, logger="app.services.agente_bi_calculo_service")
    norte = _chamar(base, definicao, ["g1", "g2"], parametros={"faixa": "norte"}, monkeypatch=monkeypatch)
    assert _por_id(norte["calculo"], "g1")["resultado"] == "4"
    assert _por_id(norte["calculo"], "g2")["resultado"] == "2"
    assert norte["entrada_efetiva"]["g1"] == [{"nome": "faixa", "tipo": "texto", "valor": "norte"}]
    assert norte["entrada_efetiva"]["g2"] == [{"nome": "faixa", "tipo": "texto", "valor": "norte"}]
    assert norte["entrada_efetiva"]["g1"] is not norte["entrada_efetiva"]["g2"]
    assert not _revela(norte["calculo"], "norte")
    assert "norte" not in " ".join(
        rec.message for rec in caplog.records if rec.name.endswith("agente_bi_calculo_service")
    )
    sul = _chamar(base, definicao, ["g1", "g2"], parametros={"faixa": "sul"}, monkeypatch=monkeypatch)
    assert _por_id(sul["calculo"], "g1")["resultado"] == "1"
    assert _por_id(sul["calculo"], "g2")["resultado"] == "1"
    assert sul["entrada_efetiva"]["g1"] == [{"nome": "faixa", "tipo": "texto", "valor": "sul"}]
    assert sul["entrada_efetiva"]["g2"] == [{"nome": "faixa", "tipo": "texto", "valor": "sul"}]
    assert not _revela(sul["calculo"], "sul")
    assert "entrada_efetiva" not in sul["calculo"]


def _revela(memoria, valor) -> bool:
    if isinstance(memoria, dict):
        return any(_revela(item, valor) for item in memoria.values())
    if isinstance(memoria, list):
        return any(_revela(item, valor) for item in memoria)
    return memoria == valor


def _analise_fora(usuario, slug):
    ingerido = _ingerir(usuario, _planilha(), "movimento.xlsx", chave=f"lote-{slug}")
    dataset = ingerido["dataset"]
    return criar_analise(
        escopo_do_usuario(usuario),
        {
            "chave": f"plano-{slug}",
            "rodada": 1,
            "dataset_id": dataset["id"],
            "versao": dataset["versao"],
            "impressao": dataset["impressao"],
            "etapa": "definicao",
            "estado": "concluida",
            "contexto": {"contrato": "contexto.plano.v2"},
            "regras_negocio": {"contrato": CONTRATO_PATCH_V2},
            "esclarecimentos": {},
            "contrato_versao": CONTRATO_PATCH_V2,
            "resultado": {
                "contrato": CONTRATO_PATCH_V2,
                "versao": 2,
                "situacao": "proposto",
                "operacoes": [],
            },
        },
    )
