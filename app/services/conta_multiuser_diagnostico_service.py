"""
Diagnóstico/reconciliação Multiuser (Fase 8).

Detecta e classifica divergências. Não muta quantity, ciclo, consumo,
plano, price nem Stripe. Não reimplementa a máquina F6.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.extensions import db
from app.models import (
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserAumentoExcepcional,
    ContaMultiuserAumentoOperacao,
    ContaMultiuserConvite,
    ContaMultiuserReducaoQuantity,
    ContaVinculoOrganizacional,
    utcnow_naive,
)
from app.services.conta_multiuser_capacidade_service import (
    contar_reservas_pendentes_validas,
    ids_franquias_reservadas_validas,
    listar_divergencias_conta,
    observar_quantity_externa_persistida,
)
from app.services.conta_organizacional_rules import (
    CODIGO_DIAG_CICLO,
    CODIGO_DIAG_CONTRATANTES,
    CODIGO_DIAG_CONVITES,
    CODIGO_DIAG_F5_EM_PROCESSAMENTO,
    CODIGO_DIAG_F6_PARCIAL,
    CODIGO_DIAG_MEMBERSHIP,
    CODIGO_DIAG_OCUPACAO,
    CODIGO_DIAG_QUANTITY_LOCAL_STRIPE,
    CODIGO_DIAG_REDUCAO_PENDENTE,
    CODIGO_DIAG_STRIPE_INDISPONIVEL,
    CODIGO_DIV_ATIVOS_MAIOR_QUE_QUANTITY,
    CODIGO_DIV_CICLO_DIVERGENTE,
    CODIGO_DIV_USER_CONTA_FRANQUIA_INCOERENTE,
    ESTADO_ATIVO,
    PAPEL_CONTRATANTE,
    SLUG_CONTA_SISTEMA,
    STATUS_DIAG_DIVERGENTE,
    STATUS_DIAG_INCONCLUSIVO,
    STATUS_DIAG_OK,
    STATUS_DIAG_PENDENTE_ESPERADO,
    STATUS_DIAG_RECONCILIACAO_NECESSARIA,
    STRIPE_CONSULTA_INDISPONIVEL,
    STRIPE_CONSULTA_NAO_APLICAVEL,
    STRIPE_CONSULTA_NAO_CONSULTADO,
    STRIPE_CONSULTA_OK,
)

logger = logging.getLogger(__name__)

_PRIORIDADE_STATUS = {
    STATUS_DIAG_OK: 0,
    STATUS_DIAG_PENDENTE_ESPERADO: 1,
    STATUS_DIAG_DIVERGENTE: 2,
    STATUS_DIAG_RECONCILIACAO_NECESSARIA: 3,
    STATUS_DIAG_INCONCLUSIVO: 4,
}


@dataclass(frozen=True)
class AchadoDiagnostico:
    codigo: str
    classificacao: str
    detalhe: str
    franquia_id: int | None = None
    user_id: int | None = None
    auto_reparo: bool = False
    acao: str = "observar"


@dataclass
class DiagnosticoContaMultiuser:
    conta_id: int
    status: str
    stripe_consulta: str
    quantity_local: int | None
    quantity_stripe: int | None
    memberships_ativos: int
    reservas_validas: int
    quantity_futura: int | None
    customer_id: str | None
    subscription_id: str | None
    subscription_item_id: str | None
    correlation_id: str | None
    detectado_em: str
    achados: list[AchadoDiagnostico] = field(default_factory=list)
    mutou_estado_comercial: bool = False

    def para_dict(self) -> dict[str, Any]:
        return {
            "conta_id": self.conta_id,
            "status": self.status,
            "stripe_consulta": self.stripe_consulta,
            "quantity_local": self.quantity_local,
            "quantity_stripe": self.quantity_stripe,
            "memberships_ativos": self.memberships_ativos,
            "reservas_validas": self.reservas_validas,
            "quantity_futura": self.quantity_futura,
            "customer_id": self.customer_id,
            "subscription_id": self.subscription_id,
            "subscription_item_id": self.subscription_item_id,
            "correlation_id": self.correlation_id,
            "detectado_em": self.detectado_em,
            "mutou_estado_comercial": self.mutou_estado_comercial,
            "achados": [
                {
                    "codigo": a.codigo,
                    "classificacao": a.classificacao,
                    "detalhe": a.detalhe,
                    "franquia_id": a.franquia_id,
                    "user_id": a.user_id,
                    "auto_reparo": a.auto_reparo,
                    "acao": a.acao,
                }
                for a in self.achados
            ],
        }


def reparos_seguros_conhecidos() -> tuple[dict[str, str], ...]:
    """Inventário de reparos F1–F7. F8 não os dispara."""
    return (
        {
            "codigo": "alinhar_franquias_sem_ciclo_ao_canonico",
            "fase": "F2",
            "quando": "somente ciclo nulo com período canônico determinável",
        },
        {
            "codigo": "executar_reconciliacao_multiuser_antes_enforcement",
            "fase": "F2",
            "quando": "backfill legado reexecutável; não sobrescreve quantity",
        },
    )


def _pior_status(atual: str, candidato: str) -> str:
    if _PRIORIDADE_STATUS.get(candidato, 0) > _PRIORIDADE_STATUS.get(atual, 0):
        return candidato
    return atual


def _vinculo_monetario(conta_id: int) -> ContaMonetizacaoVinculo | None:
    return (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), ativo=True)
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .first()
    )


def _contar_ativos(conta_id: int) -> int:
    return ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta_id),
        estado=ESTADO_ATIVO,
    ).count()


def _contar_contratantes(conta_id: int) -> int:
    return ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta_id),
        estado=ESTADO_ATIVO,
        papel=PAPEL_CONTRATANTE,
    ).count()


def _reducao_pendente(conta_id: int) -> ContaMultiuserReducaoQuantity | None:
    return (
        ContaMultiuserReducaoQuantity.query.filter_by(
            conta_id=int(conta_id),
            estado=ContaMultiuserReducaoQuantity.ESTADO_PENDENTE,
        )
        .order_by(ContaMultiuserReducaoQuantity.id.asc())
        .first()
    )


def _aumento_f5_em_processamento(conta_id: int) -> ContaMultiuserAumentoOperacao | None:
    return (
        ContaMultiuserAumentoOperacao.query.filter(
            ContaMultiuserAumentoOperacao.conta_id == int(conta_id),
            ContaMultiuserAumentoOperacao.estado.in_(
                (
                    ContaMultiuserAumentoOperacao.ESTADO_INICIADO,
                    ContaMultiuserAumentoOperacao.ESTADO_STRIPE_ENVIADO,
                )
            ),
        )
        .order_by(ContaMultiuserAumentoOperacao.id.desc())
        .first()
    )


def _excepcional_relevante(conta_id: int) -> ContaMultiuserAumentoExcepcional | None:
    return (
        ContaMultiuserAumentoExcepcional.query.filter(
            ContaMultiuserAumentoExcepcional.conta_id == int(conta_id),
            ContaMultiuserAumentoExcepcional.estado.in_(
                (
                    ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO,
                    ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO,
                    ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA,
                )
            ),
        )
        .order_by(ContaMultiuserAumentoExcepcional.id.desc())
        .first()
    )


def _consultar_quantity_stripe(subscription_id: str) -> tuple[int | None, str | None, bool]:
    """Retorna (quantity, item_id, ok). ok=False se a leitura falhou."""
    from app.services.conta_multiuser_aumento_service import (
        _carregar_assinatura_stripe,
        _ler_quantity_stripe_assinatura,
    )

    try:
        assinatura = _carregar_assinatura_stripe(subscription_id)
        qty, item_id = _ler_quantity_stripe_assinatura(assinatura)
        return qty, item_id, True
    except Exception:
        logger.info(
            "evento=multiuser_diagnostico motivo=stripe_indisponivel subscription_id=%s",
            subscription_id,
        )
        return None, None, False


def _local_diverge_de_stripe(local: int | None, stripe: int | None) -> bool:
    return local is not None and stripe is not None and int(local) != int(stripe)


def _quantity_alvo_f6(f6: ContaMultiuserAumentoExcepcional) -> int | None:
    delta = f6.quantidade_aprovada
    if delta is None:
        delta = f6.quantidade_solicitada
    try:
        return int(f6.quantity_atual) + int(delta)
    except (TypeError, ValueError):
        return None


def _f6_efeito_stripe_correlacionado(
    f6: ContaMultiuserAumentoExcepcional,
    *,
    local: int | None,
    stripe: int | None,
) -> bool:
    """
    Um estado F6 só explica mismatch quando o efeito Stripe persistido
    pertence à mesma correlation e a quantity viva coincide com esse efeito.
    Quantity coincidente de outra operação não é transitório legítimo.
    """
    if f6.estado == ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO:
        return False
    if f6.estado != ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO:
        return False
    if not _local_diverge_de_stripe(local, stripe):
        return False
    correlation_id = (f6.correlation_id or "").strip()
    if not correlation_id:
        return False
    from app.services.conta_multiuser_aumento_excepcional_service import (
        _stripe_quantity_desta_operacao,
    )

    efeito = _stripe_quantity_desta_operacao(correlation_id)
    if efeito is None:
        return False
    alvo = _quantity_alvo_f6(f6)
    if alvo is None or stripe is None:
        return False
    return int(efeito) == int(alvo) and int(stripe) == int(efeito)


def _classificar_quantity(
    *,
    local: int | None,
    stripe: int | None,
    stripe_consulta: str,
    f5: ContaMultiuserAumentoOperacao | None,
    f6: ContaMultiuserAumentoExcepcional | None,
    reducao: ContaMultiuserReducaoQuantity | None,
) -> list[AchadoDiagnostico]:
    """
    Precedência: Stripe indisponível → F6/F5 legítimos e causalmente
    correlacionados → local×Stripe atual → redução (só explica atual×futura
    ou Stripe já preparada na futura). Estado transitório não mascara
    divergência sem causalidade demonstrada.
    """
    if stripe_consulta == STRIPE_CONSULTA_INDISPONIVEL:
        return [
            AchadoDiagnostico(
                codigo=CODIGO_DIAG_STRIPE_INDISPONIVEL,
                classificacao=STATUS_DIAG_INCONCLUSIVO,
                detalhe="consulta_stripe_falhou_sem_mutacao",
                acao="observar",
            )
        ]
    if f6 is not None and f6.estado == ContaMultiuserAumentoExcepcional.ESTADO_RECONCILIACAO_NECESSARIA:
        return [
            AchadoDiagnostico(
                codigo=CODIGO_DIAG_F6_PARCIAL,
                classificacao=STATUS_DIAG_RECONCILIACAO_NECESSARIA,
                detalhe="f6_reconciliacao_necessaria_preservada",
                acao="observar",
            )
        ]
    if f6 is not None and f6.estado == ContaMultiuserAumentoExcepcional.ESTADO_PAGAMENTO_CONFIRMADO:
        if _f6_efeito_stripe_correlacionado(f6, local=local, stripe=stripe):
            return [
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_F6_PARCIAL,
                    classificacao=STATUS_DIAG_PENDENTE_ESPERADO,
                    detalhe="pagamento_confirmado_liberacao_incompleta",
                    acao="observar",
                )
            ]
        if not _local_diverge_de_stripe(local, stripe):
            return []
        # mismatch sem quantity alvo coerente desta operação: cai na divergência
    elif f6 is not None and f6.estado == ContaMultiuserAumentoExcepcional.ESTADO_AGUARDANDO_PAGAMENTO:
        if not _local_diverge_de_stripe(local, stripe):
            return []
        # sem efeito Stripe desta F6: mismatch permanece DIVERGENTE abaixo
    elif f5 is not None:
        if _local_diverge_de_stripe(local, stripe):
            return [
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_F5_EM_PROCESSAMENTO,
                    classificacao=STATUS_DIAG_PENDENTE_ESPERADO,
                    detalhe="aumento_f5_em_processamento",
                    acao="observar",
                )
            ]
        return []

    out: list[AchadoDiagnostico] = []
    if _local_diverge_de_stripe(local, stripe):
        stripe_preparada_reducao = (
            reducao is not None
            and stripe is not None
            and int(stripe) == int(reducao.quantity_futura)
            and local is not None
            and int(local) == int(reducao.quantity_atual_no_pedido)
        )
        if stripe_preparada_reducao:
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_REDUCAO_PENDENTE,
                    classificacao=STATUS_DIAG_PENDENTE_ESPERADO,
                    detalhe="stripe_preparada_na_quantity_futura_antes_da_cobranca",
                    acao="observar",
                )
            )
        else:
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_QUANTITY_LOCAL_STRIPE,
                    classificacao=STATUS_DIAG_DIVERGENTE,
                    detalhe="quantity_local_diverge_da_stripe_sem_operacao_legitima",
                    acao="observar",
                )
            )
    if reducao is not None and local is not None and int(local) != int(reducao.quantity_futura):
        if not any(a.codigo == CODIGO_DIAG_REDUCAO_PENDENTE for a in out):
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_REDUCAO_PENDENTE,
                    classificacao=STATUS_DIAG_PENDENTE_ESPERADO,
                    detalhe="quantity_atual_diferente_da_futura_nao_e_divergencia",
                    acao="observar",
                )
            )
    return out


def _achados_ciclo_membership_convites(
    conta_id: int,
    *,
    quantity_local: int | None,
    ativos: int,
    reservas: int,
    reducao: ContaMultiuserReducaoQuantity | None,
) -> list[AchadoDiagnostico]:
    out: list[AchadoDiagnostico] = []
    contratantes = _contar_contratantes(conta_id)
    if contratantes != 1:
        out.append(
            AchadoDiagnostico(
                codigo=CODIGO_DIAG_CONTRATANTES,
                classificacao=STATUS_DIAG_DIVERGENTE,
                detalhe="contratantes_ativos_diferente_de_um",
                acao="observar",
            )
        )

    for div in listar_divergencias_conta(conta_id):
        if div.codigo == CODIGO_DIV_CICLO_DIVERGENTE:
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_CICLO,
                    classificacao=STATUS_DIAG_DIVERGENTE,
                    detalhe=div.detalhe,
                    franquia_id=div.franquia_id,
                    acao="observar",
                )
            )
        elif div.codigo == CODIGO_DIV_USER_CONTA_FRANQUIA_INCOERENTE:
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_MEMBERSHIP,
                    classificacao=STATUS_DIAG_DIVERGENTE,
                    detalhe=div.detalhe,
                    franquia_id=div.franquia_id,
                    user_id=div.user_id,
                    acao="observar",
                )
            )
        elif div.codigo == CODIGO_DIV_ATIVOS_MAIOR_QUE_QUANTITY:
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_OCUPACAO,
                    classificacao=STATUS_DIAG_DIVERGENTE,
                    detalhe=div.detalhe,
                    acao="observar",
                )
            )

    if quantity_local is not None and (ativos + reservas) > int(quantity_local):
        if not any(a.codigo == CODIGO_DIAG_OCUPACAO for a in out):
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_CONVITES,
                    classificacao=STATUS_DIAG_DIVERGENTE,
                    detalhe="ativos_mais_reservas_acima_da_capacity",
                    acao="observar",
                )
            )

    if (
        reducao is not None
        and (ativos + reservas) > int(reducao.quantity_futura)
    ):
        out.append(
            AchadoDiagnostico(
                codigo=CODIGO_DIAG_CONVITES,
                classificacao=STATUS_DIAG_PENDENTE_ESPERADO,
                detalhe="reserva_ou_ativo_incompativel_com_quantity_futura",
                acao="observar",
            )
        )

    reservas_ids = ids_franquias_reservadas_validas(conta_id)
    if reservas_ids:
        ocupadas = {
            int(v.franquia_id)
            for v in ContaVinculoOrganizacional.query.filter_by(
                conta_id=int(conta_id),
                estado=ESTADO_ATIVO,
            )
        }
        if reservas_ids & ocupadas:
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_CONVITES,
                    classificacao=STATUS_DIAG_DIVERGENTE,
                    detalhe="convite_pendente_em_franquia_ja_ocupada",
                    acao="observar",
                )
            )

        aceitos_ainda_pendentes = ContaMultiuserConvite.query.filter(
            ContaMultiuserConvite.conta_id == int(conta_id),
            ContaMultiuserConvite.estado == ContaMultiuserConvite.ESTADO_PENDENTE,
            ContaMultiuserConvite.accepted_at.isnot(None),
        ).count()
        if aceitos_ainda_pendentes:
            out.append(
                AchadoDiagnostico(
                    codigo=CODIGO_DIAG_CONVITES,
                    classificacao=STATUS_DIAG_DIVERGENTE,
                    detalhe="convite_consumido_ainda_como_reserva",
                    acao="observar",
                )
            )

    return out


def diagnosticar_conta_multiuser(
    conta_id: int,
    *,
    consultar_stripe: bool = True,
) -> DiagnosticoContaMultiuser:
    """
    Diagnóstico reutilizável, sem Flask/request/template.
    Não repara. Não chama billing novo. Não reseta ciclo/consumo.
    Projeção derivada: não persiste snapshot (F7 permanece head da chain).
    """
    conta = db.session.get(Conta, int(conta_id))
    if conta is None:
        raise ValueError("Conta não encontrada.")
    cid = int(conta.id)
    agora = utcnow_naive()
    achados: list[AchadoDiagnostico] = []
    status = STATUS_DIAG_OK

    if (conta.slug or "") == SLUG_CONTA_SISTEMA or not bool(conta.multiuser_ativa):
        return DiagnosticoContaMultiuser(
            conta_id=cid,
            status=STATUS_DIAG_OK,
            stripe_consulta=STRIPE_CONSULTA_NAO_APLICAVEL,
            quantity_local=int(conta.quantidade_assentos_contratados)
            if conta.quantidade_assentos_contratados is not None
            else None,
            quantity_stripe=None,
            memberships_ativos=_contar_ativos(cid) if bool(conta.multiuser_ativa) else 0,
            reservas_validas=0,
            quantity_futura=None,
            customer_id=None,
            subscription_id=None,
            subscription_item_id=None,
            correlation_id=None,
            detectado_em=agora.isoformat(),
        )

    quantity_local = conta.quantidade_assentos_contratados
    ativos = _contar_ativos(cid)
    reservas = contar_reservas_pendentes_validas(cid)
    reducao = _reducao_pendente(cid)
    f5 = _aumento_f5_em_processamento(cid)
    f6 = _excepcional_relevante(cid)
    vinculo = _vinculo_monetario(cid)
    customer_id = (vinculo.customer_id or "").strip() or None if vinculo else None
    subscription_id = (vinculo.subscription_id or "").strip() or None if vinculo else None
    item_id = None
    quantity_stripe = None
    stripe_consulta = STRIPE_CONSULTA_NAO_APLICAVEL
    correlation_id = None
    if f6 is not None:
        correlation_id = (f6.correlation_id or "").strip() or None
    elif f5 is not None:
        correlation_id = (f5.correlation_id or "").strip() or None
    elif reducao is not None:
        correlation_id = (reducao.correlation_id or "").strip() or None

    if subscription_id and consultar_stripe:
        qty, item_id, ok = _consultar_quantity_stripe(subscription_id)
        if ok:
            stripe_consulta = STRIPE_CONSULTA_OK
            quantity_stripe = qty
        else:
            stripe_consulta = STRIPE_CONSULTA_INDISPONIVEL
            quantity_stripe = None
    elif subscription_id:
        stripe_consulta = STRIPE_CONSULTA_NAO_CONSULTADO
        quantity_stripe = observar_quantity_externa_persistida(cid)
    elif consultar_stripe:
        stripe_consulta = STRIPE_CONSULTA_NAO_APLICAVEL

    for achado_q in _classificar_quantity(
        local=quantity_local,
        stripe=quantity_stripe,
        stripe_consulta=stripe_consulta,
        f5=f5,
        f6=f6,
        reducao=reducao,
    ):
        achados.append(achado_q)
        status = _pior_status(status, achado_q.classificacao)

    for achado in _achados_ciclo_membership_convites(
        cid,
        quantity_local=quantity_local,
        ativos=ativos,
        reservas=reservas,
        reducao=reducao,
    ):
        achados.append(achado)
        status = _pior_status(status, achado.classificacao)

    if stripe_consulta == STRIPE_CONSULTA_INDISPONIVEL:
        status = STATUS_DIAG_INCONCLUSIVO

    unique: list[AchadoDiagnostico] = []
    seen: set[tuple] = set()
    for a in achados:
        key = (a.codigo, a.detalhe, a.franquia_id, a.user_id)
        if key in seen:
            continue
        seen.add(key)
        unique.append(a)

    diag = DiagnosticoContaMultiuser(
        conta_id=cid,
        status=status,
        stripe_consulta=stripe_consulta,
        quantity_local=int(quantity_local) if quantity_local is not None else None,
        quantity_stripe=int(quantity_stripe) if quantity_stripe is not None else None,
        memberships_ativos=int(ativos),
        reservas_validas=int(reservas),
        quantity_futura=int(reducao.quantity_futura) if reducao is not None else None,
        customer_id=customer_id,
        subscription_id=subscription_id,
        subscription_item_id=item_id,
        correlation_id=correlation_id,
        detectado_em=agora.isoformat(),
        achados=unique,
        mutou_estado_comercial=False,
    )
    if status != STATUS_DIAG_OK:
        logger.info(
            "evento=multiuser_diagnostico conta_id=%s status=%s stripe=%s tipos=%s correlation_id=%s",
            cid,
            status,
            stripe_consulta,
            ",".join(sorted({a.codigo for a in unique})),
            correlation_id or "",
        )
    return diag
