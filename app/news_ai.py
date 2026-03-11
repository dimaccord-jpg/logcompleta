import os
import json
import logging
from datetime import datetime
from app.extensions import db
from app.models import Lead, NoticiaPortal, Pauta

logger = logging.getLogger(__name__)

# --- LÓGICA DE LEADS (NEWSLETTER) ---

def registrar_lead_newsletter(email):
    """
    Gerencia a entrada de novos leads no leads.db
    """
    if not email:
        return False, "E-mail é obrigatório."
    
    try:
        # Verifica se o lead já existe
        existe = Lead.query.filter_by(email=email).first()
        if existe:
            return True, "Você já está na nossa lista de inteligência!"
        
        novo_lead = Lead(email=email)
        db.session.add(novo_lead)
        db.session.commit()
        logger.info(f"✅ NOVO LEAD: {email} cadastrado com sucesso.")
        return True, "Bem-vindo à LogTech! Sua inscrição foi confirmada."
    
    except Exception as e:
        db.session.rollback()
        logger.error(f"❌ ERRO AO SALVAR LEAD: {e}")
        return False, "Erro interno ao processar cadastro."

# --- LÓGICA DE NOTÍCIAS E BLOG ---
def buscar_noticias_portal():
    """
    Retorna as 10 notícias mais recentes processadas pela Júlia.
    """
    try:
        # Buscamos apenas as 10 últimas para manter a performance
        return NoticiaPortal.query.order_by(NoticiaPortal.data_publicacao.desc()).limit(10).all()
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