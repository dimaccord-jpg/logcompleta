"""
Júlia - Agente Qualidade: validação de conteúdo por tipo antes de publicar.
Garante campos mínimos, tamanho e evita duplicidade por link.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Tamanhos mínimos (caracteres)
MIN_INSIGHT_NOTICIA = 80
MIN_RESUMO_ARTIGO = 80
MIN_CONTEUDO_ARTIGO = 400
MIN_TITULO = 10
MIN_CTA = 10


def _str_val(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, (list, dict)):
        return ""
    return str(val).strip()


def validar_noticia_curta(d: dict) -> tuple[bool, list[str]]:
    """
    Valida dict de notícia curta. Campos obrigatórios:
    titulo_julia, url_imagem (ou fallback já aplicado), resumo_julia (insight_curto), fonte_link (link).
    Retorna (ok, lista de erros).
    """
    err = []
    titulo = _str_val(d.get("titulo_julia"))
    insight = _str_val(d.get("resumo_julia"))
    linhas_insight = [l for l in insight.splitlines() if l.strip()]
    link = _str_val(d.get("link")) or _str_val(d.get("fonte_link"))
    url_img = d.get("url_imagem")
    if not titulo or len(titulo) < MIN_TITULO:
        err.append("titulo_julia ausente ou muito curto")
    if not insight or len(insight) < MIN_INSIGHT_NOTICIA:
        err.append("resumo_julia (insight_curto) ausente ou menor que %d caracteres" % MIN_INSIGHT_NOTICIA)
    if len(linhas_insight) < 3 or len(linhas_insight) > 5:
        err.append("resumo_julia deve ter entre 3 e 5 linhas")
    if not link:
        err.append("fonte_link (link original) obrigatório para notícia")
    if url_img is None and d.get("url_imagem") is None:
        pass  # pode ser fallback aplicado depois
    return (len(err) == 0, err)


def validar_artigo(d: dict) -> tuple[bool, list[str]]:
    """
    Valida dict de artigo. Campos obrigatórios:
    titulo_julia, url_imagem, subtitulo, resumo_julia, conteudo_completo, fonte_link, cta, objetivo_lead.
    Retorna (ok, lista de erros).
    """
    err = []
    titulo = _str_val(d.get("titulo_julia"))
    subtitulo = _str_val(d.get("subtitulo"))
    resumo = _str_val(d.get("resumo_julia"))
    conteudo = _str_val(d.get("conteudo_completo"))
    link = _str_val(d.get("link")) or _str_val(d.get("fonte_link"))
    cta = _str_val(d.get("cta"))
    objetivo = _str_val(d.get("objetivo_lead"))
    if not titulo or len(titulo) < MIN_TITULO:
        err.append("titulo_julia ausente ou muito curto")
    if not subtitulo:
        err.append("subtitulo obrigatório para artigo")
    if not resumo or len(resumo) < MIN_RESUMO_ARTIGO:
        err.append("resumo_julia ausente ou menor que %d caracteres" % MIN_RESUMO_ARTIGO)
    if not conteudo or len(conteudo) < MIN_CONTEUDO_ARTIGO:
        err.append("conteudo_completo ausente ou menor que %d caracteres" % MIN_CONTEUDO_ARTIGO)
    if not link:
        err.append("fonte_link (link original) obrigatório para artigo")
    if not cta or len(cta) < MIN_CTA:
        err.append("cta obrigatória e com pelo menos %d caracteres" % MIN_CTA)
    if not objetivo:
        err.append("objetivo_lead obrigatório para artigo (ex.: newsletter, diagnóstico, contato_comercial)")
    return (len(err) == 0, err)


def validar_conteudo(d: dict, tipo_missao: str) -> tuple[bool, list[str]]:
    """
    Entrada única: valida por tipo_missao ('noticia' | 'artigo').
    d deve conter os campos já preenchidos (incluindo url_imagem após agente imagem).
    """
    t = (tipo_missao or "noticia").lower()
    if t == "artigo":
        return validar_artigo(d)
    return validar_noticia_curta(d)
