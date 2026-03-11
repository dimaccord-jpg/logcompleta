"""
Júlia - Regras compartilhadas: parâmetros de elegibilidade e filtros comuns.
Centraliza valores que antes ficavam apenas em módulos específicos da pipeline.
"""
import os
from typing import List


def status_verificacao_permitidos() -> List[str]:
    """
    Define quais status do Verificador podem alimentar a Júlia.
    Padrão seguro: apenas 'aprovado'.
    Exemplo em homolog: JULIA_STATUS_VERIFICACAO_PERMITIDOS=aprovado,revisar
    """
    raw = (os.getenv("JULIA_STATUS_VERIFICACAO_PERMITIDOS", "aprovado") or "aprovado").strip().lower()
    permitidos = [x.strip() for x in raw.split(",") if x.strip()]
    validos = [x for x in permitidos if x in ("aprovado", "revisar")]
    return validos or ["aprovado"]

