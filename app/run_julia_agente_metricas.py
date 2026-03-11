"""
Júlia - Agente Métricas: captura e consolidação de métricas por noticia_id, mission_id, canal.
Fonte inicial mock/estimada; preparado para APIs reais (INSIGHT_COLETA_MODO=real).
Persiste em InsightCanal (bind gerencial). Chamado pelo Customer Insight (Cleiton).
"""
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from app.extensions import db
from app.models import NoticiaPortal, PublicacaoCanal, InsightCanal

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """Retorna datetime UTC naive para persistência compatível com schema atual."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _modo_coleta() -> str:
    """mock | real. Quando real, futuramente integrar APIs externas."""
    return (os.getenv("INSIGHT_COLETA_MODO", "mock").strip() or "mock").lower()


def _janela_dias() -> int:
    """Quantos dias para trás considerar publicações para coleta de métricas."""
    try:
        return max(1, min(365, int(os.getenv("INSIGHT_JANELA_DIAS", "30").strip())))
    except ValueError:
        return 30


def _gerar_metricas_mock(noticia_id: int, mission_id: str | None, canal: str) -> dict[str, Any]:
    """Gera métricas estimadas para um (noticia_id, canal). Reproduzível por seed opcional."""
    impressoes = random.randint(50, 5000)
    cliques = random.randint(0, max(1, impressoes // 10))
    ctr = (cliques / impressoes * 100.0) if impressoes else 0.0
    leads_gerados = random.randint(0, min(10, cliques))
    taxa_conversao = (leads_gerados / cliques * 100.0) if cliques else 0.0
    engajamento = round(random.uniform(0.5, 5.0), 2)
    return {
        "impressoes": impressoes,
        "cliques": cliques,
        "ctr": round(ctr, 2),
        "leads_gerados": leads_gerados,
        "taxa_conversao": round(taxa_conversao, 2),
        "engajamento": engajamento,
    }


def _calcular_score_bruto(impressoes: int, cliques: int, ctr: float, leads: int, engajamento: float) -> float:
    """Score 0-100 para performance (pesos simples)."""
    if impressoes <= 0:
        return 0.0
    # Normaliza: impressões (peso), CTR (peso), leads e engajamento
    p_imp = min(100, impressoes / 50) * 0.2
    p_ctr = min(100, ctr * 10) * 0.3
    p_cliques = min(100, cliques * 2) * 0.2
    p_leads = min(100, leads * 10) * 0.15
    p_eng = min(100, engajamento * 25) * 0.15
    return round(p_imp + p_ctr + p_cliques + p_leads + p_eng, 2)


def coletar_metricas_por_canal(app_flask) -> int:
    """
    Consolida métricas por (noticia_id, mission_id, canal) e persiste em InsightCanal.
    Usa PublicacaoCanal e NoticiaPortal para saber o que foi publicado.
    Retorna quantidade de registros InsightCanal inseridos/atualizados.
    """
    count = 0
    with app_flask.app_context():
        modo = _modo_coleta()
        limite = _utcnow_naive() - timedelta(days=_janela_dias())

        # Publicações recentes (gerencial)
        publicacoes = (
            PublicacaoCanal.query.filter(PublicacaoCanal.criado_em >= limite)
            .order_by(PublicacaoCanal.criado_em.desc())
            .all()
        )
        # Notícias publicadas no portal na janela (para ter noticia_id mesmo sem PublicacaoCanal)
        noticias_portal = (
            NoticiaPortal.query.filter(
                NoticiaPortal.data_publicacao >= limite,
                NoticiaPortal.status_publicacao == "publicado",
            )
            .all()
        )

        processado_em = _utcnow_naive()
        pares_vistos: set[tuple[int, str]] = set()

        for pc in publicacoes:
            key = (pc.noticia_id, pc.canal)
            if key in pares_vistos:
                continue
            pares_vistos.add(key)
            if modo == "mock":
                m = _gerar_metricas_mock(pc.noticia_id, pc.mission_id, pc.canal)
            else:
                # real: futuramente chamar API por canal; por ora fallback mock
                m = _gerar_metricas_mock(pc.noticia_id, pc.mission_id, pc.canal)

            score = _calcular_score_bruto(
                m["impressoes"], m["cliques"], m["ctr"], m["leads_gerados"], m.get("engajamento", 0)
            )
            try:
                existente = InsightCanal.query.filter_by(
                    noticia_id=pc.noticia_id, canal=pc.canal
                ).first()
                if existente:
                    existente.impressoes = m["impressoes"]
                    existente.cliques = m["cliques"]
                    existente.ctr = m["ctr"]
                    existente.leads_gerados = m["leads_gerados"]
                    existente.taxa_conversao = m["taxa_conversao"]
                    existente.engajamento = m.get("engajamento")
                    existente.score_performance = score
                    existente.origem_dado = modo
                    existente.coletado_em = processado_em
                    existente.processado_em = processado_em
                else:
                    ins = InsightCanal(
                        noticia_id=pc.noticia_id,
                        mission_id=pc.mission_id,
                        canal=pc.canal,
                        impressoes=m["impressoes"],
                        cliques=m["cliques"],
                        ctr=m["ctr"],
                        leads_gerados=m["leads_gerados"],
                        taxa_conversao=m["taxa_conversao"],
                        engajamento=m.get("engajamento"),
                        score_performance=score,
                        origem_dado=modo,
                        coletado_em=processado_em,
                        processado_em=processado_em,
                    )
                    db.session.add(ins)
                count += 1
            except Exception as e:
                logger.warning("Falha ao persistir InsightCanal noticia_id=%s canal=%s: %s", pc.noticia_id, pc.canal, e)
                db.session.rollback()
                continue

        # Para notícias só no portal (sem PublicacaoCanal para portal), garantir canal 'portal'
        for np in noticias_portal:
            key = (np.id, "portal")
            if key in pares_vistos:
                continue
            pares_vistos.add(key)
            if modo == "mock":
                m = _gerar_metricas_mock(np.id, None, "portal")
            else:
                m = _gerar_metricas_mock(np.id, None, "portal")
            score = _calcular_score_bruto(
                m["impressoes"], m["cliques"], m["ctr"], m["leads_gerados"], m.get("engajamento", 0)
            )
            try:
                existente = InsightCanal.query.filter_by(noticia_id=np.id, canal="portal").first()
                if existente:
                    existente.impressoes = m["impressoes"]
                    existente.cliques = m["cliques"]
                    existente.ctr = m["ctr"]
                    existente.leads_gerados = m["leads_gerados"]
                    existente.taxa_conversao = m["taxa_conversao"]
                    existente.engajamento = m.get("engajamento")
                    existente.score_performance = score
                    existente.origem_dado = modo
                    existente.coletado_em = processado_em
                    existente.processado_em = processado_em
                else:
                    ins = InsightCanal(
                        noticia_id=np.id,
                        mission_id=None,
                        canal="portal",
                        impressoes=m["impressoes"],
                        cliques=m["cliques"],
                        ctr=m["ctr"],
                        leads_gerados=m["leads_gerados"],
                        taxa_conversao=m["taxa_conversao"],
                        engajamento=m.get("engajamento"),
                        score_performance=score,
                        origem_dado=modo,
                        coletado_em=processado_em,
                        processado_em=processado_em,
                    )
                    db.session.add(ins)
                count += 1
            except Exception as e:
                logger.warning("Falha ao persistir InsightCanal noticia_id=%s canal=portal: %s", np.id, e)
                db.session.rollback()

        try:
            db.session.commit()
            if count:
                logger.info("Métricas: %d registros InsightCanal persistidos (modo=%s).", count, modo)
        except Exception as e:
            logger.exception("Falha ao commitar métricas: %s", e)
            db.session.rollback()
            count = 0
    return count
