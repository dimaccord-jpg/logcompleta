"""
Cleiton - Agente Auditoria: trilha de auditoria persistida no banco.
Registra cada decisão do orquestrador (orquestração, dispatch, retry, purge).
"""
import json
import logging
from datetime import datetime
from app.extensions import db
from app.models import AuditoriaGerencial

logger = logging.getLogger(__name__)


def registrar(
    tipo_decisao: str,
    decisao: str,
    contexto: dict | None = None,
    resultado: str = "sucesso",
    detalhe: str | None = None,
) -> AuditoriaGerencial | None:
    """
    Persiste um evento na auditoria gerencial.
    tipo_decisao: orquestracao | dispatch | retry | purge_dados | purge_imagens
    resultado: sucesso | falha | ignorado
    """
    try:
        contexto_json = json.dumps(contexto, ensure_ascii=False) if contexto else None
        entry = AuditoriaGerencial(
            tipo_decisao=tipo_decisao,
            decisao=decisao[:255] if decisao else "",
            contexto_json=contexto_json,
            resultado=resultado,
            detalhe=detalhe,
        )
        db.session.add(entry)
        db.session.commit()
        logger.debug("Auditoria registrada: %s | %s", tipo_decisao, decisao)
        return entry
    except Exception as e:
        logger.exception("Falha ao registrar auditoria: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def registrar_purge(tipo: str, criterio: str, itens_afetados: int, detalhe: str | None = None) -> None:
    """Registra evento de purge (retenção) na auditoria. tipo: purge_dados | purge_imagens."""
    registrar(
        tipo_decisao=tipo,
        decisao=f"Purge por retenção: {criterio}",
        contexto={"itens_afetados": itens_afetados, "criterio": criterio},
        resultado="sucesso",
        detalhe=detalhe,
    )
