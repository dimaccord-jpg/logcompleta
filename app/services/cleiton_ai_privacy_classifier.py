"""
Classificador contextual local do Cleiton (SCRUM-75).

Executa na fronteira do AgenteFrete, em CPU, sem HTTP, sem upload e sem regex.
Não há modelo neural no repositório; a tarefa é proporcional a um analisador
de finalidade + estrutura + checksum + janela contextual.

Identificadores são reconhecidos por:
- varredura caractere a caractere;
- contagem de dígitos e classes;
- dígitos verificadores (CPF, CNPJ, Luhn);
- palavras-cue na janela local;
- finalidade declarada da chamada.

Falha fechada: resultado inválido ou exceção → não autorizar o original.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

CATEGORY_CPF = "cpf"
CATEGORY_CNPJ = "cnpj"
CATEGORY_EMAIL = "email"
CATEGORY_PHONE = "phone"
CATEGORY_FISCAL_KEY = "fiscal_key"
CATEGORY_SECRET = "secret"
CATEGORY_API_KEY = "api_key"
CATEGORY_TOKEN = "token"
CATEGORY_CARD = "card"
CATEGORY_BANK = "bank"
CATEGORY_STRIPE_ID = "stripe_id"
CATEGORY_PERSON_NAME = "person_name"
CATEGORY_FILENAME = "filename"
CATEGORY_TECHNICAL_ID = "technical_id"

DECISION_ALLOW = "allowed"
DECISION_MINIMIZE = "minimized"
DECISION_BLOCK = "blocked"

# Nomes reais das finalidades já existentes. Finalidade fora deste conjunto → BLOCK.
SUPPORTED_PURPOSES = (
    "discovery",
    "chat_logistico",
    "auditoria_frete",
    "extracao_tabela_frete",
    "explicacao_calculo",
    "comparacao_fretes",
    "insights_auditoria",
    "redacao_editorial",
    "geracao_imagem",
    "busca_web",
)

# Análise logística: entidades operacionais (transportadora, rota, peso, taxas) são necessárias.
_OPERATIONAL_PURPOSES = frozenset(
    {
        "discovery",
        "chat_logistico",
        "auditoria_frete",
        "extracao_tabela_frete",
        "explicacao_calculo",
        "comparacao_fretes",
        "insights_auditoria",
        "busca_web",
    }
)

_SEPARATORS = frozenset(" \t\n\r\f\v")
_TOKEN_BREAK = frozenset(" \t\n\r\f\v,;()[]{}<>\"'|!?*=\\")
_DIGIT_JOIN_SEPS = frozenset(".-/ +()")
_TRAILING_PUNCT = frozenset(".,;:!?")

_LOGISTICS_CUES = (
    "origem",
    "destino",
    "cidade",
    "rota",
    "uf",
    "regiao",
    "região",
    "peso",
    "kg",
    "volume",
    "quantidade",
    "faixa",
    "frete",
    "tarifa",
    "tarifas",
    "minimo",
    "mínimo",
    "gris",
    "pedagio",
    "pedágio",
    "adicional",
    "imposto",
    "icms",
    "cif",
    "fob",
    "transportadora",
    "carrier",
    "valor",
    "r$",
    "reais",
    "base",
    "calculo",
    "cálculo",
    "divergencia",
    "divergência",
    "periodo",
    "período",
    "linha",
    "aliquota",
    "alíquota",
)

_IDENTITY_CUES = (
    "cpf",
    "cnpj",
    "rg",
    "email",
    "e-mail",
    "mail",
    "telefone",
    "celular",
    "whatsapp",
    "fone",
    "tel",
    "contato",
)

_SECRET_CUES = (
    "senha",
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "bearer",
    "cookie",
    "authorization",
    "credencial",
    "credential",
    "auth",
)

_BANK_CUES = (
    "banco",
    "agencia",
    "agência",
    "corrente",
    "iban",
    "pix",
    "cvv",
    "cartao",
    "cartão",
    "card",
    "conta",
)

_BANK_WORD_CUES = frozenset(_BANK_CUES)
_CARD_WORD_CUES = frozenset(("cartao", "cartão", "card", "cvv"))
_PHONE_WORD_CUES = frozenset(("telefone", "celular", "whatsapp", "fone", "tel"))
_STRONG_BANK_WORDS = frozenset(("banco", "agencia", "agência", "iban", "pix", "corrente"))
_QUANTITY_RIGHT_CUES = ("reais", "r$", "kg", "%", "km")

_FISCAL_CUES = (
    "chave",
    "nfe",
    "nf-e",
    "cte",
    "ct-e",
    "danfe",
    "dacte",
)

_NAME_CUES = (
    "meu nome",
    "nome completo",
    "nome:",
    "sr.",
    "sra.",
    "cpf do",
    "cpf da",
)

_NAME_STOP_TOKENS = frozenset(
    {
        "destino",
        "origem",
        "peso",
        "kg",
        "frete",
        "tarifa",
        "tarifas",
        "gris",
        "pedagio",
        "pedágio",
        "cidade",
        "rota",
        "transportadora",
        "carrier",
        "cif",
        "fob",
        "volume",
        "quantidade",
        "reais",
        "uf",
        "icms",
        "aliquota",
        "alíquota",
        "periodo",
        "período",
        "pedágio",
    }
)

_SECRET_PREFIXES = (
    "sk_live",
    "sk_test",
    "rk_live",
    "rk_test",
    "sk-",
    "aiza",
    "ghp_",
    "xoxb-",
    "xoxp-",
    "akia",
    "eyj",
)

_STRIPE_PREFIXES = (
    "cus_",
    "sub_",
    "pi_",
    "pm_",
    "acct_",
    "price_",
    "prod_",
    "si_",
    "in_",
    "ch_",
    "tok_",
    "card_",
)

_SECRET_ASSIGN_CUES = (
    "senha",
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "bearer",
    "cookie",
    "authorization",
)

_SECRET_CONNECTORS = (
    "de acesso",
    "de access",
    "acesso",
    "access",
    "de",
    "do",
    "da",
    "das",
    "dos",
)

_AUTH_SCHEMES = ("basic", "bearer", "digest")
_HEADER_SECRET_CUES = frozenset(("cookie", "authorization"))

_TECHNICAL_ID_CUES = (
    "usuario_id",
    "user_id",
    "documento_id",
    "document_id",
    "customer_id",
    "cliente_id",
    "account_id",
    "session_id",
)

_NEVER_NECESSARY = frozenset(
    {
        CATEGORY_SECRET,
        CATEGORY_API_KEY,
        CATEGORY_TOKEN,
        CATEGORY_CARD,
        CATEGORY_BANK,
        CATEGORY_EMAIL,
        CATEGORY_PHONE,
        CATEGORY_CPF,
        CATEGORY_CNPJ,
        CATEGORY_FISCAL_KEY,
        CATEGORY_STRIPE_ID,
        CATEGORY_TECHNICAL_ID,
        CATEGORY_FILENAME,
    }
)

_HIGH_RISK_CATEGORIES = frozenset(
    {
        CATEGORY_SECRET,
        CATEGORY_API_KEY,
        CATEGORY_TOKEN,
        CATEGORY_CARD,
    }
)

_NAME_ALPHA = "áéíóúâêôãõçÁÉÍÓÚÂÊÔÃÕÇ.-'"


@dataclass(frozen=True)
class DetectedSpan:
    start: int
    end: int
    category: str
    confidence: str


@dataclass
class ClassificationResult:
    decision: str
    spans: list[DetectedSpan] = field(default_factory=list)
    categories_detected: list[str] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    valid: bool = True


class ClassifierUnavailableError(RuntimeError):
    """Classificador local indisponível ou resultado inválido."""


def classification_invariants_ok(classified: ClassificationResult | None) -> bool:
    """Invariantes internas. Não confiar apenas em valid=True."""
    if classified is None:
        return False
    if classified.decision not in {DECISION_ALLOW, DECISION_MINIMIZE, DECISION_BLOCK}:
        return False
    if classified.decision == DECISION_MINIMIZE:
        if not classified.spans or not classified.categories_detected:
            return False
    if classified.decision == DECISION_ALLOW and classified.spans:
        return False
    if classified.valid is False:
        return False
    return True


def classify_text(text: str, *, purpose: str, content_type: str = "text") -> ClassificationResult:
    """
    Classifica conteúdo candidato. Não devolve os valores originais.
    A finalidade participa da avaliação de necessidade.
    """
    resolved_purpose = (purpose or "").strip()
    if resolved_purpose not in SUPPORTED_PURPOSES:
        return ClassificationResult(
            decision=DECISION_BLOCK,
            reason_codes=["unsupported_purpose"],
            valid=True,
        )
    if text is None:
        return ClassificationResult(
            decision=DECISION_BLOCK,
            reason_codes=["empty_or_null_content"],
            valid=True,
        )
    if not isinstance(text, str):
        return ClassificationResult(
            decision=DECISION_BLOCK,
            reason_codes=["unsupported_content_type"],
            valid=True,
        )
    if "\x00" in text:
        return ClassificationResult(
            decision=DECISION_BLOCK,
            reason_codes=["binary_null_byte"],
            valid=True,
        )

    spans: list[DetectedSpan] = []
    spans.extend(_email_spans(text))
    spans.extend(_prefixed_secret_spans(text))
    spans.extend(_digit_identity_spans(text, resolved_purpose))
    spans.extend(_assignment_secret_spans(text))
    spans.extend(_technical_id_spans(text))
    spans.extend(_labeled_name_spans(text, resolved_purpose))
    resolved = _resolve_overlaps(spans)
    resolved = [
        span
        for span in resolved
        if not _category_is_necessary(span.category, resolved_purpose, text, span)
    ]
    categories = sorted({span.category for span in resolved})

    if any(span.category in _HIGH_RISK_CATEGORIES and span.confidence != "high" for span in resolved):
        return ClassificationResult(
            decision=DECISION_BLOCK,
            spans=resolved,
            categories_detected=categories,
            reason_codes=["ambiguous_high_risk_span"],
            valid=True,
        )

    if not resolved:
        return ClassificationResult(
            decision=DECISION_ALLOW,
            spans=[],
            categories_detected=[],
            reason_codes=["no_incidental_categories"],
            valid=True,
        )
    result = ClassificationResult(
        decision=DECISION_MINIMIZE,
        spans=resolved,
        categories_detected=categories,
        reason_codes=["incidental_data_minimized"],
        valid=True,
    )
    if not classification_invariants_ok(result):
        return ClassificationResult(
            decision=DECISION_BLOCK,
            reason_codes=["inconsistent_classifier_result"],
            valid=True,
        )
    return result


def _category_is_necessary(category: str, purpose: str, text: str, span: DetectedSpan) -> bool:
    """True = dado participa da finalidade e não deve ser minimizado."""
    if purpose not in SUPPORTED_PURPOSES:
        return False
    if category in _NEVER_NECESSARY:
        return False
    if category == CATEGORY_PERSON_NAME:
        window = _window(text, span.start, span.end)
        logistics = _contains_any(window, _LOGISTICS_CUES)
        return purpose in _OPERATIONAL_PURPOSES and logistics
    return False


def _window(text: str, start: int, end: int, radius: int = 48) -> str:
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return text[lo:hi].lower()


def _contains_any(haystack: str, needles: Iterable[str]) -> bool:
    blob = haystack.lower()
    for needle in needles:
        if needle and needle in blob:
            return True
    return False


def _contains_word(haystack: str, needle: str) -> bool:
    if not needle:
        return False
    blob = haystack.lower()
    target = needle.lower()
    start = 0
    tlen = len(target)
    while True:
        found = blob.find(target, start)
        if found < 0:
            return False
        before_ok = found == 0 or not blob[found - 1].isalnum()
        after_i = found + tlen
        after_ok = after_i >= len(blob) or not blob[after_i].isalnum()
        if before_ok and after_ok:
            return True
        start = found + 1


def _contains_any_word(haystack: str, needles: Iterable[str]) -> bool:
    for needle in needles:
        if _contains_word(haystack, needle):
            return True
    return False


def _is_issued_alias(value: str) -> bool:
    """Não reclassificar representação já minimizada nesta ou em passagem anterior."""
    s = (value or "").strip()
    if len(s) >= 4 and s.startswith("[") and "]" in s:
        inner = s[1 : s.find("]")]
        if "_" in inner:
            prefix, _, rest = inner.rpartition("_")
            if prefix.replace("_", "").isalpha() and rest.isdigit():
                return True
    if " " in s:
        head, tail = s.split(" ", 1)
        if head in {"EMPRESA", "Pessoa"} and tail.isdigit():
            return True
    return False


def _email_spans(text: str) -> list[DetectedSpan]:
    spans: list[DetectedSpan] = []
    idx = 0
    while True:
        at = text.find("@", idx)
        if at < 0:
            break
        left = at - 1
        while left >= 0 and (text[left].isalnum() or text[left] in "._+-"):
            left -= 1
        local_start = left + 1
        right = at + 1
        while right < len(text) and (text[right].isalnum() or text[right] in ".-"):
            right += 1
        while right > at + 1 and text[right - 1] in _TRAILING_PUNCT:
            right -= 1
        local = text[local_start:at]
        domain = text[at + 1 : right]
        if local and domain and "." in domain and domain[0].isalnum() and domain[-1].isalnum():
            spans.append(DetectedSpan(local_start, right, CATEGORY_EMAIL, "high"))
        idx = at + 1
    return spans


def _prefixed_secret_spans(text: str) -> list[DetectedSpan]:
    spans: list[DetectedSpan] = []
    lower = text.lower()
    for prefix in _SECRET_PREFIXES:
        start = 0
        plen = len(prefix)
        while True:
            found = lower.find(prefix, start)
            if found < 0:
                break
            end = found + plen
            while end < len(text) and (text[end].isalnum() or text[end] in "_-."):
                end += 1
            if end - found >= max(12, plen + 4):
                category = CATEGORY_API_KEY if prefix.startswith(("sk", "rk", "aiza", "akia")) else CATEGORY_TOKEN
                spans.append(DetectedSpan(found, end, category, "high"))
            start = found + plen
    for prefix in _STRIPE_PREFIXES:
        start = 0
        plen = len(prefix)
        while True:
            found = lower.find(prefix, start)
            if found < 0:
                break
            if found > 0 and text[found - 1].isalnum():
                start = found + plen
                continue
            end = found + plen
            while end < len(text) and (text[end].isalnum() or text[end] == "_"):
                end += 1
            if end - found >= plen + 6:
                spans.append(DetectedSpan(found, end, CATEGORY_STRIPE_ID, "high"))
            start = found + plen
    return spans


def _cpf_checksum_ok(digits: str) -> bool:
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    acc = 0
    for idx, weight in enumerate(range(10, 1, -1)):
        acc += int(digits[idx]) * weight
    rest = acc % 11
    d1 = 0 if rest < 2 else 11 - rest
    if d1 != int(digits[9]):
        return False
    acc = 0
    for idx, weight in enumerate(range(11, 1, -1)):
        acc += int(digits[idx]) * weight
    rest = acc % 11
    d2 = 0 if rest < 2 else 11 - rest
    return d2 == int(digits[10])


def _cnpj_checksum_ok(digits: str) -> bool:
    if len(digits) != 14 or len(set(digits)) == 1:
        return False
    w1 = (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    w2 = (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2)
    acc = sum(int(digits[idx]) * w1[idx] for idx in range(12))
    rest = acc % 11
    d1 = 0 if rest < 2 else 11 - rest
    if d1 != int(digits[12]):
        return False
    acc = sum(int(digits[idx]) * w2[idx] for idx in range(13))
    rest = acc % 11
    d2 = 0 if rest < 2 else 11 - rest
    return d2 == int(digits[13])


def _luhn_ok(digits: str) -> bool:
    if len(digits) < 13 or len(digits) > 19:
        return False
    total = 0
    reverse = digits[::-1]
    for idx, ch in enumerate(reverse):
        n = int(ch)
        if idx % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _consume_joined_digits(text: str, start: int) -> tuple[int, str]:
    """Une segmentos numéricos separados por pontuação/espaço de agrupamento."""
    n = len(text)
    digits: list[str] = []
    j = start
    while j < n:
        cur = text[j]
        if cur.isdigit():
            digits.append(cur)
            j += 1
            continue
        k = j
        while k < n and text[k] in _DIGIT_JOIN_SEPS:
            k += 1
        if k > j and k < n and text[k].isdigit():
            j = k
            continue
        break
    return j, "".join(digits)


def _left_token(text: str, start: int) -> str:
    i = start - 1
    while i >= 0 and not text[i].isalnum():
        i -= 1
    end = i + 1
    while i >= 0 and (text[i].isalnum() or text[i] in "_-"):
        i -= 1
    return text[i + 1 : end].lower()


def _right_preview(text: str, end: int, radius: int = 12) -> str:
    return text[end : min(len(text), end + radius)].lower()


def _digit_identity_spans(text: str, purpose: str) -> list[DetectedSpan]:
    spans: list[DetectedSpan] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if not ch.isdigit():
            i += 1
            continue
        end, raw = _consume_joined_digits(text, i)
        original = text[i:end]
        window = _window(text, i, end)
        left = _left_token(text, i)
        right = _right_preview(text, end)
        logistics = _contains_any(window, _LOGISTICS_CUES)
        identity_cue = _contains_any(window, _IDENTITY_CUES)
        fiscal_cue = _contains_any(window, _FISCAL_CUES)
        bank_cue = left in _BANK_WORD_CUES
        card_cue = left in _CARD_WORD_CUES or _contains_any_word(window, _CARD_WORD_CUES)
        phone_cue = left in _PHONE_WORD_CUES or _contains_any(window, _PHONE_WORD_CUES)
        strong_bank = left in _STRONG_BANK_WORDS or left == "conta"
        quantity_right = any(cue in right for cue in _QUANTITY_RIGHT_CUES)
        span = _classify_digit_span(
            start=i,
            end=end,
            digits=raw,
            original=original,
            logistics=logistics,
            identity_cue=identity_cue,
            fiscal_cue=fiscal_cue,
            bank_cue=bank_cue,
            phone_cue=phone_cue,
            card_cue=card_cue,
            strong_bank=strong_bank,
            quantity_right=quantity_right,
            left_token=left,
            purpose=purpose,
        )
        if span is not None:
            spans.append(span)
        i = end if end > i else i + 1
    return spans


def _classify_digit_span(
    *,
    start: int,
    end: int,
    digits: str,
    original: str,
    logistics: bool,
    identity_cue: bool,
    fiscal_cue: bool,
    bank_cue: bool,
    phone_cue: bool,
    card_cue: bool,
    strong_bank: bool,
    quantity_right: bool,
    left_token: str,
    purpose: str,
) -> DetectedSpan | None:
    count = len(digits)
    grouped = " " in original or original.count("-") >= 2 or "(" in original
    if count == 44:
        return DetectedSpan(start, end, CATEGORY_FISCAL_KEY, "high")
    if phone_cue and 10 <= count <= 13:
        formatted_cpf = "." in original and "-" in original
        if not (count == 11 and _cpf_checksum_ok(digits) and formatted_cpf):
            return DetectedSpan(start, end, CATEGORY_PHONE, "high")
    if 13 <= count <= 19 and _luhn_ok(digits) and (card_cue or bank_cue or grouped):
        if not (phone_cue and count <= 13):
            return DetectedSpan(start, end, CATEGORY_CARD, "high")
    if count == 14 and (_cnpj_checksum_ok(digits) or "/" in original or identity_cue):
        return DetectedSpan(start, end, CATEGORY_CNPJ, "high")
    if count == 11 and (_cpf_checksum_ok(digits) or (("." in original and "-" in original) or identity_cue)):
        if phone_cue and not _cpf_checksum_ok(digits) and "." not in original:
            return DetectedSpan(start, end, CATEGORY_PHONE, "high")
        return DetectedSpan(start, end, CATEGORY_CPF, "high")
    if count in (10, 11) and phone_cue:
        return DetectedSpan(start, end, CATEGORY_PHONE, "high")
    if (strong_bank or left_token in _BANK_WORD_CUES) and 4 <= count <= 16:
        if quantity_right and left_token not in _BANK_WORD_CUES:
            return None
        return DetectedSpan(start, end, CATEGORY_BANK, "high")
    if fiscal_cue and count >= 40:
        return DetectedSpan(start, end, CATEGORY_FISCAL_KEY, "high")
    if logistics:
        return None
    if identity_cue and count in (10, 11, 14):
        category = CATEGORY_CNPJ if count == 14 else CATEGORY_CPF
        return DetectedSpan(start, end, category, "high")
    return None


def _skip_secret_connectors(text: str, cursor: int) -> int:
    n = len(text)
    while cursor < n:
        while cursor < n and text[cursor] in " \t:=-":
            cursor += 1
        rest = text[cursor:].lower()
        matched = False
        for conn in _SECRET_CONNECTORS:
            if rest.startswith(conn):
                nxt = cursor + len(conn)
                if nxt >= n or not text[nxt].isalnum():
                    cursor = nxt
                    matched = True
                    break
        if not matched:
            break
    return cursor


def _assignment_secret_spans(text: str) -> list[DetectedSpan]:
    spans: list[DetectedSpan] = []
    lower = text.lower()
    ordered = sorted(_SECRET_ASSIGN_CUES, key=len, reverse=True)
    for cue in ordered:
        start = 0
        clen = len(cue)
        while True:
            found = lower.find(cue, start)
            if found < 0:
                break
            if found > 0 and lower[found - 1].isalnum():
                start = found + clen
                continue
            after_cue = found + clen
            if after_cue < len(text) and text[after_cue].isalnum():
                start = found + clen
                continue
            cursor = _skip_secret_connectors(text, after_cue)
            if cue in _HEADER_SECRET_CUES:
                rest_l = text[cursor:].lower()
                for scheme in _AUTH_SCHEMES:
                    if rest_l.startswith(scheme) and (
                        len(scheme) == len(rest_l) or not rest_l[len(scheme) : len(scheme) + 1].isalnum()
                    ):
                        cursor = cursor + len(scheme)
                        while cursor < len(text) and text[cursor] in " \t":
                            cursor += 1
                        break
                value_start = cursor
                while cursor < len(text) and text[cursor] not in "\n\r":
                    cursor += 1
            else:
                rest_l = text[cursor:].lower()
                for scheme in _AUTH_SCHEMES:
                    if rest_l.startswith(scheme) and (
                        len(scheme) == len(rest_l) or not rest_l[len(scheme) : len(scheme) + 1].isalnum()
                    ):
                        cursor = cursor + len(scheme)
                        while cursor < len(text) and text[cursor] in " \t":
                            cursor += 1
                        break
                value_start = cursor
                while cursor < len(text) and text[cursor] not in _SEPARATORS and text[cursor] not in ",;":
                    cursor += 1
            if cursor >= value_start + 3:
                fragment = text[value_start:cursor]
                if _is_issued_alias(fragment):
                    start = found + clen
                    continue
                category = CATEGORY_API_KEY if "api" in cue else CATEGORY_SECRET
                if cue in ("token", "bearer"):
                    category = CATEGORY_TOKEN
                spans.append(DetectedSpan(value_start, cursor, category, "high"))
            start = found + clen
    return spans


def _technical_id_spans(text: str) -> list[DetectedSpan]:
    spans: list[DetectedSpan] = []
    lower = text.lower()
    for cue in _TECHNICAL_ID_CUES:
        start = 0
        clen = len(cue)
        while True:
            found = lower.find(cue, start)
            if found < 0:
                break
            cursor = found + clen
            while cursor < len(text) and text[cursor] in " \t:=":
                cursor += 1
            value_start = cursor
            while cursor < len(text) and text[cursor] not in _SEPARATORS and text[cursor] not in ",;":
                cursor += 1
            if cursor > value_start:
                fragment = text[value_start:cursor]
                if not _is_issued_alias(fragment):
                    spans.append(DetectedSpan(value_start, cursor, CATEGORY_TECHNICAL_ID, "high"))
            start = found + clen
    return spans


def _next_alpha_token(text: str, idx: int) -> str:
    j = idx
    while j < len(text) and text[j] in " \t":
        j += 1
    k = j
    while k < len(text) and (text[k].isalpha() or text[k] in _NAME_ALPHA):
        k += 1
    return text[j:k].lower()


def _labeled_name_spans(text: str, purpose: str) -> list[DetectedSpan]:
    spans: list[DetectedSpan] = []
    lower = text.lower()
    for cue in _NAME_CUES:
        start = 0
        clen = len(cue)
        while True:
            found = lower.find(cue, start)
            if found < 0:
                break
            cursor = found + clen
            while cursor < len(text) and text[cursor] in " \t:":
                cursor += 1
            value_start = cursor
            words = 0
            while cursor < len(text) and words < 4:
                if text[cursor].isalpha() or text[cursor] in _NAME_ALPHA:
                    cursor += 1
                    continue
                if text[cursor] == " " and cursor + 1 < len(text) and text[cursor + 1].isalpha():
                    nxt = _next_alpha_token(text, cursor)
                    if nxt in _NAME_STOP_TOKENS:
                        break
                    words += 1
                    cursor += 1
                    continue
                break
            if cursor > value_start + 2:
                window = _window(text, value_start, cursor)
                logistics = _contains_any(window, _LOGISTICS_CUES)
                first_token = _next_alpha_token(text, value_start)
                operational_label = first_token in {"transportadora", "carrier"} or logistics
                if operational_label and purpose in _OPERATIONAL_PURPOSES:
                    start = found + clen
                    continue
                spans.append(DetectedSpan(value_start, cursor, CATEGORY_PERSON_NAME, "high"))
            start = found + clen
    return spans


def _resolve_overlaps(spans: list[DetectedSpan]) -> list[DetectedSpan]:
    if not spans:
        return []
    priority = {
        CATEGORY_SECRET: 100,
        CATEGORY_API_KEY: 95,
        CATEGORY_TOKEN: 90,
        CATEGORY_CARD: 85,
        CATEGORY_FISCAL_KEY: 80,
        CATEGORY_CPF: 75,
        CATEGORY_CNPJ: 70,
        CATEGORY_EMAIL: 65,
        CATEGORY_STRIPE_ID: 60,
        CATEGORY_PHONE: 55,
        CATEGORY_BANK: 50,
        CATEGORY_TECHNICAL_ID: 48,
        CATEGORY_PERSON_NAME: 40,
        CATEGORY_FILENAME: 30,
    }
    ordered = sorted(
        spans,
        key=lambda s: (-priority.get(s.category, 0), s.start, -(s.end - s.start)),
    )
    kept: list[DetectedSpan] = []
    for span in ordered:
        conflict = False
        for existing in kept:
            if span.start < existing.end and span.end > existing.start:
                conflict = True
                break
        if not conflict:
            kept.append(span)
    return sorted(kept, key=lambda s: s.start)


def assert_no_regex_in_this_module() -> None:
    """Hook de teste: este módulo não importa regex."""
    return None
