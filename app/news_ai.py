import os
import json
import logging
from datetime import datetime

from app.models import NoticiaPortal, Pauta
from app.services.newsletter_subscription_service import (
    STATUS_ALREADY_ACTIVE,
    STATUS_CREATED,
    STATUS_INVALID,
    STATUS_REACTIVATED,
    SOURCE_PUBLIC_NEWSLETTER,
    subscribe,
)

logger = logging.getLogger(__name__)

# --- LÓGICA DE NEWSLETTER (não cria Lead) ---


def registrar_newsletter_subscription(email):
    """
    Inscreve e-mail na newsletter pública.

    Não cria nem altera Lead. Não toca campanha, opt-out nem CommunicationSuppression.
    """
    try:
        result = subscribe(email, source=SOURCE_PUBLIC_NEWSLETTER, commit=True)
    except Exception:
        logger.exception("newsletter_subscription public register failed")
        return False, "Erro interno ao processar cadastro."

    if result.status == STATUS_INVALID:
        return False, "E-mail é obrigatório."
    if result.status == STATUS_ALREADY_ACTIVE:
        return True, "Você já está na nossa lista de inteligência!"
    if result.status in (STATUS_CREATED, STATUS_REACTIVATED):
        return True, "Bem-vindo à LogTech! Sua inscrição foi confirmada."
    logger.error("newsletter_subscription public register status inesperado")
    return False, "Erro interno ao processar cadastro."

# --- LÓGICA DE NOTÍCIAS E BLOG ---
def buscar_noticias_portal():
    """
    Retorna as 10 notícias mais recentes processadas pela Júlia.
    """
    try:
        # Contrato público: apenas conteúdos efetivamente publicados no portal.
        return (
            NoticiaPortal.query.filter(
                NoticiaPortal.publicado_em.isnot(None),
                NoticiaPortal.status_publicacao.in_(["publicado", "parcial"]),
            )
            .order_by(NoticiaPortal.data_publicacao.desc())
            .limit(10)
            .all()
        )
    except Exception as e:
        logger.error(f"Erro ao buscar notícias para o portal: {e}")
        return []

def processar_ciclo_noticias():
    """
    Será chamada pelo Cleiton para varredura RSS/coleta.
    Preenche a tabela Pauta para o pipeline da Júlia consumir.
    """
    logger.info("Cleiton: varredura de RSS para o Portal (preenche Pauta).")
    # Futuro: feedparser + curadoria → Pauta.query.add(...)
    pass


def popular_pautas_de_arquivo_json(caminho: str | None = None, tipo_padrao: str = "noticia") -> int:
    """
    Importa pautas de um arquivo no formato legado processadas.json
    (dict[link, {titulo_original, fonte}]) para a tabela Pauta.
    Retorna quantidade inserida. Idempotente: não duplica por link.
    Use uma vez para migrar ou semear pautas antes do pipeline.
    """
    if not caminho:
        base = os.path.dirname(os.path.abspath(__file__))
        caminho = os.path.join(base, "processadas.json")
    if not os.path.exists(caminho):
        logger.warning("Arquivo de pautas não encontrado: %s", caminho)
        return 0
    try:
        with open(caminho, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.exception("Erro ao ler JSON de pautas: %s", e)
        return 0
    inseridas = 0
    for link, info in (data.items() if isinstance(data, dict) else []):
        if not link or not isinstance(info, dict):
            continue
        if Pauta.query.filter_by(link=link).first():
            continue
        titulo = (info.get("titulo_original") or "").strip() or link[:200]
        fonte = (info.get("fonte") or "").strip()
        p = Pauta(
            titulo_original=titulo,
            fonte=fonte,
            link=link,
            tipo=tipo_padrao,
            status="pendente",
        )
        db.session.add(p)
        inseridas += 1
    if inseridas:
        db.session.commit()
        logger.info("Pautas importadas: %d de %s", inseridas, caminho)
    return inseridas