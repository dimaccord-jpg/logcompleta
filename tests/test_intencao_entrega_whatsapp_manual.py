"""Validação semântica manual com Gemini real.

Não faz parte da suíte obrigatória. Roda só com opt-in explícito:

    set RUN_GEMINI_INTENCAO_WHATSAPP=1
    pytest tests/test_intencao_entrega_whatsapp_manual.py -q
"""
from __future__ import annotations

import os

import pytest

from app.services.canal_intencao_entrega_whatsapp_service import (
    DecisaoEntregaWhatsApp,
    decidir_enviar_para_meu_whatsapp,
)

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GEMINI_INTENCAO_WHATSAPP") != "1",
    reason="Teste manual. Defina RUN_GEMINI_INTENCAO_WHATSAPP=1 para chamar o Gemini.",
)

_CASOS = (
    ("Envie o resumo para meu WhatsApp.", DecisaoEntregaWhatsApp.POSITIVO),
    ("Manda isso para meu WhatsApp.", DecisaoEntregaWhatsApp.POSITIVO),
    ("Pode enviar essa resposta no WhatsApp?", DecisaoEntregaWhatsApp.POSITIVO),
    ("Quero receber isso no meu WhatsApp.", DecisaoEntregaWhatsApp.POSITIVO),
    ("Meu WhatsApp está conectado?", DecisaoEntregaWhatsApp.NEGATIVO),
    ("Explique como funciona o WhatsApp.", DecisaoEntregaWhatsApp.NEGATIVO),
    ("Não envie isso ao meu WhatsApp.", DecisaoEntregaWhatsApp.NEGATIVO),
    ("Envie isso para o WhatsApp do João.", DecisaoEntregaWhatsApp.NEGATIVO),
    ("Mande para 19 99999-9999.", DecisaoEntregaWhatsApp.NEGATIVO),
)


def test_frases_com_gemini_real():
    from google import genai
    from google.genai import types as genai_types

    from app.run_julia_chat import _api_key_label_chat, _get_chat_model_candidates

    chave = (os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY") or "").strip()
    if not chave:
        pytest.skip("GEMINI_API_KEY_1 ou GEMINI_API_KEY ausente.")
    client = genai.Client(api_key=chave, http_options=genai_types.HttpOptions(timeout=30_000))
    modelo = _get_chat_model_candidates()[0]
    divergencias = []
    for frase, esperado in _CASOS:
        obtido = decidir_enviar_para_meu_whatsapp(
            frase,
            agent="julia",
            flow_type="julia_chat",
            client=client,
            model=modelo,
            api_key_label=_api_key_label_chat(),
        )
        if obtido is not esperado:
            divergencias.append(f"{frase!r} esperado={esperado.value} obtido={obtido.value}")
    assert divergencias == []
