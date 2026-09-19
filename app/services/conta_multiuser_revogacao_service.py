"""Revogação de membership Multiuser (Fase 7). Não reduz quantity nem apaga o User."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass

from flask import has_request_context, session as flask_session
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query

from app.cleiton_doc_escopo import invalidate_session_document_refs
from app.extensions import db
from app.models import (
    Conta,
    ContaVinculoOrganizacional,
    Franquia,
    MonetizacaoFato,
    User,
)
from app.services.cleiton_monetizacao_service import registrar_fato_monetizacao
from app.services.conta_franquia_service import (
    _slugify_local,
    criar_conta_e_franquia_operacional,
)
from app.services.conta_multiuser_aumento_service import exigir_contratante_ativo_gestao
from app.services.conta_multiuser_capacidade_service import bloquear_conta_para_capacidade
from app.services.conta_multiuser_errors import (
    GestaoMultiuserNaoAutorizadaError,
    RevogacaoMultiuserInvalidaError,
    RevogacaoMultiuserNaoAutorizadaError,
)
from app.services.conta_multiuser_notificacao_service import (
    criar_notificacao,
    tentar_enviar_email_notificacao,
)
from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    ESTADO_ENCERRADO,
    PAPEL_CONTRATANTE,
    PAPEL_MEMBRO,
    SLUG_CONTA_SISTEMA,
)
from app.services.conta_organizacional_service import encerrar_vinculo_organizacional

logger = logging.getLogger(__name__)

SESSION_GERACAO_KEY = "mu_sessao_geracao"


@dataclass
class ResultadoRevogacaoMembership:
    vinculo_id: int
    user_id: int
    conta_id: int
    franquia_id: int
    replay: bool
    quantity: int | None
    capacidade_livre: int | None


def _query_user_lock(user_id: int) -> Query:
    return db.session.query(User).filter(User.id == int(user_id)).with_for_update()


def aplicar_revogacao_contexto_sessao() -> None:
    """
    Sessão antiga perde contexto documental Multiuser após revogação.
    O User já é recarregado do banco com o contexto individual.
    """
    from flask import request
    from flask_login import current_user

    if not has_request_context():
        return
    if (request.endpoint or "") == "static":
        return
    if not getattr(current_user, "is_authenticated", False):
        return
    user = current_user._get_current_object()
    expected = int(getattr(user, "sessao_contexto_geracao", 0) or 0)
    got_raw = flask_session.get(SESSION_GERACAO_KEY)
    mismatch = False
    if got_raw is None:
        mismatch = expected > 0
    else:
        try:
            mismatch = int(got_raw) != expected
        except (TypeError, ValueError):
            mismatch = True
    if mismatch:
        invalidate_session_document_refs(flask_session)
        logger.info(
            "evento=sessao_contexto_multiuser_invalidado user_id=%s geracao=%s",
            getattr(user, "id", None),
            expected,
        )
    flask_session[SESSION_GERACAO_KEY] = expected


def _bump_geracao_sessao(user: User) -> None:
    atual = int(getattr(user, "sessao_contexto_geracao", 0) or 0)
    user.sessao_contexto_geracao = atual + 1
    db.session.add(user)


def _slugs_individuais_do_user(user: User) -> set[str]:
    email = (user.email or "").strip().lower()
    local = email.split("@")[0] if email else ""
    slug_email = _slugify_local(email, f"u{int(user.id)}")
    candidatos = {
        local,
        slug_email,
        f"conta-{slug_email}"[:80],
        f"conta-{int(user.id)}-{slug_email}"[:80],
        f"conta-{int(user.id)}-{local}"[:80] if local else "",
        f"u{int(user.id)}",
        f"conta-u{int(user.id)}"[:80],
    }
    return {item for item in candidatos if item}


def _conta_associada_ao_user(conta: Conta, user: User) -> bool:
    slug = (conta.slug or "").strip().lower()
    if not slug:
        return False
    if slug in _slugs_individuais_do_user(user):
        return True
    slug_email = _slugify_local((user.email or "").strip().lower(), f"u{int(user.id)}")
    prefixo = f"conta-{slug_email}-"
    return bool(slug_email) and slug.startswith(prefixo) and len(slug) > len(prefixo)


def _franquia_individual_elegivel(conta: Conta, *, conta_org_id: int) -> Franquia | None:
    if int(conta.id) == int(conta_org_id):
        return None
    if (conta.slug or "") == SLUG_CONTA_SISTEMA:
        return None
    if conta.status != Conta.STATUS_ATIVA:
        return None
    if bool(conta.multiuser_ativa):
        return None
    if User.query.filter_by(conta_id=int(conta.id)).count() > 0:
        return None
    return (
        Franquia.query.filter_by(conta_id=int(conta.id))
        .order_by(Franquia.id.asc())
        .first()
    )


def _conta_id_restaurada_em_fato(user_id: int, conta_org_id: int) -> int | None:
    rows = (
        MonetizacaoFato.query.filter_by(
            tipo_fato="membership_revogado",
            conta_id=int(conta_org_id),
        )
        .order_by(MonetizacaoFato.id.desc())
        .limit(40)
        .all()
    )
    for row in rows:
        raw = row.snapshot_normalizado_json or "{}"
        try:
            snap = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(snap, dict):
            continue
        if int(snap.get("alvo_user_id") or 0) != int(user_id):
            continue
        cid = snap.get("conta_individual_id")
        if cid is not None:
            return int(cid)
    return None


def _preferir_beneficio_pago(
    candidatos: list[tuple[Conta, Franquia]],
) -> tuple[Conta, Franquia]:
    from app.services.conta_multiuser_convite_service import (
        BENEFICIO_PAGO_VIGENTE,
        decidir_beneficio_pago_para_transferencia,
    )

    def _chave(item: tuple[Conta, Franquia]) -> tuple[int, int]:
        conta, _franquia = item
        pago = (
            0
            if decidir_beneficio_pago_para_transferencia(int(conta.id))
            != BENEFICIO_PAGO_VIGENTE
            else 1
        )
        return (-pago, int(conta.id))

    return sorted(candidatos, key=_chave)[0]


def _encontrar_conta_individual_restauravel(
    user: User,
    conta_org_id: int,
) -> tuple[Conta, Franquia] | None:
    """Reutiliza o contexto individual deste User, não qualquer Conta vazia do sistema."""
    associados: list[tuple[Conta, Franquia]] = []
    for conta in (
        Conta.query.filter(
            Conta.id != int(conta_org_id),
            Conta.slug != SLUG_CONTA_SISTEMA,
            Conta.status == Conta.STATUS_ATIVA,
            Conta.multiuser_ativa.is_(False),
        )
        .order_by(Conta.id.asc())
        .all()
    ):
        franquia = _franquia_individual_elegivel(conta, conta_org_id=conta_org_id)
        if franquia is None:
            continue
        if _conta_associada_ao_user(conta, user):
            associados.append((conta, franquia))
    if associados:
        return _preferir_beneficio_pago(associados)

    anterior_id = _conta_id_restaurada_em_fato(int(user.id), int(conta_org_id))
    if anterior_id is None:
        return None
    anterior = db.session.get(Conta, int(anterior_id))
    if anterior is None:
        return None
    franquia = _franquia_individual_elegivel(anterior, conta_org_id=conta_org_id)
    if franquia is None:
        return None
    return anterior, franquia


def _criar_conta_individual_para_restauracao(user: User) -> tuple[Conta, Franquia]:
    email = user.email or f"u{user.id}"
    nome = (user.full_name or user.email or f"Conta {user.id}")[:255]
    slug_email = _slugify_local(email, f"u{user.id}")
    for _ in range(8):
        slug = f"conta-{slug_email}-{uuid.uuid4().hex[:12]}"[:80]
        if Conta.query.filter_by(slug=slug).first() is not None:
            continue
        try:
            with db.session.begin_nested():
                return criar_conta_e_franquia_operacional(
                    nome_conta=nome,
                    slug_conta=slug,
                    nome_franquia="Principal",
                    slug_franquia="principal",
                )
        except IntegrityError:
            continue
    slug = f"conta-{uuid.uuid4().hex}"[:80]
    return criar_conta_e_franquia_operacional(
        nome_conta=nome,
        slug_conta=slug,
        nome_franquia="Principal",
        slug_franquia="principal",
    )


def _restaurar_contexto_individual(
    user: User,
    conta_org_id: int,
) -> None:
    restaurado = _encontrar_conta_individual_restauravel(user, conta_org_id)
    if restaurado is not None:
        conta, franquia = restaurado
        user.conta_id = conta.id
        user.franquia_id = franquia.id
        from app.services.conta_multiuser_convite_service import (
            BENEFICIO_PAGO_VIGENTE,
            decidir_beneficio_pago_para_transferencia,
        )

        decisao = decidir_beneficio_pago_para_transferencia(int(conta.id))
        if decisao != BENEFICIO_PAGO_VIGENTE:
            user.categoria = "free"
        db.session.add(user)
        db.session.flush()
        return
    conta, franquia = _criar_conta_individual_para_restauracao(user)
    user.conta_id = conta.id
    user.franquia_id = franquia.id
    user.categoria = "free"
    db.session.add(user)
    db.session.flush()


def revogar_membro(
    *,
    ator: User,
    alvo_user_id: int,
    commit: bool = True,
) -> ResultadoRevogacaoMembership:
    try:
        contratante = exigir_contratante_ativo_gestao(ator)
    except GestaoMultiuserNaoAutorizadaError as exc:
        raise RevogacaoMultiuserNaoAutorizadaError(str(exc)) from exc

    conta = bloquear_conta_para_capacidade(int(contratante.conta_id))
    alvo = _query_user_lock(int(alvo_user_id)).one_or_none()
    if alvo is None:
        raise RevogacaoMultiuserInvalidaError("Usuário alvo não encontrado.")
    if int(alvo.id) == int(ator.id):
        raise RevogacaoMultiuserInvalidaError(
            "O Contratante não pode revogar a si mesmo por esta operação."
        )

    vinculo = (
        ContaVinculoOrganizacional.query.filter_by(
            user_id=int(alvo.id),
            conta_id=int(conta.id),
            estado=ESTADO_ATIVO,
        )
        .order_by(ContaVinculoOrganizacional.id.desc())
        .with_for_update()
        .first()
    )
    if vinculo is None:
        encerrado = (
            ContaVinculoOrganizacional.query.filter_by(
                user_id=int(alvo.id),
                conta_id=int(conta.id),
                estado=ESTADO_ENCERRADO,
            )
            .order_by(ContaVinculoOrganizacional.id.desc())
            .first()
        )
        if encerrado is None:
            raise RevogacaoMultiuserInvalidaError(
                "Não há membership Multiuser deste usuário nesta Conta."
            )
        vinculo = encerrado
    if (vinculo.papel or "") == PAPEL_CONTRATANTE:
        raise RevogacaoMultiuserInvalidaError(
            "O Contratante não pode ser revogado por esta operação. "
            "Use a transferência administrativa de titularidade."
        )
    if (vinculo.papel or "") != PAPEL_MEMBRO:
        raise RevogacaoMultiuserInvalidaError("Somente um Membro pode ser revogado.")

    quantity = conta.quantidade_assentos_contratados
    if (vinculo.estado or "") == ESTADO_ENCERRADO:
        snap_livres = None
        if quantity is not None:
            ativos = ContaVinculoOrganizacional.query.filter_by(
                conta_id=int(conta.id), estado=ESTADO_ATIVO
            ).count()
            snap_livres = max(0, int(quantity) - int(ativos))
        if commit:
            db.session.commit()
        return ResultadoRevogacaoMembership(
            vinculo_id=int(vinculo.id),
            user_id=int(alvo.id),
            conta_id=int(conta.id),
            franquia_id=int(vinculo.franquia_id),
            replay=True,
            quantity=quantity,
            capacidade_livre=snap_livres,
        )

    consumo_antes = None
    franquia = db.session.get(Franquia, int(vinculo.franquia_id))
    if franquia is not None:
        consumo_antes = franquia.consumo_acumulado

    encerrar_vinculo_organizacional(int(vinculo.id), commit=False)
    db.session.refresh(vinculo)
    _restaurar_contexto_individual(alvo, int(conta.id))
    _bump_geracao_sessao(alvo)
    if franquia is not None:
        db.session.refresh(franquia)
        if consumo_antes is not None and franquia.consumo_acumulado != consumo_antes:
            raise RevogacaoMultiuserInvalidaError(
                "Revogação não pode alterar o consumo histórico da Franquia."
            )

    registrar_fato_monetizacao(
        tipo_fato="membership_revogado",
        status_tecnico="aplicado",
        conta_id=int(conta.id),
        franquia_id=int(vinculo.franquia_id),
        usuario_id=int(ator.id),
        idempotency_key=f"mu_revogacao:{vinculo.id}",
        correlation_key=f"mu_revogacao:{conta.id}:{vinculo.id}",
        snapshot_normalizado={
            "vinculo_id": int(vinculo.id),
            "alvo_user_id": int(alvo.id),
            "quantity": quantity,
            "papel": PAPEL_MEMBRO,
            "conta_individual_id": int(alvo.conta_id) if alvo.conta_id else None,
        },
    )
    criar_notificacao(
        user_id=int(alvo.id),
        conta_id=int(conta.id),
        tipo="membership_revogado",
        mensagem="Seu acesso Multiuser nesta Conta foi encerrado.",
        dedup_key=f"membership_revogado:{vinculo.id}",
        cta_interno="user.perfil",
        referencia_dominio=f"vinculo:{vinculo.id}",
        commit=False,
    )
    ativos = ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta.id), estado=ESTADO_ATIVO
    ).count()
    livres = None if quantity is None else max(0, int(quantity) - int(ativos))
    logger.info(
        "evento=membership_revogado conta_id=%s vinculo_id=%s alvo_user_id=%s "
        "quantity=%s capacidade_livre=%s",
        conta.id,
        vinculo.id,
        alvo.id,
        quantity,
        livres,
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    tentar_enviar_email_notificacao(
        alvo,
        "Acesso Multiuser encerrado",
        "Seu acesso Multiuser nesta Conta foi encerrado.",
    )
    return ResultadoRevogacaoMembership(
        vinculo_id=int(vinculo.id),
        user_id=int(alvo.id),
        conta_id=int(conta.id),
        franquia_id=int(vinculo.franquia_id),
        replay=False,
        quantity=quantity,
        capacidade_livre=livres,
    )
