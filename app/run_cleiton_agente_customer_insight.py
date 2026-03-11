"""
Cleiton - Agente Customer Insight: mede desempenho por conteúdo/canal, gera recomendações estratégicas.
Aciona coleta de métricas (Julia), calcula score, classifica (manter/escalar/ajustar/pausar),
persiste RecomendacaoEstrategica e audita com tipo_decisao=insight.
Em falha, registra auditoria e não quebra o ciclo principal.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.extensions import db
from app.models import InsightCanal, RecomendacaoEstrategica
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

logger = logging.getLogger(__name__)

CLASSIFICACAO_MANTER = "manter"
CLASSIFICACAO_ESCALAR = "escalar"
CLASSIFICACAO_AJUSTAR = "ajustar"
CLASSIFICACAO_PAUSAR = "pausar"


def _utcnow_naive() -> datetime:
    """Retorna datetime UTC naive para compatibilidade com colunas existentes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _insight_enabled() -> bool:
    return os.getenv("INSIGHT_ENABLED", "true").strip().lower() in ("true", "1", "t", "yes")


def _janela_dias() -> int:
    try:
        return max(1, min(365, int(os.getenv("INSIGHT_JANELA_DIAS", "30").strip())))
    except ValueError:
        return 30


def _score_escalar() -> float:
    """Acima deste score => classificação escalar."""
    try:
        return float(os.getenv("INSIGHT_SCORE_ESCALAR", "70").strip())
    except ValueError:
        return 70.0


def _score_pausar() -> float:
    """Abaixo deste score => classificação pausar (ou se impressões < mínimo)."""
    try:
        return float(os.getenv("INSIGHT_SCORE_PAUSAR", "25").strip())
    except ValueError:
        return 25.0


def _min_impressoes() -> int:
    """Abaixo deste número de impressões => considerar pausar."""
    try:
        return max(0, int(os.getenv("INSIGHT_MIN_IMPRESSOES", "100").strip()))
    except ValueError:
        return 100


def classificar_desempenho(score: float, impressoes: int) -> str:
    """
    Classifica desempenho: manter | escalar | ajustar | pausar.
    Regras: score >= INSIGHT_SCORE_ESCALAR => escalar; score <= INSIGHT_SCORE_PAUSAR ou impressoes < min => pausar;
    entre pausar e escalar => ajustar; score médio e impressões ok => manter.
    """
    lim_esc = _score_escalar()
    lim_pau = _score_pausar()
    min_imp = _min_impressoes()
    if impressoes < min_imp or score <= lim_pau:
        return CLASSIFICACAO_PAUSAR
    if score >= lim_esc:
        return CLASSIFICACAO_ESCALAR
    if score <= (lim_pau + lim_esc) / 2:
        return CLASSIFICACAO_AJUSTAR
    return CLASSIFICACAO_MANTER


def gerar_recomendacoes(insights: list[InsightCanal], classificacoes: dict[tuple[int, str], str]) -> list[dict[str, Any]]:
    """
    Gera lista de recomendações objetivas (tema, tipo, canal, horário, frequência) a partir dos insights.
    """
    recomendacoes: list[dict[str, Any]] = []
    agora = _utcnow_naive()

    # Agrupar por canal para sugerir canal preferencial
    por_canal: dict[str, list[InsightCanal]] = {}
    for ins in insights:
        por_canal.setdefault(ins.canal, []).append(ins)

    # Escalar: sugerir mais do mesmo canal/tema
    for ins in insights:
        key = (ins.noticia_id, ins.canal)
        cl = classificacoes.get(key, CLASSIFICACAO_MANTER)
        if cl == CLASSIFICACAO_ESCALAR:
            recomendacoes.append({
                "tipo_recomendacao": "escalar",
                "canal_preferencial": ins.canal,
                "tema_sugerido": "manter linha editorial atual",
                "tipo_conteudo": "noticia",
                "horario_sugerido": f"{agora.hour}:00",
                "frequencia_sugerida": "aumentar posts neste canal",
                "prioridade": 8,
                "contexto": {"noticia_id": ins.noticia_id, "canal": ins.canal, "score": ins.score_performance},
            })
        elif cl == CLASSIFICACAO_PAUSAR:
            recomendacoes.append({
                "tipo_recomendacao": "pausar",
                "canal_preferencial": ins.canal,
                "tema_sugerido": "revisar tema ou formato",
                "tipo_conteudo": "noticia",
                "horario_sugerido": "reavaliar horário de pico",
                "frequencia_sugerida": "reduzir ou pausar neste canal",
                "prioridade": 7,
                "contexto": {"noticia_id": ins.noticia_id, "canal": ins.canal, "score": ins.score_performance, "impressoes": ins.impressoes},
            })

    # Recomendação agregada por desempenho geral
    if insights:
        scores = [(i.score_performance or 0.0) for i in insights]
        media = sum(scores) / len(scores)
        melhor_canal = max(
            por_canal.keys(),
            key=lambda c: sum((i.score_performance or 0.0) for i in por_canal[c]) / max(1, len(por_canal[c]))
        )
        recomendacoes.append({
            "tipo_recomendacao": "estrategia",
            "canal_preferencial": melhor_canal,
            "tema_sugerido": "conteúdo de alto desempenho",
            "tipo_conteudo": "noticia",
            "horario_sugerido": "9-11h e 14-16h",
            "frequencia_sugerida": "2-3 posts/dia no canal de melhor performance",
            "prioridade": 6,
            "contexto": {"score_medio": round(media, 2), "melhor_canal": melhor_canal},
        })
    return recomendacoes


def executar_insight(app_flask) -> bool:
    """
    Fluxo completo: coleta métricas -> classifica -> gera recomendações -> persiste e audita.
    Retorna True se executou sem falha crítica; em exceção registra auditoria e retorna False.
    """
    if not _insight_enabled():
        logger.debug("Customer Insight desabilitado (INSIGHT_ENABLED=false).")
        return True

    with app_flask.app_context():
        try:
            # 1. Coleta de métricas (Julia)
            from app.run_julia_agente_metricas import coletar_metricas_por_canal
            num_metricas = coletar_metricas_por_canal(app_flask)

            # 2. Ler insights na janela
            limite = _utcnow_naive() - timedelta(days=_janela_dias())
            insights = (
                InsightCanal.query.filter(InsightCanal.coletado_em >= limite)
                .order_by(InsightCanal.score_performance.desc())
                .all()
            )
            if not insights:
                auditoria_registrar(
                    tipo_decisao="insight",
                    decisao="Insight sem dados na janela",
                    contexto={"janela_dias": _janela_dias(), "registros_coletados": num_metricas},
                    resultado="ignorado",
                )
                return True

            # 3. Classificar
            classificacoes: dict[tuple[int, str], str] = {}
            for ins in insights:
                classificacoes[(ins.noticia_id, ins.canal)] = classificar_desempenho(
                    ins.score_performance or 0, ins.impressoes or 0
                )

            # 4. Gerar recomendações
            lista_rec = gerar_recomendacoes(insights, classificacoes)

            # 5. Persistir RecomendacaoEstrategica
            criado_em = _utcnow_naive()
            for rec in lista_rec:
                try:
                    r = RecomendacaoEstrategica(
                        contexto_json=json.dumps(rec.get("contexto", {}), ensure_ascii=False),
                        recomendacao=json.dumps({
                            "tema_sugerido": rec.get("tema_sugerido"),
                            "tipo": rec.get("tipo_conteudo"),
                            "canal_preferencial": rec.get("canal_preferencial"),
                            "horario_sugerido": rec.get("horario_sugerido"),
                            "frequencia_sugerida": rec.get("frequencia_sugerida"),
                            "tipo_recomendacao": rec.get("tipo_recomendacao"),
                        }, ensure_ascii=False),
                        prioridade=rec.get("prioridade", 5),
                        status="pendente",
                        criado_em=criado_em,
                    )
                    db.session.add(r)
                except Exception as e:
                    logger.warning("Falha ao persistir recomendação: %s", e)
            db.session.commit()

            # 6. Auditoria
            auditoria_registrar(
                tipo_decisao="insight",
                decisao="Customer Insight processado",
                contexto={
                    "insights_analisados": len(insights),
                    "recomendacoes_geradas": len(lista_rec),
                    "classificacoes": {f"{k[0]}_{k[1]}": v for k, v in list(classificacoes.items())[:10]},
                },
                resultado="sucesso",
                detalhe=f"{len(lista_rec)} recomendações persistidas",
            )
            logger.info("Customer Insight: %d insights, %d recomendações.", len(insights), len(lista_rec))
            return True
        except Exception as e:
            logger.exception("Falha no Customer Insight: %s", e)
            try:
                db.session.rollback()
            except Exception:
                pass
            auditoria_registrar(
                tipo_decisao="insight",
                decisao="Customer Insight falhou",
                contexto={},
                resultado="falha",
                detalhe=str(e),
            )
            return False


def obter_recomendacoes_pendentes(app_flask, limite: int = 20) -> list[RecomendacaoEstrategica]:
    """Retorna recomendações com status=pendente para uso no dispatch (futuras decisões do Cleiton)."""
    with app_flask.app_context():
        return listar_recomendacoes_pendentes(limite)


def listar_recomendacoes_pendentes(limite: int = 20) -> list[RecomendacaoEstrategica]:
    """
    Lista recomendações pendentes ordenadas por prioridade DESC, criado_em DESC.
    Deve ser chamada dentro de app_context.
    """
    return (
        RecomendacaoEstrategica.query.filter_by(status="pendente")
        .order_by(RecomendacaoEstrategica.prioridade.desc(), RecomendacaoEstrategica.criado_em.desc())
        .limit(max(1, min(100, limite)))
        .all()
    )


def selecionar_recomendacao_prioritaria() -> RecomendacaoEstrategica | None:
    """
    Retorna a recomendação pendente de maior prioridade (prioridade DESC, criado_em DESC).
    Deve ser chamada dentro de app_context.
    """
    return (
        RecomendacaoEstrategica.query.filter_by(status="pendente")
        .order_by(RecomendacaoEstrategica.prioridade.desc(), RecomendacaoEstrategica.criado_em.desc())
        .first()
    )


def parse_recomendacao_json(texto: str | None) -> dict[str, Any]:
    """Parse seguro do JSON de recomendação. Retorna dict vazio se inválido."""
    if not texto or not texto.strip():
        return {}
    try:
        out = json.loads(texto)
        return out if isinstance(out, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def parse_contexto_json(texto: str | None) -> dict[str, Any]:
    """Parse seguro do JSON de contexto. Retorna dict vazio se inválido."""
    return parse_recomendacao_json(texto)


def atualizar_status_recomendacao(
    recomendacao_id: int,
    novo_status: str,
    app_flask,
    detalhe: str | None = None,
) -> bool:
    """
    Atualiza status da recomendação (pendente|aplicada|descartada).
    Toda mudança gera auditoria com tipo_decisao=insight.
    Retorna True se atualizou com sucesso.
    """
    if novo_status not in ("pendente", "aplicada", "descartada"):
        logger.warning("Status de recomendação inválido: %s", novo_status)
        return False
    with app_flask.app_context():
        try:
            rec = db.session.get(RecomendacaoEstrategica, recomendacao_id)
            if not rec:
                logger.warning("Recomendação id=%s não encontrada.", recomendacao_id)
                return False
            status_anterior = rec.status or "pendente"
            rec.status = novo_status
            db.session.commit()
            auditoria_registrar(
                tipo_decisao="insight",
                decisao=f"Recomendação {novo_status}",
                contexto={"recomendacao_id": recomendacao_id, "de": status_anterior, "para": novo_status},
                resultado="sucesso",
                detalhe=detalhe,
            )
            logger.info("Recomendação id=%s: %s -> %s", recomendacao_id, status_anterior, novo_status)
            return True
        except Exception as e:
            logger.exception("Falha ao atualizar status da recomendação %s: %s", recomendacao_id, e)
            try:
                db.session.rollback()
            except Exception:
                pass
            auditoria_registrar(
                tipo_decisao="insight",
                decisao="Falha ao atualizar recomendação",
                contexto={"recomendacao_id": recomendacao_id, "novo_status": novo_status},
                resultado="falha",
                detalhe=str(e),
            )
            return False
