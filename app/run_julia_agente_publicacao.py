"""
Júlia - Agente Publicação: persiste conteúdo validado em NoticiaPortal.
Normaliza tipos (str/list/dict/None) para evitar quebrar templates; define status_qualidade e origem_pauta.
"""
import json
import logging
from app.extensions import db
from app.models import NoticiaPortal
from app.run_julia_agente_imagem import normalizar_url_imagem

logger = logging.getLogger(__name__)


def _normalizar_texto(val) -> str | None:
    """Converte para string; nunca persiste list/dict bruto em coluna de texto."""
    if val is None:
        return None
    if isinstance(val, str):
        return val.strip() or None
    if isinstance(val, (list, dict)):
        return json.dumps(val, ensure_ascii=False) if val else None
    return str(val).strip() or None


def publicar(
    tipo: str,
    titulo_julia: str,
    link: str,
    fonte: str,
    resumo_julia: str | None = None,
    subtitulo: str | None = None,
    conteudo_completo: str | None = None,
    url_imagem: str | None = None,
    referencias: str | None = None,
    titulo_original: str | None = None,
    cta: str | None = None,
    objetivo_lead: str | None = None,
    origem_pauta: str | None = "pipeline",
    url_imagem_master: str | None = None,
    assets_canais_json: str | None = None,
    status_publicacao: str | None = "pendente",
) -> NoticiaPortal | None:
    """
    Cria registro em NoticiaPortal com campos normalizados (Fase 4: url_imagem_master, assets_canais_json, status_publicacao).
    Não publica se link já existir (evita duplicata). Publisher marcará publicado_em após publicar no portal.
    """
    link = _normalizar_texto(link)
    if not link:
        logger.error("Publicação abortada: link obrigatório.")
        return None
    if NoticiaPortal.query.filter_by(link=link).first():
        logger.warning("Publicação ignorada: link já existe (%s).", link[:80])
        return None
    titulo_julia = _normalizar_texto(titulo_julia) or "Sem título"
    url_imagem = normalizar_url_imagem(url_imagem)
    url_master = normalizar_url_imagem(url_imagem_master or url_imagem)
    assets_json = _normalizar_texto(assets_canais_json) if isinstance(assets_canais_json, str) else None
    if assets_canais_json is not None and isinstance(assets_canais_json, dict):
        assets_json = json.dumps(assets_canais_json, ensure_ascii=False)
    try:
        n = NoticiaPortal(
            tipo=tipo or "noticia",
            titulo_julia=titulo_julia,
            titulo_original=_normalizar_texto(titulo_original) or titulo_julia,
            link=link,
            fonte=_normalizar_texto(fonte) or "",
            resumo_julia=_normalizar_texto(resumo_julia),
            subtitulo=_normalizar_texto(subtitulo),
            conteudo_completo=_normalizar_texto(conteudo_completo),
            url_imagem=url_imagem or url_master,
            referencias=_normalizar_texto(referencias),
            cta=_normalizar_texto(cta),
            objetivo_lead=_normalizar_texto(objetivo_lead),
            status_qualidade="aprovado",
            origem_pauta=_normalizar_texto(origem_pauta) or "pipeline",
            url_imagem_master=url_master,
            assets_canais_json=assets_json,
            status_publicacao=(status_publicacao or "pendente").strip() or "pendente",
        )
        db.session.add(n)
        db.session.commit()
        logger.info("Publicação concluída: %s (id=%s)", n.titulo_julia[:50], n.id)
        return n
    except Exception as e:
        logger.exception("Falha ao publicar: %s", e)
        db.session.rollback()
        return None
