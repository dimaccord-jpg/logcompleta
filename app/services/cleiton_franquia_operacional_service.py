"""
Governança operacional Cleiton (Fase 2): reflexo do consumo técnico na entidade Franquia.

- Medições continuam nos registradores existentes (IaConsumoEvento, ProcessingEvent).
- Este módulo decide abatimento, converte uso técnico em créditos (régua CleitonCostConfig),
  atualiza consumo_acumulado e recalcula status operacional.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.consumo_identidade import TIPO_ORIGEM_HTTP_ANONIMO
from app.extensions import db
from app.models import (
    CleitonCostConfig,
    Franquia,
    IaConsumoEvento,
    ProcessingEvent,
    utcnow_naive,
)
from app.services.cleiton_cost_service import get_or_create_config
from app.services.cleiton_ciclo_franquia_service import garantir_ciclo_operacional_franquia
from app.services.cleiton_plano_resolver import (
    CODIGO_AVULSO,
    CODIGO_FREE,
    CODIGO_INTERNA,
    CODIGO_MULTIUSER,
    CODIGO_PRO,
    CODIGO_STARTER,
    CODIGO_UNKNOWN,
    PlanoResolvidoCleiton,
    resolver_plano_operacional_para_franquia,
)
from app.services.conta_franquia_service import get_sistema_interno_ids

logger = logging.getLogger(__name__)

Q6 = Decimal("0.000001")

IA_OK = ("success", "success_no_metrics")
PROC_OK = ("success",)


@dataclass(frozen=True)
class ResultadoGovernancaOperacional:
    abateu_franquia: bool
    creditos: Decimal | None
    motivo_nao_abateu: str | None
    franquia_id: int | None
    status_anterior: str | None
    status_novo: str | None
    erro_config: str | None


def _to_decimal(x: Any) -> Decimal:
    if x is None:
        return Decimal("0")
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _quantize_credit(d: Decimal) -> Decimal:
    return d.quantize(Q6, rounding=ROUND_HALF_UP)


def converter_tokens_para_creditos(tokens: int, cfg: CleitonCostConfig) -> tuple[Decimal | None, str | None]:
    rate = cfg.credit_tokens_per_credit
    if rate is None or rate <= 0:
        return None, "credit_tokens_per_credit ausente ou inválido na CleitonCostConfig"
    t = max(0, int(tokens))
    c = _quantize_credit(Decimal(t) / Decimal(str(rate)))
    return c, None


def converter_linhas_para_creditos(linhas: int, cfg: CleitonCostConfig) -> tuple[Decimal | None, str | None]:
    rate = cfg.credit_lines_per_credit
    if rate is None or rate <= 0:
        return None, "credit_lines_per_credit ausente ou inválido na CleitonCostConfig"
    n = max(0, int(linhas))
    c = _quantize_credit(Decimal(n) / Decimal(str(rate)))
    return c, None


def converter_ms_para_creditos(ms: int, cfg: CleitonCostConfig) -> tuple[Decimal | None, str | None]:
    rate = cfg.credit_ms_per_credit
    if rate is None or rate <= 0:
        return None, "credit_ms_per_credit ausente ou inválido na CleitonCostConfig"
    m = max(0, int(ms))
    c = _quantize_credit(Decimal(m) / Decimal(str(rate)))
    return c, None


def deve_abater_franquia_do_cliente(
    *,
    franquia_id: int | None,
    usuario_id: int | None,
    origem_sistema: bool | None,
    tipo_origem: str | None,
) -> tuple[bool, str | None]:
    """
    Consumo interno/sistema não abate da franquia do cliente.
    Requer identidade de operador (usuário) e franquia distinta da reserva sistema-interno.
    """
    if origem_sistema is True:
        return False, "origem_sistema"
    if usuario_id is None:
        return False, "sem_usuario_id"
    if franquia_id is None:
        return False, "sem_franquia_id"
    if tipo_origem == TIPO_ORIGEM_HTTP_ANONIMO:
        return False, "http_anonimo"
    _cid, sid = get_sistema_interno_ids()
    if sid is not None and int(franquia_id) == int(sid):
        return False, "franquia_reservada_sistema"
    return True, None


def ensure_franquia_operacional_inicializada(franquia: Franquia) -> None:
    """Defaults seguros para linhas legadas ou linhas parcialmente preenchidas."""
    if franquia.consumo_acumulado is None:
        franquia.consumo_acumulado = Decimal("0")


def _limite_efetivo_bloqueio(limite: Any) -> Decimal | None:
    if limite is None:
        return None
    d = _to_decimal(limite)
    if d <= 0:
        return None
    return d


def classificar_estado_operacional_franquia(
    fr: Franquia,
    plano: PlanoResolvidoCleiton,
    agora: datetime | None = None,
) -> tuple[str, str]:
    """
    Determina status e código de motivo (plano + vigência + limite + bloqueio manual).
    Interna: ignora limite e vigência comercial padrão; respeita apenas bloqueio manual.
    """
    agora = agora or utcnow_naive()
    if getattr(fr, "bloqueio_manual", False):
        return Franquia.STATUS_BLOCKED, "bloqueio_manual"

    if plano.codigo == CODIGO_INTERNA:
        return Franquia.STATUS_ACTIVE, "operacional_ok_interna"

    fim = fr.fim_ciclo
    if fim is not None and agora > fim:
        return Franquia.STATUS_EXPIRED, "vigencia_expirada"

    lim = _limite_efetivo_bloqueio(fr.limite_total)
    cons = _to_decimal(fr.consumo_acumulado)
    if lim is None or cons < lim:
        return Franquia.STATUS_ACTIVE, "operacional_ok"

    if plano.codigo == CODIGO_FREE:
        return Franquia.STATUS_BLOCKED, "limite_atingido_free"
    if plano.codigo in (CODIGO_STARTER, CODIGO_PRO, CODIGO_MULTIUSER):
        return Franquia.STATUS_DEGRADED, "limite_atingido_starter_pro"
    if plano.codigo == CODIGO_AVULSO:
        return Franquia.STATUS_EXPIRED, "limite_ou_vigencia_avulso"
    if plano.codigo == CODIGO_UNKNOWN:
        return Franquia.STATUS_BLOCKED, "limite_atingido_plano_indefinido"
    return Franquia.STATUS_BLOCKED, "limite_atingido_fallback"


def recalcular_status_operacional(franquia: Franquia, agora: datetime | None = None) -> str:
    plano = resolver_plano_operacional_para_franquia(franquia.id)
    st, _ = classificar_estado_operacional_franquia(franquia, plano, agora)
    return st


def aplicar_status_apos_mudanca_estrutural(franquia_id: int) -> None:
    """Após ciclo manual ou ajuste de bloqueio: garante ciclo e alinha status persistido."""
    fid = int(franquia_id)
    garantir_ciclo_operacional_franquia(fid)
    fr = db.session.get(Franquia, fid)
    if fr is None:
        return
    plano = resolver_plano_operacional_para_franquia(fid)
    st, _ = classificar_estado_operacional_franquia(fr, plano)
    if fr.status != st:
        fr.status = st
        db.session.add(fr)
        db.session.commit()


def creditos_totais_de_evento_ia(ev: IaConsumoEvento, cfg: CleitonCostConfig) -> tuple[Decimal | None, str | None]:
    if ev.status not in IA_OK:
        return Decimal("0"), None
    tot = ev.total_tokens
    if tot is not None and tot > 0:
        return converter_tokens_para_creditos(int(tot), cfg)
    inp = ev.input_tokens or 0
    out = ev.output_tokens or 0
    if inp <= 0 and out <= 0:
        return Decimal("0"), None
    return converter_tokens_para_creditos(int(inp + out), cfg)


def creditos_totais_de_evento_processing(ev: ProcessingEvent, cfg: CleitonCostConfig) -> tuple[Decimal | None, str | None]:
    if ev.status not in PROC_OK:
        return Decimal("0"), None
    total = Decimal("0")
    rows = int(ev.rows_processed or 0)
    ms = int(ev.processing_time_ms or 0)
    if rows > 0:
        c_linhas, err_l = converter_linhas_para_creditos(rows, cfg)
        if err_l:
            return None, err_l
        total += c_linhas or Decimal("0")
    if ms > 0:
        c_ms, err_m = converter_ms_para_creditos(ms, cfg)
        if err_m:
            return None, err_m
        total += c_ms or Decimal("0")
    return _quantize_credit(total), None


def aplicar_motor_apos_ia_consumo_evento(evento_id: int) -> ResultadoGovernancaOperacional:
    ev = db.session.get(IaConsumoEvento, evento_id)
    if ev is None:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=None,
            motivo_nao_abateu="evento_ia_inexistente",
            franquia_id=None,
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    pode, motivo = deve_abater_franquia_do_cliente(
        franquia_id=ev.franquia_id,
        usuario_id=ev.usuario_id,
        origem_sistema=ev.origem_sistema,
        tipo_origem=ev.tipo_origem,
    )
    if not pode:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=None,
            motivo_nao_abateu=motivo,
            franquia_id=ev.franquia_id,
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    cfg = get_or_create_config()
    creditos, err_conv = creditos_totais_de_evento_ia(ev, cfg)
    if err_conv:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=None,
            motivo_nao_abateu="falha_conversao_creditos",
            franquia_id=ev.franquia_id,
            status_anterior=None,
            status_novo=None,
            erro_config=err_conv,
        )
    if creditos is None or creditos <= 0:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=creditos or Decimal("0"),
            motivo_nao_abateu="creditos_zero",
            franquia_id=ev.franquia_id,
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    return _persistir_abatimento(ev.franquia_id, creditos)


def aplicar_motor_apos_processing_event(evento_id: int) -> ResultadoGovernancaOperacional:
    ev = db.session.get(ProcessingEvent, evento_id)
    if ev is None:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=None,
            motivo_nao_abateu="evento_processing_inexistente",
            franquia_id=None,
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    pode, motivo = deve_abater_franquia_do_cliente(
        franquia_id=ev.franquia_id,
        usuario_id=ev.usuario_id,
        origem_sistema=ev.origem_sistema,
        tipo_origem=ev.tipo_origem,
    )
    if not pode:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=None,
            motivo_nao_abateu=motivo,
            franquia_id=ev.franquia_id,
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    cfg = get_or_create_config()
    creditos, err_conv = creditos_totais_de_evento_processing(ev, cfg)
    if err_conv:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=None,
            motivo_nao_abateu="falha_conversao_creditos",
            franquia_id=ev.franquia_id,
            status_anterior=None,
            status_novo=None,
            erro_config=err_conv,
        )
    if creditos is None or creditos <= 0:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=creditos or Decimal("0"),
            motivo_nao_abateu="creditos_zero",
            franquia_id=ev.franquia_id,
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    return _persistir_abatimento(ev.franquia_id, creditos)


def _persistir_abatimento(franquia_id: int | None, creditos: Decimal) -> ResultadoGovernancaOperacional:
    if franquia_id is None:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=creditos,
            motivo_nao_abateu="sem_franquia_id",
            franquia_id=None,
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    fr = db.session.get(Franquia, int(franquia_id))
    if fr is None:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=creditos,
            motivo_nao_abateu="franquia_nao_encontrada",
            franquia_id=int(franquia_id),
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    ensure_franquia_operacional_inicializada(fr)
    garantir_ciclo_operacional_franquia(fr.id)
    fr = db.session.get(Franquia, int(franquia_id))
    if fr is None:
        return ResultadoGovernancaOperacional(
            abateu_franquia=False,
            creditos=creditos,
            motivo_nao_abateu="franquia_nao_encontrada_pos_ciclo",
            franquia_id=int(franquia_id),
            status_anterior=None,
            status_novo=None,
            erro_config=None,
        )
    st_antes = fr.status
    fr.consumo_acumulado = _quantize_credit(_to_decimal(fr.consumo_acumulado) + creditos)
    plano = resolver_plano_operacional_para_franquia(fr.id)
    st_depois = classificar_estado_operacional_franquia(fr, plano)[0]
    fr.status = st_depois
    db.session.add(fr)
    db.session.commit()
    return ResultadoGovernancaOperacional(
        abateu_franquia=True,
        creditos=creditos,
        motivo_nao_abateu=None,
        franquia_id=fr.id,
        status_anterior=st_antes,
        status_novo=st_depois,
        erro_config=None,
    )
