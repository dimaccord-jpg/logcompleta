"""
Domínio Cleiton — pacote de validação administrativa (leitura operacional + reconciliação + pendências).

Consumível por rotas admin ou ferramentas internas; não substitui leitura por eventos legada.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from app.extensions import db
from app.models import Franquia
from app.services import plano_service
from app.services.cleiton_franquia_leitura_service import ler_franquia_operacional_cleiton
from app.services.cleiton_franquia_reconciliacao_service import (
    ResultadoReconciliacaoFranquiaCleiton,
    reconciliar_franquia_cleiton,
)
from app.services.cleiton_monetizacao_service import (
    obter_contexto_monetizacao_conta,
    reprocessar_pendencias_correlacao_por_conta_admin,
)


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(x) for x in obj]
    return obj


def obter_pacote_validacao_franquia_cleiton(
    franquia_id: int,
    *,
    sincronizar_ciclo_leitura: bool = False,
    aplicar_correcao: bool = False,
) -> dict[str, Any]:
    """
    Retorna estrutura consolidada para auditoria: leitura operacional, reconciliação e pendências.

    `aplicar_correcao` repassa ao serviço de reconciliação (somente quando explicitamente True).
    """
    # Em modo de correção, reconciliar primeiro evita devolver leitura stale.
    franquia = db.session.get(Franquia, int(franquia_id))
    if franquia is None:
        return {
            "ok": False,
            "erro": "franquia_nao_encontrada",
            "franquia_id": int(franquia_id),
        }

    recon = reconciliar_franquia_cleiton(franquia_id, aplicar_correcao=aplicar_correcao)
    leitura = ler_franquia_operacional_cleiton(
        franquia_id, sincronizar_ciclo=sincronizar_ciclo_leitura
    )
    if leitura is None:
        return {
            "ok": False,
            "erro": "franquia_nao_encontrada",
            "franquia_id": int(franquia_id),
        }

    leitura_d = asdict(leitura)
    recon_d = _resultado_reconciliacao_para_dict(recon)
    monetizacao_contexto = obter_contexto_monetizacao_conta(franquia.conta_id)
    auditoria_monetizacao = dict((monetizacao_contexto.get("auditoria_monetizacao") or {}))
    auditoria_monetizacao["divergencias_relevantes"] = _detectar_divergencias_auditoria_contratual(
        monetizacao_contexto=monetizacao_contexto
    )
    plano_contexto = (leitura.plano_resolvido or "").strip().lower()
    pendencias_gateway = plano_service.listar_pendencias_gateway_monetizacao_por_plano_admin(
        plano_contexto
    )
    pendencias_governanca = list(leitura.pendencias)
    if pendencias_gateway:
        pendencias_governanca.append("stripe_configuracao_pendente_admin")

    return {
        "ok": True,
        "franquia_id": int(franquia_id),
        "leitura_operacional": _json_safe(leitura_d),
        "reconciliacao": _json_safe(recon_d),
        "pendencias_leitura": list(leitura.pendencias),
        "plano_contexto_auditado": plano_contexto,
        "contexto_monetario": _json_safe(monetizacao_contexto),
        "auditoria_monetizacao": _json_safe(auditoria_monetizacao),
        "pendencias_configuracao_stripe_planos": _json_safe(pendencias_gateway),
        "pendencias_governanca": _json_safe(pendencias_governanca),
    }


def reprocessar_pendencias_monetizacao_franquia_admin(
    *,
    franquia_id: int,
    admin_user_id: int | None = None,
    limite: int = 20,
) -> dict[str, Any]:
    franquia = db.session.get(Franquia, int(franquia_id))
    if franquia is None:
        return {
            "ok": False,
            "erro": "franquia_nao_encontrada",
            "franquia_id": int(franquia_id),
        }
    resultado = reprocessar_pendencias_correlacao_por_conta_admin(
        conta_id=int(franquia.conta_id),
        franquia_id_contexto=int(franquia.id),
        admin_user_id=admin_user_id,
        limite=limite,
    )
    return {
        "ok": True,
        "franquia_id": int(franquia.id),
        "conta_id": int(franquia.conta_id),
        "reprocessamento": _json_safe(resultado),
    }


def _detectar_divergencias_auditoria_contratual(
    *,
    monetizacao_contexto: dict[str, Any],
) -> list[dict[str, Any]]:
    vinculo_ativo = monetizacao_contexto.get("vinculo_comercial_externo_ativo") or {}
    fatos = monetizacao_contexto.get("fatos_monetizacao_recentes") or []
    ultimo_fato_com_efeito = next(
        (
            f
            for f in fatos
            if (str(f.get("status_tecnico") or "").strip().lower())
            == "efeito_operacional_aplicado"
        ),
        None,
    )
    if not ultimo_fato_com_efeito:
        return []
    snapshot = (ultimo_fato_com_efeito.get("snapshot_normalizado") or {}) if isinstance(ultimo_fato_com_efeito, dict) else {}
    divergencias: list[dict[str, Any]] = []

    plano_interno_fato = (snapshot.get("plano_resolvido") or "").strip().lower()
    plano_interno_vinculo = (vinculo_ativo.get("plano_interno") or "").strip().lower()
    if plano_interno_fato and plano_interno_vinculo and plano_interno_fato != plano_interno_vinculo:
        divergencias.append(
            {
                "tipo": "plano_interno_divergente",
                "fato_interno": plano_interno_fato,
                "vinculo_externo": plano_interno_vinculo,
            }
        )

    status_fato = (snapshot.get("status_contratual_externo") or "").strip().lower()
    status_vinculo = (vinculo_ativo.get("status_contratual_externo") or "").strip().lower()
    if status_fato and status_vinculo and status_fato != status_vinculo:
        divergencias.append(
            {
                "tipo": "status_contratual_divergente",
                "fato_interno": status_fato,
                "vinculo_externo": status_vinculo,
            }
        )
    return divergencias


def _resultado_reconciliacao_para_dict(
    r: ResultadoReconciliacaoFranquiaCleiton,
) -> dict[str, Any]:
    return {
        "franquia_id": r.franquia_id,
        "total_persistido": r.total_persistido,
        "total_recalculado": r.total_recalculado,
        "diferenca": r.diferenca,
        "status": r.status,
        "contagem_eventos_ia_abativel": r.contagem_eventos_ia_abativel,
        "contagem_eventos_processing_abativel": r.contagem_eventos_processing_abativel,
        "contagem_eventos_ia_excluidos": r.contagem_eventos_ia_excluidos,
        "contagem_eventos_processing_excluidos": r.contagem_eventos_processing_excluidos,
        "correcao_aplicada": r.correcao_aplicada,
    }
