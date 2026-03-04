import time
import logging
import sys
import os
from app.extensions import db
from datetime import datetime
import json
from dotenv import load_dotenv

# --- CARREGAMENTO DE AMBIENTE ---
env_name = os.getenv('APP_ENV', 'dev')
load_dotenv(f'.env.{env_name}')

# Configuração de Logger local
logger = logging.getLogger(__name__)

# Importamos as funções principais dos seus agentes especializados
try:
    from app.news_ai import processar_ciclo_noticias
    from app.run_julia import processar_insight_do_momento
    # Caso o finance.py ainda não exista, mantemos o try para não quebrar o fluxo
    try:
        from app.finance import atualizar_indices
    except ImportError:
        atualizar_indices = None
except ImportError as e:
    logger.error(f"❌ Erro ao importar agentes: {e}")
    logger.warning("O sistema continuará rodando, mas as funções de IA podem estar indisponíveis.")
    # Define como None para evitar NameError, o código abaixo deve tratar isso
    processar_ciclo_noticias = None
    processar_insight_do_momento = None
    atualizar_indices = None

def coordenar_analise_frete(historico_ia, rota_str):
    """
    Interface de tempo real para análise de fretes.
    Chamada pelo brain.py quando um usuário clica em 'Analisar Rota'.
    """
    from app.run_roberto import roberto # Import local para quebrar o ciclo
    logger.info(f"🤖 GESTOR CLEITON: Recebendo solicitação de análise para {rota_str}")
    
    # 1. Carregamento dos índices (Pilar Financeiro)
    indices = {}
    
    # Garante que pegamos o indices.json da pasta APP, não importa de onde o comando foi rodado
    base_dir = os.path.dirname(os.path.abspath(__file__))
    caminho_indices = os.path.join(base_dir, 'indices.json')

    try:
        with open(caminho_indices, 'r', encoding='utf-8') as f:
            indices = json.load(f)
        logger.info("Contexto de mercado (indices.json) carregado com sucesso.")
    except Exception as e:
        logger.warning(f"⚠️ Cleiton: Falha ao ler índices de mercado: {e}")
        indices = {"historico": [], "ultima_atualizacao": "N/A"}

    # 2. Delegação para o Agente Roberto
    # Roberto agora decidirá a acurácia com base nas novas instruções de sistema
    try:
        insight = roberto.analisar_frete(historico_ia, indices, rota_str)
        logger.info(f"✅ Análise concluída pelo Roberto para a rota {rota_str}")
        return insight
    except Exception as e:
        logger.exception(f"❌ Erro crítico na análise do Roberto: {e}")
        return {
            "tendencia_macro": "Erro no Processamento",
            "acuracia_percentual": "0%",
            "previsao_texto": "Ocorreu um erro interno na orquestração da IA."
        }
# 1. FOCO NA LIMPEZA: Note que não há nenhum import de 'web' aqui em cima.

def executar_orquestracao(app_flask):
    """
    O Cleiton agora é agnóstico. Ele não sabe onde o web.py mora, 
    ele apenas recebe a 'energia' (contexto) para trabalhar.
    """
    logger.info("🤖 MAESTRO CLEITON: Iniciando ciclo de inteligência via Injeção de Dependência.")
    
    # 2. Alteração: Usa o parâmetro app_flask em vez do import global
    with app_flask.app_context():
        # 1. APRENDIZADO
        # IMPORT LOCAL (Lazy Loading): Só importa quando o contexto do app já existe
        from app.models import NoticiaPortal
        ultimos_temas = NoticiaPortal.query.order_by(NoticiaPortal.data_publicacao.desc()).limit(5).all()
        temas_vistos = [t.titulo_julia for t in ultimos_temas]
        logger.info(f"Contexto atual (Memória): {len(temas_vistos)} pautas recentes analisadas.")

        # 2. DECISÃO
        tem_artigo_hoje = NoticiaPortal.query.filter(
            NoticiaPortal.tipo == 'artigo',
            NoticiaPortal.data_publicacao >= datetime.now().replace(hour=0, minute=0)
        ).first()

        tipo_missao = 'artigo' if not tem_artigo_hoje else 'noticia'
        logger.info(f"Missão definida: Gerar {tipo_missao.upper()}")

        # 3. EXECUÇÃO
        try:
            if processar_insight_do_momento:
                processar_insight_do_momento(tipo_desejado=tipo_missao)
            else:
                logger.error("Função processar_insight_do_momento não disponível (erro de import).")
        except Exception as e:
            logger.exception(f"⚠️ Falha na execução da Júlia: {e}")

if __name__ == "__main__":
    # O import dentro do IF garante que o 'web.py' só seja chamado
    # se você rodar o 'run_cleiton.py' diretamente no terminal.
    from app.web import app 
    SEGUNDOS_3H = 3 * 60 * 60
    
    # Configuração de Log APENAS quando rodado como script principal
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s | %(levelname)s | AGENTE: %(message)s',
        handlers=[
            logging.FileHandler("cleiton_operacoes.log", encoding='utf-8'),
            logging.StreamHandler(sys.stdout)
        ]
    )

    logger.info("="*50)
    logger.info("       SISTEMA MULTI-AGENTE LOG COMPLETA")
    logger.info("           ORQUESTRADOR RUN_CLEITON")
    logger.info("="*50)

    while True:
        try:
            # Passamos a instância do app para o orquestrador
            executar_orquestracao(app)
            time.sleep(SEGUNDOS_3H)
        except KeyboardInterrupt:
            logger.info("\n🛑 Maestro Cleiton interrompido. Desligando...")
            break
        except Exception as e:
            logger.exception(f"Erro no ciclo de orquestração: {e}")
            time.sleep(60) # Espera um pouco antes de tentar de novo em caso de erro