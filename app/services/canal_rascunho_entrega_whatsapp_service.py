"""Máquina de estados do rascunho de entrega WhatsApp para terceiro.

Não envia. Não escolhe telefone a partir de documento, resposta ou injeção.
Confirmação velha, versão velha e rascunho de outro contexto não executam.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import RascunhoEntregaWhatsApp, SolicitacaoEntregaCanal, utcnow_naive
from app.services.canal_elegibilidade_whatsapp_service import (
    MENSAGEM_TERCEIRO_BLOQUEADO,
    elegibilidade_terceiro,
)
from app.services.canal_resposta_compartilhavel_service import (
    hash_conteudo,
    resolver_resposta_compartilhavel,
)
from app.services.canal_telefone_declarado_service import digitos_e164

logger = logging.getLogger(__name__)

VALIDADE = timedelta(minutes=15)
_SUPERFICIES = frozenset(SolicitacaoEntregaCanal.SUPERFICIES)

MENSAGEM_DDI = "Falta o DDI do telefone. Informe o código do país."
MENSAGEM_TELEFONE_INVALIDO = "Não consegui usar esse telefone. Informe o número com DDI."
MENSAGEM_CANCELADO = "Envio cancelado."
MENSAGEM_EXPIRADO = "Esse pedido de envio expirou. Peça novamente se ainda quiser enviar."
MENSAGEM_RECUSADO = "Não posso usar esse envio."
MENSAGEM_EM_ANDAMENTO = "Já existe um envio em andamento para este pedido."
MENSAGEM_SEM_CONTEUDO = "Não encontrei essa resposta para enviar."
MENSAGEM_INCERTO = (
    "Não consegui confirmar se o WhatsApp recebeu a mensagem. Não vou tentar de novo automaticamente."
)
MENSAGEM_PARCIAL = "A entrega no WhatsApp ficou incompleta."
MENSAGEM_FALHA_TERCEIRO = "Não consegui concluir o envio no WhatsApp."

_travas: dict[str, threading.Lock] = {}
_travas_guard = threading.Lock()


@dataclass(frozen=True)
class EfeitoRascunho:
    codigo: str
    mensagem: str
    enviar: bool = False
    texto: str | None = None
    destinatario: str | None = None
    phone_number_id: str | None = None
    chave_execucao: str | None = None
    rascunho_id: int | None = None
    content_hash: str | None = None
    solicitacao_id: int | None = None


def trava_rascunho(referencia: str) -> threading.Lock:
    with _travas_guard:
        lock = _travas.get(referencia)
        if lock is None:
            lock = threading.Lock()
            _travas[referencia] = lock
        return lock


def chave_execucao_rascunho(referencia: str, versao: int) -> str:
    material = f"rascunho|{referencia}|versao|{int(versao)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _log(codigo: str, *, user_id: int | None, rascunho_id: int | None, estado: str | None, versao: int | None) -> None:
    logger.info(
        "rascunho_entrega_whatsapp user_id=%s rascunho_id=%s estado=%s versao=%s codigo=%s",
        user_id if user_id is not None else "-",
        rascunho_id if rascunho_id is not None else "-",
        estado or "-",
        versao if versao is not None else "-",
        codigo,
    )


def _agora_mais_validade():
    return utcnow_naive() + VALIDADE


def _nome(valor: object) -> str | None:
    if valor is None:
        return None
    if not isinstance(valor, str):
        raise ValueError("nome")
    limpo = valor.strip()
    if not limpo:
        return None
    if len(limpo) > 80 or any(ord(caractere) < 32 for caractere in limpo):
        raise ValueError("nome")
    return limpo


def _mensagem_telefone(nome: str | None) -> str:
    if nome:
        return f"Qual é o WhatsApp de {nome}, com DDI?"
    return "Qual é o WhatsApp do destinatário, com DDI?"


def _mensagem_confirmacao(nome: str | None, exibicao: str) -> str:
    if nome:
        return f"Vou enviar esta resposta para {nome} no WhatsApp {exibicao}. Confirmar?"
    return f"Vou enviar esta resposta para o WhatsApp {exibicao}. Confirmar?"


def _mensagem_sucesso(nome: str | None) -> str:
    if nome:
        return f"Enviei para o WhatsApp de {nome}."
    return "Enviei para o WhatsApp informado."


def _referencia_nova(prefixo: str) -> str:
    return f"{prefixo}{uuid4().hex}"


def _aberto(user_id: int, superficie: str, contexto: str) -> RascunhoEntregaWhatsApp | None:
    linhas = (
        RascunhoEntregaWhatsApp.query.filter_by(
            user_id=int(user_id),
            superficie=superficie,
            contexto_conversa=contexto,
        )
        .filter(RascunhoEntregaWhatsApp.estado.in_(RascunhoEntregaWhatsApp.ESTADOS_ABERTOS))
        .order_by(RascunhoEntregaWhatsApp.id.desc())
        .all()
    )
    for linha in linhas:
        if _expirar_se_preciso(linha):
            continue
        if linha.estado in RascunhoEntregaWhatsApp.ESTADOS_ABERTOS:
            return linha
    return None


def _expirar_se_preciso(row: RascunhoEntregaWhatsApp) -> bool:
    if row.estado in {
        RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO,
        RascunhoEntregaWhatsApp.ESTADO_CONCLUIDO,
        RascunhoEntregaWhatsApp.ESTADO_PARCIAL,
        RascunhoEntregaWhatsApp.ESTADO_FALHOU,
        RascunhoEntregaWhatsApp.ESTADO_RESULTADO_INCERTO,
        RascunhoEntregaWhatsApp.ESTADO_CANCELADO,
        RascunhoEntregaWhatsApp.ESTADO_EXPIRADO,
    }:
        return row.estado == RascunhoEntregaWhatsApp.ESTADO_EXPIRADO
    if row.expira_em > utcnow_naive():
        return False
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO:
        return False
    estado_antes = row.estado
    alteradas = _persistir_transicao(
        row,
        user_id=int(row.user_id),
        versao=int(row.versao),
        estados=(estado_antes,),
        valores={
            RascunhoEntregaWhatsApp.estado: RascunhoEntregaWhatsApp.ESTADO_EXPIRADO,
            RascunhoEntregaWhatsApp.atualizada_em: utcnow_naive(),
        },
    )
    return alteradas == 1


_ESTADOS_ALTERAVEIS = (
    RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_TELEFONE,
    RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO,
    RascunhoEntregaWhatsApp.ESTADO_BLOQUEADO_ELEGIBILIDADE,
)
_ESTADOS_CANCELAVEIS = _ESTADOS_ALTERAVEIS + (RascunhoEntregaWhatsApp.ESTADO_EXPIRADO,)


def _persistir_transicao(
    row: RascunhoEntregaWhatsApp,
    *,
    user_id: int,
    versao: int,
    estados: tuple[str, ...],
    valores: dict,
) -> int:
    """UPDATE condicional. Só uma transação grava o estado esperado."""
    identificador = int(row.id)
    superficie = row.superficie
    contexto = row.contexto_conversa
    db.session.expire(row)
    alteradas = (
        RascunhoEntregaWhatsApp.query.filter(
            RascunhoEntregaWhatsApp.id == identificador,
            RascunhoEntregaWhatsApp.user_id == int(user_id),
            RascunhoEntregaWhatsApp.superficie == superficie,
            RascunhoEntregaWhatsApp.contexto_conversa == contexto,
            RascunhoEntregaWhatsApp.versao == int(versao),
            RascunhoEntregaWhatsApp.estado.in_(estados),
        ).update(valores, synchronize_session=False)
    )
    db.session.commit()
    return int(alteradas or 0)


def _limpar_confirmacao(row: RascunhoEntregaWhatsApp) -> None:
    row.confirmacao_referencia = None
    row.confirmacao_apresentada_em = None
    row.confirmacao_versao = None


def _apresentar_confirmacao(row: RascunhoEntregaWhatsApp) -> str:
    agora = utcnow_naive()
    row.estado = RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO
    row.confirmacao_referencia = _referencia_nova("cf")
    row.confirmacao_apresentada_em = agora
    row.confirmacao_versao = int(row.versao)
    row.expira_em = agora + VALIDADE
    row.atualizada_em = agora
    exibicao = row.telefone_exibicao or row.telefone_e164 or ""
    return _mensagem_confirmacao(row.nome_destinatario, exibicao)


def _efeito_leitura(row: RascunhoEntregaWhatsApp) -> EfeitoRascunho:
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO and row.telefone_e164:
        if row.expira_em <= utcnow_naive():
            mensagem = _apresentar_confirmacao(row)
            rascunho_id = int(row.id)
            alteradas = _persistir_transicao(
                row,
                user_id=int(row.user_id),
                versao=int(row.versao),
                estados=(RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO,),
                valores={
                    RascunhoEntregaWhatsApp.estado: row.estado,
                    RascunhoEntregaWhatsApp.confirmacao_referencia: row.confirmacao_referencia,
                    RascunhoEntregaWhatsApp.confirmacao_apresentada_em: row.confirmacao_apresentada_em,
                    RascunhoEntregaWhatsApp.confirmacao_versao: row.confirmacao_versao,
                    RascunhoEntregaWhatsApp.expira_em: row.expira_em,
                    RascunhoEntregaWhatsApp.atualizada_em: row.atualizada_em,
                },
            )
            if alteradas != 1:
                return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=rascunho_id)
            return EfeitoRascunho(codigo="confirmacao", mensagem=mensagem, rascunho_id=rascunho_id)
        exibicao = row.telefone_exibicao or row.telefone_e164
        return EfeitoRascunho(
            codigo="confirmacao",
            mensagem=_mensagem_confirmacao(row.nome_destinatario, exibicao),
            rascunho_id=int(row.id),
        )
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_BLOQUEADO_ELEGIBILIDADE:
        return EfeitoRascunho(
            codigo="bloqueado_elegibilidade",
            mensagem=MENSAGEM_TERCEIRO_BLOQUEADO,
            rascunho_id=int(row.id),
        )
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO:
        return EfeitoRascunho(
            codigo="em_execucao",
            mensagem=MENSAGEM_EM_ANDAMENTO,
            rascunho_id=int(row.id),
        )
    return EfeitoRascunho(
        codigo="aguardando_telefone",
        mensagem=_mensagem_telefone(row.nome_destinatario),
        rascunho_id=int(row.id),
    )


def _aplicar_telefone(row: RascunhoEntregaWhatsApp, telefone_status: str | None, e164: str | None, exibicao: str | None, turno_ref: str | None) -> str | None:
    """None quando o telefone armazenado pode seguir. String quando a resposta já está definida."""
    if telefone_status is None:
        return None
    if telefone_status == "sem_ddi":
        row.telefone_e164 = None
        row.telefone_hash = None
        row.telefone_exibicao = None
        row.turno_telefone_ref = None
        return MENSAGEM_DDI
    if telefone_status != "ok" or not e164 or digitos_e164(e164) is None:
        row.telefone_e164 = None
        row.telefone_hash = None
        row.telefone_exibicao = None
        row.turno_telefone_ref = None
        return MENSAGEM_TELEFONE_INVALIDO
    row.telefone_e164 = e164
    row.telefone_hash = hash_conteudo(e164)
    row.telefone_exibicao = exibicao or e164
    row.turno_telefone_ref = turno_ref
    return None


def _mudou(row: RascunhoEntregaWhatsApp, *, conteudo_referencia: str, content_hash: str, nome: str | None, telefone_status: str | None, e164: str | None) -> bool:
    if row.conteudo_referencia != conteudo_referencia or row.content_hash != content_hash:
        return True
    if row.nome_destinatario != nome:
        return True
    if telefone_status is None:
        return False
    if telefone_status != "ok":
        return row.telefone_e164 is not None
    return row.telefone_e164 != e164


def _gravar(row: RascunhoEntregaWhatsApp) -> RascunhoEntregaWhatsApp | None:
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
            identificador = int(row.id)
    except IntegrityError:
        return None
    db.session.commit()
    return db.session.get(RascunhoEntregaWhatsApp, identificador)


def preparar_rascunho(
    *,
    user_id: int,
    superficie: str,
    contexto_conversa: str,
    conteudo_referencia: str,
    content_hash: str,
    nome_destinatario: str | None,
    telefone_status: str | None,
    telefone_e164: str | None,
    telefone_exibicao: str | None,
    turno_telefone_ref: str | None,
    chave_preparo: str | None,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
) -> EfeitoRascunho:
    if superficie not in _SUPERFICIES:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    resposta = resolver_resposta_compartilhavel(
        user_id=user_id,
        superficie=superficie,
        contexto_conversa=contexto_conversa,
        referencia=conteudo_referencia,
        comparison_id=comparison_id,
        escopo_auditoria=escopo_auditoria,
    )
    if resposta is None or resposta.content_hash != content_hash:
        _log("conteudo_indisponivel", user_id=user_id, rascunho_id=None, estado=None, versao=None)
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_SEM_CONTEUDO)
    try:
        nome = _nome(nome_destinatario)
    except ValueError:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if isinstance(chave_preparo, str) and chave_preparo:
        repetido = RascunhoEntregaWhatsApp.query.filter_by(chave_preparo=chave_preparo).one_or_none()
        if repetido is not None:
            if int(repetido.user_id) != int(user_id) or repetido.contexto_conversa != contexto_conversa:
                return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
            _expirar_se_preciso(repetido)
            db.session.commit()
            atual = db.session.get(RascunhoEntregaWhatsApp, int(repetido.id))
            if atual is None or atual.estado == RascunhoEntregaWhatsApp.ESTADO_EXPIRADO:
                return EfeitoRascunho(codigo="expirado", mensagem=MENSAGEM_EXPIRADO)
            efeito = _efeito_leitura(atual)
            _log(efeito.codigo, user_id=user_id, rascunho_id=int(atual.id), estado=atual.estado, versao=int(atual.versao))
            return efeito
    aberto = _aberto(user_id, superficie, contexto_conversa)
    if aberto is not None and aberto.estado == RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO:
        return EfeitoRascunho(codigo="em_execucao", mensagem=MENSAGEM_EM_ANDAMENTO, rascunho_id=int(aberto.id))
    if aberto is None:
        row = RascunhoEntregaWhatsApp(
            referencia=_referencia_nova("rw"),
            user_id=int(user_id),
            superficie=superficie,
            contexto_conversa=contexto_conversa,
            conteudo_referencia=conteudo_referencia,
            content_hash=content_hash,
            nome_destinatario=nome,
            telefone_e164=None,
            telefone_hash=None,
            telefone_exibicao=None,
            turno_telefone_ref=None,
            estado=RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_TELEFONE,
            versao=1,
            chave_preparo=chave_preparo,
            criada_em=utcnow_naive(),
            atualizada_em=utcnow_naive(),
            expira_em=_agora_mais_validade(),
        )
        aviso = _aplicar_telefone(row, telefone_status, telefone_e164, telefone_exibicao, turno_telefone_ref)
        if aviso is None and row.telefone_e164:
            mensagem = _apresentar_confirmacao(row)
            codigo = "confirmacao"
        else:
            row.estado = RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_TELEFONE
            mensagem = aviso or _mensagem_telefone(nome)
            codigo = "aguardando_telefone"
        gravado = _gravar(row)
        if gravado is None:
            return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
        _log(codigo, user_id=user_id, rascunho_id=int(gravado.id), estado=gravado.estado, versao=int(gravado.versao))
        return EfeitoRascunho(codigo=codigo, mensagem=mensagem, rascunho_id=int(gravado.id))
    return _atualizar_linha(
        aberto,
        conteudo_referencia=conteudo_referencia,
        content_hash=content_hash,
        nome=nome,
        telefone_status=telefone_status,
        telefone_e164=telefone_e164,
        telefone_exibicao=telefone_exibicao,
        turno_telefone_ref=turno_telefone_ref,
        user_id=user_id,
    )


def _atualizar_linha(
    row: RascunhoEntregaWhatsApp,
    *,
    conteudo_referencia: str,
    content_hash: str,
    nome: str | None,
    telefone_status: str | None,
    telefone_e164: str | None,
    telefone_exibicao: str | None,
    turno_telefone_ref: str | None,
    user_id: int,
) -> EfeitoRascunho:
    versao_antes = int(row.versao)
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO:
        return EfeitoRascunho(
            codigo="em_execucao",
            mensagem=MENSAGEM_EM_ANDAMENTO,
            rascunho_id=int(row.id),
        )
    if row.estado not in _ESTADOS_ALTERAVEIS:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=int(row.id))
    mudou = _mudou(
        row,
        conteudo_referencia=conteudo_referencia,
        content_hash=content_hash,
        nome=nome,
        telefone_status=telefone_status,
        e164=telefone_e164,
    )
    if (
        not mudou
        and row.estado == RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO
        and row.telefone_e164
        and row.confirmacao_referencia
        and row.expira_em > utcnow_naive()
    ):
        efeito = _efeito_leitura(row)
        _log(efeito.codigo, user_id=user_id, rascunho_id=int(row.id), estado=row.estado, versao=int(row.versao))
        return efeito
    if mudou:
        row.versao = int(row.versao) + 1
        _limpar_confirmacao(row)
        row.chave_execucao = None
    row.conteudo_referencia = conteudo_referencia
    row.content_hash = content_hash
    row.nome_destinatario = nome
    aviso = _aplicar_telefone(row, telefone_status, telefone_e164, telefone_exibicao, turno_telefone_ref)
    row.atualizada_em = utcnow_naive()
    row.expira_em = _agora_mais_validade()
    if aviso is not None or not row.telefone_e164:
        row.estado = RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_TELEFONE
        _limpar_confirmacao(row)
        mensagem = aviso or _mensagem_telefone(row.nome_destinatario)
        codigo = "aguardando_telefone"
    else:
        mensagem = _apresentar_confirmacao(row)
        codigo = "confirmacao"
    rascunho_id = int(row.id)
    estado = row.estado
    versao = int(row.versao)
    valores = {
        RascunhoEntregaWhatsApp.conteudo_referencia: row.conteudo_referencia,
        RascunhoEntregaWhatsApp.content_hash: row.content_hash,
        RascunhoEntregaWhatsApp.nome_destinatario: row.nome_destinatario,
        RascunhoEntregaWhatsApp.telefone_e164: row.telefone_e164,
        RascunhoEntregaWhatsApp.telefone_hash: row.telefone_hash,
        RascunhoEntregaWhatsApp.telefone_exibicao: row.telefone_exibicao,
        RascunhoEntregaWhatsApp.turno_telefone_ref: row.turno_telefone_ref,
        RascunhoEntregaWhatsApp.estado: estado,
        RascunhoEntregaWhatsApp.versao: versao,
        RascunhoEntregaWhatsApp.confirmacao_referencia: row.confirmacao_referencia,
        RascunhoEntregaWhatsApp.confirmacao_apresentada_em: row.confirmacao_apresentada_em,
        RascunhoEntregaWhatsApp.confirmacao_versao: row.confirmacao_versao,
        RascunhoEntregaWhatsApp.chave_execucao: row.chave_execucao,
        RascunhoEntregaWhatsApp.atualizada_em: row.atualizada_em,
        RascunhoEntregaWhatsApp.expira_em: row.expira_em,
    }
    alteradas = _persistir_transicao(
        row,
        user_id=user_id,
        versao=versao_antes,
        estados=_ESTADOS_ALTERAVEIS,
        valores=valores,
    )
    if alteradas != 1:
        _log("versao_antiga", user_id=user_id, rascunho_id=rascunho_id, estado=None, versao=versao_antes)
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=rascunho_id)
    _log(codigo, user_id=user_id, rascunho_id=rascunho_id, estado=estado, versao=versao)
    return EfeitoRascunho(codigo=codigo, mensagem=mensagem, rascunho_id=rascunho_id)


def atualizar_rascunho(
    *,
    user_id: int,
    superficie: str,
    contexto_conversa: str,
    referencia: str,
    versao: int,
    conteudo_referencia: str | None,
    content_hash: str | None,
    nome_destinatario: str | None,
    nome_informado: bool,
    telefone_status: str | None,
    telefone_e164: str | None,
    telefone_exibicao: str | None,
    turno_telefone_ref: str | None,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
) -> EfeitoRascunho:
    row = RascunhoEntregaWhatsApp.query.filter_by(referencia=referencia).one_or_none()
    if row is None or int(row.user_id) != int(user_id) or row.superficie != superficie or row.contexto_conversa != contexto_conversa:
        _log("rascunho_alheio", user_id=user_id, rascunho_id=getattr(row, "id", None), estado=None, versao=None)
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if int(row.versao) != int(versao):
        _log("versao_antiga", user_id=user_id, rascunho_id=int(row.id), estado=row.estado, versao=int(row.versao))
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if row.estado not in {
        RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_TELEFONE,
        RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO,
        RascunhoEntregaWhatsApp.ESTADO_BLOQUEADO_ELEGIBILIDADE,
    }:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if _expirar_se_preciso(row):
        db.session.commit()
        return EfeitoRascunho(codigo="expirado", mensagem=MENSAGEM_EXPIRADO, rascunho_id=int(row.id))
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO and row.expira_em <= utcnow_naive():
        rascunho_id = int(row.id)
        alteradas = _persistir_transicao(
            row,
            user_id=user_id,
            versao=int(versao),
            estados=(RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO,),
            valores={
                RascunhoEntregaWhatsApp.estado: RascunhoEntregaWhatsApp.ESTADO_EXPIRADO,
                RascunhoEntregaWhatsApp.atualizada_em: utcnow_naive(),
            },
        )
        if alteradas == 1:
            return EfeitoRascunho(codigo="expirado", mensagem=MENSAGEM_EXPIRADO, rascunho_id=rascunho_id)
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=rascunho_id)
    try:
        nome = _nome(nome_destinatario) if nome_informado else row.nome_destinatario
    except ValueError:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if conteudo_referencia is None:
        conteudo_referencia = row.conteudo_referencia
        content_hash = row.content_hash
    else:
        resposta = resolver_resposta_compartilhavel(
            user_id=user_id,
            superficie=superficie,
            contexto_conversa=contexto_conversa,
            referencia=conteudo_referencia,
            comparison_id=comparison_id,
            escopo_auditoria=escopo_auditoria,
        )
        if resposta is None or content_hash != resposta.content_hash:
            return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_SEM_CONTEUDO)
    return _atualizar_linha(
        row,
        conteudo_referencia=conteudo_referencia,
        content_hash=content_hash,
        nome=nome,
        telefone_status=telefone_status,
        telefone_e164=telefone_e164,
        telefone_exibicao=telefone_exibicao,
        turno_telefone_ref=turno_telefone_ref,
        user_id=user_id,
    )


def cancelar_rascunho(
    *,
    user_id: int,
    superficie: str,
    contexto_conversa: str,
    referencia: str,
    versao: int,
) -> EfeitoRascunho:
    row = RascunhoEntregaWhatsApp.query.filter_by(referencia=referencia).one_or_none()
    if row is None or int(row.user_id) != int(user_id) or row.superficie != superficie or row.contexto_conversa != contexto_conversa:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if int(row.versao) != int(versao):
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_CANCELADO:
        return EfeitoRascunho(codigo="cancelado", mensagem=MENSAGEM_CANCELADO, rascunho_id=int(row.id))
    if row.estado not in _ESTADOS_CANCELAVEIS:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=int(row.id))
    rascunho_id = int(row.id)
    versao_antes = int(row.versao)
    alteradas = _persistir_transicao(
        row,
        user_id=user_id,
        versao=versao_antes,
        estados=_ESTADOS_CANCELAVEIS,
        valores={
            RascunhoEntregaWhatsApp.estado: RascunhoEntregaWhatsApp.ESTADO_CANCELADO,
            RascunhoEntregaWhatsApp.atualizada_em: utcnow_naive(),
            RascunhoEntregaWhatsApp.confirmacao_referencia: None,
            RascunhoEntregaWhatsApp.confirmacao_apresentada_em: None,
            RascunhoEntregaWhatsApp.confirmacao_versao: None,
        },
    )
    if alteradas != 1:
        atual = db.session.get(RascunhoEntregaWhatsApp, rascunho_id)
        if atual is not None and atual.estado == RascunhoEntregaWhatsApp.ESTADO_CANCELADO:
            return EfeitoRascunho(codigo="cancelado", mensagem=MENSAGEM_CANCELADO, rascunho_id=rascunho_id)
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=rascunho_id)
    _log("cancelado", user_id=user_id, rascunho_id=rascunho_id, estado=RascunhoEntregaWhatsApp.ESTADO_CANCELADO, versao=versao_antes)
    return EfeitoRascunho(codigo="cancelado", mensagem=MENSAGEM_CANCELADO, rascunho_id=rascunho_id)


def _pertence(row: RascunhoEntregaWhatsApp, *, user_id: int, superficie: str, contexto: str) -> bool:
    return (
        int(row.user_id) == int(user_id)
        and row.superficie == superficie
        and row.contexto_conversa == contexto
    )


def confirmar_rascunho(
    *,
    user_id: int,
    superficie: str,
    contexto_conversa: str,
    referencia: str,
    versao: int,
    confirmacao_referencia: str,
    comparison_id: str | None = None,
    escopo_auditoria: str | None = None,
) -> EfeitoRascunho:
    row = RascunhoEntregaWhatsApp.query.filter_by(referencia=referencia).one_or_none()
    if row is None or not _pertence(row, user_id=user_id, superficie=superficie, contexto=contexto_conversa):
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if int(row.versao) != int(versao):
        _log("versao_antiga", user_id=user_id, rascunho_id=int(row.id), estado=row.estado, versao=int(row.versao))
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=int(row.id))
    if row.estado in {
        RascunhoEntregaWhatsApp.ESTADO_CONCLUIDO,
        RascunhoEntregaWhatsApp.ESTADO_PARCIAL,
        RascunhoEntregaWhatsApp.ESTADO_FALHOU,
        RascunhoEntregaWhatsApp.ESTADO_RESULTADO_INCERTO,
        RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO,
    }:
        return _convergir(row)
    if row.confirmacao_referencia != confirmacao_referencia or row.confirmacao_versao != int(versao):
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=int(row.id))
    if row.estado != RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO or not row.telefone_e164:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=int(row.id))
    if row.expira_em <= utcnow_naive():
        mensagem = _apresentar_confirmacao(row)
        rascunho_id = int(row.id)
        estado = row.estado
        versao_atual = int(row.versao)
        alteradas = _persistir_transicao(
            row,
            user_id=user_id,
            versao=int(versao),
            estados=(RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO,),
            valores={
                RascunhoEntregaWhatsApp.estado: estado,
                RascunhoEntregaWhatsApp.confirmacao_referencia: row.confirmacao_referencia,
                RascunhoEntregaWhatsApp.confirmacao_apresentada_em: row.confirmacao_apresentada_em,
                RascunhoEntregaWhatsApp.confirmacao_versao: row.confirmacao_versao,
                RascunhoEntregaWhatsApp.expira_em: row.expira_em,
                RascunhoEntregaWhatsApp.atualizada_em: row.atualizada_em,
            },
        )
        if alteradas != 1:
            return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=rascunho_id)
        _log("confirmacao_expirada", user_id=user_id, rascunho_id=rascunho_id, estado=estado, versao=versao_atual)
        return EfeitoRascunho(codigo="confirmacao", mensagem=mensagem, rascunho_id=rascunho_id)
    identificador = int(row.id)
    db.session.expire(row)
    reservadas = (
        RascunhoEntregaWhatsApp.query.filter_by(
            id=identificador,
            user_id=int(user_id),
            superficie=superficie,
            contexto_conversa=contexto_conversa,
            versao=int(versao),
            estado=RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO,
            confirmacao_referencia=confirmacao_referencia,
            confirmacao_versao=int(versao),
        ).update(
            {RascunhoEntregaWhatsApp.estado: RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO},
            synchronize_session=False,
        )
    )
    db.session.commit()
    atual = db.session.get(RascunhoEntregaWhatsApp, identificador)
    if atual is None:
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO)
    if reservadas != 1:
        return _convergir(atual)
    resposta = resolver_resposta_compartilhavel(
        user_id=user_id,
        superficie=superficie,
        contexto_conversa=contexto_conversa,
        referencia=atual.conteudo_referencia,
        comparison_id=comparison_id,
        escopo_auditoria=escopo_auditoria,
    )
    if resposta is None or resposta.content_hash != atual.content_hash:
        atual.estado = RascunhoEntregaWhatsApp.ESTADO_FALHOU
        atual.atualizada_em = utcnow_naive()
        db.session.commit()
        return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_SEM_CONTEUDO, rascunho_id=int(atual.id))
    elegibilidade = elegibilidade_terceiro(atual.telefone_e164)
    _log(
        elegibilidade.codigo,
        user_id=user_id,
        rascunho_id=int(atual.id),
        estado=atual.estado,
        versao=int(atual.versao),
    )
    if not elegibilidade.elegivel or not elegibilidade.phone_number_id:
        atual.estado = RascunhoEntregaWhatsApp.ESTADO_BLOQUEADO_ELEGIBILIDADE
        atual.atualizada_em = utcnow_naive()
        db.session.commit()
        return EfeitoRascunho(
            codigo="bloqueado_elegibilidade",
            mensagem=MENSAGEM_TERCEIRO_BLOQUEADO,
            rascunho_id=int(atual.id),
        )
    destinatario = digitos_e164(atual.telefone_e164)
    if destinatario is None:
        atual.estado = RascunhoEntregaWhatsApp.ESTADO_FALHOU
        db.session.commit()
        return EfeitoRascunho(codigo="falhou", mensagem=MENSAGEM_FALHA_TERCEIRO, rascunho_id=int(atual.id))
    chave = chave_execucao_rascunho(atual.referencia, int(atual.versao))
    atual.chave_execucao = chave
    atual.atualizada_em = utcnow_naive()
    db.session.commit()
    return EfeitoRascunho(
        codigo="enviar",
        mensagem="",
        enviar=True,
        texto=resposta.texto,
        destinatario=destinatario,
        phone_number_id=elegibilidade.phone_number_id,
        chave_execucao=chave,
        rascunho_id=int(atual.id),
        content_hash=atual.content_hash,
    )


def _convergir(row: RascunhoEntregaWhatsApp) -> EfeitoRascunho:
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_CONCLUIDO:
        return EfeitoRascunho(
            codigo="enviado",
            mensagem=_mensagem_sucesso(row.nome_destinatario),
            rascunho_id=int(row.id),
            solicitacao_id=row.solicitacao_entrega_id,
        )
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_PARCIAL:
        return EfeitoRascunho(
            codigo="parcial",
            mensagem=MENSAGEM_PARCIAL,
            rascunho_id=int(row.id),
            solicitacao_id=row.solicitacao_entrega_id,
        )
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_RESULTADO_INCERTO:
        return EfeitoRascunho(
            codigo="resultado_incerto",
            mensagem=MENSAGEM_INCERTO,
            rascunho_id=int(row.id),
            solicitacao_id=row.solicitacao_entrega_id,
        )
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_FALHOU:
        return EfeitoRascunho(
            codigo="falhou",
            mensagem=MENSAGEM_FALHA_TERCEIRO,
            rascunho_id=int(row.id),
            solicitacao_id=row.solicitacao_entrega_id,
        )
    if row.estado == RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO:
        return EfeitoRascunho(
            codigo="em_execucao",
            mensagem=MENSAGEM_EM_ANDAMENTO,
            rascunho_id=int(row.id),
            solicitacao_id=row.solicitacao_entrega_id,
        )
    return EfeitoRascunho(codigo="recusado", mensagem=MENSAGEM_RECUSADO, rascunho_id=int(row.id))


def finalizar_transporte(
    rascunho_id: int,
    *,
    codigo_transporte: str,
    solicitacao_id: int | None,
    incerto: bool,
) -> EfeitoRascunho:
    row = db.session.get(RascunhoEntregaWhatsApp, int(rascunho_id))
    if row is None:
        return EfeitoRascunho(codigo="falhou", mensagem=MENSAGEM_FALHA_TERCEIRO)
    if solicitacao_id is not None:
        row.solicitacao_entrega_id = int(solicitacao_id)
    if incerto:
        row.estado = RascunhoEntregaWhatsApp.ESTADO_RESULTADO_INCERTO
        mensagem = MENSAGEM_INCERTO
        codigo = "resultado_incerto"
    elif codigo_transporte == "enviado":
        row.estado = RascunhoEntregaWhatsApp.ESTADO_CONCLUIDO
        mensagem = _mensagem_sucesso(row.nome_destinatario)
        codigo = "enviado"
    elif codigo_transporte == "parcial":
        row.estado = RascunhoEntregaWhatsApp.ESTADO_PARCIAL
        mensagem = MENSAGEM_PARCIAL
        codigo = "parcial"
    else:
        row.estado = RascunhoEntregaWhatsApp.ESTADO_FALHOU
        mensagem = MENSAGEM_FALHA_TERCEIRO
        codigo = "falhou"
    row.atualizada_em = utcnow_naive()
    db.session.commit()
    _log(codigo, user_id=int(row.user_id), rascunho_id=int(row.id), estado=row.estado, versao=int(row.versao))
    return EfeitoRascunho(
        codigo=codigo,
        mensagem=mensagem,
        rascunho_id=int(row.id),
        solicitacao_id=row.solicitacao_entrega_id,
    )


def rascunho_aberto(
    *,
    user_id: int,
    superficie: str,
    contexto_conversa: str,
) -> RascunhoEntregaWhatsApp | None:
    return _aberto(user_id, superficie, contexto_conversa)
