"""
Cleiton - Entrypoint e fachada de compatibilidade.
Delega orquestração gerencial ao run_cleiton_agente_orquestrador.
Carregamento de .env via caminho absoluto (app/env_loader).
"""
import time
import logging
import sys
import os
import json

# --- CARREGAMENTO DE AMBIENTE (caminho absoluto baseado no diretório app) ---
from app.env_loader import load_app_env
load_app_env()

from app.extensions import db
from datetime import datetime

logger = logging.getLogger(__name__)

# Chave exclusiva do Cleiton (orquestrador)
cleiton_api_key = os.getenv("GEMINI_API_KEY")
if not cleiton_api_key:
    logger.warning(
        "GEMINI_API_KEY não configurada para o Cleiton. "
        "Ajuste no .env.{APP_ENV} ou nas variáveis de ambiente."
    )


def coordenar_analise_frete(historico_ia, rota_str):
    """
    Interface de tempo real para análise de fretes.
    Chamada pelo brain.py quando um usuário clica em 'Analisar Rota'.
    """
    from app.run_roberto import roberto
    logger.info("GESTOR CLEITON: Recebendo solicitação de análise para %s", rota_str)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    caminho_indices = os.path.join(base_dir, "indices.json")
    indices = {}
    try:
        with open(caminho_indices, "r", encoding="utf-8") as f:
            indices = json.load(f)
        logger.info("Contexto de mercado (indices.json) carregado com sucesso.")
    except Exception as e:
        logger.warning("Cleiton: Falha ao ler índices de mercado: %s", e)
        indices = {"historico": [], "ultima_atualizacao": "N/A"}
    try:
        insight = roberto.analisar_frete(historico_ia, indices, rota_str)
        logger.info("Análise concluída pelo Roberto para a rota %s", rota_str)
        return insight
    except Exception as e:
        logger.exception("Erro crítico na análise do Roberto: %s", e)
        return {
            "tendencia_macro": "Erro no Processamento",
            "acuracia_percentual": "0%",
            "previsao_texto": "Ocorreu um erro interno na orquestração da IA.",
        }


def executar_orquestracao(
    app_flask,
    bypass_frequencia: bool = False,
    tipo_missao_forcado: str | None = None,
    ignorar_trava_artigo_hoje: bool = False,
):
    """
    Fachada: delega ao orquestrador gerencial (regras, auditoria, dispatch).
    Mantém compatibilidade com rota /executar-cleiton e script em loop.
    """
    from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial
    logger.info("MAESTRO CLEITON: Iniciando ciclo gerencial (delegação ao orquestrador).")
    resultado = executar_ciclo_gerencial(
        app_flask,
        bypass_frequencia=bypass_frequencia,
        tipo_missao_forcado=tipo_missao_forcado,
        ignorar_trava_artigo_hoje=ignorar_trava_artigo_hoje,
    )
    return resultado


if __name__ == "__main__":
    from app.web import app
    segundos_ciclo = 3 * 3600
    app_env = (os.getenv("APP_ENV", "dev").strip() or "dev").lower()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | AGENTE: %(message)s",
        handlers=[
            logging.FileHandler("cleiton_operacoes.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logger.info("=" * 50)
    logger.info("       SISTEMA MULTI-AGENTE LOG COMPLETA")
    logger.info("           ORQUESTRADOR RUN_CLEITON (FACHADA)")
    logger.info("=" * 50)
    if app_env not in ["prod", "homolog"]:
        logger.warning(
            "Loop automático do Cleiton desabilitado fora de produção (APP_ENV=%s). "
            "Use rotas manuais (/executar-cleiton) no ambiente de desenvolvimento/homologação.",
            app_env,
        )
        sys.exit(0)
    while True:
        try:
            executar_orquestracao(app)
            with app.app_context():
                from app.run_cleiton_agente_regras import get_frequencia_horas
                segundos_ciclo = max(1, int(get_frequencia_horas())) * 3600
            time.sleep(segundos_ciclo)
        except KeyboardInterrupt:
            logger.info("Maestro Cleiton interrompido. Desligando...")
            break
        except Exception as e:
            logger.exception("Erro no ciclo de orquestração: %s", e)
            time.sleep(60)
