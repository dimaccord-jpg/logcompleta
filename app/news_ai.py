import os
import logging
from datetime import datetime
from app.extensions import db
from app.models import Lead, NoticiaPortal # Certifique-se que NoticiaPortal está no models.py

# Configuração de Log para acompanhar as ações do Cleiton
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
    Esta função será chamada pelo run_cleiton.py 3x ao dia.
    Ela vai varrer o RSS, passar pelo crivo da IA e salvar no banco.
    """
    logger.info("🤖 Cleiton iniciando varredura de RSS para o Portal...")
    # Aqui entrará a lógica que você já tem de feedparser + Gemini
    # Mas agora, em vez de processadas.json, salvaremos no banco de dados.
    pass