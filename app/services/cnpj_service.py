"""
Normalização e validação de domínio de CNPJ (14 dígitos, dígitos verificadores).
Sem consulta a serviço externo ou Receita Federal.

Aceita somente:
- forma pura de 14 dígitos;
- máscara canônica brasileira AA.AAA.AAA/AAAA-AA.
Whitespace externo é removido com strip(); demais caracteres não são descartados silenciosamente.
"""
from __future__ import annotations

import re

_CNPJ_PURO = re.compile(r"^\d{14}$")
_CNPJ_MASCARA_CANONICA = re.compile(r"^\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}$")


class CnpjInvalidoError(ValueError):
    """CNPJ ausente, malformado ou com dígitos verificadores inválidos."""


def _extrair_digitos_mascara_canonica(mascarado: str) -> str:
    """Remove apenas os separadores da máscara canônica já validada por regex."""
    return mascarado.replace(".", "").replace("/", "").replace("-", "")


def normalizar_cnpj(raw: str | None) -> str | None:
    """
    Aceita forma pura ou máscara canônica e devolve 14 dígitos, ou None se vazio.
    Não valida dígitos verificadores. Rejeita letras, símbolos e máscaras não canônicas.
    """
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    if _CNPJ_PURO.fullmatch(txt):
        return txt
    if _CNPJ_MASCARA_CANONICA.fullmatch(txt):
        return _extrair_digitos_mascara_canonica(txt)
    raise CnpjInvalidoError(
        "CNPJ em formato inválido. Use 14 dígitos ou a máscara 00.000.000/0000-00."
    )


def cnpj_digitos_iguais(cnpj_normalizado: str) -> bool:
    return len(cnpj_normalizado) == 14 and len(set(cnpj_normalizado)) == 1


def _digito_verificador(base: str, pesos: tuple[int, ...]) -> int:
    soma = sum(int(base[i]) * pesos[i] for i in range(len(pesos)))
    resto = soma % 11
    if resto < 2:
        return 0
    return 11 - resto


def cnpj_digitos_verificadores_validos(cnpj_normalizado: str) -> bool:
    if len(cnpj_normalizado) != 14 or not cnpj_normalizado.isdigit():
        return False
    if cnpj_digitos_iguais(cnpj_normalizado):
        return False
    pesos_1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    pesos_2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    d1 = _digito_verificador(cnpj_normalizado[:12], pesos_1)
    d2 = _digito_verificador(cnpj_normalizado[:12] + str(d1), pesos_2)
    return cnpj_normalizado[12] == str(d1) and cnpj_normalizado[13] == str(d2)


def cnpj_valido(raw: str | None) -> bool:
    try:
        normalizado = normalizar_cnpj(raw)
    except CnpjInvalidoError:
        return False
    if normalizado is None or len(normalizado) != 14 or not normalizado.isdigit():
        return False
    return cnpj_digitos_verificadores_validos(normalizado)


def exigir_cnpj_normalizado_valido(raw: str | None) -> str:
    """
    Normaliza e valida. Vazio/None não é válido neste caminho (use para ativação Multiuser).
    """
    try:
        normalizado = normalizar_cnpj(raw)
    except CnpjInvalidoError:
        raise
    if normalizado is None:
        raise CnpjInvalidoError("CNPJ é obrigatório.")
    if len(normalizado) != 14 or not normalizado.isdigit():
        raise CnpjInvalidoError("CNPJ deve conter 14 dígitos numéricos.")
    if not cnpj_digitos_verificadores_validos(normalizado):
        raise CnpjInvalidoError("CNPJ com dígitos verificadores inválidos.")
    return normalizado


def normalizar_cnpj_opcional(raw: str | None) -> str | None:
    """
    None/vazio permanece None (Conta legada). Valor presente deve ser CNPJ válido.
    """
    try:
        normalizado = normalizar_cnpj(raw)
    except CnpjInvalidoError:
        raise
    if normalizado is None:
        return None
    return exigir_cnpj_normalizado_valido(normalizado)
