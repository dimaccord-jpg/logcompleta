from __future__ import annotations

from datetime import date, datetime
from typing import Any


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def format_brl(value: Any) -> str:
    amount = max(0.0, _to_float(value))
    base = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {base}"


def format_percent(value: Any) -> str:
    amount = max(0.0, _to_float(value))
    return f"{amount:.2f}".replace(".", ",") + "%"


def format_integer(value: Any) -> str:
    amount = max(0, _to_int(value))
    return f"{amount:,}".replace(",", ".")


def format_weight(value: Any) -> str:
    amount = max(0.0, _to_float(value))
    base = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{base} kg"


def format_date_ptbr(value: Any) -> str:
    if isinstance(value, datetime):
        target = value.date()
    elif isinstance(value, date):
        target = value
    elif isinstance(value, str):
        raw = value.strip()
        if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
            yyyy, mm, dd = raw[:10].split("-")
            return f"{dd}/{mm}/{yyyy}"
        return raw
    else:
        return "n/a"
    return target.strftime("%d/%m/%Y")


def format_quantity(value: Any) -> str:
    return format_integer(value)
