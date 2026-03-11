"""
Cleiton - Política de retenção: dados 18 meses, imagens 2 meses.
Limpeza auditável e idempotente; eventos de purge registrados na auditoria.
"""
import logging
from datetime import datetime, timedelta, timezone
from app.extensions import db
from app.models import NoticiaPortal, Lead, Pauta, PublicacaoCanal, AuditoriaGerencial, InsightCanal, RecomendacaoEstrategica
from app.run_cleiton_agente_regras import get_retencao_meses_dados, get_retencao_meses_imagens
from app.run_cleiton_agente_auditoria import registrar_purge

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _data_limite_dados() -> datetime:
    return _utcnow_naive() - timedelta(days=get_retencao_meses_dados() * 30)


def _data_limite_imagens() -> datetime:
    return _utcnow_naive() - timedelta(days=get_retencao_meses_imagens() * 30)


def limpar_dados_antigos(app_flask) -> int:
    """
    Remove registros de dados editoriais/coleta mais antigos que a retenção (ex.: 18 meses).
    Retorna quantidade de itens removidos. Idempotente; registra purge na auditoria.
    """
    total = 0
    with app_flask.app_context():
        limite = _data_limite_dados()
        try:
            # Noticias (portal): dados editoriais
            q = NoticiaPortal.query.filter(NoticiaPortal.data_publicacao < limite)
            count_noticias = q.count()
            q.delete(synchronize_session=False)
            total += count_noticias
            if count_noticias:
                logger.info("Retenção: %d notícias/artigos removidos (antes de %s).", count_noticias, limite.date())
            # Leads: coleta
            q_lead = Lead.query.filter(Lead.data_inscricao < limite)
            count_leads = q_lead.count()
            q_lead.delete(synchronize_session=False)
            total += count_leads
            if count_leads:
                logger.info("Retenção: %d leads removidos (antes de %s).", count_leads, limite.date())
            # Pautas (Fase 3): retenção 18 meses por created_at
            try:
                q_pauta = Pauta.query.filter(Pauta.created_at < limite)
                count_pautas = q_pauta.count()
                q_pauta.delete(synchronize_session=False)
                total += count_pautas
                if count_pautas:
                    logger.info("Retenção: %d pautas removidas (antes de %s).", count_pautas, limite.date())
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
                    logger.info("Retenção: %d publicacao_canal removidos (antes de %s).", count_pub, limite.date())
            except Exception as e:
                logger.warning("Falha ao remover publicacao_canal antigos: %s", e)
            # Fase 5: InsightCanal e RecomendacaoEstrategica (18 meses)
            count_insight = 0
            count_rec = 0
            try:
                q_ins = InsightCanal.query.filter(InsightCanal.coletado_em < limite)
                count_insight = q_ins.count()
                q_ins.delete(synchronize_session=False)
                total += count_insight
                if count_insight:
                    logger.info("Retenção: %d insight_canal removidos (antes de %s).", count_insight, limite.date())
            except Exception as e:
                logger.warning("Falha ao remover insight_canal antigos: %s", e)
            try:
                q_rec = RecomendacaoEstrategica.query.filter(RecomendacaoEstrategica.criado_em < limite)
                count_rec = q_rec.count()
                q_rec.delete(synchronize_session=False)
                total += count_rec
                if count_rec:
                    logger.info("Retenção: %d recomendacao_estrategica removidos (antes de %s).", count_rec, limite.date())
            except Exception as e:
                logger.warning("Falha ao remover recomendacao_estrategica antigos: %s", e)
            db.session.commit()
            if total > 0:
                registrar_purge(
                    "purge_dados",
                    f"retencao_{get_retencao_meses_dados()}meses",
                    total,
                    detalhe=f"noticias={count_noticias} leads={count_leads} pautas={count_pautas} publicacao_canal={count_pub} insight_canal={count_insight} recomendacao_estrategica={count_rec}",
                )
        except Exception as e:
            logger.exception("Falha na limpeza de dados: %s", e)
            db.session.rollback()
    return total


def limpar_imagens_antigas(app_flask) -> int:
    """
    Limpa referências a imagens antigas (url_imagem em NoticiaPortal) além do prazo (ex.: 2 meses).
    Não remove arquivos de disco aqui; apenas zera url_imagem para registros antigos.
    Retorna quantidade de registros atualizados. Registra purge na auditoria.
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
            for r in rows:
                r.url_imagem = None
                total += 1
            if total:
                db.session.commit()
                logger.info("Retenção imagens: %d referências limpas (antes de %s).", total, limite.date())
                registrar_purge(
                    "purge_imagens",
                    f"retencao_{get_retencao_meses_imagens()}meses",
                    total,
                    detalhe="url_imagem zerado em NoticiaPortal",
                )
        except Exception as e:
            logger.exception("Falha na limpeza de imagens: %s", e)
            db.session.rollback()
    return total


def executar_limpeza_retencao(app_flask) -> None:
    """Executa limpeza de dados e imagens conforme política de retenção (idempotente)."""
    limpar_dados_antigos(app_flask)
    limpar_imagens_antigas(app_flask)
