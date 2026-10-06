"""Decisão estruturada: o usuário pediu para enviar a resposta anterior ao próprio WhatsApp.

A função não recebe telefone, texto nem user_id. O backend ignora argumentos.
"""
from __future__ import annotations

import logging

from app.consumo_identidade import identidade_sistema_interna
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.cleiton_ai_data_governance import PURPOSE_CHAT_LOGISTICO

logger = logging.getLogger(__name__)

NOME_ENVIAR_PARA_MEU_WHATSAPP = "enviar_para_meu_whatsapp"
DESCRICAO_ENVIAR_PARA_MEU_WHATSAPP = (
    "Use quando o usuário solicitar que a resposta anterior do assistente "
    "seja enviada ao WhatsApp vinculado à própria conta."
)


def _chamou_funcao(response: object, nome: str) -> bool:
    chamadas = getattr(response, "function_calls", None) or []
    for chamada in chamadas:
        if getattr(chamada, "name", None) == nome:
            return True
    for candidato in getattr(response, "candidates", None) or []:
        conteudo = getattr(candidato, "content", None)
        for parte in getattr(conteudo, "parts", None) or []:
            chamada = getattr(parte, "function_call", None)
            if chamada is not None and getattr(chamada, "name", None) == nome:
                return True
    return False


def _gerar_classificacao_sem_abater_franquia(client: object, **kwargs):
    """A decisão de roteamento é infraestrutura. O evento técnico permanece; a franquia do cliente não."""
    from flask import g, has_request_context

    if not has_request_context():
        return cleiton_governed_generate_content(client, **kwargs)
    tinha = hasattr(g, "identidade")
    anterior = getattr(g, "identidade", None) if tinha else None
    g.identidade = identidade_sistema_interna("interno_nao_faturavel")
    try:
        return cleiton_governed_generate_content(client, **kwargs)
    finally:
        if tinha:
            g.identidade = anterior
        elif hasattr(g, "identidade"):
            delattr(g, "identidade")


def decidir_enviar_para_meu_whatsapp(
    mensagem: str,
    *,
    agent: str,
    flow_type: str,
    client: object,
    model: str,
    api_key_label: str,
) -> bool:
    """True somente se o modelo governado chamar enviar_para_meu_whatsapp."""
    from google.genai import types as genai_types

    texto = (mensagem or "").strip()
    if not texto or client is None:
        return False
    declaracao = genai_types.FunctionDeclaration(
        name=NOME_ENVIAR_PARA_MEU_WHATSAPP,
        description=DESCRICAO_ENVIAR_PARA_MEU_WHATSAPP,
    )
    config = genai_types.GenerateContentConfig(
        tools=[genai_types.Tool(function_declarations=[declaracao])],
        temperature=0,
        max_output_tokens=32,
        system_instruction=(
            "Decida apenas se o usuário pediu para enviar a resposta anterior "
            "do assistente ao WhatsApp da própria conta. "
            "Se pediu, chame enviar_para_meu_whatsapp e não escreva texto. "
            "Se não pediu, responda apenas nao. "
            "Não informe telefone, destinatário nem conteúdo."
        ),
    )
    response = _gerar_classificacao_sem_abater_franquia(
        client,
        model=model,
        contents=texto,
        config=config,
        agent=agent,
        flow_type=flow_type,
        api_key_label=api_key_label,
        purpose=PURPOSE_CHAT_LOGISTICO,
    )
    return _chamou_funcao(response, NOME_ENVIAR_PARA_MEU_WHATSAPP)
