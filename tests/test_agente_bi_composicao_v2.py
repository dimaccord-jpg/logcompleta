"""SCRUM-269 etapa 3.2: compositor determinístico, sem execução e sem Anthropic."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.services.agente_bi_composicao_service import ComposicaoErro, compor_definicao
from app.services.agente_bi_contrato_v2_service import (
    CONTRATO_DEFINIR_V2,
    CONTRATO_PATCH_V2,
    reconhecer_historico,
    validar_definicao_evolutiva,
)
from app.services.agente_bi_definir_service import CONTRATO_DEFINIR, DSL_CONTRATO

FONTE = Path(__file__).resolve().parents[1] / "app" / "services" / "agente_bi_composicao_service.py"
REF1 = {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1}
REF2 = {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 2}
REF3 = {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 3}
REF4 = {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 4}
REF_OUTRA = {"arquivo": 0, "aba": 0, "regiao": 1, "indice": 1}


@pytest.fixture(autouse=True)
def _sem_anthropic(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)
    monkeypatch.setattr("app.services.agente_bi_evolucao_service.evoluir_dashboard", proibido)


def _coluna(indice, *, tipos, dimensoes, principal, distintos=3, presentes=3):
    return {
        "arquivo": 0,
        "aba": 0,
        "regiao": 0,
        "indice": indice,
        "tipos": tipos,
        "dimensoes": dimensoes,
        "dimensao_principal": principal,
        "presentes": presentes,
        "distintos": distintos,
    }


def _colunas():
    return [
        _coluna(1, tipos=["texto"], dimensoes=["categoria"], principal="categoria"),
        _coluna(2, tipos=["texto"], dimensoes=["categoria"], principal="categoria", distintos=4),
        _coluna(3, tipos=["data"], dimensoes=["data"], principal="data"),
        _coluna(4, tipos=["numero"], dimensoes=["valor"], principal="valor"),
    ]


def _contexto():
    return {
        "colunas": _colunas(),
        "regioes": [
            {"arquivo": 0, "aba": 0, "regiao": 0, "cabecalho": "inequivoca", "ambiguidades": []}
        ],
        "qualidade": [
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 0,
                "cabecalho_ambiguo": False,
                "formulas_sem_cache": 0,
                "datas_ambiguas": 0,
            }
        ],
        "relacoes": [],
    }


def _contexto_duas_regioes(*, relacao=False):
    contexto = _contexto()
    extra = _coluna(1, tipos=["texto"], dimensoes=["categoria"], principal="categoria")
    extra["regiao"] = 1
    contexto["colunas"].append(extra)
    contexto["regioes"].append({"arquivo": 0, "aba": 0, "regiao": 1, "cabecalho": "inequivoca", "ambiguidades": []})
    contexto["qualidade"].append(
        {
            "arquivo": 0,
            "aba": 0,
            "regiao": 1,
            "cabecalho_ambiguo": False,
            "formulas_sem_cache": 0,
            "datas_ambiguas": 0,
        }
    )
    if relacao:
        contexto["relacoes"] = [
            {
                "juncao": "nao_executada",
                "lado_unico": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1},
                "lado_associado": dict(REF_OUTRA),
            }
        ]
    return contexto


def _com_parametro(*, obrigatorio=False):
    item = _indicador()
    item["filtros"] = [{"coluna": dict(REF1), "operador": "igual", "parametro": "alvo", "parametro_fim": None}]
    item["parametros"] = [{"nome": "alvo", "tipo": "texto", "obrigatorio": obrigatorio}]
    return item


def _assinatura(grafico):
    return (
        json.dumps(grafico["metrica"], sort_keys=True),
        tuple(sorted(json.dumps(item, sort_keys=True) for item in grafico["filtros"])),
        tuple(sorted(json.dumps(item, sort_keys=True) for item in grafico["parametros"])),
    )


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


def _plano(operacoes, situacao="proposto"):
    return {"contrato": CONTRATO_PATCH_V2, "versao": 2, "situacao": situacao, "operacoes": operacoes}


def _indicador(identificador="g1", *, sugestao="s1", origem="dados"):
    return {
        "id": identificador,
        "sugestao_id": sugestao,
        "status": "definida",
        "grafico": "indicador",
        "leitura": "contagem_de_registros",
        "origem": origem,
        "metrica": {"operacao": "contagem", "coluna": dict(REF1), "unidade": "contagem"},
        "granularidade": "total",
        "dimensoes": [],
        "filtros": [],
        "parametros": [],
        "relacionamentos": [],
        "juncao": "nao_executada",
        "esclarecimento": None,
    }


def _barra(identificador="g1", *, agrupamento=1, sugestao="s1", origem="dados", operacao="soma"):
    return {
        "id": identificador,
        "sugestao_id": sugestao,
        "status": "definida",
        "grafico": "barra",
        "leitura": "medida_por_categoria",
        "origem": origem,
        "metrica": {"operacao": operacao, "coluna": dict(REF4), "unidade": "valor"},
        "granularidade": "categoria",
        "dimensoes": [
            {
                "papel": "agrupamento",
                "coluna": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": agrupamento},
                "granularidade": "categoria",
                "unidade": None,
            }
        ],
        "filtros": [],
        "parametros": [],
        "relacionamentos": [],
        "juncao": "nao_executada",
        "esclarecimento": None,
    }


def _dimensao(indice):
    return {
        "papel": "agrupamento",
        "coluna": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": indice},
        "granularidade": "categoria",
        "unidade": None,
    }


def _compor(plano, definicoes, *, vinculos=None, identidade=None, contexto=None, contrato=CONTRATO_DEFINIR):
    anterior = {"contrato": contrato, "analise_id": 41, "definicoes": definicoes}
    ident = identidade or {
        "definicao_anterior_id": 41,
        "revisao_base": 3,
        "origem_contrato": contrato,
    }
    return compor_definicao(
        plano,
        anterior,
        _contexto() if contexto is None else contexto,
        [] if vinculos is None else vinculos,
        ident,
    )


def _criar(identificador="o1", temporario="t1", **extras):
    base = {
        "id": identificador,
        "acao": "criar_grafico",
        "grafico": None,
        "temporario": temporario,
        "coluna": dict(REF4),
        "operacao": "soma",
        "apresentacao": "indicador",
    }
    base.update(extras)
    return _op(**base)


def _soma(identificador):
    return {
        "id": identificador,
        "sugestao_id": None,
        "status": "definida",
        "grafico": "indicador",
        "leitura": "indicador_de_medida",
        "origem": "dados",
        "metrica": {"operacao": "soma", "coluna": dict(REF4), "unidade": "valor"},
        "granularidade": "total",
        "dimensoes": [],
        "filtros": [],
        "parametros": [],
        "relacionamentos": [],
        "juncao": "nao_executada",
        "esclarecimento": None,
    }


def _vinculo(resultado, definicoes, contexto=None):
    return {
        "definicao_anterior_id": resultado["definicao_anterior_id"],
        "revisao_base": resultado["revisao_base"],
        "origem_contrato": resultado["origem_contrato"],
        "definicoes": [
            {"id": item["id"], "sugestao_id": item["sugestao_id"], "origem": item["origem"]} for item in definicoes
        ],
        "contexto": _contexto() if contexto is None else contexto,
        "plano": resultado["plano"],
    }


def test_inclusao_de_grafico_atribui_id_e_preserva_o_anterior():
    anterior = _indicador(origem="referencia")
    congelado = copy.deepcopy(anterior)
    saida = _compor(_plano([_criar()]), [anterior])

    assert saida["contrato"] == CONTRATO_DEFINIR_V2
    assert saida["versao"] == 2
    assert saida["dsl"] == DSL_CONTRATO
    assert saida["execucao"] == "nao_executada"
    assert saida["definicao_anterior_id"] == 41
    assert saida["revisao_base"] == 3
    assert saida["origem_contrato"] == CONTRATO_DEFINIR
    assert saida["definicoes"][0] == congelado
    assert saida["definicoes"][1] == _soma("g2")
    assert saida["proveniencia"] == {
        "definicao_anterior_id": 41,
        "revisao_base": 3,
        "origem_contrato": CONTRATO_DEFINIR,
        "plano_contrato": CONTRATO_PATCH_V2,
        "plano_versao": 2,
        "ids_preservados": ["g1"],
        "graficos_novos": [{"id": "g2", "temporario": "t1"}],
    }
    assert anterior == congelado
    assert "situacao" not in saida


def test_alteracao_de_indicador_nao_troca_medida_nem_apresentacao():
    anterior = _barra()
    congelado = copy.deepcopy(anterior)
    saida = _compor(_plano([_op(acao="alterar_indicador", operacao="media")]), [anterior])

    assert saida["definicoes"][0]["grafico"] == "barra"
    assert saida["definicoes"][0]["leitura"] == "medida_por_categoria"
    assert saida["definicoes"][0]["granularidade"] == "categoria"
    assert saida["definicoes"][0]["dimensoes"] == [_dimensao(1)]
    assert saida["definicoes"][0]["metrica"] == {
        "operacao": "media",
        "coluna": dict(REF4),
        "unidade": "valor",
    }
    assert saida["definicoes"][0]["sugestao_id"] == "s1"
    assert anterior == congelado

    recusa = _compor(
        _plano([_op(acao="alterar_indicador", operacao="media", coluna=dict(REF1))]),
        [_barra()],
    )
    assert recusa["situacao"] == "esclarecimento"
    assert recusa["motivo"] == "operacao_incompativel"
    assert recusa["operacao"] == "o1"
    assert "definicoes" not in recusa


def test_alteracao_de_agrupamento_usa_somente_a_coluna_pedida():
    saida = _compor(_plano([_op(acao="alterar_agrupamento", coluna=dict(REF2))]), [_barra()])

    assert saida["definicoes"][0]["grafico"] == "barra"
    assert saida["definicoes"][0]["metrica"]["operacao"] == "soma"
    assert saida["definicoes"][0]["metrica"]["coluna"] == REF4
    assert saida["definicoes"][0]["dimensoes"] == [_dimensao(2)]
    assert saida["definicoes"][0]["leitura"] == "medida_por_categoria"

    data = _compor(_plano([_op(acao="alterar_agrupamento", coluna=dict(REF3))]), [_barra()])
    assert data["situacao"] == "esclarecimento"
    assert data["motivo"] == "granularidade_incompativel"
    assert "definicoes" not in data

    mesma = _compor(_plano([_op(acao="alterar_agrupamento", coluna=dict(REF4))]), [_barra()])
    assert mesma["motivo"] == "grafico_incompativel"
    assert "definicoes" not in mesma


def test_alteracao_de_metrica_e_apresentacao_no_mesmo_grafico():
    saida = _compor(
        _plano(
            [
                _op(id="o1", acao="alterar_indicador", operacao="media"),
                _op(id="o2", acao="alterar_apresentacao", apresentacao="pizza"),
            ]
        ),
        [_barra()],
    )

    grafico = saida["definicoes"][0]
    assert grafico["grafico"] == "pizza"
    assert grafico["leitura"] == "medida_por_categoria"
    assert grafico["granularidade"] == "categoria"
    assert grafico["dimensoes"] == [_dimensao(1)]
    assert grafico["metrica"] == {"operacao": "media", "coluna": dict(REF4), "unidade": "valor"}
    assert grafico["filtros"] == []
    assert len(saida["definicoes"]) == 1


def test_filtro_autorizado_em_grafico_existente_e_em_grafico_novo():
    saida = _compor(
        _plano(
            [
                _op(
                    acao="definir_filtro",
                    coluna=dict(REF1),
                    operador="igual",
                    parametros=[{"forma": "tipado", "nome": "alvo", "tipo": "texto"}],
                )
            ]
        ),
        [_indicador()],
    )
    assert saida["definicoes"][0]["filtros"] == [
        {"coluna": dict(REF1), "operador": "igual", "parametro": "alvo", "parametro_fim": None}
    ]
    assert saida["definicoes"][0]["parametros"] == [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}]
    assert saida["definicoes"][0]["metrica"]["operacao"] == "contagem"
    assert saida["definicoes"][0]["grafico"] == "indicador"

    criado = _compor(
        _plano(
            [
                _op(
                    id="o2",
                    acao="definir_filtro",
                    grafico=None,
                    temporario="t1",
                    coluna=dict(REF1),
                    operador="igual",
                    parametros=[{"forma": "tipado", "nome": "alvo", "tipo": "texto"}],
                    dependencias=["o1"],
                ),
                _criar(),
            ]
        ),
        [_indicador()],
    )
    assert criado["definicoes"][0]["filtros"] == []
    assert criado["definicoes"][1]["id"] == "g2"
    assert criado["definicoes"][1]["filtros"] == [
        {"coluna": dict(REF1), "operador": "igual", "parametro": "alvo", "parametro_fim": None}
    ]
    assert criado["definicoes"][1]["parametros"] == [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}]
    assert criado["definicoes"][1]["metrica"]["operacao"] == "soma"

    barra = _barra()
    barra["filtros"] = [{"coluna": dict(REF1), "operador": "igual", "parametro": "alvo", "parametro_fim": None}]
    barra["parametros"] = [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}]
    opaco = _compor(
        _plano(
            [
                _op(
                    acao="definir_filtro",
                    coluna=dict(REF2),
                    operador="igual",
                    parametros=[{"forma": "opaca", "referencia": "p1"}],
                )
            ]
        ),
        [barra],
        vinculos=[{"referencia": "p1", "grafico": "g1", "nome": "alvo", "tipo": "texto"}],
    )
    assert opaco["definicoes"][0]["filtros"] == [
        {"coluna": dict(REF1), "operador": "igual", "parametro": "alvo", "parametro_fim": None},
        {"coluna": dict(REF2), "operador": "igual", "parametro": "alvo", "parametro_fim": None},
    ]
    assert opaco["definicoes"][0]["parametros"] == [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}]

    outro = _barra("g2", sugestao="s2")
    outro["filtros"] = [{"coluna": dict(REF1), "operador": "igual", "parametro": "alvo", "parametro_fim": None}]
    outro["parametros"] = [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}]
    cruzado = _compor(
        _plano(
            [
                _op(
                    acao="definir_filtro",
                    grafico="g1",
                    coluna=dict(REF2),
                    operador="igual",
                    parametros=[{"forma": "opaca", "referencia": "p1"}],
                )
            ]
        ),
        [_barra(), outro],
        vinculos=[{"referencia": "p1", "grafico": "g2", "nome": "alvo", "tipo": "texto"}],
    )
    assert cruzado["situacao"] == "esclarecimento"
    assert cruzado["motivo"] == "parametro_inconsistente"
    assert cruzado["operacao"] == "o1"
    assert "definicoes" not in cruzado

    ausente = _compor(
        _plano(
            [
                _op(
                    acao="definir_filtro",
                    coluna=dict(REF1),
                    operador="igual",
                    parametros=[{"forma": "opaca", "referencia": "p9"}],
                )
            ]
        ),
        [_indicador()],
    )
    assert ausente["motivo"] == "referencia_inexistente"
    assert "definicoes" not in ausente


def test_periodo_autorizado_grava_os_dois_limites():
    saida = _compor(
        _plano(
            [
                _op(
                    acao="alterar_periodo",
                    coluna=dict(REF3),
                    operador="entre",
                    parametros=[
                        {"forma": "tipado", "nome": "inicio", "tipo": "data"},
                        {"forma": "tipado", "nome": "fim", "tipo": "data"},
                    ],
                )
            ]
        ),
        [_indicador()],
    )

    assert saida["definicoes"][0]["filtros"] == [
        {"coluna": dict(REF3), "operador": "entre", "parametro": "inicio", "parametro_fim": "fim"}
    ]
    assert saida["definicoes"][0]["parametros"] == [
        {"nome": "inicio", "tipo": "data", "obrigatorio": True},
        {"nome": "fim", "tipo": "data", "obrigatorio": True},
    ]
    assert saida["definicoes"][0]["grafico"] == "indicador"
    assert saida["definicoes"][0]["metrica"]["coluna"] == REF1
    assert saida["proveniencia"]["graficos_novos"] == []


def test_dependencias_fora_da_ordem_textual_compoem_o_estado_final():
    anterior = _indicador()
    congelado = copy.deepcopy(anterior)
    saida = _compor(
        _plano(
            [
                _op(
                    id="o2",
                    acao="alterar_agrupamento",
                    grafico=None,
                    temporario="t1",
                    coluna=dict(REF1),
                    dependencias=["o1"],
                ),
                _op(
                    id="o3",
                    acao="alterar_apresentacao",
                    grafico=None,
                    temporario="t1",
                    apresentacao="barra",
                    dependencias=["o2"],
                ),
                _criar(apresentacao="indicador"),
            ]
        ),
        [anterior],
    )

    assert saida["definicoes"][0] == congelado
    assert saida["definicoes"][1] == {
        "id": "g2",
        "sugestao_id": None,
        "status": "definida",
        "grafico": "barra",
        "leitura": "medida_por_categoria",
        "origem": "dados",
        "metrica": {"operacao": "soma", "coluna": dict(REF4), "unidade": "valor"},
        "granularidade": "categoria",
        "dimensoes": [_dimensao(1)],
        "filtros": [],
        "parametros": [],
        "relacionamentos": [],
        "juncao": "nao_executada",
        "esclarecimento": None,
    }
    assert saida["proveniencia"]["graficos_novos"] == [{"id": "g2", "temporario": "t1"}]


def test_referencias_temporarias_recebem_id_estavel_e_recusam_ausente():
    primeiro = _indicador()
    quarto = _indicador("g4", sugestao="s4")
    intervalo = _compor(
        _plano(
            [
                _criar(),
                _op(id="o2", acao="consultar", grafico=None, temporario="t1", dependencias=["o1"]),
            ]
        ),
        [primeiro, quarto],
    )
    assert [item["id"] for item in intervalo["definicoes"]] == ["g1", "g4", "g2"]
    assert intervalo["definicoes"][0] == primeiro
    assert intervalo["definicoes"][1] == quarto
    assert intervalo["definicoes"][2] == _soma("g2")
    assert intervalo["proveniencia"]["ids_preservados"] == ["g1", "g4"]
    assert intervalo["proveniencia"]["graficos_novos"] == [{"id": "g2", "temporario": "t1"}]

    textual = _compor(
        _plano(
            [
                _criar("o2", "t2", dependencias=["o1"]),
                _criar("o1", "t1"),
            ]
        ),
        [_indicador()],
    )
    assert [item["id"] for item in textual["definicoes"]] == ["g1", "g2", "g3"]
    assert textual["definicoes"][1]["metrica"]["operacao"] == "soma"
    assert textual["definicoes"][2]["metrica"]["operacao"] == "soma"
    assert textual["proveniencia"]["graficos_novos"] == [
        {"id": "g2", "temporario": "t2"},
        {"id": "g3", "temporario": "t1"},
    ]

    ausente = _compor(
        _plano([_op(acao="consultar", grafico=None, temporario="t9")]),
        [_indicador()],
    )
    assert ausente == {
        "situacao": "esclarecimento",
        "motivo": "referencia_temporaria_invalida",
        "operacao": None,
        "esclarecimentos": [{"operacao": None, "motivo": "referencia_temporaria_invalida"}],
    }


def test_preserva_grafico_nao_citado_e_altera_dois_alvos_na_mesma_solicitacao():
    primeiro = _barra()
    segundo = _barra("g2", sugestao="s2")
    segundo["filtros"] = [{"coluna": dict(REF2), "operador": "igual", "parametro": "alvo", "parametro_fim": None}]
    segundo["parametros"] = [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}]
    congelado = copy.deepcopy(segundo)
    saida = _compor(_plano([_op(acao="alterar_indicador", operacao="media")]), [primeiro, segundo])

    assert saida["definicoes"][0]["metrica"]["operacao"] == "media"
    assert saida["definicoes"][1] == congelado
    assert saida["proveniencia"]["ids_preservados"] == ["g1", "g2"]
    assert saida["proveniencia"]["graficos_novos"] == []

    ambos = _compor(
        _plano(
            [
                _op(id="o1", acao="alterar_apresentacao", grafico="g1", apresentacao="tabela"),
                _op(id="o2", acao="alterar_indicador", grafico="g2", operacao="media"),
            ]
        ),
        [_barra(), _barra("g2", sugestao="s2")],
    )
    assert ambos["definicoes"][0]["grafico"] == "tabela"
    assert ambos["definicoes"][0]["leitura"] == "tabela_agregada"
    assert ambos["definicoes"][0]["metrica"]["operacao"] == "soma"
    assert ambos["definicoes"][1]["grafico"] == "barra"
    assert ambos["definicoes"][1]["metrica"]["operacao"] == "media"
    assert len(ambos["definicoes"]) == 2


def test_seis_graficos_sao_aceitos_e_o_setimo_e_recusado():
    criacoes = [_criar(f"o{indice}", f"t{indice}") for indice in range(1, 6)]
    saida = _compor(_plano(criacoes), [_indicador()])

    assert [item["id"] for item in saida["definicoes"]] == ["g1", "g2", "g3", "g4", "g5", "g6"]
    assert saida["definicoes"][0]["metrica"]["operacao"] == "contagem"
    assert saida["definicoes"][1:] == [_soma(f"g{indice}") for indice in range(2, 7)]
    assert saida["proveniencia"]["graficos_novos"] == [
        {"id": f"g{indice + 1}", "temporario": f"t{indice}"} for indice in range(1, 6)
    ]

    excesso = _compor(_plano([_criar(f"o{indice}", f"t{indice}") for indice in range(1, 7)]), [_indicador()])
    assert excesso["situacao"] == "esclarecimento"
    assert excesso["motivo"] == "limite_graficos"
    assert "definicoes" not in excesso
    assert "g7" not in json.dumps(excesso)


def test_conflito_de_escrita_no_mesmo_campo_nao_aplica_nenhuma():
    anterior = _barra()
    congelado = copy.deepcopy(anterior)
    plano = _plano(
        [
            _op(id="o1", acao="alterar_indicador", operacao="media"),
            _op(id="o2", acao="alterar_indicador", operacao="minimo"),
        ]
    )
    saida = _compor(plano, [anterior])

    assert saida == {
        "situacao": "esclarecimento",
        "motivo": "conflito_escrita",
        "operacao": "o2",
        "esclarecimentos": [
            {"operacao": "o1", "motivo": "conflito_escrita"},
            {"operacao": "o2", "motivo": "conflito_escrita"},
        ],
    }
    assert anterior == congelado
    assert "media" not in json.dumps(saida)
    assert "minimo" not in json.dumps(saida)

    filtros = _compor(
        _plano(
            [
                _op(
                    id="o1",
                    acao="definir_filtro",
                    coluna=dict(REF1),
                    operador="igual",
                    parametros=[{"forma": "tipado", "nome": "alvo", "tipo": "texto"}],
                ),
                _op(
                    id="o2",
                    acao="definir_filtro",
                    coluna=dict(REF1),
                    operador="diferente",
                    parametros=[{"forma": "tipado", "nome": "outro", "tipo": "texto"}],
                ),
            ]
        ),
        [_indicador()],
    )
    assert filtros["motivo"] == "conflito_escrita"
    assert filtros["operacao"] == "o2"
    assert "definicoes" not in filtros


def test_operacao_final_invalida_nao_devolve_composicao_parcial():
    anterior = _barra()
    congelado = copy.deepcopy(anterior)
    saida = _compor(
        _plano(
            [
                _op(id="o1", acao="alterar_indicador", operacao="media"),
                _op(id="o2", acao="alterar_apresentacao", apresentacao="linha"),
            ]
        ),
        [anterior],
    )

    assert saida["situacao"] == "esclarecimento"
    assert saida["motivo"] == "grafico_incompativel"
    assert saida["operacao"] == "o2"
    assert "definicoes" not in saida
    assert "media" not in json.dumps(saida)
    assert anterior == congelado

    plano = _plano(
        [_op(esclarecimento={"motivo": "confirmar_rotulos"})],
        situacao="esclarecimento",
    )
    esclarecido = _compor(plano, [_indicador()])
    assert esclarecido["situacao"] == "esclarecimento"
    assert esclarecido["motivo"] == "confirmar_rotulos"
    assert esclarecido["operacao"] == "o1"
    assert "definicoes" not in esclarecido


def test_composicao_pura_e_idempotente_e_nao_altera_as_entradas():
    plano = _plano([_op(acao="alterar_indicador", operacao="media")])
    definicoes = [_barra()]
    contexto = _contexto()
    identidade = {"definicao_anterior_id": 41, "revisao_base": 3, "origem_contrato": CONTRATO_DEFINIR}
    anterior = {"contrato": CONTRATO_DEFINIR, "analise_id": 41, "definicoes": definicoes}
    antes = copy.deepcopy((plano, anterior, contexto, identidade))

    primeira = compor_definicao(plano, anterior, contexto, [], identidade)
    segunda = compor_definicao(plano, anterior, contexto, [], identidade)

    assert primeira == segunda
    assert primeira["definicoes"][0]["metrica"]["operacao"] == "media"
    assert (plano, anterior, contexto, identidade) == antes
    assert definicoes[0]["metrica"]["operacao"] == "soma"


def test_validacao_final_da_dsl_repete_o_documento_canonico():
    contexto = _contexto()
    contexto["colunas"][0]["nome"] = "cabecalho-secreto"
    definicoes = [_barra()]
    saida = _compor(
        _plano([_op(acao="alterar_indicador", operacao="media")]),
        definicoes,
        contexto=contexto,
    )

    assert saida == validar_definicao_evolutiva(saida, _vinculo(saida, definicoes, contexto))
    assert saida["dsl"] == DSL_CONTRATO
    assert saida["definicoes"][0]["leitura"] == "medida_por_categoria"
    assert saida["definicoes"][0]["granularidade"] == "categoria"
    assert saida["definicoes"][0]["metrica"]["unidade"] == "valor"
    assert saida["definicoes"][0]["status"] == "definida"
    assert saida["definicoes"][0]["esclarecimento"] is None
    bruto = json.dumps(saida, ensure_ascii=False)
    assert "cabecalho-secreto" not in bruto
    assert "usuario_id" not in bruto
    assert "dataset_id" not in bruto

    bloqueada = _compor(_plano([_op(acao="alterar_indicador", operacao="count_if")]), [_indicador()])
    assert bloqueada["definicoes"][0]["status"] == "bloqueada"
    assert bloqueada["definicoes"][0]["esclarecimento"] == "informar_parametro"
    assert bloqueada["definicoes"][0]["metrica"] == {
        "operacao": "count_if",
        "coluna": dict(REF1),
        "unidade": "contagem",
    }
    assert bloqueada["definicoes"][0]["filtros"] == []
    assert bloqueada["definicoes"][0]["parametros"] == []
    assert bloqueada == validar_definicao_evolutiva(bloqueada, _vinculo(bloqueada, [_indicador()]))


def test_compatibilidade_com_contratos_v1_nao_converte_o_historico():
    anterior = _indicador(origem="referencia")
    documento = {
        "contrato": CONTRATO_DEFINIR,
        "analise_id": 41,
        "definicoes": [anterior],
        "dataset": {"id": 9, "versao": 1, "impressao": "impressao-interna"},
        "vinculo_preparacao": "preparacao-interna",
    }
    congelado = copy.deepcopy(documento)
    saida = compor_definicao(
        _plano([_criar()]),
        documento,
        _contexto(),
        [],
        {"definicao_anterior_id": 41, "revisao_base": 3, "origem_contrato": CONTRATO_DEFINIR},
    )

    assert saida["contrato"] == CONTRATO_DEFINIR_V2
    assert saida["origem_contrato"] == CONTRATO_DEFINIR
    assert saida["definicoes"][0]["origem"] == "referencia"
    assert saida["definicoes"][1]["origem"] == "dados"
    assert documento == congelado
    assert reconhecer_historico(documento)["contrato"] == CONTRATO_DEFINIR
    bruto = json.dumps(saida, ensure_ascii=False)
    assert "impressao-interna" not in bruto
    assert "preparacao-interna" not in bruto
    assert "dataset" not in saida

    v2 = {
        "contrato": CONTRATO_DEFINIR_V2,
        "analise_id": 50,
        "definicoes": [_indicador()],
        "definicao_anterior_id": 41,
        "revisao_base": 2,
        "origem_contrato": CONTRATO_DEFINIR,
    }
    evoluido = compor_definicao(
        _plano([_op()]),
        v2,
        _contexto(),
        [],
        {"definicao_anterior_id": 50, "revisao_base": 9, "origem_contrato": CONTRATO_DEFINIR_V2},
    )
    assert evoluido["definicao_anterior_id"] == 50
    assert evoluido["revisao_base"] == 9
    assert evoluido["origem_contrato"] == CONTRATO_DEFINIR_V2
    assert evoluido["definicoes"] == [_indicador()]
    assert v2["revisao_base"] == 2

    with pytest.raises(ComposicaoErro) as erro:
        compor_definicao(
            _plano([_op()]),
            {"contrato": CONTRATO_DEFINIR, "analise_id": 41, "definicoes": [_indicador()], "usuario_id": 7},
            _contexto(),
            [],
            {"definicao_anterior_id": 41, "revisao_base": 3, "origem_contrato": CONTRATO_DEFINIR},
        )
    assert erro.value.codigo == "autorizacao_indevida"
    assert erro.value.status == 403

    fonte = FONTE.read_text(encoding="utf-8")
    assert "import re" not in fonte
    assert "except Exception" not in fonte
    assert "except:" not in fonte
    assert "agente_bi_evolucao_service" not in fonte
    assert "agente_bi_calculo_service" not in fonte
    assert "agente_bi_dashboard_service" not in fonte
    assert "chamar_anthropic" not in fonte
    assert "montar_dashboard" not in fonte
    assert "calcular_definicao" not in fonte


def test_definicao_anterior_invalida_nao_e_sobrescrita():
    executada = _indicador()
    executada["status"] = "executada"
    sem_alteracao = _compor(_plano([_op(acao="consultar")]), [executada])
    assert sem_alteracao["situacao"] == "esclarecimento"
    assert sem_alteracao["motivo"] == "proveniencia_inconsistente"
    assert "definicoes" not in sem_alteracao

    com_alteracao = _compor(
        _plano([_op(acao="alterar_apresentacao", apresentacao="tabela")]),
        [copy.deepcopy(executada)],
    )
    assert com_alteracao["situacao"] == "esclarecimento"
    assert com_alteracao["motivo"] == "proveniencia_inconsistente"
    assert "definicoes" not in com_alteracao
    assert "tabela" not in json.dumps(com_alteracao)

    leitura = _indicador()
    leitura["leitura"] = "invalida"
    apresentacao = _compor(
        _plano([_op(acao="alterar_apresentacao", apresentacao="tabela")]),
        [leitura],
    )
    assert apresentacao["situacao"] == "esclarecimento"
    assert apresentacao["motivo"] == "proveniencia_inconsistente"
    assert "definicoes" not in apresentacao

    indice = _indicador()
    indice["metrica"] = {"operacao": "contagem", "coluna": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 99}, "unidade": "contagem"}
    substituida = _compor(
        _plano([_op(acao="alterar_indicador", operacao="soma", coluna=dict(REF4))]),
        [indice],
    )
    assert substituida["situacao"] == "esclarecimento"
    assert substituida["motivo"] == "proveniencia_inconsistente"
    assert "definicoes" not in substituida
    assert "soma" not in json.dumps(substituida)

    sem_metrica = _indicador()
    sem_metrica["metrica"] = None
    metrica = _compor(_plano([_op(acao="alterar_indicador", operacao="soma", coluna=dict(REF4))]), [sem_metrica])
    assert metrica == {
        "situacao": "esclarecimento",
        "motivo": "proveniencia_inconsistente",
        "operacao": None,
        "esclarecimentos": [{"operacao": None, "motivo": "proveniencia_inconsistente"}],
    }

    sem_dimensoes = _barra()
    sem_dimensoes["dimensoes"] = None
    dimensoes = _compor(_plano([_op(acao="alterar_apresentacao", apresentacao="pizza")]), [sem_dimensoes])
    assert dimensoes["situacao"] == "esclarecimento"
    assert dimensoes["motivo"] == "proveniencia_inconsistente"
    assert "definicoes" not in dimensoes
    assert isinstance(dimensoes, dict)

    divergente = _contexto()
    divergente["colunas"][3]["dimensao_principal"] = "quantidade"
    divergente["colunas"][3]["dimensoes"] = ["quantidade"]
    contexto = _compor(
        _plano([_op(acao="alterar_indicador", operacao="contagem")]),
        [_barra()],
        contexto=divergente,
    )
    assert contexto["situacao"] == "esclarecimento"
    assert contexto["motivo"] == "proveniencia_inconsistente"
    assert "definicoes" not in contexto

    valido = _indicador("g2", sugestao="s2")
    parcial = _compor(
        _plano([_op(acao="alterar_apresentacao", grafico="g2", apresentacao="tabela")]),
        [sem_metrica, valido],
    )
    assert parcial["motivo"] == "proveniencia_inconsistente"
    assert "definicoes" not in parcial


def test_reutilizacao_de_parametro_opaco_independe_da_ordem():
    vinculo = [{"referencia": "p1", "grafico": "g1", "nome": "alvo", "tipo": "texto"}]
    trocar = _op(
        id="o1",
        acao="definir_filtro",
        coluna=dict(REF1),
        operador="igual",
        parametros=[{"forma": "tipado", "nome": "novo", "tipo": "texto"}],
    )
    reusar = _op(
        id="o2",
        acao="definir_filtro",
        coluna=dict(REF2),
        operador="igual",
        parametros=[{"forma": "opaca", "referencia": "p1"}],
    )
    anterior = _com_parametro(obrigatorio=False)
    congelado = copy.deepcopy(anterior)
    direta = _compor(_plano([trocar, reusar]), [anterior], vinculos=vinculo)
    inversa = _compor(_plano([reusar, trocar]), [copy.deepcopy(congelado)], vinculos=vinculo)

    assert "situacao" not in direta
    assert "situacao" not in inversa
    assert _assinatura(direta["definicoes"][0]) == _assinatura(inversa["definicoes"][0])
    assert direta["definicoes"][0]["parametros"] == [
        {"nome": "novo", "tipo": "texto", "obrigatorio": True},
        {"nome": "alvo", "tipo": "texto", "obrigatorio": False},
    ]
    assert {item["nome"]: item["obrigatorio"] for item in inversa["definicoes"][0]["parametros"]} == {
        "novo": True,
        "alvo": False,
    }
    assert direta == validar_definicao_evolutiva(direta, _vinculo(direta, [congelado]))
    assert inversa == validar_definicao_evolutiva(inversa, _vinculo(inversa, [congelado]))
    assert anterior == congelado

    outro = _barra("g2", sugestao="s2")
    outro["filtros"] = [{"coluna": dict(REF2), "operador": "igual", "parametro": "alvo", "parametro_fim": None}]
    outro["parametros"] = [{"nome": "alvo", "tipo": "texto", "obrigatorio": True}]
    alheio = _compor(
        _plano(
            [
                _op(
                    acao="definir_filtro",
                    grafico="g1",
                    coluna=dict(REF1),
                    operador="igual",
                    parametros=[{"forma": "opaca", "referencia": "p1"}],
                )
            ]
        ),
        [_com_parametro(), outro],
        vinculos=[{"referencia": "p1", "grafico": "g2", "nome": "alvo", "tipo": "texto"}],
    )
    assert alheio["situacao"] == "esclarecimento"
    assert alheio["motivo"] == "parametro_inconsistente"
    assert "definicoes" not in alheio

    ausente = _compor(
        _plano(
            [
                _op(
                    acao="definir_filtro",
                    coluna=dict(REF2),
                    operador="igual",
                    parametros=[{"forma": "opaca", "referencia": "p9"}],
                )
            ]
        ),
        [_com_parametro()],
        vinculos=vinculo,
    )
    assert ausente["motivo"] == "referencia_inexistente"
    assert "definicoes" not in ausente

    incompativel = _compor(
        _plano(
            [
                _op(
                    acao="definir_filtro",
                    coluna=dict(REF4),
                    operador="igual",
                    parametros=[{"forma": "opaca", "referencia": "p1"}],
                )
            ]
        ),
        [_com_parametro()],
        vinculos=vinculo,
    )
    assert incompativel["motivo"] == "parametro_inconsistente"
    assert "definicoes" not in incompativel

    criado = _compor(
        _plano(
            [
                _criar(),
                _op(
                    id="o2",
                    acao="definir_filtro",
                    grafico=None,
                    temporario="t1",
                    coluna=dict(REF1),
                    operador="igual",
                    parametros=[{"forma": "opaca", "referencia": "p1"}],
                    dependencias=["o1"],
                ),
            ]
        ),
        [_com_parametro()],
        vinculos=vinculo,
    )
    assert criado["situacao"] == "esclarecimento"
    assert criado["motivo"] == "parametro_inconsistente"
    assert "definicoes" not in criado

    inventado = _compor(
        _plano([_op(acao="consultar")]),
        [_com_parametro()],
        vinculos=[{"referencia": "p1", "grafico": "g1", "nome": "fantasma", "tipo": "texto"}],
    )
    assert inventado["motivo"] == "parametro_inconsistente"
    assert "definicoes" not in inventado


def test_compatibilidade_de_filtro_usa_a_medida_final():
    contexto = _contexto_duas_regioes()
    filtrar = _op(
        id="o1",
        acao="definir_filtro",
        coluna=dict(REF_OUTRA),
        operador="igual",
        parametros=[{"forma": "tipado", "nome": "alvo", "tipo": "texto"}],
    )
    medir = _op(id="o2", acao="alterar_indicador", operacao="contagem", coluna=dict(REF_OUTRA))
    anterior = _indicador()
    congelado = copy.deepcopy(anterior)
    direta = _compor(_plano([filtrar, medir]), [anterior], contexto=contexto)
    inversa = _compor(_plano([medir, filtrar]), [copy.deepcopy(congelado)], contexto=contexto)

    assert "situacao" not in direta
    assert direta["definicoes"][0]["metrica"]["coluna"] == REF_OUTRA
    assert direta["definicoes"][0]["filtros"] == [
        {"coluna": dict(REF_OUTRA), "operador": "igual", "parametro": "alvo", "parametro_fim": None}
    ]
    assert direta["definicoes"][0]["relacionamentos"] == []
    assert direta["definicoes"][0]["juncao"] == "nao_executada"
    assert _assinatura(direta["definicoes"][0]) == _assinatura(inversa["definicoes"][0])
    assert direta["definicoes"] == inversa["definicoes"]
    assert direta == validar_definicao_evolutiva(direta, _vinculo(direta, [congelado], contexto))
    assert inversa == validar_definicao_evolutiva(inversa, _vinculo(inversa, [congelado], contexto))
    assert anterior == congelado

    mantida = _compor(_plano([filtrar]), [_indicador()], contexto=contexto)
    assert mantida["situacao"] == "esclarecimento"
    assert mantida["motivo"] == "juncao_nao_confirmada"
    assert "definicoes" not in mantida

    existente = _com_parametro(obrigatorio=True)
    movida = _compor(
        _plano([_op(acao="alterar_indicador", operacao="contagem", coluna=dict(REF_OUTRA))]),
        [existente],
        contexto=contexto,
    )
    assert movida["situacao"] == "esclarecimento"
    assert movida["motivo"] == "juncao_nao_confirmada"
    assert "definicoes" not in movida

    parcial = _compor(
        _plano(
            [
                _op(id="o1", acao="alterar_indicador", operacao="contagem_distinta"),
                _op(
                    id="o2",
                    acao="definir_filtro",
                    coluna=dict(REF_OUTRA),
                    operador="igual",
                    parametros=[{"forma": "tipado", "nome": "alvo", "tipo": "texto"}],
                ),
            ]
        ),
        [_indicador()],
        contexto=contexto,
    )
    assert parcial["situacao"] == "esclarecimento"
    assert parcial["motivo"] == "juncao_nao_confirmada"
    assert "definicoes" not in parcial
    assert "contagem_distinta" not in json.dumps(parcial)

    com_relacao = _contexto_duas_regioes(relacao=True)
    juncao = _compor(_plano([filtrar]), [_indicador()], contexto=com_relacao)
    assert juncao["motivo"] == "juncao_nao_confirmada"
    assert "definicoes" not in juncao
    assert "relacionamentos" not in json.dumps(juncao)
