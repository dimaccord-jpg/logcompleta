"""
Exportacao CSV local de auditoria de clientes para o dashboard admin.
Somente leitura: nao altera plano, franquia, vinculos, consumo ou fatos.
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from collections import defaultdict
from typing import Any

from app.extensions import db
from app.models import ConfigRegras, ContaMonetizacaoVinculo, Franquia, MonetizacaoFato, User
from app.services.admin_dashboard_service import _apply_dashboard_filters
from app.services.cleiton_franquia_operacional_service import (
    classificar_estado_operacional_franquia,
)
from app.services.cleiton_monetizacao_service import STATUS_TEC_APLICADO
from app.services.cleiton_plano_resolver import resolver_plano_operacional_para_franquia

logger = logging.getLogger(__name__)

CSV_COLUMNS = [
    "user_id",
    "conta_id",
    "franquia_id",
    "email",
    "full_name",
    "user_created_at",
    "last_login_at",
    "is_admin",
    "usuario_cancelado",
    "user_categoria",
    "plano_usuario_legacy",
    "plano_contratual_vinculo",
    "plano_operacional_resolvido",
    "status_operacional_franquia",
    "fonte_verdade_operacional",
    "fonte_verdade_contratual",
    "user_creditos",
    "franquia_status",
    "bloqueio_manual",
    "limite_total",
    "consumo_acumulado",
    "saldo_disponivel",
    "inicio_ciclo",
    "fim_ciclo",
    "ciclo_vencido",
    "free_com_fim_ciclo_preenchido",
    "expired_sem_bloqueio_manual",
    "vinculo_id_ativo",
    "provider",
    "customer_id",
    "subscription_id",
    "price_id",
    "plano_interno",
    "status_contratual_externo",
    "vigencia_externa_inicio",
    "vigencia_externa_fim",
    "vinculo_ativo",
    "vinculo_canonico_ambiguo",
    "criterio_vinculo_exibido",
    "vinculo_confiabilidade",
    "vinculo_confiabilidade_conclusiva",
    "motivo_vinculo_confiabilidade",
    "vinculo_desativado_em",
    "vinculo_updated_at",
    "mudanca_pendente",
    "tipo_mudanca",
    "plano_futuro",
    "efetivar_em",
    "pendencias_snapshot",
    "confianca_ciclo",
    "fonte_evento",
    "qtd_vinculos_total_conta",
    "qtd_vinculos_ativos_conta",
    "qtd_customer_ids_distintos_conta",
    "qtd_subscription_ids_distintas_conta",
    "tem_vinculo_divergente_historico",
    "pendencia_em_vinculo_desativado",
    "pendencia_desativada_efetivar_em",
    "pendencia_desativada_plano_futuro",
    "pendencia_desativada_vencida",
    "pendencia_resolvida_por_fato_correlacionado",
    "pendencia_fato_correlacionado_tipo",
    "pendencia_fato_correlacionado_timestamp",
    "pendencia_correlacao_forca",
    "pendencia_janela_resolucao_horas",
    "ultimo_tipo_fato_monetizacao",
    "ultimo_status_tecnico_monetizacao",
    "ultimo_event_id",
    "ultimo_invoice_id",
    "ultimo_price_id",
    "ultimo_customer_id",
    "ultimo_subscription_id",
    "timestamp_ultimo_fato",
    "ultimo_fato_geral_tipo",
    "ultimo_fato_geral_status",
    "ultimo_fato_geral_timestamp",
    "ultimo_fato_efeito_tipo",
    "ultimo_fato_efeito_status",
    "ultimo_fato_efeito_event_id",
    "ultimo_fato_efeito_invoice_id",
    "ultimo_fato_efeito_price_id",
    "ultimo_fato_efeito_customer_id",
    "ultimo_fato_efeito_subscription_id",
    "ultimo_fato_efeito_timestamp",
    "ultimo_fato_relevante_tipo",
    "ultimo_fato_relevante_status",
    "ultimo_fato_relevante_customer_id",
    "ultimo_fato_relevante_subscription_id",
    "ultimo_fato_relevante_price_id",
    "ultimo_fato_relevante_event_id",
    "ultimo_fato_relevante_timestamp",
    "ultimo_fato_relevante_criterio",
    "status_operacional_recalculado",
    "motivo_status_recalculado",
    "price_id_esperado_plano_contratual",
    "plano_contratual_eh_pago",
    "price_id_configurado_encontrado",
    "flag_price_id_config_ausente",
    "flag_legacy_user_categoria_vs_vinculo",
    "observacao_legacy_categoria",
    "flag_plano_user_vs_vinculo",
    "flag_free_expired",
    "flag_free_com_fim_ciclo",
    "flag_pago_sem_subscription_ativa",
    "flag_pago_vinculo_inconclusivo",
    "flag_subscription_ativa_usuario_free",
    "flag_multiplo_customer",
    "flag_multipla_subscription",
    "flag_multiplos_customers_historico",
    "flag_multiplas_subscriptions_historico",
    "flag_multiplos_vinculos_ativos",
    "flag_pendencia_perdida",
    "flag_pendencia_perdida_vencida",
    "flag_consumo_maior_que_limite",
    "flag_bloqueio_manual",
    "flag_status_franquia_incompativel",
    "flag_status_persistido_diverge_recalculado",
    "status_divergencia_severidade",
    "flag_ids_entrelacados",
    "flag_price_id_incompativel_plano",
    "nivel_risco_auditoria",
    "flag_requer_revisao_manual",
]

ACTIVE_EXTERNAL_STATUS = {"active", "trialing", "paid", "past_due"}
PLANOS_PAGOS_FALLBACK = {"starter", "pro", "multiuser", "enterprise", "avulso"}
JANELA_RESOLUCAO_PENDENCIA_HORAS = 48
TIPOS_FATO_RELEVANTES_AUDITORIA = {
    "stripe_vinculo_guardrail_ids_inconsistentes",
    "stripe_vinculo_persistencia_bloqueada",
    "stripe_checkout_guardrail_mudanca_pendente",
    "cleiton_cron_pendencia_ausente_no_vinculo_ativo",
    "cleiton_downgrade_efetivado_virada_ciclo",
    "stripe_subscription_deleted_pendencia_limpa",
    "stripe_subscription_deleted_free_efetivado",
}
TIPOS_FATO_RESOLUCAO_PENDENCIA = {
    "cleiton_downgrade_efetivado_virada_ciclo",
    "stripe_subscription_deleted_pendencia_limpa",
    "stripe_subscription_deleted_free_efetivado",
}


def _parse_json_obj(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.isoformat()


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _norm_text(value: Any) -> str:
    return str(value or "").strip().lower()


def _csv_bool(value: bool) -> str:
    return "true" if bool(value) else "false"


def _norm_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _is_usuario_cancelado(email: str | None) -> bool:
    em = _norm_text(email)
    return em.startswith("encerrado") and em.endswith("@anon.local")


def _is_free_plan(*plans: str | None) -> bool:
    norm = {_norm_text(p) for p in plans if p is not None}
    return "free" in norm


def _parse_positive_money(value_real: Any, value_texto: str | None) -> bool:
    dec = _norm_decimal(value_real)
    if dec is not None:
        return dec > 0
    dec_txt = _norm_decimal(value_texto)
    return bool(dec_txt is not None and dec_txt > 0)


def _is_paid_plan(plan: str | None, paid_plans: set[str]) -> bool:
    return _norm_text(plan) in paid_plans


def _vinculo_consistente_pago(
    vinculo: ContaMonetizacaoVinculo | None, *, paid_plans: set[str]
) -> bool:
    if vinculo is None or not bool(vinculo.ativo):
        return False
    if not (vinculo.subscription_id and vinculo.customer_id):
        return False
    if not _is_paid_plan(vinculo.plano_interno, paid_plans):
        return False
    return _norm_text(vinculo.status_contratual_externo) in ACTIVE_EXTERNAL_STATUS


def _serialize_snapshot_list(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _obter_mapa_price_ids_plano() -> dict[str, str]:
    rows = (
        db.session.query(ConfigRegras)
        .filter(ConfigRegras.chave.like("plano_gateway_price_id_admin_%"))
        .all()
    )
    out: dict[str, str] = {}
    for row in rows:
        chave = row.chave or ""
        plano = chave.replace("plano_gateway_price_id_admin_", "").strip().lower()
        price = (row.valor_texto or "").strip()
        if plano and price:
            out[plano] = price
    return out


def _obter_planos_pagos_dinamicos(price_id_por_plano: dict[str, str]) -> set[str]:
    rows = (
        db.session.query(ConfigRegras)
        .filter(ConfigRegras.chave.like("plano_valor_admin_%"))
        .all()
    )
    planos: set[str] = set()
    for row in rows:
        chave = row.chave or ""
        plano = chave.replace("plano_valor_admin_", "").strip().lower()
        if not plano:
            continue
        if _parse_positive_money(getattr(row, "valor_real", None), row.valor_texto):
            planos.add(plano)
    planos.update({p for p in price_id_por_plano.keys() if p})
    if not planos:
        return set(PLANOS_PAGOS_FALLBACK)
    return planos


def _is_fato_relevante(fato: MonetizacaoFato) -> bool:
    if fato.status_tecnico == STATUS_TEC_APLICADO:
        return True
    tipo = _norm_text(fato.tipo_fato)
    return tipo in TIPOS_FATO_RELEVANTES_AUDITORIA


def _criterio_fato_relevante(fato: MonetizacaoFato | None) -> str:
    if fato is None:
        return ""
    if fato.status_tecnico == STATUS_TEC_APLICADO:
        return "status_aplicado"
    if _norm_text(fato.tipo_fato) in TIPOS_FATO_RELEVANTES_AUDITORIA:
        return "tipo_explicito"
    return ""


def _is_fato_efetivacao_pendencia(fato: MonetizacaoFato) -> bool:
    tipo = _norm_text(fato.tipo_fato)
    if tipo in TIPOS_FATO_RESOLUCAO_PENDENCIA:
        return True
    if fato.status_tecnico == STATUS_TEC_APLICADO:
        return True
    return False


def _correlacionar_fato_pendencia(
    fatos_conta: list[MonetizacaoFato],
    *,
    efetivar_em: datetime | None,
    customer_id_ref: str | None,
    subscription_id_ref: str | None,
    plano_futuro: str | None,
    price_to_plan: dict[str, str],
) -> tuple[MonetizacaoFato | None, str]:
    if efetivar_em is None:
        return None, "ausente"
    customer_ref_n = _norm_text(customer_id_ref)
    subscription_ref_n = _norm_text(subscription_id_ref)
    plano_futuro_n = _norm_text(plano_futuro)
    janela_fim = efetivar_em + timedelta(hours=JANELA_RESOLUCAO_PENDENCIA_HORAS)
    candidato_fraco: MonetizacaoFato | None = None
    for fato in fatos_conta:
        if not _is_fato_efetivacao_pendencia(fato):
            continue
        ts = fato.timestamp_interno
        if ts is None or ts < efetivar_em or ts > janela_fim:
            continue
        tipo_n = _norm_text(fato.tipo_fato)
        tipo_resolucao_explicito = tipo_n in TIPOS_FATO_RESOLUCAO_PENDENCIA
        if subscription_ref_n:
            sub_fato_n = _norm_text(fato.subscription_id)
            if (not sub_fato_n) or sub_fato_n != subscription_ref_n:
                continue
        if customer_ref_n:
            cus_fato_n = _norm_text(fato.customer_id)
            if (not cus_fato_n) or cus_fato_n != customer_ref_n:
                continue
        plano_info_disponivel = False
        plano_compat = True
        if plano_futuro_n:
            plano_por_price = _norm_text(price_to_plan.get((fato.price_id or "").strip()))
            plano_info_disponivel = bool(plano_por_price)
            if plano_futuro_n == "free":
                if (
                    plano_por_price not in ("", "free")
                    and "subscription_deleted" not in tipo_n
                ):
                    plano_compat = False
            elif plano_por_price and plano_por_price != plano_futuro_n:
                plano_compat = False
        if not plano_compat:
            continue
        if tipo_resolucao_explicito:
            return fato, "forte"
        if fato.status_tecnico == STATUS_TEC_APLICADO and (
            not plano_futuro_n or plano_info_disponivel
        ):
            return fato, "forte"
        if candidato_fraco is None:
            candidato_fraco = fato
    if candidato_fraco is not None:
        return candidato_fraco, "fraca"
    return None, "ausente"


def gerar_csv_auditoria_clientes(filtros: dict[str, str | None]) -> tuple[str, int]:
    categoria = (filtros.get("categoria") or "").strip() or None
    franquia_status = (filtros.get("franquia_status") or "").strip() or None
    cancelado = (filtros.get("cancelado") or "ativos").strip().lower()

    query = User.query.join(Franquia, User.franquia_id == Franquia.id)
    query = _apply_dashboard_filters(
        query,
        categoria=categoria,
        franquia_status=franquia_status,
        cancelado=cancelado,
    )
    usuarios = query.order_by(User.id.asc()).all()
    conta_ids = sorted({int(u.conta_id) for u in usuarios if u.conta_id is not None})

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=CSV_COLUMNS)
    writer.writeheader()

    plano_cache: dict[int, Any] = {}
    price_id_por_plano = _obter_mapa_price_ids_plano()
    paid_plans = _obter_planos_pagos_dinamicos(price_id_por_plano)
    plano_por_price_id = {v: k for k, v in price_id_por_plano.items() if v}

    vinculos_por_conta: dict[int, list[ContaMonetizacaoVinculo]] = defaultdict(list)
    if conta_ids:
        vinculos_lote = (
            ContaMonetizacaoVinculo.query.filter(
                ContaMonetizacaoVinculo.conta_id.in_(conta_ids)
            )
            .order_by(
                ContaMonetizacaoVinculo.conta_id.asc(),
                ContaMonetizacaoVinculo.updated_at.desc(),
                ContaMonetizacaoVinculo.created_at.desc(),
                ContaMonetizacaoVinculo.id.desc(),
            )
            .all()
        )
        for v in vinculos_lote:
            vinculos_por_conta[int(v.conta_id)].append(v)

    fatos_por_conta: dict[int, list[MonetizacaoFato]] = defaultdict(list)
    if conta_ids:
        fatos_lote = (
            MonetizacaoFato.query.filter(MonetizacaoFato.conta_id.in_(conta_ids))
            .order_by(
                MonetizacaoFato.conta_id.asc(),
                MonetizacaoFato.timestamp_interno.desc(),
                MonetizacaoFato.id.desc(),
            )
            .all()
        )
        for f in fatos_lote:
            fatos_por_conta[int(f.conta_id)].append(f)

    for user in usuarios:
        franquia = user.franquia
        if franquia is None:
            continue
        conta_id = int(user.conta_id)

        if franquia.id not in plano_cache:
            try:
                plano_cache[int(franquia.id)] = resolver_plano_operacional_para_franquia(
                    int(franquia.id)
                )
            except Exception:
                logger.exception(
                    "Falha ao resolver plano operacional; fallback user.categoria franquia_id=%s",
                    franquia.id,
                )
                plano_cache[int(franquia.id)] = type(
                    "PlanoFallback", (), {"codigo": (user.categoria or "free")}
                )()
        plano_resolvido = plano_cache[int(franquia.id)]
        plano_operacional = plano_resolvido.codigo

        vinculos = vinculos_por_conta.get(conta_id, [])
        ativos = [v for v in vinculos if bool(v.ativo)]
        if len(ativos) == 1:
            vinculo_exibido = ativos[0]
            vinculo_canonico_ambiguo = False
            criterio_vinculo_exibido = "ativo_unico"
        elif len(ativos) > 1:
            vinculo_exibido = ativos[0]
            vinculo_canonico_ambiguo = True
            criterio_vinculo_exibido = "multiplos_ativos_mais_recente_para_exibicao"
        else:
            vinculo_exibido = None
            vinculo_canonico_ambiguo = False
            criterio_vinculo_exibido = "sem_ativo"

        fatos_conta = fatos_por_conta.get(conta_id, [])
        ultimo_fato = fatos_conta[0] if fatos_conta else None
        ultimo_fato_efeito = next(
            (f for f in fatos_conta if f.status_tecnico == STATUS_TEC_APLICADO), None
        )
        ultimo_fato_relevante = next((f for f in fatos_conta if _is_fato_relevante(f)), None)

        snapshot_ativo = _parse_json_obj(
            vinculo_exibido.snapshot_normalizado_json if vinculo_exibido else None
        )
        mudanca_pendente_ativa = bool(snapshot_ativo.get("mudanca_pendente"))
        customer_ids = {
            _norm_text(v.customer_id)
            for v in vinculos
            if (v.customer_id or "").strip()
        }
        subscription_ids = {
            _norm_text(v.subscription_id)
            for v in vinculos
            if (v.subscription_id or "").strip()
        }

        pendencia_em_desativado = False
        pendencia_desativada_efetivar_em_dt: datetime | None = None
        pendencia_desativada_efetivar_em = ""
        pendencia_desativada_plano_futuro = ""
        pendencia_desativada_customer_id = ""
        pendencia_desativada_subscription_id = ""
        for v in vinculos:
            if bool(v.ativo):
                continue
            snap = _parse_json_obj(v.snapshot_normalizado_json)
            if bool(snap.get("mudanca_pendente")):
                pendencia_em_desativado = True
                pendencia_desativada_efetivar_em_dt = _parse_datetime(
                    snap.get("efetivar_em")
                )
                pendencia_desativada_efetivar_em = (
                    _fmt_datetime(pendencia_desativada_efetivar_em_dt)
                    if pendencia_desativada_efetivar_em_dt is not None
                    else ""
                )
                pendencia_desativada_plano_futuro = str(snap.get("plano_futuro") or "")
                pendencia_desativada_customer_id = v.customer_id or ""
                pendencia_desativada_subscription_id = v.subscription_id or ""
                break

        user_categoria = user.categoria or ""
        franquia_status_norm = _norm_text(franquia.status)
        limite_total = _norm_decimal(franquia.limite_total)
        consumo_acumulado = _norm_decimal(franquia.consumo_acumulado)
        saldo_disponivel = (
            limite_total - consumo_acumulado
            if limite_total is not None and consumo_acumulado is not None
            else None
        )

        fim_ciclo = franquia.fim_ciclo
        ciclo_vencido = bool(fim_ciclo and _utcnow_naive() > fim_ciclo)
        free_com_fim_ciclo = _is_free_plan(user_categoria, plano_operacional) and bool(
            fim_ciclo
        )
        expired_sem_bloqueio_manual = (
            franquia_status_norm == "expired" and not bool(franquia.bloqueio_manual)
        )

        plano_contratual_vinculo = (
            (vinculo_exibido.plano_interno or "") if vinculo_exibido else ""
        )
        flag_legacy_user_categoria_vs_vinculo = bool(
            vinculo_exibido
            and (vinculo_exibido.plano_interno or "").strip()
            and _norm_text(vinculo_exibido.plano_interno) != _norm_text(user_categoria)
            and not (
                _is_usuario_cancelado(user.email)
                and _is_free_plan(user_categoria, vinculo_exibido.plano_interno)
            )
        )
        flag_free_expired = _is_free_plan(user_categoria, plano_operacional) and (
            franquia_status_norm == "expired"
        )
        flag_subscription_ativa_usuario_free = _is_free_plan(
            user_categoria, plano_operacional
        ) and bool(_vinculo_consistente_pago(vinculo_exibido, paid_plans=paid_plans))
        flag_multiplos_customers_historico = len(customer_ids) > 1
        flag_multiplas_subscriptions_historico = len(subscription_ids) > 1
        flag_multiplos_vinculos_ativos = len(ativos) > 1
        flag_multiplo_customer = flag_multiplos_customers_historico
        flag_multipla_subscription = flag_multiplas_subscriptions_historico

        fato_correlacionado_pendencia, pendencia_correlacao_forca = _correlacionar_fato_pendencia(
            fatos_conta,
            efetivar_em=pendencia_desativada_efetivar_em_dt,
            customer_id_ref=pendencia_desativada_customer_id,
            subscription_id_ref=pendencia_desativada_subscription_id,
            plano_futuro=pendencia_desativada_plano_futuro,
            price_to_plan=plano_por_price_id,
        )
        pendencia_resolvida_por_fato_correlacionado = (
            pendencia_correlacao_forca == "forte"
        )
        pendencia_desativada_vencida = bool(
            pendencia_em_desativado
            and pendencia_desativada_efetivar_em_dt is not None
            and pendencia_desativada_efetivar_em_dt < _utcnow_naive()
            and not mudanca_pendente_ativa
            and not pendencia_resolvida_por_fato_correlacionado
        )
        flag_pendencia_perdida = pendencia_em_desativado and not mudanca_pendente_ativa
        flag_pendencia_perdida_vencida = pendencia_desativada_vencida
        flag_consumo_maior_que_limite = bool(
            limite_total is not None
            and consumo_acumulado is not None
            and consumo_acumulado > limite_total
        )
        flag_bloqueio_manual = bool(franquia.bloqueio_manual)
        try:
            status_recalculado, motivo_recalculado = classificar_estado_operacional_franquia(
                franquia, plano_resolvido
            )
        except Exception:
            logger.exception(
                "Falha no recalculo de status operacional franquia_id=%s", franquia.id
            )
            status_recalculado, motivo_recalculado = (franquia.status or ""), "erro_recalculo"
        flag_status_persistido_diverge_recalculado = (
            _norm_text(franquia.status) != _norm_text(status_recalculado)
        )
        flag_status_franquia_incompativel = flag_status_persistido_diverge_recalculado

        mismatch_customer = bool(
            ultimo_fato_relevante
            and vinculo_exibido
            and (ultimo_fato_relevante.customer_id or "").strip()
            and (vinculo_exibido.customer_id or "").strip()
            and _norm_text(ultimo_fato_relevante.customer_id)
            != _norm_text(vinculo_exibido.customer_id)
        )
        mismatch_subscription = bool(
            ultimo_fato_relevante
            and vinculo_exibido
            and (ultimo_fato_relevante.subscription_id or "").strip()
            and (vinculo_exibido.subscription_id or "").strip()
            and _norm_text(ultimo_fato_relevante.subscription_id)
            != _norm_text(vinculo_exibido.subscription_id)
        )
        flag_ids_entrelacados = bool(mismatch_customer or mismatch_subscription)

        plano_contratual_n = _norm_text(plano_contratual_vinculo)
        plano_contratual_eh_pago = _is_paid_plan(plano_contratual_vinculo, paid_plans)
        price_id_configurado_encontrado = bool(
            plano_contratual_eh_pago
            and bool(price_id_por_plano.get(plano_contratual_n, ""))
        )
        price_id_esperado_plano_contratual = (
            price_id_por_plano.get(plano_contratual_n, "")
            if plano_contratual_eh_pago
            else ""
        )
        flag_price_id_config_ausente = bool(
            plano_contratual_eh_pago and not price_id_configurado_encontrado
        )
        flag_price_id_incompativel_plano = bool(
            vinculo_exibido
            and plano_contratual_eh_pago
            and price_id_esperado_plano_contratual
            and (vinculo_exibido.price_id or "").strip()
            and (vinculo_exibido.price_id or "").strip()
            != price_id_esperado_plano_contratual
        )

        if len(ativos) == 0:
            vinculo_confiabilidade = "ausente"
            motivo_vinculo_confiabilidade = "sem_vinculo_ativo"
        elif len(ativos) > 1:
            vinculo_confiabilidade = "ambiguo"
            motivo_vinculo_confiabilidade = "multiplos_vinculos_ativos"
        else:
            historico_ids_multiplos = bool(
                flag_multiplos_customers_historico or flag_multiplas_subscriptions_historico
            )
            if historico_ids_multiplos:
                vinculo_confiabilidade = "inconclusivo"
                motivo_vinculo_confiabilidade = "historico_ids_multiplos_requer_revisao"
            elif flag_ids_entrelacados:
                vinculo_confiabilidade = "inconclusivo"
                motivo_vinculo_confiabilidade = "ultimo_fato_relevante_diverge_do_vinculo"
            elif flag_pendencia_perdida:
                vinculo_confiabilidade = "inconclusivo"
                motivo_vinculo_confiabilidade = "pendencia_perdida_sem_resolucao"
            elif plano_contratual_eh_pago and (
                flag_price_id_config_ausente or flag_price_id_incompativel_plano
            ):
                vinculo_confiabilidade = "inconclusivo"
                motivo_vinculo_confiabilidade = "price_id_configuracao_inconsistente"
            else:
                vinculo_confiabilidade = "confiavel"
                motivo_vinculo_confiabilidade = "ativo_unico_coerente"

        vinculo_confiabilidade_conclusiva = (
            vinculo_confiabilidade == "confiavel"
            and not flag_multiplos_customers_historico
            and not flag_multiplas_subscriptions_historico
            and not flag_ids_entrelacados
            and not flag_pendencia_perdida
            and (
                not plano_contratual_eh_pago
                or (
                    price_id_configurado_encontrado
                    and not flag_price_id_incompativel_plano
                )
            )
        )

        flag_pago_vinculo_inconclusivo = bool(
            plano_contratual_eh_pago and vinculo_confiabilidade != "confiavel"
        )
        flag_pago_sem_subscription_ativa = (
            (plano_contratual_eh_pago or _is_paid_plan(plano_operacional, paid_plans))
            and not (
                _vinculo_consistente_pago(vinculo_exibido, paid_plans=paid_plans)
                and vinculo_confiabilidade == "confiavel"
                and (not plano_contratual_eh_pago or price_id_configurado_encontrado)
                and not flag_price_id_incompativel_plano
                and not flag_price_id_config_ausente
            )
        )
        observacao_legacy_categoria = (
            "Divergencia legacy entre User.categoria e vinculo; nao usar isoladamente como prova financeira"
            if flag_legacy_user_categoria_vs_vinculo
            else ""
        )

        evidencias_fortes_status = any(
            [
                (free_com_fim_ciclo and franquia_status_norm == "expired"),
                flag_consumo_maior_que_limite,
                flag_pago_sem_subscription_ativa,
                flag_subscription_ativa_usuario_free,
                vinculo_confiabilidade in {"ambiguo", "inconclusivo"},
                flag_price_id_incompativel_plano,
            ]
        )
        if not flag_status_persistido_diverge_recalculado:
            status_divergencia_severidade = "nenhuma"
        elif evidencias_fortes_status:
            status_divergencia_severidade = "crítico"
        else:
            status_divergencia_severidade = "atenção"

        criticos = any(
            [
                flag_multiplos_vinculos_ativos,
                vinculo_confiabilidade == "ambiguo",
                (vinculo_confiabilidade == "inconclusivo" and flag_ids_entrelacados),
                flag_pago_sem_subscription_ativa,
                flag_pago_vinculo_inconclusivo,
                flag_subscription_ativa_usuario_free,
                flag_price_id_incompativel_plano,
                (flag_pendencia_perdida_vencida and not pendencia_resolvida_por_fato_correlacionado),
                (free_com_fim_ciclo and franquia_status_norm == "expired"),
                (flag_ids_entrelacados and bool(ultimo_fato_relevante)),
                status_divergencia_severidade == "crítico",
            ]
        )
        atencao = any(
            [
                flag_legacy_user_categoria_vs_vinculo,
                flag_free_expired,
                free_com_fim_ciclo,
                flag_multiplos_customers_historico,
                flag_multiplas_subscriptions_historico,
                flag_pendencia_perdida,
                flag_price_id_config_ausente,
                flag_consumo_maior_que_limite,
                flag_bloqueio_manual,
                status_divergencia_severidade == "atenção",
            ]
        )
        nivel_risco_auditoria = "crítico" if criticos else ("atenção" if atencao else "ok")
        flag_requer_revisao_manual = criticos or atencao

        writer.writerow(
            {
                "user_id": user.id,
                "conta_id": conta_id,
                "franquia_id": user.franquia_id,
                "email": user.email or "",
                "full_name": user.full_name or "",
                "user_created_at": _fmt_datetime(user.created_at),
                "last_login_at": _fmt_datetime(user.last_login_at),
                "is_admin": _csv_bool(bool(user.is_admin)),
                "usuario_cancelado": _csv_bool(_is_usuario_cancelado(user.email)),
                "user_categoria": user_categoria,
                "plano_usuario_legacy": user_categoria,
                "plano_contratual_vinculo": plano_contratual_vinculo,
                "plano_operacional_resolvido": plano_operacional,
                "status_operacional_franquia": franquia.status or "",
                "fonte_verdade_operacional": "franquia_cleiton",
                "fonte_verdade_contratual": "conta_monetizacao_vinculo",
                "user_creditos": user.creditos if user.creditos is not None else "",
                "franquia_status": franquia.status or "",
                "bloqueio_manual": _csv_bool(bool(franquia.bloqueio_manual)),
                "limite_total": str(limite_total) if limite_total is not None else "",
                "consumo_acumulado": (
                    str(consumo_acumulado) if consumo_acumulado is not None else ""
                ),
                "saldo_disponivel": (
                    str(saldo_disponivel) if saldo_disponivel is not None else ""
                ),
                "inicio_ciclo": _fmt_datetime(franquia.inicio_ciclo),
                "fim_ciclo": _fmt_datetime(franquia.fim_ciclo),
                "ciclo_vencido": _csv_bool(ciclo_vencido),
                "free_com_fim_ciclo_preenchido": _csv_bool(free_com_fim_ciclo),
                "expired_sem_bloqueio_manual": _csv_bool(expired_sem_bloqueio_manual),
                "vinculo_id_ativo": vinculo_exibido.id if vinculo_exibido else "",
                "provider": vinculo_exibido.provider if vinculo_exibido else "",
                "customer_id": vinculo_exibido.customer_id if vinculo_exibido else "",
                "subscription_id": (
                    vinculo_exibido.subscription_id if vinculo_exibido else ""
                ),
                "price_id": vinculo_exibido.price_id if vinculo_exibido else "",
                "plano_interno": vinculo_exibido.plano_interno if vinculo_exibido else "",
                "status_contratual_externo": (
                    vinculo_exibido.status_contratual_externo if vinculo_exibido else ""
                ),
                "vigencia_externa_inicio": _fmt_datetime(
                    vinculo_exibido.vigencia_externa_inicio if vinculo_exibido else None
                ),
                "vigencia_externa_fim": _fmt_datetime(
                    vinculo_exibido.vigencia_externa_fim if vinculo_exibido else None
                ),
                "vinculo_ativo": _csv_bool(bool(vinculo_exibido and vinculo_exibido.ativo)),
                "vinculo_canonico_ambiguo": _csv_bool(vinculo_canonico_ambiguo),
                "criterio_vinculo_exibido": criterio_vinculo_exibido,
                "vinculo_confiabilidade": vinculo_confiabilidade,
                "vinculo_confiabilidade_conclusiva": _csv_bool(
                    vinculo_confiabilidade_conclusiva
                ),
                "motivo_vinculo_confiabilidade": motivo_vinculo_confiabilidade,
                "vinculo_desativado_em": _fmt_datetime(
                    vinculo_exibido.desativado_em if vinculo_exibido else None
                ),
                "vinculo_updated_at": _fmt_datetime(
                    vinculo_exibido.updated_at if vinculo_exibido else None
                ),
                "mudanca_pendente": _csv_bool(mudanca_pendente_ativa),
                "tipo_mudanca": snapshot_ativo.get("tipo_mudanca", "") or "",
                "plano_futuro": snapshot_ativo.get("plano_futuro", "") or "",
                "efetivar_em": str(snapshot_ativo.get("efetivar_em", "") or ""),
                "pendencias_snapshot": _serialize_snapshot_list(
                    snapshot_ativo.get("pendencias")
                ),
                "confianca_ciclo": str(snapshot_ativo.get("confianca_ciclo", "") or ""),
                "fonte_evento": str(snapshot_ativo.get("fonte_evento", "") or ""),
                "qtd_vinculos_total_conta": len(vinculos),
                "qtd_vinculos_ativos_conta": len(ativos),
                "qtd_customer_ids_distintos_conta": len(customer_ids),
                "qtd_subscription_ids_distintas_conta": len(subscription_ids),
                "tem_vinculo_divergente_historico": _csv_bool(
                    len(ativos) > 1
                    or flag_multiplos_customers_historico
                    or flag_multiplas_subscriptions_historico
                ),
                "pendencia_em_vinculo_desativado": _csv_bool(pendencia_em_desativado),
                "pendencia_desativada_efetivar_em": pendencia_desativada_efetivar_em,
                "pendencia_desativada_plano_futuro": pendencia_desativada_plano_futuro,
                "pendencia_desativada_vencida": _csv_bool(pendencia_desativada_vencida),
                "pendencia_resolvida_por_fato_correlacionado": _csv_bool(
                    pendencia_resolvida_por_fato_correlacionado
                ),
                "pendencia_fato_correlacionado_tipo": (
                    (fato_correlacionado_pendencia.tipo_fato or "")
                    if fato_correlacionado_pendencia
                    else ""
                ),
                "pendencia_fato_correlacionado_timestamp": _fmt_datetime(
                    fato_correlacionado_pendencia.timestamp_interno
                    if fato_correlacionado_pendencia
                    else None
                ),
                "pendencia_correlacao_forca": pendencia_correlacao_forca,
                "pendencia_janela_resolucao_horas": JANELA_RESOLUCAO_PENDENCIA_HORAS,
                "ultimo_tipo_fato_monetizacao": (
                    (ultimo_fato.tipo_fato or "") if ultimo_fato else ""
                ),
                "ultimo_status_tecnico_monetizacao": (
                    (ultimo_fato.status_tecnico or "") if ultimo_fato else ""
                ),
                "ultimo_event_id": (
                    (ultimo_fato.external_event_id or "") if ultimo_fato else ""
                ),
                "ultimo_invoice_id": (ultimo_fato.invoice_id or "") if ultimo_fato else "",
                "ultimo_price_id": (ultimo_fato.price_id or "") if ultimo_fato else "",
                "ultimo_customer_id": (
                    (ultimo_fato.customer_id or "") if ultimo_fato else ""
                ),
                "ultimo_subscription_id": (
                    (ultimo_fato.subscription_id or "") if ultimo_fato else ""
                ),
                "timestamp_ultimo_fato": _fmt_datetime(
                    ultimo_fato.timestamp_interno if ultimo_fato else None
                ),
                "ultimo_fato_geral_tipo": (ultimo_fato.tipo_fato or "") if ultimo_fato else "",
                "ultimo_fato_geral_status": (
                    (ultimo_fato.status_tecnico or "") if ultimo_fato else ""
                ),
                "ultimo_fato_geral_timestamp": _fmt_datetime(
                    ultimo_fato.timestamp_interno if ultimo_fato else None
                ),
                "ultimo_fato_efeito_tipo": (
                    (ultimo_fato_efeito.tipo_fato or "") if ultimo_fato_efeito else ""
                ),
                "ultimo_fato_efeito_status": (
                    (ultimo_fato_efeito.status_tecnico or "")
                    if ultimo_fato_efeito
                    else ""
                ),
                "ultimo_fato_efeito_event_id": (
                    (ultimo_fato_efeito.external_event_id or "")
                    if ultimo_fato_efeito
                    else ""
                ),
                "ultimo_fato_efeito_invoice_id": (
                    (ultimo_fato_efeito.invoice_id or "") if ultimo_fato_efeito else ""
                ),
                "ultimo_fato_efeito_price_id": (
                    (ultimo_fato_efeito.price_id or "") if ultimo_fato_efeito else ""
                ),
                "ultimo_fato_efeito_customer_id": (
                    (ultimo_fato_efeito.customer_id or "")
                    if ultimo_fato_efeito
                    else ""
                ),
                "ultimo_fato_efeito_subscription_id": (
                    (ultimo_fato_efeito.subscription_id or "")
                    if ultimo_fato_efeito
                    else ""
                ),
                "ultimo_fato_efeito_timestamp": _fmt_datetime(
                    ultimo_fato_efeito.timestamp_interno if ultimo_fato_efeito else None
                ),
                "ultimo_fato_relevante_tipo": (
                    (ultimo_fato_relevante.tipo_fato or "") if ultimo_fato_relevante else ""
                ),
                "ultimo_fato_relevante_status": (
                    (ultimo_fato_relevante.status_tecnico or "")
                    if ultimo_fato_relevante
                    else ""
                ),
                "ultimo_fato_relevante_customer_id": (
                    (ultimo_fato_relevante.customer_id or "")
                    if ultimo_fato_relevante
                    else ""
                ),
                "ultimo_fato_relevante_subscription_id": (
                    (ultimo_fato_relevante.subscription_id or "")
                    if ultimo_fato_relevante
                    else ""
                ),
                "ultimo_fato_relevante_price_id": (
                    (ultimo_fato_relevante.price_id or "")
                    if ultimo_fato_relevante
                    else ""
                ),
                "ultimo_fato_relevante_event_id": (
                    (ultimo_fato_relevante.external_event_id or "")
                    if ultimo_fato_relevante
                    else ""
                ),
                "ultimo_fato_relevante_timestamp": _fmt_datetime(
                    ultimo_fato_relevante.timestamp_interno
                    if ultimo_fato_relevante
                    else None
                ),
                "ultimo_fato_relevante_criterio": _criterio_fato_relevante(
                    ultimo_fato_relevante
                ),
                "status_operacional_recalculado": status_recalculado or "",
                "motivo_status_recalculado": motivo_recalculado or "",
                "price_id_esperado_plano_contratual": price_id_esperado_plano_contratual,
                "plano_contratual_eh_pago": _csv_bool(plano_contratual_eh_pago),
                "price_id_configurado_encontrado": _csv_bool(price_id_configurado_encontrado),
                "flag_price_id_config_ausente": _csv_bool(flag_price_id_config_ausente),
                "flag_legacy_user_categoria_vs_vinculo": _csv_bool(
                    flag_legacy_user_categoria_vs_vinculo
                ),
                "observacao_legacy_categoria": observacao_legacy_categoria,
                "flag_plano_user_vs_vinculo": _csv_bool(
                    flag_legacy_user_categoria_vs_vinculo
                ),
                "flag_free_expired": _csv_bool(flag_free_expired),
                "flag_free_com_fim_ciclo": _csv_bool(free_com_fim_ciclo),
                "flag_pago_sem_subscription_ativa": _csv_bool(
                    flag_pago_sem_subscription_ativa
                ),
                "flag_pago_vinculo_inconclusivo": _csv_bool(flag_pago_vinculo_inconclusivo),
                "flag_subscription_ativa_usuario_free": _csv_bool(
                    flag_subscription_ativa_usuario_free
                ),
                "flag_multiplo_customer": _csv_bool(flag_multiplo_customer),
                "flag_multipla_subscription": _csv_bool(flag_multipla_subscription),
                "flag_multiplos_customers_historico": _csv_bool(
                    flag_multiplos_customers_historico
                ),
                "flag_multiplas_subscriptions_historico": _csv_bool(
                    flag_multiplas_subscriptions_historico
                ),
                "flag_multiplos_vinculos_ativos": _csv_bool(flag_multiplos_vinculos_ativos),
                "flag_pendencia_perdida": _csv_bool(flag_pendencia_perdida),
                "flag_pendencia_perdida_vencida": _csv_bool(
                    flag_pendencia_perdida_vencida
                ),
                "flag_consumo_maior_que_limite": _csv_bool(
                    flag_consumo_maior_que_limite
                ),
                "flag_bloqueio_manual": _csv_bool(flag_bloqueio_manual),
                "flag_status_franquia_incompativel": _csv_bool(
                    flag_status_franquia_incompativel
                ),
                "flag_status_persistido_diverge_recalculado": _csv_bool(
                    flag_status_persistido_diverge_recalculado
                ),
                "status_divergencia_severidade": status_divergencia_severidade,
                "flag_ids_entrelacados": _csv_bool(flag_ids_entrelacados),
                "flag_price_id_incompativel_plano": _csv_bool(
                    flag_price_id_incompativel_plano
                ),
                "nivel_risco_auditoria": nivel_risco_auditoria,
                "flag_requer_revisao_manual": _csv_bool(flag_requer_revisao_manual),
            }
        )

    return out.getvalue(), len(usuarios)
