"""
Serviço de auditoria administrativa.
Registra ações do painel admin (operações, vínculos, reprocessamento) na trilha gerencial.
Não quebra o fluxo em caso de falha ao persistir.
"""
import logging
from app.run_cleiton_agente_auditoria import registrar as _registrar_auditoria

logger = logging.getLogger(__name__)


def registrar_auditoria_admin(
    actor_email: str | None,
    tipo_decisao: str,
    decisao: str,
    entidade: str,
    entidade_id: int | None,
    estado_antes: dict | None,
    estado_depois: dict | None,
    motivo: str | None,
    resultado: str,
    detalhe: str | None = None,
) -> None:
    """
    Registra evento de auditoria padronizado para ações do admin.
    tipo_decisao: admin_operacao | admin_vinculo | admin_reprocessamento etc.
    resultado: sucesso | falha | ignorado
    """
    try:
        contexto = {
            "ator": actor_email,
            "entidade": entidade,
            "entidade_id": entidade_id,
            "antes": estado_antes,
            "depois": estado_depois,
            "motivo": motivo,
        }
        _registrar_auditoria(
            tipo_decisao=tipo_decisao,
            decisao=decisao,
            contexto=contexto,
            resultado=resultado,
            detalhe=detalhe,
        )
    except Exception:
        logger.exception(
            "Falha ao registrar auditoria admin para %s id=%s", entidade, entidade_id
        )
