"""
Cleiton - Agente Scout: coleta de pautas a partir de fontes configuradas (RSS/API/URL).
Normaliza e insere em Pauta sem duplicar por link. Não publica conteúdo; não redige.
"""
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from app.extensions import db
from app.models import Pauta, NoticiaPortal
from app.run_cleiton_agente_auditoria import registrar as auditoria_registrar

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _scout_enabled() -> bool:
    return os.getenv("SCOUT_ENABLED", "true").strip().lower() in ("true", "1", "t", "yes")


def _scout_max_itens() -> int:
    try:
        return max(1, min(100, int(os.getenv("SCOUT_MAX_ITENS_POR_CICLO", "20").strip())))
    except ValueError:
        return 20


def _scout_sources() -> list[dict]:
    """Lista de fontes a partir de SCOUT_SOURCES_JSON (JSON array) ou default seguro."""
    raw = os.getenv("SCOUT_SOURCES_JSON", "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning("SCOUT_SOURCES_JSON inválido; ignorando fontes.")
        return []


def _link_canonico(url: str) -> str:
    """Normaliza URL para comparação (sem fragmento, lowercase host)."""
    if not url or not isinstance(url, str):
        return ""
    url = url.strip()
    try:
        p = urlparse(url)
        netloc = (p.netloc or "").lower()
        path = (p.path or "/").rstrip("/") or "/"
        return f"{p.scheme or 'https'}://{netloc}{path}" + (f"?{p.query}" if p.query else "")
    except Exception:
        return url


def _hash_conteudo(link: str, titulo: str) -> str:
    return hashlib.sha256((link + "|" + (titulo or "")).encode("utf-8")).hexdigest()


def _link_ja_existe(link: str) -> bool:
    """True se link já está em Pauta ou NoticiaPortal."""
    canon = _link_canonico(link)
    if not canon:
        return True
    if Pauta.query.filter_by(link=canon).first():
        return True
    if NoticiaPortal.query.filter_by(link=canon).first():
        return True
    return False


def _coletar_rss(url: str, max_itens: int, tipo_sugerido: str, fonte_tipo: str = "rss") -> list[dict]:
    """
    Coleta itens de um feed RSS (inclui feeds de Google Alerts).
    Retorna lista de dicts normalizados, anotando o fonte_tipo (rss | google_alerts_rss | ...).
    Lança exceção em caso de dependência ausente, erro de rede ou parse inválido.
    """
    try:
        import feedparser
    except ImportError as e:
        raise RuntimeError("dependencia_feedparser_ausente") from e

    feed = feedparser.parse(url, request_headers={"User-Agent": "LogCompleta-Scout/1.0"})
    if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
        raise RuntimeError("parse_invalido_rss")

    out = []
    for e in (feed.entries or [])[:max_itens]:
        link = (e.get("link") or "").strip()
        if not link:
            continue
        titulo = (e.get("title") or "").strip() or "Sem título"
        # fonte pode ser o nome do feed ou o domínio
        fonte = getattr(feed.feed, "title", None) or urlparse(link).netloc or "RSS"
        if isinstance(fonte, str):
            fonte = fonte[:200]
        else:
            fonte = str(fonte)[:200]
        out.append({
            "titulo_original": titulo[:500],
            "fonte": fonte,
            "link": _link_canonico(link),
            "tipo": (tipo_sugerido or "noticia").lower() in ("artigo", "noticia") and tipo_sugerido.lower() or "noticia",
            "fonte_tipo": (fonte_tipo or "rss")[:30],
        })
    return out


def _coletar_url_lista(url: str, max_itens: int, tipo_sugerido: str) -> list[dict]:
    """Coleta de API/URL que retorna JSON com lista de itens (chave 'items' ou array).
    Lança exceção em caso de dependência ausente, erro HTTP/rede ou parse inválido.
    """
    try:
        import requests
    except ImportError as e:
        raise RuntimeError("dependencia_requests_ausente") from e

    r = requests.get(url, timeout=15, headers={"User-Agent": "LogCompleta-Scout/1.0"})
    r.raise_for_status()
    data = r.json()
    items = data if isinstance(data, list) else (data.get("items") or data.get("entries") or [])
    if not isinstance(items, list):
        raise RuntimeError("parse_invalido_api")
    out = []
    for e in items[:max_itens]:
        if not isinstance(e, dict):
            continue
        link = (e.get("link") or e.get("url") or "").strip()
        if not link:
            continue
        titulo = (e.get("title") or e.get("titulo") or "").strip() or "Sem título"
        fonte = (e.get("fonte") or e.get("source") or urlparse(link).netloc or "API")[:200]
        out.append({
            "titulo_original": titulo[:500],
            "fonte": fonte,
            "link": _link_canonico(link),
            "tipo": (tipo_sugerido or "noticia").lower() in ("artigo", "noticia") and tipo_sugerido.lower() or "noticia",
            "fonte_tipo": "api",
        })
    return out


def _detalhe_erro_fonte(exc: Exception) -> str:
    """Gera detalhe resumido para diagnóstico de falha por fonte."""
    if exc is None:
        return "Erro desconhecido ao processar a fonte."
    msg_low = str(exc).lower()
    nome = exc.__class__.__name__

    if "dependencia_feedparser_ausente" in msg_low or "feedparser" in msg_low:
        return "Dependência ausente: feedparser."
    if "dependencia_requests_ausente" in msg_low or "requests" in msg_low:
        return "Dependência ausente: requests."
    if "timeout" in msg_low:
        return "Erro de rede/timeout ao acessar a fonte."
    if "parse_invalido" in msg_low or "jsondecodeerror" in msg_low:
        return "Falha ao interpretar o conteúdo da fonte."
    if "http" in msg_low or "status code" in msg_low or "404" in msg_low or "500" in msg_low:
        return "Erro HTTP/rede ao acessar a fonte."
    return f"Erro ao processar a fonte ({nome})."


def _inserir_pauta(item: dict) -> bool:
    """Insere uma pauta se o link não existir. Retorna True se inseriu."""
    link = (item.get("link") or "").strip()
    if not link:
        return False
    if _link_ja_existe(link):
        return False
    titulo = (item.get("titulo_original") or "").strip() or "Sem título"
    fonte = (item.get("fonte") or "")[:200]
    tipo = (item.get("tipo") or "noticia").lower()
    if tipo not in ("noticia", "artigo"):
        tipo = "noticia"
    fonte_tipo = (item.get("fonte_tipo") or "manual")[:30]
    agora = _utcnow_naive()
    try:
        p = Pauta(
            titulo_original=titulo,
            fonte=fonte,
            link=link,
            tipo=tipo,
            status="pendente",
            status_verificacao="pendente",
            fonte_tipo=fonte_tipo,
            hash_conteudo=_hash_conteudo(link, titulo),
            coletado_em=agora,
            created_at=agora,
        )
        db.session.add(p)
        db.session.commit()
        return True
    except Exception as e:
        logger.exception("Falha ao inserir pauta: %s", e)
        db.session.rollback()
        return False


def executar_coleta() -> dict[str, Any]:
    """
    Executa ciclo de coleta: lê fontes configuradas, normaliza e insere em Pauta.
    Retorna dict com (chaves legadas + novas):
      - inseridas
      - ignoradas_duplicata
      - erros
      - fontes_processadas
      - fontes_com_erro
      - fontes_sem_itens
      - fontes_com_itens
      - diagnostico_fontes (lista de diagnósticos por fonte)
    """
    if not _scout_enabled():
        logger.info("Scout desabilitado (SCOUT_ENABLED=false).")
        resultado = {
            "inseridas": 0,
            "ignoradas_duplicata": 0,
            "erros": 0,
            "fontes_processadas": 0,
            "fontes_com_erro": 0,
            "fontes_sem_itens": 0,
            "fontes_com_itens": 0,
            "diagnostico_fontes": [],
        }
        auditoria_registrar(
            tipo_decisao="scout",
            decisao="Scout desabilitado por configuração",
            contexto=resultado,
            resultado="ignorado",
            detalhe="SCOUT_ENABLED=false",
        )
        return resultado
    sources = _scout_sources()
    if not sources:
        logger.info("Scout: nenhuma fonte configurada (SCOUT_SOURCES_JSON).")
        resultado = {
            "inseridas": 0,
            "ignoradas_duplicata": 0,
            "erros": 0,
            "fontes_processadas": 0,
            "fontes_com_erro": 0,
            "fontes_sem_itens": 0,
            "fontes_com_itens": 0,
            "diagnostico_fontes": [],
        }
        auditoria_registrar(
            tipo_decisao="scout",
            decisao="Scout sem fontes configuradas",
            contexto=resultado,
            resultado="ignorado",
            detalhe="SCOUT_SOURCES_JSON vazio",
        )
        return resultado
    max_itens = _scout_max_itens()
    inseridas = 0
    ignoradas = 0
    erros = 0
    fontes_com_erro = 0
    fontes_sem_itens = 0
    fontes_com_itens = 0
    diagnostico_fontes: list[dict] = []
    for src in sources:
        if not isinstance(src, dict):
            erros += 1
            fontes_com_erro += 1
            diagnostico_fontes.append({
                "url": "",
                "tipo_fonte": "desconhecido",
                "status": "erro",
                "detalhe_resumido": "Fonte inválida: item não é objeto JSON.",
                "itens_coletados": 0,
            })
            continue
        url = (src.get("url") or src.get("href") or "").strip()
        if not url:
            erros += 1
            fontes_com_erro += 1
            diagnostico_fontes.append({
                "url": "",
                "tipo_fonte": (src.get("tipo_fonte") or src.get("fonte_tipo") or "desconhecido").strip().lower() or "desconhecido",
                "status": "erro",
                "detalhe_resumido": "Fonte inválida: URL ausente.",
                "itens_coletados": 0,
            })
            continue
        tipo_sug = (src.get("tipo") or "noticia").strip().lower() or "noticia"
        tipo_fonte = (src.get("tipo_fonte") or src.get("fonte_tipo") or "rss").strip().lower()

        diag = {
            "url": url,
            "tipo_fonte": tipo_fonte or "desconhecido",
            "status": "nao_processada",
            "detalhe_resumido": "",
            "itens_coletados": 0,
        }

        try:
            if tipo_fonte == "api":
                itens = _coletar_url_lista(url, max_itens, tipo_sug)
            elif tipo_fonte in ("google_alerts_rss", "google-alerts-rss", "google_alerts"):
                # Feed RSS de Google Alerts Notícias (sem scraping HTML frágil)
                itens = _coletar_rss(url, max_itens, tipo_sug, fonte_tipo="google_alerts_rss")
            else:
                itens = _coletar_rss(url, max_itens, tipo_sug, fonte_tipo="rss")

            qtd_itens = len(itens)
            if qtd_itens == 0:
                diag["status"] = "sem_itens"
                diag["detalhe_resumido"] = "Fonte processada, mas sem itens retornados."
                fontes_sem_itens += 1
            else:
                diag["status"] = "ok"
                diag["detalhe_resumido"] = f"{qtd_itens} itens retornados pela fonte."
                diag["itens_coletados"] = qtd_itens
                fontes_com_itens += 1

            for item in itens:
                if _link_ja_existe(item.get("link") or ""):
                    ignoradas += 1
                    continue
                if _inserir_pauta(item):
                    inseridas += 1
                else:
                    ignoradas += 1
        except Exception as e:
            logger.exception("Scout falha na fonte %s: %s", url[:80], e)
            erros += 1
            fontes_com_erro += 1
            diag["status"] = "erro"
            diag["detalhe_resumido"] = _detalhe_erro_fonte(e)
            diag["itens_coletados"] = 0

        diagnostico_fontes.append(diag)

    resultado = {
        "inseridas": inseridas,
        "ignoradas_duplicata": ignoradas,
        "erros": erros,
        "fontes_processadas": len(sources),
        "fontes_com_erro": fontes_com_erro,
        "fontes_sem_itens": fontes_sem_itens,
        "fontes_com_itens": fontes_com_itens,
        "diagnostico_fontes": diagnostico_fontes,
    }
    auditoria_registrar(
        tipo_decisao="scout",
        decisao=f"Coleta Scout: {inseridas} pautas inseridas, {ignoradas} ignoradas",
        contexto=resultado,
        resultado="sucesso" if erros == 0 else "falha",
        detalhe=str(resultado),
    )
    logger.info("Scout: %s", resultado)
    return resultado
