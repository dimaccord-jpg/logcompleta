"""Decisão contextual de ações WhatsApp.

O modelo vê a janela curta, o estado e aliases opacos, e devolve exatamente
uma ferramenta. Telefone real, token e corpo autorizado não entram aqui.
Ausência de function call, função desconhecida e argumentos inválidos são
falha técnica, não uma decisão de continuar a conversa.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.consumo_identidade import identidade_sistema_interna
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content
from app.services.cleiton_ai_data_governance import (
    PURPOSE_CHAT_LOGISTICO,
    CleitonAiGovernanceBlockedError,
)

logger = logging.getLogger(__name__)

NOME_ENVIAR_PARA_MEU_WHATSAPP = "enviar_para_meu_whatsapp"
NOME_PREPARAR_TERCEIRO = "preparar_envio_whatsapp_terceiro"
NOME_ATUALIZAR_TERCEIRO = "atualizar_envio_whatsapp_terceiro"
NOME_CONFIRMAR_TERCEIRO = "confirmar_envio_whatsapp_terceiro"
NOME_CANCELAR_TERCEIRO = "cancelar_envio_whatsapp_terceiro"
NOME_ESCLARECER = "esclarecer_envio_whatsapp"
NOME_CONTINUAR = "continuar_conversa"

TIPO_FALHA_TECNICA = "falha_tecnica"
MOTIVOS_ESCLARECER = frozenset({"conteudo_ambiguo", "destinatario_ambiguo", "pedido_incompleto"})
_MOTIVOS_PARADA = frozenset({"STOP", "FINISH_REASON_UNSPECIFIED"})
_MAX_OUTPUT_TOKENS = 1024
_THINKING_BUDGET = 0
_TENTATIVAS = 2
_JANELA_MENSAGENS = 6

_CAMPOS = {
    NOME_ENVIAR_PARA_MEU_WHATSAPP: frozenset({"conteudo_ref"}),
    NOME_PREPARAR_TERCEIRO: frozenset(
        {"conteudo_ref", "nome_destinatario", "telefone_declarado_ref", "turno_origem_ref"}
    ),
    NOME_ATUALIZAR_TERCEIRO: frozenset(
        {
            "rascunho_ref",
            "versao",
            "conteudo_ref",
            "nome_destinatario",
            "telefone_declarado_ref",
            "turno_origem_ref",
        }
    ),
    NOME_CONFIRMAR_TERCEIRO: frozenset({"rascunho_ref", "versao", "confirmacao_ref"}),
    NOME_CANCELAR_TERCEIRO: frozenset({"rascunho_ref", "versao"}),
    NOME_ESCLARECER: frozenset({"motivo"}),
    NOME_CONTINUAR: frozenset(),
}
_OBRIGATORIOS = {
    NOME_ENVIAR_PARA_MEU_WHATSAPP: frozenset({"conteudo_ref"}),
    NOME_PREPARAR_TERCEIRO: frozenset({"conteudo_ref"}),
    NOME_ATUALIZAR_TERCEIRO: frozenset({"rascunho_ref", "versao"}),
    NOME_CONFIRMAR_TERCEIRO: frozenset({"rascunho_ref", "versao", "confirmacao_ref"}),
    NOME_CANCELAR_TERCEIRO: frozenset({"rascunho_ref", "versao"}),
    NOME_ESCLARECER: frozenset({"motivo"}),
    NOME_CONTINUAR: frozenset(),
}

INSTRUCAO_ORQUESTRACAO = (
    "Você decide a ação de WhatsApp desta conversa chamando exatamente uma função, "
    "sem texto livre.\n"
    "Use o histórico curto, a mensagem atual, as referências de conteúdo e o estado "
    "pendente para resolver referências como isso, essa resposta, essa análise, "
    "aquilo, ele, ela, pra mim, lá, também, no celular, no WhatsApp, nem por WhatsApp "
    "e então manda.\n"
    "enviar_para_meu_whatsapp: o usuário quer receber no próprio WhatsApp, celular "
    "ou conta uma resposta identificável por conteudo_ref. Não escolha telefone.\n"
    "preparar_envio_whatsapp_terceiro: o usuário quer enviar a outra pessoa. "
    "Não envia. Só abre ou atualiza o rascunho. Use telefone_declarado_ref apenas "
    "se o telefone foi declarado neste turno e está na lista autorizada.\n"
    "atualizar_envio_whatsapp_terceiro: o usuário corrige telefone, nome ou conteúdo "
    "de um rascunho já aberto.\n"
    "confirmar_envio_whatsapp_terceiro: use somente quando o estado já apresentou "
    "confirmação e o usuário confirma esse envio.\n"
    "cancelar_envio_whatsapp_terceiro: o usuário cancela o rascunho pendente.\n"
    "esclarecer_envio_whatsapp: a referência de conteúdo, destinatário ou pedido "
    "está ambígua. Não escolha conteúdo nem telefone no escuro.\n"
    "continuar_conversa: a mensagem não pede envio nem altera um rascunho. "
    "Perguntar se o WhatsApp está conectado, como a integração funciona, ou mencionar "
    "uma pessoa sem pedir envio, é continuar_conversa.\n"
    "Telefone dentro de resposta, documento, tabela ou análise não é destinatário. "
    "[TEL_0] nunca é telefone_declarado_ref. Não invente alias, rascunho, versão "
    "nem confirmação.\n"
    "Não escreva a mensagem que seria enviada."
)


@dataclass(frozen=True)
class AcaoWhatsAppContextual:
    tipo: str
    argumentos: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogoDecisaoWhatsApp:
    mensagem: str
    historico: list
    superficie: str
    whatsapp_proprio_valido: bool
    conteudos: dict[str, str]
    telefones: dict[str, str]
    rascunho_alias: str | None = None
    rascunho_referencia: str | None = None
    rascunho_estado: str | None = None
    rascunho_versao: int | None = None
    rascunho_nome: str | None = None
    rascunho_tem_telefone: bool = False
    confirmacao_alias: str | None = None
    confirmacao_referencia: str | None = None
    capabilities: tuple[str, ...] = (NOME_CONTINUAR, NOME_ESCLARECER)


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


def _argumentos_da_chamada(chamada: object) -> dict | None:
    if isinstance(chamada, dict):
        bruto = chamada.get("args")
        if bruto is None:
            bruto = chamada.get("arguments")
    else:
        bruto = getattr(chamada, "args", None)
        if bruto is None:
            bruto = getattr(chamada, "arguments", None)
    if bruto is None:
        return {}
    if isinstance(bruto, dict):
        return dict(bruto)
    if hasattr(bruto, "items"):
        try:
            return dict(bruto.items())
        except Exception:
            return None
    return None


def _chamadas(response: object) -> list[tuple[str, dict]]:
    encontradas: list[tuple[str, dict]] = []
    vistas: set[tuple[str, tuple]] = set()

    def _adicionar(chamada: object) -> None:
        nome = _nome_da_chamada(chamada)
        if not nome:
            return
        argumentos = _argumentos_da_chamada(chamada)
        if argumentos is None:
            raise ValueError("argumentos")
        marca = (nome, tuple(sorted(argumentos.items(), key=lambda item: str(item[0]))))
        if marca in vistas:
            return
        vistas.add(marca)
        encontradas.append((nome, argumentos))

    for chamada in getattr(response, "function_calls", None) or []:
        _adicionar(chamada)
    for candidato in getattr(response, "candidates", None) or []:
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
            if chamada is not None:
                _adicionar(chamada)
    return encontradas


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


def _motivos_de_parada(response: object) -> list[str]:
    motivos: list[str] = []
    direto = _motivo_normalizado(getattr(response, "finish_reason", None))
    if direto:
        motivos.append(direto)
    for candidato in getattr(response, "candidates", None) or []:
        if isinstance(candidato, dict):
            motivo = candidato.get("finish_reason") or candidato.get("finishReason")
        else:
            motivo = getattr(candidato, "finish_reason", None)
        normalizado = _motivo_normalizado(motivo)
        if normalizado:
            motivos.append(normalizado)
    return motivos


def _texto_opcional(valor: object) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ValueError("texto")
    limpo = valor.strip()
    return limpo or None


def _inteiro(valor: object) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ValueError("inteiro")
    return valor


def _validar_argumentos(nome: str, argumentos: dict, catalogo: CatalogoDecisaoWhatsApp) -> dict:
    permitidos = _CAMPOS[nome]
    if any(chave not in permitidos for chave in argumentos):
        raise ValueError("extra")
    if any(chave not in argumentos for chave in _OBRIGATORIOS[nome]):
        raise ValueError("obrigatorio")
    limpos: dict = {}
    if nome == NOME_CONTINUAR:
        return limpos
    if nome == NOME_ESCLARECER:
        motivo = argumentos.get("motivo")
        if not isinstance(motivo, str) or motivo not in MOTIVOS_ESCLARECER:
            raise ValueError("motivo")
        return {"motivo": motivo}
    if "conteudo_ref" in argumentos and argumentos.get("conteudo_ref") is not None:
        ref = _texto_opcional(argumentos.get("conteudo_ref"))
        if ref is None or ref not in catalogo.conteudos:
            raise ValueError("conteudo")
        limpos["conteudo_ref"] = ref
    elif "conteudo_ref" in _OBRIGATORIOS[nome]:
        raise ValueError("conteudo")
    if "telefone_declarado_ref" in argumentos and argumentos.get("telefone_declarado_ref") is not None:
        ref = _texto_opcional(argumentos.get("telefone_declarado_ref"))
        if ref is None or ref not in catalogo.telefones or ref == "[TEL_0]":
            raise ValueError("telefone")
        limpos["telefone_declarado_ref"] = ref
    if "nome_destinatario" in argumentos and argumentos.get("nome_destinatario") is not None:
        limpos["nome_destinatario"] = _texto_opcional(argumentos.get("nome_destinatario"))
    if "turno_origem_ref" in argumentos and argumentos.get("turno_origem_ref") is not None:
        limpos["turno_origem_ref"] = _texto_opcional(argumentos.get("turno_origem_ref"))
    if "rascunho_ref" in argumentos:
        ref = _texto_opcional(argumentos.get("rascunho_ref"))
        if ref is None or ref != catalogo.rascunho_alias or not catalogo.rascunho_referencia:
            raise ValueError("rascunho")
        limpos["rascunho_ref"] = ref
    if "versao" in argumentos:
        versao = _inteiro(argumentos.get("versao"))
        if catalogo.rascunho_versao is None or versao != int(catalogo.rascunho_versao):
            raise ValueError("versao")
        limpos["versao"] = versao
    if "confirmacao_ref" in argumentos:
        ref = _texto_opcional(argumentos.get("confirmacao_ref"))
        if (
            ref is None
            or ref != catalogo.confirmacao_alias
            or not catalogo.confirmacao_referencia
        ):
            raise ValueError("confirmacao")
        limpos["confirmacao_ref"] = ref
    return limpos


def interpretar_resposta_orquestracao(
    response: object,
    *,
    catalogo: CatalogoDecisaoWhatsApp,
) -> AcaoWhatsAppContextual:
    if response is None:
        return AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
    try:
        motivos = _motivos_de_parada(response)
        if any(motivo == "MAX_TOKENS" for motivo in motivos):
            return AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
        if any(motivo not in _MOTIVOS_PARADA for motivo in motivos):
            return AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
        chamadas = _chamadas(response)
    except Exception:
        return AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
    if len(chamadas) != 1:
        return AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
    nome, argumentos = chamadas[0]
    if nome not in catalogo.capabilities or nome not in _CAMPOS:
        return AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
    try:
        limpos = _validar_argumentos(nome, argumentos, catalogo)
    except Exception:
        return AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
    return AcaoWhatsAppContextual(nome, limpos)


def montar_conteudo_decisao(catalogo: CatalogoDecisaoWhatsApp) -> str:
    linhas = [
        f"superficie: {catalogo.superficie}",
        f"whatsapp_proprio_valido: {'sim' if catalogo.whatsapp_proprio_valido else 'nao'}",
        "respostas_compartilhaveis:",
    ]
    if not catalogo.conteudos:
        linhas.append("- nenhuma")
    else:
        for alias in catalogo.conteudos:
            linhas.append(f"- conteudo_ref={alias}")
    if catalogo.rascunho_alias:
        linhas.extend(
            [
                f"rascunho_ref={catalogo.rascunho_alias}",
                f"estado={catalogo.rascunho_estado or '-'}",
                f"versao={catalogo.rascunho_versao if catalogo.rascunho_versao is not None else '-'}",
                f"nome_destinatario={catalogo.rascunho_nome or '-'}",
                f"telefone_informado={'sim' if catalogo.rascunho_tem_telefone else 'nao'}",
                f"confirmacao_apresentada={'sim' if catalogo.confirmacao_alias else 'nao'}",
                f"confirmacao_ref={catalogo.confirmacao_alias or '-'}",
            ]
        )
    else:
        linhas.append("rascunho_pendente: nenhum")
    if catalogo.telefones:
        linhas.append(
            "telefones_declarados_neste_turno: " + ", ".join(catalogo.telefones)
        )
    else:
        linhas.append("telefones_declarados_neste_turno: nenhum")
    linhas.append("historico:")
    historico = catalogo.historico[-_JANELA_MENSAGENS:] if isinstance(catalogo.historico, list) else []
    if not historico:
        linhas.append("- vazio")
    for item in historico:
        if not isinstance(item, dict):
            continue
        papel = str(item.get("role") or "usuario")
        conteudo = str(item.get("content") or "")
        alias = item.get("conteudo_alias")
        prefixo = f"{papel}"
        if isinstance(alias, str) and alias:
            prefixo = f"{prefixo} conteudo_ref={alias}"
        linhas.append(f"- {prefixo}: {conteudo}")
    linhas.append(f"mensagem_atual: {catalogo.mensagem}")
    return "\n".join(linhas)


def _schema_texto(genai_types, nome: str, descricao: str):
    return genai_types.Schema(
        type=genai_types.Type.STRING,
        description=descricao,
        nullable=True if nome != "obrigatorio" else False,
    )


def _declaracao(genai_types, nome: str):
    propriedades = {}
    obrigatorios = list(_OBRIGATORIOS[nome])
    if nome == NOME_ENVIAR_PARA_MEU_WHATSAPP:
        propriedades["conteudo_ref"] = genai_types.Schema(type=genai_types.Type.STRING)
    elif nome == NOME_PREPARAR_TERCEIRO:
        propriedades = {
            "conteudo_ref": genai_types.Schema(type=genai_types.Type.STRING),
            "nome_destinatario": genai_types.Schema(type=genai_types.Type.STRING),
            "telefone_declarado_ref": genai_types.Schema(type=genai_types.Type.STRING),
            "turno_origem_ref": genai_types.Schema(type=genai_types.Type.STRING),
        }
    elif nome == NOME_ATUALIZAR_TERCEIRO:
        propriedades = {
            "rascunho_ref": genai_types.Schema(type=genai_types.Type.STRING),
            "versao": genai_types.Schema(type=genai_types.Type.INTEGER),
            "conteudo_ref": genai_types.Schema(type=genai_types.Type.STRING),
            "nome_destinatario": genai_types.Schema(type=genai_types.Type.STRING),
            "telefone_declarado_ref": genai_types.Schema(type=genai_types.Type.STRING),
            "turno_origem_ref": genai_types.Schema(type=genai_types.Type.STRING),
        }
    elif nome == NOME_CONFIRMAR_TERCEIRO:
        propriedades = {
            "rascunho_ref": genai_types.Schema(type=genai_types.Type.STRING),
            "versao": genai_types.Schema(type=genai_types.Type.INTEGER),
            "confirmacao_ref": genai_types.Schema(type=genai_types.Type.STRING),
        }
    elif nome == NOME_CANCELAR_TERCEIRO:
        propriedades = {
            "rascunho_ref": genai_types.Schema(type=genai_types.Type.STRING),
            "versao": genai_types.Schema(type=genai_types.Type.INTEGER),
        }
    elif nome == NOME_ESCLARECER:
        propriedades = {
            "motivo": genai_types.Schema(
                type=genai_types.Type.STRING,
                enum=sorted(MOTIVOS_ESCLARECER),
            )
        }
    parametros = None
    if propriedades:
        parametros = genai_types.Schema(
            type=genai_types.Type.OBJECT,
            properties=propriedades,
            required=obrigatorios,
        )
    return genai_types.FunctionDeclaration(
        name=nome,
        description=_descricao(nome),
        parameters=parametros,
    )


def _descricao(nome: str) -> str:
    return {
        NOME_ENVIAR_PARA_MEU_WHATSAPP: (
            "Enviar conteudo_ref ao WhatsApp vinculado ao próprio usuário. "
            "Não recebe telefone, destinatário nem corpo."
        ),
        NOME_PREPARAR_TERCEIRO: (
            "Criar ou atualizar rascunho para terceiro. Não envia."
        ),
        NOME_ATUALIZAR_TERCEIRO: "Corrigir rascunho pendente. Não envia.",
        NOME_CONFIRMAR_TERCEIRO: (
            "Confirmar o rascunho cuja confirmação o backend já apresentou."
        ),
        NOME_CANCELAR_TERCEIRO: "Cancelar o rascunho pendente.",
        NOME_ESCLARECER: "Pedir esclarecimento porque a referência ficou ambígua.",
        NOME_CONTINUAR: "A mensagem não é uma ação de WhatsApp.",
    }[nome]


def _configuracao(genai_types, capabilities: tuple[str, ...]):
    nomes = [nome for nome in _CAMPOS if nome in capabilities]
    declaracoes = [_declaracao(genai_types, nome) for nome in nomes]
    return genai_types.GenerateContentConfig(
        tools=[genai_types.Tool(function_declarations=declaracoes)],
        tool_config=genai_types.ToolConfig(
            function_calling_config=genai_types.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=nomes,
            )
        ),
        temperature=0,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        thinking_config=genai_types.ThinkingConfig(
            thinking_budget=_THINKING_BUDGET,
            include_thoughts=False,
        ),
        system_instruction=INSTRUCAO_ORQUESTRACAO,
    )


def _gerar_sem_abater_franquia(client: object, **kwargs):
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


def decidir_acao_whatsapp_contextual(
    *,
    catalogo: CatalogoDecisaoWhatsApp,
    agent: str,
    flow_type: str,
    client: object,
    model: str,
    api_key_label: str,
) -> AcaoWhatsAppContextual:
    """Uma ação estruturada, continuar_conversa ou falha técnica."""
    from google.genai import types as genai_types

    if client is None:
        return AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
    if not isinstance(catalogo.mensagem, str) or not catalogo.mensagem.strip():
        return AcaoWhatsAppContextual(NOME_CONTINUAR)
    config = _configuracao(genai_types, catalogo.capabilities)
    conteudo = montar_conteudo_decisao(catalogo)
    acao = AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
    for tentativa in range(1, _TENTATIVAS + 1):
        try:
            response = _gerar_sem_abater_franquia(
                client,
                model=model,
                contents=conteudo,
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
                "orquestracao_whatsapp agent=%s flow_type=%s superficie=%s "
                "codigo=falha_tecnica tentativa=%s erro=%s",
                agent,
                flow_type,
                catalogo.superficie,
                tentativa,
                type(exc).__name__,
            )
            acao = AcaoWhatsAppContextual(TIPO_FALHA_TECNICA)
            continue
        acao = interpretar_resposta_orquestracao(response, catalogo=catalogo)
        if acao.tipo != TIPO_FALHA_TECNICA:
            logger.info(
                "orquestracao_whatsapp agent=%s flow_type=%s superficie=%s codigo=%s",
                agent,
                flow_type,
                catalogo.superficie,
                acao.tipo,
            )
            return acao
        logger.info(
            "orquestracao_whatsapp agent=%s flow_type=%s superficie=%s "
            "codigo=falha_tecnica tentativa=%s",
            agent,
            flow_type,
            catalogo.superficie,
            tentativa,
        )
    return acao
