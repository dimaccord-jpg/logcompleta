"""
Custo de frete (R$/kg) — definição central e robusta para BI e modelo Roberto.

Por registro: custo = valor_frete / peso (sem usar peso_registro).
Por coleção: média aparada simétrica de 10% (remove 10% dos menores e 10% dos maiores
valores de custo por linha, depois média aritmética do miolo). Reduz a dominância
estatística de poucos extremos sem descartá-los como “erro”.

Fallback: quando não há amostra suficiente para aparar com segurança (menos de 10
registros de custo válido, ou após aparar não sobra observação), usa a mediana dos
custos por linha — medida robusta e determinística para amostras pequenas.
"""
from __future__ import annotations

import statistics
from typing import Any


def _valor_peso_para_custo(r: dict[str, Any]) -> tuple[float, float] | None:
    """Extrai (valor_frete, peso) para R$/kg; aceita contrato BI ou modelo (valor/peso)."""
    if "valor_frete_total" in r or "peso_real" in r:
        v = float(r.get("valor_frete_total") or 0)
        p = float(r.get("peso_real") or 0)
    else:
        v = float(r.get("valor") or 0)
        p = float(r.get("peso") or 0)
    if p <= 0:
        return None
    return (v, p)


def calcular_custo_robusto_rs_kg(registros: list[dict[str, Any]]) -> float | None:
    """
    Custo agregado robusto (R$/kg) para uma coleção de registros de frete.

    1. Ignora linhas sem peso > 0.
    2. Por linha: razão valor_frete_total/peso_real (ou valor/peso no contrato modelo).
    3. Se n >= 10: trimmed mean 10% simétrico (remove floor(n*10%) de cada cauda).
    4. Caso contrário: mediana dos custos por linha (fallback explícito).

    Retorna None se não houver nenhuma linha válida.
    """
    ratios: list[float] = []
    for r in registros:
        vp = _valor_peso_para_custo(r)
        if vp is None:
            continue
        v, p = vp
        ratios.append(v / p)
    if not ratios:
        return None

    n = len(ratios)
    ratios.sort()
    k = n // 10  # floor(10%): ex. n=10 -> k=1; n=9 -> k=0
    if k >= 1 and n - 2 * k >= 1:
        miolo = ratios[k : n - k]
        return round(statistics.mean(miolo), 4)
    return round(statistics.median(ratios), 4)
