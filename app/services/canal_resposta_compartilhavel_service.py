"""Registro server-side das respostas que podem ir para o WhatsApp.

O modelo escolhe o alias. O texto enviado é o que esta tabela guardou.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from uuid import uuid4

from app.extensions import db
from app.models import RespostaCompartilhavelCanal, SolicitacaoEntregaCanal, utcnow_naive

logger = logging.getLogger(__name__)

RETENCAO = timedelta(hours=6)
LIMITE_RECENTES = 3
_SUPERFICIES = frozenset(SolicitacaoEntregaCanal.SUPERFICIES)
_CHAVE_CONVERSA_JULIA = "julia_entrega_conversa_id"
_CHAVE_USUARIO_JULIA = "julia_entrega_conversa_user_id"
_G_CONVERSA_JULIA = "_julia_entrega_conversa_id"
_G_USUARIO_JULIA = "_julia_entrega_conversa_user"


def hash_conteudo(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def _historico_tem_turno(historico: object) -> bool:
    if not isinstance(historico, list):
        return False
    for item in historico:
        if isinstance(item, dict):
            bruto = item.get("content")
            if not isinstance(bruto, str):
                bruto = item.get("answer")
            if isinstance(bruto, str) and bruto.strip():
                return True
        elif isinstance(item, str) and item.strip():
            return True
    return False


def _token_conversa_valido(valor: object) -> bool:
    return isinstance(valor, str) and len(valor) == 32 and all(caractere in "0123456789abcdef" for caractere in valor)


def _mesmo_usuario(valor: object, user_id: int) -> bool:
    if isinstance(valor, bool) or not isinstance(valor, int):
        return False
    return int(valor) == int(user_id)


def _ler_sessao(sessao: object, chave: str):
    try:
        return sessao.get(chave)
    except RuntimeError:
        return None


def _gravar_sessao(sessao: object, token: str, user_id: int) -> None:
    try:
        sessao[_CHAVE_CONVERSA_JULIA] = token
        sessao[_CHAVE_USUARIO_JULIA] = int(user_id)
        if hasattr(sessao, "modified"):
            sessao.modified = True
    except RuntimeError:
        return None


def _sessao_flask():
    from flask import has_request_context, session

    if not has_request_context():
        return None
    return session


def _token_conversa_julia(user_id: int, historico: object, sessao: object | None) -> str:
    """Identidade da conversa emitida pelo servidor e amarrada à sessão."""
    from flask import g, has_request_context

    usar_requisicao = sessao is None
    if sessao is None:
        sessao = _sessao_flask()
    if usar_requisicao and has_request_context():
        guardado = getattr(g, _G_CONVERSA_JULIA, None)
        if _token_conversa_valido(guardado) and _mesmo_usuario(getattr(g, _G_USUARIO_JULIA, None), user_id):
            return guardado
    token = None
    if sessao is not None and _historico_tem_turno(historico):
        atual = _ler_sessao(sessao, _CHAVE_CONVERSA_JULIA)
        dono = _ler_sessao(sessao, _CHAVE_USUARIO_JULIA)
        if _token_conversa_valido(atual) and _mesmo_usuario(dono, user_id):
            token = atual
    if token is None:
        token = uuid4().hex
        if sessao is not None:
            _gravar_sessao(sessao, token, int(user_id))
    if usar_requisicao and has_request_context():
        setattr(g, _G_CONVERSA_JULIA, token)
        setattr(g, _G_USUARIO_JULIA, int(user_id))
    return token


def montar_contexto(
    *,
    superficie: str,
    user_id: int,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
    historico: object = None,
    sessao: object | None = None,
) -> str:
    if superficie == SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO:
        identificador = (comparison_id or "").strip() or "sem_comparacao"
        return f"comparacao:{identificador}"[:160]
    if superficie == SolicitacaoEntregaCanal.SUPERFICIE_AUDITORIA:
        escopo = (escopo_auditoria or "").strip() or "sessao"
        return f"auditoria:{int(user_id)}:{escopo}"[:160]
    if historico is None and sessao is None:
        return f"julia:{int(user_id)}"
    token = _token_conversa_julia(int(user_id), historico, sessao)
    return f"julia:{int(user_id)}:{token}"[:160]


def escopo_de_documentos(identificadores: object) -> str:
    if not isinstance(identificadores, (list, tuple)) or not identificadores:
        return "sem_documentos"
    material = "|".join(sorted(str(item).strip() for item in identificadores if str(item).strip()))
    if not material:
        return "sem_documentos"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def escopo_insights(batch_scope: object) -> str:
    escopo = str(batch_scope or "").strip() or "sem_lote"
    return f"insights:{escopo}"[:160]


def _user_id(usuario: object) -> int | None:
    if usuario is None or not getattr(usuario, "is_authenticated", False):
        return None
    identificador = getattr(usuario, "id", None)
    if isinstance(identificador, bool) or not isinstance(identificador, int) or identificador <= 0:
        return None
    return identificador


def registrar_resposta_compartilhavel(
    *,
    usuario: object,
    superficie: str,
    contexto_conversa: str,
    texto: str,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
) -> str | None:
    """Persiste o texto emitido. None quando não há o que registrar."""
    user_id = _user_id(usuario)
    if user_id is None or superficie not in _SUPERFICIES:
        return None
    if not isinstance(texto, str):
        return None
    corpo = texto.strip()
    if not corpo or len(corpo) > RespostaCompartilhavelCanal.TEXTO_MAXIMO:
        return None
    if not isinstance(contexto_conversa, str) or not contexto_conversa.strip():
        return None
    agora = utcnow_naive()
    referencia = f"rc{uuid4().hex}"
    row = RespostaCompartilhavelCanal(
        referencia=referencia,
        user_id=user_id,
        superficie=superficie,
        contexto_conversa=contexto_conversa.strip()[:160],
        comparison_id=(comparison_id or "").strip()[:80] or None,
        escopo_auditoria=(escopo_auditoria or "").strip()[:160] or None,
        content_hash=hash_conteudo(corpo),
        texto=corpo,
        valida=True,
        criada_em=agora,
        expira_em=agora + RETENCAO,
    )
    try:
        db.session.add(row)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.info(
            "resposta_compartilhavel codigo=falha_registro superficie=%s erro=%s",
            superficie,
            type(exc).__name__,
        )
        return None
    logger.info(
        "resposta_compartilhavel user_id=%s superficie=%s referencia=%s",
        user_id,
        superficie,
        referencia,
    )
    return referencia


def listar_respostas_recentes(
    *,
    user_id: int,
    superficie: str,
    contexto_conversa: str,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
    limite: int = LIMITE_RECENTES,
) -> list[RespostaCompartilhavelCanal]:
    if superficie not in _SUPERFICIES or not isinstance(user_id, int):
        return []
    agora = utcnow_naive()
    consulta = RespostaCompartilhavelCanal.query.filter_by(
        user_id=int(user_id),
        superficie=superficie,
        contexto_conversa=contexto_conversa,
        valida=True,
    ).filter(RespostaCompartilhavelCanal.expira_em > agora)
    if superficie == SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO and comparison_id:
        consulta = consulta.filter_by(comparison_id=comparison_id)
    if superficie == SolicitacaoEntregaCanal.SUPERFICIE_AUDITORIA and escopo_auditoria:
        consulta = consulta.filter_by(escopo_auditoria=escopo_auditoria)
    return (
        consulta.order_by(RespostaCompartilhavelCanal.id.desc())
        .limit(max(1, int(limite)))
        .all()
    )


def resolver_resposta_compartilhavel(
    *,
    user_id: int,
    superficie: str,
    contexto_conversa: str,
    referencia: str,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
) -> RespostaCompartilhavelCanal | None:
    if not isinstance(referencia, str) or not referencia.strip():
        return None
    row = RespostaCompartilhavelCanal.query.filter_by(referencia=referencia.strip()).one_or_none()
    if row is None or not row.valida:
        return None
    if int(row.user_id) != int(user_id) or row.superficie != superficie:
        return None
    if row.contexto_conversa != contexto_conversa:
        return None
    if row.expira_em <= utcnow_naive():
        return None
    if superficie == SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO:
        if comparison_id and row.comparison_id != comparison_id:
            return None
    if superficie == SolicitacaoEntregaCanal.SUPERFICIE_AUDITORIA and escopo_auditoria:
        if row.escopo_auditoria != escopo_auditoria:
            return None
    return row
