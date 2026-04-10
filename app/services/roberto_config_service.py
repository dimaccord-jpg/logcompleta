"""
Configuração operacional do Roberto (persistência em ConfigRegras).

Objetivo:
- centralizar parâmetros calibráveis sem hardcode espalhado;
- manter defaults seguros quando ainda não houver cadastro no admin;
- permitir evolução sem alterar fluxo oficial de governança/billing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from flask import g, has_request_context

from app.extensions import db
from app.models import ConfigRegras

# Prefixo único para evitar colisão com outras regras administrativas.
_CFG_PREFIX = "roberto_cfg_"

DEFAULTS: dict[str, int] = {
    "upload_total_max": 10000,
    "previsao_meses": 18,
    "min_linhas_mes_modelo": 10,
    "min_linhas_uf_heatmap_ranking": 10,
    "max_pontos_dispersao": 500,
    # Valor inicial provisório sujeito a calibração.
    "max_linhas_mes_modelo": 300,
    # Valor inicial provisório sujeito a calibração.
    "max_linhas_uf_heatmap": 300,
    # Valor inicial provisório sujeito a calibração.
    "max_linhas_uf_ranking": 300,
    "upload_ttl_minutes": 30,
}

DESCRICOES: dict[str, str] = {
    "upload_total_max": "Máximo total de linhas do upload usadas no BI.",
    "previsao_meses": "Janela de meses históricos usada na série/previsão.",
    "min_linhas_mes_modelo": "Mínimo de linhas por mês para qualidade do modelo.",
    "min_linhas_uf_heatmap_ranking": "Mínimo de linhas por UF para heatmap/ranking.",
    "max_pontos_dispersao": "Máximo de pontos exibidos no gráfico de dispersão.",
    "max_linhas_mes_modelo": "Máximo de linhas por mês no histórico do modelo (provisório).",
    "max_linhas_uf_heatmap": "Máximo de linhas por UF para heatmap/ranking (provisório).",
    "max_linhas_uf_ranking": "Máximo de linhas por UF para ranking (provisório).",
    "upload_ttl_minutes": "Tempo de expiração dos dados temporários do upload.",
}


@dataclass(frozen=True)
class RobertoConfig:
    upload_total_max: int
    previsao_meses: int
    min_linhas_mes_modelo: int
    min_linhas_uf_heatmap_ranking: int
    max_pontos_dispersao: int
    max_linhas_mes_modelo: int
    max_linhas_uf_heatmap: int
    max_linhas_uf_ranking: int
    upload_ttl_minutes: int


def _cfg_key(nome: str) -> str:
    return f"{_CFG_PREFIX}{nome}"


def _coerce_positive_int(value: Any, default: int) -> int:
    try:
        v = int(str(value).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _bounds(nome: str, valor: int) -> int:
    # Guardrails operacionais mínimos/máximos para evitar configurações inválidas.
    if nome == "upload_total_max":
        return min(max(100, valor), 200000)
    if nome == "previsao_meses":
        return min(max(3, valor), 60)
    if nome in ("min_linhas_mes_modelo", "min_linhas_uf_heatmap_ranking"):
        return min(max(1, valor), 1000)
    if nome == "max_pontos_dispersao":
        return min(max(50, valor), 5000)
    if nome in ("max_linhas_mes_modelo", "max_linhas_uf_heatmap", "max_linhas_uf_ranking"):
        return min(max(20, valor), 10000)
    if nome == "upload_ttl_minutes":
        return min(max(5, valor), 240)
    return valor


def _parse_from_row(nome: str, row: ConfigRegras | None) -> int:
    base = DEFAULTS[nome]
    if row is None:
        return base
    raw = row.valor_inteiro if row.valor_inteiro is not None else row.valor_texto
    coerced = _coerce_positive_int(raw, base)
    return _bounds(nome, coerced)


def _load_cfg_map() -> dict[str, ConfigRegras]:
    keys = [_cfg_key(nome) for nome in DEFAULTS.keys()]
    rows = ConfigRegras.query.filter(ConfigRegras.chave.in_(keys)).all()
    return {row.chave: row for row in rows}


def _validar_relacoes(valores: dict[str, int]) -> None:
    min_mes = valores["min_linhas_mes_modelo"]
    max_mes = valores["max_linhas_mes_modelo"]
    min_uf = valores["min_linhas_uf_heatmap_ranking"]
    max_heatmap = valores["max_linhas_uf_heatmap"]
    max_ranking = valores["max_linhas_uf_ranking"]
    previsao_meses = valores["previsao_meses"]
    upload_total_max = valores["upload_total_max"]

    if min_mes > max_mes:
        raise ValueError(
            "Configuração inválida: min_linhas_mes_modelo não pode ser maior que max_linhas_mes_modelo."
        )
    if min_uf > max_heatmap:
        raise ValueError(
            "Configuração inválida: min_linhas_uf_heatmap_ranking não pode ser maior que max_linhas_uf_heatmap."
        )
    if min_uf > max_ranking:
        raise ValueError(
            "Configuração inválida: min_linhas_uf_heatmap_ranking não pode ser maior que max_linhas_uf_ranking."
        )
    capacidade_minima_modelo = previsao_meses * min_mes
    if upload_total_max < capacidade_minima_modelo:
        raise ValueError(
            "Configuração inválida: upload_total_max é insuficiente para a janela de previsão "
            "com o mínimo mensal do modelo. Ajuste upload_total_max, previsao_meses "
            "ou min_linhas_mes_modelo."
        )


def get_roberto_config() -> RobertoConfig:
    if has_request_context():
        cached = getattr(g, "_roberto_cfg", None)
        if isinstance(cached, RobertoConfig):
            return cached

    cfg_map = _load_cfg_map()
    values = {
        nome: _parse_from_row(nome, cfg_map.get(_cfg_key(nome)))
        for nome in DEFAULTS.keys()
    }
    try:
        _validar_relacoes(values)
    except ValueError:
        values = dict(DEFAULTS)
    cfg = RobertoConfig(**values)
    if has_request_context():
        g._roberto_cfg = cfg
    return cfg


def salvar_roberto_config(raw_values: dict[str, str | None]) -> RobertoConfig:
    cfg_map = _load_cfg_map()
    novos_valores: dict[str, int] = {}
    for nome, default in DEFAULTS.items():
        entrada = raw_values.get(nome)
        parsed = _coerce_positive_int(entrada, default)
        bounded = _bounds(nome, parsed)
        novos_valores[nome] = bounded

    _validar_relacoes(novos_valores)

    for nome, bounded in novos_valores.items():
        row = cfg_map.get(_cfg_key(nome))
        if row is None:
            row = ConfigRegras(
                chave=_cfg_key(nome),
                descricao=DESCRICOES.get(nome),
            )
            db.session.add(row)
            cfg_map[row.chave] = row
        row.valor_inteiro = bounded
        row.valor_texto = None
        row.valor_real = None

    db.session.commit()
    if has_request_context():
        g._roberto_cfg = None
    return get_roberto_config()
