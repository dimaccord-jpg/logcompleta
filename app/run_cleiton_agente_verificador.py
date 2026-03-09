"""
Cleiton - Agente Verificador: classifica confiabilidade da pauta (fonte, link, duplicidade, ruído).
Define score, status_verificacao (aprovado | revisar | rejeitado) e motivo. Só aprovadas vão para Julia.
"""
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from difflib import SequenceMatcher

from app.extensions import db
from app.models import Pauta, NoticiaPortal
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

STATUS_APROVADO = "aprovado"
STATUS_REVISAR = "revisar"
STATUS_REJEITADO = "rejeitado"
STATUS_PENDENTE = "pendente"


def _score_minimo() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("VERIFICADOR_SCORE_MINIMO", "0.5").strip())))
    except ValueError:
        return 0.5


def _termos_relevantes() -> list[str]:
    """
    Termos de relevância simples para logística/supply chain/frete, configuráveis por env.
    VERIFICADOR_TERMOS_RELEVANTES: lista separada por vírgula.
    """
    raw = os.getenv("VERIFICADOR_TERMOS_RELEVANTES", "").strip()
    if not raw:
        # Fallback seguro; pode ser ajustado por ambiente sem alterar código.
        raw = "logistica,logístico,logística,frete,fretes,supply chain,supply-chain,cadeia de suprimentos,transportes,roteirizacao,última milha,ultima milha"
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _limites_recencia_horas() -> tuple[int, int]:
    """
    Limites de recência em horas para pontuação:
    - quente: até VERIFICADOR_RECENCIA_HORAS_QUENTE (default 24h)
    - aceitável: até VERIFICADOR_RECENCIA_HORAS_ACEITAVEL (default 72h)
    """
    def _parse_env(name: str, default: int) -> int:
        try:
            return max(1, int(os.getenv(name, str(default)).strip()))
        except ValueError:
            return default

    quente = _parse_env("VERIFICADOR_RECENCIA_HORAS_QUENTE", 24)
    aceitavel = _parse_env("VERIFICADOR_RECENCIA_HORAS_ACEITAVEL", 72)
    if aceitavel < quente:
        aceitavel = quente
    return quente, aceitavel


def _normalizar_dominio(dominio: str) -> str:
    """
    Normaliza domínio para comparação:
    - lowercase
    - remove prefixo www.
    """
    d = (dominio or "").strip().lower()
    if d.startswith("www."):
        d = d[4:]
    return d


def _fontes_confiaveis() -> list[str]:
    """Lista de domínios ou nomes de fonte considerados confiáveis (separados por vírgula)."""
    raw = os.getenv("VERIFICADOR_FONTES_CONFIAVEIS", "").strip()
    if not raw:
        return []
    return [_normalizar_dominio(x) for x in raw.split(",") if x.strip()]


def _dominios_bloqueados() -> list[str]:
    raw = os.getenv("VERIFICADOR_BLOQUEAR_DOMINIOS", "").strip()
    if not raw:
        return []
    return [_normalizar_dominio(x) for x in raw.split(",") if x.strip()]


def _limiar_similaridade() -> float:
    try:
        return max(0.0, min(1.0, float(os.getenv("VERIFICADOR_SIMILARIDADE_TITULO", "0.85").strip())))
    except ValueError:
        return 0.85


def _dominio_confiavel(dominio: str, fontes_ok: list[str]) -> bool:
    """Retorna True quando domínio está coberto pela whitelist (exato ou subdomínio)."""
    if not dominio or not fontes_ok:
        return False
    for fonte in fontes_ok:
        if dominio == fonte or dominio.endswith("." + fonte):
            return True
    return False


def _dominio_do_link(link: str) -> str:
    if not link:
        return ""
    try:
        netloc = (urlparse(link).netloc or "").lower()
        return _normalizar_dominio(netloc)
    except Exception:
        return ""


def _link_valido(link: str) -> bool:
    if not link or not isinstance(link, str):
        return False
    link = link.strip()
    if len(link) < 10:
        return False
    if not re.match(r"^https?://", link, re.IGNORECASE):
        return False
    try:
        p = urlparse(link)
        return bool(p.netloc)
    except Exception:
        return False


def _titulo_similar_existente(titulo: str, link: str, pauta_id: int | None) -> tuple[bool, float]:
    """
    Verifica se já existe pauta ou notícia com título muito similar.
    Retorna (similar_encontrado, maior_ratio).
    """
    if not titulo or len(titulo) < 5:
        return False, 0.0
    limiar = _limiar_similaridade()
    titulo_norm = (titulo or "").strip().lower()
    maior = 0.0
    # Pautas (outras que não esta)
    for p in Pauta.query.filter(Pauta.id != pauta_id).all():
        t = (p.titulo_original or "").strip().lower()
        if t:
            r = SequenceMatcher(None, titulo_norm, t).ratio()
            maior = max(maior, r)
            if r >= limiar:
                return True, r
    # Noticias já publicadas
    for n in NoticiaPortal.query.all():
        t = (n.titulo_original or n.titulo_julia or "").strip().lower()
        if t:
            r = SequenceMatcher(None, titulo_norm, t).ratio()
            maior = max(maior, r)
            if r >= limiar:
                return True, r
    return False, maior


def _calcular_score_e_decisao(pauta: Pauta) -> tuple[float, str, str]:
    """
    Calcula score (0.0 a 1.0), status (aprovado | revisar | rejeitado) e motivo.
    Regras: link inválido -> rejeitado; domínio bloqueado -> rejeitado; fonte não confiável reduz score;
    título similar -> revisar ou rejeitado conforme config.
    """
    motivo_partes = []
    score = 1.0
    link = (pauta.link or "").strip()
    titulo = (pauta.titulo_original or "").strip()
    fonte = (pauta.fonte or "").strip()
    dominio = _dominio_do_link(link)
    fontes_ok = _fontes_confiaveis()
    bloqueados = _dominios_bloqueados()
    limiar_sim = _limiar_similaridade()
    score_min = _score_minimo()

    if not _link_valido(link):
        return 0.0, STATUS_REJEITADO, "Link inválido ou ausente."

    if dominio and dominio in bloqueados:
        return 0.0, STATUS_REJEITADO, f"Domínio bloqueado: {dominio}"

    if dominio and fontes_ok:
        # aceita equivalência exata e subdomínios do domínio raiz listado como confiável
        if not _dominio_confiavel(dominio, fontes_ok):
            # Fonte não está na lista de confiáveis: reduz score
            score -= 0.2
            motivo_partes.append("fonte não listada como confiável")

    # Relevância por termos simples (logística/supply chain/frete/etc.)
    termos = _termos_relevantes()
    texto_busca = f"{titulo} {fonte}".lower()
    if any(t in texto_busca for t in termos):
        # Pequeno bônus, sem ultrapassar 1.0
        score = min(1.0, score + 0.15)
        motivo_partes.append("contém termos de relevância logística")

    # Recência da pauta (apenas para notícias; artigos podem ser perenes)
    try:
        from app.models import utcnow_naive  # import local para evitar ciclos
    except Exception:
        utcnow_naive = None
    if getattr(pauta, "tipo", "noticia") == "noticia" and utcnow_naive is not None:
        agora = utcnow_naive()
        referencia = getattr(pauta, "coletado_em", None) or getattr(pauta, "created_at", None)
        if referencia is not None:
            delta_horas = (agora - referencia).total_seconds() / 3600.0
            quente_h, aceitavel_h = _limites_recencia_horas()
            if delta_horas <= quente_h:
                # notícia quente: reforça score (mantendo teto em 1.0)
                score = min(1.0, score + 0.2)
                motivo_partes.append("notícia recente (janela quente)")
            elif delta_horas <= aceitavel_h:
                # ok, mantém ou ajusta levemente
                motivo_partes.append("notícia em janela aceitável de recência")
            else:
                # muito antiga: reduz score
                score -= 0.25
                motivo_partes.append("notícia antiga fora da janela de recência")

    similar, ratio = _titulo_similar_existente(titulo, link, pauta.id)
    if similar:
        score -= 0.4
        motivo_partes.append(f"título similar a existente (ratio={ratio:.2f})")
        if ratio >= 0.95:
            return max(0.0, score), STATUS_REJEITADO, "Duplicidade semântica (título muito similar)."
        if score < score_min:
            return max(0.0, score), STATUS_REJEITADO, "; ".join(motivo_partes) or "Score abaixo do mínimo."
        return max(0.0, score), STATUS_REVISAR, "; ".join(motivo_partes) or "Revisar manualmente."

    score = max(0.0, min(1.0, score))
    if score < score_min:
        return score, STATUS_REJEITADO, "; ".join(motivo_partes) or "Score abaixo do mínimo."
    if score >= 0.8 and (not fontes_ok or _dominio_confiavel(dominio, fontes_ok)):
        return score, STATUS_APROVADO, "Aprovado."
    if score >= score_min:
        return score, STATUS_REVISAR, "; ".join(motivo_partes) or "Revisar."
    return score, STATUS_REJEITADO, "; ".join(motivo_partes) or "Rejeitado."


def verificar_pauta(pauta: Pauta) -> None:
    """Atualiza pauta com score, status_verificacao e motivo_verificacao."""
    score, status, motivo = _calcular_score_e_decisao(pauta)
    agora = _utcnow_naive()
    try:
        pauta.score_confiabilidade = round(score, 4)
        pauta.status_verificacao = status
        pauta.motivo_verificacao = (motivo or "")[:2000]
        pauta.verificado_em = agora
        db.session.commit()
        logger.debug("Pauta %s verificada: score=%.2f status=%s", pauta.id, score, status)
    except Exception as e:
        logger.exception("Falha ao atualizar verificação da pauta %s: %s", pauta.id, e)
        db.session.rollback()


def executar_verificacao(max_pautas: int = 50) -> dict[str, Any]:
    """
    Processa pautas com status_verificacao=pendente; atualiza score e status.
    Retorna dict com: processadas, aprovadas, revisar, rejeitadas.
    """
    try:
        pendentes = (
            Pauta.query.filter_by(status_verificacao=STATUS_PENDENTE)
            .order_by(Pauta.created_at.asc())
            .limit(max_pautas)
            .all()
        )
    except Exception as e:
        logger.warning("Verificador: falha ao listar pendentes (coluna pode não existir): %s", e)
        pendentes = []
    aprovadas = 0
    revisar = 0
    rejeitadas = 0
    for p in pendentes:
        verificar_pauta(p)
        if p.status_verificacao == STATUS_APROVADO:
            aprovadas += 1
        elif p.status_verificacao == STATUS_REVISAR:
            revisar += 1
        else:
            rejeitadas += 1
    resultado = {
        "processadas": len(pendentes),
        "aprovadas": aprovadas,
        "revisar": revisar,
        "rejeitadas": rejeitadas,
    }
    auditoria_registrar(
        tipo_decisao="verificador",
        decisao=f"Verificação: {aprovadas} aprovadas, {revisar} revisar, {rejeitadas} rejeitadas",
        contexto=resultado,
        resultado="sucesso",
        detalhe=str(resultado),
    )
    logger.info("Verificador: %s", resultado)
    return resultado
