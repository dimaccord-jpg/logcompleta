"""
Júlia - Entrypoint e fachada de compatibilidade.
Mantém assinatura processar_insight_do_momento(tipo_desejado) e delega ao pipeline operacional.
Remove dependência direta de processadas.json: pautas vêm da tabela Pauta.
"""
import logging
import uuid
from app.env_loader import load_app_env

load_app_env()

logger = logging.getLogger(__name__)


def processar_insight_do_momento(tipo_desejado: str = "noticia", payload_cleiton: dict | None = None):
    """
    Fachada compatível com chamadas existentes (Cleiton dispatcher, scripts).
    Se payload_cleiton for passado (pelo dispatcher), usa mission_id e tipo_missao dele;
    senão monta payload mínimo com tipo_desejado.
    Retorna True apenas quando publicação for concluída no formato correto.
    """
    from app.web import app
    from app.run_julia_agente_pipeline import executar_pipeline
    if payload_cleiton and isinstance(payload_cleiton, dict):
        payload = payload_cleiton
    else:
        tipo = (tipo_desejado or "noticia").strip().lower()
        payload = {
            "mission_id": str(uuid.uuid4()),
            "tipo_missao": tipo,
            "tema": "",
            "prioridade": 5,
            "janela_publicacao": {},
            "tentativa_atual": 1,
            "metadados": {},
        }
    logger.info("Júlia fachada: executando pipeline para tipo=%s", payload.get("tipo_missao", "noticia"))
    return bool(executar_pipeline(payload, app))
