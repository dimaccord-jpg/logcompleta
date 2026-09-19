"""
Convites Multiuser (Fase 4): criação, reserva de assento, e-mail, token e aceite.

Não chama Stripe. Não altera quantity, ciclo nem consumo.
Não evolui MultiuserFranquiaCodigo. Código legado nunca ocupa assento.

Locks (compatíveis com F2): Contas por id crescente → convite → User → Franquia.
Duas Contas no transfer: ordem determinística por id para reduzir deadlock.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query

from app.auth_services import send_email
from app.extensions import db
from app.models import (
    AuditoriaGerencial,
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserConvite,
    ContaVinculoOrganizacional,
    Franquia,
    MonetizacaoFato,
    User,
    utcnow_naive,
)
from app.cleiton_doc_escopo import invalidar_sessao_documental_pos_transferencia
from app.services.conta_multiuser_autorizacao_service import (
    exigir_contratante_ativo as exigir_contratante_ativo_compartilhado,
    vinculo_ativo_do_user as _vinculo_ativo_user,
)
from app.services.conta_multiuser_capacidade_service import (
    bloquear_conta_para_capacidade,
    contar_capacidade_comprometida,
    ids_franquias_reservadas_validas,
    liberar_reservas_expiradas_conta,
    limite_ocupacao_conta,
    materializar_franquias_faltantes,
    ocupar_assento,
)
from app.services.conta_multiuser_errors import (
    CapacidadeEsgotadaError,
    ConviteContratanteProprioError,
    ConviteContratoIncompativelError,
    ConviteEmailDivergenteError,
    ConviteExpiradoError,
    ConviteInvalidoError,
    ConviteJaVinculadoError,
    ConviteNaoAutorizadoError,
    VinculoInconsistenteError,
)
from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    ORIGEM_CONVITE,
    PAPEL_CONTRATANTE,
    PAPEL_MEMBRO,
    UQ_CONVITE_CONTA_EMAIL_PENDENTE,
    UQ_CONVITE_FRANQUIA_PENDENTE,
)

logger = logging.getLogger(__name__)

CONVITE_TTL_SEGUNDOS = 3600
CONVITE_TOKEN_SALT = "multiuser-invite-v1"
CONVITE_CSRF_SALT = "multiuser-invite-accept-csrf"
CONVITE_GESTION_CSRF_SALT = "multiuser-invite-manage-csrf"
CONVITE_CSRF_MAX_AGE = 3600

PLANOS_PAGOS_INCOMPATIVEIS = frozenset({"starter", "pro", "multiuser", "enterprise"})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONSTRAINTS_CONVITE = frozenset(
    {UQ_CONVITE_FRANQUIA_PENDENTE, UQ_CONVITE_CONTA_EMAIL_PENDENTE}
)
_SQLITE_UNIQUE_SIGNATURES = (
    (
        "conta_multiuser_convite.franquia_id",
        UQ_CONVITE_FRANQUIA_PENDENTE,
    ),
    (
        UQ_CONVITE_FRANQUIA_PENDENTE,
        UQ_CONVITE_FRANQUIA_PENDENTE,
    ),
    (
        "conta_multiuser_convite.conta_id, conta_multiuser_convite.email_destino",
        UQ_CONVITE_CONTA_EMAIL_PENDENTE,
    ),
    (
        UQ_CONVITE_CONTA_EMAIL_PENDENTE,
        UQ_CONVITE_CONTA_EMAIL_PENDENTE,
    ),
)

BENEFICIO_PAGO_VIGENTE = "beneficio_vigente"
SEM_BENEFICIO_CONCLUSIVAMENTE = "sem_beneficio_conclusivamente"
BENEFICIO_INCONCLUSIVO = "inconclusivo"
MENSAGEM_TRANSFERENCIA_INCONCLUSIVA = (
    "Não foi possível confirmar a elegibilidade da transferência neste momento."
)


@dataclass(frozen=True)
class ResultadoConvite:
    convite_id: int
    conta_id: int
    franquia_id: int
    estado: str
    reutilizado: bool
    token: str
    expires_at: object
    ja_vinculado: bool = False


@dataclass(frozen=True)
class ContextoLandingConvite:
    convite_id: int
    token: str
    empresa_nome: str
    estado: str
    expirado: bool
    autenticado: bool
    email_corresponde: bool
    exige_transferencia: bool
    bloqueio: str | None
    csrf_token: str | None = None


@dataclass(frozen=True)
class ResultadoAceite:
    convite_id: int
    user_id: int
    conta_id: int
    franquia_id: int
    papel: str
    idempotente: bool
    ja_vinculado: bool = False


def normalizar_email_convite(email: str | None) -> str:
    return (email or "").strip().lower()


def _validar_email_destino(email: str) -> str:
    valor = normalizar_email_convite(email)
    if not valor or not _EMAIL_RE.match(valor) or len(valor) > 150:
        raise ValueError("Informe um e-mail de destino válido.")
    return valor


def _agora():
    return utcnow_naive()


def _token_serializer() -> URLSafeTimedSerializer:
    secret = current_app.config["SECRET_KEY"]
    return URLSafeTimedSerializer(secret, salt=CONVITE_TOKEN_SALT)


def _csrf_aceite_serializer() -> URLSafeTimedSerializer:
    secret = current_app.config["SECRET_KEY"]
    return URLSafeTimedSerializer(secret, salt=CONVITE_CSRF_SALT)


def _csrf_gestao_serializer() -> URLSafeTimedSerializer:
    secret = current_app.config["SECRET_KEY"]
    return URLSafeTimedSerializer(secret, salt=CONVITE_GESTION_CSRF_SALT)


def emitir_token_convite(convite_id: int) -> str:
    return _token_serializer().dumps({"invite_id": int(convite_id), "v": 1})


def gerar_csrf_token_aceite(user_id: int, convite_id: int) -> str:
    return _csrf_aceite_serializer().dumps(
        {"u": str(int(user_id)), "i": str(int(convite_id))}
    )


def validar_csrf_token_aceite(token: str | None, user_id: int, convite_id: int) -> bool:
    if not token:
        return False
    try:
        data = _csrf_aceite_serializer().loads(token, max_age=CONVITE_CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("u") == str(int(user_id)) and data.get("i") == str(int(convite_id))


def gerar_csrf_token_gestao_convite(user_id: int) -> str:
    return _csrf_gestao_serializer().dumps(str(int(user_id)))


def validar_csrf_token_gestao_convite(token: str | None, user_id: int) -> bool:
    if not token:
        return False
    try:
        valor = _csrf_gestao_serializer().loads(token, max_age=CONVITE_CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False
    return valor == str(int(user_id))


def decodificar_token_convite(token: str | None) -> int:
    """Valida assinatura temporal. Banco permanece autoridade do convite."""
    if not token or not str(token).strip():
        logger.info("evento=token_invalido motivo=ausente")
        raise ConviteInvalidoError("Convite inválido ou expirado.")
    try:
        data = _token_serializer().loads(str(token), max_age=CONVITE_TTL_SEGUNDOS)
    except SignatureExpired:
        logger.info("evento=token_invalido motivo=assinatura_expirada")
        raise ConviteExpiradoError("Convite inválido ou expirado.")
    except (BadSignature, TypeError, ValueError):
        logger.info("evento=token_invalido motivo=adulterado")
        raise ConviteInvalidoError("Convite inválido ou expirado.")
    if not isinstance(data, dict):
        logger.info("evento=token_invalido motivo=payload")
        raise ConviteInvalidoError("Convite inválido ou expirado.")
    raw_id = data.get("invite_id")
    try:
        convite_id = int(raw_id)
    except (TypeError, ValueError):
        logger.info("evento=token_invalido motivo=invite_id")
        raise ConviteInvalidoError("Convite inválido ou expirado.")
    if convite_id <= 0:
        logger.info("evento=token_invalido motivo=invite_id")
        raise ConviteInvalidoError("Convite inválido ou expirado.")
    return convite_id


def _identificar_constraint_convite(exc: IntegrityError) -> str | None:
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None) if orig is not None else None
    nome_pg = getattr(diag, "constraint_name", None) if diag is not None else None
    if nome_pg is not None and str(nome_pg).strip():
        nome = str(nome_pg).strip()
        return nome if nome in _CONSTRAINTS_CONVITE else None
    msg = str(orig if orig is not None else exc).strip()
    if msg in _CONSTRAINTS_CONVITE:
        return msg
    marker = "UNIQUE constraint failed:"
    if marker in msg:
        spec = msg.split(marker, 1)[1].strip().split("\n", 1)[0].strip()
        if spec in _CONSTRAINTS_CONVITE:
            return spec
        for signature, nome in _SQLITE_UNIQUE_SIGNATURES:
            if spec == signature:
                return nome
    return None


def _query_convite_lock(convite_id: int) -> Query:
    return (
        db.session.query(ContaMultiuserConvite)
        .filter(ContaMultiuserConvite.id == int(convite_id))
        .with_for_update()
    )


def _query_user_lock(user_id: int) -> Query:
    return db.session.query(User).filter(User.id == int(user_id)).with_for_update()


def _query_franquia_lock(franquia_id: int) -> Query:
    return (
        db.session.query(Franquia)
        .filter(Franquia.id == int(franquia_id))
        .with_for_update()
    )


def _bloquear_contas_por_id(*conta_ids: int) -> dict[int, Conta]:
    ids = sorted({int(cid) for cid in conta_ids if cid is not None})
    out: dict[int, Conta] = {}
    for cid in ids:
        out[cid] = bloquear_conta_para_capacidade(cid)
    return out


def exigir_contratante_ativo(user: User) -> ContaVinculoOrganizacional:
    """Autoridade: vínculo organizacional ativo papel=contratante. Não usa categoria/is_admin."""
    return exigir_contratante_ativo_compartilhado(
        user,
        erro_cls=ConviteNaoAutorizadoError,
        mensagem="Somente o Contratante ativo da Conta pode gerenciar convites.",
        evento_log="convite_bloqueado",
    )


def _ids_franquias_ocupadas(conta_id: int) -> set[int]:
    rows = (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta_id),
            estado=ESTADO_ATIVO,
        )
        .all()
    )
    return {int(v.franquia_id) for v in rows}


def _ids_franquias_reservadas_validas(conta_id: int, agora) -> set[int]:
    return ids_franquias_reservadas_validas(conta_id, agora)


def _liberar_reservas_expiradas(conta_id: int, agora) -> int:
    return liberar_reservas_expiradas_conta(conta_id, agora)


def _convite_pendente_valido(
    *,
    conta_id: int,
    email_destino: str,
    agora,
) -> ContaMultiuserConvite | None:
    return (
        ContaMultiuserConvite.query.filter(
            ContaMultiuserConvite.conta_id == int(conta_id),
            ContaMultiuserConvite.email_destino == email_destino,
            ContaMultiuserConvite.estado == ContaMultiuserConvite.ESTADO_PENDENTE,
            ContaMultiuserConvite.expires_at > agora,
        )
        .order_by(ContaMultiuserConvite.id.asc())
        .first()
    )


def _franquias_conta(conta_id: int) -> list[Franquia]:
    return (
        Franquia.query.filter_by(conta_id=int(conta_id))
        .order_by(Franquia.id.asc())
        .all()
    )


def _escolher_franquia_livre_para_reserva(conta_id: int, agora) -> Franquia | None:
    ocupadas = _ids_franquias_ocupadas(conta_id)
    reservadas = _ids_franquias_reservadas_validas(conta_id, agora)
    indisponiveis = ocupadas | reservadas
    for fr in _franquias_conta(conta_id):
        if int(fr.id) not in indisponiveis:
            return fr
    return None


def _nome_empresa(conta: Conta) -> str:
    for raw in (conta.nome_fantasia, conta.razao_social, conta.nome):
        txt = (raw or "").strip()
        if txt:
            return txt
    return "uma organização"


def _vinculo_monetario_ativo(conta_id: int) -> ContaMonetizacaoVinculo | None:
    return (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), ativo=True)
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .first()
    )


def _parse_dt_iso(raw, *, strict: bool = False) -> datetime | None:
    from app.services.cleiton_monetizacao_service import _to_datetime_utc_naive

    return _to_datetime_utc_naive(raw, strict=strict)


def _snapshot_cutoff_futuro(
    vinculo: ContaMonetizacaoVinculo,
    agora,
    *,
    strict: bool = False,
) -> bool:
    if vinculo.vigencia_externa_fim is not None and vinculo.vigencia_externa_fim > agora:
        return True
    raw = vinculo.snapshot_normalizado_json
    if raw is None:
        return False
    from app.services.cleiton_monetizacao_service import (
        _extrair_pendencia_downgrade_snapshot,
        _json_loads,
    )

    if isinstance(raw, str):
        if not raw.strip():
            return False
        parsed = _json_loads(raw, strict=strict)
    elif isinstance(raw, dict):
        parsed = raw
    elif strict:
        raise TypeError("snapshot_normalizado_json presente com tipo invalido")
    else:
        return False
    if strict:
        pend = _extrair_pendencia_downgrade_snapshot(parsed, strict=True)
        if pend is None:
            return False
        efetivar = _parse_dt_iso(pend.get("efetivar_em"), strict=True)
        return efetivar is not None and efetivar > agora
    if not parsed.get("mudanca_pendente"):
        return False
    efetivar = _parse_dt_iso(parsed.get("efetivar_em"), strict=strict)
    return efetivar is not None and efetivar > agora


def _conta_tem_ciclo_operacional_futuro(conta_id: int, agora) -> bool:
    for fr in Franquia.query.filter_by(conta_id=int(conta_id)).all():
        if fr.fim_ciclo is not None and fr.fim_ciclo > agora:
            return True
    return False


def _conta_tem_concessao_paga_operacional_futura(conta_id: int, agora) -> bool:
    """
    Concessão Starter/Pro persistida pelo produto (admin ou sync operacional)
    com ciclo futuro. Categoria sozinha não basta; Avulso com ciclo não bloqueia.
    """
    if not _conta_tem_ciclo_operacional_futuro(conta_id, agora):
        return False
    return (
        db.session.query(User.id)
        .filter(
            User.conta_id == int(conta_id),
            User.categoria.in_(("starter", "pro")),
        )
        .first()
        is not None
    )


def _validar_estado_financeiro_transferencia_strict(conta_id: int | None) -> None:
    """
    Barreira única: valida TODO estado financeiro PRESENTE antes de qualquer
    retorno conclusivo da decisão de transferência.

    Ordem obrigatória: carregar → validar estrutural → validar semântico.
    Interpretação e tri-state só acontecem depois desta função retornar.
    PRESENTE + inválido / exception propagam para o caller (inconclusivo).
    """
    if conta_id is None:
        return

    from app.services.cleiton_monetizacao_service import (
        TIPO_FATO_INVOICE_PAID,
        TIPO_FATO_INVOICE_PAYMENT_FAILED,
        _obter_vinculo_ativo_por_conta,
        _validar_estrutura_fato_monetizacao,
        _validar_estrutura_vinculo_monetizacao,
    )

    cid = int(conta_id)
    vistos: set[int] = set()
    monetario = _vinculo_monetario_ativo(cid)
    if monetario is not None:
        _validar_estrutura_vinculo_monetizacao(monetario, strict=True)
        vistos.add(int(monetario.id))

    stripe_vinculo = _obter_vinculo_ativo_por_conta(cid)
    if stripe_vinculo is not None and int(stripe_vinculo.id) not in vistos:
        _validar_estrutura_vinculo_monetizacao(stripe_vinculo, strict=True)

    fatos = (
        MonetizacaoFato.query.filter(
            MonetizacaoFato.conta_id == cid,
            MonetizacaoFato.tipo_fato.in_(
                (TIPO_FATO_INVOICE_PAYMENT_FAILED, TIPO_FATO_INVOICE_PAID)
            ),
        )
        .order_by(MonetizacaoFato.timestamp_interno.desc(), MonetizacaoFato.id.desc())
        .all()
    )
    for fato in fatos:
        _validar_estrutura_fato_monetizacao(fato, strict=True)


def conta_possui_beneficio_pago_vigente(
    conta_id: int | None, *, agora=None, strict: bool = True
) -> bool:
    """
    Source of truth composta: vínculo Stripe pago vigente, pendência de cutoff,
    falha mensal vigente e concessão Starter/Pro com ciclo futuro persistida
    pelo produto. As autoridades atuais são analisadas em conjunto.

    Vínculo monetário Free persistido (ex.: assinatura encerrada) não apaga
    concessão administrativa Starter/Pro ainda vigente. Categoria sozinha não
    bloqueia. Avulso com ciclo não é incompatível.

    strict=True (F4): falha interna do resolver não vira ausência de benefício.

    Interpretação somente. A validação de estado financeiro PRESENTE ocorre em
    _validar_estado_financeiro_transferencia_strict, antes de qualquer
    retorno conclusivo de decidir_beneficio_pago_para_transferencia.
    """
    if conta_id is None:
        return False
    momento = agora or utcnow_naive()
    cid = int(conta_id)

    from app.services.cleiton_monetizacao_service import (
        obter_pendencia_downgrade_conta_ativa,
        require_optional_plano_strict,
        resolver_falha_mensal_vigente_conta,
    )

    falha = resolver_falha_mensal_vigente_conta(cid, strict=strict)
    if falha.get("falha_mensal_vigente"):
        return True

    monetario = _vinculo_monetario_ativo(cid)
    if monetario is not None:
        if strict:
            plano_n = require_optional_plano_strict(
                monetario.plano_interno, nome="plano_interno"
            )
            plano = plano_n or ""
        else:
            plano = (monetario.plano_interno or "").strip().lower()
    else:
        plano = ""
    pago = plano in PLANOS_PAGOS_INCOMPATIVEIS
    if monetario is not None and pago:
        if monetario.vigencia_externa_fim is not None and monetario.vigencia_externa_fim > momento:
            return True

    pend = obter_pendencia_downgrade_conta_ativa(cid, strict=strict)
    if pend is not None:
        efetivar = _parse_dt_iso(pend.get("efetivar_em"), strict=strict)
        if efetivar is not None and efetivar > momento:
            return True

    if monetario is None or not pago:
        return _conta_tem_concessao_paga_operacional_futura(cid, momento)

    if monetario.vigencia_externa_fim is not None:
        if pend is not None:
            efetivar = _parse_dt_iso(pend.get("efetivar_em"), strict=strict)
            if efetivar is not None and efetivar <= momento:
                if not _conta_tem_ciclo_operacional_futuro(cid, momento):
                    return False
        elif not _conta_tem_ciclo_operacional_futuro(cid, momento):
            return False

    if _snapshot_cutoff_futuro(monetario, momento, strict=strict):
        return True
    if _conta_tem_ciclo_operacional_futuro(cid, momento):
        return True
    return True


def decidir_beneficio_pago_para_transferencia(conta_id: int | None, *, agora=None) -> str:
    """
    Decisão tri-state para transferência F4.

    Transferência só é permitida em SEM_BENEFICIO_CONCLUSIVAMENTE.

    Ordem: pré-validar estado financeiro PRESENTE → interpretar regras → tri-state.
    Nenhum retorno conclusivo ocorre antes da barreira de pré-validação.
    """
    try:
        _validar_estado_financeiro_transferencia_strict(conta_id)
        if conta_possui_beneficio_pago_vigente(conta_id, agora=agora, strict=True):
            return BENEFICIO_PAGO_VIGENTE
        return SEM_BENEFICIO_CONCLUSIVAMENTE
    except Exception:
        logger.info("evento=decisao_financeira_inconclusiva conta_id=%s", conta_id)
        return BENEFICIO_INCONCLUSIVO


def contrato_pago_incompativel(user: User) -> str | None:
    """
    Motivo de bloqueio para landing/GET. A decisão financeira final do aceite
    usa exclusivamente decidir_beneficio_pago_para_transferencia (tri-state).
    Não consulta Stripe live. Avulso não é incompatível só pela categoria.
    """
    vinculo_org = _vinculo_ativo_user(user.id)
    if vinculo_org is not None:
        if (vinculo_org.papel or "") == PAPEL_CONTRATANTE:
            return "contratante_outra_conta"
        return "membro_multiuser_outra_conta"

    if not user.conta_id:
        return None
    decisao = decidir_beneficio_pago_para_transferencia(int(user.conta_id))
    if decisao == BENEFICIO_INCONCLUSIVO:
        return "inconclusivo"
    if decisao != BENEFICIO_PAGO_VIGENTE:
        return None
    monetario = _vinculo_monetario_ativo(int(user.conta_id))
    plano = (monetario.plano_interno or "").strip().lower() if monetario is not None else ""
    if plano in PLANOS_PAGOS_INCOMPATIVEIS:
        return f"contrato_{plano}"
    return "contrato_pago_vigente"


def _user_por_email(email: str) -> User | None:
    return (
        User.query.filter(func.lower(User.email) == email)
        .order_by(User.id.asc())
        .first()
    )


def _montar_email_convite(*, empresa: str, invite_url: str) -> tuple[str, str, str]:
    subject = "Convite para uma Conta no Agente Frete"
    text = (
        f"Você foi convidado para a Conta {empresa} no Agente Frete.\n\n"
        "Para aceitar o convite, acesse o link abaixo (válido por 1 hora):\n"
        f"{invite_url}\n\n"
        "Se você não esperava este convite, ignore este e-mail.\n"
    )
    html = f"""
<p>Você foi convidado para a Conta <strong>{empresa}</strong> no Agente Frete.</p>
<p>Para aceitar o convite, clique no link abaixo (válido por 1 hora):<br>
<a href="{invite_url}">{invite_url}</a></p>
<p>Se você não esperava este convite, ignore este e-mail.</p>
""".strip()
    return subject, html, text


def _enviar_email_convite(convite: ContaMultiuserConvite, token: str, build_invite_url) -> None:
    invite_url = build_invite_url(token)
    conta = db.session.get(Conta, convite.conta_id)
    empresa = _nome_empresa(conta) if conta is not None else "uma organização"
    subject, html, text = _montar_email_convite(empresa=empresa, invite_url=invite_url)
    send_email(
        to_email=convite.email_destino,
        subject=subject,
        html=html,
        text=text,
    )


def _marcar_envio_realizado(convite: ContaMultiuserConvite, *, reenvio: bool) -> None:
    """H09 / FSD §§49–50: enviado_em só após sucesso real da projeção de envio."""
    agora = _agora()
    if convite.enviado_em is None:
        convite.enviado_em = agora
    if reenvio:
        convite.reenviado_em = agora
    convite.updated_at = agora
    db.session.add(convite)
    db.session.flush()
    logger.info(
        "evento=convite_enviado convite_id=%s conta_id=%s franquia_id=%s reenvio=%s",
        convite.id,
        convite.conta_id,
        convite.franquia_id,
        1 if reenvio else 0,
    )


def _enviar_e_marcar_se_solicitado(
    convite: ContaMultiuserConvite,
    token: str,
    build_invite_url,
    *,
    enviar: bool,
    reenvio: bool,
    commit: bool,
) -> None:
    if not enviar or build_invite_url is None:
        return
    try:
        _enviar_email_convite(convite, token, build_invite_url)
    except Exception:
        logger.error(
            "evento=convite_email_falhou convite_id=%s conta_id=%s category=email_delivery",
            convite.id,
            convite.conta_id,
        )
        raise
    _marcar_envio_realizado(convite, reenvio=reenvio)
    if commit:
        db.session.commit()


def _registrar_trilha_transferencia_convite(
    *,
    user: User,
    convite: ContaMultiuserConvite,
    conta_anterior_id: int,
    conta_nova_id: int,
    vinculo_id: int,
    agora,
) -> None:
    """H07 / FSD §19 / CA-54: trilha durável da Conta anterior → nova no aceite."""
    db.session.add(
        AuditoriaGerencial(
            tipo_decisao="multiuser_convite_transferencia",
            decisao="aceite",
            contexto_json=json.dumps(
                {
                    "user_id": int(user.id),
                    "conta_anterior_id": int(conta_anterior_id),
                    "conta_nova_id": int(conta_nova_id),
                    "convite_id": int(convite.id),
                    "vinculo_id": int(vinculo_id),
                    "origem": ORIGEM_CONVITE,
                    "accepted_at": agora.isoformat() if agora is not None else None,
                },
                sort_keys=True,
            ),
            resultado="sucesso",
            detalhe="transferencia_por_convite",
            created_at=agora,
        )
    )
    db.session.flush()


def _resultado(convite: ContaMultiuserConvite, token: str, *, reutilizado: bool, ja_vinculado: bool = False) -> ResultadoConvite:
    return ResultadoConvite(
        convite_id=int(convite.id),
        conta_id=int(convite.conta_id),
        franquia_id=int(convite.franquia_id),
        estado=convite.estado,
        reutilizado=reutilizado,
        token=token,
        expires_at=convite.expires_at,
        ja_vinculado=ja_vinculado,
    )


def criar_convite(
    *,
    ator: User,
    email_destino: str,
    conta_id_payload: int | None = None,
    build_invite_url=None,
    enviar: bool = True,
    commit: bool = True,
) -> ResultadoConvite:
    """
    Cria ou reutiliza convite pendente. Conta vem do vínculo do ator.
    conta_id_payload é ignorado deliberadamente.
    """
    _ = conta_id_payload  # nunca confiar no browser
    email = _validar_email_destino(email_destino)
    vinculo = exigir_contratante_ativo(ator)
    conta_id = int(vinculo.conta_id)
    agora = _agora()

    conta = bloquear_conta_para_capacidade(conta_id)
    _liberar_reservas_expiradas(conta.id, agora)

    existente_user = _user_por_email(email)
    if existente_user is not None:
        vinculo_dest = _vinculo_ativo_user(existente_user.id)
        if vinculo_dest is not None and int(vinculo_dest.conta_id) == int(conta.id):
            if (vinculo_dest.papel or "") == PAPEL_CONTRATANTE:
                logger.info(
                    "evento=convite_bloqueado motivo=contratante_proprio conta_id=%s user_id=%s",
                    conta.id,
                    existente_user.id,
                )
                raise ConviteContratanteProprioError(
                    "O Contratante da Conta não pode ser convidado como Membro."
                )
            logger.info(
                "evento=convite_idempotente motivo=ja_vinculado conta_id=%s user_id=%s",
                conta.id,
                existente_user.id,
            )
            pendente = _convite_pendente_valido(
                conta_id=conta.id, email_destino=email, agora=agora
            )
            if pendente is not None:
                pendente.estado = ContaMultiuserConvite.ESTADO_EXPIRADO
                pendente.updated_at = agora
                db.session.add(pendente)
                db.session.flush()
            if commit:
                db.session.commit()
            raise ConviteJaVinculadoError("Este e-mail já está vinculado à Conta.")

    pendente = _convite_pendente_valido(
        conta_id=conta.id, email_destino=email, agora=agora
    )
    if pendente is not None:
        token = emitir_token_convite(pendente.id)
        pendente.updated_at = agora
        db.session.add(pendente)
        db.session.flush()
        if commit:
            db.session.commit()
        _enviar_e_marcar_se_solicitado(
            pendente,
            token,
            build_invite_url,
            enviar=enviar,
            reenvio=True,
            commit=commit,
        )
        logger.info(
            "evento=convite_reenviado convite_id=%s conta_id=%s franquia_id=%s",
            pendente.id,
            pendente.conta_id,
            pendente.franquia_id,
        )
        return _resultado(pendente, token, reutilizado=True)

    qtd = conta.quantidade_assentos_contratados
    if qtd is None:
        logger.info(
            "evento=aceite_bloqueado_capacidade motivo=quantity_ausente conta_id=%s",
            conta.id,
        )
        raise CapacidadeEsgotadaError(
            "Capacidade Multiuser indisponível para novos convites."
        )

    ocupadas = _ids_franquias_ocupadas(conta.id)
    reservadas = _ids_franquias_reservadas_validas(conta.id, agora)
    teto = limite_ocupacao_conta(conta)
    if teto is None:
        teto = int(qtd)
    if contar_capacidade_comprometida(conta.id, agora) >= int(teto):
        logger.info(
            "evento=aceite_bloqueado_capacidade motivo=sem_assento conta_id=%s ocupados=%s reservados=%s qtd=%s",
            conta.id,
            len(ocupadas),
            len(reservadas),
            qtd,
        )
        raise CapacidadeEsgotadaError(
            "Não há assento livre para um novo convite."
        )

    franquias = _franquias_conta(conta.id)
    if len(franquias) < int(qtd):
        limite = franquias[0].limite_total if franquias else None
        materializar_franquias_faltantes(
            conta,
            int(qtd),
            limite_referencia=limite,
            aplicar_limite_nas_existentes=False,
        )

    alvo = _escolher_franquia_livre_para_reserva(conta.id, agora)
    if alvo is None:
        logger.info(
            "evento=aceite_bloqueado_capacidade motivo=sem_franquia_livre conta_id=%s",
            conta.id,
        )
        raise CapacidadeEsgotadaError(
            "Não há assento livre para um novo convite."
        )
    alvo = _query_franquia_lock(alvo.id).one()
    if int(alvo.conta_id) != int(conta.id):
        raise VinculoInconsistenteError("Franquia não pertence à Conta informada.")

    expires_at = agora + timedelta(seconds=CONVITE_TTL_SEGUNDOS)
    row = ContaMultiuserConvite(
        conta_id=conta.id,
        franquia_id=alvo.id,
        criado_por_user_id=ator.id,
        email_destino=email,
        estado=ContaMultiuserConvite.ESTADO_PENDENTE,
        created_at=agora,
        updated_at=agora,
        expires_at=expires_at,
        enviado_em=None,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        nome = _identificar_constraint_convite(exc)
        if nome == UQ_CONVITE_CONTA_EMAIL_PENDENTE:
            existente = (
                ContaMultiuserConvite.query.filter_by(
                    conta_id=conta.id,
                    email_destino=email,
                    estado=ContaMultiuserConvite.ESTADO_PENDENTE,
                )
                .order_by(ContaMultiuserConvite.id.asc())
                .first()
            )
            if existente is not None:
                token = emitir_token_convite(existente.id)
                existente.updated_at = agora
                db.session.add(existente)
                db.session.flush()
                if commit:
                    db.session.commit()
                _enviar_e_marcar_se_solicitado(
                    existente,
                    token,
                    build_invite_url,
                    enviar=enviar,
                    reenvio=True,
                    commit=commit,
                )
                logger.info(
                    "evento=convite_reenviado convite_id=%s conta_id=%s franquia_id=%s motivo=corrida_email",
                    existente.id,
                    existente.conta_id,
                    existente.franquia_id,
                )
                return _resultado(existente, token, reutilizado=True)
        if nome == UQ_CONVITE_FRANQUIA_PENDENTE:
            logger.info(
                "evento=aceite_bloqueado_capacidade motivo=unique_reserva conta_id=%s constraint=%s",
                conta.id,
                nome,
            )
            raise CapacidadeEsgotadaError(
                "Não há assento livre para um novo convite."
            ) from exc
        raise

    token = emitir_token_convite(row.id)
    if commit:
        db.session.commit()
    logger.info(
        "evento=convite_criado convite_id=%s conta_id=%s franquia_id=%s",
        row.id,
        row.conta_id,
        row.franquia_id,
    )
    _enviar_e_marcar_se_solicitado(
        row,
        token,
        build_invite_url,
        enviar=enviar,
        reenvio=False,
        commit=commit,
    )
    return _resultado(row, token, reutilizado=False)


def reenviar_convite(
    *,
    ator: User,
    convite_id: int | None = None,
    email_destino: str | None = None,
    conta_id_payload: int | None = None,
    build_invite_url=None,
    enviar: bool = True,
    commit: bool = True,
) -> ResultadoConvite:
    _ = conta_id_payload
    vinculo = exigir_contratante_ativo(ator)
    conta_id = int(vinculo.conta_id)
    agora = _agora()
    conta = bloquear_conta_para_capacidade(conta_id)
    _liberar_reservas_expiradas(conta.id, agora)

    alvo: ContaMultiuserConvite | None = None
    if convite_id is not None:
        alvo = (
            ContaMultiuserConvite.query.filter_by(
                id=int(convite_id),
                conta_id=conta.id,
            )
            .first()
        )
        if alvo is None:
            raise ConviteInvalidoError("Convite inválido ou expirado.")
        email = alvo.email_destino
    else:
        email = _validar_email_destino(email_destino)
        alvo = (
            ContaMultiuserConvite.query.filter_by(
                conta_id=conta.id,
                email_destino=email,
            )
            .order_by(ContaMultiuserConvite.id.desc())
            .first()
        )

    if alvo is None:
        return criar_convite(
            ator=ator,
            email_destino=email,
            build_invite_url=build_invite_url,
            enviar=enviar,
            commit=commit,
        )

    if alvo.estado == ContaMultiuserConvite.ESTADO_ACEITO:
        raise ConviteJaVinculadoError("Este convite já foi aceito.")

    if (
        alvo.estado == ContaMultiuserConvite.ESTADO_PENDENTE
        and alvo.expires_at > agora
    ):
        token = emitir_token_convite(alvo.id)
        alvo.updated_at = agora
        db.session.add(alvo)
        db.session.flush()
        if commit:
            db.session.commit()
        _enviar_e_marcar_se_solicitado(
            alvo,
            token,
            build_invite_url,
            enviar=enviar,
            reenvio=True,
            commit=commit,
        )
        logger.info(
            "evento=convite_reenviado convite_id=%s conta_id=%s franquia_id=%s",
            alvo.id,
            alvo.conta_id,
            alvo.franquia_id,
        )
        return _resultado(alvo, token, reutilizado=True)

    return criar_convite(
        ator=ator,
        email_destino=alvo.email_destino,
        build_invite_url=build_invite_url,
        enviar=enviar,
        commit=commit,
    )


def _carregar_convite_por_token(token: str) -> ContaMultiuserConvite:
    convite_id = decodificar_token_convite(token)
    convite = db.session.get(ContaMultiuserConvite, convite_id)
    if convite is None:
        logger.info("evento=token_invalido motivo=inexistente convite_id=%s", convite_id)
        raise ConviteInvalidoError("Convite inválido ou expirado.")
    return convite


def convite_pendente_valido(convite: ContaMultiuserConvite, agora=None) -> bool:
    agora = agora or _agora()
    return (
        convite.estado == ContaMultiuserConvite.ESTADO_PENDENTE
        and convite.expires_at is not None
        and convite.expires_at > agora
    )


def montar_contexto_landing(
    token: str,
    user: User | None,
) -> ContextoLandingConvite:
    """GET: valida superficialmente. Não efetiva vínculo."""
    convite = _carregar_convite_por_token(token)
    agora = _agora()
    expirado = not convite_pendente_valido(convite, agora)
    if convite.estado == ContaMultiuserConvite.ESTADO_ACEITO:
        estado_ui = ContaMultiuserConvite.ESTADO_ACEITO
    elif expirado:
        estado_ui = ContaMultiuserConvite.ESTADO_EXPIRADO
    else:
        estado_ui = ContaMultiuserConvite.ESTADO_PENDENTE

    conta = db.session.get(Conta, convite.conta_id)
    empresa = _nome_empresa(conta) if conta is not None else "uma organização"
    autenticado = user is not None
    email_ok = False
    bloqueio = None
    exige_transferencia = False
    csrf = None

    if expirado and convite.estado == ContaMultiuserConvite.ESTADO_PENDENTE:
        bloqueio = "expirado"
    elif convite.estado == ContaMultiuserConvite.ESTADO_ACEITO:
        bloqueio = "aceito"
    elif autenticado:
        email_ok = normalizar_email_convite(user.email) == normalizar_email_convite(
            convite.email_destino
        )
        if not email_ok:
            bloqueio = "email_divergente"
        else:
            csrf = gerar_csrf_token_aceite(user.id, convite.id)
            vinculo = _vinculo_ativo_user(user.id)
            if vinculo is not None and int(vinculo.conta_id) == int(convite.conta_id):
                if (vinculo.papel or "") == PAPEL_CONTRATANTE:
                    bloqueio = "contratante_proprio"
                else:
                    bloqueio = "ja_vinculado"
            else:
                try:
                    motivo = contrato_pago_incompativel(user)
                except Exception:
                    motivo = "inconclusivo"
                    logger.info(
                        "evento=decisao_financeira_inconclusiva convite_id=%s user_id=%s",
                        convite.id,
                        user.id,
                    )
                if motivo == "contratante_outra_conta":
                    bloqueio = "contratante_outra_conta"
                elif motivo == "membro_multiuser_outra_conta":
                    bloqueio = "membro_outra_conta"
                elif motivo == "inconclusivo":
                    bloqueio = "inconclusivo"
                    logger.info(
                        "evento=decisao_financeira_inconclusiva convite_id=%s user_id=%s",
                        convite.id,
                        user.id,
                    )
                elif motivo:
                    bloqueio = "contrato_incompativel"
                    logger.info(
                        "evento=aceite_bloqueado_contrato convite_id=%s user_id=%s motivo=%s",
                        convite.id,
                        user.id,
                        motivo,
                    )
                elif int(user.conta_id) != int(convite.conta_id):
                    exige_transferencia = True

    return ContextoLandingConvite(
        convite_id=int(convite.id),
        token=token,
        empresa_nome=empresa,
        estado=estado_ui,
        expirado=expirado or convite.estado != ContaMultiuserConvite.ESTADO_PENDENTE,
        autenticado=autenticado,
        email_corresponde=email_ok,
        exige_transferencia=exige_transferencia,
        bloqueio=bloqueio,
        csrf_token=csrf,
    )


def aceitar_convite(
    *,
    user: User,
    token: str,
    csrf_token: str | None,
    commit: bool = True,
) -> ResultadoAceite:
    convite_id = decodificar_token_convite(token)
    agora = _agora()
    convite_preview = db.session.get(ContaMultiuserConvite, convite_id)
    if convite_preview is None:
        logger.info("evento=token_invalido motivo=inexistente convite_id=%s", convite_id)
        raise ConviteInvalidoError("Convite inválido ou expirado.")

    dest_conta_id = int(convite_preview.conta_id)
    origem_conta_id = int(user.conta_id) if user.conta_id else dest_conta_id
    _bloquear_contas_por_id(dest_conta_id, origem_conta_id)

    convite = _query_convite_lock(convite_id).one_or_none()
    if convite is None:
        raise ConviteInvalidoError("Convite inválido ou expirado.")

    user_locked = _query_user_lock(user.id).one_or_none()
    if user_locked is None:
        raise ConviteInvalidoError("Convite inválido ou expirado.")

    if not validar_csrf_token_aceite(csrf_token, user_locked.id, convite.id):
        logger.info(
            "evento=aceite_bloqueado motivo=csrf convite_id=%s user_id=%s",
            convite.id,
            user_locked.id,
        )
        raise ConviteInvalidoError("Convite inválido ou expirado.")

    if normalizar_email_convite(user_locked.email) != normalizar_email_convite(
        convite.email_destino
    ):
        logger.info(
            "evento=aceite_bloqueado motivo=email_divergente convite_id=%s user_id=%s",
            convite.id,
            user_locked.id,
        )
        raise ConviteEmailDivergenteError(
            "O e-mail da sua conta não corresponde a este convite."
        )

    if convite.estado == ContaMultiuserConvite.ESTADO_ACEITO:
        if convite.accepted_user_id and int(convite.accepted_user_id) == int(user_locked.id):
            vinculo = _vinculo_ativo_user(user_locked.id)
            if vinculo is not None and int(vinculo.conta_id) == int(convite.conta_id):
                logger.info(
                    "evento=convite_aceito convite_id=%s user_id=%s idempotente=1",
                    convite.id,
                    user_locked.id,
                )
                if commit:
                    db.session.commit()
                return ResultadoAceite(
                    convite_id=int(convite.id),
                    user_id=int(user_locked.id),
                    conta_id=int(vinculo.conta_id),
                    franquia_id=int(vinculo.franquia_id),
                    papel=vinculo.papel,
                    idempotente=True,
                    ja_vinculado=True,
                )
        logger.info(
            "evento=aceite_bloqueado motivo=ja_aceito convite_id=%s user_id=%s",
            convite.id,
            user_locked.id,
        )
        raise ConviteInvalidoError("Convite inválido ou expirado.")

    if convite.estado != ContaMultiuserConvite.ESTADO_PENDENTE or convite.expires_at <= agora:
        if convite.estado == ContaMultiuserConvite.ESTADO_PENDENTE:
            convite.estado = ContaMultiuserConvite.ESTADO_EXPIRADO
            convite.updated_at = agora
            db.session.add(convite)
            db.session.flush()
            logger.info(
                "evento=convite_expirado convite_id=%s conta_id=%s franquia_id=%s",
                convite.id,
                convite.conta_id,
                convite.franquia_id,
            )
        if commit:
            db.session.commit()
        raise ConviteExpiradoError("Convite inválido ou expirado.")

    vinculo_atual = _vinculo_ativo_user(user_locked.id)
    if vinculo_atual is not None and int(vinculo_atual.conta_id) == int(convite.conta_id):
        if (vinculo_atual.papel or "") == PAPEL_CONTRATANTE:
            convite.estado = ContaMultiuserConvite.ESTADO_EXPIRADO
            convite.updated_at = agora
            db.session.add(convite)
            db.session.flush()
            if commit:
                db.session.commit()
            logger.info(
                "evento=aceite_bloqueado motivo=contratante_proprio convite_id=%s user_id=%s",
                convite.id,
                user_locked.id,
            )
            raise ConviteContratanteProprioError(
                "O Contratante da Conta não pode ser convertido em Membro."
            )
        convite.estado = ContaMultiuserConvite.ESTADO_ACEITO
        convite.accepted_at = agora
        convite.accepted_user_id = user_locked.id
        convite.updated_at = agora
        db.session.add(convite)
        db.session.flush()
        if commit:
            db.session.commit()
        logger.info(
            "evento=convite_aceito convite_id=%s user_id=%s idempotente=1 motivo=ja_vinculado",
            convite.id,
            user_locked.id,
        )
        return ResultadoAceite(
            convite_id=int(convite.id),
            user_id=int(user_locked.id),
            conta_id=int(vinculo_atual.conta_id),
            franquia_id=int(vinculo_atual.franquia_id),
            papel=vinculo_atual.papel,
            idempotente=True,
            ja_vinculado=True,
        )

    vinculo_org_bloqueio = _vinculo_ativo_user(user_locked.id)
    if vinculo_org_bloqueio is not None:
        logger.info(
            "evento=aceite_bloqueado_contrato convite_id=%s user_id=%s motivo=%s",
            convite.id,
            user_locked.id,
            (
                "contratante_outra_conta"
                if (vinculo_org_bloqueio.papel or "") == PAPEL_CONTRATANTE
                else "membro_multiuser_outra_conta"
            ),
        )
        raise ConviteContratoIncompativelError(
            "Não é possível aceitar este convite com o contrato atual."
        )

    decisao = decidir_beneficio_pago_para_transferencia(user_locked.conta_id)
    if decisao == BENEFICIO_INCONCLUSIVO:
        logger.info(
            "evento=aceite_bloqueado_contrato convite_id=%s user_id=%s motivo=inconclusivo",
            convite.id,
            user_locked.id,
        )
        raise RuntimeError(MENSAGEM_TRANSFERENCIA_INCONCLUSIVA)
    if decisao == BENEFICIO_PAGO_VIGENTE:
        logger.info(
            "evento=aceite_bloqueado_contrato convite_id=%s user_id=%s motivo=beneficio_vigente",
            convite.id,
            user_locked.id,
        )
        raise ConviteContratoIncompativelError(
            "Não é possível aceitar este convite com o contrato atual."
        )

    franquia = _query_franquia_lock(convite.franquia_id).one_or_none()
    if franquia is None or int(franquia.conta_id) != int(convite.conta_id):
        logger.info(
            "evento=aceite_bloqueado motivo=reserva_inconsistente convite_id=%s franquia_id=%s",
            convite.id,
            convite.franquia_id,
        )
        raise VinculoInconsistenteError("Reserva de assento inconsistente.")

    ocupante = (
        ContaVinculoOrganizacional.query.filter_by(
            franquia_id=int(franquia.id),
            estado=ESTADO_ATIVO,
        )
        .first()
    )
    if ocupante is not None:
        logger.info(
            "evento=aceite_bloqueado_capacidade motivo=franquia_ocupada convite_id=%s franquia_id=%s",
            convite.id,
            franquia.id,
        )
        raise CapacidadeEsgotadaError("A Franquia reservada não está mais disponível.")

    consumo_antes = franquia.consumo_acumulado
    inicio_antes = franquia.inicio_ciclo
    fim_antes = franquia.fim_ciclo

    try:
        user_locked.conta_id = convite.conta_id
        user_locked.franquia_id = franquia.id
        user_locked.categoria = "multiuser"
        db.session.add(user_locked)
        db.session.flush()

        try:
            vinculo_novo = ocupar_assento(
                conta_id=convite.conta_id,
                user_id=user_locked.id,
                franquia_id=franquia.id,
                papel=PAPEL_MEMBRO,
                origem=ORIGEM_CONVITE,
                criado_por_user_id=convite.criado_por_user_id,
                commit=False,
                convite_id_em_conversao=int(convite.id),
            )
        except CapacidadeEsgotadaError:
            logger.info(
                "evento=aceite_bloqueado_capacidade convite_id=%s conta_id=%s user_id=%s",
                convite.id,
                convite.conta_id,
                user_locked.id,
            )
            raise

        db.session.refresh(franquia)
        if franquia.consumo_acumulado != consumo_antes:
            raise VinculoInconsistenteError("Aceite não pode alterar consumo da Franquia.")
        if franquia.inicio_ciclo != inicio_antes or franquia.fim_ciclo != fim_antes:
            raise VinculoInconsistenteError("Aceite não pode alterar o ciclo da Franquia.")

        convite.estado = ContaMultiuserConvite.ESTADO_ACEITO
        convite.accepted_at = agora
        convite.accepted_user_id = user_locked.id
        convite.updated_at = agora
        db.session.add(convite)
        db.session.flush()
        if int(origem_conta_id) != int(convite.conta_id):
            _registrar_trilha_transferencia_convite(
                user=user_locked,
                convite=convite,
                conta_anterior_id=int(origem_conta_id),
                conta_nova_id=int(convite.conta_id),
                vinculo_id=int(vinculo_novo.id),
                agora=agora,
            )
        if commit:
            db.session.commit()
    except Exception:
        if commit:
            db.session.rollback()
        raise
    if int(origem_conta_id) != int(convite.conta_id):
        invalidar_sessao_documental_pos_transferencia()
    logger.info(
        "evento=convite_aceito convite_id=%s conta_id=%s franquia_id=%s user_id=%s vinculo_id=%s conta_anterior_id=%s",
        convite.id,
        convite.conta_id,
        franquia.id,
        user_locked.id,
        vinculo_novo.id,
        origem_conta_id if int(origem_conta_id) != int(convite.conta_id) else None,
    )
    return ResultadoAceite(
        convite_id=int(convite.id),
        user_id=int(user_locked.id),
        conta_id=int(vinculo_novo.conta_id),
        franquia_id=int(vinculo_novo.franquia_id),
        papel=vinculo_novo.papel,
        idempotente=False,
    )
