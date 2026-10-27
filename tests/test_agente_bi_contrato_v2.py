"""SCRUM-269: contratos patch.definir.v2 e definir.v2, sem execução e sem Anthropic."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.models import IaChamadaTentativa
from app.services.agente_bi_analisar_service import CONTRATO_ANALISAR
from app.services.agente_bi_contrato_v2_service import (
    CONTRATO_DEFINIR_V2,
    CONTRATO_PATCH_V1,
    CONTRATO_PATCH_V2,
    descrever_contratos_v2,
    reconhecer_historico,
    validar_definicao_evolutiva,
    validar_plano,
)
from app.services.agente_bi_definir_service import (
    CONTRATO_DEFINIR,
    DSL_CONTRATO,
    DefinirErro,
    definir,
    montar_contexto_definir,
    validar_saida,
)
from app.services.agente_bi_evolucao_service import CONTRATO_PATCH
from tests.test_agente_bi_analisar import SENTINELA
from tests.test_agente_bi_definir import _jornada

ROOT = Path(__file__).resolve().parents[1]
ARQUIVO = "planilha-secreta.xlsx"
ABA = "aba-secreta"
CABECALHO = "cabecalho-secreto"


@pytest.fixture(autouse=True)
def _sem_anthropic(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.chamar_anthropic_governado", proibido)


def _coluna(indice, *, tipos, dimensoes, principal):
    return {
        "arquivo": 0,
        "aba": 0,
        "regiao": 0,
        "indice": indice,
        "tipos": tipos,
        "dimensoes": dimensoes,
        "dimensao_principal": principal,
        "presentes": 3,
        "distintos": 2,
        "nome": CABECALHO,
        "nome_arquivo": ARQUIVO,
        "nome_aba": ABA,
    }


def _colunas():
    return [
        _coluna(1, tipos=["texto"], dimensoes=["categoria"], principal="categoria"),
        _coluna(2, tipos=["data"], dimensoes=["data"], principal="data"),
        _coluna(4, tipos=["numero"], dimensoes=["valor"], principal="valor"),
    ]


def _ref(indice):
    return {"arquivo": 0, "aba": 0, "regiao": 0, "indice": indice}


def _vinculo(graficos=("g1",), colunas=None):
    return {"graficos": list(graficos), "colunas": list(_colunas() if colunas is None else colunas)}


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


def _recusa(plano, codigo, vinculo=None):
    with pytest.raises(DefinirErro) as erro:
        validar_plano(plano, _vinculo() if vinculo is None else vinculo)
    assert erro.value.codigo == codigo


def _sem_rotulo(documento):
    bruto = json.dumps(documento, ensure_ascii=False)
    assert ARQUIVO not in bruto
    assert ABA not in bruto
    assert CABECALHO not in bruto
    assert "usuario_id" not in bruto
    assert "conta_id" not in bruto
    assert "dashboard_id" not in bruto
    assert "dataset_id" not in bruto


def test_plano_com_uma_operacao_e_idempotente():
    plano = _plano([_op()])
    antes = copy.deepcopy(plano)
    saida = validar_plano(plano, _vinculo())
    assert plano == antes
    assert saida == validar_plano(saida, _vinculo())
    assert saida["contrato"] == CONTRATO_PATCH_V2
    assert saida["versao"] == 2
    assert saida["situacao"] == "proposto"
    assert saida["operacoes"][0]["acao"] == "consultar"
    assert saida["operacoes"][0]["grafico"] == "g1"
    _sem_rotulo(saida)


def test_plano_com_multiplas_operacoes_e_dependencias_validas():
    plano = _plano(
        [
            _op(id="o1"),
            _op(id="o2", acao="alterar_indicador", operacao="media", coluna=_ref(4), dependencias=["o1"]),
            _op(
                id="o3",
                acao="criar_grafico",
                grafico=None,
                temporario="t1",
                coluna=_ref(4),
                operacao="soma",
                apresentacao="indicador",
                dependencias=["o2"],
            ),
            _op(id="o4", acao="alterar_agrupamento", coluna=_ref(1)),
            _op(id="o5", acao="alterar_apresentacao", apresentacao="tabela"),
            _op(
                id="o6",
                acao="definir_filtro",
                coluna=_ref(1),
                operador="igual",
                parametros=[{"forma": "opaca", "referencia": "p1"}],
            ),
            _op(
                id="o7",
                acao="alterar_periodo",
                coluna=_ref(2),
                operador="entre",
                parametros=[
                    {"forma": "tipado", "nome": "inicio", "tipo": "data"},
                    {"forma": "tipado", "nome": "fim", "tipo": "data"},
                ],
            ),
        ]
    )
    saida = validar_plano(plano, _vinculo())
    assert [item["acao"] for item in saida["operacoes"]] == [
        "consultar",
        "alterar_indicador",
        "criar_grafico",
        "alterar_agrupamento",
        "alterar_apresentacao",
        "definir_filtro",
        "alterar_periodo",
    ]
    assert saida["operacoes"][2]["temporario"] == "t1"
    assert saida["operacoes"][2]["dependencias"] == ["o2"]
    _sem_rotulo(saida)
    assert set(descrever_contratos_v2()["plano"]["acoes"]) == {
        "consultar",
        "criar_grafico",
        "alterar_indicador",
        "alterar_agrupamento",
        "alterar_apresentacao",
        "definir_filtro",
        "alterar_periodo",
    }


def test_dependencia_encadeada_e_referencia_temporaria_indireta():
    plano = _plano(
        [
            _op(
                id="o1",
                acao="criar_grafico",
                grafico=None,
                temporario="t1",
                coluna=_ref(4),
                operacao="soma",
                apresentacao="indicador",
            ),
            _op(id="o2", dependencias=["o1"]),
            _op(
                id="o3",
                acao="alterar_agrupamento",
                grafico=None,
                temporario="t1",
                coluna=_ref(1),
                dependencias=["o2"],
            ),
        ]
    )
    saida = validar_plano(plano, _vinculo())
    assert saida["operacoes"][2]["temporario"] == "t1"
    assert saida["operacoes"][2]["grafico"] is None


def test_dependencias_invalidas_e_ciclicas():
    _recusa(_plano([_op(dependencias=["o9"])]), "dependencia_invalida")
    _recusa(_plano([_op(dependencias=["o1"])]), "dependencia_invalida")
    _recusa(_plano([_op(dependencias=["o2", "o2"]), _op(id="o2")]), "dependencia_invalida")
    ciclo = _plano([_op(id="o1", dependencias=["o2"]), _op(id="o2", dependencias=["o1"])])
    _recusa(ciclo, "dependencia_invalida")
    ciclo_longo = _plano(
        [
            _op(id="o1", dependencias=["o3"]),
            _op(id="o2", dependencias=["o1"]),
            _op(id="o3", dependencias=["o2"]),
        ]
    )
    _recusa(ciclo_longo, "dependencia_invalida")


def test_grafico_alvo_existente_e_inexistente():
    assert validar_plano(_plano([_op(grafico="g1")]), _vinculo())["operacoes"][0]["grafico"] == "g1"
    _recusa(_plano([_op(grafico="g9")]), "referencia_inexistente")
    _recusa(_plano([_op(grafico="t1")]), "contrato_saida_invalido")


def test_referencias_temporarias_validas_e_invalidas():
    valido = _plano(
        [
            _op(
                id="o1",
                acao="criar_grafico",
                grafico=None,
                temporario="t1",
                coluna=_ref(4),
                operacao="soma",
                apresentacao="indicador",
            ),
            _op(
                id="o2",
                acao="alterar_indicador",
                grafico=None,
                temporario="t1",
                operacao="media",
                dependencias=["o1"],
            ),
        ]
    )
    assert validar_plano(valido, _vinculo())["operacoes"][1]["temporario"] == "t1"
    _recusa(
        _plano([_op(acao="alterar_agrupamento", grafico=None, temporario="t1", coluna=_ref(1))]),
        "referencia_temporaria_invalida",
    )
    _recusa(
        _plano(
            [
                _op(
                    id="o1",
                    acao="criar_grafico",
                    grafico=None,
                    temporario="t1",
                    coluna=_ref(4),
                    operacao="soma",
                    apresentacao="indicador",
                ),
                _op(id="o2", acao="alterar_agrupamento", grafico=None, temporario="t1", coluna=_ref(1)),
            ]
        ),
        "referencia_temporaria_invalida",
    )
    _recusa(
        _plano(
            [
                _op(
                    id="o1",
                    acao="criar_grafico",
                    grafico=None,
                    temporario="t1",
                    coluna=_ref(4),
                    operacao="soma",
                    apresentacao="indicador",
                ),
                _op(
                    id="o2",
                    acao="criar_grafico",
                    grafico=None,
                    temporario="t1",
                    coluna=_ref(4),
                    operacao="soma",
                    apresentacao="indicador",
                ),
            ]
        ),
        "referencia_temporaria_invalida",
    )
    _recusa(
        _plano(
            [
                _op(
                    acao="criar_grafico",
                    grafico=None,
                    temporario="g1",
                    coluna=_ref(4),
                    operacao="soma",
                    apresentacao="indicador",
                )
            ]
        ),
        "referencia_temporaria_invalida",
    )


def test_acao_desconhecida_campos_extras_e_tipos_invalidos():
    _recusa(_plano([_op(acao="remover")]), "contrato_saida_invalido")
    _recusa(_plano([_op(acao="crie um grafico de vendas")]), "contrato_saida_invalido")
    extra = _plano([_op()])
    extra["texto"] = "filtre o sul"
    _recusa(extra, "contrato_saida_invalido")
    _recusa(_plano([_op()], situacao="publicado"), "contrato_saida_invalido")
    _recusa({**_plano([_op()]), "versao": "2"}, "contrato_saida_invalido")
    _recusa(_plano([_op(dependencias="o1")]), "contrato_saida_invalido")
    _recusa(_plano([_op(id="o1"), _op(id="o1", acao="alterar_apresentacao", apresentacao="tabela")]), "contrato_saida_invalido")
    coluna = _ref(1)
    coluna["nome"] = CABECALHO
    _recusa(_plano([_op(acao="alterar_agrupamento", coluna=coluna)]), "contrato_saida_invalido")
    _recusa(
        _plano(
            [
                _op(
                    acao="criar_grafico",
                    grafico=None,
                    temporario="t1",
                    coluna=_ref(4),
                    operacao="soma",
                    apresentacao="indicador",
                    parametros=[{"forma": "tipado", "nome": "select", "tipo": "texto"}],
                )
            ]
        ),
        "expressao_nao_permitida",
    )
    _recusa(_plano([_op(acao="select * from dados")]), "expressao_nao_permitida")
    _recusa(
        _plano(
            [
                _op(
                    acao="alterar_periodo",
                    coluna=_ref(4),
                    operador="entre",
                    parametros=[
                        {"forma": "tipado", "nome": "inicio", "tipo": "data"},
                        {"forma": "tipado", "nome": "fim", "tipo": "data"},
                    ],
                )
            ]
        ),
        "filtro_incompativel",
    )
    _recusa(
        _plano(
            [
                _op(
                    acao="definir_filtro",
                    coluna=_ref(1),
                    operador="maior",
                    parametros=[{"forma": "tipado", "nome": "limite", "tipo": "numero"}],
                )
            ]
        ),
        "filtro_incompativel",
    )


def test_esclarecimento_estruturado_sem_texto_livre():
    plano = _plano(
        [
            _op(
                acao="definir_filtro",
                grafico="g1",
                coluna=None,
                operador=None,
                esclarecimento={"motivo": "informar_parametro"},
            )
        ],
        situacao="esclarecimento",
    )
    saida = validar_plano(plano, _vinculo())
    assert saida["situacao"] == "esclarecimento"
    assert saida["operacoes"][0]["esclarecimento"] == {"motivo": "informar_parametro"}
    assert "Não identifiquei" not in json.dumps(saida, ensure_ascii=False)
    _recusa(
        _plano([_op(esclarecimento={"motivo": "informar_parametro"})]),
        "contrato_saida_invalido",
    )
    _recusa(_plano([_op()], situacao="esclarecimento"), "contrato_saida_invalido")
    pedido = _plano([_op(esclarecimento={"motivo": "informar_parametro", "pergunta": "qual valor?"})], situacao="esclarecimento")
    _recusa(pedido, "contrato_saida_invalido")


def test_plano_aceita_seis_criacoes_e_recusa_a_setima_vaga():
    def criacoes(quantidade):
        return [
            _op(
                id=f"o{indice}",
                acao="criar_grafico",
                grafico=None,
                temporario=f"t{indice}",
                coluna=_ref(4),
                operacao="soma",
                apresentacao="indicador",
            )
            for indice in range(1, quantidade + 1)
        ]

    assert len(validar_plano(_plano(criacoes(6)), _vinculo(graficos=()))["operacoes"]) == 6
    _recusa(_plano(criacoes(7)), "limite_graficos", _vinculo(graficos=()))
    _recusa(_plano(criacoes(6)), "limite_graficos")


def _vinculo_medidas():
    colunas = _colunas()
    colunas.append(_coluna(5, tipos=["numero"], dimensoes=["quantidade"], principal="quantidade"))
    return _vinculo(colunas=colunas)


def _criar(operacao, apresentacao, *, segunda=_ref(5), temporario="t1"):
    return _op(
        acao="criar_grafico",
        grafico=None,
        temporario=temporario,
        coluna=_ref(4),
        segunda_coluna=segunda,
        operacao=operacao,
        apresentacao=apresentacao,
    )


def test_segunda_medida_em_razao_par_e_alterar_indicador():
    vinculo = _vinculo_medidas()
    razao = validar_plano(_plano([_criar("razao", "indicador")]), vinculo)
    par = validar_plano(_plano([_criar("par_numerico", "dispersao", temporario="t2")]), vinculo)
    indicador = validar_plano(
        _plano([_op(acao="alterar_indicador", operacao="razao", coluna=_ref(4), segunda_coluna=_ref(5))]),
        vinculo,
    )
    indicador_par = validar_plano(
        _plano([_op(acao="alterar_indicador", operacao="par_numerico", coluna=_ref(4), segunda_coluna=_ref(5))]),
        vinculo,
    )
    assert razao["operacoes"][0]["segunda_coluna"] == _ref(5)
    assert par["operacoes"][0]["operacao"] == "par_numerico"
    assert par["operacoes"][0]["segunda_coluna"] == _ref(5)
    assert indicador["operacoes"][0]["acao"] == "alterar_indicador"
    assert indicador["operacoes"][0]["segunda_coluna"] == _ref(5)
    assert indicador_par["operacoes"][0]["segunda_coluna"] == _ref(5)
    assert razao == validar_plano(razao, vinculo)
    assert descrever_contratos_v2()["plano"]["operacoes_segunda_coluna"] == ["par_numerico", "razao"]
    assert "segunda_coluna" in descrever_contratos_v2()["plano"]["chaves_operacao"]

    _recusa(
        _plano([_op(acao="alterar_indicador", operacao="razao", coluna=_ref(4))]),
        "contrato_saida_invalido",
        vinculo,
    )
    _recusa(
        _plano([_op(acao="alterar_indicador", operacao="par_numerico", coluna=_ref(4))]),
        "contrato_saida_invalido",
        vinculo,
    )
    esclarecido = _plano(
        [
            _op(
                acao="alterar_indicador",
                operacao="razao",
                coluna=_ref(4),
                esclarecimento={"motivo": "informar_parametro"},
            )
        ],
        situacao="esclarecimento",
    )
    saida = validar_plano(esclarecido, vinculo)
    assert saida["situacao"] == "esclarecimento"
    assert saida["operacoes"][0]["segunda_coluna"] is None
    par_esclarecido = _plano(
        [
            _op(
                acao="criar_grafico",
                grafico=None,
                temporario="t1",
                coluna=_ref(4),
                operacao="par_numerico",
                apresentacao="dispersao",
                esclarecimento={"motivo": "informar_unidade"},
            )
        ],
        situacao="esclarecimento",
    )
    assert validar_plano(par_esclarecido, vinculo)["operacoes"][0]["segunda_coluna"] is None

    _recusa(
        _plano([_op(acao="alterar_indicador", operacao="razao", coluna=_ref(4), segunda_coluna=_ref(4))]),
        "contrato_saida_invalido",
        vinculo,
    )
    _recusa(
        _plano([_criar("razao", "indicador", segunda=_ref(9))]),
        "referencia_inexistente",
        vinculo,
    )
    _recusa(
        _plano([_op(acao="alterar_indicador", operacao="soma", coluna=_ref(4), segunda_coluna=_ref(5))]),
        "contrato_saida_invalido",
        vinculo,
    )
    _recusa(_plano([_criar("contagem", "indicador")]), "contrato_saida_invalido", vinculo)
    _recusa(
        _plano([_op(acao="alterar_indicador", operacao="participacao", coluna=_ref(4), segunda_coluna=_ref(5))]),
        "contrato_saida_invalido",
        vinculo,
    )
    _recusa(_plano([_op(segunda_coluna=_ref(4))]), "contrato_saida_invalido", vinculo)


def _criacao_t1():
    return _op(
        id="o1",
        acao="criar_grafico",
        grafico=None,
        temporario="t1",
        coluna=_ref(4),
        operacao="soma",
        apresentacao="indicador",
    )


def test_consulta_referencia_grafico_temporario_do_plano():
    direto = _plano(
        [
            _criacao_t1(),
            _op(id="o2", acao="consultar", grafico=None, temporario="t1", dependencias=["o1"]),
        ]
    )
    saida = validar_plano(direto, _vinculo())
    assert saida["operacoes"][1]["grafico"] is None
    assert saida["operacoes"][1]["temporario"] == "t1"
    assert saida["operacoes"][1]["dependencias"] == ["o1"]

    transitivo = _plano(
        [
            _criacao_t1(),
            _op(id="o2", dependencias=["o1"]),
            _op(id="o3", acao="consultar", grafico=None, temporario="t1", dependencias=["o2"]),
        ]
    )
    assert validar_plano(transitivo, _vinculo())["operacoes"][2]["temporario"] == "t1"
    assert validar_plano(_plano([_op(grafico="g1")]), _vinculo())["operacoes"][0]["grafico"] == "g1"

    _recusa(_plano([_op(grafico=None, temporario="t1")]), "referencia_temporaria_invalida")
    _recusa(
        _plano(
            [
                _criacao_t1(),
                _op(id="o2", acao="consultar", grafico=None, temporario="t1"),
            ]
        ),
        "referencia_temporaria_invalida",
    )
    _recusa(_plano([_op(grafico="g1", temporario="t1")]), "contrato_saida_invalido")
    _recusa(_plano([_op(grafico=None, temporario=None)]), "contrato_saida_invalido")


_VALORES_NAO_TEXTO = ([], {}, 1, True)


def _definir_erro(chamada, codigo="contrato_saida_invalido"):
    with pytest.raises(DefinirErro) as erro:
        chamada()
    assert erro.value.codigo == codigo
    assert erro.type is DefinirErro


def _item_definicao(**extras):
    item = {
        "id": "g1",
        "sugestao_id": None,
        "status": "definida",
        "grafico": "indicador",
        "leitura": "indicador_de_medida",
        "origem": "dados",
        "metrica": {"operacao": "contagem", "coluna": _ref(1), "unidade": "contagem"},
        "granularidade": "total",
        "dimensoes": [],
        "filtros": [],
        "parametros": [],
        "relacionamentos": [],
        "juncao": "nao_executada",
        "esclarecimento": None,
    }
    item.update(extras)
    return item


def _par_definicao(definicoes, *, origem="dados", origem_contrato=CONTRATO_DEFINIR, contexto=None):
    plano = _plano([_op(grafico="g1")])
    documento = {
        "contrato": CONTRATO_DEFINIR_V2,
        "versao": 2,
        "catalogo": "catalogo.analitico.v1",
        "dsl": DSL_CONTRATO,
        "execucao": "nao_executada",
        "definicao_anterior_id": 1,
        "revisao_base": 1,
        "origem_contrato": CONTRATO_DEFINIR,
        "proveniencia": {
            "definicao_anterior_id": 1,
            "revisao_base": 1,
            "origem_contrato": CONTRATO_DEFINIR,
            "plano_contrato": CONTRATO_PATCH_V2,
            "plano_versao": 2,
            "ids_preservados": ["g1"],
            "graficos_novos": [],
        },
        "plano": plano,
        "definicoes": definicoes,
    }
    vinculo = {
        "definicao_anterior_id": 1,
        "revisao_base": 1,
        "origem_contrato": origem_contrato,
        "definicoes": [{"id": "g1", "sugestao_id": None, "origem": origem}],
        "contexto": contexto
        or {"colunas": _colunas(), "regioes": [], "qualidade": [], "relacoes": []},
        "plano": copy.deepcopy(plano),
    }
    return documento, vinculo


def test_tipos_json_invalidos_produzem_definir_erro():
    for valor in _VALORES_NAO_TEXTO:
        _definir_erro(lambda situacao=valor: validar_plano(_plano([_op()], situacao=situacao), _vinculo()))
        _definir_erro(lambda acao=valor: validar_plano(_plano([_op(acao=acao)]), _vinculo()))
        _definir_erro(lambda operacao=valor: validar_plano(_plano([_op(operacao=operacao)]), _vinculo()))
        _definir_erro(lambda apresentacao=valor: validar_plano(_plano([_op(apresentacao=apresentacao)]), _vinculo()))
        _definir_erro(lambda operador=valor: validar_plano(_plano([_op(operador=operador)]), _vinculo()))
        _definir_erro(
            lambda motivo=valor: validar_plano(
                _plano([_op(esclarecimento={"motivo": motivo})], situacao="esclarecimento"),
                _vinculo(),
            )
        )
        _definir_erro(
            lambda tipo=valor: validar_plano(
                _plano(
                    [
                        _op(
                            parametros=[{"forma": "tipado", "nome": "limite", "tipo": tipo}],
                        )
                    ]
                ),
                _vinculo(),
            ),
            "parametro_inconsistente",
        )
        _definir_erro(lambda contrato=valor: reconhecer_historico({"contrato": contrato}))
        _definir_erro(
            lambda status=valor: validar_definicao_evolutiva(*_par_definicao([_item_definicao(status=status)]))
        )
        _definir_erro(
            lambda origem=valor: validar_definicao_evolutiva(*_par_definicao([_item_definicao(origem=origem)]))
        )
        _definir_erro(
            lambda grafico=valor: validar_definicao_evolutiva(*_par_definicao([_item_definicao(grafico=grafico)]))
        )
        metrica = {"operacao": valor, "coluna": _ref(1), "unidade": "contagem"}
        _definir_erro(lambda atual=metrica: validar_definicao_evolutiva(*_par_definicao([_item_definicao(metrica=atual)])))
        unidade = {"operacao": "contagem", "coluna": _ref(1), "unidade": valor}
        _definir_erro(
            lambda atual=unidade: validar_definicao_evolutiva(*_par_definicao([_item_definicao(metrica=atual)]))
        )
        _definir_erro(lambda origem=valor: validar_definicao_evolutiva(*_par_definicao([_item_definicao()], origem=origem)))
        _definir_erro(
            lambda origem_contrato=valor: validar_definicao_evolutiva(
                *_par_definicao([_item_definicao()], origem_contrato=origem_contrato)
            )
        )
        _definir_erro(
            lambda tipo=valor: validar_definicao_evolutiva(
                *_par_definicao([_item_definicao(parametros=[{"nome": "limite", "tipo": tipo, "obrigatorio": True}])])
            ),
            "parametro_inconsistente",
        )
        _definir_erro(
            lambda operador=valor: validar_definicao_evolutiva(
                *_par_definicao(
                    [
                        _item_definicao(
                            filtros=[
                                {"coluna": _ref(1), "operador": operador, "parametro": None, "parametro_fim": None}
                            ]
                        )
                    ]
                )
            )
        )
        _definir_erro(
            lambda granularidade=valor: validar_definicao_evolutiva(
                *_par_definicao(
                    [
                        _item_definicao(
                            dimensoes=[
                                {
                                    "papel": "agrupamento",
                                    "coluna": _ref(1),
                                    "granularidade": granularidade,
                                    "unidade": None,
                                }
                            ]
                        )
                    ]
                )
            )
        )
        colunas = _colunas()
        colunas[0]["dimensao_principal"] = valor
        _definir_erro(lambda atuais=colunas: validar_plano(_plano([_op()]), _vinculo(colunas=atuais)))

    for valor in ([{}], {"codigo": "x"}, 1, True):
        colunas = _colunas()
        colunas[0]["tipos"] = valor
        _definir_erro(lambda atuais=colunas: validar_plano(_plano([_op()]), _vinculo(colunas=atuais)))
        colunas = _colunas()
        colunas[0]["dimensoes"] = valor
        _definir_erro(lambda atuais=colunas: validar_plano(_plano([_op()]), _vinculo(colunas=atuais)))
        colunas = _colunas()
        colunas[0]["evidencias"] = valor
        _definir_erro(lambda atuais=colunas: validar_plano(_plano([_op()]), _vinculo(colunas=atuais)))
        contexto = {"colunas": _colunas(), "regioes": [{"ambiguidades": valor}], "qualidade": [], "relacoes": []}
        _definir_erro(
            lambda atual=contexto: validar_definicao_evolutiva(*_par_definicao([_item_definicao()], contexto=atual))
        )


_CHAVES_AUSENTES = frozenset(
    {"usuario_id", "conta_id", "franquia_id", "dashboard_id", "dataset_id", "dataset_versao", "dataset_impressao"}
)


def test_isolamento_das_identidades_no_plano():
    plano = _plano([_op()])
    plano["usuario_id"] = 7
    plano["conta_id"] = 8
    plano["dashboard_id"] = 9
    plano["dataset_id"] = 10
    _recusa(plano, "autorizacao_indevida")
    vinculo = _vinculo()
    vinculo["conta_id"] = 8
    vinculo["dashboard_id"] = 9
    _recusa(_plano([_op()]), "autorizacao_indevida", vinculo)
    _recusa(_plano([_op(grafico="g1")]), "referencia_inexistente", _vinculo(graficos=("g2",)))
    saida = validar_plano(_plano([_op()]), _vinculo())
    assert _CHAVES_AUSENTES.isdisjoint(saida)
    _sem_rotulo(saida)


def test_historico_v1_nao_e_convertido_nem_aceito_como_v2():
    assert CONTRATO_PATCH == CONTRATO_PATCH_V1 == "patch.definir.v1"
    assert CONTRATO_DEFINIR == "definir.v1"
    assert CONTRATO_ANALISAR == "analisar.v1"
    patch = {
        "contrato": CONTRATO_PATCH,
        "acao": "alterar_indicador",
        "operador": None,
        "operacao": "media",
        "coluna_indice": 1,
    }
    definicao = {"contrato": CONTRATO_DEFINIR, "dataset_id": 4, "definicoes": [{"id": "g1"}]}
    analise = {"contrato": CONTRATO_ANALISAR, "catalogo": "catalogo.analitico.v1"}
    for documento in (patch, definicao, analise):
        copia = copy.deepcopy(documento)
        reconhecido = reconhecer_historico(documento)
        assert reconhecido == documento == copia
        assert reconhecido is not documento
        assert "segunda_coluna" not in reconhecido
        reconhecido["contrato"] = "definir.v2"
        assert documento["contrato"] != "definir.v2"
    with pytest.raises(DefinirErro) as erro:
        validar_plano(patch, _vinculo())
    assert erro.value.codigo == "contrato_saida_invalido"
    with pytest.raises(DefinirErro) as erro:
        validar_definicao_evolutiva(definicao, {})
    assert erro.value.codigo == "contrato_saida_invalido"
    with pytest.raises(DefinirErro) as erro:
        reconhecer_historico({"contrato": CONTRATO_DEFINIR_V2})
    assert erro.value.codigo == "contrato_saida_invalido"
    fonte = (ROOT / "app" / "services" / "agente_bi_contrato_v2_service.py").read_text(encoding="utf-8")
    assert "import re" not in fonte
    assert "chamar_anthropic" not in fonte
    assert "calcular_definicao" not in fonte
    assert "montar_dashboard" not in fonte


def _ids_livres(usados, quantidade):
    saida = []
    numero = 1
    while len(saida) < quantidade:
        candidato = f"g{numero}"
        numero += 1
        if candidato not in usados:
            saida.append(candidato)
    return saida


def _plano_de(base, faltam):
    if faltam == 0:
        return _plano([_op(grafico=base[0]["id"])])
    temps = [f"t{indice}" for indice in range(1, faltam + 1)]
    modelo = base[0]
    operacao = modelo["metrica"]["operacao"]
    segunda = None
    if operacao in {"par_numerico", "razao"}:
        segunda = next(
            (item["coluna"] for item in modelo["dimensoes"] if item.get("papel") == "segunda_medida"),
            None,
        )
    return _plano(
        [
            _op(
                id=f"o{indice}",
                acao="criar_grafico",
                grafico=None,
                temporario=temp,
                coluna=modelo["metrica"]["coluna"],
                segunda_coluna=segunda,
                operacao=operacao,
                apresentacao=modelo["grafico"],
            )
            for indice, temp in enumerate(temps, start=1)
        ]
    )


def _conjunto(base, contexto, total, *, anterior=41, revisao=3):
    faltam = total - len(base)
    definicoes = copy.deepcopy(base)
    for grafico in _ids_livres({item["id"] for item in base}, faltam):
        copia = copy.deepcopy(base[0])
        copia["id"] = grafico
        copia["sugestao_id"] = None
        definicoes.append(copia)
    plano = _plano_de(base, faltam)
    ids_base = [item["id"] for item in base]
    novos = [item["id"] for item in definicoes[len(base) :]]
    criacoes = [item["temporario"] for item in plano["operacoes"] if item["acao"] == "criar_grafico"]
    documento = {
        "contrato": CONTRATO_DEFINIR_V2,
        "versao": 2,
        "catalogo": "catalogo.analitico.v1",
        "dsl": DSL_CONTRATO,
        "execucao": "nao_executada",
        "definicao_anterior_id": anterior,
        "revisao_base": revisao,
        "origem_contrato": CONTRATO_DEFINIR,
        "proveniencia": {
            "definicao_anterior_id": anterior,
            "revisao_base": revisao,
            "origem_contrato": CONTRATO_DEFINIR,
            "plano_contrato": CONTRATO_PATCH_V2,
            "plano_versao": 2,
            "ids_preservados": ids_base,
            "graficos_novos": [{"id": grafico, "temporario": temp} for grafico, temp in zip(novos, criacoes)],
        },
        "plano": plano,
        "definicoes": definicoes,
    }
    vinculo = {
        "definicao_anterior_id": anterior,
        "revisao_base": revisao,
        "origem_contrato": CONTRATO_DEFINIR,
        "definicoes": base,
        "contexto": contexto,
        "plano": copy.deepcopy(plano),
    }
    return documento, vinculo


def test_definicao_evolutiva_preserva_ids_limite_proveniencia_e_v1(ctx):
    _usuario, _franquia, _escopo, perfil, pacote, _aprovada = _jornada("contrato.v2@test.com", "contrato-v2")
    documento = definir(perfil, pacote)
    contexto = montar_contexto_definir(perfil, pacote)
    assert documento["contrato"] == CONTRATO_DEFINIR
    assert validar_saida(contexto, documento)["contrato"] == CONTRATO_DEFINIR
    historico = copy.deepcopy(documento)
    assert reconhecer_historico(documento) == documento
    assert documento == historico

    contexto_local = copy.deepcopy(contexto)
    contexto_local["nome_arquivo"] = ARQUIVO
    contexto_local["nome_aba"] = ABA
    contexto_local["usuario_id"] = 99
    base = documento["definicoes"]
    preservado, vinculo = _conjunto(base, contexto_local, len(base))
    antes = copy.deepcopy(preservado)
    antes_chamadas = IaChamadaTentativa.query.count()
    saida = validar_definicao_evolutiva(preservado, vinculo)
    assert preservado == antes
    assert [item["id"] for item in saida["definicoes"]] == [item["id"] for item in base]
    assert [item["sugestao_id"] for item in saida["definicoes"]] == [item["sugestao_id"] for item in base]
    assert saida["origem_contrato"] == CONTRATO_DEFINIR
    assert saida["definicao_anterior_id"] == 41
    assert saida["revisao_base"] == 3
    assert saida["proveniencia"]["ids_preservados"] == [item["id"] for item in base]
    assert saida["proveniencia"]["graficos_novos"] == []
    assert saida["dsl"] == DSL_CONTRATO
    assert saida["execucao"] == "nao_executada"
    assert "contrato_saida_validado" not in saida
    assert documento["contrato"] == CONTRATO_DEFINIR
    _sem_rotulo(saida)
    assert SENTINELA not in json.dumps(saida, ensure_ascii=False)
    assert IaChamadaTentativa.query.count() == antes_chamadas

    if len(base) < 6:
        completo, vinculo_completo = _conjunto(base, contexto_local, 6)
        seis = validar_definicao_evolutiva(completo, vinculo_completo)
        assert len(seis["definicoes"]) == 6
        assert [item["id"] for item in seis["definicoes"][: len(base)]] == [item["id"] for item in base]
        assert all(item["sugestao_id"] is None for item in seis["definicoes"][len(base) :])
    with pytest.raises(DefinirErro) as limite:
        validar_definicao_evolutiva(*_conjunto(base, contexto_local, 7))
    assert limite.value.codigo == "limite_graficos"

    divergente, vinculo_divergente = _conjunto(base, contexto_local, len(base))
    divergente["revisao_base"] = 4
    divergente["proveniencia"]["revisao_base"] = 4
    with pytest.raises(DefinirErro) as proveniencia:
        validar_definicao_evolutiva(divergente, vinculo_divergente)
    assert proveniencia.value.codigo == "proveniencia_inconsistente"

    outro_plano, vinculo_plano = _conjunto(base, contexto_local, len(base))
    outro_plano["plano"] = _plano([_op(id="o2", grafico=base[0]["id"], acao="alterar_apresentacao", apresentacao="tabela")])
    with pytest.raises(DefinirErro) as plano_divergente:
        validar_definicao_evolutiva(outro_plano, vinculo_plano)
    assert plano_divergente.value.codigo == "proveniencia_inconsistente"

    marcado, vinculo_marcado = _conjunto(base, contexto_local, len(base))
    marcado["contrato_saida_validado"] = True
    with pytest.raises(DefinirErro) as flag:
        validar_definicao_evolutiva(marcado, vinculo_marcado)
    assert flag.value.codigo == "contrato_saida_invalido"
    invalido, vinculo_invalido = _conjunto(base, contexto_local, len(base))
    atual = invalido["definicoes"][0]["metrica"]["unidade"]
    invalido["definicoes"][0]["metrica"]["unidade"] = "contagem" if atual != "contagem" else "valor"
    with pytest.raises(DefinirErro) as unidade:
        validar_definicao_evolutiva(invalido, vinculo_invalido)
    assert unidade.value.codigo == "unidade_incompativel"

    concedido, vinculo_concedido = _conjunto(base, contexto_local, len(base))
    concedido["usuario_id"] = 7
    concedido["dataset_id"] = contexto["dataset"]["id"]
    with pytest.raises(DefinirErro) as identidade:
        validar_definicao_evolutiva(concedido, vinculo_concedido)
    assert identidade.value.codigo == "autorizacao_indevida"
    vinculo_concedido["dashboard_id"] = 15
    with pytest.raises(DefinirErro) as vinculo_identidade:
        validar_definicao_evolutiva(_conjunto(base, contexto_local, len(base))[0], vinculo_concedido)
    assert vinculo_identidade.value.codigo == "autorizacao_indevida"
