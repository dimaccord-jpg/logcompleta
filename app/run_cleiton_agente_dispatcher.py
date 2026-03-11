"""
Cleiton - Agente Dispatcher: contrato e despacho para agentes operacionais.
Payload padronizado: mission_id, tipo_missao, tema, prioridade, janela_publicacao, tentativa_atual, metadados.
Nunca gera conteúdo final; apenas invoca os agentes (Julia, coleta, etc.).
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any
from app.extensions import db
from app.models import MissaoAgente
from app.run_cleiton_agente_regras import get_prioridade_padrao, get_max_retries
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def construir_payload(
    tipo_missao: str,
    tema: str | None = None,
    prioridade: int | None = None,
    janela_publicacao_inicio: datetime | None = None,
    janela_publicacao_fim: datetime | None = None,
    tentativa_atual: int = 1,
    metadados: dict | None = None,
    mission_id: str | None = None,
) -> dict[str, Any]:
    """
    Monta o payload padronizado para envio aos agentes operacionais.
    """
    mission_id = mission_id or str(uuid.uuid4())
    prioridade = prioridade if prioridade is not None else get_prioridade_padrao()
    payload = {
        "mission_id": mission_id,
        "tipo_missao": tipo_missao,
        "tema": tema or "",
        "prioridade": prioridade,
        "janela_publicacao": {
            "inicio": janela_publicacao_inicio.isoformat() if janela_publicacao_inicio else None,
            "fim": janela_publicacao_fim.isoformat() if janela_publicacao_fim else None,
        },
        "tentativa_atual": tentativa_atual,
        "metadados": metadados or {},
    }
    return payload


def registrar_missao(payload: dict[str, Any]) -> MissaoAgente | None:
    """Persiste a missão no banco para rastreio e retries."""
    try:
        mission_id = payload["mission_id"]
        janela = payload.get("janela_publicacao") or {}
        m = MissaoAgente(
            mission_id=mission_id,
            tipo_missao=payload["tipo_missao"],
            tema=payload.get("tema"),
            prioridade=payload.get("prioridade", get_prioridade_padrao()),
            janela_publicacao_inicio=datetime.fromisoformat(janela["inicio"]) if janela.get("inicio") else None,
            janela_publicacao_fim=datetime.fromisoformat(janela["fim"]) if janela.get("fim") else None,
            tentativa_atual=payload.get("tentativa_atual", 1),
            max_tentativas=get_max_retries(),
            status="pendente",
            payload_metadados=json.dumps(payload, ensure_ascii=False),
        )
        db.session.add(m)
        db.session.commit()
        return m
    except Exception as e:
        logger.exception("Falha ao registrar missão: %s", e)
        try:
            db.session.rollback()
        except Exception:
            pass
        return None


def marcar_missao_resultado(mission_id: str, status: str) -> None:
    """Atualiza status da missão: enviado, sucesso, falha."""
    try:
        m = MissaoAgente.query.filter_by(mission_id=mission_id).first()
        if m:
            m.status = status
            if status in ("sucesso", "falha"):
                m.concluido_em = _utcnow_naive()
            db.session.commit()
    except Exception as e:
        logger.warning("Falha ao atualizar missão %s: %s", mission_id, e)
        try:
            db.session.rollback()
        except Exception:
            pass


def despachar_para_julia(payload: dict[str, Any], app_flask) -> bool:
    """
    Despacha missão para a Júlia (agente operacional de redação).
    Retorna True apenas quando houve publicação bem-sucedida.
    """
    mission_id = payload.get("mission_id", "")
    tipo_missao = payload.get("tipo_missao", "noticia")
    try:
        with app_flask.app_context():
            from app.run_julia import processar_insight_do_momento
            publicado = bool(processar_insight_do_momento(payload_cleiton=payload))
        marcar_missao_resultado(mission_id, "sucesso" if publicado else "falha")
        auditoria_registrar(
            tipo_decisao="dispatch",
            decisao=f"Despacho Julia | mission_id={mission_id} | tipo={tipo_missao}",
            contexto=payload,
            resultado="sucesso" if publicado else "falha",
            detalhe=None if publicado else "Júlia não publicou conteúdo (falha de geração ou sem pauta válida).",
        )
        return publicado
    except Exception as e:
        logger.exception("Falha ao despachar para Julia: %s", e)
        marcar_missao_resultado(mission_id, "falha")
        auditoria_registrar(
            tipo_decisao="dispatch",
            decisao=f"Despacho Julia falhou | mission_id={mission_id}",
            contexto=payload,
            resultado="falha",
            detalhe=str(e),
        )
        return False


def despachar(payload: dict[str, Any], app_flask) -> bool:
    """
    Roteia o payload para o agente operacional correspondente ao tipo_missao.
    Tipos conhecidos: artigo, noticia (Julia). Outros podem ser adicionados (coleta, imagem, qa, publicacao).
    """
    tipo = (payload.get("tipo_missao") or "noticia").lower()
    if tipo in ("artigo", "noticia"):
        return despachar_para_julia(payload, app_flask)
    # Futuro: coleta, curadoria, imagem, qa, publicacao
    logger.warning("Tipo de missão não mapeado para dispatch: %s", tipo)
    return False
