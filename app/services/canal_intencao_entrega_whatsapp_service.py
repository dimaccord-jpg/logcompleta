"""Decisão estruturada: o usuário pediu para enviar a resposta anterior ao próprio WhatsApp.

A função não recebe telefone, texto nem user_id. O backend ignora argumentos.
Ausência de function call, truncamento e erro do provedor são falha técnica,
não uma decisão negativa.
"""
from __future__ import annotations

import enum
import logging

from app.consumo_identidade import identidade_sistema_interna
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.cleiton_ai_data_governance import (
    PURPOSE_CHAT_LOGISTICO,
    CleitonAiGovernanceBlockedError,
)

logger = logging.getLogger(__name__)

NOME_ENVIAR_PARA_MEU_WHATSAPP = "enviar_para_meu_whatsapp"
NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP = "nao_enviar_para_meu_whatsapp"
_FUNCOES_DECISAO = frozenset(
    {
        NOME_ENVIAR_PARA_MEU_WHATSAPP,
        NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP,
    }
)
_MOTIVOS_ACEITOS = frozenset({"STOP", "FINISH_REASON_UNSPECIFIED"})
_MAX_OUTPUT_TOKENS = 256
_THINKING_BUDGET_DESLIGADO = 0
_TENTATIVAS_CLASSIFICACAO = 2

DESCRICAO_ENVIAR_PARA_MEU_WHATSAPP = (
    "O usuário pediu para enviar a resposta anterior do assistente "
    "ao WhatsApp vinculado à própria conta."
)
DESCRICAO_NAO_ENVIAR_PARA_MEU_WHATSAPP = (
    "O usuário não pediu esse envio. Inclui apenas mencionar WhatsApp, "
    "perguntar se está conectado, perguntar como a integração funciona, "
    "negar o envio, pedir envio a outra pessoa ou indicar telefone de terceiro "
    "ou número digitado na própria mensagem."
)

INSTRUCAO_CLASSIFICADOR = (
    "Classifique o pedido chamando exatamente uma função, sem texto livre.\n"
    "Classifique como ENVIO, chamando enviar_para_meu_whatsapp, quando o usuário "
    "solicitar que a resposta anterior do assistente seja enviada ao WhatsApp "
    "vinculado à própria conta.\n"
    "Exemplos de ENVIO:\n"
    "- Envie o resumo para meu WhatsApp.\n"
    "- Manda isso para meu WhatsApp.\n"
    "- Pode enviar essa resposta no WhatsApp?\n"
    "- Quero receber isso no meu WhatsApp.\n"
    "- Me manda essa análise no WhatsApp.\n"
    "Classifique como NÃO ENVIO, chamando nao_enviar_para_meu_whatsapp, quando o "
    "usuário apenas mencionar WhatsApp, perguntar se o WhatsApp está conectado, "
    "perguntar como funciona a integração, negar explicitamente o envio, pedir "
    "envio para outra pessoa, informar ou indicar telefone de terceiro, ou pedir "
    "envio para número digitado na própria mensagem.\n"
    "Exemplos de NÃO ENVIO:\n"
    "- Meu WhatsApp está conectado?\n"
    "- Como funciona o WhatsApp do AgenteFrete?\n"
    "- Explique como funciona o WhatsApp.\n"
    "- Não envie isso para meu WhatsApp.\n"
    "- Não envie isso ao meu WhatsApp.\n"
    "- Envie para o WhatsApp do João.\n"
    "- Envie isso para o WhatsApp do João.\n"
    "- Mande para 19 99999-9999.\n"
    "Não escreva texto. Não informe telefone, destinatário nem conteúdo."
)


class DecisaoEntregaWhatsApp(enum.Enum):
    POSITIVO = "positivo"
    NEGATIVO = "negativo"
    FALHA_TECNICA = "falha_tecnica"


def _nome_da_chamada(chamada: object) -> str | None:
    if chamada is None:
        return None
    if isinstance(chamada, dict):
        nome = chamada.get("name")
    else:
        nome = getattr(chamada, "name", None)
    if isinstance(nome, str) and nome.strip():
        return nome.strip()
    return None


def _motivo_normalizado(motivo: object) -> str | None:
    if motivo is None:
        return None
    nome = getattr(motivo, "name", None)
    if isinstance(nome, str) and nome:
        return nome
    valor = getattr(motivo, "value", None)
    if isinstance(valor, str) and valor:
        return valor
    if isinstance(motivo, str) and motivo.strip():
        return motivo.strip()
    return None


def _motivos_de_parada(response: object) -> list[object]:
    motivos: list[object] = []
    direto = getattr(response, "finish_reason", None)
    if direto is not None:
        motivos.append(direto)
    candidatos = getattr(response, "candidates", None) or []
    for candidato in candidatos:
        if isinstance(candidato, dict):
            motivo = candidato.get("finish_reason") or candidato.get("finishReason")
        else:
            motivo = getattr(candidato, "finish_reason", None)
        if motivo is not None:
            motivos.append(motivo)
    return motivos


def _nomes_de_chamada(response: object) -> set[str]:
    nomes: set[str] = set()
    chamadas = getattr(response, "function_calls", None) or []
    for chamada in chamadas:
        nome = _nome_da_chamada(chamada)
        if nome:
            nomes.add(nome)
    candidatos = getattr(response, "candidates", None) or []
    for candidato in candidatos:
        if isinstance(candidato, dict):
            conteudo = candidato.get("content")
        else:
            conteudo = getattr(candidato, "content", None)
        if isinstance(conteudo, dict):
            partes = conteudo.get("parts") or []
        else:
            partes = getattr(conteudo, "parts", None) or []
        for parte in partes:
            if isinstance(parte, dict):
                chamada = parte.get("function_call") or parte.get("functionCall")
            else:
                chamada = getattr(parte, "function_call", None)
            nome = _nome_da_chamada(chamada)
            if nome:
                nomes.add(nome)
    return nomes


def _interpretar_resposta_classificacao(response: object) -> DecisaoEntregaWhatsApp:
    """Mapeia a resposta do provedor. Não converte omissão em decisão negativa."""
    if response is None:
        return DecisaoEntregaWhatsApp.FALHA_TECNICA
    try:
        motivos = [_motivo_normalizado(motivo) for motivo in _motivos_de_parada(response)]
        if any(motivo == "MAX_TOKENS" for motivo in motivos):
            return DecisaoEntregaWhatsApp.FALHA_TECNICA
        if any(motivo not in _MOTIVOS_ACEITOS for motivo in motivos):
            return DecisaoEntregaWhatsApp.FALHA_TECNICA
        nomes = _nomes_de_chamada(response)
    except Exception:
        return DecisaoEntregaWhatsApp.FALHA_TECNICA
    if not nomes or any(nome not in _FUNCOES_DECISAO for nome in nomes) or len(nomes) != 1:
        return DecisaoEntregaWhatsApp.FALHA_TECNICA
    if NOME_ENVIAR_PARA_MEU_WHATSAPP in nomes:
        return DecisaoEntregaWhatsApp.POSITIVO
    if NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP in nomes:
        return DecisaoEntregaWhatsApp.NEGATIVO
    return DecisaoEntregaWhatsApp.FALHA_TECNICA


def _configuracao_classificacao(genai_types: object):
    enviar = genai_types.FunctionDeclaration(
        name=NOME_ENVIAR_PARA_MEU_WHATSAPP,
        description=DESCRICAO_ENVIAR_PARA_MEU_WHATSAPP,
    )
    nao_enviar = genai_types.FunctionDeclaration(
        name=NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP,
        description=DESCRICAO_NAO_ENVIAR_PARA_MEU_WHATSAPP,
    )
    return genai_types.GenerateContentConfig(
        tools=[genai_types.Tool(function_declarations=[enviar, nao_enviar])],
        tool_config=genai_types.ToolConfig(
            function_calling_config=genai_types.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=[
                    NOME_ENVIAR_PARA_MEU_WHATSAPP,
                    NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP,
                ],
            )
        ),
        temperature=0,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        thinking_config=genai_types.ThinkingConfig(
            thinking_budget=_THINKING_BUDGET_DESLIGADO,
            include_thoughts=False,
        ),
        system_instruction=INSTRUCAO_CLASSIFICADOR,
    )


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


def _classificar_uma_vez(
    *,
    client: object,
    model: str,
    texto: str,
    config: object,
    agent: str,
    flow_type: str,
    api_key_label: str,
    tentativa: int,
) -> DecisaoEntregaWhatsApp:
    try:
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
    except CleitonAiGovernanceBlockedError:
        raise
    except Exception as exc:
        logger.info(
            "intencao_entrega_whatsapp agent=%s flow_type=%s decisao=falha_tecnica "
            "tentativa=%s erro=%s",
            agent,
            flow_type,
            tentativa,
            type(exc).__name__,
        )
        return DecisaoEntregaWhatsApp.FALHA_TECNICA
    decisao = _interpretar_resposta_classificacao(response)
    if decisao is DecisaoEntregaWhatsApp.FALHA_TECNICA:
        logger.info(
            "intencao_entrega_whatsapp agent=%s flow_type=%s decisao=falha_tecnica tentativa=%s",
            agent,
            flow_type,
            tentativa,
        )
    return decisao


def decidir_enviar_para_meu_whatsapp(
    mensagem: str,
    *,
    agent: str,
    flow_type: str,
    client: object,
    model: str,
    api_key_label: str,
) -> DecisaoEntregaWhatsApp:
    """Positivo, negativo ou falha técnica. Não escolhe telefone nem destinatário."""
    from google.genai import types as genai_types

    texto = (mensagem or "").strip()
    if not texto:
        return DecisaoEntregaWhatsApp.NEGATIVO
    if client is None:
        return DecisaoEntregaWhatsApp.FALHA_TECNICA
    config = _configuracao_classificacao(genai_types)
    decisao = DecisaoEntregaWhatsApp.FALHA_TECNICA
    for tentativa in range(1, _TENTATIVAS_CLASSIFICACAO + 1):
        decisao = _classificar_uma_vez(
            client=client,
            model=model,
            texto=texto,
            config=config,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            tentativa=tentativa,
        )
        if decisao is not DecisaoEntregaWhatsApp.FALHA_TECNICA:
            return decisao
    return DecisaoEntregaWhatsApp.FALHA_TECNICA
