"""
Contratação comercial Multiuser (Fase 3).

Separa perfil empresarial (pré-Checkout) de benefício financeiro confirmado.
Não chama Stripe no núcleo de ativação local. A leitura do Price
Stripe ocorre só na validação de contratação (resumo/checkout).
Não envia convites. Não cria Users fictícios.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Conta, ContaVinculoOrganizacional, User
from app.services.cnpj_service import exigir_cnpj_normalizado_valido
from app.services.conta_multiuser_capacidade_service import (
    bloquear_conta_para_capacidade,
    materializar_franquias_faltantes,
    ocupar_assento,
    snapshot_capacidade,
)
from app.services.conta_multiuser_ciclo_service import (
    aplicar_fanout_ciclo_confirmado,
    evento_atrasado_relativo_ciclo_canonico,
    periodo_completo_valido,
)
from app.services.conta_multiuser_errors import VinculoInconsistenteError
from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    ORIGEM_CONTRATACAO_STRIPE,
    PAPEL_CONTRATANTE,
    quantidade_assentos_valida,
)
from app.services.conta_organizacional_service import (
    CnpjMultiuserDuplicadoError,
    buscar_conta_multiuser_ativa_por_cnpj,
    converter_ou_relancar_integrity_error,
    marcar_conta_multiuser_ativa,
    persistir_dados_empresariais_conta,
    persistir_quantidade_assentos_contratados,
)
from app.services.plano_service import (
    obter_configuracao_gateway_plano_admin,
    obter_limite_referencia_plano_admin,
    obter_quantidade_minima_multiuser_admin,
    obter_valor_admin_plano,
)

logger = logging.getLogger(__name__)

CODIGO_MULTIUSER = "multiuser"
_CENTAVOS = Decimal("0.01")

_CAMPOS_EMPRESARIAIS_OBRIGATORIOS = (
    "razao_social",
    "nome_fantasia",
    "cnpj",
    "email_empresarial",
    "endereco_logradouro",
    "endereco_numero",
    "endereco_cidade",
    "endereco_uf",
    "endereco_cep",
)


class ConfiguracaoMultiuserIncompletaError(ValueError):
    """Product/Price/mínimo/valor Multiuser ausente ou inválido."""


class QuantityMultiuserInvalidaError(ValueError):
    """Quantity abaixo do mínimo configurado ou inválida."""


class PriceMultiuserIncompativelError(ValueError):
    """Price Stripe não corresponde ao plano Multiuser configurado."""


class PeriodoFinanceiroInvalidoError(ValueError):
    """Período Stripe ausente ou cronologicamente inválido."""


class PriceStripeDivergenteError(ValueError):
    """Price real da Stripe diverge da configuração administrativa."""


class ReservaInvoiceMultiuserReplay(Exception):
    """Idempotência da invoice já adquirida por outro processamento."""


@dataclass(frozen=True)
class ResumoContratacaoMultiuser:
    quantidade: int
    quantidade_minima: int
    valor_unitario: Decimal
    valor_mensal: Decimal
    currency: str | None
    interval: str | None
    price_id: str | None
    product_id: str | None
    gateway_pronto: bool
    pendencias: tuple[str, ...]


@dataclass
class ResultadoAtivacaoMultiuser:
    conta_id: int
    quantity_confirmada: int
    quantity_solicitada_observada: int | None
    divergencia_quantity: bool
    franquias_total: int
    franquias_livres: int
    vinculo_contratante_id: int | None
    franquia_contratante_id: int | None
    replay: bool = False
    ciclo_ignorado_evento_antigo: bool = False
    consumo_resetado: bool = False
    motivo: str | None = None
    franquia_ids: list[int] = field(default_factory=list)


def _to_int_or_none(value: Any) -> int | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        txt = str(value).strip()
        if not txt:
            return None
        return int(txt)
    except (TypeError, ValueError):
        return None


def exigir_gateway_multiuser_pronto() -> dict[str, Any]:
    """
    Falha fechada se Product/Price/currency/interval/pronto não estiverem completos.
    Não inventa IDs. Não cria Product/Price.
    """
    cfg = obter_configuracao_gateway_plano_admin(CODIGO_MULTIUSER)
    if not cfg:
        raise ConfiguracaoMultiuserIncompletaError(
            "Plano Multiusuário sem configuração de gateway no admin."
        )
    pendencias = list(cfg.get("pendencias") or [])
    if not cfg.get("configuracao_valida"):
        raise ConfiguracaoMultiuserIncompletaError(
            "Plano Multiusuário com configuração Stripe pendente no admin."
        )
    price_id = (cfg.get("price_id") or "").strip()
    if not price_id:
        pendencias.append("gateway_price_id_nao_configurado")
        raise ConfiguracaoMultiuserIncompletaError(
            "Plano Multiusuário sem Price ID Stripe configurado."
        )
    if (cfg.get("provider") or "").strip().lower() != "stripe":
        raise ConfiguracaoMultiuserIncompletaError(
            "Plano Multiusuário exige provider Stripe."
        )
    return cfg


def valor_admin_para_centavos(valor: Decimal) -> int:
    quantizado = valor.quantize(_CENTAVOS, rounding=ROUND_HALF_UP)
    return int((quantizado * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))


def confrontar_price_stripe_multiuser_admin(*, registrar_fato: bool = False) -> dict[str, Any]:
    """
    Lê o Price real na Stripe e confronta com a configuração administrativa.
    Divergência: fail-closed. Não corrige o valor exibido.
    """
    cfg = exigir_gateway_multiuser_pronto()
    valor_admin = obter_valor_admin_plano(CODIGO_MULTIUSER, exigir_configurado=True)
    if valor_admin is None:
        raise ConfiguracaoMultiuserIncompletaError(
            "Valor por acesso do plano Multiusuário não está configurado em /admin/planos."
        )
    price_id = (cfg.get("price_id") or "").strip()
    from app.services.cleiton_monetizacao_service import _stripe_get

    try:
        price = _stripe_get(f"/prices/{price_id}")
    except Exception as exc:
        raise PriceStripeDivergenteError(
            "Não foi possível ler o Price Stripe do Multiusuário."
        ) from exc
    if not isinstance(price, dict) or (price.get("id") or "").strip() != price_id:
        raise PriceStripeDivergenteError("Price Stripe inexistente ou ID divergente.")
    if price.get("active") is not True:
        raise PriceStripeDivergenteError("Price Stripe do Multiusuário está inativo.")
    currency_stripe = (price.get("currency") or "").strip().lower()
    currency_admin = (cfg.get("currency") or "").strip().lower()
    if currency_stripe != currency_admin:
        raise PriceStripeDivergenteError("Currency do Price Stripe diverge da configuração administrativa.")
    product = price.get("product")
    product_id = (product.get("id") if isinstance(product, dict) else product) or ""
    product_id = str(product_id).strip()
    product_admin = (cfg.get("product_id") or "").strip()
    if product_id != product_admin:
        raise PriceStripeDivergenteError("Product do Price Stripe diverge da configuração administrativa.")
    recurring = price.get("recurring") if isinstance(price.get("recurring"), dict) else None
    if not recurring:
        raise PriceStripeDivergenteError("Price Stripe do Multiusuário não é recorrente.")
    interval_stripe = (recurring.get("interval") or "").strip().lower()
    interval_admin = (cfg.get("interval") or "").strip().lower()
    if interval_stripe != interval_admin:
        raise PriceStripeDivergenteError("Intervalo do Price Stripe diverge da configuração administrativa.")
    usage_type = (recurring.get("usage_type") or "").strip().lower()
    if usage_type and usage_type != "licensed":
        raise PriceStripeDivergenteError("Price Stripe não é licensed/quantity para Multiusuário.")
    cents_admin = valor_admin_para_centavos(valor_admin)
    unit_amount = price.get("unit_amount")
    unit_decimal = price.get("unit_amount_decimal")
    cents_stripe = None
    if unit_amount is not None:
        try:
            cents_stripe = int(unit_amount)
        except (TypeError, ValueError) as exc:
            raise PriceStripeDivergenteError("unit_amount Stripe inválido.") from exc
    if unit_decimal is not None and str(unit_decimal).strip() != "":
        try:
            cents_from_decimal = int(Decimal(str(unit_decimal)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise PriceStripeDivergenteError("unit_amount_decimal Stripe inválido.") from exc
        if cents_stripe is not None and cents_from_decimal != cents_stripe:
            raise PriceStripeDivergenteError("unit_amount e unit_amount_decimal Stripe divergem entre si.")
        cents_stripe = cents_from_decimal if cents_stripe is None else cents_stripe
    if cents_stripe is None:
        raise PriceStripeDivergenteError("Price Stripe sem unit_amount utilizável.")
    if cents_stripe != cents_admin:
        raise PriceStripeDivergenteError(
            "Valor unitário do Price Stripe diverge do preço administrativo."
        )
    return {
        "price_id": price_id,
        "unit_amount": cents_stripe,
        "currency": currency_stripe,
        "interval": interval_stripe,
        "product_id": product_id,
        "active": True,
    }


def multiuser_pronto_para_contratacao_publica() -> bool:
    try:
        exigir_gateway_multiuser_pronto()
        obter_valor_admin_plano(CODIGO_MULTIUSER, exigir_configurado=True)
        obter_quantidade_minima_multiuser_admin(exigir_configurado=True)
        obter_limite_referencia_plano_admin(CODIGO_MULTIUSER, exigir_configurado=True)
        confrontar_price_stripe_multiuser_admin()
    except (ValueError, ConfiguracaoMultiuserIncompletaError, PriceStripeDivergenteError):
        return False
    return True


def validar_quantity_solicitada(quantidade: Any) -> int:
    qtd = _to_int_or_none(quantidade)
    if qtd is None:
        raise QuantityMultiuserInvalidaError("Quantidade de acessos inválida.")
    quantidade_assentos_valida(qtd)
    minimo = obter_quantidade_minima_multiuser_admin(exigir_configurado=True)
    if minimo is None:
        raise ConfiguracaoMultiuserIncompletaError(
            "Quantidade mínima do plano Multiusuário não está configurada em /admin/planos."
        )
    if qtd < int(minimo):
        raise QuantityMultiuserInvalidaError(
            "Quantidade de acessos abaixo do mínimo configurado para o plano Multiusuário."
        )
    return qtd


def calcular_resumo_contratacao_multiuser(quantidade: Any) -> ResumoContratacaoMultiuser:
    """Cálculo canônico da mensalidade: valor_admin × quantity. Sem hardcode comercial."""
    pendencias: list[str] = []
    cfg: dict[str, Any] | None = None
    try:
        cfg = exigir_gateway_multiuser_pronto()
    except ConfiguracaoMultiuserIncompletaError:
        cfg = obter_configuracao_gateway_plano_admin(CODIGO_MULTIUSER) or {}
        pendencias.extend(list(cfg.get("pendencias") or ["gateway_multiuser_incompleto"]))

    minimo = obter_quantidade_minima_multiuser_admin(exigir_configurado=False)
    if minimo is None:
        pendencias.append("quantidade_minima_nao_configurada")
        raise ConfiguracaoMultiuserIncompletaError(
            "Quantidade mínima do plano Multiusuário não está configurada em /admin/planos."
        )
    valor_unitario = obter_valor_admin_plano(CODIGO_MULTIUSER, exigir_configurado=True)
    if valor_unitario is None:
        raise ConfiguracaoMultiuserIncompletaError(
            "Valor por acesso do plano Multiusuário não está configurado em /admin/planos."
        )
    qtd = validar_quantity_solicitada(quantidade)
    confrontar_price_stripe_multiuser_admin()
    valor_mensal = (valor_unitario * Decimal(qtd)).quantize(_CENTAVOS, rounding=ROUND_HALF_UP)
    gateway_pronto = bool(cfg and cfg.get("configuracao_valida") and not pendencias)
    return ResumoContratacaoMultiuser(
        quantidade=qtd,
        quantidade_minima=int(minimo),
        valor_unitario=valor_unitario.quantize(_CENTAVOS, rounding=ROUND_HALF_UP),
        valor_mensal=valor_mensal,
        currency=(cfg.get("currency") if cfg else None),
        interval=(cfg.get("interval") if cfg else None),
        price_id=(cfg.get("price_id") if cfg else None),
        product_id=(cfg.get("product_id") if cfg else None),
        gateway_pronto=gateway_pronto,
        pendencias=tuple(pendencias),
    )


def resumo_contratacao_multiuser_para_api(quantidade: Any) -> dict[str, Any]:
    resumo = calcular_resumo_contratacao_multiuser(quantidade)
    return {
        "plano": "Multiusuário",
        "plano_codigo": CODIGO_MULTIUSER,
        "acessos": resumo.quantidade,
        "quantidade_minima": resumo.quantidade_minima,
        "valor_por_acesso": str(resumo.valor_unitario),
        "total_mensal": str(resumo.valor_mensal),
        "currency": resumo.currency,
        "interval": resumo.interval,
        "price_id": resumo.price_id,
        "gateway_pronto": resumo.gateway_pronto,
        "pendencias": list(resumo.pendencias),
    }


def _exigir_campos_empresariais(dados: dict[str, Any]) -> None:
    faltantes = [
        campo
        for campo in _CAMPOS_EMPRESARIAIS_OBRIGATORIOS
        if not str(dados.get(campo) or "").strip()
    ]
    if faltantes:
        raise ValueError(
            "Dados empresariais incompletos para contratação Multiusuário."
        )


def persistir_perfil_empresarial_pre_checkout(
    conta_id: int,
    dados: dict[str, Any],
    *,
    commit: bool = False,
) -> Conta:
    """
    Persiste perfil empresarial sem ativar benefício, quantity local ou Contratante.
    CNPJ válido e unicidade contra Conta Multiuser ativa são obrigatórios.
    """
    _exigir_campos_empresariais(dados)
    cnpj_norm = exigir_cnpj_normalizado_valido(dados.get("cnpj"))
    existente = buscar_conta_multiuser_ativa_por_cnpj(
        cnpj_norm, excluir_conta_id=int(conta_id)
    )
    if existente is not None:
        raise CnpjMultiuserDuplicadoError(
            "CNPJ já utilizado por outra Conta Multiusuário ativa."
        )
    conta = persistir_dados_empresariais_conta(
        int(conta_id),
        razao_social=dados.get("razao_social"),
        nome_fantasia=dados.get("nome_fantasia"),
        cnpj=cnpj_norm,
        email_empresarial=dados.get("email_empresarial"),
        endereco_logradouro=dados.get("endereco_logradouro"),
        endereco_numero=dados.get("endereco_numero"),
        endereco_complemento=dados.get("endereco_complemento"),
        endereco_bairro=dados.get("endereco_bairro"),
        endereco_cidade=dados.get("endereco_cidade"),
        endereco_uf=dados.get("endereco_uf"),
        endereco_cep=dados.get("endereco_cep"),
        commit=commit,
    )
    logger.info(
        "Perfil empresarial Multiuser persistido conta_id=%s (benefício não ativado)",
        conta.id,
    )
    return conta


def _price_pertence_ao_multiuser(price_id: str | None) -> bool:
    cfg = obter_configuracao_gateway_plano_admin(CODIGO_MULTIUSER)
    if not cfg or not cfg.get("configuracao_valida"):
        return False
    esperado = (cfg.get("price_id") or "").strip()
    recebido = (price_id or "").strip()
    return bool(esperado) and esperado == recebido


def chave_idempotencia_ciclo_multiuser(conta_id: int, invoice_id: str) -> str:
    return f"stripe_multiuser_ciclo:{int(conta_id)}:{invoice_id}"[:190]


def _fato_ciclo_multiuser_existente(conta_id: int, invoice_id: str | None):
    from app.models import MonetizacaoFato

    if not invoice_id:
        return None
    return MonetizacaoFato.query.filter_by(
        idempotency_key=chave_idempotencia_ciclo_multiuser(conta_id, invoice_id)
    ).first()


def reservar_idempotencia_invoice_multiuser(
    *,
    conta_id: int,
    invoice_id: str | None,
    price_id: str | None,
    usuario_id: int | None,
) -> tuple[Any, bool]:
    """
    INSERT da chave única ANTES dos efeitos. Mesma transação.
    Retorna (fato, replay).
    """
    from app.models import MonetizacaoFato, utcnow_naive

    if not invoice_id:
        raise PeriodoFinanceiroInvalidoError("Invoice ID obrigatório para reserva Multiuser.")
    key = chave_idempotencia_ciclo_multiuser(conta_id, invoice_id)
    existente = MonetizacaoFato.query.filter_by(idempotency_key=key).first()
    if existente is not None:
        return existente, True
    fato = MonetizacaoFato(
        tipo_fato="stripe_multiuser_ciclo_aplicado",
        status_tecnico="efeito_operacional_aplicado",
        idempotency_key=key,
        provider="stripe",
        conta_id=int(conta_id),
        usuario_id=_to_int_or_none(usuario_id),
        invoice_id=invoice_id,
        price_id=price_id,
        timestamp_interno=utcnow_naive(),
        snapshot_normalizado_json=json.dumps(
            {"fase": "reservado", "invoice_id": invoice_id},
            ensure_ascii=True,
            sort_keys=True,
        ),
    )
    db.session.add(fato)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        existente = MonetizacaoFato.query.filter_by(idempotency_key=key).first()
        if existente is not None:
            return existente, True
        raise
    return fato, False


def ativar_beneficio_multiuser_confirmado(
    *,
    conta_id: int,
    user_id: int | None,
    quantity_stripe: int,
    quantity_solicitada: int | None,
    inicio_ciclo: datetime | None,
    fim_ciclo: datetime | None,
    price_id: str | None,
    invoice_id: str | None = None,
    evento_atrasado: bool | None = None,
    intencao_id: int | None = None,
    commit: bool = False,
) -> ResultadoAtivacaoMultiuser:
    """
    Ativa ou renova Multiuser somente após confirmação financeira.

    Reserva idempotência da invoice ANTES de fan-out/reset.
    Quantity local = quantity Stripe confirmada (mínimo revalidado).
    """
    if not _price_pertence_ao_multiuser(price_id):
        raise PriceMultiuserIncompativelError(
            "Price Stripe não pertence ao plano Multiusuário configurado."
        )
    if not periodo_completo_valido(inicio_ciclo, fim_ciclo):
        raise PeriodoFinanceiroInvalidoError(
            "Período Stripe inválido para ativação Multiusuário."
        )
    qtd = validar_quantity_solicitada(quantity_stripe)
    bloquear_conta_para_capacidade(int(conta_id))
    conta = db.session.get(Conta, int(conta_id))
    if conta is None:
        raise ValueError("Conta não encontrada.")

    divergencia = (
        quantity_solicitada is not None
        and int(quantity_solicitada) != int(qtd)
    )
    if divergencia:
        logger.warning(
            "Divergência quantity Multiuser conta_id=%s stripe=%s solicitada=%s",
            conta.id,
            qtd,
            quantity_solicitada,
        )

    snap_atual = snapshot_capacidade(conta.id)
    atrasado = (
        bool(evento_atrasado)
        if evento_atrasado is not None
        else evento_atrasado_relativo_ciclo_canonico(conta.id, inicio_ciclo, fim_ciclo)
    )
    if atrasado and conta.multiuser_ativa:
        logger.info(
            "Evento Multiuser atrasado ignorado conta_id=%s invoice_id=%s",
            conta.id,
            invoice_id,
        )
        return ResultadoAtivacaoMultiuser(
            conta_id=int(conta.id),
            quantity_confirmada=int(conta.quantidade_assentos_contratados or qtd),
            quantity_solicitada_observada=quantity_solicitada,
            divergencia_quantity=divergencia,
            franquias_total=snap_atual.franquias_total,
            franquias_livres=snap_atual.franquias_disponiveis,
            vinculo_contratante_id=None,
            franquia_contratante_id=None,
            replay=True,
            ciclo_ignorado_evento_antigo=True,
            motivo="evento_periodo_atrasado",
        )

    try:
        return _ativar_beneficio_multiuser_confirmado_tx(
            conta=conta,
            user_id=user_id,
            qtd=qtd,
            quantity_solicitada=quantity_solicitada,
            divergencia=divergencia,
            snap_atual=snap_atual,
            inicio_ciclo=inicio_ciclo,
            fim_ciclo=fim_ciclo,
            price_id=price_id,
            invoice_id=invoice_id,
            intencao_id=intencao_id,
            commit=commit,
        )
    except Exception:
        db.session.rollback()
        raise


def _ativar_beneficio_multiuser_confirmado_tx(
    *,
    conta: Conta,
    user_id: int | None,
    qtd: int,
    quantity_solicitada: int | None,
    divergencia: bool,
    snap_atual,
    inicio_ciclo: datetime | None,
    fim_ciclo: datetime | None,
    price_id: str | None,
    invoice_id: str | None,
    intencao_id: int | None,
    commit: bool,
) -> ResultadoAtivacaoMultiuser:
    fato_reserva, replay = reservar_idempotencia_invoice_multiuser(
        conta_id=int(conta.id),
        invoice_id=invoice_id,
        price_id=price_id,
        usuario_id=user_id,
    )
    if replay:
        return ResultadoAtivacaoMultiuser(
            conta_id=int(conta.id),
            quantity_confirmada=int(conta.quantidade_assentos_contratados or qtd),
            quantity_solicitada_observada=quantity_solicitada,
            divergencia_quantity=divergencia,
            franquias_total=snap_atual.franquias_total,
            franquias_livres=snap_atual.franquias_disponiveis,
            vinculo_contratante_id=None,
            franquia_contratante_id=None,
            replay=True,
            motivo="replay_invoice",
        )

    ja_ativa = bool(conta.multiuser_ativa)
    renovacao = ja_ativa

    exigir_cnpj_normalizado_valido(conta.cnpj)
    existente = buscar_conta_multiuser_ativa_por_cnpj(
        str(conta.cnpj), excluir_conta_id=int(conta.id)
    )
    if existente is not None:
        raise CnpjMultiuserDuplicadoError(
            "CNPJ já utilizado por outra Conta Multiusuário ativa."
        )

    persistir_quantidade_assentos_contratados(conta.id, qtd, commit=False)
    try:
        marcar_conta_multiuser_ativa(conta.id, exigir_cnpj=True, commit=False)
    except IntegrityError as exc:
        converter_ou_relancar_integrity_error(exc)

    db.session.flush()
    conta = db.session.get(Conta, int(conta.id))
    if conta is None:
        raise ValueError("Conta não encontrada após ativação.")

    limite_ref = obter_limite_referencia_plano_admin(
        CODIGO_MULTIUSER, exigir_configurado=False
    )
    materializar_franquias_faltantes(
        conta,
        qtd,
        limite_referencia=limite_ref,
        aplicar_limite_nas_existentes=True,
    )

    fanout = aplicar_fanout_ciclo_confirmado(
        conta.id,
        inicio_ciclo,
        fim_ciclo,
        resetar_consumo=True,
    )
    if fanout.ignorado:
        raise PeriodoFinanceiroInvalidoError(
            "Fan-out recusado: período Stripe cronologicamente inválido."
        )

    vinculo_id = None
    franquia_contratante_id = None
    if user_id is not None:
        user = db.session.get(User, int(user_id))
        if user is None:
            raise ValueError("User contratante não encontrado.")
        if int(user.conta_id) != int(conta.id):
            raise VinculoInconsistenteError(
                "User autenticado não pertence à Conta da contratação."
            )
        vinculo_row = ocupar_assento(
            conta_id=conta.id,
            user_id=int(user.id),
            papel=PAPEL_CONTRATANTE,
            origem=ORIGEM_CONTRATACAO_STRIPE,
            limite_referencia=limite_ref,
            commit=False,
        )
        vinculo_id = int(vinculo_row.id)
        franquia_contratante_id = int(vinculo_row.franquia_id)
        user.categoria = CODIGO_MULTIUSER
        user.conta_id = conta.id
        user.franquia_id = vinculo_row.franquia_id
        db.session.add(user)

    if intencao_id is not None:
        from app.models import ContaMonetizacaoCheckoutIntencao
        from app.services.conta_multiuser_checkout_intencao_service import (
            consumir_intencao_checkout,
        )

        intencao = db.session.get(ContaMonetizacaoCheckoutIntencao, int(intencao_id))
        if intencao is None:
            raise ValueError("Intenção de Checkout não encontrada para consumo.")
        consumir_intencao_checkout(intencao)

    if fato_reserva is not None:
        fato_reserva.franquia_id = franquia_contratante_id
        fato_reserva.snapshot_normalizado_json = json.dumps(
            {
                "fase": "aplicado",
                "quantity_confirmada": qtd,
                "quantity_solicitada": quantity_solicitada,
                "divergencia_quantity": bool(divergencia),
                "renovacao": bool(renovacao),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        db.session.add(fato_reserva)

    if commit:
        db.session.commit()
    else:
        db.session.flush()

    snap = snapshot_capacidade(conta.id)
    logger.info(
        "Benefício Multiuser confirmado conta_id=%s quantity=%s renovacao=%s",
        conta.id,
        qtd,
        renovacao,
    )
    return ResultadoAtivacaoMultiuser(
        conta_id=int(conta.id),
        quantity_confirmada=qtd,
        quantity_solicitada_observada=quantity_solicitada,
        divergencia_quantity=bool(divergencia),
        franquias_total=snap.franquias_total,
        franquias_livres=snap.franquias_disponiveis,
        vinculo_contratante_id=vinculo_id,
        franquia_contratante_id=franquia_contratante_id,
        replay=False,
        consumo_resetado=True,
        motivo="renovacao" if renovacao else "ativacao_inicial",
        franquia_ids=list(fanout.franquia_ids),
    )

def contar_vinculos_ativos_conta(conta_id: int) -> int:
    return (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta_id),
            estado=ESTADO_ATIVO,
        )
        .count()
    )


def contar_vinculos_contratante_ativos(conta_id: int) -> int:
    return (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta_id),
            papel=PAPEL_CONTRATANTE,
            estado=ESTADO_ATIVO,
        )
        .count()
    )
