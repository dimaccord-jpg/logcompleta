"""SCRUM-187: ações e lista rolável de notificações em /perfil (somente frontend)."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from app.extensions import db
from app.models import NotificacaoInterna
from app.services.conta_multiuser_notificacao_service import (
    contar_nao_lidas,
    criar_notificacao,
    listar_notificacoes_do_user,
)
from tests.test_fase5_multiuser_painel_aumento import (
    _build_client,
    _login,
    _preparar_conta_multiuser,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app" / "templates" / "user_area.html"
BASE = ROOT / "app" / "templates" / "base.html"
CSS = ROOT / "app" / "static" / "css" / "user_area_perfil.css"
USER_AREA_PY = ROOT / "app" / "user_area.py"
WEB_PY = ROOT / "app" / "web.py"
PAINEL_PY = ROOT / "app" / "conta_multiuser_painel_routes.py"
NOTIFICACAO_SVC = ROOT / "app" / "services" / "conta_multiuser_notificacao_service.py"
MODELS_PY = ROOT / "app" / "models.py"

VIEWPORTS = (
    {"width": 1280, "height": 800, "max_height_px": 320},
    {"width": 800, "height": 900, "max_height_px": 288},
    {"width": 375, "height": 800, "max_height_px": 256},
)


def _src_template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _src_base() -> str:
    return BASE.read_text(encoding="utf-8")


def _src_css() -> str:
    return CSS.read_text(encoding="utf-8")


def _extract_script() -> str:
    src = _src_template()
    start = src.index("<script>")
    end = src.index("</script>", start) + len("</script>")
    return src[start:end]


def _build_perfil_client(app):
    from flask_login import current_user

    client = _build_client(app)

    @app.context_processor
    def _inject_nao_lidas():
        n = 0
        try:
            if getattr(current_user, "is_authenticated", False):
                n = contar_nao_lidas(current_user._get_current_object())
        except Exception:
            n = 0
        return {"notificacoes_nao_lidas": n}

    return client


def _html_perfil(client, user) -> str:
    _login(client, user)
    resp = client.get("/perfil")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def _criar(user, conta, *, mensagem: str, dedup: str, cta: str | None, lida: bool = False):
    row = criar_notificacao(
        user_id=user.id,
        conta_id=conta.id,
        tipo=NotificacaoInterna.TIPO_CONVITE_RELEVANTE,
        mensagem=mensagem,
        dedup_key=dedup,
        cta_interno=cta,
        commit=True,
    )
    if lida:
        row.read_at = datetime(2026, 1, 1, 12, 0, 0)
        db.session.add(row)
        db.session.commit()
    return row


def _item_html(html: str, mensagem: str) -> str:
    items = re.findall(
        r'(<li class="mb-2[^"]*" data-notificacao-item>.*?</li>)',
        html,
        flags=re.S,
    )
    for bloco in items:
        if mensagem in bloco:
            return bloco
    raise AssertionError(f"item não encontrado: {mensagem}")


def _bloco_notificacoes(html: str) -> str:
    start = html.index('id="notificacoes"')
    return html[start : html.index("</script>", start)]


def test_template_oculta_abrir_somente_quando_cta_e_perfil():
    src = _src_template()
    assert "perfil_cta_url = url_for('user.perfil')" in src
    assert "item.cta_url != perfil_cta_url" in src
    assert "item.cta_url" in src
    assert ">Abrir</a>" in src
    assert "data-notificacao-item" in src
    assert "data-marcar-notificacao-lida" in src
    assert "id=\"notificacao" not in src


def test_template_fetch_intercepta_somente_formulario_de_marcacao():
    src = _src_template()
    script = _extract_script()
    assert 'form.addEventListener("submit"' in script or "form.addEventListener(\"submit\"" in script
    assert "event.preventDefault()" in script
    assert "fetch(form.action" in script
    assert 'method: "POST"' in script
    assert "form[data-marcar-notificacao-lida]" in script
    assert "payload.ok !== true" in script
    assert "item.classList.remove(\"fw-semibold\")" in script
    assert "form.remove()" in script
    assert "notificacoes-nao-lidas-badge" in script
    assert "botao.disabled = true" in script
    assert "botao.disabled = false" in script
    assert "Não foi possível marcar como lida. Tente novamente." in script
    assert "url_for('multiuser_painel.api_marcar_notificacao_lida'" in src
    assert "/api/notificacoes-internas" not in src


def test_base_adiciona_somente_id_estavel_ao_badge():
    src = _src_base()
    assert 'id="notificacoes-nao-lidas-badge"' in src
    assert "{{ notificacoes_nao_lidas }}" in src
    assert "{% if notificacoes_nao_lidas|default(0) %}" in src
    assert 'href="{{ url_for(\'user.perfil\') }}#notificacoes"' in src
    assert src.count("id=\"notificacoes-nao-lidas-badge\"") == 1
    badge = src[src.index('id="notificacoes-nao-lidas-badge"') : src.index("{{ notificacoes_nao_lidas }}") + 30]
    assert "position-absolute top-0 start-100 translate-middle badge rounded-pill bg-danger" in badge


def test_css_lista_rolavel_somente_na_classe_exclusiva():
    css = _src_css()
    bloco = css[css.index(".af-profile-page .profile-notifications-list {") :]
    assert "overflow-y: auto;" in bloco
    assert "overflow-x: hidden;" in bloco
    assert "min-width: 0;" in bloco
    assert "overflow-wrap: anywhere;" in bloco
    assert "max-height: 20rem;" in css
    assert "max-height: 18rem;" in css
    assert "max-height: min(16rem, 35vh);" in css
    assert "height:" not in css[css.index(".af-profile-page .profile-notifications-list {") : css.index(".af-profile-page .profile-notifications-list li")]


def test_template_scroll_somente_quando_ha_itens():
    src = _src_template()
    inicio = src.index("{% if notificacoes_internas %}")
    ramo_else = src.index("{% else %}", inicio)
    ramo_fim = src.index("{% endif %}", ramo_else)
    ramo_com_itens = src[inicio:ramo_else]
    ramo_vazio = src[ramo_else:ramo_fim]
    assert "profile-notifications-list" in ramo_com_itens
    assert "Nenhuma notificação interna no momento." in ramo_vazio
    assert "profile-notifications-list" not in ramo_vazio


def test_backend_nao_foi_alterado():
    svc = NOTIFICACAO_SVC.read_text(encoding="utf-8")
    painel = PAINEL_PY.read_text(encoding="utf-8")
    web = WEB_PY.read_text(encoding="utf-8")
    user_area = USER_AREA_PY.read_text(encoding="utf-8")
    models = MODELS_PY.read_text(encoding="utf-8")
    assert "def listar_notificacoes_do_user(user: User, *, limite: int = 30)" in svc
    assert ".order_by(NotificacaoInterna.created_at.desc(), NotificacaoInterna.id.desc())" in svc
    assert ".limit(int(limite))" in svc
    assert "def contar_nao_lidas(user: User | None) -> int:" in svc
    assert "def api_marcar_notificacao_lida(notificacao_id):" in painel
    assert 'return jsonify({"ok": True})' in painel
    assert '"notificacoes_nao_lidas": nao_lidas' in web
    assert "notificacoes_internas=listar_notificacoes_do_user(user_obj)" in user_area
    assert "class NotificacaoInterna(db.Model):" in models
    assert "cta_url =" not in user_area
    assert "preventDefault" not in user_area
    assert "profile-notifications-list" not in user_area


def test_perfil_cta_perfil_nao_mostra_abrir_e_multiuser_mostra(app):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "ntf-cta", qtd=5, email="ntf.cta@test.com"
        )
        _criar(user, conta, mensagem="Aviso do perfil", dedup="cta-perfil", cta="user.perfil")
        _criar(
            user,
            conta,
            mensagem="Aviso da equipe",
            dedup="cta-mu",
            cta="multiuser_painel.gestao_multiuser",
        )
        html = _html_perfil(_build_perfil_client(app), user)
        bloco_perfil = _item_html(html, "Aviso do perfil")
        bloco_mu = _item_html(html, "Aviso da equipe")
        assert "Abrir" not in bloco_perfil
        assert 'href="/perfil"' not in bloco_perfil
        assert "Abrir" in bloco_mu
        assert 'href="/gestao-multiuser"' in bloco_mu
        assert "Aviso do perfil" in html
        assert "Aviso da equipe" in html


def test_estado_vazio_preserva_texto_sem_lista_rolavel(app):
    with app.app_context():
        _conta, user = _preparar_conta_multiuser(
            "ntf-empty", qtd=5, email="ntf.empty@test.com"
        )
        html = _html_perfil(_build_perfil_client(app), user)
        bloco = _bloco_notificacoes(html).split("<script>", 1)[0]
        assert "Nenhuma notificação interna no momento." in bloco
        assert "profile-notifications-list" not in bloco
        assert "data-notificacao-item>" not in bloco


def test_lista_renderiza_mais_recente_primeiro(app):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "ntf-ord", qtd=5, email="ntf.ord@test.com"
        )
        antiga = _criar(user, conta, mensagem="Mensagem antiga", dedup="ord-old", cta=None)
        recente = _criar(user, conta, mensagem="Mensagem recente", dedup="ord-new", cta=None)
        antiga.created_at = datetime(2026, 1, 1, 10, 0, 0)
        recente.created_at = datetime(2026, 9, 21, 10, 0, 0)
        db.session.commit()
        itens = listar_notificacoes_do_user(user)
        assert [item.mensagem for item in itens[:2]] == ["Mensagem recente", "Mensagem antiga"]
        html = _html_perfil(_build_perfil_client(app), user)
        assert html.index("Mensagem recente") < html.index("Mensagem antiga")
        assert "profile-notifications-list" in html
        assert html.count("data-notificacao-item>") == 2


def test_marcar_como_lida_backend_persiste_e_recarrega_sem_acao(app):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "ntf-lida", qtd=5, email="ntf.lida@test.com"
        )
        row = _criar(user, conta, mensagem="Pendente persistente", dedup="lida-1", cta="user.perfil")
        client = _build_perfil_client(app)
        html = _html_perfil(client, user)
        assert "Pendente persistente" in html
        assert "Marcar como lida" in html
        assert "fw-semibold" in _item_html(html, "Pendente persistente")
        assert f"/api/notificacoes-internas/{row.id}/lida" in html
        assert contar_nao_lidas(user) == 1

        resp = client.post(
            f"/api/notificacoes-internas/{row.id}/lida",
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert resp.request.path.endswith(f"/api/notificacoes-internas/{row.id}/lida")

        reload_html = client.get("/perfil").get_data(as_text=True)
        assert client.get("/perfil").status_code == 200
        bloco = _item_html(reload_html, "Pendente persistente")
        assert "Pendente persistente" in bloco
        assert "Marcar como lida" not in bloco
        assert "fw-semibold" not in bloco
        assert "Abrir" not in bloco
        assert contar_nao_lidas(user) == 0


def test_formulario_de_marcacao_nao_e_id_duplicado(app):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "ntf-ids", qtd=5, email="ntf.ids@test.com"
        )
        _criar(user, conta, mensagem="Um", dedup="id-1", cta=None)
        _criar(user, conta, mensagem="Dois", dedup="id-2", cta=None)
        html = _html_perfil(_build_perfil_client(app), user)
        assert html.count("data-marcar-notificacao-lida>") == 2
        assert html.count('id="marcar-notificacao') == 0
        assert html.count('id="notificacao-item') == 0


def _fixture_html(*, badge="10", items=None, include_list=True, extra_items=0) -> str:
    css = _src_css()
    script = _extract_script()
    rows = items if items is not None else [
        {
            "mensagem": "Mensagem recente visível",
            "lida": False,
            "cta": "/gestao-multiuser",
            "action": "/api/notificacoes-internas/11/lida",
        },
        {
            "mensagem": "Mensagem antiga abaixo",
            "lida": False,
            "cta": None,
            "action": "/api/notificacoes-internas/10/lida",
        },
    ]
    lis = []
    for row in rows:
        classes = "mb-2 fw-semibold" if not row.get("lida") else "mb-2"
        cta = (
            f'<a href="{row["cta"]}" class="ms-1">Abrir</a>'
            if row.get("cta")
            else ""
        )
        form = ""
        if not row.get("lida"):
            form = (
                f'<form method="post" action="{row["action"]}" class="d-inline" '
                f'data-marcar-notificacao-lida>'
                f'<button type="submit" class="btn btn-link btn-sm p-0 align-baseline">'
                f"Marcar como lida</button></form>"
            )
        lis.append(
            f'<li class="{classes}" data-notificacao-item>'
            f'{row["mensagem"]}{cta}{form}</li>'
        )
    for idx in range(extra_items):
        lis.append(
            f'<li class="mb-2 fw-semibold" data-notificacao-item>'
            f"Item extra {idx} "
            f'<form method="post" action="/api/notificacoes-internas/{100 + idx}/lida" '
            f'class="d-inline" data-marcar-notificacao-lida>'
            f'<button type="submit">Marcar como lida</button></form></li>'
        )
    lista = ""
    if include_list:
        lista = (
            '<ul class="profile-notifications-list list-unstyled small mb-0" '
            'tabindex="0" aria-label="Lista de notificações">'
            + "".join(lis)
            + "</ul>"
        )
    else:
        lista = '<p class="small profile-card-copy mb-0">Nenhuma notificação interna no momento.</p>'
    badge_html = ""
    if badge is not None:
        badge_html = (
            f'<span id="notificacoes-nao-lidas-badge" '
            f'class="position-absolute badge rounded-pill bg-danger">{badge}</span>'
        )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<base href="https://example.test/">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
{css}
</style>
</head>
<body>
<div class="container py-4 af-profile-page">
  <div class="profile-secondary-grid">
    <a class="profile-payment-link" href="/contrate-um-plano">
      <div class="card shadow-sm h-100"><div class="card-body">Pagamento</div></div>
    </a>
    <div class="card shadow-sm h-100">
      <div class="card-body">
        <h5 class="card-title mb-0">Notificações</h5>
        <small class="profile-card-kicker">Alertas e comunicação</small>
        <p class="profile-card-copy">Descrição fora da área rolável.</p>
        <div id="notificacoes">{lista}</div>
      </div>
    </div>
  </div>
</div>
<a href="/perfil#notificacoes">{badge_html}</a>
{script}
</body>
</html>"""


@pytest.fixture(scope="module")
def browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        chromium = pw.chromium.launch(headless=True, channel="chrome")
    except Exception:
        chromium = pw.chromium.launch(headless=True)
    yield chromium
    chromium.close()
    pw.stop()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    pg = context.new_page()
    yield pg
    context.close()


def _open(page, html: str):
    page.set_content(html, wait_until="domcontentloaded")


def _mock_lida(page, *, status=200, body='{"ok": true}', hold=False, invalid=False):
    state = {"requests": [], "pending": []}

    def _handle(route):
        req = route.request
        state["requests"].append({"url": req.url, "method": req.method})
        if hold:
            state["pending"].append(route)
            return
        if invalid:
            route.fulfill(status=status, content_type="text/plain", body="nao-json")
            return
        route.fulfill(status=status, content_type="application/json", body=body)

    page.route("**/api/notificacoes-internas/**", _handle)
    return state


def test_browser_sucesso_nao_navega_atualiza_item_e_decrementa_badge(page):
    mock = _mock_lida(page)
    _open(page, _fixture_html(badge="10"))
    page.click("form[data-marcar-notificacao-lida] button")
    page.locator("[data-notificacao-item]").first.locator("form").wait_for(state="detached")
    item = page.locator("[data-notificacao-item]").first
    assert "fw-semibold" not in (item.get_attribute("class") or "")
    assert item.locator("form[data-marcar-notificacao-lida]").count() == 0
    assert "Mensagem recente visível" in item.inner_text()
    assert item.locator("a", has_text="Abrir").count() == 1
    assert page.locator("#notificacoes-nao-lidas-badge").inner_text() == "9"
    assert page.locator("#notificacoes").count() == 1
    assert '"ok": true' not in page.content()
    assert len(mock["requests"]) == 1
    assert mock["requests"][0]["method"] == "POST"
    assert mock["requests"][0]["url"].endswith("/api/notificacoes-internas/11/lida")
    assert page.locator("[data-notificacao-item]").count() == 2


def test_browser_badge_um_para_zero_desaparece(page):
    _mock_lida(page)
    _open(page, _fixture_html(badge="1"))
    page.click("form[data-marcar-notificacao-lida] button")
    page.wait_for_function("() => !document.getElementById('notificacoes-nao-lidas-badge')")
    assert page.locator("#notificacoes-nao-lidas-badge").count() == 0


def test_browser_erro_mantem_nao_lido_acao_e_badge(page):
    _mock_lida(page, status=500, body='{"ok": false}')
    _open(page, _fixture_html(badge="10"))
    page.click("form[data-marcar-notificacao-lida] button")
    page.wait_for_selector("[data-notificacao-feedback]")
    item = page.locator("[data-notificacao-item]").first
    assert "fw-semibold" in (item.get_attribute("class") or "")
    assert item.locator("form[data-marcar-notificacao-lida]").count() == 1
    assert page.locator("#notificacoes-nao-lidas-badge").inner_text() == "10"
    assert "Não foi possível marcar como lida" in item.inner_text()
    assert item.locator("button").is_enabled()


@pytest.mark.parametrize("modo", ["http", "rede", "json", "not-ok"])
def test_browser_erros_nao_alteram_estado(page, modo):
    if modo == "http":
        _mock_lida(page, status=404, body='{"ok": false}')
    elif modo == "rede":
        page.route("**/api/notificacoes-internas/**", lambda route: route.abort("connectionrefused"))
    elif modo == "json":
        _mock_lida(page, invalid=True)
    else:
        _mock_lida(page, status=200, body='{"ok": false}')
    _open(page, _fixture_html(badge="3"))
    page.click("form[data-marcar-notificacao-lida] button")
    page.wait_for_selector("[data-notificacao-feedback]")
    item = page.locator("[data-notificacao-item]").first
    assert "fw-semibold" in (item.get_attribute("class") or "")
    assert item.locator("form[data-marcar-notificacao-lida]").count() == 1
    assert page.locator("#notificacoes-nao-lidas-badge").inner_text() == "3"


def test_browser_duplo_clique_nao_reenvia(page):
    mock = _mock_lida(page, hold=True)
    _open(page, _fixture_html(badge="4"))
    page.click("form[data-marcar-notificacao-lida] button")
    page.click("form[data-marcar-notificacao-lida] button", force=True)
    page.wait_for_timeout(100)
    assert len(mock["requests"]) == 1
    for pending in mock["pending"]:
        pending.fulfill(status=200, content_type="application/json", body='{"ok": true}')
    page.wait_for_function(
        "() => document.querySelectorAll('form[data-marcar-notificacao-lida]').length === 1"
    )


def test_browser_lista_curta_nao_estica_e_longa_rola(page):
    _open(page, _fixture_html())
    page.set_viewport_size({"width": 1280, "height": 800})
    curto = page.evaluate(
        """() => {
            const list = document.querySelector('.profile-notifications-list');
            const style = getComputedStyle(list);
            return {
                height: list.getBoundingClientRect().height,
                maxHeight: style.maxHeight,
                overflowY: style.overflowY,
                scrollable: list.scrollHeight > list.clientHeight + 1
            };
        }"""
    )
    assert curto["overflowY"] == "auto"
    assert curto["maxHeight"] == "320px"
    assert curto["height"] < 320
    assert curto["scrollable"] is False

    _open(page, _fixture_html(extra_items=20))
    longo = page.evaluate(
        """() => {
            const list = document.querySelector('.profile-notifications-list');
            const first = list.querySelector('[data-notificacao-item]');
            const style = getComputedStyle(list);
            return {
                height: list.getBoundingClientRect().height,
                maxHeight: style.maxHeight,
                overflowY: style.overflowY,
                scrollable: list.scrollHeight > list.clientHeight + 1,
                firstText: first.textContent
            };
        }"""
    )
    assert longo["overflowY"] == "auto"
    assert longo["height"] == pytest.approx(320, abs=1)
    assert longo["scrollable"] is True
    assert "Mensagem recente visível" in longo["firstText"]


def test_browser_estado_vazio_sem_scrollbar(page):
    _open(page, _fixture_html(include_list=False, badge=None))
    assert page.locator(".profile-notifications-list").count() == 0
    assert page.locator("text=Nenhuma notificação interna no momento.").count() == 1


def test_browser_viewports_sem_overflow_e_ctas_acessiveis(page):
    _open(page, _fixture_html(extra_items=16))
    for viewport in VIEWPORTS:
        page.set_viewport_size({"width": viewport["width"], "height": viewport["height"]})
        metrics = page.evaluate(
            """(expected) => {
                const list = document.querySelector('.profile-notifications-list');
                const style = getComputedStyle(list);
                const last = list.querySelector('[data-notificacao-item]:last-child');
                last.scrollIntoView();
                const btn = last.querySelector('button');
                const rect = btn.getBoundingClientRect();
                const listRect = list.getBoundingClientRect();
                return {
                    docOverflow: document.documentElement.scrollWidth > window.innerWidth + 2,
                    listOverflowX: list.scrollWidth > list.clientWidth + 2,
                    maxHeight: style.maxHeight,
                    overflowY: style.overflowY,
                    btnVisible: rect.height > 0 && rect.bottom <= listRect.bottom + 2 && rect.top >= listRect.top - 2
                };
            }""",
            viewport["max_height_px"],
        )
        assert metrics["docOverflow"] is False
        assert metrics["listOverflowX"] is False
        assert metrics["overflowY"] == "auto"
        assert metrics["maxHeight"] == f"{viewport['max_height_px']}px"
        assert metrics["btnVisible"] is True
