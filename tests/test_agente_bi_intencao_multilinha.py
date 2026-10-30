"""SCRUM-273: intenção com quebra de linha, tabulação e retorno de carro.

O transporte é simulado. Nenhuma chamada usa o cliente da Anthropic.
"""
from __future__ import annotations

import copy

import pytest

from app.services.agente_bi_analisar_service import (
    AnalisarErro,
    TransferenciaRecusada,
    _anotacao_autorizada,
    _intencao_original,
    _validar_pedido,
    governar_representacao,
)
from app.services.agente_bi_descoberta_service import DescobertaErro
from app.services.agente_bi_interpretacao_v2_service import InterpretacaoErro, interpretar_plano
from app.services.cleiton_ai_data_governance import DECISION_ALLOW, GovernanceResult
from tests.test_agente_bi_descoberta_claude import _condicional, _descobrir, _mensagem, _pedido, _preparar, _transporte
from tests.test_agente_bi_explicacao import _factual, _pedir
from tests.test_agente_bi_interpretacao_v2 import (
    _classificador_proibido,
    _creditar,
    _mensagem as _mensagem_plano,
    _operacao,
    _plano,
    _resposta,
)
from tests.test_agente_bi_publicacao_v2 import _preparar as _preparar_painel


LINHA = "comparar a quantidade por categoria"
QUEBRA = "comparar a quantidade\npor categoria"
TABULACAO = "comparar\tquantidade"
RETORNO = "primeira linha\r\nsegunda linha"
MISTA = "antes \n meio\t\tfim\r\n\r\nultimo"
INTERNA = "comparar\nquantidade\tpor categoria\r\nno periodo"


@pytest.fixture(autouse=True)
def _transporte_simulado(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENTE_BI_WORKER_HABILITADO", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)


def _pedido_c(texto):
    return {"modalidades": ["C"], "texto": texto}


def _fronteira(intencao):
    return {
        "amostras": False,
        "valores_de_celula": False,
        "rotulos": False,
        "apto_anthropic": False,
        "autorizado_para_provedor": False,
        "transferencia": "bloqueada",
        "intencao": intencao,
    }


def _allow(conteudo, **_kwargs):
    return GovernanceResult(
        decision=DECISION_ALLOW,
        safe_content=copy.deepcopy(conteudo),
        reason_codes=["no_incidental_categories"],
        categories_detected=[],
        categories_removed=[],
        issued_by_cleiton=True,
    )


@pytest.mark.parametrize("texto", [LINHA, QUEBRA, TABULACAO, RETORNO, MISTA, INTERNA])
def test_intencao_original_preserva_texto_interno(texto):
    assert _intencao_original(_pedido_c(texto)) == texto
    assert _anotacao_autorizada(texto) is True


def test_strip_preserva_quebras_internas():
    bruto = "\n\t" + INTERNA + "\r\n "
    assert _intencao_original(_pedido_c(bruto)) == INTERNA
    assert _anotacao_autorizada(bruto) is True
    assert _anotacao_autorizada("\n") is True


def test_controles_nomeados_sao_recusados():
    for controle in ("\x00", "\x1b", "\x07"):
        texto = f"comparar{controle}quantidade"
        assert _intencao_original(_pedido_c(texto)) is None
        assert _anotacao_autorizada(texto) is False
        assert _anotacao_autorizada(controle) is False


def test_demais_caracteres_de_controle_continuam_recusados():
    for codigo in range(32):
        caractere = chr(codigo)
        if caractere in "\t\n\r":
            continue
        texto = f"comparar{caractere}quantidade"
        assert _intencao_original(_pedido_c(texto)) is None
        assert _anotacao_autorizada(texto) is False


@pytest.mark.parametrize("texto", ["", "   ", "\n", "\t\r\n", "\n\n"])
def test_texto_vazio_nao_vira_intencao(texto):
    assert _intencao_original(_pedido_c(texto)) is None


def test_ausencia_nao_e_texto_livre():
    assert _intencao_original(_pedido_c(None)) is None
    assert _anotacao_autorizada(None) is True


@pytest.mark.parametrize("texto", [10, 1.5, True, ["linha"], {"texto": "linha"}, b"linha"])
def test_tipo_invalido_nao_vira_intencao(texto):
    assert _intencao_original(_pedido_c(texto)) is None
    assert _anotacao_autorizada(texto) is False


def test_modalidade_sem_c_nao_transporta_texto():
    assert _intencao_original({"modalidades": ["A"], "texto": INTERNA}) is None


def test_limites_existentes_permanecem():
    no_limite = "a\n" + ("b" * 1998)
    acima = no_limite + "c"
    com_quebra_final = ("a" * 1999) + "\n"
    assert len(no_limite) == 2_000
    assert len(acima) == 2_001
    assert _anotacao_autorizada(no_limite) is True
    assert _anotacao_autorizada(com_quebra_final) is True
    assert _anotacao_autorizada(acima) is False
    assert _anotacao_autorizada("") is False
    assert _validar_pedido(_pedido_c(no_limite))["texto"] == no_limite
    assert _validar_pedido(_pedido_c(com_quebra_final))["texto"] == com_quebra_final
    assert _intencao_original(_pedido_c(no_limite)) == no_limite
    assert _intencao_original(_pedido_c(com_quebra_final)) == "a" * 1999
    with pytest.raises(AnalisarErro) as limite:
        _validar_pedido(_pedido_c(acima))
    assert limite.value.codigo == "pedido_invalido"
    with pytest.raises(AnalisarErro) as tipo:
        _validar_pedido(_pedido_c(10))
    assert tipo.value.codigo == "pedido_invalido"


@pytest.mark.parametrize("texto", [LINHA, QUEBRA, TABULACAO, RETORNO, MISTA, INTERNA])
def test_allow_preserva_intencao_identica(texto):
    original = _fronteira(texto)
    saida = governar_representacao(original, governar=_allow)
    assert saida["decisao"] == DECISION_ALLOW
    assert saida["anotacoes_removidas"] is False
    assert saida["payload"] == original
    assert saida["payload"]["intencao"] == texto


def test_allow_usa_o_texto_interno_depois_do_strip():
    intencao = _intencao_original(_pedido_c("\n" + INTERNA + "\t"))
    assert intencao == INTERNA
    saida = governar_representacao(_fronteira(intencao), governar=_allow)
    assert saida["payload"]["intencao"] == INTERNA


@pytest.mark.parametrize("controle", ["\x00", "\x1b", "\x07"])
def test_retorno_governado_com_controle_e_recusado(controle):
    original = _fronteira(INTERNA)

    def envenenar(conteudo, **_kwargs):
        seguro = copy.deepcopy(conteudo)
        seguro["intencao"] = f"{conteudo['intencao']}{controle}"
        return _allow(seguro)

    with pytest.raises(TransferenciaRecusada):
        governar_representacao(original, governar=envenenar)


def test_descoberta_a_c_transporta_intencao_multilinha(ctx):
    _usuario, _franquia, escopo, dataset_id, perfil = _preparar("multilinha-ac@test.com")
    for texto in (LINHA, INTERNA):
        estado = {}
        corpo = _descobrir(escopo, dataset_id, _pedido(perfil, ["A", "C"], texto=texto), _transporte(estado, _condicional(perfil)))
        assert estado["chamadas"] == 1
        assert _mensagem(estado)["intencao"] == texto
        assert isinstance(corpo["analise_id"], int)
        with pytest.raises(DescobertaErro) as erro:
            _descobrir(
                escopo,
                dataset_id,
                _pedido(perfil, ["A", "C"], texto=f"{texto}\x00"),
                _transporte({}, _condicional(perfil)),
            )
        assert erro.value.codigo == "intencao_nao_transportada"


def test_interpretacao_v2_transporta_intencao_multilinha(ctx, monkeypatch):
    _classificador_proibido(monkeypatch)
    _franquia, escopo, painel = _creditar("plano.multilinha@test.com", "plano-multilinha")
    identificador = painel["dashboard"]["id"]

    for texto in (LINHA, "consulte o grafico existente\ne nao use media\r\nno indicador\tpublicado"):
        estado = {"chamadas": 0, "mensagem": None}

        def enviar(pedido, estado=estado):
            estado["mensagem"] = _mensagem_plano(pedido)
            estado["chamadas"] += 1
            return _resposta(_plano([_operacao()]))

        saida = interpretar_plano(escopo, identificador, {"texto": texto}, transporte=enviar)
        assert estado["chamadas"] == 1
        assert estado["mensagem"]["intencao"] == texto
        assert saida["situacao"] == "proposto"


def test_explicacao_transporta_intencao_multilinha(ctx):
    base = _preparar_painel("explicacao.multilinha@test.com", "explicacao-multilinha")
    texto = "O que esses numeros dizem\nsobre a operacao\r\nlogistica\tno periodo?"
    resposta, estado, _antes = _pedir(base["escopo"], base["dashboard_id"], texto, _factual)
    assert resposta["situacao"] == "explicacao"
    assert estado["explicacao"]["intencao"] == texto


def test_interpretacao_rejeita_controle_no_texto(ctx, monkeypatch):
    _classificador_proibido(monkeypatch)
    _franquia, escopo, painel = _creditar("plano.controle@test.com", "plano-controle")

    def enviar(_pedido):
        raise AssertionError("transporte")

    with pytest.raises(InterpretacaoErro) as erro:
        interpretar_plano(escopo, painel["dashboard"]["id"], {"texto": "consulte o grafico\x00existente"}, transporte=enviar)
    assert erro.value.codigo == "payload_invalido"
