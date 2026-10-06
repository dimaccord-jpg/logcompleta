"""Executa a ação estruturada. O modelo não escolhe destino nem reescreve o corpo."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.models import RascunhoEntregaWhatsApp, SolicitacaoEntregaCanal
from app.services.canal_orquestracao_whatsapp_contextual_service import (
    NOME_ATUALIZAR_TERCEIRO,
    NOME_CANCELAR_TERCEIRO,
    NOME_CONFIRMAR_TERCEIRO,
    NOME_CONTINUAR,
    NOME_ENVIAR_PARA_MEU_WHATSAPP,
    NOME_ESCLARECER,
    NOME_PREPARAR_TERCEIRO,
    TIPO_FALHA_TECNICA,
    AcaoWhatsAppContextual,
    CatalogoDecisaoWhatsApp,
)
from app.services.canal_rascunho_entrega_whatsapp_service import (
    EfeitoRascunho,
    atualizar_rascunho,
    cancelar_rascunho,
    confirmar_rascunho,
    preparar_rascunho,
    rascunho_aberto,
    trava_rascunho,
)
from app.services.canal_resposta_compartilhavel_service import (
    hash_conteudo,
    listar_respostas_recentes,
    resolver_resposta_compartilhavel,
)
from app.services.canal_telefone_declarado_service import (
    alias_telefones_do_turno,
    normalizar_telefone_e164,
    ocultar_telefones,
)

ALIAS_RASCUNHO = "[RASCUNHO_1]"
ALIAS_CONFIRMACAO = "[CONFIRMACAO_1]"
MENSAGENS_ESCLARECER = {
    "conteudo_ambiguo": "Qual resposta você quer que eu envie?",
    "destinatario_ambiguo": "Para quem devo enviar essa resposta?",
    "pedido_incompleto": "Pode completar o pedido de envio?",
}
MENSAGEM_FALHA = "Não consegui processar o pedido de envio para o WhatsApp agora. Tente novamente."


@dataclass(frozen=True)
class EfeitoExecucao:
    codigo: str
    mensagem: str | None = None
    continuar: bool = False
    texto_proprio: str | None = None
    terceiro: EfeitoRascunho | None = None


def _alias_conteudo(indice: int) -> str:
    return f"[CONTEUDO_{int(indice)}]"


def _user_id(usuario: object) -> int | None:
    identificador = getattr(usuario, "id", None)
    if isinstance(identificador, bool) or not isinstance(identificador, int) or identificador <= 0:
        return None
    return identificador


def _chave_preparo(user_id: int, superficie: str, contexto: str, identidade: str | None) -> str | None:
    if not isinstance(identidade, str) or not identidade.strip():
        return None
    material = f"{int(user_id)}|{superficie}|{contexto}|{identidade.strip()}|preparar"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _capabilities(respostas: bool, rascunho: RascunhoEntregaWhatsApp | None) -> tuple[str, ...]:
    nomes = [NOME_CONTINUAR, NOME_ESCLARECER]
    if respostas:
        nomes.extend([NOME_ENVIAR_PARA_MEU_WHATSAPP, NOME_PREPARAR_TERCEIRO])
    if rascunho is not None and rascunho.estado in {
        RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_TELEFONE,
        RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO,
        RascunhoEntregaWhatsApp.ESTADO_BLOQUEADO_ELEGIBILIDADE,
    }:
        nomes.extend([NOME_ATUALIZAR_TERCEIRO, NOME_CANCELAR_TERCEIRO])
    if (
        rascunho is not None
        and rascunho.estado == RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO
        and rascunho.confirmacao_referencia
    ):
        nomes.append(NOME_CONFIRMAR_TERCEIRO)
    if rascunho is not None and rascunho.estado == RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO:
        nomes.append(NOME_CONFIRMAR_TERCEIRO)
    return tuple(dict.fromkeys(nomes))


def construir_catalogo(
    *,
    usuario: object,
    superficie: str,
    contexto_conversa: str,
    mensagem: str,
    historico: object,
    whatsapp_proprio_valido: bool,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
) -> CatalogoDecisaoWhatsApp | None:
    user_id = _user_id(usuario)
    if user_id is None:
        return None
    recentes = listar_respostas_recentes(
        user_id=user_id,
        superficie=superficie,
        contexto_conversa=contexto_conversa,
        comparison_id=comparison_id,
        escopo_auditoria=escopo_auditoria,
    )
    conteudos: dict[str, str] = {}
    por_hash: dict[str, str] = {}
    for indice, row in enumerate(recentes, start=1):
        alias = _alias_conteudo(indice)
        conteudos[alias] = row.referencia
        por_hash[row.content_hash] = alias
    mensagem_modelo, declarados = alias_telefones_do_turno(mensagem if isinstance(mensagem, str) else "")
    telefones = {item.alias: item.original for item in declarados}
    historico_modelo: list[dict] = []
    if isinstance(historico, list):
        for item in historico[-6:]:
            if not isinstance(item, dict):
                continue
            bruto = item.get("content")
            if not isinstance(bruto, str):
                bruto = item.get("answer")
            if not isinstance(bruto, str):
                continue
            texto = ocultar_telefones(bruto.strip())
            if not texto:
                continue
            alias = por_hash.get(hash_conteudo(bruto.strip()))
            historico_modelo.append(
                {
                    "role": str(item.get("role") or "usuario"),
                    "content": texto[:2000],
                    "conteudo_alias": alias,
                }
            )
    pendente = rascunho_aberto(
        user_id=user_id,
        superficie=superficie,
        contexto_conversa=contexto_conversa,
    )
    return CatalogoDecisaoWhatsApp(
        mensagem=mensagem_modelo,
        historico=historico_modelo,
        superficie=superficie,
        whatsapp_proprio_valido=bool(whatsapp_proprio_valido),
        conteudos=conteudos,
        telefones=telefones,
        rascunho_alias=ALIAS_RASCUNHO if pendente is not None else None,
        rascunho_referencia=pendente.referencia if pendente is not None else None,
        rascunho_estado=pendente.estado if pendente is not None else None,
        rascunho_versao=int(pendente.versao) if pendente is not None else None,
        rascunho_nome=pendente.nome_destinatario if pendente is not None else None,
        rascunho_tem_telefone=bool(pendente is not None and pendente.telefone_e164),
        confirmacao_alias=ALIAS_CONFIRMACAO if pendente is not None and pendente.confirmacao_referencia else None,
        confirmacao_referencia=pendente.confirmacao_referencia if pendente is not None else None,
        capabilities=_capabilities(bool(conteudos), pendente),
    )


def _conteudo(catalogo: CatalogoDecisaoWhatsApp, alias: str, *, user_id: int, superficie: str, contexto: str, comparison_id: str | None, escopo: str | None):
    referencia = catalogo.conteudos.get(alias)
    if not referencia:
        return None
    return resolver_resposta_compartilhavel(
        user_id=user_id,
        superficie=superficie,
        contexto_conversa=contexto,
        referencia=referencia,
        comparison_id=comparison_id,
        escopo_auditoria=escopo,
    )


def _telefone(catalogo: CatalogoDecisaoWhatsApp, argumentos: dict) -> tuple[str | None, str | None, str | None, str | None]:
    alias = argumentos.get("telefone_declarado_ref")
    if not alias:
        return None, None, None, None
    original = catalogo.telefones.get(alias)
    if not original:
        return "invalido", None, None, None
    normalizado = normalizar_telefone_e164(original)
    if normalizado.codigo != "ok":
        return normalizado.codigo, None, None, alias
    return "ok", normalizado.e164, normalizado.exibicao, alias


def executar_acao_whatsapp(
    acao: AcaoWhatsAppContextual,
    *,
    catalogo: CatalogoDecisaoWhatsApp,
    usuario: object,
    superficie: str,
    contexto_conversa: str,
    identidade_requisicao: str | None,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
) -> EfeitoExecucao:
    user_id = _user_id(usuario)
    if user_id is None or acao.tipo == TIPO_FALHA_TECNICA:
        return EfeitoExecucao(codigo="falha_tecnica", mensagem=MENSAGEM_FALHA)
    if acao.tipo == NOME_CONTINUAR:
        return EfeitoExecucao(codigo="continuar", continuar=True)
    if acao.tipo == NOME_ESCLARECER:
        motivo = acao.argumentos.get("motivo")
        return EfeitoExecucao(
            codigo="esclarecimento",
            mensagem=MENSAGENS_ESCLARECER.get(motivo, MENSAGENS_ESCLARECER["pedido_incompleto"]),
        )
    if acao.tipo == NOME_ENVIAR_PARA_MEU_WHATSAPP:
        resposta = _conteudo(
            catalogo,
            acao.argumentos.get("conteudo_ref") or "",
            user_id=user_id,
            superficie=superficie,
            contexto=contexto_conversa,
            comparison_id=comparison_id,
            escopo=escopo_auditoria,
        )
        if resposta is None:
            return EfeitoExecucao(codigo="falha_tecnica", mensagem=MENSAGEM_FALHA)
        return EfeitoExecucao(codigo="enviar_proprio", texto_proprio=resposta.texto)
    if acao.tipo in {NOME_PREPARAR_TERCEIRO, NOME_ATUALIZAR_TERCEIRO, NOME_CONFIRMAR_TERCEIRO, NOME_CANCELAR_TERCEIRO}:
        if not catalogo.rascunho_referencia and acao.tipo != NOME_PREPARAR_TERCEIRO:
            return EfeitoExecucao(codigo="falha_tecnica", mensagem=MENSAGEM_FALHA)
        referencia_trava = catalogo.rascunho_referencia or f"preparo:{user_id}:{contexto_conversa}"
        with trava_rascunho(referencia_trava):
            return _executar_terceiro(
                acao,
                catalogo=catalogo,
                user_id=user_id,
                superficie=superficie,
                contexto_conversa=contexto_conversa,
                identidade_requisicao=identidade_requisicao,
                comparison_id=comparison_id,
                escopo_auditoria=escopo_auditoria,
            )
    return EfeitoExecucao(codigo="falha_tecnica", mensagem=MENSAGEM_FALHA)


def _executar_terceiro(
    acao: AcaoWhatsAppContextual,
    *,
    catalogo: CatalogoDecisaoWhatsApp,
    user_id: int,
    superficie: str,
    contexto_conversa: str,
    identidade_requisicao: str | None,
    comparison_id: str | None,
    escopo_auditoria: str | None,
) -> EfeitoExecucao:
    if acao.tipo == NOME_CANCELAR_TERCEIRO:
        efeito = cancelar_rascunho(
            user_id=user_id,
            superficie=superficie,
            contexto_conversa=contexto_conversa,
            referencia=catalogo.rascunho_referencia or "",
            versao=int(acao.argumentos["versao"]),
        )
        return EfeitoExecucao(codigo=efeito.codigo, mensagem=efeito.mensagem, terceiro=efeito)
    if acao.tipo == NOME_CONFIRMAR_TERCEIRO:
        efeito = confirmar_rascunho(
            user_id=user_id,
            superficie=superficie,
            contexto_conversa=contexto_conversa,
            referencia=catalogo.rascunho_referencia or "",
            versao=int(acao.argumentos["versao"]),
            confirmacao_referencia=catalogo.confirmacao_referencia or "",
            comparison_id=comparison_id,
            escopo_auditoria=escopo_auditoria,
        )
        return EfeitoExecucao(codigo=efeito.codigo, mensagem=efeito.mensagem or None, terceiro=efeito)
    status, e164, exibicao, turno = _telefone(catalogo, acao.argumentos)
    if acao.tipo == NOME_PREPARAR_TERCEIRO:
        resposta = _conteudo(
            catalogo,
            acao.argumentos.get("conteudo_ref") or "",
            user_id=user_id,
            superficie=superficie,
            contexto=contexto_conversa,
            comparison_id=comparison_id,
            escopo=escopo_auditoria,
        )
        if resposta is None:
            return EfeitoExecucao(codigo="falha_tecnica", mensagem=MENSAGEM_FALHA)
        efeito = preparar_rascunho(
            user_id=user_id,
            superficie=superficie,
            contexto_conversa=contexto_conversa,
            conteudo_referencia=resposta.referencia,
            content_hash=resposta.content_hash,
            nome_destinatario=acao.argumentos.get("nome_destinatario"),
            telefone_status=status,
            telefone_e164=e164,
            telefone_exibicao=exibicao,
            turno_telefone_ref=turno,
            chave_preparo=_chave_preparo(user_id, superficie, contexto_conversa, identidade_requisicao),
            comparison_id=comparison_id,
            escopo_auditoria=escopo_auditoria,
        )
        return EfeitoExecucao(codigo=efeito.codigo, mensagem=efeito.mensagem, terceiro=efeito)
    conteudo_alias = acao.argumentos.get("conteudo_ref")
    conteudo_referencia = None
    content_hash = None
    if conteudo_alias:
        resposta = _conteudo(
            catalogo,
            conteudo_alias,
            user_id=user_id,
            superficie=superficie,
            contexto=contexto_conversa,
            comparison_id=comparison_id,
            escopo=escopo_auditoria,
        )
        if resposta is None:
            return EfeitoExecucao(codigo="falha_tecnica", mensagem=MENSAGEM_FALHA)
        conteudo_referencia = resposta.referencia
        content_hash = resposta.content_hash
    efeito = atualizar_rascunho(
        user_id=user_id,
        superficie=superficie,
        contexto_conversa=contexto_conversa,
        referencia=catalogo.rascunho_referencia or "",
        versao=int(acao.argumentos["versao"]),
        conteudo_referencia=conteudo_referencia,
        content_hash=content_hash,
        nome_destinatario=acao.argumentos.get("nome_destinatario"),
        nome_informado="nome_destinatario" in acao.argumentos,
        telefone_status=status,
        telefone_e164=e164,
        telefone_exibicao=exibicao,
        turno_telefone_ref=turno,
        comparison_id=comparison_id,
        escopo_auditoria=escopo_auditoria,
    )
    return EfeitoExecucao(codigo=efeito.codigo, mensagem=efeito.mensagem, terceiro=efeito)
