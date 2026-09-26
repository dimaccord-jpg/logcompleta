"""SCRUM-216 — limpar checkout Starter/Pro ao selecionar Multiusuário."""
from __future__ import annotations

import json
import pathlib
import re

import pytest

TEMPLATE = pathlib.Path("app/templates/contrate_plano.html")


def _src() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def test_scrum170_empty_checkout_css_preserved():
    src = _src()
    assert "#checkout-embed:empty" in src
    block = src[src.index("#checkout-embed:empty") : src.index("#checkout-embed:empty") + 180]
    assert "min-height: 0 !important" in block
    assert "border: 0 !important" in block
    assert "padding: 0 !important" in block


def test_multiuser_selection_invalidates_and_clears_checkout():
    src = _src()
    assert "function limparCheckoutEmbedded()" in src
    assert "function invalidarCheckoutPendente()" in src
    assert "let checkoutGeneration = 0" in src
    multiuser_branch = src[
        src.index('if (planoNormalizado === "multiuser")') :
        src.index("if (formMultiuser) {\n                formMultiuser.classList.add")
    ]
    assert "invalidarCheckoutPendente()" in multiuser_branch
    assert "iniciarCheckout(" not in multiuser_branch


def test_iniciar_checkout_guards_stale_generation_before_mount():
    src = _src()
    fn = src[src.index("async function iniciarCheckout") : src.index("function coletarDadosMultiuser")]
    assert "const generation = ++checkoutGeneration" in fn
    assert "if (generation !== checkoutGeneration)" in fn
    assert "checkoutInstance.destroy" in fn
    assert "limparCheckoutEmbedded()" in fn
    assert 'embeddedCheckout.mount("#checkout-embed")' in fn


def _build_test_html() -> str:
    src = _src()
    style = src[src.index("<style>") : src.index("</style>") + len("</style>")]
    form = src[src.index('<form id="form-multiuser"') : src.index("</form>") + len("</form>")]
    script_start = src.index("<script>\n(() => {")
    script_end = src.index("</script>", script_start) + len("</script>")
    script = src[script_start:script_end]
    form = re.sub(r"\{\{[^}]+\}\}", "", form)
    script = script.replace(
        "{{ url_for('user.resumo_contratacao_multiuser') }}",
        "/api/resumo-multiuser",
    )
    script = script.replace(
        "{{ url_for('user.iniciar_contratacao_stripe') }}",
        "/api/contratacao/stripe/iniciar",
    )
    script = script.replace(
        "{{ url_for('growth.growth_plan_selected') }}",
        "/api/growth/plan-selected",
    )
    script = script.replace(
        "{{ url_for('growth.growth_checkout_started') }}",
        "/api/growth/checkout-started",
    )
    script = re.sub(r"\{\{[^}]+\}\}", "", script)
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<base href="http://127.0.0.1/">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
{style}
</head>
<body>
<div class="container py-4">
<div id="plano-contexto" data-plano-atual="free" data-vencimento-atual=""></div>
<button type="button" class="js-iniciar-checkout" data-plano-codigo="starter" data-plano-nome="Starter">Contratar Starter</button>
<button type="button" class="js-iniciar-checkout" data-plano-codigo="pro" data-plano-nome="Pro">Contratar Pro</button>
<button type="button" class="js-iniciar-checkout" data-plano-codigo="multiuser" data-plano-nome="Multiusuário" data-quantidade-minima="5" data-valor-unitario="49.90">Contratar Multiusuário</button>
<div id="checkout-status" class="alert alert-light border small mb-3"></div>
{form}
<div id="checkout-embed" class="border rounded p-2" style="min-height: 420px;"></div>
</div>
<script>
window.__stripeMock = {{ mounts: [], destroys: [], inits: [], live: 0 }};
window.Stripe = function (pk) {{
  return {{
    initEmbeddedCheckout: async function (opts) {{
      const secret = opts && opts.clientSecret;
      window.__stripeMock.inits.push({{ pk: pk, clientSecret: secret }});
      const marker = document.createElement("div");
      marker.className = "fake-stripe-checkout";
      marker.setAttribute("data-client-secret", secret || "");
      marker.textContent = "checkout:" + (secret || "");
      const instance = {{
        _destroyed: false,
        mount: function (selector) {{
          const el = document.querySelector(selector);
          if (!el) {{ throw new Error("missing mount target"); }}
          el.appendChild(marker);
          window.__stripeMock.mounts.push(secret);
          window.__stripeMock.live += 1;
        }},
        destroy: function () {{
          if (instance._destroyed) {{ return; }}
          instance._destroyed = true;
          if (marker.parentNode) {{ marker.parentNode.removeChild(marker); }}
          window.__stripeMock.destroys.push(secret);
          window.__stripeMock.live = Math.max(0, window.__stripeMock.live - 1);
        }},
      }};
      return instance;
    }},
  }};
}};
</script>
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


class CheckoutApiMock:
    def __init__(self, page):
        self.page = page
        self.requests = []
        self._hold = {}
        self._pending = []
        page.route("**/api/resumo-multiuser", self._resumo)
        page.route("**/api/contratacao/stripe/iniciar", self._iniciar)

    def _resumo(self, route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "plano": "multiuser",
                    "acessos": 5,
                    "valor_por_acesso": "49.90",
                    "total_mensal": "249.50",
                }
            ),
        )

    def _iniciar(self, route):
        body = route.request.post_data or "{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        plano = (payload.get("plano_codigo") or "").lower()
        self.requests.append(payload)
        if plano in self._hold:
            self._pending.append((plano, route, payload))
            return
        self._fulfill(route, payload)

    def _fulfill(self, route, payload):
        plano = (payload.get("plano_codigo") or "unknown").lower()
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "ok": True,
                    "publishable_key": f"pk_test_{plano}",
                    "checkout_client_secret": f"cs_test_{plano}_{len(self.requests)}",
                }
            ),
        )

    def hold(self, plano):
        self._hold[plano.lower()] = True

    def release(self, plano):
        plano = plano.lower()
        self._hold.pop(plano, None)
        remaining = []
        for held_plano, route, payload in self._pending:
            if held_plano == plano:
                try:
                    self._fulfill(route, payload)
                except Exception:
                    pass
            else:
                remaining.append((held_plano, route, payload))
        self._pending = remaining

    def pending_count(self, plano):
        plano = plano.lower()
        return sum(1 for held, _, _ in self._pending if held == plano)


def _open(page):
    page.set_content(_build_test_html(), wait_until="domcontentloaded")


def _state(page):
    return page.evaluate(
        """() => {
            const embed = document.getElementById('checkout-embed');
            const form = document.getElementById('form-multiuser');
            return {
                formVisible: form ? !form.classList.contains('d-none') : false,
                embedHTML: embed ? embed.innerHTML : null,
                embedEmpty: embed ? embed.childNodes.length === 0 : null,
                fakeCount: document.querySelectorAll('.fake-stripe-checkout').length,
                secrets: Array.from(document.querySelectorAll('.fake-stripe-checkout')).map(
                    (el) => el.getAttribute('data-client-secret')
                ),
                mock: window.__stripeMock,
                status: (document.getElementById('checkout-status') || {}).textContent || '',
            };
        }"""
    )


def _click_plano(page, codigo):
    page.click(f'button.js-iniciar-checkout[data-plano-codigo="{codigo}"]')


def _wait_mounted(page, *, secret_prefix=None, timeout=8000):
    page.wait_for_function(
        """(prefix) => {
            const nodes = document.querySelectorAll('.fake-stripe-checkout');
            if (!nodes.length) return false;
            if (!prefix) return true;
            return Array.from(nodes).some((n) => (n.getAttribute('data-client-secret') || '').startsWith(prefix));
        }""",
        arg=secret_prefix,
        timeout=timeout,
    )


def _fill_multiuser_form(page):
    page.fill("#mu-razao-social", "Empresa Teste LTDA")
    page.fill("#mu-nome-fantasia", "Empresa")
    page.fill("#mu-cnpj", "11222333000181")
    page.fill("#mu-email", "empresa@test.com")
    page.fill("#mu-logradouro", "Rua A")
    page.fill("#mu-numero", "100")
    page.fill("#mu-cidade", "São Paulo")
    page.fill("#mu-uf", "SP")
    page.fill("#mu-cep", "01310100")
    page.fill("#mu-quantity", "5")


def test_starter_to_multiuser_clears_previous_checkout(page):
    api = CheckoutApiMock(page)
    _open(page)
    _click_plano(page, "starter")
    _wait_mounted(page, secret_prefix="cs_test_starter")
    before = _state(page)
    assert before["fakeCount"] == 1
    assert before["mock"]["live"] == 1

    _click_plano(page, "multiuser")
    page.wait_for_timeout(100)
    after = _state(page)
    assert after["formVisible"] is True
    assert after["fakeCount"] == 0
    assert after["embedEmpty"] is True
    assert after["mock"]["live"] == 0
    assert after["mock"]["destroys"]
    assert "Preencha os dados empresariais" in after["status"]
    assert len(api.requests) == 1


def test_pro_to_multiuser_clears_previous_checkout(page):
    CheckoutApiMock(page)
    _open(page)
    _click_plano(page, "pro")
    _wait_mounted(page, secret_prefix="cs_test_pro")
    _click_plano(page, "multiuser")
    page.wait_for_timeout(100)
    after = _state(page)
    assert after["formVisible"] is True
    assert after["fakeCount"] == 0
    assert after["embedEmpty"] is True
    assert after["mock"]["live"] == 0


def test_multiuser_selected_keeps_checkout_closed(page):
    api = CheckoutApiMock(page)
    _open(page)
    _click_plano(page, "multiuser")
    page.wait_for_timeout(100)
    state = _state(page)
    assert state["formVisible"] is True
    assert state["fakeCount"] == 0
    assert state["embedEmpty"] is True
    assert api.requests == []
    assert state["mock"]["inits"] == []


def test_ir_para_checkout_opens_only_multiuser(page):
    api = CheckoutApiMock(page)
    _open(page)
    _click_plano(page, "multiuser")
    _fill_multiuser_form(page)
    page.click("#btn-confirmar-multiuser")
    _wait_mounted(page, secret_prefix="cs_test_multiuser")
    state = _state(page)
    assert state["fakeCount"] == 1
    assert state["mock"]["live"] == 1
    assert state["secrets"] == [state["mock"]["mounts"][-1]]
    assert state["secrets"][0].startswith("cs_test_multiuser")
    assert len(api.requests) == 1
    assert api.requests[0]["plano_codigo"] == "multiuser"


def test_stale_starter_load_does_not_remount_after_multiuser(page):
    api = CheckoutApiMock(page)
    api.hold("starter")
    _open(page)
    _click_plano(page, "starter")
    page.wait_for_timeout(50)
    assert api.pending_count("starter") == 1

    _click_plano(page, "multiuser")
    page.wait_for_timeout(50)
    mid = _state(page)
    assert mid["formVisible"] is True
    assert mid["fakeCount"] == 0

    api.release("starter")
    page.wait_for_timeout(200)
    after = _state(page)
    assert after["formVisible"] is True
    assert after["fakeCount"] == 0
    assert after["embedEmpty"] is True
    assert after["mock"]["live"] == 0
    assert all(not s.startswith("cs_test_starter") for s in after["secrets"])


def test_stale_pro_load_does_not_remount_after_multiuser(page):
    api = CheckoutApiMock(page)
    api.hold("pro")
    _open(page)
    _click_plano(page, "pro")
    page.wait_for_timeout(50)
    assert api.pending_count("pro") == 1
    _click_plano(page, "multiuser")
    api.release("pro")
    page.wait_for_timeout(200)
    after = _state(page)
    assert after["formVisible"] is True
    assert after["fakeCount"] == 0
    assert after["mock"]["live"] == 0


def test_multiuser_to_starter_mounts_starter(page):
    CheckoutApiMock(page)
    _open(page)
    _click_plano(page, "multiuser")
    _click_plano(page, "starter")
    _wait_mounted(page, secret_prefix="cs_test_starter")
    state = _state(page)
    assert state["formVisible"] is False
    assert state["fakeCount"] == 1
    assert state["secrets"][0].startswith("cs_test_starter")
    assert state["mock"]["live"] == 1


def test_multiuser_to_pro_mounts_pro(page):
    CheckoutApiMock(page)
    _open(page)
    _click_plano(page, "multiuser")
    _click_plano(page, "pro")
    _wait_mounted(page, secret_prefix="cs_test_pro")
    state = _state(page)
    assert state["formVisible"] is False
    assert state["fakeCount"] == 1
    assert state["secrets"][0].startswith("cs_test_pro")
    assert state["mock"]["live"] == 1


def test_no_multiple_simultaneous_checkout_instances(page):
    CheckoutApiMock(page)
    _open(page)
    _click_plano(page, "starter")
    _wait_mounted(page, secret_prefix="cs_test_starter")
    _click_plano(page, "pro")
    _wait_mounted(page, secret_prefix="cs_test_pro")
    state = _state(page)
    assert state["fakeCount"] == 1
    assert state["mock"]["live"] == 1
    assert state["secrets"][0].startswith("cs_test_pro")

    _click_plano(page, "multiuser")
    _fill_multiuser_form(page)
    page.click("#btn-confirmar-multiuser")
    _wait_mounted(page, secret_prefix="cs_test_multiuser")
    page.click("#btn-confirmar-multiuser")
    page.wait_for_timeout(150)
    final = _state(page)
    assert final["fakeCount"] == 1
    assert final["mock"]["live"] == 1
    assert final["secrets"][0].startswith("cs_test_multiuser")


def test_empty_container_collapses_after_cleanup(page):
    CheckoutApiMock(page)
    _open(page)
    _click_plano(page, "starter")
    _wait_mounted(page, secret_prefix="cs_test_starter")
    _click_plano(page, "multiuser")
    page.wait_for_timeout(100)
    metrics = page.evaluate(
        """() => {
            const el = document.getElementById('checkout-embed');
            const cs = window.getComputedStyle(el);
            return {
                empty: el.childNodes.length === 0,
                minHeight: cs.minHeight,
                borderTopWidth: cs.borderTopWidth,
                paddingTop: cs.paddingTop,
                paddingBottom: cs.paddingBottom,
            };
        }"""
    )
    assert metrics["empty"] is True
    assert metrics["minHeight"] in ("0px", "0")
    assert metrics["borderTopWidth"] in ("0px", "0")
    assert metrics["paddingTop"] in ("0px", "0")
    assert metrics["paddingBottom"] in ("0px", "0")