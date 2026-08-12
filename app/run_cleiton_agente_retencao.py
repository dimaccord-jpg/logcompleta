"""
Cleiton - Politica de retencao: dados 18 meses, imagens 2 meses.
Limpeza auditavel e idempotente; eventos de purge registrados na auditoria.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from app.extensions import db
from app.models import (
    AuditoriaGerencial,
    InsightCanal,
    Lead,
    NoticiaPortal,
    Pauta,
    PublicacaoCanal,
    RecomendacaoEstrategica,
)
from app.run_cleiton_agente_auditoria import registrar_purge
from app.run_cleiton_agente_regras import get_retencao_meses_dados, get_retencao_meses_imagens

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _data_limite_dados() -> datetime:
    return _utcnow_naive() - timedelta(days=get_retencao_meses_dados() * 30)


def _data_limite_imagens() -> datetime:
    return _utcnow_naive() - timedelta(days=get_retencao_meses_imagens() * 30)


def limpar_dados_antigos(app_flask) -> int:
    """
    Remove registros de dados editoriais/coleta mais antigos que a retencao (ex.: 18 meses).
    Retorna quantidade de itens removidos. Idempotente; registra purge na auditoria.
    """
    total = 0
    with app_flask.app_context():
        limite = _data_limite_dados()
        try:
            q = NoticiaPortal.query.filter(NoticiaPortal.data_publicacao < limite)
            count_noticias = q.count()
            q.delete(synchronize_session=False)
            total += count_noticias
            if count_noticias:
                logger.info("Retencao: %d noticias/artigos removidos (antes de %s).", count_noticias, limite.date())

            # Lead: data_inscricao antiga NÃO basta se houver captura recente de campanha.
            q_lead = Lead.query.filter(
                Lead.data_inscricao < limite,
                or_(
                    Lead.campaign_captured_at.is_(None),
                    Lead.campaign_captured_at < limite,
                ),
            )
            count_leads = q_lead.count()
            q_lead.delete(synchronize_session=False)
            total += count_leads
            if count_leads:
                logger.info("Retencao: %d leads removidos (antes de %s).", count_leads, limite.date())

            try:
                q_pauta = Pauta.query.filter(Pauta.created_at < limite)
                count_pautas = q_pauta.count()
                q_pauta.delete(synchronize_session=False)
                total += count_pautas
                if count_pautas:
                    logger.info("Retencao: %d pautas removidas (antes de %s).", count_pautas, limite.date())
            except Exception as e:
                logger.warning("Falha ao remover pautas antigas: %s", e)
                count_pautas = 0

            count_pub = 0
            try:
                q_pub = PublicacaoCanal.query.filter(PublicacaoCanal.criado_em < limite)
                count_pub = q_pub.count()
                q_pub.delete(synchronize_session=False)
                total += count_pub
                if count_pub:
                    logger.info("Retencao: %d publicacao_canal removidos (antes de %s).", count_pub, limite.date())
            except Exception as e:
                logger.warning("Falha ao remover publicacao_canal antigos: %s", e)

            count_insight = 0
            count_rec = 0
            try:
                q_ins = InsightCanal.query.filter(InsightCanal.coletado_em < limite)
                count_insight = q_ins.count()
                q_ins.delete(synchronize_session=False)
                total += count_insight
                if count_insight:
                    logger.info("Retencao: %d insight_canal removidos (antes de %s).", count_insight, limite.date())
            except Exception as e:
                logger.warning("Falha ao remover insight_canal antigos: %s", e)
            try:
                q_rec = RecomendacaoEstrategica.query.filter(RecomendacaoEstrategica.criado_em < limite)
                count_rec = q_rec.count()
                q_rec.delete(synchronize_session=False)
                total += count_rec
                if count_rec:
                    logger.info(
                        "Retencao: %d recomendacao_estrategica removidos (antes de %s).",
                        count_rec,
                        limite.date(),
                    )
            except Exception as e:
                logger.warning("Falha ao remover recomendacao_estrategica antigos: %s", e)

            db.session.commit()
            if total > 0:
                registrar_purge(
                    "purge_dados",
                    f"retencao_{get_retencao_meses_dados()}meses",
                    total,
                    detalhe=(
                        f"noticias={count_noticias} leads={count_leads} pautas={count_pautas} "
                        f"publicacao_canal={count_pub} insight_canal={count_insight} "
                        f"recomendacao_estrategica={count_rec}"
                    ),
                )
        except Exception as e:
            logger.exception("Falha na limpeza de dados: %s", e)
            db.session.rollback()
    return total


def limpar_imagens_antigas(app_flask) -> int:
    """
    Audita imagens antigas sem alterar patrimonio editorial publicado.
    O fallback deve acontecer apenas na renderizacao read-only, nunca persistindo nova URL.
    Retorna quantidade de registros elegiveis auditados. Registra purge na auditoria.
    """
    total = 0
    with app_flask.app_context():
        limite = _data_limite_imagens()
        try:
            q = NoticiaPortal.query.filter(
                NoticiaPortal.data_publicacao < limite,
                NoticiaPortal.url_imagem.isnot(None),
                NoticiaPortal.url_imagem != "",
            )
            rows = q.all()
            total = len(rows)
            if total:
                logger.info(
                    "Retencao imagens: %d referencias auditadas (antes de %s) sem alterar url_imagem.",
                    total,
                    limite.date(),
                )
                registrar_purge(
                    "purge_imagens",
                    f"retencao_{get_retencao_meses_imagens()}meses",
                    total,
                    detalhe="nenhum write em noticia.url_imagem; fallback permitido apenas em renderizacao read-only",
                )
        except Exception as e:
            logger.exception("Falha na limpeza de imagens: %s", e)
            db.session.rollback()
    return total


def executar_limpeza_retencao(app_flask) -> None:
    """Executa limpeza de dados e imagens conforme politica de retencao (idempotente)."""
    limpar_dados_antigos(app_flask)
    limpar_imagens_antigas(app_flask)
