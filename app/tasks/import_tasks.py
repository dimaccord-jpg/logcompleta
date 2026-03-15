"""
Execução em background de importações e atualização de índices.
Permite disparar atualização de índices financeiros em background para evitar timeout.
"""
import logging
from app.services.agent_service import persistir_execucao_indices_admin

logger = logging.getLogger(__name__)


def run_atualizar_indices_background(app) -> None:
    """
    Executa atualização manual dos índices financeiros em background e persiste
    o resultado no log de execuções do admin.
    Chamado pela rota quando se deseja execução assíncrona.
    """
    try:
        from app.finance import atualizar_indices
        resultado = atualizar_indices() or {}
        persistir_execucao_indices_admin(resultado, app)
        logger.info(
            "Atualização de índices (background) concluída: status_global=%s",
            resultado.get("status_global"),
        )
    except Exception as e:
        logger.exception("Falha ao atualizar índices em background: %s", e)
        persistir_execucao_indices_admin(
            {
                "status_global": "falha",
                "mensagem": str(e),
            },
            app,
        )
