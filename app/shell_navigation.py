"""Navegação principal do shell AgenteFrete.

Fonte única dos destinos de Início, Habilidades e Feed.
Templates só renderizam esta sequência — desktop e mobile não mantêm listas próprias.

Personalização futura deve reordenar SHELL_NAV_CATALOG (em especial o grupo
``habilidades``). Não duplique rótulos em templates de página.

URLs e a exigência de login vêm de ``capability_taxonomy.DESTINATIONS``.
Identificadores internos (julia, roberto, cleide, agente_compara) permanecem
nos destinos; a navegação mostra só o rótulo externo.

A Home reutiliza o mesmo catálogo. Entradas com ``home_summary`` aparecem como
habilidades sugeridas, na ordem do catálogo. Um perfil futuro pode filtrar ou
reordenar ``SHELL_NAV_CATALOG`` sem criar outra lista de destinos.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from app.capability_taxonomy import DESTINATIONS

UrlFor = Callable[..., str]
HasEndpoint = Callable[[str], bool]


@dataclass(frozen=True)
class ShellNavEntry:
    id: str
    label: str
    group: str
    icon: str
    endpoint: str | None = None
    endpoint_values: tuple[tuple[str, str], ...] = ()
    destination_id: str | None = None
    fallback_path: str | None = None
    # None = usar requires_login do destino na taxonomia.
    requires_login: bool | None = None
    active_exact: bool = False
    active_prefixes: tuple[str, ...] = ()
    # Texto curto da Home. Ausente = a entrada não vira card de habilidade.
    home_summary: str | None = None


# Ordem de aparição. Grupos sem rótulo (inicio, feed) não ganham cabeçalho.
GROUP_LABELS: dict[str, str] = {
    "habilidades": "Habilidades",
}

SHELL_NAV_CATALOG: tuple[ShellNavEntry, ...] = (
    ShellNavEntry(
        id="inicio",
        label="Início",
        group="inicio",
        icon="bi-house-door",
        endpoint="index",
        fallback_path="/",
        requires_login=False,
        active_exact=True,
    ),
    ShellNavEntry(
        id="consultar_agentefrete",
        label="Consultar o AgenteFrete",
        group="habilidades",
        icon="bi-chat-dots",
        endpoint="chat_julia",
        endpoint_values=(("mode", "operational"),),
        destination_id="julia_operational",
        active_prefixes=("/chat_julia",),
    ),
    ShellNavEntry(
        id="analisar_fretes",
        label="Analisar fretes",
        group="habilidades",
        icon="bi-bar-chart-line",
        endpoint="fretes",
        destination_id="roberto_bi",
        # GET /fretes é público. O shell não coloca login na frente desse destino.
        requires_login=False,
        active_prefixes=("/fretes",),
        home_summary="Indicadores e leitura dos fretes.",
    ),
    ShellNavEntry(
        id="auditar_cobrancas",
        label="Auditar cobranças de frete",
        group="habilidades",
        icon="bi-clipboard-check",
        endpoint="cleide.cleide_auditoria",
        destination_id="cleide_freight_audit",
        fallback_path="/auditoria-frete",
        active_prefixes=("/auditoria-frete",),
        home_summary="Conferência de cobranças de frete.",
    ),
    ShellNavEntry(
        id="comparar_tabelas",
        label="Comparar tabelas",
        group="habilidades",
        icon="bi-table",
        endpoint="agente_compara.agente_compara_page",
        destination_id="agente_compara",
        fallback_path="/agente-compara",
        active_prefixes=("/agente-compara",),
        home_summary="Comparação entre tabelas de frete.",
    ),
    ShellNavEntry(
        id="feed",
        label="Feed",
        group="feed",
        icon="bi-rss",
        endpoint="feed",
        destination_id="feed",
        fallback_path="/feed",
        active_prefixes=("/feed",),
    ),
)


def _requires_login(entry: ShellNavEntry) -> bool:
    if entry.requires_login is not None:
        return entry.requires_login
    if not entry.destination_id:
        return False
    return bool(DESTINATIONS[entry.destination_id].requires_login)


def _canonical_path(entry: ShellNavEntry, *, url_for: UrlFor, has_endpoint: HasEndpoint) -> str:
    if entry.endpoint and has_endpoint(entry.endpoint):
        return url_for(entry.endpoint, **dict(entry.endpoint_values))
    if entry.fallback_path:
        return entry.fallback_path
    if entry.destination_id:
        url = DESTINATIONS[entry.destination_id].url
        if url:
            return url
    raise ValueError(f"Destino de navegação sem URL: {entry.id}")


def _login_href(url_for: UrlFor, canonical: str) -> str:
    """Preserva query do destino. url_for deixa '?' cru e parte o parâmetro next."""
    login_path = url_for("login")
    if any(char in canonical for char in "?&"):
        return f"{login_path}?next={quote(canonical, safe='')}"
    return url_for("login", next=canonical)


def _is_active(entry: ShellNavEntry, request_path: str) -> bool:
    path = request_path or "/"
    if entry.active_exact:
        return path == "/"
    for prefix in entry.active_prefixes:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def build_home_skills(
    *,
    authenticated: bool,
    url_for: UrlFor,
    has_endpoint: HasEndpoint,
) -> list[dict[str, Any]]:
    """Habilidades da Home, na ordem do catálogo, com a mesma regra de login do shell."""
    skills: list[dict[str, Any]] = []
    for entry in SHELL_NAV_CATALOG:
        if not entry.home_summary:
            continue
        canonical = _canonical_path(entry, url_for=url_for, has_endpoint=has_endpoint)
        requires_login = _requires_login(entry)
        if requires_login and not authenticated:
            href = _login_href(url_for, canonical)
        else:
            href = canonical
        skills.append({
            "id": entry.id,
            "label": entry.label,
            "summary": entry.home_summary,
            "icon": entry.icon,
            "href": href,
            "destination_id": entry.destination_id,
            "requires_login": requires_login,
        })
    return skills


def home_skill_presentation(skills: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Rótulo e resumo para o handoff visual. A URL continua vindo do payload."""
    presentation: dict[str, dict[str, str]] = {}
    for skill in skills:
        destination_id = skill.get("destination_id")
        if not destination_id:
            continue
        presentation[str(destination_id)] = {
            "label": str(skill.get("label") or ""),
            "summary": str(skill.get("summary") or ""),
        }
    return presentation


def build_shell_navigation(
    *,
    authenticated: bool,
    request_path: str,
    url_for: UrlFor,
    has_endpoint: HasEndpoint,
) -> dict[str, Any]:
    """Monta as seções do shell. Visitante em destino protegido vai para /login?next=."""
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    for entry in SHELL_NAV_CATALOG:
        canonical = _canonical_path(entry, url_for=url_for, has_endpoint=has_endpoint)
        requires_login = _requires_login(entry)
        if requires_login and not authenticated:
            href = _login_href(url_for, canonical)
        else:
            href = canonical
        item = {
            "id": entry.id,
            "label": entry.label,
            "icon": entry.icon,
            "href": href,
            "active": _is_active(entry, request_path),
            "destination_id": entry.destination_id,
            "requires_login": requires_login,
        }
        if current is None or current["id"] != entry.group:
            current = {
                "id": entry.group,
                "label": GROUP_LABELS.get(entry.group),
                "links": [],
            }
            sections.append(current)
        current["links"].append(item)

    return {"sections": sections}
