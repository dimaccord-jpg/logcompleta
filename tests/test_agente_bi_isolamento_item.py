"""SCRUM-286: incoerência local isola o item e preserva a sugestão válida.

Provedores falsos e dataset sintético. Não chama Claude, Gemini nem outra IA faturável.
"""
from __future__ import annotations

import copy
import json
import logging
from types import SimpleNamespace

import pytest

from app.models import AgenteBiExecucao
from app.services.agente_bi_analisar_service import (
    _INCOERENCIAS_LOCAIS,
    _varrer_saida,
    AnalisarErro,
    analisar,
    montar_contexto,
    validar_saida,
)
from app.services.agente_bi_colaboracao_service import (
    _CAMPOS_DESCARTE,
    _CHECKPOINT,
    _MOTIVOS_DESCARTE,
    _isolar,
    _motivo_recuperavel,
    _progresso,
    _representar_proposta,
    instrucao_de_critica,
    instrucao_de_revisao,
    revalidar_contrato_tecnico,
)
from app.services.agente_bi_colaboracao_service import (
    _MOTIVO_CATALOGO,
)
from app.services.agente_bi_execucao_service import ExecucaoErro, _referencias
from app.services import agente_bi_colaboracao_service as colaboracao
from tests.test_agente_bi_analisar import SENTINELA
from tests.test_agente_bi_colaboracao import (  # noqa: F401 - fixture autouse
    _Claude,
    _Gemini,
    _colaboracao_falsa,
    _conteudo_claude,
    _critica,
    _descobrir,
    _proposta_viavel,
    _tecnicas,
)
from tests.test_agente_bi_descoberta_claude import _coord, _pedido, _preparar, _regiao

_VAZAMENTOS = (
    SENTINELA,
    "norte",
    "sul",
    "K200",
    "10.5",
    "alfa",
    "beta",
    "quando",
    "qtd",
    "valor",
    "Traceback",
    "AnalisarErro",
)
_RAMOS = (
    ("viavel_com_pergunta", "sugestoes", "status"),
    ("pergunta_ausente", "sugestoes", "duvidas"),
    ("lacuna_incoerente", "sugestoes", "item"),
    ("esclarecimento_incoerente", "sugestoes", "item"),
    ("campos_da_operacao", "sugestoes", "item"),
    ("leitura_incompativel", "sugestoes", "leitura"),
    ("origem_sem_referencia", "sugestoes", "origem"),
    ("orcamento_relacoes_incoerente", "descobertas", "codigo"),
)
# Reprodução em memória da validação: leitura contagem_de_registros fora do indicador.
_REPRODUCOES = (
    ("barra_contagem", "barra", "contagem", "texto"),
    ("pizza_contagem", "pizza", "contagem", "texto"),
    ("linha_contagem", "linha", "contagem", "data"),
    ("tabela_contagem", "tabela", "contagem", "texto"),
    ("barra_contagem_distinta", "barra", "contagem_distinta", "texto"),
    ("linha_soma", "linha", "soma", "data"),
    ("indicador_soma", "indicador", "soma", None),
    ("dispersao_par", "dispersao", "par_numerico", None),
)


def _valor(perfil):
    return next(coluna for coluna in perfil["colunas"] if coluna["dimensao_principal"] == "valor")


def _irma(perfil, valor, predicado):
    return next(
        coluna
        for coluna in perfil["colunas"]
        if coluna["aba"] == valor["aba"]
        and coluna["regiao"] == valor["regiao"]
        and coluna["indice"] != valor["indice"]
        and predicado(coluna)
    )


def _texto(perfil, valor):
    return _irma(
        perfil,
        valor,
        lambda coluna: "texto" in (coluna.get("tipos") or [])
        and coluna.get("dimensao_principal") not in {"valor", "quantidade"}
        and not ({"data", "tempo"} & set(coluna.get("dimensoes") or [])),
    )


def _temporal(perfil, valor):
    return _irma(perfil, valor, lambda coluna: bool({"data", "tempo"} & set(coluna.get("dimensoes") or [])))


def _outra_numerica(perfil, valor):
    return _irma(perfil, valor, lambda coluna: coluna.get("dimensao_principal") in {"valor", "quantidade"})


def _evidencia(coluna):
    return [codigo for codigo in (coluna.get("evidencias") or []) if codigo != "aba_oculta"][:1]


def _regiao_de(coluna):
    return {"arquivo": coluna["arquivo"], "aba": coluna["aba"], "regiao": coluna["regiao"]}


def _base(perfil, *, status="viavel", leitura="esclarecimento_necessario", grafico=None, operacao=None, origem="dados", lacuna=None, duvidas=None):
    return {
        "status": status,
        "leitura": leitura,
        "grafico": grafico,
        "operacao": operacao,
        "origem": origem,
        "regiao": _regiao(perfil),
        "medida": None,
        "agrupamento": None,
        "segunda_medida": None,
        "evidencias": [],
        "juncao": "nao_executada",
        "lacuna": lacuna,
        "duvidas": [] if duvidas is None else list(duvidas),
    }


def _grafico_leitura(perfil, grafico, operacao, grupo):
    valor = _valor(perfil)
    agrupamento = None
    segunda = None
    if grupo == "texto":
        agrupamento = _coord(_texto(perfil, valor))
    elif grupo == "data":
        agrupamento = _coord(_temporal(perfil, valor))
    if operacao == "par_numerico":
        segunda = _coord(_outra_numerica(perfil, valor))
    return {
        "status": "viavel",
        "leitura": "contagem_de_registros",
        "grafico": grafico,
        "operacao": operacao,
        "origem": "dados",
        "regiao": _regiao_de(valor),
        "medida": _coord(valor),
        "agrupamento": agrupamento,
        "segunda_medida": segunda,
        "evidencias": _evidencia(valor),
        "juncao": "nao_executada",
        "lacuna": None,
        "duvidas": [],
    }


def _item_do_ramo(ramo, perfil, boa, pedido):
    if ramo == "viavel_com_pergunta":
        item = copy.deepcopy(boa)
        item["duvidas"] = ["escolher_medida"]
        return item
    if ramo == "pergunta_ausente":
        return _base(perfil, status="condicional")
    if ramo == "lacuna_incoerente":
        return _base(
            perfil,
            status="dependente_de_novos_dados",
            leitura="lacuna_de_dados",
            operacao="contagem",
            lacuna="sem_registros",
            duvidas=["informar_dados_ausentes"],
        )
    if ramo == "esclarecimento_incoerente":
        return _base(perfil, status="condicional", grafico="indicador", duvidas=["escolher_medida"])
    if ramo == "campos_da_operacao":
        return _base(perfil, leitura="contagem_de_registros", operacao="contagem")
    if ramo == "leitura_incompativel":
        return _grafico_leitura(perfil, "barra", "contagem", "texto")
    if ramo == "origem_sem_referencia":
        item = copy.deepcopy(boa)
        item["origem"] = "referencia"
        return item
    if ramo == "orcamento_relacoes_incoerente":
        orcamento = montar_contexto(perfil, pedido)["orcamento_relacoes"]
        codigo = next(nome for nome in ("analise_relacoes_parcial", "limite_relacoes_atingido") if orcamento[nome] is False)
        return {"codigo": codigo, "regiao": _regiao(perfil), "relacao": None}
    raise AssertionError(ramo)


def _proposta_do_ramo(ramo, perfil, boa, pedido):
    item = _item_do_ramo(ramo, perfil, boa, pedido)
    if ramo == "orcamento_relacoes_incoerente":
        return {"descobertas": [item], "sugestoes": [boa]}
    return {"descobertas": [], "sugestoes": [boa, item]}


def _eventos(caplog):
    return [
        registro.getMessage()
        for registro in caplog.records
        if registro.getMessage().startswith("evento=agente_bi_descarte")
    ]


def _sem_vazamento(mensagens):
    for mensagem in mensagens:
        for trecho in _VAZAMENTOS:
            assert trecho not in mensagem
        for motivo in _INCOERENCIAS_LOCAIS:
            if f"motivo={motivo}" in mensagem:
                continue
            assert motivo not in mensagem
        assert "contagem_de_registros" not in mensagem


def _recuperado(app, caplog, email, montar, motivo, lista, posicao, campo):
    caplog.set_level(logging.INFO)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar(email)
        pedido = _pedido(perfil, ["A"])
        boa = _proposta_viavel(perfil, pedido)["sugestoes"][0]
        bruto = montar(perfil, boa, pedido)
        claude = _Claude([bruto])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["colaboracao"]["fase"] == "gemini_revisando"
        tecnica = _tecnicas(dataset_id)[0]
        documento = json.loads(tecnica.resultado_json)
        execucao = AgenteBiExecucao.query.filter_by(analise_id=tecnica.id).one()
        assert revalidar_contrato_tecnico(tecnica, _referencias(execucao, tecnica), documento) == documento
        assert documento["proposta"]["sugestoes"] == [boa]
        assert documento["descartes"] == [
            {"lista": lista, "posicao": posicao, "campo": campo, "motivo": _MOTIVO_CATALOGO}
        ]
        assert motivo not in tecnica.resultado_json
        mensagens = _eventos(caplog)
        assert any(f"motivo={motivo}" in mensagem and "etapa=validar_" in mensagem for mensagem in mensagens)
        _sem_vazamento(mensagens)


@pytest.mark.parametrize(("ramo", "lista", "campo"), _RAMOS, ids=[item[0] for item in _RAMOS])
def test_ramo_local_preserva_a_sugestao_valida(app, caplog, ramo, lista, campo):
    _recuperado(
        app,
        caplog,
        f"isol-{ramo}@test.com",
        lambda perfil, boa, pedido: _proposta_do_ramo(ramo, perfil, boa, pedido),
        ramo,
        lista,
        0 if lista == "descobertas" else 1,
        campo,
    )


@pytest.mark.parametrize(
    ("nome", "grafico", "operacao", "grupo"),
    _REPRODUCOES,
    ids=[item[0] for item in _REPRODUCOES],
)
def test_reproducao_de_leitura_contagem_de_registros(app, caplog, nome, grafico, operacao, grupo):
    def montar(perfil, boa, _pedido):
        return {"descobertas": [], "sugestoes": [boa, _grafico_leitura(perfil, grafico, operacao, grupo)]}

    _recuperado(
        app,
        caplog,
        f"isol-rep-{nome}@test.com",
        montar,
        "leitura_incompativel",
        "sugestoes",
        1,
        "leitura",
    )


def test_so_itens_incoerentes_continua_irrecuperavel(app, caplog):
    caplog.set_level(logging.INFO)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("isol-so@test.com")
        pedido = _pedido(perfil, ["A"])
        bruto = {
            "descobertas": [],
            "sugestoes": [
                _grafico_leitura(perfil, "barra", "contagem", "texto"),
                _grafico_leitura(perfil, "linha", "contagem", "data"),
            ],
        }
        claude = _Claude([bruto])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert _tecnicas(dataset_id)[0].resultado_json is None
        assert len(gemini.geracoes) == 0
        assert "verificacao_nao_recuperavel" in caplog.text
        assert any("motivo=leitura_incompativel" in mensagem for mensagem in _eventos(caplog))


def test_checkpoint_e_incoerencia_o_codigo_vem_do_checkpoint(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("isol-check@test.com")
        pedido = _pedido(perfil, ["A"])
        boa = _proposta_viavel(perfil, pedido)["sugestoes"][0]
        valor = _valor(perfil)
        checkpoint = {
            "status": "viavel",
            "leitura": "medida_por_categoria",
            "grafico": "barra",
            "operacao": "soma",
            "origem": "dados",
            "regiao": _regiao_de(valor),
            "medida": _coord(valor),
            "agrupamento": _coord(_temporal(perfil, valor)),
            "segunda_medida": None,
            "evidencias": _evidencia(valor),
            "juncao": "nao_executada",
            "lacuna": None,
            "duvidas": [],
        }
        bruto = {
            "descobertas": [],
            "sugestoes": [boa, checkpoint, _grafico_leitura(perfil, "barra", "contagem", "texto")],
        }
        claude = _Claude([bruto])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["colaboracao"]["fase"] == "gemini_revisando"
        documento = json.loads(_tecnicas(dataset_id)[0].resultado_json)
        execucao = AgenteBiExecucao.query.filter_by(analise_id=_tecnicas(dataset_id)[0].id).one()
        assert revalidar_contrato_tecnico(_tecnicas(dataset_id)[0], _referencias(execucao, _tecnicas(dataset_id)[0]), documento) == documento
        assert documento["verificacao"]["codigo"] == "grafico_incompativel"
        assert documento["proposta"]["sugestoes"] == [boa]
        assert documento["descartes"] == [
            {"lista": "sugestoes", "posicao": 1, "campo": "item", "motivo": "grafico_incompativel"},
            {"lista": "sugestoes", "posicao": 2, "campo": "leitura", "motivo": _MOTIVO_CATALOGO},
        ]


def test_ramos_globais_e_varredura_continuam_irrecuperaveis(app, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("isol-global@test.com")
        pedido = _pedido(perfil, ["A"])
        boa = _proposta_viavel(perfil, pedido)["sugestoes"][0]
        saida = analisar(perfil, pedido, provedor=lambda _contexto: {"descobertas": [], "sugestoes": [boa]})
        contexto = montar_contexto(perfil, pedido)

        def recusa(documento):
            with pytest.raises(AnalisarErro) as erro:
                validar_saida(contexto, documento)
            assert erro.value.codigo == "contrato_saida_invalido"
            assert erro.value.motivo is None
            assert erro.value.motivo not in _INCOERENCIAS_LOCAIS

        modalidades = copy.deepcopy(saida)
        modalidades["modalidades"] = ["B"]
        vinculo = copy.deepcopy(saida)
        vinculo["vinculo_preparacao"] = "ab" * 32
        trilha = copy.deepcopy(saida)
        trilha["trilha"] = {**trilha["trilha"], "perfil": "outro"}
        instrucoes = copy.deepcopy(saida)
        instrucoes["instrucoes_nao_confiaveis"] = {
            **instrucoes["instrucoes_nao_confiaveis"],
            "detectadas": not instrucoes["instrucoes_nao_confiaveis"]["detectadas"],
        }
        chaves = copy.deepcopy(saida)
        chaves["sugestoes"] = [{**chaves["sugestoes"][0], "id": "s9", "extra": "fora"}]
        teto = copy.deepcopy(saida)
        teto["sugestoes"] = list(saida["sugestoes"]) * 9
        identificadores = copy.deepcopy(saida)
        identificadores["sugestoes"] = [dict(saida["sugestoes"][0]), dict(saida["sugestoes"][0])]
        critica = copy.deepcopy(saida)
        critica["critica_referencia"] = [{"id": "c1", "codigo": "metrica_inventada", "reproduzida": False}]
        for documento in (modalidades, vinculo, trilha, instrucoes, chaves, teto, identificadores, critica):
            recusa(documento)
        with pytest.raises(AnalisarErro) as varredura:
            _varrer_saida({"rotulo": "alfa"})
        assert varredura.value.codigo == "contrato_saida_invalido"
        assert varredura.value.motivo is None
        assert _motivo_recuperavel({"descobertas": [], "sugestoes": {}}, perfil, pedido) is False

        extra = {"descobertas": [], "sugestoes": [boa], "extra": "nao-enviar"}
        recusado = _descobrir(escopo, dataset_id, pedido, _Claude([extra]), _Gemini("{}"))
        assert recusado["colaboracao"]["estado"] == "recusada"
        assert _tecnicas(dataset_id)[0].resultado_json is None
        assert "nao-enviar" not in caplog.text

        bruto = {"descobertas": [], "sugestoes": [boa, _grafico_leitura(perfil, "barra", "contagem", "texto")]}
        proposta, descartes = _representar_proposta(bruto)
        monkeypatch.setattr(
            "app.services.agente_bi_analisar_service._varrer_saida",
            lambda _valor: (_ for _ in ()).throw(AnalisarErro("contrato_saida_invalido")),
        )
        assert _isolar(bruto, proposta, descartes, perfil, pedido) is None

        def inventado(*_args, **_kwargs):
            raise AnalisarErro("contrato_saida_invalido", motivo="motivo_inventado")

        monkeypatch.setattr(colaboracao, "analisar", inventado)
        assert _motivo_recuperavel({"descobertas": [], "sugestoes": [boa]}, perfil, pedido) is False

        def bloqueio(*_args, **_kwargs):
            raise AnalisarErro("autorizacao_indevida")

        monkeypatch.setattr(colaboracao, "analisar", bloqueio)
        with pytest.raises(ExecucaoErro) as duro:
            _motivo_recuperavel({"descobertas": [], "sugestoes": [boa]}, perfil, pedido)
        assert duro.value.codigo == "autorizacao_indevida"
        assert _INCOERENCIAS_LOCAIS.isdisjoint(_MOTIVOS_DESCARTE)
        for motivo in _INCOERENCIAS_LOCAIS:
            assert motivo not in _CAMPOS_DESCARTE["sugestoes"]
            assert motivo not in _CAMPOS_DESCARTE["descobertas"]


def test_mensagens_levam_so_campos_e_motivos_ja_existentes(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("isol-msg@test.com")
        pedido = _pedido(perfil, ["A"])
        boa = _proposta_viavel(perfil, pedido)["sugestoes"][0]
        bruto = {"descobertas": [], "sugestoes": [boa, _grafico_leitura(perfil, "barra", "contagem", "texto")]}
        limpa = {"descobertas": [], "sugestoes": [boa]}
        claude = _Claude([bruto, limpa])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        enviado_gemini = json.loads(gemini.geracoes[0]["contents"])
        revisao = _conteudo_claude(claude.pedidos[1])
        descartes = enviado_gemini["descartes"]
        assert descartes == revisao["verificacao_anterior"]["descartes"]
        assert descartes == [
            {"lista": "sugestoes", "posicao": 1, "campo": "leitura", "motivo": _MOTIVO_CATALOGO}
        ]
        for carga in (enviado_gemini, revisao):
            texto = json.dumps(carga, ensure_ascii=False)
            for motivo in _INCOERENCIAS_LOCAIS:
                assert motivo not in texto
            for item in carga.get("descartes") or revisao["verificacao_anterior"]["descartes"]:
                assert set(item) == {"lista", "posicao", "campo", "motivo"}
                assert item["motivo"] in _MOTIVOS_DESCARTE
                assert item["campo"] in _CAMPOS_DESCARTE[item["lista"]]
        instrucoes = instrucao_de_revisao() + "\n" + instrucao_de_critica()
        for motivo in _INCOERENCIAS_LOCAIS:
            assert motivo not in instrucoes
        assert "contagem_de_registros" not in json.dumps(enviado_gemini["descartes"])


def test_progresso_nao_entra_em_laco(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("isol-laco@test.com")
        pedido = _pedido(perfil, ["A"])
        boa = _proposta_viavel(perfil, pedido)["sugestoes"][0]
        bruto = {"descobertas": [], "sugestoes": [boa, _grafico_leitura(perfil, "linha", "contagem", "data")]}
        claude = _Claude([bruto])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = None
        for _ in range(5):
            corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
            if (corpo.get("colaboracao") or {}).get("continuar") is not True:
                break
        else:
            raise AssertionError("laco")
        assert claude.chamadas == 2
        assert corpo["colaboracao"]["continuar"] is False
        contexto = montar_contexto(perfil, pedido)
        documento = json.loads(next(item.resultado_json for item in _tecnicas(dataset_id) if item.resultado_json and json.loads(item.resultado_json).get("papel") == "claude"))
        tecnicas = [SimpleNamespace(estado="concluida", resultado_json=documento) for _ in range(3)]
        assert _progresso(contexto, tecnicas) is False
        assert _CHECKPOINT
