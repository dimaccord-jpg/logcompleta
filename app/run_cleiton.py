"""
Cleiton - Entrypoint e fachada de compatibilidade.
Delega orquestração gerencial ao run_cleiton_agente_orquestrador.

A configuração de ambiente (APP_ENV, .env.{APP_ENV}, diretórios persistentes e
tokens operacionais) é carregada de forma centralizada em app.settings, evitando
divergência entre web, jobs e runners.
"""
import time
import logging
import sys
import os
import json

from pathlib import Path

from app.settings import settings  # noqa: F401  (import side-effect: garante carga centralizada de env)
from app.finance import LEGACY_INDICES_FILE
from app.settings import settings

from datetime import datetime
from typing import Optional

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

    # Caminhos de índices unificados com a configuração central,
    # mantendo compatibilidade com o arquivo legado em app/indices.json.
    primary_indices_path = Path(settings.indices_file_path)
    candidate_paths = [
        primary_indices_path,
        LEGACY_INDICES_FILE,
    ]

    indices = {"historico": [], "ultima_atualizacao": "N/A"}
    for p in candidate_paths:
        try:
            if not p:
                continue
            path_obj = Path(p)
            if not path_obj.exists():
                continue
            with path_obj.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                indices = loaded
                logger.info("Contexto de mercado carregado com sucesso de %s.", path_obj)
                break
        except Exception as e:
            logger.warning("Cleiton: Falha ao ler índices de mercado em %s: %s", p, e)
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
    ignorar_janela_publicacao: bool = False,
    consumo_identidade: Optional[dict] = None,
):
    """
    Fachada: delega ao orquestrador gerencial (regras, auditoria, dispatch).
    Mantém compatibilidade com rota /executar-cleiton e script em loop.
    ignorar_janela_publicacao: quando True (ex.: botão "Executar artigo agora"), não bloqueia por janela de publicação.
    """
    from app.run_cleiton_agente_orquestrador import executar_ciclo_gerencial
    logger.info("MAESTRO CLEITON: Iniciando ciclo gerencial (delegação ao orquestrador).")
    resultado = executar_ciclo_gerencial(
        app_flask,
        bypass_frequencia=bypass_frequencia,
        tipo_missao_forcado=tipo_missao_forcado,
        ignorar_trava_artigo_hoje=ignorar_trava_artigo_hoje,
        ignorar_janela_publicacao=ignorar_janela_publicacao,
        consumo_identidade=consumo_identidade,
    )
    return resultado


if __name__ == "__main__":
    from app.web import app
    segundos_ciclo = 3 * 3600
    app_env = settings.app_env
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
