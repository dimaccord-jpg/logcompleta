"""SCRUM-276: compatibilidade de gráfico na descoberta alinhada à definição.

Não chama provedor externo. A definição e o cálculo permanecem intactos.
"""
from __future__ import annotations

import copy
import json
from datetime import date, datetime, time

import pytest

from app.models import AgenteBiAnalise
from app.services.agente_bi_analisar_service import (
    AnalisarErro,
    _leitura,
    analisar,
    analisar_dataset,
    montar_contexto,
    validar_saida,
)
from app.services.agente_bi_definir_service import (
    DefinirErro,
    _forma,
    _grafico_compativel,
)
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import escopo_do_usuario, obter_analise
from tests.test_agente_bi_analisar import _afirmacao, _coord, _movimento, _pedido_a, _perfil
from tests.test_agente_bi_ingestao import _par, _xlsx

OPERACOES_VALIDAS = ("contagem", "contagem_distinta", "soma", "media", "minimo", "maximo")


def _planilha() -> bytes:
    categorias = ["norte", "sul", "leste"] * 3
    return _xlsx(
        {
            "movimento": [
                ["categoria", "ampla", "unico", "medida", "quando", "quantidade", "ativo", "hora", "instante"],
                *[
                    [
                        categorias[indice],
                        f"g{indice}",
                        "mesmo",
                        10.5 if indice % 3 == 0 else 4.25 if indice % 3 == 1 else 8.0,
                        date(2024, 3, indice + 1),
                        10 if indice % 2 == 0 else 4,
                        indice % 2 == 0,
                        time(8, 0) if indice % 2 == 0 else time(9, 15),
                        datetime(2024, 3, indice + 1, 8, 30),
                    ]
                    for indice in range(9)
                ],
            ]
        }
    )


def _base(email: str):
    _conta, _franquia, usuario = _par(email.split("@")[0], email)
    _resultado, perfil = _perfil(usuario, _planilha(), "compatibilidade.xlsx", email.split("@")[0])
    return usuario, perfil


def _coluna(perfil, **filtro):
    return next(coluna for coluna in perfil["colunas"] if all(coluna[chave] == valor for chave, valor in filtro.items()))


def _por_papel(perfil):
    aba = _coluna(perfil, dimensao_principal="valor")
    mesma = [
        coluna
        for coluna in perfil["colunas"]
        if coluna["aba"] == aba["aba"] and coluna["regiao"] == aba["regiao"]
    ]

    def achar(predicado):
        return next(coluna for coluna in mesma if predicado(coluna))

    return {
        "medida": aba,
        "quantidade": achar(lambda coluna: coluna["dimensao_principal"] == "quantidade"),
        "categoria": achar(lambda coluna: "texto" in coluna["tipos"] and coluna["distintos"] == 3),
        "ampla": achar(lambda coluna: "texto" in coluna["tipos"] and coluna["distintos"] == 9),
        "unico": achar(lambda coluna: "texto" in coluna["tipos"] and coluna["distintos"] == 1),
        "quando": achar(lambda coluna: "data" in coluna["dimensoes"] and "tempo" not in coluna["dimensoes"]),
        "instante": achar(lambda coluna: "tempo" in coluna["dimensoes"] and "tipo_data" in coluna["evidencias"]),
        "hora": achar(lambda coluna: "tipo_hora" in coluna["evidencias"]),
        "ativo": achar(lambda coluna: "booleano" in coluna["tipos"]),
    }


def _rascunho(grafico, operacao, medida, agrupamento=None, segunda=None):
    evidencias = [
        codigo
        for codigo in ("tipo_numero_fracionado", "tipo_numero_inteiro", "tipo_texto", "unicidade_observada", "valores_repetidos")
        if codigo in medida["evidencias"]
    ]
    return {
        "status": "viavel",
        "leitura": _leitura(grafico, operacao),
        "grafico": grafico,
        "operacao": operacao,
        "origem": "dados",
        "regiao": {"arquivo": medida["arquivo"], "aba": medida["aba"], "regiao": medida["regiao"]},
        "medida": _coord(medida),
        "agrupamento": None if agrupamento is None else _coord(agrupamento),
        "segunda_medida": None if segunda is None else _coord(segunda),
        "evidencias": evidencias[:1] or list(medida["evidencias"][:1]),
        "juncao": "nao_executada",
        "lacuna": None,
        "duvidas": [],
    }


def _julgar(perfil, rascunho):
    try:
        saida = analisar(
            perfil,
            _pedido_a(),
            provedor=lambda _contexto, proposta={"descobertas": [], "sugestoes": [rascunho]}: proposta,
        )
    except AnalisarErro as exc:
        return exc.codigo, None
    return None, saida


def _no_contexto(contexto, ref):
    if ref is None:
        return None
    for coluna in contexto["colunas"]:
        if all(coluna[chave] == ref[chave] for chave in ("arquivo", "aba", "regiao", "indice")):
            return coluna
    raise AssertionError(ref)


def _definicao_aceita(contexto, sugestao) -> None:
    medida = _no_contexto(contexto, sugestao["medida"])
    agrupamento = _no_contexto(contexto, sugestao["agrupamento"])
    segunda = _no_contexto(contexto, sugestao["segunda_medida"])
    assert _grafico_compativel(sugestao["grafico"], sugestao["operacao"], medida, agrupamento, segunda) is True
    _forma(sugestao["grafico"], sugestao["operacao"], medida, agrupamento, segunda)


def _definicao_recusa_granularidade(contexto, grafico, operacao, medida, agrupamento) -> None:
    coluna_medida = _no_contexto(contexto, _coord(medida))
    coluna_grupo = _no_contexto(contexto, _coord(agrupamento))
    with pytest.raises(DefinirErro) as erro:
        _forma(grafico, operacao, coluna_medida, coluna_grupo, None)
    assert erro.value.codigo == "granularidade_incompativel"


def test_agrupamentos_temporais_numericos_e_booleanos_sao_recusados_na_descoberta(ctx):
    _usuario, perfil = _base("compat.recusa@test.com")
    colunas = _por_papel(perfil)
    contexto = montar_contexto(perfil, _pedido_a())
    assert "texto" not in colunas["ativo"]["tipos"]
    assert colunas["ativo"]["distintos"] >= 2
    assert colunas["quando"]["distintos"] >= 2
    assert colunas["quantidade"]["distintos"] >= 2
    assert colunas["hora"]["distintos"] >= 2
    grupos = {
        "data": colunas["quando"],
        "tempo": colunas["instante"],
        "hora": colunas["hora"],
        "quantidade": colunas["quantidade"],
        "valor": colunas["medida"],
        "booleano": colunas["ativo"],
    }
    for nome, grupo in grupos.items():
        for operacao in OPERACOES_VALIDAS:
            for grafico in ("barra", "pizza", "tabela"):
                codigo, saida = _julgar(perfil, _rascunho(grafico, operacao, colunas["medida"], grupo))
                assert codigo == "grafico_incompativel", (nome, operacao, grafico, codigo)
                assert saida is None
        _definicao_recusa_granularidade(contexto, "barra", "soma", colunas["medida"], grupo)


def test_sinais_temporais_e_numericos_isolados_tambem_sao_recusados(ctx):
    _usuario, perfil = _base("compat.sinais@test.com")
    colunas = _por_papel(perfil)
    categoria = colunas["categoria"]
    assert "texto" in categoria["tipos"]
    assert not ({"data", "tempo"} & set(categoria["dimensoes"]))

    def alterar(**mudancas):
        copia = copy.deepcopy(perfil)
        alvo = next(
            coluna
            for coluna in copia["colunas"]
            if (coluna["arquivo"], coluna["aba"], coluna["regiao"], coluna["indice"])
            == (categoria["arquivo"], categoria["aba"], categoria["regiao"], categoria["indice"])
        )
        alvo.update(mudancas)
        return copia, alvo

    por_dimensao, grupo_data = alterar(dimensoes=["data"], dimensao_principal="data", evidencias=["tipo_texto"])
    por_evidencia, grupo_evidencia = alterar(evidencias=list(categoria["evidencias"]) + ["tipo_data"])
    por_numero, grupo_numero = alterar(tipos=["texto", "numero"], evidencias=["tipo_texto"])
    por_evidencia_numerica, grupo_numero_evidencia = alterar(
        evidencias=list(categoria["evidencias"]) + ["tipo_numero_inteiro"]
    )
    assert "tipo_data" not in grupo_data["evidencias"]
    assert "data" not in grupo_evidencia["dimensoes"]
    assert "tempo" not in grupo_evidencia["dimensoes"]
    assert grupo_numero["dimensao_principal"] not in {"valor", "quantidade"}
    assert grupo_numero_evidencia["dimensao_principal"] not in {"valor", "quantidade"}

    for copia, grupo in (
        (por_dimensao, grupo_data),
        (por_evidencia, grupo_evidencia),
        (por_numero, grupo_numero),
        (por_evidencia_numerica, grupo_numero_evidencia),
    ):
        codigo, _saida = _julgar(copia, _rascunho("barra", "soma", colunas["medida"], grupo))
        assert codigo == "grafico_incompativel"
        _definicao_recusa_granularidade(montar_contexto(copia, _pedido_a()), "barra", "soma", colunas["medida"], grupo)


def test_segunda_medida_fora_de_par_numerico_e_recusada(ctx):
    _usuario, perfil = _base("compat.segunda@test.com")
    colunas = _por_papel(perfil)
    contexto = montar_contexto(perfil, _pedido_a())
    medida = _no_contexto(contexto, _coord(colunas["medida"]))
    segunda = _no_contexto(contexto, _coord(colunas["quantidade"]))
    categoria = _no_contexto(contexto, _coord(colunas["categoria"]))
    for operacao in OPERACOES_VALIDAS:
        codigo, saida = _julgar(
            perfil,
            _rascunho("indicador", operacao, colunas["medida"], segunda=colunas["quantidade"]),
        )
        assert codigo == "grafico_incompativel", operacao
        assert saida is None
        assert _grafico_compativel("indicador", operacao, medida, None, segunda) is False
    codigo, _saida = _julgar(
        perfil,
        _rascunho("barra", "soma", colunas["medida"], colunas["categoria"], colunas["quantidade"]),
    )
    assert codigo == "grafico_incompativel"
    assert _grafico_compativel("barra", "soma", medida, categoria, segunda) is False
    codigo, saida = _julgar(
        perfil,
        _rascunho("dispersao", "par_numerico", colunas["medida"], segunda=colunas["quantidade"]),
    )
    assert codigo is None
    _definicao_aceita(contexto, saida["sugestoes"][0])


def test_graficos_e_operacoes_validos_continuam_aceitos_e_passam_na_definicao(ctx):
    _usuario, perfil = _base("compat.valida@test.com")
    colunas = _por_papel(perfil)
    contexto = montar_contexto(perfil, _pedido_a())
    aceitos = []
    for operacao in OPERACOES_VALIDAS:
        aceitos.append(_rascunho("indicador", operacao, colunas["medida"]))
        aceitos.append(_rascunho("tabela", operacao, colunas["medida"]))
        aceitos.append(_rascunho("barra", operacao, colunas["medida"], colunas["categoria"]))
        aceitos.append(_rascunho("tabela", operacao, colunas["medida"], colunas["categoria"]))
        if 2 <= colunas["categoria"]["distintos"] <= 8:
            aceitos.append(_rascunho("pizza", operacao, colunas["medida"], colunas["categoria"]))
        aceitos.append(_rascunho("linha", operacao, colunas["medida"], colunas["quando"]))
    aceitos.append(_rascunho("dispersao", "par_numerico", colunas["medida"], segunda=colunas["quantidade"]))
    aceitos.append(_rascunho("barra", "soma", colunas["medida"], colunas["ampla"]))
    vistos = set()
    for rascunho in aceitos:
        codigo, saida = _julgar(perfil, rascunho)
        chave = (rascunho["grafico"], rascunho["operacao"], rascunho["agrupamento"] is not None)
        assert codigo is None, chave
        sugestao = saida["sugestoes"][0]
        assert sugestao["grafico"] == rascunho["grafico"]
        assert sugestao["operacao"] == rascunho["operacao"]
        _definicao_aceita(contexto, sugestao)
        vistos.add(chave)
    assert ("linha", "soma", True) in vistos
    assert ("indicador", "contagem", False) in vistos
    assert ("indicador", "contagem_distinta", False) in vistos
    assert ("tabela", "soma", False) in vistos
    assert ("barra", "media", True) in vistos
    assert ("pizza", "minimo", True) in vistos
    assert ("dispersao", "par_numerico", False) in vistos

    for grafico in ("barra", "pizza"):
        codigo, _saida = _julgar(perfil, _rascunho(grafico, "soma", colunas["medida"], colunas["unico"]))
        assert codigo == "grafico_incompativel"
        assert _grafico_compativel(
            grafico,
            "soma",
            _no_contexto(contexto, _coord(colunas["medida"])),
            _no_contexto(contexto, _coord(colunas["unico"])),
            None,
        ) is False
    codigo, saida = _julgar(perfil, _rascunho("pizza", "soma", colunas["medida"], colunas["ampla"]))
    assert codigo == "grafico_incompativel"
    assert saida is None
    assert _grafico_compativel(
        "pizza",
        "soma",
        _no_contexto(contexto, _coord(colunas["medida"])),
        _no_contexto(contexto, _coord(colunas["ampla"])),
        None,
    ) is False
    codigo, saida = _julgar(perfil, _rascunho("tabela", "soma", colunas["medida"], colunas["ampla"]))
    assert codigo is None
    _definicao_aceita(contexto, saida["sugestoes"][0])


def test_referencia_textual_permanece_e_agrupamento_temporal_nao_entra(ctx):
    _usuario, perfil = _base("compat.referencia@test.com")
    colunas = _por_papel(perfil)
    medida = _coord(colunas["medida"])
    compativel = analisar(
        perfil,
        {
            "modalidades": ["B"],
            "referencia": {
                "afirmacoes": [_afirmacao("soma", medida, grafico="barra", agrupamento=_coord(colunas["categoria"]))],
                "formulas": [],
            },
        },
    )
    assert [item["codigo"] for item in compativel["critica_referencia"]] == ["referencia_compativel"]
    assert compativel["sugestoes"][0]["grafico"] == "barra"
    assert compativel["sugestoes"][0]["agrupamento"] == _coord(colunas["categoria"])

    recusada = analisar(
        perfil,
        {
            "modalidades": ["B"],
            "referencia": {
                "afirmacoes": [_afirmacao("soma", medida, grafico="barra", agrupamento=_coord(colunas["quando"]))],
                "formulas": [],
            },
        },
    )
    assert [item["codigo"] for item in recusada["critica_referencia"]] == ["operacao_incompativel"]
    assert recusada["sugestoes"] == []


def test_documento_historico_compativel_continua_revalidando(ctx):
    _conta, _franquia, usuario = _par("compat-hist", "compat.hist@test.com")
    resultado, _perfil_preparado = _perfil(usuario, _movimento(), "historico.xlsx", "compat-hist")
    pedido = _pedido_a()
    escopo = escopo_do_usuario(usuario)
    gravado = analisar_dataset(
        escopo,
        resultado["dataset"]["id"],
        chave="compat-hist",
        rodada=1,
        pedido=pedido,
    )
    documento = gravado["resultado"]
    analise = AgenteBiAnalise.query.filter_by(id=gravado["analise"]["id"]).one()
    historico = json.loads(analise.resultado_json)
    assert historico == documento
    perfil_persistido = perfilar_dataset(escopo, resultado["dataset"]["id"])
    contexto = montar_contexto(perfil_persistido, pedido)
    assert validar_saida(contexto, historico)["contrato_saida_validado"] is True
    assert validar_saida(contexto, json.loads(json.dumps(historico)))["contrato"] == "analisar.v1"
    persistido = obter_analise(escopo, gravado["analise"]["id"])["resultado"]
    assert validar_saida(contexto, persistido)["contrato_saida_validado"] is True
    assert persistido["sugestoes"]
    for sugestao in persistido["sugestoes"]:
        if sugestao["operacao"] is None:
            continue
        _definicao_aceita(contexto, sugestao)
        assert sugestao["grafico"] in {"indicador", "linha", "barra", "pizza", "tabela", "dispersao"}
    assert any(item["grafico"] == "linha" for item in persistido["sugestoes"])
    assert any(item["grafico"] == "indicador" and item["agrupamento"] is None for item in persistido["sugestoes"])


def test_combinacoes_do_provedor_simulado_passam_na_definicao(ctx):
    _usuario, perfil = _base("compat.simulado@test.com")
    pedido = _pedido_a()
    saida = analisar(perfil, pedido)
    contexto = montar_contexto(perfil, pedido)
    assert validar_saida(contexto, saida)["contrato_saida_validado"] is True
    operacoes = set()
    graficos = set()
    for sugestao in saida["sugestoes"]:
        if sugestao["operacao"] is None:
            continue
        _definicao_aceita(contexto, sugestao)
        operacoes.add(sugestao["operacao"])
        graficos.add(sugestao["grafico"])
        if sugestao["grafico"] in {"barra", "pizza", "tabela"} and sugestao["agrupamento"] is not None:
            grupo = _no_contexto(contexto, sugestao["agrupamento"])
            assert "texto" in grupo["tipos"]
            assert grupo["dimensao_principal"] not in {"valor", "quantidade"}
            assert not ({"data", "tempo"} & set(grupo["dimensoes"]))
    assert "soma" in operacoes
    assert {"indicador", "linha", "barra"} <= graficos
