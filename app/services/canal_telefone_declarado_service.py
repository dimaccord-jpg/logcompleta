"""Telefone declarado neste turno.

A interpretação de que o número é um destinatário é do modelo.
Aqui só há reconhecimento de formato, alias opaco e normalização E.164.
Nada neste módulo decide se o usuário pediu envio.
"""
from __future__ import annotations

from dataclasses import dataclass

_SEPARADORES = frozenset(" \t\n\r.-()")
_ALIAS_OCULTO = "[TEL_0]"


@dataclass(frozen=True)
class TelefoneDeclaradoTurno:
    alias: str
    original: str


@dataclass(frozen=True)
class TelefoneNormalizado:
    codigo: str
    e164: str | None = None
    exibicao: str | None = None


def _alias_turno(indice: int) -> str:
    return f"[TEL_{int(indice)}]"


def _spans_formato_telefone(texto: str) -> list[tuple[int, int]]:
    """Sequências com 8 a 15 dígitos e separadores telefônicos. Não é intenção."""
    spans: list[tuple[int, int]] = []
    if not isinstance(texto, str) or not texto:
        return spans
    i = 0
    n = len(texto)
    while i < n:
        caractere = texto[i]
        if caractere != "+" and not caractere.isdigit():
            i += 1
            continue
        if i > 0 and (texto[i - 1].isalnum() or texto[i - 1] == "_"):
            i += 1
            continue
        inicio = i
        j = i
        digitos = 0
        viu_digito = False
        if texto[j] == "+":
            j += 1
        while j < n:
            atual = texto[j]
            if atual.isdigit():
                digitos += 1
                viu_digito = True
                j += 1
                continue
            if atual in _SEPARADORES and viu_digito:
                k = j + 1
                while k < n and texto[k] in _SEPARADORES:
                    k += 1
                if k < n and texto[k].isdigit():
                    j = k
                    continue
            break
        if j < n and (texto[j].isalnum() or texto[j] == "_"):
            i = j if j > i else i + 1
            continue
        if 8 <= digitos <= 15:
            spans.append((inicio, j))
        i = j if j > i else i + 1
    return spans


def _spans_telefone_governanca(texto: str) -> list[tuple[int, int]]:
    """Reusa o classificador de privacidade só para minimizar. Não classifica intenção."""
    try:
        from app.services.cleiton_ai_privacy_classifier import CATEGORY_PHONE, classify_text
    except Exception:
        return []
    try:
        resultado = classify_text(texto, purpose="chat_logistico")
    except Exception:
        return []
    spans = getattr(resultado, "spans", None) or []
    encontrados: list[tuple[int, int]] = []
    for span in spans:
        if getattr(span, "category", None) != CATEGORY_PHONE:
            continue
        inicio = getattr(span, "start", None)
        fim = getattr(span, "end", None)
        if isinstance(inicio, int) and isinstance(fim, int) and fim > inicio:
            encontrados.append((inicio, fim))
    return encontrados


def _unir_spans(texto: str, grupos: list[list[tuple[int, int]]]) -> list[tuple[int, int]]:
    bruto: list[tuple[int, int]] = []
    for grupo in grupos:
        bruto.extend(grupo)
    if not bruto:
        return []
    bruto.sort()
    unidos: list[tuple[int, int]] = []
    for inicio, fim in bruto:
        if not unidos or inicio >= unidos[-1][1]:
            unidos.append((inicio, fim))
            continue
        anterior = unidos[-1]
        unidos[-1] = (anterior[0], max(anterior[1], fim))
    return [(inicio, fim) for inicio, fim in unidos if 0 <= inicio < fim <= len(texto)]


def alias_telefones_do_turno(mensagem: str) -> tuple[str, tuple[TelefoneDeclaradoTurno, ...]]:
    """Troca telefones do turno atual por aliases selecionáveis. O original fica só no retorno."""
    texto = mensagem if isinstance(mensagem, str) else ""
    spans = _unir_spans(
        texto,
        [_spans_formato_telefone(texto), _spans_telefone_governanca(texto)],
    )
    if not spans:
        return texto, ()
    declarados: list[TelefoneDeclaradoTurno] = []
    partes: list[str] = []
    cursor = 0
    for indice, (inicio, fim) in enumerate(spans, start=1):
        alias = _alias_turno(indice)
        original = texto[inicio:fim]
        declarados.append(TelefoneDeclaradoTurno(alias=alias, original=original))
        partes.append(texto[cursor:inicio])
        partes.append(alias)
        cursor = fim
    partes.append(texto[cursor:])
    return "".join(partes), tuple(declarados)


def ocultar_telefones(texto: str) -> str:
    """Tira telefone de histórico e de resposta. O alias oculto não é selecionável."""
    if not isinstance(texto, str) or not texto:
        return "" if not isinstance(texto, str) else texto
    spans = _unir_spans(
        texto,
        [_spans_formato_telefone(texto), _spans_telefone_governanca(texto)],
    )
    if not spans:
        return texto
    partes: list[str] = []
    cursor = 0
    for inicio, fim in spans:
        partes.append(texto[cursor:inicio])
        partes.append(_ALIAS_OCULTO)
        cursor = fim
    partes.append(texto[cursor:])
    return "".join(partes)


def _remover_formatacao(original: str) -> tuple[bool, str]:
    """Remove separadores. True quando há DDI explícito (+ ou prefixo 00)."""
    if not isinstance(original, str):
        return False, ""
    bruto = original.strip()
    if not bruto:
        return False, ""
    tem_ddi = False
    if bruto.startswith("+"):
        tem_ddi = True
        bruto = bruto[1:]
    elif bruto.startswith("00"):
        tem_ddi = True
        bruto = bruto[2:]
    digitos: list[str] = []
    for caractere in bruto:
        if caractere.isdigit():
            digitos.append(caractere)
            continue
        if caractere in _SEPARADORES:
            continue
        return tem_ddi, ""
    return tem_ddi, "".join(digitos)


def normalizar_telefone_e164(original: str) -> TelefoneNormalizado:
    """Validação técnica. Sem DDI explícito não assume país."""
    if not isinstance(original, str) or not original.strip():
        return TelefoneNormalizado(codigo="invalido")
    exibicao = " ".join(original.split())
    if len(exibicao) > 32:
        exibicao = exibicao[:32]
    tem_ddi, digitos = _remover_formatacao(original)
    if not digitos:
        return TelefoneNormalizado(codigo="invalido")
    if not tem_ddi:
        return TelefoneNormalizado(codigo="sem_ddi")
    if digitos.startswith("0") or not 8 <= len(digitos) <= 15:
        return TelefoneNormalizado(codigo="invalido")
    return TelefoneNormalizado(codigo="ok", e164=f"+{digitos}", exibicao=exibicao)


def digitos_e164(e164: str) -> str | None:
    if not isinstance(e164, str) or not e164.startswith("+"):
        return None
    digitos = e164[1:]
    if not digitos or not digitos.isdigit() or not 8 <= len(digitos) <= 15:
        return None
    return digitos
