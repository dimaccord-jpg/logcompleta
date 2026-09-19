"""Transferência administrativa de titularidade Multiuser (Fase 7). Sem self-service."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AuditoriaGerencial,
    ContaMonetizacaoVinculo,
    ContaMultiuserTitularidadeSolicitacao,
    ContaVinculoOrganizacional,
    Franquia,
    User,
    utcnow_naive,
)
from app.services.conta_multiuser_aumento_service import formatar_data_br
from app.services.conta_multiuser_capacidade_service import bloquear_conta_para_capacidade
from app.services.conta_multiuser_ciclo_service import resolver_ciclo_canonico_conta
from app.services.conta_multiuser_errors import (
    TitularidadeConflitoError,
    TitularidadeInvalidaError,
    TitularidadeNaoAutorizadaError,
)
from app.services.conta_multiuser_notificacao_service import (
    criar_notificacao,
    tentar_enviar_email_notificacao,
)
from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    PAPEL_CONTRATANTE,
    PAPEL_MEMBRO,
)
from app.services.conta_organizacional_rules import titular_obrigatorio_de_papel

logger = logging.getLogger(__name__)


@dataclass
class ResultadoTitularidade:
    estado: str
    replay: bool
    conflito: bool
    solicitacao_id: int
    conta_id: int
    versao: int
    mensagem: str = ""


def _exigir_admin(user: User) -> None:
    if user is None or not bool(getattr(user, "is_admin", False)):
        raise TitularidadeNaoAutorizadaError(
            "Somente a equipe administrativa da plataforma pode conduzir a titularidade."
        )


def _cas_update(row: ContaMultiuserTitularidadeSolicitacao, values: dict) -> bool:
    expected = int(row.versao)
    values = dict(values)
    values["versao"] = expected + 1
    values["updated_at"] = utcnow_naive()
    result = db.session.execute(
        update(ContaMultiuserTitularidadeSolicitacao)
        .where(ContaMultiuserTitularidadeSolicitacao.id == int(row.id))
        .where(ContaMultiuserTitularidadeSolicitacao.versao == expected)
        .where(ContaMultiuserTitularidadeSolicitacao.estado == row.estado)
        .values(**values)
    )
    db.session.flush()
    if int(result.rowcount or 0) != 1:
        return False
    db.session.refresh(row)
    return True


def _auditoria(decisao: str, contexto: dict, resultado: str = "sucesso") -> None:
    db.session.add(
        AuditoriaGerencial(
            tipo_decisao="titularidade_solicitada"
            if decisao == "solicitada"
            else (
                "titularidade_aprovada"
                if decisao == "aprovada"
                else "titularidade_rejeitada"
            ),
            decisao=decisao,
            contexto_json=json.dumps(
                contexto, ensure_ascii=True, sort_keys=True, default=str
            ),
            resultado=resultado,
        )
    )


def _vinculo_ativo(user_id: int, conta_id: int | None = None) -> ContaVinculoOrganizacional | None:
    q = ContaVinculoOrganizacional.query.filter_by(
        user_id=int(user_id),
        estado=ESTADO_ATIVO,
    )
    if conta_id is not None:
        q = q.filter_by(conta_id=int(conta_id))
    return q.order_by(ContaVinculoOrganizacional.id.asc()).first()


def _validar_candidato(*, conta_id: int, titular_id: int, candidato_id: int) -> ContaVinculoOrganizacional:
    if int(candidato_id) == int(titular_id):
        raise TitularidadeInvalidaError("O candidato não pode ser o titular atual.")
    candidato_user = db.session.get(User, int(candidato_id))
    if candidato_user is None:
        raise TitularidadeInvalidaError("Candidato inexistente.")
    vinculo = _vinculo_ativo(int(candidato_id), conta_id=int(conta_id))
    if vinculo is None:
        outro = _vinculo_ativo(int(candidato_id))
        if outro is not None and int(outro.conta_id) != int(conta_id):
            raise TitularidadeInvalidaError("Candidato pertence a outra Conta.")
        encerrado = (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=int(candidato_id),
                conta_id=int(conta_id),
                estado="encerrado",
            )
            .first()
        )
        if encerrado is not None:
            raise TitularidadeInvalidaError("Candidato com membership revogado.")
        raise TitularidadeInvalidaError("Candidato sem membership ativo na Conta.")
    if (vinculo.papel or "") != PAPEL_MEMBRO:
        raise TitularidadeInvalidaError("O candidato deve ser Membro ativo da mesma Conta.")
    return vinculo


def _snapshot_billing(conta_id: int) -> dict:
    from app.models import Conta

    vinculo = (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), ativo=True)
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .first()
    )
    conta = db.session.get(Conta, int(conta_id))
    ciclo = resolver_ciclo_canonico_conta(int(conta_id))
    franquias = (
        Franquia.query.filter_by(conta_id=int(conta_id))
        .order_by(Franquia.id.asc())
        .all()
    )
    return {
        "customer_id": vinculo.customer_id if vinculo else None,
        "subscription_id": vinculo.subscription_id if vinculo else None,
        "price_id": vinculo.price_id if vinculo else None,
        "quantity": conta.quantidade_assentos_contratados if conta else None,
        "ciclo_inicio": ciclo.inicio.isoformat() if ciclo.inicio else None,
        "ciclo_fim": ciclo.fim.isoformat() if ciclo.fim else None,
        "franquia_ids": [int(fr.id) for fr in franquias],
        "consumos": {str(fr.id): str(fr.consumo_acumulado) for fr in franquias},
        "plano_interno": vinculo.plano_interno if vinculo else None,
    }


def solicitar_titularidade(
    *,
    admin: User,
    conta_id: int,
    candidato_id: int,
    motivo: str,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> ResultadoTitularidade:
    _exigir_admin(admin)
    motivo_n = (motivo or "").strip()[:500]
    if not motivo_n:
        raise TitularidadeInvalidaError("Informe o motivo da transferência.")
    conta = bloquear_conta_para_capacidade(int(conta_id))
    titular_v = (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta.id),
            papel=PAPEL_CONTRATANTE,
            estado=ESTADO_ATIVO,
        )
        .order_by(ContaVinculoOrganizacional.id.asc())
        .first()
    )
    if titular_v is None:
        raise TitularidadeInvalidaError("Conta sem Contratante ativo.")
    _validar_candidato(
        conta_id=int(conta.id),
        titular_id=int(titular_v.user_id),
        candidato_id=int(candidato_id),
    )
    chave = (idempotency_key or "").strip() or uuid.uuid4().hex
    existente = ContaMultiuserTitularidadeSolicitacao.query.filter_by(
        idempotency_key=chave
    ).first()
    if existente is not None:
        if int(existente.conta_id) != int(conta.id) or int(existente.candidato_id) != int(
            candidato_id
        ):
            raise TitularidadeConflitoError(
                "Chave de idempotência já identifica outra solicitação."
            )
        if commit:
            db.session.commit()
        return ResultadoTitularidade(
            estado=existente.estado,
            replay=True,
            conflito=False,
            solicitacao_id=int(existente.id),
            conta_id=int(conta.id),
            versao=int(existente.versao),
            mensagem="Solicitação já registrada.",
        )
    pendente = (
        ContaMultiuserTitularidadeSolicitacao.query.filter_by(
            conta_id=int(conta.id),
            estado=ContaMultiuserTitularidadeSolicitacao.ESTADO_SOLICITADA,
        )
        .first()
    )
    if pendente is not None:
        if int(pendente.candidato_id) == int(candidato_id):
            if commit:
                db.session.commit()
            return ResultadoTitularidade(
                estado=pendente.estado,
                replay=True,
                conflito=False,
                solicitacao_id=int(pendente.id),
                conta_id=int(conta.id),
                versao=int(pendente.versao),
                mensagem="Solicitação já registrada.",
            )
        raise TitularidadeConflitoError(
            "Já existe uma solicitação de titularidade pendente para esta Conta."
        )

    row = ContaMultiuserTitularidadeSolicitacao(
        conta_id=int(conta.id),
        titular_atual_id=int(titular_v.user_id),
        candidato_id=int(candidato_id),
        solicitante_id=int(admin.id),
        motivo=motivo_n,
        estado=ContaMultiuserTitularidadeSolicitacao.ESTADO_SOLICITADA,
        idempotency_key=chave,
        correlation_id=uuid.uuid4().hex,
        versao=1,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        raise TitularidadeConflitoError(
            "Já existe uma solicitação de titularidade pendente para esta Conta."
        ) from exc
    _auditoria(
        "solicitada",
        {
            "conta_id": int(conta.id),
            "titular_atual_id": int(titular_v.user_id),
            "candidato_id": int(candidato_id),
            "solicitacao_id": int(row.id),
        },
    )
    for uid, tipo, msg in (
        (
            int(titular_v.user_id),
            "titularidade_solicitada",
            "Há uma solicitação administrativa de transferência de titularidade da Conta.",
        ),
        (
            int(candidato_id),
            "titularidade_solicitada",
            "Você foi indicado como novo Contratante desta Conta. A decisão é administrativa.",
        ),
    ):
        criar_notificacao(
            user_id=uid,
            conta_id=int(conta.id),
            tipo=tipo,
            mensagem=msg,
            dedup_key=f"titularidade_solicitada:{row.correlation_id}:{uid}",
            cta_interno="user.perfil",
            referencia_dominio=f"titularidade:{row.id}",
            commit=False,
        )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return ResultadoTitularidade(
        estado=row.estado,
        replay=False,
        conflito=False,
        solicitacao_id=int(row.id),
        conta_id=int(conta.id),
        versao=int(row.versao),
        mensagem="Solicitação de titularidade registrada.",
    )


def decidir_titularidade(
    *,
    admin: User,
    solicitacao_id: int,
    decisao: str,
    versao: int | None = None,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> ResultadoTitularidade:
    _exigir_admin(admin)
    decisao_n = (decisao or "").strip().lower()
    if decisao_n not in {"aprovada", "rejeitada", "aprovar", "rejeitar"}:
        raise TitularidadeInvalidaError("Decisão inválida.")
    if decisao_n == "aprovar":
        decisao_n = "aprovada"
    if decisao_n == "rejeitar":
        decisao_n = "rejeitada"

    row = db.session.get(ContaMultiuserTitularidadeSolicitacao, int(solicitacao_id))
    if row is None:
        raise TitularidadeInvalidaError("Solicitação de titularidade não encontrada.")
    chave_decisao = (idempotency_key or "").strip() or None
    if chave_decisao:
        mesma = ContaMultiuserTitularidadeSolicitacao.query.filter_by(
            decisao_idempotency_key=chave_decisao
        ).first()
        if mesma is not None and int(mesma.id) == int(row.id):
            if (mesma.estado or "") != decisao_n:
                raise TitularidadeConflitoError(
                    "Esta chave de idempotência já registrou outra decisão."
                )
            if commit:
                db.session.commit()
            return ResultadoTitularidade(
                estado=mesma.estado,
                replay=True,
                conflito=False,
                solicitacao_id=int(mesma.id),
                conta_id=int(mesma.conta_id),
                versao=int(mesma.versao),
                mensagem="Decisão já aplicada.",
            )
        if mesma is not None:
            raise TitularidadeConflitoError(
                "Chave de idempotência já identifica outra decisão."
            )

    conta = bloquear_conta_para_capacidade(int(row.conta_id))
    db.session.refresh(row)
    if versao is not None and int(versao) != int(row.versao):
        raise TitularidadeConflitoError("Versão desatualizada da solicitação.")

    if row.estado != ContaMultiuserTitularidadeSolicitacao.ESTADO_SOLICITADA:
        if row.estado == decisao_n:
            if commit:
                db.session.commit()
            return ResultadoTitularidade(
                estado=row.estado,
                replay=True,
                conflito=False,
                solicitacao_id=int(row.id),
                conta_id=int(row.conta_id),
                versao=int(row.versao),
                mensagem="Decisão já aplicada.",
            )
        raise TitularidadeConflitoError("Esta solicitação já foi decidida.")

    agora = utcnow_naive()
    values = {
        "estado": decisao_n,
        "administrador_id": int(admin.id),
        "decidido_em": agora,
        "decisao_idempotency_key": chave_decisao,
    }

    if decisao_n == ContaMultiuserTitularidadeSolicitacao.ESTADO_REJEITADA:
        ok = _cas_update(row, values)
        if not ok:
            raise TitularidadeConflitoError("Não foi possível registrar a decisão.")
        _auditoria(
            "rejeitada",
            {"solicitacao_id": int(row.id), "conta_id": int(conta.id)},
        )
        for uid in (int(row.titular_atual_id), int(row.candidato_id)):
            criar_notificacao(
                user_id=uid,
                conta_id=int(conta.id),
                tipo="titularidade_rejeitada",
                mensagem="A transferência administrativa de titularidade foi rejeitada.",
                dedup_key=f"titularidade_rejeitada:{row.correlation_id}:{uid}",
                cta_interno="user.perfil",
                referencia_dominio=f"titularidade:{row.id}",
                commit=False,
            )
        if commit:
            db.session.commit()
        return ResultadoTitularidade(
            estado=row.estado,
            replay=False,
            conflito=False,
            solicitacao_id=int(row.id),
            conta_id=int(conta.id),
            versao=int(row.versao),
            mensagem="Titularidade rejeitada.",
        )

    billing_antes = _snapshot_billing(int(conta.id))
    admin_flags = {
        int(row.titular_atual_id): bool(
            getattr(db.session.get(User, int(row.titular_atual_id)), "is_admin", False)
        ),
        int(row.candidato_id): bool(
            getattr(db.session.get(User, int(row.candidato_id)), "is_admin", False)
        ),
    }
    titular_v = (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta.id),
            papel=PAPEL_CONTRATANTE,
            estado=ESTADO_ATIVO,
        )
        .with_for_update()
        .first()
    )
    if titular_v is None or int(titular_v.user_id) != int(row.titular_atual_id):
        raise TitularidadeInvalidaError("Titular atual não confere no momento da aprovação.")
    candidato_v = _validar_candidato(
        conta_id=int(conta.id),
        titular_id=int(row.titular_atual_id),
        candidato_id=int(row.candidato_id),
    )
    candidato_v = (
        db.session.query(ContaVinculoOrganizacional)
        .filter(ContaVinculoOrganizacional.id == int(candidato_v.id))
        .with_for_update()
        .one()
    )

    titular_v.papel = PAPEL_MEMBRO
    titular_v.titular = titular_obrigatorio_de_papel(PAPEL_MEMBRO)
    titular_v.updated_at = agora
    db.session.add(titular_v)
    db.session.flush()
    candidato_v.papel = PAPEL_CONTRATANTE
    candidato_v.titular = titular_obrigatorio_de_papel(PAPEL_CONTRATANTE)
    candidato_v.updated_at = agora
    db.session.add(candidato_v)
    db.session.flush()

    contratantes = ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta.id),
        papel=PAPEL_CONTRATANTE,
        estado=ESTADO_ATIVO,
    ).count()
    if contratantes != 1:
        raise TitularidadeInvalidaError(
            "A transferência deve deixar exatamente um Contratante ativo."
        )

    billing_depois = _snapshot_billing(int(conta.id))
    for chave_b in (
        "customer_id",
        "subscription_id",
        "price_id",
        "quantity",
        "ciclo_inicio",
        "ciclo_fim",
        "franquia_ids",
        "consumos",
        "plano_interno",
    ):
        if billing_antes.get(chave_b) != billing_depois.get(chave_b):
            raise TitularidadeInvalidaError(
                "A transferência não pode alterar billing, ciclo, quantity ou Franquias."
            )
    for uid, flag in admin_flags.items():
        user = db.session.get(User, uid)
        if user is not None and bool(user.is_admin) != flag:
            raise TitularidadeInvalidaError("A transferência não altera User.is_admin.")

    ok = _cas_update(row, values)
    if not ok:
        raise TitularidadeConflitoError("Não foi possível registrar a decisão.")
    _auditoria(
        "aprovada",
        {
            "solicitacao_id": int(row.id),
            "conta_id": int(conta.id),
            "titular_anterior_id": int(titular_v.user_id),
            "novo_titular_id": int(candidato_v.user_id),
        },
    )
    criar_notificacao(
        user_id=int(titular_v.user_id),
        conta_id=int(conta.id),
        tipo="titularidade_aprovada",
        mensagem="A titularidade da Conta foi transferida. Você permanece como Membro.",
        dedup_key=f"titularidade_aprovada:{row.correlation_id}:{titular_v.user_id}",
        cta_interno="user.perfil",
        referencia_dominio=f"titularidade:{row.id}",
        commit=False,
    )
    criar_notificacao(
        user_id=int(candidato_v.user_id),
        conta_id=int(conta.id),
        tipo="titularidade_aprovada",
        mensagem="Você passou a ser o Contratante desta Conta.",
        dedup_key=f"titularidade_aprovada:{row.correlation_id}:{candidato_v.user_id}",
        cta_interno="multiuser_painel.gestao_multiuser",
        referencia_dominio=f"titularidade:{row.id}",
        commit=False,
    )
    logger.info(
        "evento=titularidade_aprovada conta_id=%s de=%s para=%s",
        conta.id,
        titular_v.user_id,
        candidato_v.user_id,
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    for uid, assunto, texto in (
        (
            int(titular_v.user_id),
            "Titularidade transferida",
            "A titularidade da Conta foi transferida. Você permanece como Membro.",
        ),
        (
            int(candidato_v.user_id),
            "Você é o novo Contratante",
            "A transferência administrativa de titularidade foi aprovada.",
        ),
    ):
        tentar_enviar_email_notificacao(db.session.get(User, uid), assunto, texto)
    return ResultadoTitularidade(
        estado=row.estado,
        replay=False,
        conflito=False,
        solicitacao_id=int(row.id),
        conta_id=int(conta.id),
        versao=int(row.versao),
        mensagem="Titularidade transferida.",
    )


def listar_para_admin(limite: int = 40) -> list[dict]:
    rows = (
        ContaMultiuserTitularidadeSolicitacao.query.order_by(
            ContaMultiuserTitularidadeSolicitacao.id.desc()
        )
        .limit(int(limite))
        .all()
    )
    from app.models import Conta

    out: list[dict] = []
    for row in rows:
        conta = db.session.get(Conta, int(row.conta_id))
        titular = db.session.get(User, int(row.titular_atual_id))
        candidato = db.session.get(User, int(row.candidato_id))
        out.append(
            {
                "id": row.id,
                "conta_id": row.conta_id,
                "conta_nome": conta.nome if conta is not None else f"Conta {row.conta_id}",
                "titular_email": titular.email if titular is not None else None,
                "candidato_email": candidato.email if candidato is not None else None,
                "motivo": row.motivo,
                "estado": row.estado,
                "estado_rotulo": row.estado.replace("_", " ").capitalize(),
                "versao": row.versao,
                "created_at_rotulo": formatar_data_br(row.created_at),
                "pendente": row.estado
                == ContaMultiuserTitularidadeSolicitacao.ESTADO_SOLICITADA,
            }
        )
    return out
