"""
Busca web contextual e restrita ao chat da Julia.

Objetivo: enriquecer respostas com links uteis quando a pergunta pede
contexto dinamico/tecnico. Falha em busca nao deve quebrar o chat.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_TRIGGER_TERMS = (
    "link",
    "links",
    "fonte",
    "fontes",
    "site",
    "sites",
    "documentacao",
    "documentação",
    "docs",
    "manual",
    "fornecedor",
    "fabricante",
    "api",
    "integra",
    "integracao",
    "integração",
    "norma",
    "regul",
    "cotacao",
    "cotação",
    "preco",
    "preço",
    "atual",
    "hoje",
    "tabela",
)

_TECHNICAL_QUESTION_TERMS = (
    "equipamento",
    "equipamentos",
    "maquina",
    "máquina",
    "maquinas",
    "máquinas",
    "ponte rolante",
    "empilhadeira",
    "esteira",
    "sorter",
    "transelevador",
    "paleteira",
    "rack",
    "doca",
    "docas",
    "cabine",
    "guindaste",
    "transportador",
    "automacao",
    "automação",
    "especificacao",
    "especificação",
    "especificacoes",
    "especificações",
)

_RESEARCH_INTENT_TERMS = (
    "criterio",
    "critério",
    "criterios",
    "critérios",
    "avaliar",
    "comparar",
    "comparacao",
    "comparação",
    "pesquisar",
    "pesquisa",
    "opcoes",
    "opções",
    "modelo",
    "modelos",
)

_LOGISTICS_TERMS = (
    "logistica",
    "logística",
    "supply",
    "chain",
    "frete",
    "transporte",
    "tms",
    "wms",
    "armazen",
    "roteir",
    "frota",
    "last mile",
    "dock",
    "fulfillment",
)

_STOPWORDS_PT = {
    "de", "da", "do", "das", "dos", "e", "em", "para", "com", "sem", "por",
    "sobre", "que", "qual", "quais", "como", "onde", "quando", "uma", "um",
    "no", "na", "nos", "nas", "a", "o", "as", "os", "ao", "aos", "à", "às",
    "ser", "sao", "são", "mais", "menos", "ou", "se", "nao", "não", "me",
    "quero", "preciso", "ajuda", "pode", "podem", "mostrar", "indicar",
}

_BLOCKED_URL_TERMS = (
    "mercadolivre",
    "amazon.",
    "shopee",
    "aliexpress",
    "pinterest",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "wikipedia.org/wiki/isbn",
    "/isbn",
)


def _tokenize(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]{3,}", (text or "").lower())
    return [t for t in raw if t not in _STOPWORDS_PT]


def _is_query_in_logistics_domain(query: str) -> bool:
    q = (query or "").lower()
    return any(term in q for term in _LOGISTICS_TERMS)


def _score_result_relevance(query: str, result: dict[str, str]) -> int:
    title = (result.get("title") or "").lower()
    snippet = (result.get("snippet") or "").lower()
    url = (result.get("url") or "").lower()
    text_blob = f"{title} {snippet} {url}"
    parsed = urlparse(url)
    domain = parsed.netloc.lower()

    score = 0
    if any(term in text_blob for term in _LOGISTICS_TERMS):
        score += 3
    if any(bad in text_blob for bad in _BLOCKED_URL_TERMS):
        score -= 4
    if any(k in domain for k in ("docs.", "developer.", "support.", "help.", "knowledgebase", "kb.")):
        score += 1
    if domain.endswith(".gov.br") or domain.endswith(".gov") or domain.endswith(".org") or domain.endswith(".edu"):
        score += 1

    query_tokens = _tokenize(query)
    hits = 0
    for token in query_tokens[:8]:
        if token in text_blob:
            hits += 1
    if hits >= 2:
        score += 2
    elif hits == 1:
        score += 1
    return score


def filter_relevant_links(query: str, links: list[dict[str, str]]) -> list[dict[str, str]]:
    """Mantem apenas resultados com aderencia clara ao tema consultado."""
    if not links:
        return []
    query_is_logistics = _is_query_in_logistics_domain(query)
    filtered: list[tuple[int, dict[str, str]]] = []
    for link in links:
        score = _score_result_relevance(query, link)
        blob = f"{(link.get('title') or '').lower()} {(link.get('snippet') or '').lower()} {(link.get('url') or '').lower()}"
        has_logistics_signal = any(term in blob for term in _LOGISTICS_TERMS)
        if query_is_logistics and not has_logistics_signal:
            continue
        if score < 2:
            continue
        filtered.append((score, link))
    filtered.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in filtered]


def should_search_web_for_question(question: str) -> bool:
    text = (question or "").strip().lower()
    if not text:
        return False
    if any(term in text for term in _TRIGGER_TERMS):
        return True

    has_technical_subject = any(term in text for term in _TECHNICAL_QUESTION_TERMS)
    has_research_intent = any(term in text for term in _RESEARCH_INTENT_TERMS)
    return has_technical_subject and has_research_intent


def _resolve_duckduckgo_redirect(url: str) -> str:
    parsed = urlparse(url or "")
    if "duckduckgo.com" not in parsed.netloc:
        return url
    query = parse_qs(parsed.query or "")
    target = query.get("uddg", [None])[0]
    if target:
        return unquote(target)
    return url


def search_web_links(query: str, *, max_results: int | None = None) -> list[dict[str, str]]:
    if not (query or "").strip():
        return []
    limit_env = (os.getenv("JULIA_CHAT_WEB_RESULTS_LIMIT") or "").strip()
    timeout_env = (os.getenv("JULIA_CHAT_WEB_TIMEOUT_SEC") or "").strip()
    try:
        limit = int(limit_env) if limit_env else 3
    except ValueError:
        limit = 3
    try:
        timeout = float(timeout_env) if timeout_env else 2.5
    except ValueError:
        timeout = 2.5
    limit = max(1, min(max_results or limit, 5))
    prefilter_target = max(limit + 3, min(limit * 3, 12))
    timeout = max(1.0, min(timeout, 6.0))

    try:
        response = requests.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; JuliaChat/1.0)"},
        )
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in soup.select(".result"):
            link_el = item.select_one(".result__a")
            if link_el is None:
                continue
            raw_href = (link_el.get("href") or "").strip()
            href = _resolve_duckduckgo_redirect(raw_href)
            parsed = urlparse(href)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                continue
            if href in seen:
                continue
            seen.add(href)
            snippet_el = item.select_one(".result__snippet")
            title = link_el.get_text(" ", strip=True)
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            results.append(
                {
                    "title": title[:180] if title else href,
                    "url": href,
                    "snippet": snippet[:320],
                }
            )
            if len(results) >= prefilter_target:
                break
        curated = filter_relevant_links(query, results)
        return curated[:limit]
    except Exception as exc:
        logger.warning("Julia web search failed: %s", exc)
        return []
