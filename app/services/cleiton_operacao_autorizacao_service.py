"""
Autorização operacional central por franquia (pré-consumo) no domínio Cleiton.

Fonte central para decisões de operação por status operacional de franquia.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.models import Franquia
from app.services.cleiton_mensageria_operacao_service import (
    montar_mensagem_operacao,
    montar_regularizacao_cta_operacao,
    montar_upgrade_cta_operacao,
    status_exibe_cta_upgrade,
)
from app.services.cleiton_franquia_leitura_service import (
    ler_franquia_operacional_cleiton,
)

MODO_OPERACAO_NORMAL = "normal"
MODO_OPERACAO_DEGRADED = "degraded"
MODO_OPERACAO_BLOCKED = "blocked"


@dataclass(frozen=True)
class DecisaoOperacaoFranquia:
    permitido: bool
    status_franquia: str
    modo_operacao: str
    motivo: str
    mensagem_usuario: str | None
    sugerir_upgrade: bool
    upgrade_cta: dict[str, Any] | None
    franquia: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sugerir_upgrade(status_franquia: str, motivo: str) -> bool:
    if motivo == "bloqueio_manual":
        return False
    return status_franquia in (
        Franquia.STATUS_DEGRADED,
        Franquia.STATUS_BLOCKED,
        Franquia.STATUS_EXPIRED,
    )


def _mapear_modo_operacao(status_franquia: str) -> str:
    if status_franquia == Franquia.STATUS_ACTIVE:
        return MODO_OPERACAO_NORMAL
    if status_franquia == Franquia.STATUS_DEGRADED:
        return MODO_OPERACAO_DEGRADED
    if status_franquia in (Franquia.STATUS_BLOCKED, Franquia.STATUS_EXPIRED):
        return MODO_OPERACAO_BLOCKED
    return MODO_OPERACAO_BLOCKED


def _is_permitido(modo_operacao: str) -> bool:
    return modo_operacao in (MODO_OPERACAO_NORMAL, MODO_OPERACAO_DEGRADED)


def avaliar_autorizacao_operacao_por_franquia(
    user,
    *,
    sincronizar_ciclo: bool = True,
) -> dict[str, Any]:
    """
    Avalia autorização operacional pré-consumo com base na franquia do usuário.

    Contrato de retorno:
      - permitido: bool
      - status_franquia: str (status efetivo calculado)
      - modo_operacao: normal | degraded | blocked
      - motivo: código técnico interno
      - mensagem_usuario: mensagem para UI (quando aplicável)
      - sugerir_upgrade: bool
      - upgrade_cta: CTA estruturado para limite de plano (quando aplicável)
      - franquia: snapshot opcional de contexto operacional
    """
    if user is None or not getattr(user, "is_authenticated", False):
        decisao = DecisaoOperacaoFranquia(
            permitido=True,
            status_franquia="not_authenticated",
            modo_operacao=MODO_OPERACAO_NORMAL,
            motivo="usuario_nao_autenticado",
            mensagem_usuario=None,
            sugerir_upgrade=False,
            upgrade_cta=None,
            franquia=None,
        )
        return decisao.to_dict()

    franquia_id = getattr(user, "franquia_id", None)
    if franquia_id is None:
        decisao = DecisaoOperacaoFranquia(
            permitido=False,
            status_franquia="missing",
            modo_operacao=MODO_OPERACAO_BLOCKED,
            motivo="usuario_sem_franquia",
            mensagem_usuario="Sua conta não possui franquia operacional vinculada.",
            sugerir_upgrade=False,
            upgrade_cta=None,
            franquia=None,
        )
        return decisao.to_dict()

    leitura = ler_franquia_operacional_cleiton(
        int(franquia_id),
        sincronizar_ciclo=sincronizar_ciclo,
    )
    if leitura is None:
        decisao = DecisaoOperacaoFranquia(
            permitido=False,
            status_franquia="missing",
            modo_operacao=MODO_OPERACAO_BLOCKED,
            motivo="franquia_nao_encontrada",
            mensagem_usuario="A franquia operacional vinculada não foi encontrada.",
            sugerir_upgrade=False,
            upgrade_cta=None,
            franquia=None,
        )
        return decisao.to_dict()

    status_franquia = leitura.status
    motivo = leitura.motivo_status or "status_indefinido"
    modo_operacao = _mapear_modo_operacao(status_franquia)
    permitido = _is_permitido(modo_operacao)
    sugerir_upgrade = _sugerir_upgrade(status_franquia, motivo)
    mensagem_usuario = montar_mensagem_operacao(
        status_franquia=status_franquia,
        motivo=motivo,
        plano_resolvido=leitura.plano_resolvido,
        sugerir_upgrade=sugerir_upgrade,
    )
    upgrade_cta = None
    if status_exibe_cta_upgrade(status_franquia):
        upgrade_cta = montar_upgrade_cta_operacao(leitura.plano_resolvido)

    decisao = DecisaoOperacaoFranquia(
        permitido=permitido,
        status_franquia=status_franquia,
        modo_operacao=modo_operacao,
        motivo=motivo,
        mensagem_usuario=mensagem_usuario,
        sugerir_upgrade=sugerir_upgrade,
        upgrade_cta=upgrade_cta,
        franquia={
            "id": leitura.franquia_id,
            "status": leitura.status,
            "plano_resolvido": leitura.plano_resolvido,
            "motivo_status": leitura.motivo_status,
            "limite_total": (
                None if leitura.limite_total is None else str(leitura.limite_total)
            ),
            "consumo_acumulado": str(leitura.consumo_acumulado),
            "saldo_disponivel": (
                None
                if leitura.saldo_disponivel is None
                else str(leitura.saldo_disponivel)
            ),
            "pendencias": list(leitura.pendencias),
        },
    )
    resultado = decisao.to_dict()

    conta_id = getattr(user, "conta_id", None)
    if conta_id is not None:
        from app.services.cleiton_monetizacao_service import (
            montar_mensagem_renovacao_falha_mensal,
            resolver_falha_mensal_vigente_conta,
        )

        estado_falha = resolver_falha_mensal_vigente_conta(int(conta_id))
        if estado_falha.get("falha_mensal_vigente"):
            plano_exibicao = estado_falha.get("plano_exibicao")
            mensagem_renovacao = montar_mensagem_renovacao_falha_mensal(
                str(plano_exibicao or "")
            )
            if mensagem_renovacao:
                resultado["mensagem_usuario"] = mensagem_renovacao
                resultado["sugerir_upgrade"] = False
                resultado["upgrade_cta"] = None
                resultado["regularizacao_cta"] = montar_regularizacao_cta_operacao(
                    str(plano_exibicao or "")
                )

    return resultado
