"""Feature flags do AgenteBI.

Ausente ou inválida, a API permanece desligada. A ingestão de arquivos tem
uma segunda flag, também desligada por padrão, para não abrir upload quando
só a persistência da SCRUM-252 está habilitada. Não há chamada de IA aqui.
"""
from __future__ import annotations

import os

FLAG_AGENTE_BI_API = "AGENTE_BI_API_ENABLED"
FLAG_AGENTE_BI_INGESTAO = "AGENTE_BI_INGESTAO_ENABLED"


def _parse_bool_strict(raw: str | None) -> bool | None:
    if raw is None:
        return None
    valor = str(raw).strip().lower()
    if valor in {"1", "true", "yes", "on"}:
        return True
    if valor in {"0", "false", "no", "off"}:
        return False
    return None


def agente_bi_api_habilitada() -> bool:
    bruto = os.getenv(FLAG_AGENTE_BI_API)
    return _parse_bool_strict(bruto) is True


def agente_bi_ingestao_habilitada() -> bool:
    bruto = os.getenv(FLAG_AGENTE_BI_INGESTAO)
    return _parse_bool_strict(bruto) is True
