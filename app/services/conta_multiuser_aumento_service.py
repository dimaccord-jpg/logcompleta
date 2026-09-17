"""
Aumento automático de assentos Multiuser (Fase 5).

Preview e confirmação no backend. Limite cumulativo por ciclo via
plano_service.obter_limite_aumento_automatico_ciclo_multiuser().
Não cobra proporcionalmente. Não implementa F6/F7/F8.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserAumentoOperacao,
    ContaMultiuserCicloAumento,
    ContaMultiuserConvite,
    ContaVinculoOrganizacional,
    Franquia,
    User,
    utcnow_naive,
)
from app.services.conta_multiuser_autorizacao_service import (
    exigir_contratante_ativo as exigir_contratante_ativo_compartilhado,
    user_eh_contratante_ativo,
)
from app.services.conta_multiuser_capacidade_service import (
    bloquear_conta_para_capacidade,
    contar_reservas_pendentes_validas,
    liberar_reservas_expiradas_conta,
    listar_divergencias_conta,
    materializar_franquias_faltantes,
    snapshot_capacidade,
)
from app.services.conta_multiuser_ciclo_service import resolver_ciclo_canonico_conta
from app.services.conta_multiuser_errors import (
    AumentoMultiuserCicloIndeterminadoError,
    AumentoMultiuserDivergenteError,
    AumentoMultiuserInvalidoError,
    GestaoMultiuserNaoAutorizadaError,
)
from app.services.conta_organizacional_rules import (
    CODIGO_DIV_QUANTITY_EXTERNA_LOCAL,
    ESTADO_ATIVO,
    ESTADO_ENCERRADO,
    ESTADO_GERENCIAL_AGUARDANDO_VINCULO,
    ESTADO_GERENCIAL_ATIVO,
    ESTADO_GERENCIAL_PENDENTE_ATIVACAO,
    ESTADO_GERENCIAL_REVOGADO,
    PAPEL_CONTRATANTE,
)
from app.services.conta_organizacional_service import persistir_quantidade_assentos_contratados
from app.services.plano_service import (
    obter_limite_aumento_automatico_ciclo_multiuser,
    obter_limite_referencia_plano_admin,
    obter_valor_admin_plano,
)

logger = logging.getLogger(__name__)

CODIGO_MULTIUSER = "multiuser"
_CENTAVOS = Decimal("0.01")
_ZERO = Decimal("0.00")
_CSRF_SALT = "multiuser-aumento-csrf"
_CSRF_MAX_AGE = 3600
PRORATION_BEHAVIOR_NONE = "none"

ROTULO_ESTADO_GERENCIAL = {
    ESTADO_GERENCIAL_AGUARDANDO_VINCULO: "Aguardando vínculo",
    ESTADO_GERENCIAL_PENDENTE_ATIVACAO: "Pendente de ativação",
    ESTADO_GERENCIAL_ATIVO: "Ativo",
    ESTADO_GERENCIAL_REVOGADO: "Revogado",
}


@dataclass(frozen=True)
class PreviewAumentoAssentos:
    quantity_atual: int
    quantidade_solicitada: int
    nova_quantity: int
    valor_unitario: Decimal
    valor_mensal_atual: Decimal
    aumento_renovacao: Decimal
    valor_mensal_projetado: Decimal
    valor_hoje: Decimal
    proxima_cobranca: datetime
    automatico: bool
    acumulado_ciclo: int
    saldo_automatico: int
    limite_automatico_ciclo: int
    requer_analise: bool


@dataclass
class ResultadoAumentoAssentos:
    estado: str
    automatico: bool
    requer_analise: bool
    quantity_atual: int
    quantity_nova: int | None
    quantidade_solicitada: int
    valor_hoje: Decimal
    valor_mensal_atual: Decimal
    aumento_renovacao: Decimal
    valor_mensal_projetado: Decimal
    proxima_cobranca: datetime | None
    acumulado_ciclo: int
    saldo_automatico: int
    franquias_criadas: int = 0
    replay: bool = False
    correlation_id: str | None = None
    operacao_id: int | None = None
    customer_id: str | None = None
    subscription_id: str | None = None
    subscription_item_id: str | None = None
    proration_behavior: str | None = None
    mensagem: str = ""


@dataclass(frozen=True)
class MembroPainel:
    nome: str | None
    email: str | None
    papel: str | None
    estado_vinculo: str | None
    estado_convite: str | None
    estado_gerencial: str
    estado_gerencial_rotulo: str
    ocupa_assento: bool
    user_id: int | None = None
    vinculo_id: int | None = None


@dataclass
class PainelContratante:
    conta_id: int
    assentos_contratados: int
    usuarios_ativos: int
    convites_pendentes: int
    assentos_disponiveis: int
    aumento_automatico_utilizado: int
    aumento_automatico_restante: int
    limite_automatico_ciclo: int
    proxima_cobranca: datetime | None
    valor_mensal_atual: Decimal
    valor_unitario: Decimal
    membros: list[MembroPainel] = field(default_factory=list)
    solicitacao_analise_pendente: bool = False


def _csrf_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_CSRF_SALT)


def gerar_csrf_token_aumento(user_id: int) -> str:
    return _csrf_serializer().dumps(str(int(user_id)))


def validar_csrf_token_aumento(token: str | None, user_id: int) -> bool:
    if not token:
        return False
    try:
        valor = _csrf_serializer().loads(token, max_age=_CSRF_MAX_AGE)
    except (BadSignature, SignatureExpired, TypeError, ValueError):
        return False
    return valor == str(int(user_id))


def exigir_contratante_ativo_gestao(user: User) -> ContaVinculoOrganizacional:
    """Autoridade: vínculo organizacional ativo papel=contratante. Não usa categoria/is_admin."""
    return exigir_contratante_ativo_compartilhado(
        user,
        erro_cls=GestaoMultiuserNaoAutorizadaError,
        mensagem="Somente o Contratante ativo da Conta pode gerenciar assentos.",
        evento_log="gestao_multiuser_bloqueada",
    )


def montar_painel_contratante_por_ator_id(actor_id: int) -> "PainelContratante":
    ator = db.session.get(User, int(actor_id))
    if ator is None:
        raise GestaoMultiuserNaoAutorizadaError(
            "Somente o Contratante ativo da Conta pode gerenciar assentos."
        )
    return montar_painel_contratante(ator)


def _to_int_positivo(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            txt = str(value or "").strip()
            if not txt or txt.startswith("-"):
                raise AumentoMultiuserInvalidoError(
                    "Informe uma quantidade adicional inteira positiva."
                )
            value = int(txt)
        except (TypeError, ValueError) as exc:
            raise AumentoMultiuserInvalidoError(
                "Informe uma quantidade adicional inteira positiva."
            ) from exc
    if value < 1:
        raise AumentoMultiuserInvalidoError(
            "A quantidade adicional deve ser maior ou igual a 1."
        )
    return int(value)


def _money(valor: Decimal) -> Decimal:
    return valor.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)


def formatar_brl(valor: Decimal) -> str:
    q = _money(valor)
    sinal = "-" if q < 0 else ""
    abs_txt = f"{abs(q):.2f}"
    reais, centavos = abs_txt.split(".")
    grupos: list[str] = []
    while reais:
        grupos.append(reais[-3:])
        reais = reais[:-3]
    corpo = ".".join(reversed(grupos))
    return f"{sinal}R$ {corpo},{centavos}"


def formatar_data_br(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%d/%m/%Y")


def _vinculo_monetario_ativo(conta_id: int) -> ContaMonetizacaoVinculo | None:
    return (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), ativo=True)
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .first()
    )


def _registrar_fato(
    *,
    tipo_fato: str,
    status_tecnico: str,
    conta_id: int,
    usuario_id: int | None,
    correlation_id: str | None,
    idempotency_key: str | None = None,
    customer_id: str | None = None,
    subscription_id: str | None = None,
    snapshot: dict[str, Any] | None = None,
) -> None:
    from app.services.cleiton_monetizacao_service import registrar_fato_monetizacao

    registrar_fato_monetizacao(
        tipo_fato=tipo_fato,
        status_tecnico=status_tecnico,
        conta_id=int(conta_id),
        usuario_id=usuario_id,
        provider="stripe",
        correlation_key=correlation_id,
        idempotency_key=idempotency_key,
        customer_id=customer_id,
        subscription_id=subscription_id,
        snapshot_normalizado=snapshot or {},
    )


def _limite_automatico_ciclo() -> int:
    return int(obter_limite_aumento_automatico_ciclo_multiuser(exigir_configurado=False))


def decidir_aumento_automatico(
    *,
    acumulado_ciclo: int,
    quantidade_solicitada: int,
    limite_ciclo: int | None = None,
) -> bool:
    """
    Regra pura: o limite é cumulativo no ciclo, não por request.
    A solicitação inteira sai do automático se o novo acumulado exceder o limite.
    Sem aplicação parcial.
    """
    limite = int(limite_ciclo if limite_ciclo is not None else _limite_automatico_ciclo())
    return int(acumulado_ciclo) + int(quantidade_solicitada) <= limite


def _obter_ou_criar_contador_ciclo(
    conta_id: int,
    ciclo_inicio: datetime,
    ciclo_fim: datetime,
) -> ContaMultiuserCicloAumento:
    row = (
        ContaMultiuserCicloAumento.query.filter_by(
            conta_id=int(conta_id),
            ciclo_inicio=ciclo_inicio,
            ciclo_fim=ciclo_fim,
        )
        .with_for_update()
        .one_or_none()
    )
    if row is not None:
        return row
    row = ContaMultiuserCicloAumento(
        conta_id=int(conta_id),
        ciclo_inicio=ciclo_inicio,
        ciclo_fim=ciclo_fim,
        acumulado_automatico=0,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        row = (
            ContaMultiuserCicloAumento.query.filter_by(
                conta_id=int(conta_id),
                ciclo_inicio=ciclo_inicio,
                ciclo_fim=ciclo_fim,
            )
            .with_for_update()
            .one()
        )
    return row


def _resolver_ciclo_conta(conta_id: int) -> Any:
    ciclo = resolver_ciclo_canonico_conta(int(conta_id))
    if not ciclo.determinavel or ciclo.inicio is None or ciclo.fim is None:
        raise AumentoMultiuserCicloIndeterminadoError(
            "Não foi possível determinar o ciclo comercial da Conta."
        )
    if ciclo.divergente:
        raise AumentoMultiuserCicloIndeterminadoError(
            "O ciclo comercial da Conta está divergente; o aumento foi bloqueado."
        )
    return ciclo


def _contador_ciclo_atual(conta_id: int) -> tuple[ContaMultiuserCicloAumento, Any]:
    ciclo = _resolver_ciclo_conta(int(conta_id))
    row = _obter_ou_criar_contador_ciclo(int(conta_id), ciclo.inicio, ciclo.fim)
    return row, ciclo


def _ler_acumulado_ciclo(conta_id: int, ciclo: Any) -> int:
    """Leitura do acumulado do ciclo. Não cria contador."""
    row = (
        ContaMultiuserCicloAumento.query.filter_by(
            conta_id=int(conta_id),
            ciclo_inicio=ciclo.inicio,
            ciclo_fim=ciclo.fim,
        )
        .one_or_none()
    )
    if row is None:
        return 0
    return int(row.acumulado_automatico or 0)


def _mesmo_comando_aumento(
    operacao: ContaMultiuserAumentoOperacao,
    quantidade: int,
) -> bool:
    return int(operacao.quantidade_solicitada) == int(quantidade)


def _primeiro_item_stripe(subscription: dict[str, Any]) -> dict[str, Any] | None:
    items = subscription.get("items") if isinstance(subscription, dict) else None
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    return data[0]


def _preco_unitario_stripe_assinatura(subscription: dict[str, Any]) -> Decimal | None:
    item = _primeiro_item_stripe(subscription)
    if item is None:
        return None
    price = item.get("price")
    if not isinstance(price, dict):
        return None
    raw = price.get("unit_amount")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        cents = int(raw)
    except (TypeError, ValueError):
        return None
    if cents < 0:
        return None
    return _money(Decimal(cents) / Decimal("100"))


def _periodo_stripe_assinatura(
    subscription: dict[str, Any],
) -> tuple[datetime | None, datetime | None]:
    from app.services.cleiton_monetizacao_service import _to_datetime_utc_naive

    start = subscription.get("current_period_start") if isinstance(subscription, dict) else None
    end = subscription.get("current_period_end") if isinstance(subscription, dict) else None
    item = _primeiro_item_stripe(subscription)
    if item is not None:
        if start is None:
            start = item.get("current_period_start")
        if end is None:
            end = item.get("current_period_end")
    return _to_datetime_utc_naive(start), _to_datetime_utc_naive(end)


def _ler_quantity_stripe_assinatura(subscription: dict[str, Any]) -> tuple[int | None, str | None]:
    from app.services.cleiton_monetizacao_service import _primeiro_subscription_item_id

    items = subscription.get("items") if isinstance(subscription, dict) else None
    data = items.get("data") if isinstance(items, dict) else None
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None, _primeiro_subscription_item_id(subscription)
    qtd = data[0].get("quantity")
    item_id = data[0].get("id") or _primeiro_subscription_item_id(subscription)
    try:
        qtd_i = int(qtd) if qtd is not None and not isinstance(qtd, bool) else None
    except (TypeError, ValueError):
        qtd_i = None
    if qtd_i is not None and qtd_i < 1:
        qtd_i = None
    return qtd_i, (str(item_id) if item_id else None)


def _carregar_assinatura_stripe(subscription_id: str) -> dict[str, Any]:
    from app.services.cleiton_monetizacao_service import _stripe_get

    return _stripe_get(f"/subscriptions/{subscription_id}")


def _atualizar_quantity_item_stripe(
    *,
    item_id: str,
    nova_quantity: int,
    idempotency_key: str,
) -> dict[str, Any]:
    from app.services.cleiton_monetizacao_service import _stripe_post

    return _stripe_post(
        f"/subscription_items/{item_id}",
        {
            "quantity": str(int(nova_quantity)),
            "proration_behavior": PRORATION_BEHAVIOR_NONE,
        },
        idempotency_key=idempotency_key,
    )


def _extrair_ids_vinculo(vinculo: ContaMonetizacaoVinculo) -> tuple[str | None, str | None]:
    customer_id = (vinculo.customer_id or "").strip() or None
    subscription_id = (vinculo.subscription_id or "").strip() or None
    return customer_id, subscription_id


def _atualizar_snapshot_quantity_vinculo(
    vinculo: ContaMonetizacaoVinculo,
    nova_quantity: int,
) -> None:
    """Alinha a quantity persistida no vínculo após aumento F5, sem inventar IDs."""
    for attr in ("snapshot_normalizado_json", "payload_bruto_sanitizado_json"):
        raw = getattr(vinculo, attr, None)
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        parsed["quantity"] = int(nova_quantity)
        items = parsed.get("items")
        if isinstance(items, dict):
            arr = items.get("data")
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                arr[0]["quantity"] = int(nova_quantity)
        setattr(vinculo, attr, json.dumps(parsed, ensure_ascii=True, sort_keys=True))
    db.session.add(vinculo)


def _divergencia_quantity_relevante(
    conta: Conta,
    quantity_stripe: int | None,
) -> bool:
    local = conta.quantidade_assentos_contratados
    if local is None or quantity_stripe is None:
        return True
    if int(local) != int(quantity_stripe):
        return True
    for div in listar_divergencias_conta(int(conta.id)):
        if div.codigo == CODIGO_DIV_QUANTITY_EXTERNA_LOCAL:
            return True
    return False


def _calcular_preview_locked(
    conta: Conta,
    quantidade_solicitada: int,
    *,
    registrar_preview: bool = False,
    usuario_id: int | None = None,
) -> PreviewAumentoAssentos:
    qtd_atual = conta.quantidade_assentos_contratados
    if qtd_atual is None or int(qtd_atual) < 1:
        raise AumentoMultiuserInvalidoError(
            "A Conta não possui quantity contratada para aumento."
        )
    qtd_atual = int(qtd_atual)
    nova = qtd_atual + int(quantidade_solicitada)
    if nova <= qtd_atual:
        raise AumentoMultiuserInvalidoError("Este fluxo permite somente aumento de assentos.")

    valor_unitario = obter_valor_admin_plano(CODIGO_MULTIUSER, exigir_configurado=True)
    if valor_unitario is None:
        raise AumentoMultiuserInvalidoError(
            "Valor por assento do plano Multiusuário não está configurado."
        )
    valor_unitario = _money(valor_unitario)
    mensal_atual = _money(valor_unitario * Decimal(qtd_atual))
    mensal_novo = _money(valor_unitario * Decimal(nova))
    aumento = _money(mensal_novo - mensal_atual)

    ciclo = _resolver_ciclo_conta(int(conta.id))
    limite = _limite_automatico_ciclo()
    acumulado = _ler_acumulado_ciclo(int(conta.id), ciclo)
    saldo = max(0, limite - acumulado)
    automatico = decidir_aumento_automatico(
        acumulado_ciclo=acumulado,
        quantidade_solicitada=int(quantidade_solicitada),
        limite_ciclo=limite,
    )
    return PreviewAumentoAssentos(
        quantity_atual=qtd_atual,
        quantidade_solicitada=int(quantidade_solicitada),
        nova_quantity=nova,
        valor_unitario=valor_unitario,
        valor_mensal_atual=mensal_atual,
        aumento_renovacao=aumento,
        valor_mensal_projetado=mensal_novo,
        valor_hoje=_ZERO,
        proxima_cobranca=ciclo.fim,
        automatico=automatico,
        acumulado_ciclo=acumulado,
        saldo_automatico=saldo,
        limite_automatico_ciclo=limite,
        requer_analise=not automatico,
    )


def calcular_preview_aumento(
    ator: User,
    quantidade_adicional: Any,
    *,
    commit: bool = True,
) -> PreviewAumentoAssentos:
    """Preview é somente leitura/cálculo. Não persiste efeito comercial."""
    vinculo = exigir_contratante_ativo_gestao(ator)
    quantidade = _to_int_positivo(quantidade_adicional)
    bloquear_conta_para_capacidade(int(vinculo.conta_id))
    conta = db.session.get(Conta, int(vinculo.conta_id))
    if conta is None:
        raise AumentoMultiuserInvalidoError("Conta não encontrada.")
    preview = _calcular_preview_locked(
        conta,
        quantidade,
        registrar_preview=False,
        usuario_id=int(ator.id),
    )
    _ = commit
    return preview


def preview_para_api(preview: PreviewAumentoAssentos) -> dict[str, Any]:
    return {
        "quantity_atual": preview.quantity_atual,
        "quantidade_solicitada": preview.quantidade_solicitada,
        "nova_quantity": preview.nova_quantity,
        "valor_mensal_atual": str(preview.valor_mensal_atual),
        "aumento_renovacao": str(preview.aumento_renovacao),
        "valor_mensal_projetado": str(preview.valor_mensal_projetado),
        "valor_hoje": str(preview.valor_hoje),
        "valor_hoje_rotulo": f"Hoje: {formatar_brl(preview.valor_hoje)}",
        "aumento_renovacao_rotulo": f"Na próxima renovação: +{formatar_brl(preview.aumento_renovacao)}",
        "valor_mensal_atual_rotulo": f"Valor mensal atual: {formatar_brl(preview.valor_mensal_atual)}",
        "preco_unitario": str(preview.valor_unitario),
        "preco_unitario_rotulo": f"Preço unitário: {formatar_brl(preview.valor_unitario)}",
        "quantity_atual_rotulo": f"Assentos atuais: {preview.quantity_atual}",
        "quantidade_solicitada_rotulo": f"Adicionar: {preview.quantidade_solicitada}",
        "nova_quantity_rotulo": f"Nova quantidade: {preview.nova_quantity}",
        "impacto_imediato_rotulo": "Valor adicional hoje: R$ 0,00",
        "valor_mensal_projetado_rotulo": (
            f"Novo valor mensal: {formatar_brl(preview.valor_mensal_projetado)}"
        ),
        "proxima_cobranca": preview.proxima_cobranca.isoformat()
        if preview.proxima_cobranca
        else None,
        "proxima_cobranca_rotulo": formatar_data_br(preview.proxima_cobranca),
        "automatico": preview.automatico,
        "requer_analise": preview.requer_analise,
        "acumulado_ciclo": preview.acumulado_ciclo,
        "saldo_automatico": preview.saldo_automatico,
        "limite_automatico_ciclo": preview.limite_automatico_ciclo,
    }


def _resultado_de_operacao(
    operacao: ContaMultiuserAumentoOperacao,
    preview: PreviewAumentoAssentos,
    *,
    replay: bool,
    franquias_criadas: int = 0,
    mensagem: str = "",
) -> ResultadoAumentoAssentos:
    return ResultadoAumentoAssentos(
        estado=operacao.estado,
        automatico=operacao.estado
        == ContaMultiuserAumentoOperacao.ESTADO_APROVADO_AUTOMATICO,
        requer_analise=operacao.estado
        == ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE,
        quantity_atual=int(operacao.quantity_anterior),
        quantity_nova=operacao.quantity_nova,
        quantidade_solicitada=int(operacao.quantidade_solicitada),
        valor_hoje=preview.valor_hoje,
        valor_mensal_atual=preview.valor_mensal_atual,
        aumento_renovacao=preview.aumento_renovacao,
        valor_mensal_projetado=preview.valor_mensal_projetado,
        proxima_cobranca=preview.proxima_cobranca,
        acumulado_ciclo=preview.acumulado_ciclo,
        saldo_automatico=preview.saldo_automatico,
        franquias_criadas=franquias_criadas,
        replay=replay,
        correlation_id=operacao.correlation_id,
        operacao_id=operacao.id,
        customer_id=operacao.stripe_customer_id,
        subscription_id=operacao.stripe_subscription_id,
        subscription_item_id=operacao.stripe_subscription_item_id,
        proration_behavior=PRORATION_BEHAVIOR_NONE
        if operacao.estado == ContaMultiuserAumentoOperacao.ESTADO_APROVADO_AUTOMATICO
        else None,
        mensagem=mensagem,
    )


def _preview_financeiro_da_operacao(
    conta: Conta,
    operacao: ContaMultiuserAumentoOperacao,
) -> PreviewAumentoAssentos:
    """Valores do comando persistido. Não recalcula um aumento hipotético extra."""
    quantidade = int(operacao.quantidade_solicitada)
    if operacao.estado != ContaMultiuserAumentoOperacao.ESTADO_APROVADO_AUTOMATICO:
        return _calcular_preview_locked(conta, quantidade, registrar_preview=False)
    qtd_final = int(
        operacao.quantity_nova
        if operacao.quantity_nova is not None
        else int(operacao.quantity_anterior) + quantidade
    )
    valor_unitario = obter_valor_admin_plano(CODIGO_MULTIUSER, exigir_configurado=True)
    if valor_unitario is None:
        raise AumentoMultiuserInvalidoError(
            "Valor por assento do plano Multiusuário não está configurado."
        )
    valor_unitario = _money(valor_unitario)
    mensal = _money(valor_unitario * Decimal(qtd_final))
    aumento = _money(valor_unitario * Decimal(quantidade))
    ciclo = _resolver_ciclo_conta(int(conta.id))
    limite = _limite_automatico_ciclo()
    acumulado = _ler_acumulado_ciclo(int(conta.id), ciclo)
    return PreviewAumentoAssentos(
        quantity_atual=qtd_final,
        quantidade_solicitada=quantidade,
        nova_quantity=qtd_final,
        valor_unitario=valor_unitario,
        valor_mensal_atual=mensal,
        aumento_renovacao=aumento,
        valor_mensal_projetado=mensal,
        valor_hoje=_ZERO,
        proxima_cobranca=ciclo.fim,
        automatico=True,
        acumulado_ciclo=acumulado,
        saldo_automatico=max(0, limite - acumulado),
        limite_automatico_ciclo=limite,
        requer_analise=False,
    )


def _responder_operacao_existente(
    *,
    existente: ContaMultiuserAumentoOperacao,
    conta: Conta,
    quantidade: int,
    commit: bool,
) -> ResultadoAumentoAssentos | None:
    """
    None: operação ainda iniciada; o caller continua.
    Replay ou conflito: retorna ou levanta.
    """
    if existente.conta_id != int(conta.id):
        raise AumentoMultiuserInvalidoError(
            "Chave de idempotência não pertence a esta Conta."
        )
    if not _mesmo_comando_aumento(existente, quantidade):
        raise AumentoMultiuserInvalidoError(
            "Conflito de idempotência: a chave já identifica outra operação."
        )
    if existente.estado == ContaMultiuserAumentoOperacao.ESTADO_STRIPE_ENVIADO:
        if commit:
            db.session.commit()
        raise AumentoMultiuserDivergenteError(
            "A Stripe já confirmou este aumento; a conclusão local está pendente de reconciliação."
        )
    if existente.estado in (
        ContaMultiuserAumentoOperacao.ESTADO_APROVADO_AUTOMATICO,
        ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE,
        ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO,
    ):
        if commit:
            db.session.commit()
        return _resultado_de_operacao(
            existente,
            _preview_financeiro_da_operacao(conta, existente),
            replay=True,
            mensagem=(
                "Esta solicitação requer análise."
                if existente.estado
                == ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE
                else "Aumento já processado."
            ),
        )
    return None


def _marcar_falha_reconciliacao(
    *,
    operacao: ContaMultiuserAumentoOperacao,
    conta: Conta,
    ator: User,
    customer_id: str | None,
    subscription_id: str | None,
    status_tecnico: str,
    snapshot: dict[str, Any],
    commit: bool,
    mensagem: str,
) -> None:
    operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO
    db.session.add(operacao)
    _registrar_fato(
        tipo_fato="multiuser_aumento_falha_reconciliacao",
        status_tecnico=status_tecnico,
        conta_id=int(conta.id),
        usuario_id=int(ator.id),
        correlation_id=operacao.correlation_id,
        customer_id=customer_id,
        subscription_id=subscription_id,
        snapshot=snapshot,
    )
    if commit:
        db.session.commit()
    raise AumentoMultiuserDivergenteError(mensagem)


def _reservar_acumulado_automatico(
    contador: ContaMultiuserCicloAumento,
    quantidade: int,
    limite: int,
) -> bool:
    """
    Reserva atômica do saldo automático do ciclo.
    Impede +3/+3 simultâneo de ultrapassar o limite mesmo sem FOR UPDATE efetivo.
    """
    result = db.session.execute(
        update(ContaMultiuserCicloAumento)
        .where(ContaMultiuserCicloAumento.id == int(contador.id))
        .where(
            ContaMultiuserCicloAumento.acumulado_automatico + int(quantidade)
            <= int(limite)
        )
        .values(
            acumulado_automatico=ContaMultiuserCicloAumento.acumulado_automatico
            + int(quantidade),
            updated_at=utcnow_naive(),
        )
    )
    db.session.flush()
    db.session.refresh(contador)
    return int(result.rowcount or 0) == 1


def _desfazer_reserva_acumulado(
    contador: ContaMultiuserCicloAumento,
    quantidade: int,
) -> None:
    novo = max(0, int(contador.acumulado_automatico or 0) - int(quantidade))
    contador.acumulado_automatico = novo
    contador.updated_at = utcnow_naive()
    db.session.add(contador)
    db.session.flush()


def _aplicar_efeitos_locais_aumento(
    *,
    conta: Conta,
    nova_quantity: int,
    vinculo_monetario: ContaMonetizacaoVinculo,
) -> int:
    persistir_quantidade_assentos_contratados(
        int(conta.id),
        int(nova_quantity),
        commit=False,
    )
    db.session.refresh(conta)
    limite_ref = obter_limite_referencia_plano_admin(
        CODIGO_MULTIUSER, exigir_configurado=False
    )
    criadas = materializar_franquias_faltantes(
        conta,
        int(nova_quantity),
        limite_referencia=limite_ref,
        aplicar_limite_nas_existentes=False,
    )
    _atualizar_snapshot_quantity_vinculo(vinculo_monetario, int(nova_quantity))
    db.session.flush()
    return len(criadas)


def confirmar_aumento_assentos(
    ator: User,
    quantidade_adicional: Any,
    *,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> ResultadoAumentoAssentos:
    vinculo = exigir_contratante_ativo_gestao(ator)
    quantidade = _to_int_positivo(quantidade_adicional)
    chave = (idempotency_key or "").strip() or uuid.uuid4().hex
    if len(chave) > 200:
        chave = chave[:200]

    bloquear_conta_para_capacidade(int(vinculo.conta_id))
    conta = db.session.get(Conta, int(vinculo.conta_id))
    if conta is None:
        raise AumentoMultiuserInvalidoError("Conta não encontrada.")
    from app.services.conta_multiuser_aumento_excepcional_service import (
        exigir_sem_liberacao_paga_incompleta,
    )

    exigir_sem_liberacao_paga_incompleta(int(conta.id))
    liberar_reservas_expiradas_conta(conta.id)

    existente = ContaMultiuserAumentoOperacao.query.filter_by(
        idempotency_key=chave
    ).one_or_none()
    preview = _calcular_preview_locked(conta, quantidade, registrar_preview=False)
    vinculo_monetario = _vinculo_monetario_ativo(int(conta.id))
    if vinculo_monetario is None:
        raise AumentoMultiuserDivergenteError(
            "Vínculo comercial Stripe da Conta não encontrado."
        )
    customer_id, subscription_id = _extrair_ids_vinculo(vinculo_monetario)
    if not customer_id or not subscription_id:
        raise AumentoMultiuserDivergenteError(
            "Customer ou Subscription Stripe da Conta ausente."
        )

    if existente is not None:
        reproduzido = _responder_operacao_existente(
            existente=existente,
            conta=conta,
            quantidade=quantidade,
            commit=commit,
        )
        if reproduzido is not None:
            return reproduzido
        operacao = existente
    else:
        operacao = ContaMultiuserAumentoOperacao(
            conta_id=int(conta.id),
            solicitado_por_user_id=int(ator.id),
            idempotency_key=chave,
            correlation_id=uuid.uuid4().hex,
            quantidade_solicitada=int(quantidade),
            quantity_anterior=int(preview.quantity_atual),
            quantity_nova=int(preview.nova_quantity) if preview.automatico else None,
            estado=ContaMultiuserAumentoOperacao.ESTADO_INICIADO,
            stripe_customer_id=customer_id,
            stripe_subscription_id=subscription_id,
        )
        db.session.add(operacao)
        try:
            with db.session.begin_nested():
                db.session.flush()
        except IntegrityError:
            existente = ContaMultiuserAumentoOperacao.query.filter_by(
                idempotency_key=chave
            ).one()
            reproduzido = _responder_operacao_existente(
                existente=existente,
                conta=conta,
                quantidade=quantidade,
                commit=commit,
            )
            if reproduzido is not None:
                return reproduzido
            operacao = existente

    _registrar_fato(
        tipo_fato="multiuser_aumento_confirmado",
        status_tecnico="recebido",
        conta_id=int(conta.id),
        usuario_id=int(ator.id),
        correlation_id=operacao.correlation_id,
        idempotency_key=f"mu_aumento_confirmado:{operacao.correlation_id}",
        snapshot={
            "quantidade_solicitada": quantidade,
            "automatico_preview": preview.automatico,
        },
    )

    if preview.requer_analise:
        operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE
        operacao.quantity_nova = None
        db.session.add(operacao)
        _registrar_fato(
            tipo_fato="multiuser_aumento_enviado_analise",
            status_tecnico="sem_efeito",
            conta_id=int(conta.id),
            usuario_id=int(ator.id),
            correlation_id=operacao.correlation_id,
            idempotency_key=f"mu_aumento_analise:{operacao.correlation_id}",
            snapshot={
                "quantidade_solicitada": quantidade,
                "acumulado_ciclo": preview.acumulado_ciclo,
                "saldo_automatico": preview.saldo_automatico,
            },
        )
        logger.info(
            "evento=aumento_enviado_analise conta_id=%s correlation_id=%s qtd=%s",
            conta.id,
            operacao.correlation_id,
            quantidade,
        )
        from app.services.conta_multiuser_aumento_excepcional_service import (
            adotar_operacao_enviada_analise,
            comunicar_estado_excepcional,
        )

        row_exc = adotar_operacao_enviada_analise(operacao, commit=False)
        comunicar_estado_excepcional(row_exc, "enviado_analise", enviar_email=bool(commit))
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return _resultado_de_operacao(
            operacao,
            preview,
            replay=False,
            mensagem="Esta solicitação requer análise.",
        )

    try:
        assinatura = _carregar_assinatura_stripe(subscription_id)
    except Exception as exc:
        operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO
        db.session.add(operacao)
        _registrar_fato(
            tipo_fato="multiuser_aumento_falha_reconciliacao",
            status_tecnico="assinatura_indisponivel",
            conta_id=int(conta.id),
            usuario_id=int(ator.id),
            correlation_id=operacao.correlation_id,
            subscription_id=subscription_id,
            customer_id=customer_id,
            snapshot={"motivo": "falha_leitura_assinatura"},
        )
        if commit:
            db.session.commit()
        raise AumentoMultiuserDivergenteError(
            "Não foi possível ler a assinatura Stripe para validar a quantity."
        ) from exc

    customer_stripe = str(assinatura.get("customer") or "").strip() or None
    sub_stripe = str(assinatura.get("id") or "").strip() or None
    qty_stripe, item_id = _ler_quantity_stripe_assinatura(assinatura)
    if customer_stripe and customer_stripe != customer_id:
        operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO
        db.session.add(operacao)
        _registrar_fato(
            tipo_fato="multiuser_aumento_falha_reconciliacao",
            status_tecnico="customer_divergente",
            conta_id=int(conta.id),
            usuario_id=int(ator.id),
            correlation_id=operacao.correlation_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            snapshot={"motivo": "customer_divergente"},
        )
        if commit:
            db.session.commit()
        raise AumentoMultiuserDivergenteError(
            "Customer Stripe diverge do vínculo comercial da Conta."
        )
    if sub_stripe and sub_stripe != subscription_id:
        operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO
        db.session.add(operacao)
        if commit:
            db.session.commit()
        raise AumentoMultiuserDivergenteError(
            "Subscription Stripe diverge do vínculo comercial da Conta."
        )
    if item_id is None:
        operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO
        db.session.add(operacao)
        if commit:
            db.session.commit()
        raise AumentoMultiuserDivergenteError(
            "Subscription Item Stripe não encontrado para atualizar a quantity."
        )
    if _divergencia_quantity_relevante(conta, qty_stripe):
        operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO
        db.session.add(operacao)
        _registrar_fato(
            tipo_fato="multiuser_aumento_falha_reconciliacao",
            status_tecnico="quantity_divergente",
            conta_id=int(conta.id),
            usuario_id=int(ator.id),
            correlation_id=operacao.correlation_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            snapshot={
                "quantity_local": conta.quantidade_assentos_contratados,
                "quantity_stripe": qty_stripe,
            },
        )
        logger.info(
            "evento=aumento_bloqueado_divergencia conta_id=%s local=%s stripe=%s",
            conta.id,
            conta.quantidade_assentos_contratados,
            qty_stripe,
        )
        if commit:
            db.session.commit()
        raise AumentoMultiuserDivergenteError(
            "Quantity local e Stripe divergem; o aumento foi bloqueado."
        )

    ciclo_local = _resolver_ciclo_conta(int(conta.id))
    preco_stripe = _preco_unitario_stripe_assinatura(assinatura)
    if preco_stripe is not None and preco_stripe != preview.valor_unitario:
        _marcar_falha_reconciliacao(
            operacao=operacao,
            conta=conta,
            ator=ator,
            customer_id=customer_id,
            subscription_id=subscription_id,
            status_tecnico="preco_divergente",
            snapshot={
                "preco_local": str(preview.valor_unitario),
                "preco_stripe": str(preco_stripe),
            },
            commit=commit,
            mensagem="O preço unitário da assinatura Stripe diverge do preço configurado.",
        )
    ini_stripe, fim_stripe = _periodo_stripe_assinatura(assinatura)
    if ini_stripe is not None and fim_stripe is not None:
        if ini_stripe != ciclo_local.inicio or fim_stripe != ciclo_local.fim:
            _marcar_falha_reconciliacao(
                operacao=operacao,
                conta=conta,
                ator=ator,
                customer_id=customer_id,
                subscription_id=subscription_id,
                status_tecnico="ciclo_divergente",
                snapshot={
                    "ciclo_local_inicio": ciclo_local.inicio.isoformat(),
                    "ciclo_local_fim": ciclo_local.fim.isoformat(),
                    "ciclo_stripe_inicio": ini_stripe.isoformat(),
                    "ciclo_stripe_fim": fim_stripe.isoformat(),
                },
                commit=commit,
                mensagem="O ciclo comercial local diverge do período vigente na Stripe.",
            )

    contador, _ciclo = _contador_ciclo_atual(int(conta.id))
    reservado = _reservar_acumulado_automatico(
        contador,
        int(quantidade),
        int(preview.limite_automatico_ciclo),
    )
    if not reservado:
        operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE
        operacao.quantity_nova = None
        db.session.add(operacao)
        _registrar_fato(
            tipo_fato="multiuser_aumento_enviado_analise",
            status_tecnico="sem_efeito",
            conta_id=int(conta.id),
            usuario_id=int(ator.id),
            correlation_id=operacao.correlation_id,
            idempotency_key=f"mu_aumento_analise:{operacao.correlation_id}",
            snapshot={
                "quantidade_solicitada": quantidade,
                "motivo": "saldo_automatico_insuficiente_concorrencia",
            },
        )
        logger.info(
            "evento=aumento_enviado_analise motivo=concorrencia conta_id=%s qtd=%s",
            conta.id,
            quantidade,
        )
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        preview_conc = _calcular_preview_locked(conta, quantidade, registrar_preview=False)
        return _resultado_de_operacao(
            operacao,
            preview_conc,
            replay=False,
            mensagem="Esta solicitação requer análise.",
        )

    stripe_key = f"stripe_multiuser_aumento:{operacao.correlation_id}"[:200]
    ja_atualizado = (
        qty_stripe is not None and int(qty_stripe) == int(preview.nova_quantity)
    )
    if not ja_atualizado:
        try:
            item_resp = _atualizar_quantity_item_stripe(
                item_id=item_id,
                nova_quantity=int(preview.nova_quantity),
                idempotency_key=stripe_key,
            )
        except Exception as exc:
            _desfazer_reserva_acumulado(contador, int(quantidade))
            operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO
            db.session.add(operacao)
            _registrar_fato(
                tipo_fato="multiuser_aumento_falha_reconciliacao",
                status_tecnico="stripe_update_falhou",
                conta_id=int(conta.id),
                usuario_id=int(ator.id),
                correlation_id=operacao.correlation_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                snapshot={"motivo": "stripe_update_falhou"},
            )
            if commit:
                db.session.commit()
            raise AumentoMultiuserDivergenteError(
                "Não foi possível atualizar a quantity na Stripe."
            ) from exc
        item_id_resp = str(item_resp.get("id") or item_id)
        if item_id_resp != item_id:
            _desfazer_reserva_acumulado(contador, int(quantidade))
            operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_FALHA_RECONCILIACAO
            db.session.add(operacao)
            if commit:
                db.session.commit()
            raise AumentoMultiuserDivergenteError(
                "A Stripe devolveu um Subscription Item diferente do item atual."
            )

    operacao.stripe_customer_id = customer_id
    operacao.stripe_subscription_id = subscription_id
    operacao.stripe_subscription_item_id = item_id
    operacao.stripe_quantity_enviada = int(preview.nova_quantity)
    operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_STRIPE_ENVIADO
    db.session.add(operacao)
    db.session.flush()
    _registrar_fato(
        tipo_fato="multiuser_aumento_stripe_quantity_atualizada",
        status_tecnico="stripe_ok",
        conta_id=int(conta.id),
        usuario_id=int(ator.id),
        correlation_id=operacao.correlation_id,
        idempotency_key=f"mu_aumento_stripe:{operacao.correlation_id}",
        customer_id=customer_id,
        subscription_id=subscription_id,
        snapshot={
            "quantity": int(preview.nova_quantity),
            "proration_behavior": PRORATION_BEHAVIOR_NONE,
        },
    )
    if commit:
        db.session.commit()
        bloquear_conta_para_capacidade(int(conta.id))
        conta = db.session.get(Conta, int(conta.id))
        if conta is None:
            raise AumentoMultiuserInvalidoError("Conta não encontrada.")
        vinculo_monetario = _vinculo_monetario_ativo(int(conta.id))
        if vinculo_monetario is None:
            raise AumentoMultiuserDivergenteError(
                "Vínculo comercial Stripe da Conta não encontrado."
            )
        operacao = db.session.get(ContaMultiuserAumentoOperacao, int(operacao.id))
        if operacao is None:
            raise AumentoMultiuserDivergenteError(
                "Operação de aumento não encontrada após a confirmação Stripe."
            )
        db.session.refresh(contador)

    criadas = _aplicar_efeitos_locais_aumento(
        conta=conta,
        nova_quantity=int(preview.nova_quantity),
        vinculo_monetario=vinculo_monetario,
    )
    operacao.quantity_nova = int(preview.nova_quantity)
    operacao.estado = ContaMultiuserAumentoOperacao.ESTADO_APROVADO_AUTOMATICO
    db.session.add(operacao)
    _registrar_fato(
        tipo_fato="multiuser_aumento_automatico_aprovado",
        status_tecnico="aplicado",
        conta_id=int(conta.id),
        usuario_id=int(ator.id),
        correlation_id=operacao.correlation_id,
        idempotency_key=f"mu_aumento_aprovado:{operacao.correlation_id}",
        customer_id=customer_id,
        subscription_id=subscription_id,
        snapshot={
            "quantity_nova": int(preview.nova_quantity),
            "acumulado_ciclo": int(contador.acumulado_automatico),
        },
    )
    _registrar_fato(
        tipo_fato="multiuser_aumento_quantity_local_atualizada",
        status_tecnico="aplicado",
        conta_id=int(conta.id),
        usuario_id=int(ator.id),
        correlation_id=operacao.correlation_id,
        idempotency_key=f"mu_aumento_local:{operacao.correlation_id}",
        snapshot={"quantity_nova": int(preview.nova_quantity)},
    )
    _registrar_fato(
        tipo_fato="multiuser_aumento_assentos_disponibilizados",
        status_tecnico="aplicado",
        conta_id=int(conta.id),
        usuario_id=int(ator.id),
        correlation_id=operacao.correlation_id,
        idempotency_key=f"mu_aumento_assentos:{operacao.correlation_id}",
        snapshot={
            "franquias_criadas": criadas,
            "quantity_nova": int(preview.nova_quantity),
        },
    )
    logger.info(
        "evento=aumento_automatico_aprovado conta_id=%s correlation_id=%s "
        "qtd=%s quantity_nova=%s franquias_criadas=%s",
        conta.id,
        operacao.correlation_id,
        quantidade,
        preview.nova_quantity,
        criadas,
    )
    preview_final = _calcular_preview_locked(conta, 1, registrar_preview=False)
    resultado = _resultado_de_operacao(
        operacao,
        PreviewAumentoAssentos(
            quantity_atual=int(preview.nova_quantity),
            quantidade_solicitada=int(quantidade),
            nova_quantity=int(preview.nova_quantity),
            valor_unitario=preview.valor_unitario,
            valor_mensal_atual=preview.valor_mensal_projetado,
            aumento_renovacao=preview.aumento_renovacao,
            valor_mensal_projetado=preview.valor_mensal_projetado,
            valor_hoje=_ZERO,
            proxima_cobranca=preview.proxima_cobranca,
            automatico=True,
            acumulado_ciclo=int(contador.acumulado_automatico),
            saldo_automatico=max(
                0, preview.limite_automatico_ciclo - int(contador.acumulado_automatico)
            ),
            limite_automatico_ciclo=preview.limite_automatico_ciclo,
            requer_analise=False,
        ),
        replay=False,
        franquias_criadas=criadas,
        mensagem="Assentos adicionados. A cobrança entra na próxima renovação.",
    )
    _ = preview_final
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return resultado


def resultado_para_api(resultado: ResultadoAumentoAssentos) -> dict[str, Any]:
    return {
        "estado": resultado.estado,
        "automatico": resultado.automatico,
        "requer_analise": resultado.requer_analise,
        "mensagem": resultado.mensagem,
        "quantity_atual": resultado.quantity_atual,
        "quantity_nova": resultado.quantity_nova,
        "quantidade_solicitada": resultado.quantidade_solicitada,
        "valor_hoje": str(resultado.valor_hoje),
        "valor_hoje_rotulo": f"Hoje: {formatar_brl(resultado.valor_hoje)}",
        "valor_mensal_atual": str(resultado.valor_mensal_atual),
        "aumento_renovacao": str(resultado.aumento_renovacao),
        "valor_mensal_projetado": str(resultado.valor_mensal_projetado),
        "proxima_cobranca_rotulo": formatar_data_br(resultado.proxima_cobranca),
        "acumulado_ciclo": resultado.acumulado_ciclo,
        "saldo_automatico": resultado.saldo_automatico,
        "franquias_criadas": resultado.franquias_criadas,
        "replay": resultado.replay,
    }


def _listar_membros_gerenciais(conta_id: int, agora) -> list[MembroPainel]:
    membros: list[MembroPainel] = []
    vinculos = (
        ContaVinculoOrganizacional.query.filter_by(conta_id=int(conta_id))
        .order_by(ContaVinculoOrganizacional.id.asc())
        .all()
    )
    for vinculo in vinculos:
        user = db.session.get(User, int(vinculo.user_id))
        if (vinculo.estado or "") == ESTADO_ATIVO:
            estado_g = ESTADO_GERENCIAL_ATIVO
        elif (vinculo.estado or "") == ESTADO_ENCERRADO:
            estado_g = ESTADO_GERENCIAL_REVOGADO
        else:
            estado_g = ESTADO_GERENCIAL_ATIVO
        membros.append(
            MembroPainel(
                nome=(user.full_name if user is not None else None),
                email=(user.email if user is not None else None),
                papel=vinculo.papel,
                estado_vinculo=vinculo.estado,
                estado_convite=None,
                estado_gerencial=estado_g,
                estado_gerencial_rotulo=ROTULO_ESTADO_GERENCIAL[estado_g],
                ocupa_assento=(vinculo.estado or "") == ESTADO_ATIVO,
                user_id=int(vinculo.user_id),
                vinculo_id=int(vinculo.id),
            )
        )
    convites = (
        ContaMultiuserConvite.query.filter(
            ContaMultiuserConvite.conta_id == int(conta_id),
            ContaMultiuserConvite.estado == ContaMultiuserConvite.ESTADO_PENDENTE,
            ContaMultiuserConvite.expires_at > agora,
        )
        .order_by(ContaMultiuserConvite.id.asc())
        .all()
    )
    for convite in convites:
        membros.append(
            MembroPainel(
                nome=None,
                email=convite.email_destino,
                papel=None,
                estado_vinculo=None,
                estado_convite=convite.estado,
                estado_gerencial=ESTADO_GERENCIAL_PENDENTE_ATIVACAO,
                estado_gerencial_rotulo=ROTULO_ESTADO_GERENCIAL[
                    ESTADO_GERENCIAL_PENDENTE_ATIVACAO
                ],
                ocupa_assento=False,
            )
        )
    snap = snapshot_capacidade(int(conta_id))
    pendentes = contar_reservas_pendentes_validas(int(conta_id), agora)
    ativos = snap.vinculos_ativos
    contratados = snap.quantidade_contratada or 0
    vagos = max(0, int(contratados) - int(ativos) - int(pendentes))
    for _ in range(vagos):
        membros.append(
            MembroPainel(
                nome=None,
                email=None,
                papel=None,
                estado_vinculo=None,
                estado_convite=None,
                estado_gerencial=ESTADO_GERENCIAL_AGUARDANDO_VINCULO,
                estado_gerencial_rotulo=ROTULO_ESTADO_GERENCIAL[
                    ESTADO_GERENCIAL_AGUARDANDO_VINCULO
                ],
                ocupa_assento=False,
            )
        )
    return membros


def montar_painel_contratante(ator: User) -> PainelContratante:
    vinculo = exigir_contratante_ativo_gestao(ator)
    conta = db.session.get(Conta, int(vinculo.conta_id))
    if conta is None:
        raise AumentoMultiuserInvalidoError("Conta não encontrada.")
    agora = utcnow_naive()
    liberar_reservas_expiradas_conta(conta.id, agora)
    snap = snapshot_capacidade(int(conta.id))
    contratados = int(snap.quantidade_contratada or 0)
    ativos = int(snap.vinculos_ativos)
    pendentes = contar_reservas_pendentes_validas(int(conta.id), agora)
    disponiveis = max(0, contratados - ativos - pendentes)
    limite = _limite_automatico_ciclo()
    try:
        contador, ciclo = _contador_ciclo_atual(int(conta.id))
        utilizado = int(contador.acumulado_automatico or 0)
        proxima = ciclo.fim
    except AumentoMultiuserCicloIndeterminadoError:
        utilizado = 0
        proxima = None
    restante = max(0, limite - utilizado)
    valor_unitario = obter_valor_admin_plano(CODIGO_MULTIUSER, exigir_configurado=False)
    if valor_unitario is None:
        valor_unitario = _ZERO
    else:
        valor_unitario = _money(valor_unitario)
    valor_mensal = _money(valor_unitario * Decimal(contratados))
    analise_pendente = (
        ContaMultiuserAumentoOperacao.query.filter_by(
            conta_id=int(conta.id),
            estado=ContaMultiuserAumentoOperacao.ESTADO_ENVIADO_ANALISE,
        ).first()
        is not None
    )
    return PainelContratante(
        conta_id=int(conta.id),
        assentos_contratados=contratados,
        usuarios_ativos=ativos,
        convites_pendentes=pendentes,
        assentos_disponiveis=disponiveis,
        aumento_automatico_utilizado=utilizado,
        aumento_automatico_restante=restante,
        limite_automatico_ciclo=limite,
        proxima_cobranca=proxima,
        valor_mensal_atual=valor_mensal,
        valor_unitario=valor_unitario,
        membros=_listar_membros_gerenciais(int(conta.id), agora),
        solicitacao_analise_pendente=analise_pendente,
    )


def painel_para_template(painel: PainelContratante) -> dict[str, Any]:
    return {
        "conta_id": painel.conta_id,
        "assentos_contratados": painel.assentos_contratados,
        "usuarios_ativos": painel.usuarios_ativos,
        "convites_pendentes": painel.convites_pendentes,
        "assentos_disponiveis": painel.assentos_disponiveis,
        "aumento_automatico_utilizado": painel.aumento_automatico_utilizado,
        "aumento_automatico_restante": painel.aumento_automatico_restante,
        "limite_automatico_ciclo": painel.limite_automatico_ciclo,
        "proxima_cobranca_rotulo": formatar_data_br(painel.proxima_cobranca),
        "valor_mensal_atual_rotulo": formatar_brl(painel.valor_mensal_atual),
        "valor_unitario_rotulo": formatar_brl(painel.valor_unitario),
        "membros": painel.membros,
        "solicitacao_analise_pendente": painel.solicitacao_analise_pendente,
    }
