"""
Validações genéricas reutilizáveis no admin e em outros módulos.
Evita duplicação e hardcode de limites e regras de formato.
"""
from typing import Tuple


# Limites freemium (ConfigRegras)
JULIA_CHAT_MAX_HISTORY_MIN = 1
JULIA_CHAT_MAX_HISTORY_MAX = 100
FREEMIUM_CONSULTAS_DIA_MIN = 1
FREEMIUM_CONSULTAS_DIA_MAX = 100
FREEMIUM_TRIAL_DIAS_MIN = 0
FREEMIUM_TRIAL_DIAS_MAX = 999999999


def clamp_int(value: int, min_val: int, max_val: int) -> int:
    """Garante que value esteja entre min_val e max_val."""
    return max(min_val, min(max_val, value))


def parse_positive_int(raw: str | None, default: int | None = None) -> int | None:
    """
    Tenta converter string em inteiro positivo.
    Retorna None se vazio/inválido; default se informado.
    """
    if not raw or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
        return v if v > 0 else default
    except ValueError:
        return default


def parse_int_bounded(
    raw: str | None,
    min_val: int,
    max_val: int,
    default: int | None = None,
) -> int | None:
    """
    Converte string em inteiro e aplica limite [min_val, max_val].
    Retorna None se vazio/inválido.
    """
    if not raw or not str(raw).strip():
        return default
    try:
        v = int(str(raw).strip())
        return clamp_int(v, min_val, max_val)
    except ValueError:
        return default


def status_item_serie_valido(status: str) -> bool:
    """Verifica se status é um valor permitido para SerieItemEditorial."""
    return status in ("planejado", "em_andamento", "publicado", "falha", "pulado")


def status_pauta_valido(status: str) -> bool:
    """Verifica se status é um valor permitido para Pauta."""
    return status in ("pendente", "em_processamento", "publicada", "falha")


def tipo_pauta_valido(tipo: str) -> bool:
    """Verifica se tipo é um valor permitido para Pauta."""
    return tipo in ("noticia", "artigo")
