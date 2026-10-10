"""Identidade operacional municipal.

Identidade semântica por nome e UF. Não é código oficial de município.
id_cidade da base_localidades não é autoridade. O catálogo, quando existe,
só confirma candidatos; não classifica capital nem interior.
"""
from __future__ import annotations

from typing import Any

from app.services.semantic_freight_contract.evidence import local_fingerprint, normalize_identity_text
from app.services.semantic_freight_contract.models import BR_UFS
from app.services.semantic_freight_contract.validator import entity_identity
from app.services.semantic_resolution.evidence import typed_evidence

# Rótulos de formulário. Saem do texto antes de procurar UF e nome.
# Não são um parser por delimitador: /, -, | e : viram espaço na normalização.
_LABEL_TOKENS = frozenset(
    {
        "CIDADE",
        "MUNICIPIO",
        "UF",
        "ESTADO",
        "CITY",
        "STATE",
        "DESTINO",
        "ORIGEM",
    }
)

_COUNTRY_BR = frozenset({"BR", "BRA", "BRASIL"})


class AuxiliaryGeographyCatalog:
    """Lookup auxiliar de localidades.

    Limitação registrada de propósito: base_localidades não é autoridade.
    id_cidade não é código oficial. Cada linha não é um município canônico.
    O catálogo não prova capital nem interior. Se não houver revisão
    confiável, a evidência marca a ausência — não inventamos versão official.
    """

    LIMITATION = (
        "base_localidades é lookup auxiliar de aliases e validação. "
        "id_cidade não é código oficial de município. "
        "Cada linha não é um município canônico. "
        "O catálogo não prova capital nem interior."
    )

    def __init__(
        self,
        rows: list[dict[str, Any]] | None = None,
        *,
        revision: str | None = None,
        source_name: str = "base_localidades_auxiliary",
    ) -> None:
        self.revision = revision
        self.source_name = source_name
        stored: list[dict[str, Any]] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            city = normalize_identity_text(str(row.get("cidade_nome") or row.get("city") or ""))
            state = str(row.get("uf_nome") or row.get("state") or "").strip().upper()
            aliases = [
                normalize_identity_text(str(item))
                for item in (row.get("aliases") or [])
                if normalize_identity_text(str(item))
            ]
            if not city or state not in BR_UFS:
                continue
            stored.append({"city": city, "state": state, "aliases": aliases})
        stored.sort(key=lambda item: (item["state"], item["city"], tuple(item["aliases"])))
        self.rows = stored
        # id_cidade fica de fora: não participa da identidade nem do fingerprint.
        self.fingerprint = local_fingerprint(
            {
                "source_name": source_name,
                "revision": revision,
                "rows": stored,
                "official_authority": False,
            }
        )

    def lookup(self, name: str, state: str | None = None) -> list[dict[str, Any]]:
        target = normalize_identity_text(name)
        if not target:
            return []
        found: list[dict[str, Any]] = []
        for row in self.rows:
            names = {row["city"], *row["aliases"]}
            if target not in names:
                continue
            if state is not None and row["state"] != state:
                continue
            found.append(row)
        return found


def catalog_from_locality_rows(
    rows: list[dict[str, Any]],
    *,
    revision: str | None = None,
) -> AuxiliaryGeographyCatalog:
    """Adapta linhas já carregadas no formato de base_localidades.

    Não abre sessão nem consulta o banco. id_cidade é ignorado.
    """
    return AuxiliaryGeographyCatalog(rows, revision=revision)


def municipality_entity_id(state: str, name: str) -> str | None:
    """Mesma identidade do Lote 3: sem:BR:municipality:<UF>:<NOME>."""
    identity = entity_identity(
        {
            "kind": "municipality",
            "country": "BR",
            "state": state,
            "name": name,
        }
    )
    if identity is None:
        return None
    slug = identity[3].replace(" ", "_")
    return f"sem:BR:municipality:{identity[2]}:{slug}"


def place_candidates(text: str) -> list[tuple[str, str | None]]:
    """Candidatos locais (nome, UF ou None).

    Delimitadores só separam tokens. Um candidato não é identidade
    enquanto não houver campo estruturado ou confirmação única no catálogo.
    """
    normalized = normalize_identity_text(text)
    if not normalized:
        return []
    tokens = [token for token in normalized.split(" ") if token and token not in _LABEL_TOKENS]
    ufs: list[str] = []
    city_tokens: list[str] = []
    for token in tokens:
        if token in BR_UFS:
            if token not in ufs:
                ufs.append(token)
        else:
            city_tokens.append(token)
    city = " ".join(city_tokens)
    if not city and not ufs:
        return []
    if not ufs:
        return [(city, None)] if city else []
    return [(city, uf) for uf in ufs]


def _unique(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for pair in pairs:
        if pair[0] and pair[1] in BR_UFS and pair not in found:
            found.append(pair)
    return found


def _stated_pairs(text: str | None) -> list[tuple[str, str]]:
    """Pares ditos no texto, ainda sem virar identidade."""
    pairs: list[tuple[str, str]] = []
    for name, uf in place_candidates(text or ""):
        if name and uf in BR_UFS:
            pairs.append((name, uf))
    return _unique(pairs)


def _catalog_pairs(
    candidates: list[tuple[str, str | None]],
    catalog: AuxiliaryGeographyCatalog | None,
) -> list[tuple[str, str]]:
    if catalog is None:
        return []
    found: list[tuple[str, str]] = []
    for name, uf in candidates:
        if not name:
            continue
        if uf in BR_UFS:
            if catalog.lookup(name, uf):
                found.append((name, uf))
            continue
        states = sorted({row["state"] for row in catalog.lookup(name, None) if row["state"] in BR_UFS})
        if len(states) == 1:
            found.append((name, states[0]))
    return _unique(found)


def _entity(state: str, name: str) -> dict[str, Any] | None:
    entity_id = municipality_entity_id(state, name)
    if entity_id is None:
        return None
    identity = entity_identity(
        {"kind": "municipality", "country": "BR", "state": state, "name": name}
    )
    assert identity is not None
    return {
        "entity_id": entity_id,
        "kind": "municipality",
        "country": "BR",
        "state": identity[2],
        "name": identity[3],
    }


def _geography_evidence(catalog: AuxiliaryGeographyCatalog | None, *, role: str) -> dict[str, Any]:
    revision = None if catalog is None else catalog.revision
    return typed_evidence(
        "geography",
        {
            "source_name": None if catalog is None else catalog.source_name,
            "source_revision": revision,
            "source_fingerprint": None if catalog is None else catalog.fingerprint,
            "official_authority": False,
            "geography_revision_unavailable": revision is None,
            "limitation": AuxiliaryGeographyCatalog.LIMITATION,
            "role": role,
        },
    )


def _operational_evidence(
    *,
    field: str,
    original_value: Any,
    row_ref: str | None,
    document_ref: str | None,
    job_ref: str | None,
    originals: dict[str, Any],
) -> dict[str, Any]:
    return typed_evidence(
        "operational",
        {
            "row_ref": row_ref,
            "document_ref": document_ref,
            "job_ref": job_ref,
            "field": field,
            "original_value": original_value,
            "originals": originals,
        },
    )


def _result(
    *,
    ok: bool,
    reasons: list[str],
    entity: dict[str, Any] | None,
    evidence: list[dict[str, Any]],
    confidence: str,
    source: str,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "reason_codes": list(dict.fromkeys(reasons)),
        "entity": entity,
        "evidence": evidence,
        "confidence": confidence,
        "source": source,
    }


def _text_names(text: str | None) -> list[str]:
    names: list[str] = []
    for name, _uf in place_candidates(text or ""):
        if name and name not in names:
            names.append(name)
    return names


def _catalog_cities(catalog: AuxiliaryGeographyCatalog, name: str, state: str | None) -> set[str]:
    return {
        row["city"]
        for row in catalog.lookup(name, state)
        if row.get("city") and row.get("state") in BR_UFS
    }


def identify_place(
    *,
    city: str | None = None,
    state: str | None = None,
    country: str | None = None,
    text: str | None = None,
    catalog: AuxiliaryGeographyCatalog | None = None,
    row_ref: str | None = None,
    document_ref: str | None = None,
    job_ref: str | None = None,
    field: str = "destination",
) -> dict[str, Any]:
    """Campo estruturado presente é restrição obrigatória.

    O texto só propõe o que falta, dentro dessa restrição. UF ou cidade
    estruturada não é substituída pelo texto. Incompatibilidade não escolhe
    uma das versões.
    """
    originals = {"city": city, "state": state, "country": country, "text": text}
    evidence = [
        _operational_evidence(
            field=field,
            original_value=text if text not in (None, "") else {"city": city, "state": state},
            row_ref=row_ref,
            document_ref=document_ref,
            job_ref=job_ref,
            originals=originals,
        )
    ]
    country_norm = normalize_identity_text(country or "")
    if country_norm and country_norm not in _COUNTRY_BR:
        return _result(
            ok=False,
            reasons=["unsupported_country"],
            entity=None,
            evidence=evidence,
            confidence="none",
            source="none",
        )

    raw_state = str(state).strip().upper() if state not in (None, "") else ""
    structured_city = normalize_identity_text(city or "")
    state_invalid = bool(raw_state) and raw_state not in BR_UFS
    stated = _stated_pairs(text)
    structured_pair = (structured_city, raw_state) if structured_city and raw_state in BR_UFS else None

    if state_invalid:
        reasons = ["invalid_state"]
        if stated and structured_city and any(pair != (structured_city, raw_state) for pair in stated):
            reasons.append("structured_text_conflict")
        return _result(
            ok=False,
            reasons=reasons,
            entity=None,
            evidence=evidence,
            confidence="none",
            source="none",
        )

    if structured_pair is not None:
        return _identify_complete_structured(
            structured_city=structured_city,
            raw_state=raw_state,
            structured_pair=structured_pair,
            stated=stated,
            text=text,
            catalog=catalog,
            evidence=evidence,
        )

    if raw_state in BR_UFS:
        return _identify_state_constrained(
            structured_state=raw_state,
            text=text,
            stated=stated,
            catalog=catalog,
            evidence=evidence,
        )

    if structured_city:
        return _identify_city_constrained(
            structured_city=structured_city,
            text=text,
            catalog=catalog,
            evidence=evidence,
        )

    return _identify_unconstrained_text(text=text, stated=stated, catalog=catalog, evidence=evidence)


def _reject(evidence: list[dict[str, Any]], reasons: list[str]) -> dict[str, Any]:
    return _result(
        ok=False,
        reasons=reasons,
        entity=None,
        evidence=evidence,
        confidence="none",
        source="none",
    )


def _accept(
    evidence: list[dict[str, Any]],
    entity: dict[str, Any] | None,
    *,
    confidence: str,
    source: str,
) -> dict[str, Any]:
    if entity is None:
        return _reject(evidence, ["ambiguous_municipality"])
    return _result(
        ok=True,
        reasons=[],
        entity=entity,
        evidence=evidence,
        confidence=confidence,
        source=source,
    )


def _identify_complete_structured(
    *,
    structured_city: str,
    raw_state: str,
    structured_pair: tuple[str, str],
    stated: list[tuple[str, str]],
    text: str | None,
    catalog: AuxiliaryGeographyCatalog | None,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Cidade e UF estruturadas. O texto não substitui nenhum dos dois."""
    if stated and any(pair != structured_pair for pair in stated):
        return _reject(evidence, ["structured_text_conflict"])
    if text and not stated and catalog is not None:
        confirmed = _catalog_pairs(place_candidates(text), catalog)
        if confirmed and any(pair != structured_pair for pair in confirmed):
            evidence.append(_geography_evidence(catalog, role="auxiliary_lookup"))
            return _reject(evidence, ["structured_text_conflict"])
    if catalog is not None:
        evidence.append(_geography_evidence(catalog, role="auxiliary_lookup"))
    return _accept(
        evidence,
        _entity(raw_state, structured_city),
        confidence="high",
        source="structured",
    )


def _identify_state_constrained(
    *,
    structured_state: str,
    text: str | None,
    stated: list[tuple[str, str]],
    catalog: AuxiliaryGeographyCatalog | None,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """UF estruturada restringe. O texto pode descobrir a cidade só dentro dela."""
    if any(uf != structured_state for _name, uf in stated):
        return _reject(evidence, ["structured_text_conflict"])

    matching = _unique([pair for pair in stated if pair[1] == structured_state])
    if len(matching) > 1:
        return _reject(evidence, ["ambiguous_municipality"])
    if len(matching) == 1:
        name, _uf = matching[0]
        if catalog is not None:
            evidence.append(_geography_evidence(catalog, role="auxiliary_lookup"))
            in_state = _catalog_cities(catalog, name, structured_state)
            anywhere = _catalog_cities(catalog, name, None)
            if (anywhere and not in_state) or len(in_state) > 1:
                return _reject(evidence, ["ambiguous_municipality"])
        return _accept(
            evidence,
            _entity(structured_state, name),
            confidence="medium",
            source="structured",
        )

    names = _text_names(text)
    if len(names) != 1 or catalog is None:
        return _reject(evidence, ["ambiguous_municipality"])
    evidence.append(_geography_evidence(catalog, role="candidate_validation"))
    cities = _catalog_cities(catalog, names[0], structured_state)
    if len(cities) != 1:
        return _reject(evidence, ["ambiguous_municipality"])
    return _accept(
        evidence,
        _entity(structured_state, names[0]),
        confidence="medium",
        source="catalog_validated_candidate",
    )


def _identify_city_constrained(
    *,
    structured_city: str,
    text: str | None,
    catalog: AuxiliaryGeographyCatalog | None,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Cidade estruturada restringe. O texto pode completar a UF se o catálogo confirmar o par."""
    if any(name != structured_city for name in _text_names(text)):
        return _reject(evidence, ["structured_text_conflict"])

    stated_for_city = _unique([pair for pair in _stated_pairs(text) if pair[0] == structured_city])
    if len(stated_for_city) > 1:
        return _reject(evidence, ["ambiguous_municipality"])
    if len(stated_for_city) == 1:
        _name, uf = stated_for_city[0]
        if catalog is None:
            return _reject(evidence, ["ambiguous_municipality"])
        evidence.append(_geography_evidence(catalog, role="candidate_validation"))
        if len(_catalog_cities(catalog, structured_city, uf)) != 1:
            return _reject(evidence, ["ambiguous_municipality"])
        return _accept(
            evidence,
            _entity(uf, structured_city),
            confidence="medium",
            source="catalog_validated_candidate",
        )

    if catalog is None:
        return _reject(evidence, ["ambiguous_municipality"])
    evidence.append(_geography_evidence(catalog, role="candidate_validation"))
    states = sorted(
        {
            row["state"]
            for row in catalog.lookup(structured_city, None)
            if row.get("state") in BR_UFS
        }
    )
    if len(states) != 1:
        return _reject(evidence, ["ambiguous_municipality"])
    return _accept(
        evidence,
        _entity(states[0], structured_city),
        confidence="medium",
        source="catalog_validated_candidate",
    )


def _identify_unconstrained_text(
    *,
    text: str | None,
    stated: list[tuple[str, str]],
    catalog: AuxiliaryGeographyCatalog | None,
    evidence: list[dict[str, Any]],
) -> dict[str, Any]:
    """Sem campo estruturado, o texto só vira identidade com um par único no catálogo."""
    candidates = place_candidates(text or "")
    if not candidates:
        return _reject(evidence, ["ambiguous_municipality"])
    if any(uf is None for _name, uf in candidates) or catalog is not None:
        evidence.append(_geography_evidence(catalog, role="candidate_validation"))
    confirmed = _catalog_pairs(candidates, catalog)
    if len(stated) > 1 or len(confirmed) != 1:
        return _reject(evidence, ["ambiguous_municipality"])
    name, uf = confirmed[0]
    return _accept(
        evidence,
        _entity(uf, name),
        confidence="medium",
        source="catalog_validated_candidate",
    )
