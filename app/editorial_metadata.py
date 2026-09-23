"""
Metadados editoriais do pipeline da Júlia.

Intenções: news, analysis, evergreen.
Conteúdo antigo sem esse bloco continua nas colunas já publicadas.
O bloco novo fica em NoticiaPortal.assets_canais_json["editorial"],
sem coluna nem migration.
"""
from __future__ import annotations

import json
import re
from typing import Any

INTENCOES_EDITORIAIS = ("news", "analysis", "evergreen")
INTENCOES_ARTIGO = ("analysis", "evergreen")

PERFIS_INTERESSE = (
    "motorista",
    "operador",
    "analista",
    "gestor",
    "gerente de supply chain",
    "embarcador",
    "transportador",
)

CAMPOS_EDITORIAIS = (
    "intencao",
    "tema",
    "perfil",
    "intencao_busca",
    "habilidade_id",
    "titulo",
    "meta_description",
    "alt_imagem",
)

_TAG_INTENCAO = re.compile(
    r"^\s*\[(news|analysis|evergreen)\]\s*",
    flags=re.IGNORECASE,
)
_H1_ABRE = re.compile(r"<\s*h1(\b[^>]*)>", flags=re.IGNORECASE)
_H1_FECHA = re.compile(r"<\s*/\s*h1\s*>", flags=re.IGNORECASE)
_EVERGREEN_PADROES = (
    re.compile(r"\bo que [eé]\b", re.IGNORECASE),
    re.compile(r"\bcomo funciona\b", re.IGNORECASE),
    re.compile(r"\bcomo analisar\b", re.IGNORECASE),
    re.compile(r"\bcomo calcular\b", re.IGNORECASE),
    re.compile(r"\bcomo auditar\b", re.IGNORECASE),
    re.compile(r"\bo que considerar\b", re.IGNORECASE),
    re.compile(r"\bqual a diferen[cç]a\b", re.IGNORECASE),
    re.compile(r"\bquais as diferen[cç]as\b", re.IGNORECASE),
    re.compile(r"\bdiferen[cç]a entre\b", re.IGNORECASE),
    re.compile(r"\bquais indicadores\b", re.IGNORECASE),
    re.compile(r"\bo que significa\b", re.IGNORECASE),
)
_META_PADRAO = "Notícia sobre logística e transporte rodoviário."


def extrair_tag_intencao(titulo: str | None) -> tuple[str | None, str]:
    """Lê um marcador opcional [news|analysis|evergreen] no início do título da pauta."""
    bruto = titulo or ""
    encontrado = _TAG_INTENCAO.match(bruto)
    if not encontrado:
        return None, re.sub(r"\s+", " ", bruto).strip()
    tag = encontrado.group(1).lower()
    resto = re.sub(r"\s+", " ", bruto[encontrado.end():]).strip()
    return tag, resto


def titulo_para_redacao(titulo: str | None) -> str:
    _tag, limpo = extrair_tag_intencao(titulo)
    return limpo


def titulo_publico(titulo: str | None) -> str:
    return titulo_para_redacao(titulo)


def intencao_explicita_do_payload(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None
    bruto = payload.get("intencao_editorial")
    metadados = payload.get("metadados")
    if not bruto and isinstance(metadados, dict):
        bruto = metadados.get("intencao_editorial")
    if isinstance(bruto, str):
        return bruto.strip() or None
    return None


def _intencao_artigo(valor: str | None) -> str | None:
    normalizado = (valor or "").strip().lower()
    if normalizado in INTENCOES_ARTIGO:
        return normalizado
    return None


def titulo_indica_evergreen(titulo: str | None) -> bool:
    texto = titulo_para_redacao(titulo)
    if not texto:
        return False
    return any(padrao.search(texto) for padrao in _EVERGREEN_PADROES)


def pauta_marcada_evergreen_explicita(titulo: str | None) -> bool:
    """True quando o título traz o marcador inequívoco [evergreen]."""
    tag, _resto = extrair_tag_intencao(titulo)
    return tag == "evergreen"


def pauta_candidata_evergreen_automatico(titulo: str | None) -> bool:
    """
    Elegibilidade segura para automação evergreen (sem chamada de IA):
    - marcador [evergreen]; ou
    - padrões determinísticos já usados por titulo_indica_evergreen;
    - nunca trata [analysis]/[news] como evergreen.
    """
    tag, titulo_limpo = extrair_tag_intencao(titulo)
    if tag == "evergreen":
        return True
    if tag in ("analysis", "news"):
        return False
    return titulo_indica_evergreen(titulo_limpo)


def resolver_intencao_editorial(
    *,
    tipo_missao: str,
    titulo: str | None = None,
    explicita: str | None = None,
) -> str:
    """
    Notícia automática permanece news.
    Artigo manual usa intenção explícita, marcador no título ou padrão de busca;
    sem isso, cai em analysis.
    """
    if (tipo_missao or "noticia").strip().lower() != "artigo":
        return "news"
    tag, titulo_limpo = extrair_tag_intencao(titulo)
    escolhida = _intencao_artigo(explicita) or _intencao_artigo(tag)
    if escolhida:
        return escolhida
    if titulo_indica_evergreen(titulo_limpo):
        return "evergreen"
    return "analysis"


def listar_habilidades() -> list[dict[str, str]]:
    """Habilidades reais do shell. Não duplica a lista de URLs."""
    from app.capability_taxonomy import DESTINATIONS
    from app.shell_navigation import SHELL_NAV_CATALOG

    itens: list[dict[str, str]] = []
    for entry in SHELL_NAV_CATALOG:
        if entry.group != "habilidades":
            continue
        path = ""
        destination_id = entry.destination_id or ""
        if destination_id and destination_id in DESTINATIONS:
            path = DESTINATIONS[destination_id].url or ""
        if not path:
            path = entry.fallback_path or ""
        if not path:
            continue
        itens.append(
            {
                "id": entry.id,
                "label": entry.label,
                "path": path,
                "destination_id": destination_id,
            }
        )
    return itens


def ids_habilidades_para_prompt() -> str:
    ids = [item["id"] for item in listar_habilidades()]
    return ", ".join(ids) if ids else "(nenhuma; deixe vazio)"


def resolver_habilidade(valor: str | None) -> dict[str, str] | None:
    bruto = (valor or "").strip()
    if not bruto:
        return None
    chave = bruto.casefold().rstrip("/")
    for item in listar_habilidades():
        candidatos = {
            item["id"].casefold(),
            item["path"].casefold().rstrip("/"),
        }
        if item.get("destination_id"):
            candidatos.add(item["destination_id"].casefold())
        if chave in candidatos:
            return item
    return None


def normalizar_perfil(valor: str | None) -> str:
    bruto = re.sub(r"\s+", " ", (valor or "").strip()).casefold()
    for perfil in PERFIS_INTERESSE:
        if bruto == perfil:
            return perfil
    return ""


def rebaixar_h1(html: str | None) -> str:
    """A página já tem um H1. Conteúdo gerado começa em H2."""
    if not html:
        return html or ""
    texto = _H1_ABRE.sub(r"<h2\1>", str(html))
    return _H1_FECHA.sub("</h2>", texto)


def _cortar(texto: str, limite: int) -> str:
    limpo = re.sub(r"\s+", " ", (texto or "").strip())
    if len(limpo) <= limite:
        return limpo
    corte = limpo[:limite].rsplit(" ", 1)[0].rstrip(" .,;:")
    return corte or limpo[:limite].rstrip()


def _normalizar_texto_meta(texto: str | None) -> str:
    return re.sub(r"\s+", " ", (texto or "").replace("\n", " ")).strip()


def escolher_meta_description(
    titulo: str | None,
    meta_editorial: str | None = None,
    resumo: str | None = None,
    subtitulo: str | None = None,
) -> str:
    """Primeiro candidato que, após normalização, não repete o título."""
    titulo_norm = _normalizar_texto_meta(titulo).casefold()
    for candidato in (meta_editorial, resumo, subtitulo):
        limpo = _normalizar_texto_meta(candidato)
        if limpo and limpo.casefold() != titulo_norm:
            return limpo
    return _META_PADRAO


def derivar_meta_description(
    titulo: str,
    resumo: str,
    explicita: str | None,
    subtitulo: str | None = None,
) -> str:
    escolhida = escolher_meta_description(titulo, explicita, resumo, subtitulo)
    if escolhida == _META_PADRAO:
        return escolhida
    return _cortar(escolhida, 160)


def derivar_alt(alt_modelo: str | None, tema: str | None, titulo: str | None) -> str:
    alt = re.sub(r"\s+", " ", (alt_modelo or "").strip())
    genericos = {"capa da matéria", "capa da materia", "imagem", "foto", "image"}
    if alt and alt.casefold() not in genericos:
        return _cortar(alt, 180)
    base = re.sub(r"\s+", " ", (tema or titulo or "logística").strip())
    return _cortar(f"Imagem ilustrativa de {base}", 180)


def _objetivo_lead_valido(valor: str | None) -> str:
    from app.run_julia_agente_qualidade import OBJETIVOS_LEAD_VALIDOS

    bruto = (valor or "").strip()
    if bruto.lower() in OBJETIVOS_LEAD_VALIDOS:
        return bruto
    return ""


def completar_metadados_editoriais(
    conteudo: dict,
    *,
    tipo_missao: str,
    titulo_pauta: str | None,
    explicita: str | None = None,
) -> dict:
    """Preenche SEO e classificação com o que a redação já devolveu. Sem nova chamada de modelo."""
    intencao = resolver_intencao_editorial(
        tipo_missao=tipo_missao,
        titulo=titulo_pauta,
        explicita=explicita or conteudo.get("intencao_editorial"),
    )
    titulo_limpo = titulo_para_redacao(titulo_pauta)
    titulo = titulo_publico(conteudo.get("titulo_julia")) or titulo_limpo or "Conteúdo logístico"
    conteudo["titulo_julia"] = titulo
    resumo = conteudo.get("resumo_julia") or ""
    if conteudo.get("conteudo_completo"):
        conteudo["conteudo_completo"] = rebaixar_h1(conteudo.get("conteudo_completo"))

    habilidade = resolver_habilidade(conteudo.get("habilidade_relacionada"))
    if habilidade:
        conteudo["habilidade_relacionada"] = habilidade["id"]
        conteudo["cta"] = habilidade["label"]
        if intencao == "news":
            conteudo["objetivo_lead"] = ""
        else:
            conteudo["objetivo_lead"] = _objetivo_lead_valido(conteudo.get("objetivo_lead"))
    else:
        conteudo["habilidade_relacionada"] = ""
        conteudo["cta"] = ""
        conteudo["objetivo_lead"] = ""

    if intencao == "evergreen":
        intencao_busca = re.sub(r"\s+", " ", (conteudo.get("intencao_busca") or "").strip())
        if not intencao_busca:
            intencao_busca = titulo_limpo or titulo
    elif intencao == "analysis":
        intencao_busca = re.sub(r"\s+", " ", (conteudo.get("intencao_busca") or "").strip())
    else:
        intencao_busca = ""

    tema = re.sub(r"\s+", " ", (conteudo.get("tema") or "").strip()) or titulo_limpo or titulo
    tema = _cortar(tema, 120)
    perfil = normalizar_perfil(conteudo.get("perfil_interesse") or conteudo.get("perfil"))
    meta = derivar_meta_description(
        titulo,
        resumo,
        conteudo.get("meta_description"),
        subtitulo=conteudo.get("subtitulo"),
    )
    alt = derivar_alt(conteudo.get("alt_imagem"), tema, titulo)
    editorial = {
        "intencao": intencao,
        "tema": tema,
        "perfil": perfil,
        "intencao_busca": _cortar(intencao_busca, 180),
        "habilidade_id": habilidade["id"] if habilidade else "",
        "titulo": _cortar(titulo, 255),
        "meta_description": meta,
        "alt_imagem": alt,
    }
    conteudo["intencao_editorial"] = intencao
    conteudo["editorial"] = editorial
    conteudo["meta_description"] = meta
    conteudo["alt_imagem"] = alt
    conteudo["tema"] = tema
    conteudo["perfil_interesse"] = perfil
    conteudo["intencao_busca"] = editorial["intencao_busca"]
    return conteudo


def anexar_editorial_em_assets(assets_json: str | None, editorial: dict | None) -> str | None:
    if not isinstance(editorial, dict) or not editorial:
        return assets_json
    payload: dict[str, Any] = {}
    if assets_json:
        try:
            parsed = json.loads(assets_json)
            if isinstance(parsed, dict):
                payload = parsed
            else:
                payload = {"assets_raw": parsed}
        except Exception:
            payload = {"assets_raw": assets_json}
    payload["editorial"] = {campo: editorial.get(campo) or "" for campo in CAMPOS_EDITORIAIS}
    return json.dumps(payload, ensure_ascii=False)


def ler_bloco_editorial(assets_json: str | dict | None) -> dict:
    if isinstance(assets_json, dict):
        payload = assets_json
    elif isinstance(assets_json, str) and assets_json.strip():
        try:
            payload = json.loads(assets_json)
        except Exception:
            return {}
    else:
        return {}
    bloco = payload.get("editorial") if isinstance(payload, dict) else None
    return bloco if isinstance(bloco, dict) else {}


def apresentacao_editorial(noticia: Any) -> dict[str, str]:
    """SEO da página individual. Sem bloco novo, usa título e resumo já persistidos."""
    editorial = ler_bloco_editorial(getattr(noticia, "assets_canais_json", None))
    intencao_bruta = editorial.get("intencao")
    intencao_armazenada = intencao_bruta if intencao_bruta in INTENCOES_EDITORIAIS else None
    tipo = (getattr(noticia, "tipo", None) or "noticia").strip().lower()
    intencao = intencao_armazenada or ("news" if tipo != "artigo" else "analysis")
    titulo = (
        getattr(noticia, "titulo_julia", None)
        or editorial.get("titulo")
        or "Conteúdo AgenteFrete"
    )
    titulo = titulo_publico(str(titulo).strip()) or "Conteúdo AgenteFrete"
    meta = escolher_meta_description(
        titulo,
        editorial.get("meta_description"),
        getattr(noticia, "resumo_julia", None),
        getattr(noticia, "subtitulo", None),
    )
    alt = editorial.get("alt_imagem") or derivar_alt(None, editorial.get("tema"), titulo)
    habilidade = resolver_habilidade(editorial.get("habilidade_id"))
    schema_type = "Article" if intencao_armazenada in INTENCOES_ARTIGO else "NewsArticle"
    return {
        "intencao": intencao,
        "titulo": titulo,
        "meta_description": meta,
        "alt": str(alt),
        "intencao_busca": str(editorial.get("intencao_busca") or ""),
        "tema": str(editorial.get("tema") or ""),
        "perfil": str(editorial.get("perfil") or ""),
        "habilidade_id": habilidade["id"] if habilidade else "",
        "habilidade_href": habilidade["path"] if habilidade else "",
        "habilidade_rotulo": habilidade["label"] if habilidade else "",
        "schema_type": schema_type,
    }
