"""
Execução em background dos agentes (Cleiton, artigo manual).
Usado pelo admin quando ADMIN_CLEITON_EXEC_MODE=async ou em homolog/prod.
Persiste resultado para exibição na próxima carga da página.
"""
from __future__ import annotations

import logging
from app.services.agent_service import (
    persistir_ultima_execucao_manual,
)

logger = logging.getLogger(__name__)

ORIGEM_CLEITON = "Executar Cleiton"
ORIGEM_ARTIGO_MANUAL = "Executar artigo agora"


def run_cleiton_background(app, bypass_frequencia: bool, consumo_identidade: dict | None = None) -> None:
    """
    Executa ciclo Cleiton no background e persiste resultado em last_admin_run.json.
    Chamado pelo admin_routes quando modo async está ativo.
    """
    try:
        from app.run_cleiton import executar_orquestracao
        resultado = executar_orquestracao(
            app,
            bypass_frequencia=bypass_frequencia,
            ignorar_janela_publicacao=bypass_frequencia,
            consumo_identidade=consumo_identidade,
        ) or {}
        logger.info(
            "Cleiton admin (async) concluído: status=%s mission_id=%s motivo=%s caminho=%s",
            resultado.get("status"),
            resultado.get("mission_id"),
            resultado.get("motivo_final") or resultado.get("motivo"),
            resultado.get("caminho_usado"),
        )
        persistir_ultima_execucao_manual(resultado, ORIGEM_CLEITON, app_flask=app)
    except Exception as e:
        logger.exception("Falha no ciclo Cleiton admin (async): %s", e)
        persistir_ultima_execucao_manual(
            {
                "status": "falha",
                "motivo": str(e),
                "caminho_usado": "excecao",
            },
            ORIGEM_CLEITON,
            app_flask=app,
        )


def run_artigo_manual_background(app, consumo_identidade: dict | None = None) -> None:
    """
    Executa missão manual de artigo no background e persiste resultado.
    """
    try:
        from app.run_cleiton import executar_orquestracao
        resultado = executar_orquestracao(
            app,
            bypass_frequencia=True,
            tipo_missao_forcado="artigo",
            ignorar_trava_artigo_hoje=True,
            ignorar_janela_publicacao=True,
            consumo_identidade=consumo_identidade,
        ) or {}
        logger.info(
            "Artigo manual admin (async) concluído: status=%s mission_id=%s motivo=%s caminho=%s",
            resultado.get("status"),
            resultado.get("mission_id"),
            resultado.get("motivo_final") or resultado.get("motivo"),
            resultado.get("caminho_usado"),
        )
        persistir_ultima_execucao_manual(
            resultado, ORIGEM_ARTIGO_MANUAL, app_flask=app
        )
    except Exception as e:
        logger.exception("Falha no artigo manual admin (async): %s", e)
        persistir_ultima_execucao_manual(
            {
                "status": "falha",
                "motivo": str(e),
                "caminho_usado": "excecao",
            },
            ORIGEM_ARTIGO_MANUAL,
            app_flask=app,
        )
