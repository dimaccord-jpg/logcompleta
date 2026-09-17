"""Intenção durável e exclusiva de Checkout Multiuser."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import ContaMonetizacaoCheckoutIntencao, ContaMonetizacaoVinculo, utcnow_naive
from app.services.conta_multiuser_capacidade_service import bloquear_conta_para_capacidade

logger = logging.getLogger(__name__)

CODIGO_MULTIUSER = "multiuser"

MSG_INTENCAO_INCONCLUSIVA = (
    "Não foi possível confirmar o estado da contratação Multiusuário pendente. "
    "Aguarde e tente novamente em instantes."
)
MSG_INTENCAO_EM_ANDAMENTO = (
    "Existe uma contratação Multiusuário em andamento. "
    "Aguarde a confirmação do pagamento."
)
MSG_INTENCAO_INCOMPATIVEL = (
    "Existe uma contratação Multiusuário pendente com parâmetros diferentes. "
    "Conclua ou aguarde o encerramento da tentativa atual."
)


class IntencaoCheckoutMultiuserInvalidaError(ValueError):
    """Intenção ausente, de outra Conta, consumida ou incompatível."""


class IntencaoCheckoutMultiuserInconclusivaError(ValueError):
    """Intenção pendente com estado Stripe inconclusivo. Não é expiração."""


class IntencaoCheckoutMultiuserIncompativelError(ValueError):
    """Intenção pendente incompatível com a requisição atual. Não mutar nem expirar."""


@dataclass
class ResultadoIntencaoCheckout:
    intencao: ContaMonetizacaoCheckoutIntencao
    reutilizou: bool
    nova_session_necessaria: bool
    session: dict[str, Any] | None = None


@dataclass(frozen=True)
class CargaSessionStripe:
    recuperada: bool
    session: dict[str, Any] | None
    motivo_falha: str | None = None


class InvestigacaoSubscriptionDerivada:
    """Tri-state da investigação de contrato derivado. Não usar bool."""

    EXISTE = "existe"
    NAO_EXISTE_CONCLUSIVAMENTE = "nao_existe_conclusivamente"
    INCONCLUSIVO = "inconclusivo"


def chave_idempotencia_checkout_multiuser(correlation_id: str) -> str:
    return f"stripe_multiuser_checkout:{correlation_id}"[:190]


def _to_datetime_utc_naive(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _norm_id(value: Any) -> str | None:
    txt = str(value).strip() if value is not None else ""
    return txt or None


def _mesmo_id_opcional(esquerda: Any, direita: Any) -> bool:
    a = _norm_id(esquerda)
    b = _norm_id(direita)
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return int(a) == int(b)
    except (TypeError, ValueError):
        return a == b


def intencao_pendente_conta(conta_id: int) -> ContaMonetizacaoCheckoutIntencao | None:
    return (
        ContaMonetizacaoCheckoutIntencao.query.filter_by(
            conta_id=int(conta_id),
            plano_interno=CODIGO_MULTIUSER,
            estado=ContaMonetizacaoCheckoutIntencao.ESTADO_PENDENTE,
        )
        .order_by(ContaMonetizacaoCheckoutIntencao.id.asc())
        .first()
    )


def buscar_intencao_por_correlation(
    correlation_id: str | None,
) -> ContaMonetizacaoCheckoutIntencao | None:
    cid = (correlation_id or "").strip()
    if not cid:
        return None
    return ContaMonetizacaoCheckoutIntencao.query.filter_by(correlation_id=cid).first()


def _classificar_falha_get_stripe(exc: Exception) -> str:
    nome = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "timeout" in nome or "timeout" in msg:
        return "timeout"
    if "connection" in nome or "dns" in msg:
        return "network"
    if "http 404" in msg:
        return "http_404"
    if "http 5" in msg:
        return "http_5xx"
    return "inconclusivo"


def _carregar_session_stripe(session_id: str | None) -> CargaSessionStripe:
    sid = (session_id or "").strip()
    if not sid:
        return CargaSessionStripe(recuperada=False, session=None, motivo_falha="sem_session_id")
    from app.services.cleiton_monetizacao_service import _stripe_get

    try:
        body = _stripe_get(
            f"/checkout/sessions/{sid}",
            params=[("expand[]", "line_items.data.price")],
        )
    except Exception as exc:
        motivo = _classificar_falha_get_stripe(exc)
        logger.info(
            "Checkout Session Multiuser inconclusiva session_id=%s motivo=%s",
            sid,
            motivo,
        )
        return CargaSessionStripe(recuperada=False, session=None, motivo_falha=motivo)
    if not isinstance(body, dict) or not _norm_id(body.get("id")):
        return CargaSessionStripe(recuperada=False, session=None, motivo_falha="parse")
    return CargaSessionStripe(recuperada=True, session=body, motivo_falha=None)


def _status_session(session: dict[str, Any]) -> str:
    return (_norm_id(session.get("status")) or "").lower()


def _payment_status_session(session: dict[str, Any]) -> str:
    return (_norm_id(session.get("payment_status")) or "").lower()


def _customer_id_de_objeto(valor: Any) -> str | None:
    if isinstance(valor, dict):
        return _norm_id(valor.get("id"))
    return _norm_id(valor)


def _subscription_id_de_objeto(valor: Any) -> str | None:
    if isinstance(valor, dict):
        return _norm_id(valor.get("id"))
    return _norm_id(valor)


def _session_indica_contrato_financeiro(session: dict[str, Any]) -> bool:
    status = _status_session(session)
    payment = _payment_status_session(session)
    if status == "complete":
        return True
    if payment in {"paid", "no_payment_required"}:
        return True
    if _subscription_id_de_objeto(session.get("subscription")):
        return True
    return False


def _session_expirada_inequivoca(session: dict[str, Any]) -> bool:
    if _session_indica_contrato_financeiro(session):
        return False
    return _status_session(session) == "expired"


def _vinculo_ativo_conta(conta_id: int) -> ContaMonetizacaoVinculo | None:
    return (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), ativo=True)
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .first()
    )


def _evidencia_local_subscription_derivada(
    intencao: ContaMonetizacaoCheckoutIntencao,
) -> bool:
    if _norm_id(intencao.subscription_id):
        return True
    vinculo = _vinculo_ativo_conta(int(intencao.conta_id))
    return vinculo is not None and bool(_norm_id(vinculo.subscription_id))


def _customers_para_investigacao_subscription(
    intencao: ContaMonetizacaoCheckoutIntencao,
    session: dict[str, Any] | None,
) -> list[str]:
    encontrados: list[str] = []
    vinculo = _vinculo_ativo_conta(int(intencao.conta_id))
    candidatos = [
        intencao.customer_id,
        _customer_id_de_objeto(session.get("customer")) if session is not None else None,
        vinculo.customer_id if vinculo is not None else None,
    ]
    for raw in candidatos:
        cid = _norm_id(raw)
        if cid and cid not in encontrados:
            encontrados.append(cid)
    return encontrados


def _listar_subscriptions_customer_tristate(customer_id: str) -> str:
    from app.services.cleiton_monetizacao_service import _stripe_get

    try:
        body = _stripe_get(
            "/subscriptions",
            params=[("customer", customer_id), ("limit", "30")],
        )
    except Exception:
        logger.info(
            "Lista de Subscriptions inconclusiva customer_id=%s",
            customer_id,
        )
        return InvestigacaoSubscriptionDerivada.INCONCLUSIVO
    if not isinstance(body, dict):
        return InvestigacaoSubscriptionDerivada.INCONCLUSIVO
    data = body.get("data")
    if not isinstance(data, list):
        return InvestigacaoSubscriptionDerivada.INCONCLUSIVO
    for item in data:
        if not isinstance(item, dict):
            return InvestigacaoSubscriptionDerivada.INCONCLUSIVO
        sid = _subscription_id_de_objeto(item.get("id")) or _subscription_id_de_objeto(item)
        if sid:
            return InvestigacaoSubscriptionDerivada.EXISTE
    return InvestigacaoSubscriptionDerivada.NAO_EXISTE_CONCLUSIVAMENTE


def _investigar_subscription_derivada(
    intencao: ContaMonetizacaoCheckoutIntencao,
    session: dict[str, Any] | None = None,
) -> str:
    """
    EXISTE: evidência estrutural conclusiva de contrato derivado.
    NAO_EXISTE_CONCLUSIVAMENTE: fontes necessárias avaliadas com sucesso, sem contrato.
    INCONCLUSIVO: falha técnica, timeout, 404/5xx, parse ou resposta inesperada.
    """
    if _evidencia_local_subscription_derivada(intencao):
        return InvestigacaoSubscriptionDerivada.EXISTE
    if session is not None and _subscription_id_de_objeto(session.get("subscription")):
        return InvestigacaoSubscriptionDerivada.EXISTE
    from app.services.cleiton_monetizacao_service import _obter_assinatura_stripe_ativa

    try:
        assinatura = _obter_assinatura_stripe_ativa(int(intencao.conta_id))
    except Exception:
        logger.info(
            "Recuperação de Subscription inconclusiva conta_id=%s",
            intencao.conta_id,
        )
        return InvestigacaoSubscriptionDerivada.INCONCLUSIVO
    if isinstance(assinatura, dict) and _norm_id(assinatura.get("subscription_id")):
        return InvestigacaoSubscriptionDerivada.EXISTE
    if assinatura is not None:
        return InvestigacaoSubscriptionDerivada.INCONCLUSIVO

    customers = _customers_para_investigacao_subscription(intencao, session)
    if not customers:
        return InvestigacaoSubscriptionDerivada.NAO_EXISTE_CONCLUSIVAMENTE

    viu_ausencia = False
    for cid in customers:
        resultado = _listar_subscriptions_customer_tristate(cid)
        if resultado == InvestigacaoSubscriptionDerivada.EXISTE:
            return InvestigacaoSubscriptionDerivada.EXISTE
        if resultado == InvestigacaoSubscriptionDerivada.INCONCLUSIVO:
            return InvestigacaoSubscriptionDerivada.INCONCLUSIVO
        viu_ausencia = True
    if viu_ausencia:
        return InvestigacaoSubscriptionDerivada.NAO_EXISTE_CONCLUSIVAMENTE
    return InvestigacaoSubscriptionDerivada.INCONCLUSIVO


def _linhas_session(session: dict[str, Any]) -> list[dict[str, Any]]:
    linhas = session.get("line_items")
    if isinstance(linhas, dict):
        data = linhas.get("data")
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    if isinstance(linhas, list):
        return [x for x in linhas if isinstance(x, dict)]
    return []


def _price_id_linha(linha: dict[str, Any]) -> str | None:
    price = linha.get("price")
    if isinstance(price, dict):
        return _norm_id(price.get("id"))
    return _norm_id(price)


def _quantity_linha(linha: dict[str, Any]) -> int | None:
    try:
        if linha.get("quantity") is None:
            return None
        return int(linha.get("quantity"))
    except (TypeError, ValueError):
        return None


def validar_compatibilidade_intencao(
    intencao: ContaMonetizacaoCheckoutIntencao,
    *,
    conta_id: int,
    franquia_id: int | None,
    usuario_id: int | None,
    price_id: str,
    quantity_solicitada: int,
    customer_id: str | None,
    session: dict[str, Any] | None = None,
) -> None:
    """Compara a intenção persistida com os parâmetros correntes. Não muta a intenção."""
    if int(intencao.conta_id) != int(conta_id):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    if (intencao.plano_interno or "").strip().lower() != CODIGO_MULTIUSER:
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    if not _mesmo_id_opcional(intencao.franquia_id, franquia_id):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    if not _mesmo_id_opcional(intencao.usuario_id, usuario_id):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    if (_norm_id(intencao.price_id) or "") != (_norm_id(price_id) or ""):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    if int(intencao.quantity_solicitada) != int(quantity_solicitada):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)

    intent_cus = _norm_id(intencao.customer_id)
    request_cus = _norm_id(customer_id)
    session_presente = session is not None
    session_cus = _customer_id_de_objeto(session.get("customer")) if session_presente else None
    if not _customer_compativel(
        intent_cus,
        request_cus,
        session_cus,
        session_presente=session_presente,
    ):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)


def _customer_esperado(intent_cus: str | None, request_cus: str | None) -> str | None:
    """Request explícito prevalece; senão o Customer persistido na intenção. None não é wildcard."""
    return request_cus or intent_cus


def _customer_compativel(
    intent_cus: str | None,
    request_cus: str | None,
    session_cus: str | None,
    *,
    session_presente: bool,
) -> bool:
    if request_cus is not None and intent_cus is not None and request_cus != intent_cus:
        return False
    esperado = _customer_esperado(intent_cus, request_cus)
    if not session_presente:
        return True
    if esperado is not None:
        return session_cus is not None and session_cus == esperado
    return True


def _customer_vinculo_ativo_conflita(conta_id: int, customer_id: str) -> bool:
    vinculo = _vinculo_ativo_conta(int(conta_id))
    vinculo_cus = _norm_id(vinculo.customer_id) if vinculo is not None else None
    return vinculo_cus is not None and vinculo_cus != _norm_id(customer_id)


def _associar_customer_da_session_se_ausente(
    intencao: ContaMonetizacaoCheckoutIntencao,
    session: dict[str, Any],
) -> None:
    if _norm_id(intencao.customer_id):
        return
    session_cus = _customer_id_de_objeto(session.get("customer"))
    if not session_cus:
        return
    if _customer_vinculo_ativo_conflita(int(intencao.conta_id), session_cus):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    intencao.customer_id = session_cus


def _validar_session_reutilizavel(
    session: dict[str, Any],
    *,
    session_id_esperado: str,
    price_id: str,
    quantity_solicitada: int,
    customer_id: str | None,
    correlation_id: str | None,
    intent_customer_id: str | None = None,
) -> None:
    if _norm_id(session.get("id")) != _norm_id(session_id_esperado):
        raise IntencaoCheckoutMultiuserInconclusivaError(MSG_INTENCAO_INCONCLUSIVA)
    if _status_session(session) != "open":
        raise IntencaoCheckoutMultiuserInconclusivaError(MSG_INTENCAO_INCONCLUSIVA)
    linhas = _linhas_session(session)
    if not linhas:
        raise IntencaoCheckoutMultiuserInconclusivaError(MSG_INTENCAO_INCONCLUSIVA)
    price_esperado = _norm_id(price_id)
    candidatas = [ln for ln in linhas if _price_id_linha(ln) == price_esperado]
    if len(candidatas) != 1:
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    linha = candidatas[0]
    qty = _quantity_linha(linha)
    if qty != int(quantity_solicitada):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    session_cus = _customer_id_de_objeto(session.get("customer"))
    esperado_cus = _customer_esperado(_norm_id(intent_customer_id), _norm_id(customer_id))
    if esperado_cus is not None and session_cus != esperado_cus:
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)
    meta = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    corr_session = _norm_id(meta.get("correlation_id")) or _norm_id(meta.get("checkout_intent_id"))
    if correlation_id and corr_session and corr_session != _norm_id(correlation_id):
        raise IntencaoCheckoutMultiuserIncompativelError(MSG_INTENCAO_INCOMPATIVEL)


def _expirar_intencao(intencao: ContaMonetizacaoCheckoutIntencao) -> None:
    intencao.estado = ContaMonetizacaoCheckoutIntencao.ESTADO_EXPIRADA
    intencao.expirada_em = utcnow_naive()
    intencao.updated_at = utcnow_naive()
    db.session.add(intencao)
    db.session.flush()


def _criar_intencao(
    *,
    conta_id: int,
    franquia_id: int | None,
    usuario_id: int | None,
    price_id: str,
    quantity_solicitada: int,
    customer_id: str | None,
) -> ContaMonetizacaoCheckoutIntencao:
    correlation_id = uuid.uuid4().hex
    row = ContaMonetizacaoCheckoutIntencao(
        conta_id=int(conta_id),
        franquia_id=int(franquia_id) if franquia_id is not None else None,
        usuario_id=int(usuario_id) if usuario_id is not None else None,
        plano_interno=CODIGO_MULTIUSER,
        estado=ContaMonetizacaoCheckoutIntencao.ESTADO_PENDENTE,
        correlation_id=correlation_id,
        stripe_idempotency_key=chave_idempotencia_checkout_multiuser(correlation_id),
        price_id=price_id,
        quantity_solicitada=int(quantity_solicitada),
        customer_id=customer_id,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _criar_intencao_exclusiva(
    *,
    conta_id: int,
    franquia_id: int | None,
    usuario_id: int | None,
    price_id: str,
    quantity_solicitada: int,
    customer_id: str | None,
) -> ResultadoIntencaoCheckout:
    try:
        with db.session.begin_nested():
            criada = _criar_intencao(
                conta_id=int(conta_id),
                franquia_id=franquia_id,
                usuario_id=usuario_id,
                price_id=price_id,
                quantity_solicitada=quantity_solicitada,
                customer_id=customer_id,
            )
        return ResultadoIntencaoCheckout(
            intencao=criada,
            reutilizou=False,
            nova_session_necessaria=True,
        )
    except IntegrityError:
        db.session.flush()
        vencedora = intencao_pendente_conta(int(conta_id))
        if vencedora is None:
            raise IntencaoCheckoutMultiuserInvalidaError(
                "Não foi possível adquirir intenção de Checkout Multiusuário exclusiva."
            )
        return _resolver_intencao_existente(
            vencedora,
            conta_id=conta_id,
            franquia_id=franquia_id,
            usuario_id=usuario_id,
            price_id=price_id,
            quantity_solicitada=quantity_solicitada,
            customer_id=customer_id,
        )


def _fail_closed_inconclusivo(intencao: ContaMonetizacaoCheckoutIntencao) -> None:
    logger.info(
        "Checkout Multiuser fail-closed intenção inconclusiva id=%s estado=%s",
        intencao.id,
        intencao.estado,
    )
    raise IntencaoCheckoutMultiuserInconclusivaError(MSG_INTENCAO_INCONCLUSIVA)


def _resolver_intencao_existente(
    existente: ContaMonetizacaoCheckoutIntencao,
    *,
    conta_id: int,
    franquia_id: int | None,
    usuario_id: int | None,
    price_id: str,
    quantity_solicitada: int,
    customer_id: str | None,
) -> ResultadoIntencaoCheckout:
    if _evidencia_local_subscription_derivada(existente):
        raise IntencaoCheckoutMultiuserInconclusivaError(MSG_INTENCAO_EM_ANDAMENTO)

    if not existente.checkout_session_id:
        validar_compatibilidade_intencao(
            existente,
            conta_id=conta_id,
            franquia_id=franquia_id,
            usuario_id=usuario_id,
            price_id=price_id,
            quantity_solicitada=quantity_solicitada,
            customer_id=customer_id,
            session=None,
        )
        return ResultadoIntencaoCheckout(
            intencao=existente,
            reutilizou=True,
            nova_session_necessaria=True,
        )

    carga = _carregar_session_stripe(existente.checkout_session_id)
    if not carga.recuperada or carga.session is None:
        _fail_closed_inconclusivo(existente)

    session = carga.session
    if _session_indica_contrato_financeiro(session):
        if _subscription_id_de_objeto(session.get("subscription")):
            existente.subscription_id = (
                _subscription_id_de_objeto(session.get("subscription")) or existente.subscription_id
            )
            db.session.add(existente)
            db.session.flush()
        raise IntencaoCheckoutMultiuserInconclusivaError(MSG_INTENCAO_EM_ANDAMENTO)

    if _session_expirada_inequivoca(session):
        investigacao = _investigar_subscription_derivada(existente, session)
        if investigacao == InvestigacaoSubscriptionDerivada.EXISTE:
            raise IntencaoCheckoutMultiuserInconclusivaError(MSG_INTENCAO_EM_ANDAMENTO)
        if investigacao != InvestigacaoSubscriptionDerivada.NAO_EXISTE_CONCLUSIVAMENTE:
            raise IntencaoCheckoutMultiuserInconclusivaError(MSG_INTENCAO_INCONCLUSIVA)
        _expirar_intencao(existente)
        return _criar_intencao_exclusiva(
            conta_id=conta_id,
            franquia_id=franquia_id,
            usuario_id=usuario_id,
            price_id=price_id,
            quantity_solicitada=quantity_solicitada,
            customer_id=customer_id,
        )

    validar_compatibilidade_intencao(
        existente,
        conta_id=conta_id,
        franquia_id=franquia_id,
        usuario_id=usuario_id,
        price_id=price_id,
        quantity_solicitada=quantity_solicitada,
        customer_id=customer_id,
        session=session,
    )
    _validar_session_reutilizavel(
        session,
        session_id_esperado=existente.checkout_session_id,
        price_id=price_id,
        quantity_solicitada=quantity_solicitada,
        customer_id=customer_id,
        correlation_id=existente.correlation_id,
        intent_customer_id=existente.customer_id,
    )
    _associar_customer_da_session_se_ausente(existente, session)
    existente.checkout_status = _status_session(session) or existente.checkout_status
    existente.checkout_expires_at = _to_datetime_utc_naive(session.get("expires_at")) or existente.checkout_expires_at
    db.session.add(existente)
    db.session.flush()
    return ResultadoIntencaoCheckout(
        intencao=existente,
        reutilizou=True,
        nova_session_necessaria=False,
        session=session,
    )


def adquirir_intencao_checkout_multiuser(
    *,
    conta_id: int,
    franquia_id: int | None,
    usuario_id: int | None,
    price_id: str,
    quantity_solicitada: int,
    customer_id: str | None = None,
) -> ResultadoIntencaoCheckout:
    """
    1 Conta → no máximo 1 intenção Multiuser pendente.
    Lock da Conta + unique parcial. Idempotency key estável.
    Expira somente com prova Stripe inequívoca.
    """
    bloquear_conta_para_capacidade(int(conta_id))
    existente = intencao_pendente_conta(int(conta_id))
    if existente is not None:
        return _resolver_intencao_existente(
            existente,
            conta_id=int(conta_id),
            franquia_id=franquia_id,
            usuario_id=usuario_id,
            price_id=price_id,
            quantity_solicitada=quantity_solicitada,
            customer_id=customer_id,
        )
    return _criar_intencao_exclusiva(
        conta_id=int(conta_id),
        franquia_id=franquia_id,
        usuario_id=usuario_id,
        price_id=price_id,
        quantity_solicitada=quantity_solicitada,
        customer_id=customer_id,
    )


def anexar_session_a_intencao(
    intencao: ContaMonetizacaoCheckoutIntencao,
    session: dict[str, Any],
) -> ContaMonetizacaoCheckoutIntencao:
    intencao.checkout_session_id = (session.get("id") or "").strip() or intencao.checkout_session_id
    intencao.checkout_status = (session.get("status") or "").strip() or intencao.checkout_status
    intencao.checkout_expires_at = _to_datetime_utc_naive(session.get("expires_at"))
    customer = _customer_id_de_objeto(session.get("customer"))
    if customer:
        intencao.customer_id = customer
    subscription = _subscription_id_de_objeto(session.get("subscription"))
    if subscription:
        intencao.subscription_id = subscription
    intencao.updated_at = utcnow_naive()
    db.session.add(intencao)
    db.session.flush()
    return intencao


def consumir_intencao_checkout(
    intencao: ContaMonetizacaoCheckoutIntencao,
    *,
    subscription_id: str | None = None,
    customer_id: str | None = None,
) -> ContaMonetizacaoCheckoutIntencao:
    if intencao.estado != ContaMonetizacaoCheckoutIntencao.ESTADO_PENDENTE:
        raise IntencaoCheckoutMultiuserInvalidaError(
            "Intenção de Checkout Multiusuário não está pendente."
        )
    intencao.estado = ContaMonetizacaoCheckoutIntencao.ESTADO_CONSUMIDA
    intencao.consumida_em = utcnow_naive()
    if subscription_id:
        intencao.subscription_id = subscription_id
    if customer_id:
        intencao.customer_id = customer_id
    intencao.updated_at = utcnow_naive()
    db.session.add(intencao)
    db.session.flush()
    return intencao


def exigir_intencao_pendente_correlacionada(
    *,
    conta_id: int,
    correlation_id: str | None,
    price_id: str | None,
) -> ContaMonetizacaoCheckoutIntencao:
    intencao = buscar_intencao_por_correlation(correlation_id)
    if intencao is None:
        raise IntencaoCheckoutMultiuserInvalidaError(
            "Ativação inicial Multiusuário exige intenção de Checkout pendente correlacionada."
        )
    if int(intencao.conta_id) != int(conta_id):
        raise IntencaoCheckoutMultiuserInvalidaError(
            "Intenção de Checkout não pertence à Conta do evento."
        )
    if intencao.plano_interno != CODIGO_MULTIUSER:
        raise IntencaoCheckoutMultiuserInvalidaError(
            "Intenção de Checkout não é Multiusuário."
        )
    if intencao.estado != ContaMonetizacaoCheckoutIntencao.ESTADO_PENDENTE:
        raise IntencaoCheckoutMultiuserInvalidaError(
            "Intenção de Checkout Multiusuário já foi consumida ou expirou."
        )
    if price_id and (intencao.price_id or "").strip() != (price_id or "").strip():
        raise IntencaoCheckoutMultiuserInvalidaError(
            "Price da invoice não corresponde à intenção de Checkout."
        )
    return intencao
