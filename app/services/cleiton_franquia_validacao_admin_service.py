"""
Domínio Cleiton — pacote de validação administrativa (leitura operacional + reconciliação + pendências).

Consumível por rotas admin ou ferramentas internas; não substitui leitura por eventos legada.
"""
from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from app.services.cleiton_franquia_leitura_service import ler_franquia_operacional_cleiton
from app.services.cleiton_franquia_reconciliacao_service import (
    ResultadoReconciliacaoFranquiaCleiton,
    reconciliar_franquia_cleiton,
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

    return {
        "ok": True,
        "franquia_id": int(franquia_id),
        "leitura_operacional": _json_safe(leitura_d),
        "reconciliacao": _json_safe(recon_d),
        "pendencias_leitura": list(leitura.pendencias),
    }


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
