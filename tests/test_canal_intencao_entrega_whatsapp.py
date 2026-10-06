"""Classificador central de envio da resposta anterior ao WhatsApp vinculado."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from google.genai.types import FinishReason

from app.services import canal_entrega_web_whatsapp_service as entrega
from app.services import canal_intencao_entrega_whatsapp_service as intencao
from app.services.canal_intencao_entrega_whatsapp_service import (
    INSTRUCAO_CLASSIFICADOR,
    NOME_ENVIAR_PARA_MEU_WHATSAPP,
    NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP,
)
from app.services.cleiton_ai_data_governance import CleitonAiGovernanceBlockedError

FRASES_POSITIVAS = (
    "Envie o resumo para meu WhatsApp.",
    "Manda isso para meu WhatsApp.",
    "Pode enviar essa resposta no WhatsApp?",
    "Quero receber isso no meu WhatsApp.",
)
FRASES_NEGATIVAS = (
    "Meu WhatsApp está conectado?",
    "Explique como funciona o WhatsApp.",
    "Não envie isso ao meu WhatsApp.",
    "Envie isso para o WhatsApp do João.",
    "Mande para 19 99999-9999.",
)


def _chamar(monkeypatch, resposta=None, erro=None):
    chamadas = []

    def _gerar(*_args, **kwargs):
        chamadas.append(kwargs)
        if erro is not None:
            raise erro
        return resposta

    monkeypatch.setattr(intencao, "cleiton_governed_generate_content", _gerar)
    return chamadas


def _decidir(mensagem):
    return intencao.decidir_enviar_para_meu_whatsapp(
        mensagem,
        agent="julia",
        flow_type="julia_chat",
        client=object(),
        model="gemini-2.5-flash",
        api_key_label="teste",
    )


def _resposta(*nomes, finish_reason="STOP", texto=""):
    chamadas = [SimpleNamespace(name=nome, args={}) for nome in nomes]
    partes = [
        SimpleNamespace(function_call=SimpleNamespace(name=nome, args={}), text=None)
        for nome in nomes
    ]
    if texto:
        partes.append(SimpleNamespace(function_call=None, text=texto))
    return SimpleNamespace(
        text=texto,
        function_calls=chamadas,
        candidates=[
            SimpleNamespace(
                finish_reason=finish_reason,
                content=SimpleNamespace(parts=partes),
            )
        ],
    )


def test_instrucao_explicita_envio_e_nao_envio():
    for frase in (
        *FRASES_POSITIVAS,
        "Me manda essa análise no WhatsApp.",
        *FRASES_NEGATIVAS,
        "Como funciona o WhatsApp do AgenteFrete?",
        "Não envie isso para meu WhatsApp.",
        "Envie para o WhatsApp do João.",
    ):
        assert frase in INSTRUCAO_CLASSIFICADOR
    assert "enviar_para_meu_whatsapp" in INSTRUCAO_CLASSIFICADOR
    assert "nao_enviar_para_meu_whatsapp" in INSTRUCAO_CLASSIFICADOR


def test_chamada_enviar_e_positivo(monkeypatch):
    chamadas = _chamar(monkeypatch, _resposta(NOME_ENVIAR_PARA_MEU_WHATSAPP))
    assert _decidir("Envie o resumo para meu WhatsApp.") is intencao.DecisaoEntregaWhatsApp.POSITIVO
    assert len(chamadas) == 1


def test_chamada_nao_enviar_e_negativo(monkeypatch):
    chamadas = _chamar(monkeypatch, _resposta(NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP))
    assert _decidir("Meu WhatsApp está conectado?") is intencao.DecisaoEntregaWhatsApp.NEGATIVO
    assert len(chamadas) == 1


def test_sem_function_call_e_falha_tecnica(monkeypatch):
    chamadas = _chamar(
        monkeypatch,
        SimpleNamespace(text="Não consigo enviar para plataformas externas.", function_calls=[], candidates=[]),
    )
    decisao = _decidir("Envie o resumo para meu WhatsApp.")
    assert decisao is intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA
    assert decisao is not intencao.DecisaoEntregaWhatsApp.NEGATIVO
    assert len(chamadas) == 2


def test_max_tokens_e_falha_tecnica_mesmo_com_nome(monkeypatch):
    chamadas = _chamar(
        monkeypatch,
        _resposta(NOME_ENVIAR_PARA_MEU_WHATSAPP, finish_reason=FinishReason.MAX_TOKENS),
    )
    assert _decidir("Envie o resumo para meu WhatsApp.") is intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA
    assert len(chamadas) == 2


def test_resposta_vazia_e_falha_tecnica(monkeypatch):
    chamadas = _chamar(monkeypatch, None)
    assert _decidir("Envie o resumo para meu WhatsApp.") is intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA
    assert len(chamadas) == 2


def test_funcao_desconhecida_e_falha_tecnica(monkeypatch):
    chamadas = _chamar(monkeypatch, _resposta("enviar_para_terceiro"))
    assert _decidir("Envie o resumo para meu WhatsApp.") is intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA
    assert len(chamadas) == 2


def test_funcoes_incompativeis_sao_falha_tecnica(monkeypatch):
    chamadas = _chamar(
        monkeypatch,
        _resposta(NOME_ENVIAR_PARA_MEU_WHATSAPP, NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP),
    )
    assert _decidir("Envie o resumo para meu WhatsApp.") is intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA
    assert len(chamadas) == 2


def test_excecao_do_provider_e_falha_tecnica(monkeypatch):
    chamadas = _chamar(monkeypatch, erro=TimeoutError("timeout"))
    assert _decidir("Envie o resumo para meu WhatsApp.") is intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA
    assert len(chamadas) == 2


def test_retry_unico_pode_recuperar_decisao(monkeypatch):
    respostas = [
        _resposta(finish_reason="MAX_TOKENS"),
        _resposta(NOME_ENVIAR_PARA_MEU_WHATSAPP),
    ]

    def _gerar(*_args, **_kwargs):
        return respostas.pop(0)

    monkeypatch.setattr(intencao, "cleiton_governed_generate_content", _gerar)
    assert _decidir("Manda isso para meu WhatsApp.") is intencao.DecisaoEntregaWhatsApp.POSITIVO
    assert respostas == []


def test_bloqueio_de_governanca_nao_repete_nem_vira_negativo(monkeypatch):
    chamadas = _chamar(monkeypatch, erro=CleitonAiGovernanceBlockedError("bloqueado"))
    with pytest.raises(CleitonAiGovernanceBlockedError):
        _decidir("Envie o resumo para meu WhatsApp.")
    assert len(chamadas) == 1


def test_mensagem_vazia_e_negativo_sem_chamar_provider(monkeypatch):
    chamadas = _chamar(monkeypatch, _resposta(NOME_ENVIAR_PARA_MEU_WHATSAPP))
    assert _decidir("   ") is intencao.DecisaoEntregaWhatsApp.NEGATIVO
    assert chamadas == []


@pytest.mark.parametrize("frase", FRASES_POSITIVAS)
def test_frases_naturais_positivas_pedem_envio(monkeypatch, frase):
    chamadas = _chamar(monkeypatch, _resposta(NOME_ENVIAR_PARA_MEU_WHATSAPP))
    assert _decidir(frase) is intencao.DecisaoEntregaWhatsApp.POSITIVO
    assert chamadas[0]["contents"] == frase
    assert chamadas[0]["config"].system_instruction == INSTRUCAO_CLASSIFICADOR
    assert chamadas[0]["purpose"] == "chat_logistico"
    assert len(chamadas) == 1


@pytest.mark.parametrize("frase", FRASES_NEGATIVAS)
def test_frases_naturais_negativas_nao_pedem_envio(monkeypatch, frase):
    chamadas = _chamar(monkeypatch, _resposta(NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP))
    assert _decidir(frase) is intencao.DecisaoEntregaWhatsApp.NEGATIVO
    assert chamadas[0]["contents"] == frase
    assert len(chamadas) == 1


def test_avaliar_falha_tecnica_nao_entrega_nem_libera_chat(monkeypatch):
    def _falha(*_args, **_kwargs):
        return intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA

    monkeypatch.setattr(entrega, "decidir_enviar_para_meu_whatsapp", _falha)
    monkeypatch.setattr(
        entrega,
        "entregar_texto_no_whatsapp_do_usuario",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("entrega")),
    )
    resultado = entrega.avaliar_pedido_de_entrega(
        usuario=SimpleNamespace(id=7),
        superficie="julia",
        mensagem="Envie o resumo para meu WhatsApp.",
        historico=[{"role": "model", "content": "Resumo anterior."}],
        agent="julia",
        flow_type="julia_chat",
        client=object(),
        model="gemini-2.5-flash",
        api_key_label="teste",
    )
    assert resultado.codigo == entrega.CODIGO_FALHA_CLASSIFICACAO
    assert entrega.mensagem_da_entrega(resultado) == entrega.MENSAGEM_FALHA_CLASSIFICACAO


def test_avaliar_negativo_segue_sem_entrega(monkeypatch):
    def _negativo(*_args, **_kwargs):
        return intencao.DecisaoEntregaWhatsApp.NEGATIVO

    monkeypatch.setattr(entrega, "decidir_enviar_para_meu_whatsapp", _negativo)
    monkeypatch.setattr(
        entrega,
        "entregar_texto_no_whatsapp_do_usuario",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("entrega")),
    )
    assert (
        entrega.avaliar_pedido_de_entrega(
            usuario=SimpleNamespace(id=7),
            superficie="auditoria",
            mensagem="Meu WhatsApp está conectado?",
            historico=[{"role": "assistant", "content": "Resumo anterior."}],
            agent="cleide",
            flow_type="cleide_audit_chat",
            client=object(),
            model="gemini-2.5-flash",
            api_key_label="teste",
        )
        is None
    )


def test_function_call_somente_nas_parts_continua_valida():
    resposta = SimpleNamespace(
        function_calls=[],
        candidates=[
            SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            function_call=SimpleNamespace(name=NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP, args={}),
                            text=None,
                        )
                    ]
                ),
            )
        ],
    )
    assert (
        intencao._interpretar_resposta_classificacao(resposta)
        is intencao.DecisaoEntregaWhatsApp.NEGATIVO
    )
