"""
Júlia - Agente Designer: especialista em asset visual por canal/formato.
Recebe conteúdo validado e metadados; gera/seleciona asset por canal com fallback seguro.
Não publica em canal; não altera texto editorial. Saída normalizada para Publisher.
"""
import json
import logging
import os
from typing import Any
from urllib.parse import quote

from app.run_julia_agente_imagem import gerar_fallback_imagem_estatica

logger = logging.getLogger(__name__)

IMAGEM_FALLBACK = (os.getenv("IMAGEM_FALLBACK_URL", "") or "").strip()


def _designer_enabled() -> bool:
    return os.getenv("DESIGNER_ENABLED", "true").strip().lower() in ("true", "1", "t", "yes")


def _designer_provider() -> str:
    return (os.getenv("DESIGNER_PROVIDER", "fallback").strip() or "fallback").lower()


def _aspect_ratio_padrao() -> str:
    return (os.getenv("DESIGNER_ASPECT_RATIO_PADRAO", "16:9").strip() or "16:9")


def _canais_ativos() -> list[str]:
    raw = os.getenv("DESIGNER_CANAIS_ATIVOS", "portal").strip()
    if not raw:
        return ["portal"]
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _url_para_canal(canal: str, url_master: str | None, prompt: str, aspect: str) -> str:
    """Retorna URL de asset para o canal (mesmo master ou placeholder por canal)."""
    if url_master and url_master.strip():
        return url_master.strip()
    # Fallback por canal (placeholder com texto)
    t = (prompt or "logistica")[:40].replace(" ", "+")
    return f"https://placehold.co/800x450/0d6efd/fff?text={quote(t)}"


def _fallback_master(prompt: str | None) -> str:
    if IMAGEM_FALLBACK:
        return IMAGEM_FALLBACK
    return gerar_fallback_imagem_estatica(prompt)


def gerar_assets_por_canal(
    url_imagem_atual: str | None,
    prompt_imagem: str | None,
    tipo_conteudo: str = "noticia",
) -> dict[str, Any]:
    """
    Gera url_imagem_master e assets_por_canal (dict canal -> url).
    Sempre retorna estrutura normalizada; fallback seguro por canal.
    """
    if not _designer_enabled():
        master = (url_imagem_atual or "").strip() or _fallback_master(prompt_imagem)
        canais = _canais_ativos()
        assets = {c: master for c in canais}
        return {
            "url_imagem_master": master,
            "assets_por_canal": assets,
            "prompt_final": (prompt_imagem or "").strip() or "N/A",
            "provider_utilizado": "designer_disabled",
        }
    provider = _designer_provider()
    aspect = _aspect_ratio_padrao()
    prompt_final = (prompt_imagem or "").strip() or "N/A"
    # Master: usa imagem atual ou fallback
    url_master = (url_imagem_atual or "").strip() or _fallback_master(prompt_imagem)
    canais = _canais_ativos()
    assets_por_canal = {}
    for canal in canais:
        assets_por_canal[canal] = _url_para_canal(canal, url_master, prompt_final, aspect)
    return {
        "url_imagem_master": url_master,
        "assets_por_canal": assets_por_canal,
        "prompt_final": prompt_final,
        "provider_utilizado": provider,
    }


def normalizar_assets_json(val: dict | str | None) -> str | None:
    """Serializa assets_por_canal para coluna texto (nunca list/dict bruto em coluna)."""
    if val is None:
        return None
    if isinstance(val, dict):
        return json.dumps(val, ensure_ascii=False)
    if isinstance(val, str):
        return val.strip() or None
    return None
