"""
Aumento excepcional Multiuser (Fase 6).

Assume solicitações que a Fase 5 enviou para análise. Decisão
administrativa com lock+CAS, snapshot proporcional imutável,
liberação idempotente. Não reabre o automático F5.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import db
from app.models import (
    AuditoriaGerencial,
    Conta,
    ContaMultiuserAumentoExcepcional,
    ContaMultiuserAumentoOperacao,
    Franquia,
    MonetizacaoFato,
    NotificacaoInterna,
    User,
    utcnow_naive,
)
from app.services.conta_multiuser_aumento_service import (
    PRORATION_BEHAVIOR_NONE,
    _aplicar_efeitos_locais_aumento,
    _atualizar_quantity_item_stripe,
    _carregar_assinatura_stripe,
    _divergencia_quantity_relevante,
    _extrair_ids_vinculo,
    _ler_acumulado_ciclo,
    _ler_quantity_stripe_assinatura,
    _registrar_fato,
    _resolver_ciclo_conta,
    _vinculo_monetario_ativo,
    formatar_brl,
    formatar_data_br,
)
from app.services.conta_multiuser_capacidade_service import (
    bloquear_conta_para_capacidade,
)
from app.services.conta_multiuser_errors import (
    AumentoExcepcionalConflitoError,
    AumentoExcepcionalInvalidoError,
    AumentoExcepcionalNaoAutorizadoError,
    AumentoMultiuserCicloIndeterminadoError,
    AumentoMultiuserDivergenteError,
    GestaoMultiuserNaoAutorizadaError,
)
from app.services.plano_service import obter_valor_admin_plano

logger = logging.getLogger(__name__)

CODIGO_MULTIUSER = "multiuser"
FLOW_TYPE_EXTRAORDINARY = "multiuser_extraordinary"
FORMULA_VERSAO = "multiuser_proporcional_v1"
TIMEZONE_CALCULO = "UTC"
MAX_VALIDADE = timedelta(days=5)
STRIPE_CHECKOUT_MAX_TTL = timedelta(hours=24)
_CENTAVOS = Decimal("0.01")
_CEM = Decimal("100")
_CSRF_ADMIN_SALT = "multiuser-admin-ui-csrf"
_CSRF_ADMIN_MAX_AGE = 3600

ROTULO_CLIENTE = {
    ContaMultiuserAumentoExcepcional.ESTADO_EM_ANALISE: "Em análise",
    ContaMultiuserAumentoExcepcional.ESTADO_REJEITADA: "Rejeitada",
    ContaMultiuserAumentoExcepcional.ESTADO_APROVADA_GRATUITA: "Aprovada — sem cobrança",
    ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO: "Aguardando pagamento",
    ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO: "Aguardando pagamento",
    ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA: "Aprovada — sem cobrança",
    ContaMultiuserAumentoExcepcional.ESTADO_EXPIRADA: "Expirada",
    ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA: "Requer revisão",
}


@dataclass(frozen=True)
class SnapshotProporcional:
    preco_unitario_centavos: int
    quantidade_aprovada: int
    ciclo_inicio: datetime
    ciclo_fim: datetime
    instante_calculo: datetime
    timezone_calculo: str
    dias_totais: int
    dias_restantes: int
    valor_calculado_centavos: int
    versao_formula: str
    expires_at: datetime


@dataclass
class ResultadoDecisaoExcepcional:
    estado: str
    decisao: str | None
    replay: bool
    conflito: bool
    versao: int
    excepcional_id: int
    correlation_id: str
    checkout_url: str | None = None
    snapshot: SnapshotProporcional | None = None
    mensagem: str = ""
    franquias_criadas: int = 0
    quantity_nova: int | None = None


def _money(valor: Decimal) -> Decimal:
    return valor.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


def preco_unitario_centavos_atual() -> int:
    valor = obter_valor_admin_plano(CODIGO_MULTIUSER, exigir_configurado=True)
    if valor is None:
        raise AumentoExcepcionalInvalidoError(
            "Valor por assento do plano Multiusuário não está configurado."
        )
    return int((_money(Decimal(valor)) * _CEM).to_integral_value(rounding=ROUND_HALF_UP))


def calcular_dias_ciclo(
    ciclo_inicio: datetime,
    ciclo_fim: datetime,
    instante: datetime,
) -> tuple[int, int]:
    """
    Dias totais e restantes do ciclo canônico.

    dias_totais: diferença de datas civis UTC (fim.date - inicio.date), mínimo 1.
    dias_restantes: se instante >= ciclo_fim → 0; senão max(1, fim.date - instante.date).
    Último dia civil ainda vigente conta como 1.
    """
    if ciclo_inicio is None or ciclo_fim is None:
        raise AumentoExcepcionalInvalidoError("Ciclo incompleto para cálculo proporcional.")
    inicio_d = ciclo_inicio.date()
    fim_d = ciclo_fim.date()
    agora_d = instante.date()
    dias_totais = (fim_d - inicio_d).days
    if dias_totais < 1:
        dias_totais = 1
    if instante >= ciclo_fim:
        return dias_totais, 0
    brutos = (fim_d - agora_d).days
    return dias_totais, 1 if brutos < 1 else brutos


def calcular_valor_proporcional_centavos(
    *,
    preco_unitario_centavos: int,
    quantidade_aprovada: int,
    dias_restantes: int,
    dias_totais: int,
) -> int:
    """Decimal(preço) × qtd × dias_restantes ÷ dias_totais → ROUND_HALF_UP em centavos."""
    if int(dias_totais) < 1:
        raise AumentoExcepcionalInvalidoError("dias_totais inválido.")
    if int(dias_restantes) < 0 or int(quantidade_aprovada) < 1:
        raise AumentoExcepcionalInvalidoError("Operandos proporcionais inválidos.")
    if int(preco_unitario_centavos) < 0:
        raise AumentoExcepcionalInvalidoError("Preço unitário inválido.")
    preco = Decimal(int(preco_unitario_centavos)) / _CEM
    bruto = (
        preco
        * Decimal(int(quantidade_aprovada))
        * Decimal(int(dias_restantes))
        / Decimal(int(dias_totais))
    )
    valor = _money(bruto)
    return int((valor * _CEM).to_integral_value(rounding=ROUND_HALF_UP))


def montar_snapshot_proporcional(
    *,
    ciclo_inicio: datetime,
    ciclo_fim: datetime,
    quantidade_aprovada: int,
    instante: datetime | None = None,
    preco_unitario_centavos: int | None = None,
) -> SnapshotProporcional:
    agora = instante or utcnow_naive()
    preco = (
        int(preco_unitario_centavos)
        if preco_unitario_centavos is not None
        else preco_unitario_centavos_atual()
    )
    dias_totais, dias_restantes = calcular_dias_ciclo(ciclo_inicio, ciclo_fim, agora)
    if dias_restantes < 1:
        raise AumentoExcepcionalInvalidoError(
            "Não há dias restantes no ciclo para cobrança proporcional."
        )
    valor = calcular_valor_proporcional_centavos(
        preco_unitario_centavos=preco,
        quantidade_aprovada=int(quantidade_aprovada),
        dias_restantes=dias_restantes,
        dias_totais=dias_totais,
    )
    limite = agora + MAX_VALIDADE
    expires_at = ciclo_fim if ciclo_fim < limite else limite
    if expires_at > agora + MAX_VALIDADE:
        expires_at = agora + MAX_VALIDADE
    if expires_at <= agora:
        expires_at = agora + timedelta(minutes=30)
        if expires_at > agora + MAX_VALIDADE:
            expires_at = agora + MAX_VALIDADE
    return SnapshotProporcional(
        preco_unitario_centavos=preco,
        quantidade_aprovada=int(quantidade_aprovada),
        ciclo_inicio=ciclo_inicio,
        ciclo_fim=ciclo_fim,
        instante_calculo=agora,
        timezone_calculo=TIMEZONE_CALCULO,
        dias_totais=dias_totais,
        dias_restantes=dias_restantes,
        valor_calculado_centavos=valor,
        versao_formula=FORMULA_VERSAO,
        expires_at=expires_at,
    )


def _exigir_admin_plataforma(admin: User) -> None:
    if getattr(admin, "is_admin", None) is not True:
        raise AumentoExcepcionalNaoAutorizadoError(
            "Somente a equipe administrativa da plataforma pode decidir esta solicitação."
        )


def _rotulo_cliente(row: ContaMultiuserAumentoExcepcional) -> str:
    if (
        row.estado == ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA
        and row.decisao == ContaMultiuserAumentoExcepcional.DECISAO_PAGA
    ):
        return "Liberada"
    return ROTULO_CLIENTE.get(row.estado, "Em análise")


def _snapshot_consumo(conta_id: int) -> dict[str, str]:
    out: dict[str, str] = {}
    for fr in Franquia.query.filter_by(conta_id=int(conta_id)).order_by(Franquia.id.asc()):
        out[str(fr.id)] = str(fr.consumo_acumulado)
    return out


def _auditoria(decisao: str, contexto: dict[str, Any], resultado: str = "sucesso") -> None:
    db.session.add(
        AuditoriaGerencial(
            tipo_decisao="multiuser_aumento_excepcional",
            decisao=decisao,
            contexto_json=json.dumps(contexto, ensure_ascii=True, sort_keys=True, default=str),
            resultado=resultado,
        )
    )


def _notificar_solicitante(row: ContaMultiuserAumentoExcepcional, assunto: str, texto: str) -> None:
    user = db.session.get(User, int(row.solicitante_id))
    if user is None or not (user.email or "").strip():
        return
    try:
        from app.auth_services import send_email

        send_email(
            to_email=user.email,
            subject=assunto,
            html=f"<p>{texto}</p>",
            text=texto,
        )
    except Exception:
        logger.exception(
            "evento=email_excepcional_falhou excepcional_id=%s (decisao preservada)",
            row.id,
        )


def adotar_operacao_enviada_analise(
    operacao: ContaMultiuserAumentoOperacao,
    *,
    commit: bool = False,
) -> ContaMultiuserAumentoExcepcional:
    """Cria o registro F6 1:1 a partir da operação F5 em enviado_analise."""
    existente = ContaMultiuserAumentoExcepcional.query.filter_by(
        operacao_id=int(operacao.id)
    ).one_or_none()
    if existente is not None:
        return existente
    if operacao.estado != ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE:
        raise AumentoExcepcionalInvalidoError(
            "Somente solicitações enviadas para análise entram no fluxo excepcional."
        )
    ciclo = _resolver_ciclo_conta(int(operacao.conta_id))
    acumulado = _ler_acumulado_ciclo(int(operacao.conta_id), ciclo)
    row = ContaMultiuserAumentoExcepcional(
        operacao_id=int(operacao.id),
        conta_id=int(operacao.conta_id),
        solicitante_id=int(operacao.solicitado_por_user_id),
        ciclo_inicio=ciclo.inicio,
        ciclo_fim=ciclo.fim,
        quantity_atual=int(operacao.quantity_anterior),
        quantidade_solicitada=int(operacao.quantidade_solicitada),
        acumulado_automatico_ciclo=int(acumulado),
        estado=ContaMultiuserAumentoExcepcional.ESTADO_EM_ANALISE,
        correlation_id=uuid.uuid4().hex,
        request_id=operacao.correlation_id,
        versao=1,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        existente = ContaMultiuserAumentoExcepcional.query.filter_by(
            operacao_id=int(operacao.id)
        ).one()
        return existente
    _registrar_fato(
        tipo_fato="multiuser_excepcional_adotada",
        status_tecnico="recebido",
        conta_id=int(operacao.conta_id),
        usuario_id=int(operacao.solicitado_por_user_id),
        correlation_id=row.correlation_id,
        idempotency_key=f"mu_exc_adotada:{row.correlation_id}",
        snapshot={
            "operacao_id": int(operacao.id),
            "request_id": row.request_id,
            "quantidade_solicitada": int(operacao.quantidade_solicitada),
            "quantity_atual": int(operacao.quantity_anterior),
            "acumulado_automatico_ciclo": int(acumulado),
        },
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return row


def _adotar_ops_e_comunicar_novas(
    ops: list[ContaMultiuserAumentoOperacao],
) -> list[ContaMultiuserAumentoExcepcional]:
    ids_ops = [int(op.id) for op in ops]
    ja_existiam = set()
    if ids_ops:
        ja_existiam = {
            int(r.operacao_id)
            for r in ContaMultiuserAumentoExcepcional.query.filter(
                ContaMultiuserAumentoExcepcional.operacao_id.in_(ids_ops)
            ).all()
        }
    rows = [adotar_operacao_enviada_analise(op, commit=False) for op in ops]
    for row in rows:
        if int(row.operacao_id) not in ja_existiam:
            comunicar_estado_excepcional(row, "enviado_analise", enviar_email=True)
    db.session.commit()
    return rows


def adotar_pendencias_conta(conta_id: int) -> list[ContaMultiuserAumentoExcepcional]:
    ops = (
        ContaMultiuserAumentoOperacao.query.filter_by(
            conta_id=int(conta_id),
            estado=ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE,
        )
        .order_by(ContaMultiuserAumentoOperacao.id.asc())
        .all()
    )
    return _adotar_ops_e_comunicar_novas(ops)


def adotar_todas_pendencias() -> list[ContaMultiuserAumentoExcepcional]:
    ops = (
        ContaMultiuserAumentoOperacao.query.filter_by(
            estado=ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE,
        )
        .order_by(ContaMultiuserAumentoOperacao.id.asc())
        .all()
    )
    return _adotar_ops_e_comunicar_novas(ops)


def exigir_sem_liberacao_paga_incompleta(conta_id: int) -> None:
    """
    Impede novo aumento enquanto um pagamento F6 já confirmado
    ainda não concluiu a liberação. Não altera o automático F5 em si.
    """
    pendente = (
        ContaMultiuserAumentoExcepcional.query.filter(
            ContaMultiuserAumentoExcepcional.conta_id == int(conta_id),
            ContaMultiuserAumentoExcepcional.decisao
            == ContaMultiuserAumentoExcepcional.DECISAO_PAGA,
            ContaMultiuserAumentoExcepcional.estado.in_(
                (
                    ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
                    ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA,
                )
            ),
            ContaMultiuserAumentoExcepcional.quantity_nova.is_(None),
        )
        .order_by(ContaMultiuserAumentoExcepcional.id.asc())
        .first()
    )
    if pendente is None:
        return
    raise AumentoMultiuserDivergenteError(
        "Há um pagamento extraordinário confirmado aguardando liberação."
    )


def _liberacao_operacional_incompleta(row: ContaMultiuserAumentoExcepcional) -> bool:
    if row.quantity_nova is not None:
        return False
    if row.estado in {
        ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
        ContaMultiuserAumentoExcepcional.ESTADO_APROVADA_GRATUITA,
    }:
        return True
    if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA:
        return _stripe_quantity_desta_operacao(row.correlation_id) is not None
    return False


def exigir_sem_outra_liberacao_incompleta(
    conta_id: int,
    *,
    ignorar_id: int,
    correlation_id: str | None = None,
) -> None:
    """
    Serializa liberações F6 da mesma Conta.

    Operação com efeito Stripe próprio correlacionado pode concluir o
    local mesmo se outra já estiver só com pagamento confirmado.
    Operação sem efeito Stripe próprio não atravessa quem já aplicou Stripe.
    """
    atual_tem_efeito_stripe = (
        _stripe_quantity_desta_operacao(correlation_id) is not None
        if correlation_id
        else False
    )
    outras = (
        ContaMultiuserAumentoExcepcional.query.filter(
            ContaMultiuserAumentoExcepcional.conta_id == int(conta_id),
            ContaMultiuserAumentoExcepcional.id != int(ignorar_id),
            ContaMultiuserAumentoExcepcional.quantity_nova.is_(None),
            ContaMultiuserAumentoExcepcional.estado.in_(
                (
                    ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
                    ContaMultiuserAumentoExcepcional.ESTADO_APROVADA_GRATUITA,
                    ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA,
                )
            ),
        )
        .order_by(ContaMultiuserAumentoExcepcional.id.asc())
        .all()
    )
    for outra in outras:
        if not _liberacao_operacional_incompleta(outra):
            continue
        outra_tem_efeito_stripe = (
            _stripe_quantity_desta_operacao(outra.correlation_id) is not None
        )
        if atual_tem_efeito_stripe and not outra_tem_efeito_stripe:
            continue
        raise AumentoMultiuserDivergenteError(
            "Há outra operação excepcional com liberação incompleta nesta Conta."
        )


def _quantity_corrente_conta(conta: Conta) -> int:
    db.session.refresh(conta)
    atual = conta.quantidade_assentos_contratados
    if atual is None or int(atual) < 1:
        raise AumentoExcepcionalInvalidoError(
            "A Conta não possui quantity contratada para liberação."
        )
    return int(atual)


def _stripe_quantity_desta_operacao(correlation_id: str) -> int | None:
    fato = MonetizacaoFato.query.filter_by(
        idempotency_key=f"mu_exc_stripe_qty:{correlation_id}"
    ).first()
    if fato is None:
        return None
    try:
        snap = json.loads(fato.snapshot_normalizado_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(snap, dict):
        return None
    raw = snap.get("quantity")
    try:
        qtd = int(raw)
    except (TypeError, ValueError):
        return None
    return qtd if qtd >= 1 else None


def _bloquear_e_ler(excepcional_id: int) -> ContaMultiuserAumentoExcepcional:
    row = (
        ContaMultiuserAumentoExcepcional.query.filter_by(id=int(excepcional_id))
        .with_for_update()
        .one_or_none()
    )
    if row is None:
        raise AumentoExcepcionalInvalidoError("Solicitação excepcional não encontrada.")
    bloquear_conta_para_capacidade(int(row.conta_id))
    row = (
        ContaMultiuserAumentoExcepcional.query.filter_by(id=int(excepcional_id))
        .with_for_update()
        .one()
    )
    return row


def _cas_update(row: ContaMultiuserAumentoExcepcional, values: dict[str, Any]) -> bool:
    expected = int(row.versao)
    values = dict(values)
    values["versao"] = expected + 1
    values["updated_at"] = utcnow_naive()
    result = db.session.execute(
        update(ContaMultiuserAumentoExcepcional)
        .where(ContaMultiuserAumentoExcepcional.id == int(row.id))
        .where(ContaMultiuserAumentoExcepcional.versao == expected)
        .where(ContaMultiuserAumentoExcepcional.estado == row.estado)
        .values(**values)
    )
    db.session.flush()
    if int(result.rowcount or 0) != 1:
        return False
    db.session.refresh(row)
    return True


def _marcar_expirada_se_cabivel(row: ContaMultiuserAumentoExcepcional) -> None:
    if row.estado != ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO:
        return
    if row.expires_at is None or utcnow_naive() <= row.expires_at:
        return
    ok = _cas_update(
        row,
        {"estado": ContaMultiuserAumentoExcepcional.ESTADO_EXPIRADA},
    )
    if ok:
        _registrar_fato(
            tipo_fato="multiuser_excepcional_expirada",
            status_tecnico="sem_efeito",
            conta_id=int(row.conta_id),
            usuario_id=row.administrador_id,
            correlation_id=row.correlation_id,
            idempotency_key=f"mu_exc_expirada:{row.correlation_id}",
            snapshot={"expires_at": row.expires_at.isoformat() if row.expires_at else None},
        )
        comunicar_estado_excepcional(row, "expirado", enviar_email=True)


def calcular_preview_decisao_paga(excepcional_id: int) -> dict[str, Any]:
    row = db.session.get(ContaMultiuserAumentoExcepcional, int(excepcional_id))
    if row is None:
        raise AumentoExcepcionalInvalidoError("Solicitação excepcional não encontrada.")
    if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO and row.valor_calculado_centavos is not None:
        return snapshot_para_api(row)
    ciclo = _resolver_ciclo_conta(int(row.conta_id))
    snap = montar_snapshot_proporcional(
        ciclo_inicio=ciclo.inicio,
        ciclo_fim=ciclo.fim,
        quantidade_aprovada=int(row.quantidade_solicitada),
    )
    return {
        "preco_unitario_centavos": snap.preco_unitario_centavos,
        "preco_unitario_rotulo": formatar_brl(Decimal(snap.preco_unitario_centavos) / _CEM),
        "quantidade_aprovada": snap.quantidade_aprovada,
        "ciclo_inicio": snap.ciclo_inicio.isoformat(),
        "ciclo_fim": snap.ciclo_fim.isoformat(),
        "ciclo_rotulo": f"{formatar_data_br(snap.ciclo_inicio)} — {formatar_data_br(snap.ciclo_fim)}",
        "dias_totais": snap.dias_totais,
        "dias_restantes": snap.dias_restantes,
        "valor_calculado_centavos": snap.valor_calculado_centavos,
        "valor_rotulo": formatar_brl(Decimal(snap.valor_calculado_centavos) / _CEM),
        "timezone": snap.timezone_calculo,
        "versao_formula": snap.versao_formula,
        "expires_at": snap.expires_at.isoformat(),
        "expires_at_rotulo": formatar_data_br(snap.expires_at),
        "congelado": False,
    }


def snapshot_para_api(row: ContaMultiuserAumentoExcepcional) -> dict[str, Any]:
    return {
        "preco_unitario_centavos": row.preco_unitario_centavos,
        "preco_unitario_rotulo": formatar_brl(Decimal(row.preco_unitario_centavos or 0) / _CEM),
        "quantidade_aprovada": row.quantidade_aprovada,
        "ciclo_inicio": row.ciclo_inicio.isoformat() if row.ciclo_inicio else None,
        "ciclo_fim": row.ciclo_fim.isoformat() if row.ciclo_fim else None,
        "ciclo_rotulo": f"{formatar_data_br(row.ciclo_inicio)} — {formatar_data_br(row.ciclo_fim)}",
        "dias_totais": row.dias_totais,
        "dias_restantes": row.dias_restantes,
        "valor_calculado_centavos": row.valor_calculado_centavos,
        "valor_rotulo": formatar_brl(Decimal(row.valor_calculado_centavos or 0) / _CEM),
        "timezone": row.timezone_calculo,
        "versao_formula": row.versao_formula,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "expires_at_rotulo": formatar_data_br(row.expires_at),
        "congelado": row.valor_calculado_centavos is not None
        and row.decisao == ContaMultiuserAumentoExcepcional.DECISAO_PAGA,
    }


def _quantidade_aprovada_da_decisao(
    row: ContaMultiuserAumentoExcepcional,
    quantidade_aprovada: Any,
) -> int:
    solicitada = int(row.quantidade_solicitada)
    if quantidade_aprovada is None or str(quantidade_aprovada).strip() == "":
        return solicitada
    try:
        qtd = int(quantidade_aprovada)
    except (TypeError, ValueError) as exc:
        raise AumentoExcepcionalInvalidoError(
            "Quantidade aprovada inválida."
        ) from exc
    if qtd != solicitada:
        raise AumentoExcepcionalInvalidoError(
            "Aprovação parcial não é suportada; use a quantidade solicitada."
        )
    if qtd < 1 or qtd > solicitada:
        raise AumentoExcepcionalInvalidoError(
            "Quantidade aprovada deve ser positiva e não maior que a solicitada."
        )
    return qtd


def decidir_solicitacao_excepcional(
    admin: User,
    excepcional_id: int,
    *,
    decisao: str,
    versao_esperada: int | None = None,
    idempotency_key: str | None = None,
    quantidade_aprovada: Any = None,
    site_origin: str | None = None,
    commit: bool = True,
) -> ResultadoDecisaoExcepcional:
    _exigir_admin_plataforma(admin)
    decisao_n = (decisao or "").strip().lower()
    if decisao_n not in {
        ContaMultiuserAumentoExcepcional.DECISAO_GRATUITA,
        ContaMultiuserAumentoExcepcional.DECISAO_PAGA,
        ContaMultiuserAumentoExcepcional.DECISAO_REJEITADA,
    }:
        raise AumentoExcepcionalInvalidoError("Decisão inválida.")
    chave = (idempotency_key or "").strip() or None
    if chave and len(chave) > 200:
        chave = chave[:200]

    row = _bloquear_e_ler(int(excepcional_id))
    _marcar_expirada_se_cabivel(row)

    if chave and row.decisao_idempotency_key == chave:
        if row.decisao == decisao_n:
            if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_APROVADA_GRATUITA:
                resultado = liberar_assentos_excepcionais(
                    row, motivo="gratuita", commit=commit
                )
                resultado.replay = True
                return resultado
            _registrar_fato(
                tipo_fato="multiuser_excepcional_replay",
                status_tecnico="sem_efeito",
                conta_id=int(row.conta_id),
                usuario_id=int(admin.id),
                correlation_id=row.correlation_id,
                idempotency_key=f"mu_exc_replay_decisao:{chave}",
                snapshot={"decisao": decisao_n, "estado": row.estado},
            )
            if commit:
                db.session.commit()
            return ResultadoDecisaoExcepcional(
                estado=row.estado,
                decisao=row.decisao,
                replay=True,
                conflito=False,
                versao=int(row.versao),
                excepcional_id=int(row.id),
                correlation_id=row.correlation_id,
                checkout_url=row.stripe_checkout_url,
                mensagem="Decisão já registrada.",
                quantity_nova=row.quantity_nova,
            )
        raise AumentoExcepcionalConflitoError(
            "Esta chave de idempotência já registrou outra decisão."
        )

    if row.estado != ContaMultiuserAumentoExcepcional.ESTADO_EM_ANALISE:
        raise AumentoExcepcionalConflitoError(
            "A solicitação já foi decidida; a nova decisão não foi aplicada."
        )
    if versao_esperada is not None and int(versao_esperada) != int(row.versao):
        raise AumentoExcepcionalConflitoError(
            "A solicitação foi alterada por outra decisão simultânea."
        )

    qtd_apr = _quantidade_aprovada_da_decisao(row, quantidade_aprovada)
    agora = utcnow_naive()

    if decisao_n == ContaMultiuserAumentoExcepcional.DECISAO_REJEITADA:
        ok = _cas_update(
            row,
            {
                "estado": ContaMultiuserAumentoExcepcional.ESTADO_REJEITADA,
                "decisao": ContaMultiuserAumentoExcepcional.DECISAO_REJEITADA,
                "decidido_em": agora,
                "administrador_id": int(admin.id),
                "decisao_idempotency_key": chave,
                "quantidade_aprovada": None,
            },
        )
        if not ok:
            raise AumentoExcepcionalConflitoError(
                "A solicitação foi alterada por outra decisão simultânea."
            )
        _registrar_fato(
            tipo_fato="multiuser_excepcional_rejeitada",
            status_tecnico="sem_efeito",
            conta_id=int(row.conta_id),
            usuario_id=int(admin.id),
            correlation_id=row.correlation_id,
            idempotency_key=f"mu_exc_rejeitada:{row.correlation_id}",
            snapshot={"request_id": row.request_id},
        )
        _auditoria("rejeitar", {"excepcional_id": row.id, "conta_id": row.conta_id})
        comunicar_estado_excepcional(row, "rejeitado")
        if commit:
            db.session.commit()
        return ResultadoDecisaoExcepcional(
            estado=row.estado,
            decisao=row.decisao,
            replay=False,
            conflito=False,
            versao=int(row.versao),
            excepcional_id=int(row.id),
            correlation_id=row.correlation_id,
            mensagem="Solicitação rejeitada.",
        )

    if decisao_n == ContaMultiuserAumentoExcepcional.DECISAO_GRATUITA:
        ok = _cas_update(
            row,
            {
                "estado": ContaMultiuserAumentoExcepcional.ESTADO_APROVADA_GRATUITA,
                "decisao": ContaMultiuserAumentoExcepcional.DECISAO_GRATUITA,
                "decidido_em": agora,
                "administrador_id": int(admin.id),
                "decisao_idempotency_key": chave,
                "quantidade_aprovada": qtd_apr,
            },
        )
        if not ok:
            raise AumentoExcepcionalConflitoError(
                "A solicitação foi alterada por outra decisão simultânea."
            )
        _registrar_fato(
            tipo_fato="multiuser_excepcional_decisao_gratuita",
            status_tecnico="recebido",
            conta_id=int(row.conta_id),
            usuario_id=int(admin.id),
            correlation_id=row.correlation_id,
            idempotency_key=f"mu_exc_decisao_gratuita:{row.correlation_id}",
            snapshot={"quantidade_aprovada": qtd_apr},
        )
        _auditoria("aprovar_gratuita", {"excepcional_id": row.id, "conta_id": row.conta_id})
        resultado = liberar_assentos_excepcionais(row, motivo="gratuita", commit=False)
        comunicar_estado_excepcional(row, "aprovado_gratuito")
        if commit:
            db.session.commit()
        return resultado

    ciclo = _resolver_ciclo_conta(int(row.conta_id))
    snap = montar_snapshot_proporcional(
        ciclo_inicio=ciclo.inicio,
        ciclo_fim=ciclo.fim,
        quantidade_aprovada=qtd_apr,
        instante=agora,
    )
    vinculo = _vinculo_monetario_ativo(int(row.conta_id))
    if vinculo is None:
        raise AumentoMultiuserDivergenteError(
            "Vínculo comercial Stripe da Conta não encontrado."
        )
    customer_id, _subscription_id = _extrair_ids_vinculo(vinculo)
    if not customer_id:
        raise AumentoMultiuserDivergenteError(
            "Customer Stripe da Conta ausente."
        )
    from app.services.conta_multiuser_cobranca_extraordinaria_service import (
        criar_checkout_extraordinario,
    )

    session = criar_checkout_extraordinario(
        row=row,
        snapshot=snap,
        customer_id=customer_id,
        site_origin=site_origin,
    )
    ok = _cas_update(
        row,
        {
            "estado": ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO,
            "decisao": ContaMultiuserAumentoExcepcional.DECISAO_PAGA,
            "decidido_em": agora,
            "administrador_id": int(admin.id),
            "decisao_idempotency_key": chave,
            "quantidade_aprovada": qtd_apr,
            "ciclo_inicio": snap.ciclo_inicio,
            "ciclo_fim": snap.ciclo_fim,
            "preco_unitario_centavos": snap.preco_unitario_centavos,
            "instante_calculo": snap.instante_calculo,
            "timezone_calculo": snap.timezone_calculo,
            "dias_totais": snap.dias_totais,
            "dias_restantes": snap.dias_restantes,
            "valor_calculado_centavos": snap.valor_calculado_centavos,
            "versao_formula": snap.versao_formula,
            "expires_at": snap.expires_at,
            "stripe_checkout_session_id": session.get("id"),
            "stripe_payment_intent_id": session.get("payment_intent") or None,
            "stripe_checkout_url": session.get("url"),
            "stripe_customer_id": customer_id,
        },
    )
    if not ok:
        raise AumentoExcepcionalConflitoError(
            "A solicitação foi alterada por outra decisão simultânea."
        )
    _registrar_fato(
        tipo_fato="multiuser_excepcional_decisao_paga",
        status_tecnico="recebido",
        conta_id=int(row.conta_id),
        usuario_id=int(admin.id),
        correlation_id=row.correlation_id,
        customer_id=customer_id,
        idempotency_key=f"mu_exc_decisao_paga:{row.correlation_id}",
        snapshot={
            "preco_unitario_centavos": snap.preco_unitario_centavos,
            "quantidade_aprovada": snap.quantidade_aprovada,
            "valor_calculado_centavos": snap.valor_calculado_centavos,
            "dias_totais": snap.dias_totais,
            "dias_restantes": snap.dias_restantes,
            "versao_formula": snap.versao_formula,
            "expires_at": snap.expires_at.isoformat(),
        },
    )
    _registrar_fato(
        tipo_fato="multiuser_excepcional_snapshot_aprovado",
        status_tecnico="congelado",
        conta_id=int(row.conta_id),
        usuario_id=int(admin.id),
        correlation_id=row.correlation_id,
        idempotency_key=f"mu_exc_snapshot:{row.correlation_id}",
        snapshot={
            "preco_unitario_centavos": snap.preco_unitario_centavos,
            "valor_calculado_centavos": snap.valor_calculado_centavos,
            "ciclo_inicio": snap.ciclo_inicio.isoformat(),
            "ciclo_fim": snap.ciclo_fim.isoformat(),
        },
    )
    _registrar_fato(
        tipo_fato="multiuser_excepcional_cobranca_criada",
        status_tecnico="stripe_ok",
        conta_id=int(row.conta_id),
        usuario_id=int(admin.id),
        correlation_id=row.correlation_id,
        customer_id=customer_id,
        idempotency_key=f"mu_exc_cobranca:{row.correlation_id}",
        snapshot={
            "checkout_session_id": session.get("id"),
            "valor_calculado_centavos": snap.valor_calculado_centavos,
            "flow_type": FLOW_TYPE_EXTRAORDINARY,
        },
    )
    _auditoria("aprovar_paga", {"excepcional_id": row.id, "conta_id": row.conta_id})
    comunicar_estado_excepcional(row, "aguardando_pagamento")
    if commit:
        db.session.commit()
    return ResultadoDecisaoExcepcional(
        estado=row.estado,
        decisao=row.decisao,
        replay=False,
        conflito=False,
        versao=int(row.versao),
        excepcional_id=int(row.id),
        correlation_id=row.correlation_id,
        checkout_url=row.stripe_checkout_url,
        snapshot=snap,
        mensagem="Aprovada com cobrança. Aguarde o pagamento.",
    )


def liberar_assentos_excepcionais(
    row: ContaMultiuserAumentoExcepcional,
    *,
    motivo: str,
    commit: bool = True,
) -> ResultadoDecisaoExcepcional:
    """
    Lock já deve estar na Conta. Aplica quantity + Franquias uma única vez.
    """
    if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA:
        _registrar_fato(
            tipo_fato="multiuser_excepcional_replay",
            status_tecnico="sem_efeito",
            conta_id=int(row.conta_id),
            usuario_id=row.administrador_id,
            correlation_id=row.correlation_id,
            idempotency_key=f"mu_exc_replay_lib:{row.correlation_id}:{motivo}",
            snapshot={"motivo": motivo},
        )
        if commit:
            db.session.commit()
        return ResultadoDecisaoExcepcional(
            estado=row.estado,
            decisao=row.decisao,
            replay=True,
            conflito=False,
            versao=int(row.versao),
            excepcional_id=int(row.id),
            correlation_id=row.correlation_id,
            checkout_url=row.stripe_checkout_url,
            mensagem="Liberação já aplicada.",
            quantity_nova=row.quantity_nova,
        )
    if row.estado not in {
        ContaMultiuserAumentoExcepcional.ESTADO_APROVADA_GRATUITA,
        ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
    }:
        raise AumentoExcepcionalInvalidoError(
            "A solicitação não está pronta para liberação."
        )

    qtd_apr = int(row.quantidade_aprovada or row.quantidade_solicitada)
    if qtd_apr < 1:
        raise AumentoExcepcionalInvalidoError("Quantidade aprovada inválida.")

    bloquear_conta_para_capacidade(int(row.conta_id))
    exigir_sem_outra_liberacao_incompleta(
        int(row.conta_id),
        ignorar_id=int(row.id),
        correlation_id=row.correlation_id,
    )
    conta = db.session.get(Conta, int(row.conta_id))
    if conta is None:
        raise AumentoExcepcionalInvalidoError("Conta não encontrada.")
    ciclo_atual = _resolver_ciclo_conta(int(conta.id))
    if ciclo_atual.inicio != row.ciclo_inicio or ciclo_atual.fim != row.ciclo_fim:
        _cas_update(
            row,
            {"estado": ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA},
        )
        _registrar_fato(
            tipo_fato="multiuser_excepcional_reconciliacao_necessaria",
            status_tecnico="ciclo_incompativel",
            conta_id=int(row.conta_id),
            usuario_id=row.administrador_id,
            correlation_id=row.correlation_id,
            idempotency_key=f"mu_exc_recon_ciclo:{row.correlation_id}",
            snapshot={
                "ciclo_snapshot_inicio": row.ciclo_inicio.isoformat(),
                "ciclo_snapshot_fim": row.ciclo_fim.isoformat(),
                "ciclo_atual_inicio": ciclo_atual.inicio.isoformat() if ciclo_atual.inicio else None,
                "ciclo_atual_fim": ciclo_atual.fim.isoformat() if ciclo_atual.fim else None,
            },
        )
        if commit:
            db.session.commit()
        raise AumentoExcepcionalInvalidoError(
            "O ciclo comercial mudou após a decisão; a liberação exige reconciliação."
        )

    vinculo = _vinculo_monetario_ativo(int(conta.id))
    if vinculo is None:
        raise AumentoMultiuserDivergenteError(
            "Vínculo comercial Stripe da Conta não encontrado."
        )
    customer_id, subscription_id = _extrair_ids_vinculo(vinculo)
    if not customer_id or not subscription_id:
        raise AumentoMultiuserDivergenteError(
            "Customer ou Subscription Stripe da Conta ausente."
        )
    if row.stripe_customer_id and row.stripe_customer_id != customer_id:
        raise AumentoExcepcionalInvalidoError(
            "Customer Stripe diverge do snapshot da cobrança."
        )

    consumo_antes = _snapshot_consumo(int(conta.id))
    ciclo_antes = (ciclo_atual.inicio, ciclo_atual.fim)
    qtd_corrente = _quantity_corrente_conta(conta)
    nova_quantity = int(qtd_corrente) + qtd_apr

    assinatura = _carregar_assinatura_stripe(subscription_id)
    customer_stripe = str(assinatura.get("customer") or "").strip() or None
    if customer_stripe and customer_stripe != customer_id:
        raise AumentoMultiuserDivergenteError(
            "Customer Stripe diverge do vínculo comercial da Conta."
        )
    qty_stripe, item_id = _ler_quantity_stripe_assinatura(assinatura)
    if item_id is None:
        raise AumentoMultiuserDivergenteError(
            "Subscription Item Stripe não encontrado para atualizar a quantity."
        )
    stripe_qty_op = _stripe_quantity_desta_operacao(row.correlation_id)
    ja_atualizado = stripe_qty_op is not None
    if ja_atualizado and int(stripe_qty_op) != int(nova_quantity):
        raise AumentoMultiuserDivergenteError(
            "A quantity Stripe desta operação não corresponde ao alvo corrente."
        )
    if _divergencia_quantity_relevante(conta, qty_stripe) and not ja_atualizado:
        raise AumentoMultiuserDivergenteError(
            "Quantity local e Stripe divergem; a liberação foi bloqueada."
        )

    stripe_key = f"stripe_mu_exc_qty:{row.correlation_id}"[:200]
    if not ja_atualizado:
        item_resp = _atualizar_quantity_item_stripe(
            item_id=item_id,
            nova_quantity=int(nova_quantity),
            idempotency_key=stripe_key,
        )
        if str(item_resp.get("id") or item_id) != item_id:
            raise AumentoMultiuserDivergenteError(
                "A Stripe devolveu um Subscription Item diferente do item atual."
            )
    _registrar_fato(
        tipo_fato="multiuser_excepcional_stripe_quantity_atualizada",
        status_tecnico="stripe_ok",
        conta_id=int(conta.id),
        usuario_id=row.administrador_id,
        correlation_id=row.correlation_id,
        customer_id=customer_id,
        subscription_id=subscription_id,
        idempotency_key=f"mu_exc_stripe_qty:{row.correlation_id}",
        snapshot={
            "quantity": int(nova_quantity),
            "proration_behavior": PRORATION_BEHAVIOR_NONE,
            "item_id": item_id,
        },
    )
    db.session.commit()
    bloquear_conta_para_capacidade(int(conta.id))
    conta = db.session.get(Conta, int(conta.id))
    if conta is None:
        raise AumentoExcepcionalInvalidoError("Conta não encontrada.")
    row = db.session.get(ContaMultiuserAumentoExcepcional, int(row.id))
    if row is None:
        raise AumentoExcepcionalInvalidoError("Solicitação excepcional não encontrada.")
    vinculo = _vinculo_monetario_ativo(int(conta.id))
    if vinculo is None:
        raise AumentoMultiuserDivergenteError(
            "Vínculo comercial Stripe da Conta não encontrado."
        )

    criadas = _aplicar_efeitos_locais_aumento(
        conta=conta,
        nova_quantity=int(nova_quantity),
        vinculo_monetario=vinculo,
    )
    consumo_depois = _snapshot_consumo(int(conta.id))
    ciclo_depois = _resolver_ciclo_conta(int(conta.id))
    _ = consumo_depois
    ok = _cas_update(
        row,
        {
            "estado": ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA,
            "liberado_em": utcnow_naive(),
            "quantity_nova": int(nova_quantity),
            "stripe_customer_id": customer_id,
        },
    )
    if not ok:
        raise AumentoExcepcionalConflitoError(
            "A liberação perdeu a corrida de versão; nenhum efeito adicional foi aplicado."
        )
    _registrar_fato(
        tipo_fato="multiuser_excepcional_liberada",
        status_tecnico="aplicado",
        conta_id=int(conta.id),
        usuario_id=row.administrador_id,
        correlation_id=row.correlation_id,
        customer_id=customer_id,
        subscription_id=subscription_id,
        idempotency_key=f"mu_exc_liberada:{row.correlation_id}",
        snapshot={
            "motivo": motivo,
            "quantity_nova": int(nova_quantity),
            "franquias_criadas": criadas,
            "proration_behavior": PRORATION_BEHAVIOR_NONE,
            "ciclo_inicio": ciclo_antes[0].isoformat() if ciclo_antes[0] else None,
            "ciclo_fim": ciclo_antes[1].isoformat() if ciclo_antes[1] else None,
            "ciclo_depois_inicio": ciclo_depois.inicio.isoformat() if ciclo_depois.inicio else None,
            "ciclo_depois_fim": ciclo_depois.fim.isoformat() if ciclo_depois.fim else None,
            "consumo_antes": consumo_antes,
        },
    )
    logger.info(
        "evento=aumento_excepcional_liberado conta_id=%s correlation_id=%s "
        "motivo=%s quantity_nova=%s franquias_criadas=%s",
        conta.id,
        row.correlation_id,
        motivo,
        nova_quantity,
        criadas,
    )
    if commit:
        comunicar_estado_excepcional(row, "liberado")
        db.session.commit()
    return ResultadoDecisaoExcepcional(
        estado=row.estado,
        decisao=row.decisao,
        replay=False,
        conflito=False,
        versao=int(row.versao),
        excepcional_id=int(row.id),
        correlation_id=row.correlation_id,
        checkout_url=row.stripe_checkout_url,
        mensagem="Assentos liberados.",
        franquias_criadas=criadas,
        quantity_nova=int(nova_quantity),
    )


def marcar_pagamento_confirmado(
    row: ContaMultiuserAumentoExcepcional,
    *,
    payment_intent_id: str | None,
    checkout_session_id: str | None,
) -> ContaMultiuserAumentoExcepcional:
    if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA:
        return row
    if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO:
        return row
    if row.estado != ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO:
        raise AumentoExcepcionalInvalidoError(
            "A solicitação não aguarda pagamento."
        )
    values: dict[str, Any] = {
        "estado": ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
    }
    if payment_intent_id and not row.stripe_payment_intent_id:
        values["stripe_payment_intent_id"] = payment_intent_id
    if checkout_session_id and not row.stripe_checkout_session_id:
        values["stripe_checkout_session_id"] = checkout_session_id
    ok = _cas_update(row, values)
    if not ok and row.estado not in {
        ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
        ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA,
    }:
        raise AumentoExcepcionalConflitoError(
            "Não foi possível confirmar o pagamento desta solicitação."
        )
    comunicar_estado_excepcional(row, "pagamento_confirmado", enviar_email=False)
    return row


def marcar_reconciliacao(
    row: ContaMultiuserAumentoExcepcional,
    *,
    motivo: str,
    snapshot: dict[str, Any] | None = None,
) -> None:
    if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_LIBERADA:
        return
    if row.estado != ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA:
        _cas_update(
            row,
            {"estado": ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA},
        )
    _registrar_fato(
        tipo_fato="multiuser_excepcional_reconciliacao_necessaria",
        status_tecnico=motivo,
        conta_id=int(row.conta_id),
        usuario_id=row.administrador_id,
        correlation_id=row.correlation_id,
        idempotency_key=f"mu_exc_recon:{row.correlation_id}:{motivo}",
        snapshot=snapshot or {"motivo": motivo},
    )
    comunicar_estado_excepcional(row, "reconciliacao_necessaria", enviar_email=True)


def listar_para_admin() -> list[dict[str, Any]]:
    adotar_todas_pendencias()
    rows = (
        ContaMultiuserAumentoExcepcional.query.order_by(
            ContaMultiuserAumentoExcepcional.id.desc()
        )
        .limit(80)
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        conta = db.session.get(Conta, int(row.conta_id))
        solicitante = db.session.get(User, int(row.solicitante_id))
        preview = None
        if row.estado == ContaMultiuserAumentoExcepcional.ESTADO_EM_ANALISE:
            try:
                preview = calcular_preview_decisao_paga(int(row.id))
            except (
                AumentoExcepcionalInvalidoError,
                AumentoMultiuserCicloIndeterminadoError,
            ):
                preview = None
        elif row.valor_calculado_centavos is not None:
            preview = snapshot_para_api(row)
        out.append(
            {
                "id": row.id,
                "conta_id": row.conta_id,
                "conta_nome": conta.nome if conta is not None else f"Conta {row.conta_id}",
                "conta_cnpj": (conta.cnpj if conta is not None else None),
                "solicitante_email": (solicitante.email if solicitante is not None else None),
                "quantity_atual": row.quantity_atual,
                "quantidade_solicitada": row.quantidade_solicitada,
                "acumulado_automatico_ciclo": int(row.acumulado_automatico_ciclo or 0),
                "ciclo_rotulo": f"{formatar_data_br(row.ciclo_inicio)} — {formatar_data_br(row.ciclo_fim)}",
                "estado": row.estado,
                "estado_rotulo": _rotulo_cliente(row),
                "created_at_rotulo": formatar_data_br(row.created_at),
                "versao": row.versao,
                "em_analise": row.estado == ContaMultiuserAumentoExcepcional.ESTADO_EM_ANALISE,
                "preview": preview,
            }
        )
    return out


def listar_para_contratante(conta_id: int) -> list[dict[str, Any]]:
    adotar_pendencias_conta(int(conta_id))
    rows = (
        ContaMultiuserAumentoExcepcional.query.filter_by(conta_id=int(conta_id))
        .order_by(ContaMultiuserAumentoExcepcional.id.desc())
        .all()
    )
    agora = utcnow_naive()
    out: list[dict[str, Any]] = []
    for row in rows:
        _marcar_expirada_se_cabivel(row)
        checkout_valido = (
            row.estado == ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO
            and bool(row.stripe_checkout_url)
            and (row.expires_at is None or agora <= row.expires_at)
        )
        out.append(
            {
                "id": row.id,
                "estado_rotulo": _rotulo_cliente(row),
                "quantidade_solicitada": row.quantidade_solicitada,
                "pode_pagar": checkout_valido,
                "checkout_url": row.stripe_checkout_url if checkout_valido else None,
            }
        )
    return out


def url_pagamento_contratante(
    *,
    user: User,
    excepcional_id: int,
) -> str:
    from app.services.conta_multiuser_aumento_service import exigir_contratante_ativo_gestao

    vinculo = exigir_contratante_ativo_gestao(user)
    row = db.session.get(ContaMultiuserAumentoExcepcional, int(excepcional_id))
    if row is None or int(row.conta_id) != int(vinculo.conta_id):
        raise GestaoMultiuserNaoAutorizadaError(
            "Solicitação não encontrada para esta Conta."
        )
    _marcar_expirada_se_cabivel(row)
    if row.estado != ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO:
        raise AumentoExcepcionalInvalidoError(
            "Esta solicitação não está aguardando pagamento."
        )
    if row.expires_at is not None and utcnow_naive() > row.expires_at:
        raise AumentoExcepcionalInvalidoError("O link de pagamento expirou.")
    if not row.stripe_checkout_url:
        raise AumentoExcepcionalInvalidoError("Link de pagamento indisponível.")
    return row.stripe_checkout_url


def site_origin_padrao() -> str:
    return (
        (os.getenv("SITE_ORIGIN") or os.getenv("PREFERRED_URL") or "").strip()
        or "http://localhost"
    )


def _csrf_admin_serializer() -> URLSafeTimedSerializer:
    from flask import current_app

    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_CSRF_ADMIN_SALT)


def gerar_csrf_token_admin_multiuser(user_id: int) -> str:
    return _csrf_admin_serializer().dumps(str(int(user_id)))


def validar_csrf_token_admin_multiuser(token: str | None, user_id: int) -> bool:
    if not token:
        return False
    try:
        valor = _csrf_admin_serializer().loads(token, max_age=_CSRF_ADMIN_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False
    return str(valor) == str(int(user_id))


def comunicar_estado_excepcional(
    row: ContaMultiuserAumentoExcepcional,
    evento: str,
    *,
    enviar_email: bool = True,
) -> None:
    """
    Fato já persistido. Notificação/e-mail não desfazem decisão financeira.
    """
    from app.services.conta_multiuser_notificacao_service import (
        criar_notificacao,
        tentar_enviar_email_notificacao,
    )

    mapa = {
        "enviado_analise": (
            "excepcional_enviado_analise",
            "Sua solicitação de aumento de assentos foi enviada para análise comercial.",
            "Solicitação de assentos em análise",
            "Sua solicitação ultrapassa o limite de ampliações automáticas deste ciclo e será analisada pela equipe comercial do Agente Frete. Você receberá o retorno por e-mail e poderá acompanhar o andamento pela plataforma.",
            "multiuser_painel.gestao_multiuser",
            None,
        ),
        "aprovado_gratuito": (
            "excepcional_aprovado_gratuito",
            "Sua solicitação de aumento de assentos foi aprovada sem cobrança adicional.",
            "Solicitação de assentos aprovada",
            "Sua solicitação de aumento de assentos foi aprovada sem cobrança adicional. Os acessos já estão disponíveis.",
            "multiuser_painel.gestao_multiuser",
            None,
        ),
        "aguardando_pagamento": (
            "excepcional_aguardando_pagamento",
            "Sua solicitação foi aprovada com cobrança proporcional. Finalize o pagamento.",
            "Solicitação de assentos aprovada — pagamento pendente",
            None,
            "multiuser_painel.gestao_multiuser",
            row.stripe_checkout_url,
        ),
        "rejeitado": (
            "excepcional_rejeitado",
            "Sua solicitação de aumento de assentos foi recusada.",
            "Solicitação de assentos recusada",
            "Sua solicitação de aumento de assentos foi recusada. Acompanhe o painel da equipe.",
            "multiuser_painel.gestao_multiuser",
            None,
        ),
        "pagamento_confirmado": (
            "excepcional_pagamento_confirmado",
            "O pagamento da solicitação excepcional foi confirmado.",
            "Pagamento da solicitação excepcional confirmado",
            "O pagamento da sua solicitação de assentos foi confirmado. Os acessos serão liberados em seguida.",
            "multiuser_painel.gestao_multiuser",
            None,
        ),
        "liberado": (
            "excepcional_liberado",
            "Os assentos adicionais da solicitação excepcional foram liberados.",
            "Assentos adicionais liberados",
            "Os assentos adicionais da sua solicitação excepcional já estão disponíveis.",
            "multiuser_painel.gestao_multiuser",
            None,
        ),
        "expirado": (
            "excepcional_expirado",
            "O prazo de pagamento da solicitação excepcional expirou.",
            "Prazo de pagamento da solicitação excepcional expirado",
            "O prazo para pagar a solicitação excepcional expirou. Nenhuma capacidade adicional foi liberada.",
            "multiuser_painel.gestao_multiuser",
            None,
        ),
        "reconciliacao_necessaria": (
            "excepcional_reconciliacao_necessaria",
            "Há uma divergência operacional na solicitação excepcional. A equipe já foi acionada.",
            "Solicitação excepcional em reconciliação",
            "Identificamos uma divergência operacional na sua solicitação excepcional. A equipe comercial já foi acionada e você será informado quando houver regularização. Nenhuma cobrança extra é gerada por esta mensagem.",
            "multiuser_painel.gestao_multiuser",
            None,
        ),
    }
    spec = mapa.get(evento)
    if spec is None:
        return
    tipo, msg_interna, assunto, texto_email, cta, checkout_url = spec
    dedup = f"{tipo}:{row.correlation_id}"
    existente = (
        NotificacaoInterna.query.filter_by(
            user_id=int(row.solicitante_id),
            dedup_key=dedup,
        )
        .order_by(NotificacaoInterna.id.asc())
        .first()
    )
    nova = existente is None
    try:
        criar_notificacao(
            user_id=int(row.solicitante_id),
            conta_id=int(row.conta_id),
            tipo=tipo,
            mensagem=msg_interna,
            dedup_key=dedup,
            cta_interno=cta,
            referencia_dominio=f"excepcional:{row.id}",
            commit=False,
        )
    except Exception:
        logger.exception(
            "evento=notificacao_excepcional_falhou excepcional_id=%s evento=%s (decisao preservada)",
            row.id,
            evento,
        )
        nova = False
    if not enviar_email or not nova:
        return
    if evento == "aguardando_pagamento":
        link = (checkout_url or "").strip()
        if link:
            texto_email = (
                "Sua solicitação de assentos adicionais foi aprovada com cobrança proporcional. "
                f"Finalize o pagamento neste link: {link}"
            )
        else:
            texto_email = (
                "Sua solicitação foi aprovada com cobrança proporcional. "
                "Acesse o painel da equipe para finalizar o pagamento."
            )
    user = db.session.get(User, int(row.solicitante_id))
    tentar_enviar_email_notificacao(user, assunto, texto_email or "")
