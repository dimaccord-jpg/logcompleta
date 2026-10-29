import importlib
import inspect
import os
import re
from pathlib import Path
from types import SimpleNamespace

import flask_login.utils
import pytest

ROOT = Path(__file__).resolve().parents[1]
WELCOME = "Envie seus dados logísticos para criar análises, indicadores e dashboards personalizados."
UNAVAILABLE = "O envio e a análise de arquivos estarão disponíveis em uma próxima etapa."
GUIDANCE = "Para começar, envie seus dados logísticos pelo botão +."
AUTH_MESSAGE = "Autenticação necessária para utilizar o AgenteBI. Faça login para continuar."
LOGIN_HREF = "/login?next=/cleide-bi-frete"
MENU_ORDER = (
    "inicio",
    "consultar_agentefrete",
    "auditar_cobrancas",
    "comparar_tabelas",
    "feed",
)
GUEST_MENU_HREFS = {
    "inicio": 'href="/"',
    "consultar_agentefrete": 'href="/login?next=%2Fchat_julia%3Fmode%3Doperational"',
    "auditar_cobrancas": 'href="/login?next=/auditoria-frete"',
    "comparar_tabelas": 'href="/login?next=/agente-compara"',
    "feed": 'href="/feed"',
}
AUTH_MENU_HREFS = {
    "inicio": 'href="/"',
    "consultar_agentefrete": 'href="/chat_julia?mode=operational"',
    "auditar_cobrancas": 'href="/auditoria-frete"',
    "comparar_tabelas": 'href="/agente-compara"',
    "feed": 'href="/feed"',
}
LEGACY_ASSETS = (
    "cleide_auditoria_frete.css",
    "cleide_auditoria_frete.js",
    "cleide_auditoria.js",
    "chart.umd",
    "chart.js",
    "/api/cleide/",
    "/api/chat_cleide",
    "cleideUploadForm",
    "cleideChatFloating",
)


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _user(*, authenticated):
    return SimpleNamespace(
        is_authenticated=authenticated,
        is_active=True,
        is_anonymous=not authenticated,
        get_id=lambda: "1",
        conta_id=1,
        franquia_id=1,
        categoria="pro",
        full_name="Teste AgenteBI",
        email="agentebi@example.com",
        franquia=None,
    )


def _client(monkeypatch, *, authenticated, permitido=True):
    monkeypatch.delenv("AGENTE_BI_API_ENABLED", raising=False)
    monkeypatch.delenv("AGENTE_BI_INGESTAO_ENABLED", raising=False)
    web = _load_web_module()
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: _user(authenticated=authenticated))
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {
            "permitido": permitido,
            "modo_operacao": "normal" if permitido else "blocked",
            "mensagem_usuario": "Sua franquia atingiu o limite operacional.",
        },
    )
    return web


def _menu(html):
    start = html.index('id="agente-bi-actions-menu"')
    end = html.index('id="agente-bi-unavailable"', start)
    return html[start:end]


def _assert_menu_hrefs(menu, expected):
    for action_id, href in expected.items():
        pattern = rf'{re.escape(href)}[^>]*data-agente-bi-action="{action_id}"'
        assert re.search(pattern, menu, re.S), action_id


def test_rota_original_entrega_agente_bi_sem_login_obrigatorio():
    from app.cleide_routes import auditoria_frete

    source = inspect.getsource(auditoria_frete)
    assert '@cleide_bp.route("/cleide-bi-frete", methods=["GET"])' in source
    assert "agente_bi.html" in source
    assert "login_required" not in source
    assert "get_cleide_config" not in source
    assert "get_cleide_upload_tmp_dir" not in source
    assert "process_cleide_upload" not in source
    assert "run_cleide_controlled_chat" not in source
    assert "cleide_auditoria_frete.html" not in source

    web = _load_web_module()
    matches = [rule for rule in web.app.url_map.iter_rules() if rule.endpoint == "cleide.auditoria_frete"]
    assert len(matches) == 1
    assert matches[0].rule == "/cleide-bi-frete"
    assert "GET" in matches[0].methods
    assert "POST" not in matches[0].methods
    assert "cleide.cleide_auditoria" in web.app.view_functions
    assert "/api/cleide/upload" in {rule.rule for rule in web.app.url_map.iter_rules()}
    assert "/api/chat_cleide" in {rule.rule for rule in web.app.url_map.iter_rules()}


def test_get_nao_inicializa_processamento_legado(monkeypatch):
    def _forbidden(*_args, **_kwargs):
        raise AssertionError("processamento legado nao deve rodar no GET")

    monkeypatch.setattr("app.cleide_routes.get_cleide_config", _forbidden)
    monkeypatch.setattr("app.cleide_routes.get_cleide_upload_tmp_dir", _forbidden)
    monkeypatch.setattr("app.cleide_routes.process_cleide_upload", _forbidden)
    monkeypatch.setattr("app.cleide_routes.run_cleide_controlled_chat", _forbidden)
    web = _client(monkeypatch, authenticated=False)

    resp = web.app.test_client().get("/cleide-bi-frete")
    assert resp.status_code == 200


def test_visitante_recebe_200_com_identidade_e_chat_bloqueado(monkeypatch):
    calls = {"authz": 0}

    def _authz(_u):
        calls["authz"] += 1
        return {"permitido": True}

    web = _client(monkeypatch, authenticated=False)
    monkeypatch.setattr("app.cleide_routes.avaliar_autorizacao_operacao_por_franquia", _authz)
    resp = web.app.test_client().get("/cleide-bi-frete")
    assert resp.status_code == 200
    assert calls["authz"] == 0
    html = resp.get_data(as_text=True)
    _assert_page_foundation(html, authenticated=False)
    with web.app.test_request_context():
        from flask import url_for

        assert url_for("login", next=url_for("cleide.auditoria_frete")) == LOGIN_HREF
    menu = _menu(html)
    assert [item.group(1) for item in re.finditer(r'data-agente-bi-action="([^"]+)"', menu)] == list(MENU_ORDER)
    _assert_menu_hrefs(menu, GUEST_MENU_HREFS)
    assert 'href="/fretes"' not in menu
    assert "Previsibilidade" not in menu
    assert "BI Gerencial" not in menu
    assert 'href="/auditoria-frete"' not in menu
    assert 'href="/agente-compara"' not in menu


def test_usuario_autorizado_recebe_200_e_links_diretos(monkeypatch):
    web = _client(monkeypatch, authenticated=True, permitido=True)
    client = web.app.test_client()
    resp = client.get("/cleide-bi-frete")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    _assert_page_foundation(html, authenticated=True)
    assert 'id="agente-bi-descoberta"' not in html
    assert 'id="agente-bi-dashboard"' not in html
    assert "agente_bi_descoberta.js" not in html
    assert "agente_bi_dashboard.js" not in html
    assert "chart.js" not in html
    for caminho, metodo in (
        ("/api/agente-bi/ingestao", "post"),
        ("/api/agente-bi/descoberta", "post"),
        ("/api/agente-bi/dashboards", "get"),
    ):
        bloqueio = getattr(client, metodo)(caminho)
        assert bloqueio.status_code == 404
        assert bloqueio.get_json()["codigo"] == "nao_encontrado"
    menu = _menu(html)
    _assert_menu_hrefs(menu, AUTH_MENU_HREFS)
    assert 'href="/login?next=' not in menu
    assert 'href="/fretes"' not in menu
    assert "Previsibilidade" not in menu


def test_usuario_bloqueado_continua_403(monkeypatch):
    web = _client(monkeypatch, authenticated=True, permitido=False)
    resp = web.app.test_client().get("/cleide-bi-frete")
    assert resp.status_code == 403
    html = resp.get_data(as_text=True)
    assert "Sua franquia atingiu o limite operacional." in html
    assert 'id="agente-bi-shell"' not in html


def test_navegacao_da_pagina_repete_agente_bi_no_desktop_e_no_mobile(monkeypatch):
    web = _client(monkeypatch, authenticated=False)
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    desktop = html[html.index('data-shell-nav="desktop"'):html.index('id="content"')]
    mobile = html[html.index('data-shell-nav="mobile"'):html.index('data-shell-nav="desktop"')]
    assert "AgenteBI" in desktop
    assert "AgenteBI" in mobile
    assert 'href="/cleide-bi-frete"' in desktop
    assert 'href="/cleide-bi-frete"' in mobile
    assert 'aria-label="Expandir menu lateral"' in html
    assert 'aria-label="Abrir menu de navegação"' in html


def test_pagina_nao_carrega_assets_operacionais_do_bi_antigo(monkeypatch):
    web = _client(monkeypatch, authenticated=False)
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    for token in LEGACY_ASSETS:
        assert token not in html
    assert 'type="file"' not in html
    assert "<canvas" not in html
    assert html.count("agente_bi.css") == 1
    assert html.count("agente_bi.js") == 1
    for relative in (
        "app/templates/cleide_auditoria_frete.html",
        "app/static/css/cleide_auditoria_frete.css",
        "app/static/js/cleide_auditoria_frete.js",
    ):
        assert (ROOT / relative).is_file()


def test_js_do_menu_nao_dispara_chamada_operacional():
    source = (ROOT / "app" / "static" / "js" / "agente_bi.js").read_text(encoding="utf-8").lower()
    for token in (
        "fetch(",
        "xmlhttprequest",
        "sendbeacon",
        "websocket",
        "eventsource",
        "/api/",
        "showopenfilepicker",
        'type="file"',
        "new file(",
    ):
        assert token not in source
    assert "data-authenticated" in source
    assert AUTH_MESSAGE.lower() in source
    assert "escape" in source
    assert "returnfocus" in source or "returnfocus" in source.replace(" ", "")
    assert "trigger.focus" in source
    assert "preventdefault" in source
    assert "disabled = true" in source
    assert "input.disabled = true" not in source
    assert "input.readonly = true" in source
    assert GUIDANCE.lower() in source
    assert "guidanceannounced" in source
    assert "beforeinput" in source
    assert "paste" in source
    assert '"drop"' in source
    assert "form.submit = function" in source
    assert 'event.key !== "enter"' in source
    assert "aria-readonly" in source


def test_css_usa_tokens_oficiais_e_cobre_os_breakpoints():
    css = (ROOT / "app" / "static" / "css" / "agente_bi.css").read_text(encoding="utf-8")
    template = (ROOT / "app" / "templates" / "agente_bi.html").read_text(encoding="utf-8")
    for token in (
        "var(--af-primary)",
        "var(--af-surface)",
        "var(--af-text)",
        "var(--af-surface-muted)",
        "max-width: 880px",
        "min-height: 44px",
        "@media (max-width: 767.98px)",
        "@media (min-width: 768px) and (max-width: 1439.98px)",
        "@media (min-width: 1440px)",
    ):
        assert token in css
    assert "#" not in css
    assert "data-af-theme-select" not in template
    assert "agentefrete-theme.css" not in template


def test_tema_global_permanece_na_pagina(monkeypatch):
    web = _client(monkeypatch, authenticated=False)
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert "agentefrete-theme.css" in html
    assert "af-theme-init" in html
    assert 'data-theme="dark"' in html
    assert "data-af-theme-select" in html
    assert 'data-theme-preference="dark"' in html


def _textarea_tag(html):
    match = re.search(r'<textarea\b[^>]*\bid="agente-bi-input"[^>]*>', html, re.S)
    assert match, "textarea do compositor ausente"
    return match.group(0)


def _assert_page_foundation(html, *, authenticated):
    assert html.count('<span class="af-text-gradient">AgenteFrete</span>') == 1
    assert 'id="agente-bi-name">AgenteBI</h2>' in html
    assert "Assistente de inteligência de dados logísticos." in html
    assert "O AgenteBI utiliza inteligência artificial e pode cometer erros." in html
    textarea = _textarea_tag(html)
    assert "readonly" in textarea
    assert "disabled" not in textarea
    assert 'aria-readonly="true"' in textarea
    assert 'aria-label="Mensagem para o AgenteBI"' in textarea
    assert 'aria-describedby="agente-bi-composer-guidance"' in textarea
    if authenticated:
        assert 'data-authenticated="true"' in html
        assert WELCOME in html
        assert UNAVAILABLE in html
        assert "Envie uma planilha XLSX ou um CSV pelo botão +. O chat continua bloqueado." in html
        assert f'placeholder="{GUIDANCE}"' in textarea
        assert 'id="agente-bi-login"' not in html
        assert AUTH_MESSAGE not in html
    else:
        assert 'data-authenticated="false"' in html
        assert AUTH_MESSAGE in html
        assert ">Fazer login</a>" in html
        assert f'href="{LOGIN_HREF}"' in html
        assert f'placeholder="{AUTH_MESSAGE}"' in textarea
        assert WELCOME not in html
        assert UNAVAILABLE not in html
        assert GUIDANCE not in html
        assert "Envie uma planilha" not in html
        link = re.search(r'<a\b[^>]*\bid="agente-bi-login"[^>]*>', html, re.S)
        assert link
        assert "onclick" not in link.group(0)
    assert (
        '<p class="agente-bi-composer-guidance" id="agente-bi-composer-guidance" '
        'role="status" aria-live="polite" aria-atomic="true"></p>'
    ) in html
    assert re.search(r'<button\b[^>]*\bid="agente-bi-send"[^>]*\bdisabled\b', html, re.S)
    assert re.search(r'<button\b[^>]*\bid="agente-bi-upload-item"[^>]*\bdisabled\b', html, re.S)
    assert 'onsubmit="return false;"' in html
    assert 'id="agente-bi-actions-trigger"' in html
    assert 'aria-haspopup="menu"' in html
    assert "Enviar arquivos" in html
    assert ">Home</span>" in html
    assert ">Consultar o AgenteFrete</span>" in html
    assert ">Auditar cobranças de frete</span>" in html
    assert ">Comparar tabelas</span>" in html
    assert ">Feed</span>" in html
    assert 'type="file"' not in html


def _browser_document(html):
    css = (ROOT / "app" / "static" / "css" / "agente_bi.css").read_text(encoding="utf-8")
    theme = (ROOT / "app" / "static" / "css" / "agentefrete-theme.css").read_text(encoding="utf-8")
    script = (ROOT / "app" / "static" / "js" / "agente_bi.js").read_text(encoding="utf-8")
    html = re.sub(r"<link\b[^>]*>", "", html, flags=re.I)
    html = re.sub(r"<script\b[^>]*\bsrc\s*=[^>]*>\s*</script>", "", html, flags=re.I)

    def _keep_script(match):
        body = match.group(0)
        if any(token in body for token in ("fetch(", "XMLHttpRequest", "sendBeacon", "$(document)")):
            return ""
        return body

    html = re.sub(r"<script\b[^>]*>.*?</script>", _keep_script, html, flags=re.I | re.S)
    html = html.replace("</head>", f"<style>{theme}\n{css}</style></head>", 1)
    html = html.replace(
        "<head>",
        """<head><script>
        window.__agenteBiNetwork = [];
        (function () {
          function record(kind, value) {
            window.__agenteBiNetwork.push(kind + ":" + String(value || ""));
          }
          var origFetch = window.fetch;
          window.fetch = function (input) {
            record("fetch", typeof input === "string" ? input : (input && input.url));
            if (origFetch) return origFetch.apply(this, arguments);
            return Promise.reject(new Error("sem rede"));
          };
          var OrigXHR = window.XMLHttpRequest;
          window.XMLHttpRequest = function () {
            record("xhr", "");
            return new OrigXHR();
          };
          var beacon = navigator.sendBeacon && navigator.sendBeacon.bind(navigator);
          navigator.sendBeacon = function (url) {
            record("beacon", url);
            return beacon ? beacon.apply(navigator, arguments) : false;
          };
        })();
        </script>""",
        1,
    )
    html = html.replace("</body>", f"<script>{script}</script></body>", 1)
    return html


def _launch_browser(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except Exception:
        last_error = None
        for channel in ("chrome", "msedge"):
            try:
                return playwright.chromium.launch(channel=channel, headless=True)
            except Exception as exc:
                last_error = exc
        raise last_error


def test_compositor_bloqueado_orienta_sem_enviar_e_preserva_o_menu(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    web = _client(monkeypatch, authenticated=False)
    rendered = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    document = _browser_document(rendered)
    network = []
    page_errors = []

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("request", lambda request: network.append(request.url))
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.set_content(document, wait_until="domcontentloaded")

        assert page.locator("#agente-bi-shell").get_attribute("data-authenticated") == "false"
        assert page.locator("#agente-bi-input").get_attribute("placeholder") == AUTH_MESSAGE
        assert page.locator("#agente-bi-welcome").inner_text() == AUTH_MESSAGE
        assert page.locator("#agente-bi-login").inner_text() == "Fazer login"
        assert page.locator("#agente-bi-login").get_attribute("href") == LOGIN_HREF
        assert "Envie uma planilha" not in page.locator("#agente-bi-shell").inner_text()
        assert GUIDANCE not in page.locator("#agente-bi-shell").inner_text()
        assert page.locator("#agente-bi-descoberta").count() == 0
        assert page.locator("#agente-bi-dashboard").count() == 0
        assert page.locator("#agente-bi-composer-guidance").inner_text() == ""
        assert page.locator("#agente-bi-input").get_attribute("aria-readonly") == "true"
        assert page.locator("#agente-bi-input").get_attribute("disabled") is None
        assert page.evaluate("() => document.getElementById('agente-bi-input').readOnly") is True
        assert page.locator("#agente-bi-send").is_disabled()
        assert page.locator("#agente-bi-upload-item").is_disabled()
        assert page.locator('input[type="file"]').count() == 0

        page.evaluate(
            """() => {
              const el = document.getElementById('agente-bi-composer-guidance');
              const desc = Object.getOwnPropertyDescriptor(Node.prototype, 'textContent');
              let writes = 0;
              Object.defineProperty(el, 'textContent', {
                configurable: true,
                get() { return desc.get.call(this); },
                set(value) { writes += 1; desc.set.call(this, value); }
              });
              window.__guidanceWrites = () => writes;
            }"""
        )
        baseline_network = page.evaluate("() => window.__agenteBiNetwork.slice()")
        baseline_requests = len(network)

        page.locator("#agente-bi-actions-trigger").focus()
        page.keyboard.press("Tab")
        assert page.evaluate("() => document.activeElement.id") == "agente-bi-input"
        assert page.locator("#agente-bi-composer-guidance").inner_text() == AUTH_MESSAGE
        assert page.locator("#agente-bi-login").get_attribute("href") == LOGIN_HREF
        assert page.locator("#agente-bi-composer-guidance").get_attribute("role") == "status"
        assert page.locator("#agente-bi-composer-guidance").get_attribute("aria-live") == "polite"
        assert page.locator("#agente-bi-composer-guidance").get_attribute("aria-atomic") == "true"
        assert page.evaluate("() => window.__guidanceWrites()") == 1

        page.keyboard.press("Shift+Tab")
        assert page.evaluate("() => document.activeElement.id") == "agente-bi-actions-trigger"
        page.keyboard.press("Tab")
        page.keyboard.type("mensagem que nao pode sair")
        page.keyboard.press("Enter")
        assert page.locator("#agente-bi-input").input_value() == ""
        assert page.locator("#agente-bi-composer-guidance").inner_text() == AUTH_MESSAGE
        assert page.locator("#agente-bi-login").get_attribute("href") == LOGIN_HREF
        assert page.evaluate("() => window.__guidanceWrites()") == 1

        blocked = page.evaluate(
            """() => {
              const input = document.getElementById('agente-bi-input');
              const form = document.getElementById('agente-bi-form');
              const pasteData = new DataTransfer();
              pasteData.setData('text/plain', 'mensagem colada');
              const paste = new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: pasteData });
              const pasteContinued = input.dispatchEvent(paste);
              input.value = 'mensagem colada';
              input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromPaste', data: 'mensagem colada' }));
              const dropData = new DataTransfer();
              dropData.setData('text/plain', 'mensagem solta');
              dropData.items.add(new File(['a,b'], 'fretes.csv', { type: 'text/csv' }));
              const drop = new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dropData });
              const dropContinued = input.dispatchEvent(drop);
              input.value = 'mensagem solta';
              input.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromDrop', data: 'mensagem solta' }));
              const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
              const enterContinued = input.dispatchEvent(enter);
              form.setAttribute('action', '/api/chat_cleide');
              form.setAttribute('method', 'post');
              const submit = new Event('submit', { bubbles: true, cancelable: true });
              const submitContinued = form.dispatchEvent(submit);
              form.submit();
              HTMLFormElement.prototype.submit.call(form);
              return {
                pasteContinued,
                pastePrevented: paste.defaultPrevented,
                dropContinued,
                dropPrevented: drop.defaultPrevented,
                enterContinued,
                enterPrevented: enter.defaultPrevented,
                submitContinued,
                submitPrevented: submit.defaultPrevented,
                action: form.getAttribute('action'),
                value: input.value,
                shell: !!document.getElementById('agente-bi-shell'),
                fileInputs: document.querySelectorAll('input[type="file"]').length
              };
            }"""
        )
        assert blocked["pasteContinued"] is False
        assert blocked["pastePrevented"] is True
        assert blocked["dropContinued"] is False
        assert blocked["dropPrevented"] is True
        assert blocked["enterContinued"] is False
        assert blocked["enterPrevented"] is True
        assert blocked["submitContinued"] is False
        assert blocked["submitPrevented"] is True
        assert blocked["action"] is None
        assert blocked["value"] == ""
        assert blocked["shell"] is True
        assert blocked["fileInputs"] == 0
        assert page.evaluate("() => window.__guidanceWrites()") == 1
        assert page.locator("#agente-bi-input").input_value() == ""

        restored = page.evaluate(
            """async () => {
              const input = document.getElementById('agente-bi-input');
              const send = document.getElementById('agente-bi-send');
              input.readOnly = false;
              input.disabled = true;
              input.setAttribute('name', 'mensagem');
              input.value = 'bypass';
              input.dispatchEvent(new Event('input', { bubbles: true }));
              send.disabled = false;
              send.type = 'submit';
              await new Promise((resolve) => setTimeout(resolve, 0));
              return {
                value: input.value,
                readOnly: input.readOnly,
                disabled: input.disabled,
                name: input.getAttribute('name'),
                sendDisabled: send.disabled,
                sendType: send.type
              };
            }"""
        )
        assert restored["value"] == ""
        assert restored["readOnly"] is True
        assert restored["disabled"] is False
        assert restored["name"] is None
        assert restored["sendDisabled"] is True
        assert restored["sendType"] == "button"

        page.locator("#agente-bi-actions-trigger").click()
        assert page.locator("#agente-bi-actions-menu").is_visible()
        assert page.evaluate("() => document.activeElement.getAttribute('data-agente-bi-action')") == "inicio"
        page.keyboard.press("ArrowDown")
        assert page.evaluate("() => document.activeElement.getAttribute('data-agente-bi-action')") == "consultar_agentefrete"
        page.keyboard.press("Escape")
        assert page.locator("#agente-bi-actions-menu").is_hidden()
        assert page.evaluate("() => document.activeElement.id") == "agente-bi-actions-trigger"
        assert page.locator('[data-agente-bi-action="inicio"]').get_attribute("href") == "/"
        assert page.locator('[data-agente-bi-action="feed"]').get_attribute("href") == "/feed"
        assert "/login?next=" in page.locator('[data-agente-bi-action="auditar_cobrancas"]').get_attribute("href")
        assert page.locator("#agente-bi-upload-item").is_disabled()

        page.set_viewport_size({"width": 390, "height": 844})
        guidance_box = page.locator("#agente-bi-composer-guidance").bounding_box()
        fit = page.evaluate(
            """() => {
              const el = document.getElementById('agente-bi-composer-guidance');
              const rect = el.getBoundingClientRect();
              const parent = el.parentElement.getBoundingClientRect();
              return {
                fitsItself: el.scrollWidth <= el.clientWidth + 1,
                withinParent: rect.right <= parent.right + 1
              };
            }"""
        )
        assert guidance_box is not None
        assert guidance_box["width"] > 40
        assert guidance_box["x"] >= 0
        assert fit["fitsItself"] is True
        assert fit["withinParent"] is True
        assert page.locator("#agente-bi-composer-guidance").inner_text() == AUTH_MESSAGE
        assert page.locator("#agente-bi-login").get_attribute("href") == LOGIN_HREF
        input_box = page.locator("#agente-bi-input").bounding_box()
        assert input_box is not None and input_box["height"] >= 44

        calls = page.evaluate("() => window.__agenteBiNetwork.slice()")
        assert calls == baseline_network
        assert network[baseline_requests:] == []
        assert not any("/api/" in url or "chat_cleide" in url or "upload" in url for url in network)
        relevant_errors = [error for error in page_errors if "$ is not defined" not in error]
        assert relevant_errors == []
        browser.close()


def test_cta_de_login_funciona_sem_javascript(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    web = _client(monkeypatch, authenticated=False)
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    sem_script = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.I | re.S)
    assert AUTH_MESSAGE in sem_script
    assert f'href="{LOGIN_HREF}"' in sem_script
    assert ">Fazer login</a>" in sem_script
    assert "<script" not in sem_script.lower()
    documento = sem_script.replace("<head>", '<head><base href="http://agente-bi.test/">', 1)

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        context = browser.new_context(java_script_enabled=False, viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.route(
            "**/*",
            lambda route: route.fulfill(status=200, content_type="text/html", body="<p>login</p>"),
        )
        page.set_content(documento, wait_until="domcontentloaded")
        assert page.locator("#agente-bi-login").inner_text() == "Fazer login"
        with page.expect_navigation():
            page.locator("#agente-bi-login").click()
        assert page.url == "http://agente-bi.test/login?next=/cleide-bi-frete"
        browser.close()


def test_visitante_nao_executa_upload_descoberta_nem_dashboard(monkeypatch):
    web = _client(monkeypatch, authenticated=False)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_INGESTAO_ENABLED", "1")
    client = web.app.test_client()
    html = client.get("/cleide-bi-frete").get_data(as_text=True)
    assert 'data-authenticated="false"' in html
    assert AUTH_MESSAGE in html
    assert f'href="{LOGIN_HREF}"' in html
    assert 'id="agente-bi-descoberta"' not in html
    assert 'id="agente-bi-dashboard"' not in html
    assert "agente_bi_descoberta.js" not in html
    assert "agente_bi_dashboard.js" not in html
    assert "agente_bi_reinicio.js" not in html
    assert "chart.js" not in html
    assert 'type="file"' not in html
    assert "Envie uma planilha" not in html
    assert re.search(r'<button\b[^>]*\bid="agente-bi-upload-item"[^>]*\bdisabled\b', html, re.S)
    for caminho, metodo in (
        ("/api/agente-bi/ingestao", "post"),
        ("/api/agente-bi/descoberta", "post"),
        ("/api/agente-bi/descoberta/preparacao", "post"),
        ("/api/agente-bi/descoberta/selecao", "post"),
        ("/api/agente-bi/dashboards", "get"),
        ("/api/agente-bi/dashboards/montar", "post"),
    ):
        recusa = getattr(client, metodo)(caminho)
        assert recusa.status_code == 401
        assert recusa.get_json()["codigo"] == "autenticacao_necessaria"


def test_usuario_autenticado_mantem_orientacao_de_upload_no_compositor(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    web = _client(monkeypatch, authenticated=True, permitido=True)
    rendered = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert 'data-authenticated="true"' in rendered
    assert GUIDANCE in rendered
    assert UNAVAILABLE in rendered
    assert 'id="agente-bi-login"' not in rendered
    document = _browser_document(rendered)

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(document, wait_until="domcontentloaded")
        assert page.locator("#agente-bi-input").get_attribute("placeholder") == GUIDANCE
        page.locator("#agente-bi-input").focus()
        assert page.locator("#agente-bi-composer-guidance").inner_text() == GUIDANCE
        assert page.locator("#agente-bi-login").count() == 0
        assert page.locator('input[type="file"]').count() == 0
        browser.close()
