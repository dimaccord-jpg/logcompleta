"""Busca online de CEP na contratação Multiuser (somente frontend)."""
from __future__ import annotations

import json
import pathlib
import re

import pytest

TEMPLATE = pathlib.Path("app/templates/contrate_plano.html")
COLETAR_KEYS = (
    "csrf_token",
    "quantity",
    "razao_social",
    "nome_fantasia",
    "cnpj",
    "email_empresarial",
    "endereco_logradouro",
    "endereco_numero",
    "endereco_complemento",
    "endereco_bairro",
    "endereco_cidade",
    "endereco_uf",
    "endereco_cep",
)
CEP_A = "01310100"
CEP_A_HIFEN = "01310-100"
CEP_B = "20040020"
ENDERECO_A = {
    "logradouro": "Avenida Paulista",
    "bairro": "Bela Vista",
    "localidade": "São Paulo",
    "uf": "SP",
}
ENDERECO_B = {
    "logradouro": "Praça da Bandeira",
    "bairro": "Centro",
    "localidade": "Rio de Janeiro",
    "uf": "RJ",
}


def _src() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _coletar_fn(src: str) -> str:
    start = src.index("function coletarDadosMultiuser")
    end = src.index("const CEP_DEBOUNCE_MS", start)
    return src[start:end]


def _buscar_fn(src: str) -> str:
    start = src.index("async function buscarCep")
    end = src.index("function agendarBuscaCepAutomatica", start)
    return src[start:end]


def test_template_reuses_existing_address_fields_and_names():
    src = _src()
    assert 'id="mu-cep"' in src
    assert 'name="endereco_cep"' in src
    assert 'id="mu-logradouro"' in src
    assert 'name="endereco_logradouro"' in src
    assert 'id="mu-numero"' in src
    assert 'name="endereco_numero"' in src
    assert 'id="mu-complemento"' in src
    assert 'name="endereco_complemento"' in src
    assert 'id="mu-bairro"' in src
    assert 'name="endereco_bairro"' in src
    assert 'id="mu-cidade"' in src
    assert 'name="endereco_cidade"' in src
    assert 'id="mu-uf"' in src
    assert 'name="endereco_uf"' in src


def test_buscar_cep_button_is_type_button():
    src = _src()
    assert 'id="mu-buscar-cep"' in src
    button = re.search(r"<button[^>]*id=\"mu-buscar-cep\"[^>]*>", src)
    assert button is not None
    assert 'type="button"' in button.group(0)
    assert "Buscar CEP" in src


def test_cep_status_uses_local_live_region():
    src = _src()
    assert 'id="mu-cep-status"' in src
    assert 'role="status"' in src
    assert 'aria-live="polite"' in src
    assert "checkout-status" in src
    status_block = src[src.index('id="mu-cep-status"') : src.index('id="mu-cep-status"') + 220]
    assert "checkout-status" not in status_block
    assert "mu-resumo" not in status_block


def test_single_buscar_cep_function_used_by_auto_and_button():
    src = _src()
    assert src.count("async function buscarCep") == 1
    assert "cepEl.addEventListener(\"input\", agendarBuscaCepAutomatica)" in src
    assert "btnBuscarCep.addEventListener(\"click\"" in src
    assert "buscarCep();" in src
    agenda = src[src.index("function agendarBuscaCepAutomatica") : src.index("async function atualizarResumoMultiuser")]
    assert "buscarCep()" in agenda


def test_viacep_online_only_normalized_cep_and_privacy_headers():
    fn = _buscar_fn(_src())
    assert "https://viacep.com.br/ws/${cep}/json/" in fn
    assert 'credentials: "omit"' in fn
    assert 'referrerPolicy: "no-referrer"' in fn
    assert "cnpj" not in fn
    assert "razao_social" not in fn
    assert "email_empresarial" not in fn
    assert "quantity" not in fn
    assert "stripe" not in fn.lower()
    assert "AbortController" in fn
    assert "CEP_TIMEOUT_MS" in _src()
    assert "CEP_TIMEOUT_MS = 8000" in _src()
    assert "CEP_DEBOUNCE_MS = 300" in _src()


def test_mapping_ignores_viacep_complemento_and_keeps_fields_editable():
    fn = _buscar_fn(_src())
    assert 'aplicarCampoCep("logradouro", payload.logradouro, snapshot)' in fn
    assert 'aplicarCampoCep("bairro", payload.bairro, snapshot)' in fn
    assert 'aplicarCampoCep("cidade", payload.localidade, snapshot)' in fn
    assert 'aplicarCampoCep("uf", payload.uf, snapshot)' in fn
    assert "payload.complemento" not in fn
    assert "mu-numero" not in fn
    assert "mu-complemento" not in fn
    form = _src()
    for field_id in ("mu-logradouro", "mu-bairro", "mu-cidade", "mu-uf", "mu-numero", "mu-complemento"):
        block = re.search(rf'<input[^>]*id="{field_id}"[^>]*>', form)
        assert block, field_id
        assert "readonly" not in block.group(0)
        assert "disabled" not in block.group(0)


def test_coletar_dados_multiuser_keeps_same_keys():
    fn = _coletar_fn(_src())
    keys = re.findall(r"^\s{12}([a-z_]+):", fn, flags=re.M)
    assert tuple(keys) == COLETAR_KEYS


def test_no_page_load_autoquery_hook():
    src = _src()
    assert "DOMContentLoaded" in src
    boot = src[src.index('document.addEventListener("DOMContentLoaded"') :]
    assert "buscarCep" not in boot
    assert "agendarBuscaCepAutomatica" not in boot


def _build_test_html(*, prefill=None) -> str:
    src = _src()
    style = src[src.index("<style>") : src.index("</style>") + len("</style>")]
    form = src[src.index('<form id="form-multiuser"') : src.index("</form>") + len("</form>")]
    script_start = src.index("<script>\n(() => {")
    script_end = src.index("</script>", script_start) + len("</script>")
    script = src[script_start:script_end]
    form = form.replace(" d-none", "")
    form = re.sub(r"\{\{[^}]+\}\}", "", form)
    script = script.replace("{{ url_for('user.resumo_contratacao_multiuser') }}", "/api/resumo-multiuser")
    script = script.replace("{{ url_for('user.iniciar_contratacao_stripe') }}", "/api/contratacao/stripe/iniciar")
    script = script.replace("{{ url_for('growth.growth_plan_selected') }}", "/api/growth/plan-selected")
    script = script.replace("{{ url_for('growth.growth_checkout_started') }}", "/api/growth/checkout-started")
    script = re.sub(r"\{\{[^}]+\}\}", "", script)
    prefill = prefill or {}
    for field_id, value in prefill.items():
        form = re.sub(
            rf'(id="{field_id}"[^>]*value=")',
            rf'\g<1>{value}',
            form,
            count=1,
        )
    return f"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
{style}
</head>
<body>
<div class="container py-4">
<div id="plano-contexto" data-plano-atual="free" data-vencimento-atual=""></div>
<div id="checkout-status" class="alert alert-light border small mb-3"></div>
{form}
<div id="checkout-embed"></div>
</div>
{script}
</body>
</html>"""


def _extract_cep_from_url(url: str) -> str:
    match = re.search(r"/ws/([^/]+)/json", url)
    return match.group(1) if match else ""


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


class ViaCepMock:
    def __init__(self, page, responses=None, default=None):
        self.page = page
        self.responses = responses or {}
        self.default = default if default is not None else ENDERECO_A
        self.requests = []
        self._hold = set()
        self._pending = {}
        self._fail = {}
        page.route("https://viacep.com.br/ws/**", self._handle)
        page.route("**/api/resumo-multiuser", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"ok":true,"plano":"multiuser","acessos":5,"valor_por_acesso":"49.90","total_mensal":"249.50"}'
        ))
        page.route("**/api/contratacao/stripe/iniciar", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"ok":false,"erro":"checkout-nao-deve-disparar"}'
        ))

    def _handle(self, route):
        req = route.request
        cep = _extract_cep_from_url(req.url)
        self.requests.append({
            "url": req.url,
            "method": req.method,
            "post_data": req.post_data,
            "cep": cep,
        })
        if cep in self._hold:
            self._pending.setdefault(cep, []).append(route)
            return
        self._settle(route, cep)

    def _settle(self, route, cep):
        mode = self._fail.get(cep)
        if mode == "http":
            route.fulfill(status=500, body="erro")
            return
        if mode == "network":
            route.abort("connectionrefused")
            return
        if mode == "timeout":
            return
        if mode == "json":
            route.fulfill(status=200, content_type="application/json", body="nao-e-json")
            return
        if mode == "erro":
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"erro": True}))
            return
        payload = self.responses.get(cep, self.default)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    def hold(self, cep):
        self._hold.add(cep)

    def pending_count(self, cep):
        return len(self._pending.get(cep, []))

    def release(self, cep):
        self._hold.discard(cep)
        for route in self._pending.pop(cep, []):
            try:
                self._settle(route, cep)
            except Exception:
                pass

    def fail(self, cep, mode):
        self._fail[cep] = mode

    def assert_privacy(self):
        for item in self.requests:
            assert item["method"] == "GET"
            assert item["post_data"] is None
            assert item["cep"].isdigit()
            assert len(item["cep"]) == 8
            url = item["url"].lower()
            for forbidden in ("cnpj", "razao", "email", "stripe", "quantity", "complemento", "numero"):
                assert forbidden not in url


def _open(page, *, prefill=None, viewport=None):
    if viewport:
        page.set_viewport_size(viewport)
    html = _build_test_html(prefill=prefill)
    page.set_content(html, wait_until="domcontentloaded")


def _wait_status(page, texto):
    page.wait_for_function(
        "expected => (document.getElementById('mu-cep-status') || {}).textContent === expected",
        arg=texto,
        timeout=12000,
    )


def _values(page):
    return page.evaluate(
        """() => ({
            logradouro: document.getElementById('mu-logradouro').value,
            bairro: document.getElementById('mu-bairro').value,
            cidade: document.getElementById('mu-cidade').value,
            uf: document.getElementById('mu-uf').value,
            numero: document.getElementById('mu-numero').value,
            complemento: document.getElementById('mu-complemento').value,
            cep: document.getElementById('mu-cep').value,
            readonly: ['mu-logradouro','mu-bairro','mu-cidade','mu-uf'].map((id) => ({
                id,
                readonly: document.getElementById(id).readOnly,
                disabled: document.getElementById(id).disabled,
            })),
        })"""
    )


def _payload_keys(page):
    return page.evaluate(
        """() => {
            const form = document.getElementById('form-multiuser');
            const named = Array.from(form.querySelectorAll('[name]')).map((el) => {
                const map = {
                    razao_social: 'razao_social',
                    nome_fantasia: 'nome_fantasia',
                    cnpj: 'cnpj',
                    email_empresarial: 'email_empresarial',
                    endereco_logradouro: 'endereco_logradouro',
                    endereco_numero: 'endereco_numero',
                    endereco_complemento: 'endereco_complemento',
                    endereco_bairro: 'endereco_bairro',
                    endereco_cidade: 'endereco_cidade',
                    endereco_uf: 'endereco_uf',
                    endereco_cep: 'endereco_cep',
                    quantity: 'quantity',
                };
                return map[el.name] || el.name;
            });
            return ['csrf_token', ...named];
        }"""
    )


def test_browser_valid_cep_autofills_and_keeps_manual_fields(page):
    mock = ViaCepMock(page, responses={CEP_A: ENDERECO_A})
    _open(page, prefill={"mu-numero": "100", "mu-complemento": "Sala 12"})
    page.fill("#mu-numero", "100")
    page.fill("#mu-complemento", "Sala 12")
    page.fill("#mu-cep", CEP_A)
    _wait_status(page, "CEP localizado.")
    vals = _values(page)
    assert vals["logradouro"] == "Avenida Paulista"
    assert vals["bairro"] == "Bela Vista"
    assert vals["cidade"] == "São Paulo"
    assert vals["uf"] == "SP"
    assert vals["numero"] == "100"
    assert vals["complemento"] == "Sala 12"
    assert all((not item["readonly"] and not item["disabled"]) for item in vals["readonly"])
    page.fill("#mu-logradouro", "Avenida Paulista Editada")
    assert page.input_value("#mu-logradouro") == "Avenida Paulista Editada"
    mock.assert_privacy()
    assert [item["cep"] for item in mock.requests] == [CEP_A]


def test_browser_buscar_cep_button_does_not_submit_and_uses_same_lookup(page):
    mock = ViaCepMock(page, responses={CEP_A: ENDERECO_A})
    _open(page)
    submitted = page.evaluate(
        """() => {
            window.__muSubmitted = false;
            document.getElementById('form-multiuser').addEventListener('submit', () => {
                window.__muSubmitted = true;
            });
            return true;
        }"""
    )
    assert submitted
    page.fill("#mu-cep", CEP_A_HIFEN)
    page.wait_for_timeout(50)
    page.click("#mu-buscar-cep")
    _wait_status(page, "CEP localizado.")
    assert page.evaluate("window.__muSubmitted") is False
    assert page.locator("#mu-buscar-cep").get_attribute("type") == "button"
    assert page.input_value("#mu-cidade") == "São Paulo"
    assert len(mock.requests) == 1
    assert mock.requests[0]["cep"] == CEP_A


def test_browser_auto_search_on_complete_cep_and_paste_without_incomplete_calls(page):
    mock = ViaCepMock(page, responses={CEP_A: ENDERECO_A})
    _open(page)
    page.fill("#mu-cep", "0131010")
    page.wait_for_timeout(450)
    assert mock.requests == []
    page.evaluate(
        """() => {
            const el = document.getElementById('mu-cep');
            el.focus();
            el.value = '01310-100';
            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertFromPaste' }));
        }"""
    )
    _wait_status(page, "CEP localizado.")
    assert [item["cep"] for item in mock.requests] == [CEP_A]
    page.evaluate(
        """() => {
            const el = document.getElementById('mu-cep');
            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText' }));
        }"""
    )
    page.wait_for_timeout(450)
    assert len(mock.requests) == 1


def test_browser_no_autoload_query_for_saved_cep(page):
    mock = ViaCepMock(page)
    _open(page, prefill={"mu-cep": CEP_A, "mu-logradouro": "Rua Salva"})
    page.wait_for_timeout(500)
    assert mock.requests == []
    assert page.input_value("#mu-logradouro") == "Rua Salva"


def test_browser_not_found_http_network_timeout_invalid_json(page):
    mock = ViaCepMock(page)
    mock.fail("00000000", "erro")
    mock.fail("11111111", "http")
    mock.fail("22222222", "network")
    mock.fail("33333333", "timeout")
    mock.fail("44444444", "json")
    _open(page)

    page.fill("#mu-cep", "00000000")
    page.click("#mu-buscar-cep")
    _wait_status(page, "CEP não encontrado. Preencha o endereço manualmente.")
    assert page.input_value("#mu-logradouro") == ""

    page.fill("#mu-cep", "11111111")
    page.click("#mu-buscar-cep")
    _wait_status(page, "Não foi possível consultar agora. Preencha o endereço manualmente.")

    page.fill("#mu-cep", "22222222")
    page.click("#mu-buscar-cep")
    _wait_status(page, "Não foi possível consultar agora. Preencha o endereço manualmente.")

    page.fill("#mu-cep", "33333333")
    page.click("#mu-buscar-cep")
    _wait_status(page, "Não foi possível consultar agora. Preencha o endereço manualmente.")

    page.fill("#mu-cep", "44444444")
    page.click("#mu-buscar-cep")
    _wait_status(page, "Não foi possível consultar agora. Preencha o endereço manualmente.")

    page.fill("#mu-logradouro", "Rua Manual")
    page.fill("#mu-cidade", "Campinas")
    page.fill("#mu-uf", "SP")
    assert page.input_value("#mu-logradouro") == "Rua Manual"
    assert page.input_value("#mu-cidade") == "Campinas"
    assert {item["cep"] for item in mock.requests} == {
        "00000000",
        "11111111",
        "22222222",
        "33333333",
        "44444444",
    }


def test_browser_partial_return_and_cep_swap_race(page):
    parcial = {"logradouro": "", "bairro": "", "localidade": "Brasília", "uf": "DF"}
    mock = ViaCepMock(
        page,
        responses={
            CEP_A: ENDERECO_A,
            CEP_B: ENDERECO_B,
            "70040902": parcial,
        },
    )
    mock.hold(CEP_A)
    _open(page)
    page.fill("#mu-cep", CEP_A)
    page.wait_for_timeout(400)
    assert mock.pending_count(CEP_A) == 1
    page.fill("#mu-logradouro", "Rua editada durante a consulta")
    mock.release(CEP_A)
    page.wait_for_timeout(300)
    assert page.input_value("#mu-logradouro") == "Rua editada durante a consulta"

    page.fill("#mu-cep", CEP_B)
    _wait_status(page, "CEP localizado.")
    vals = _values(page)
    assert vals["cidade"] == "Rio de Janeiro"
    assert vals["uf"] == "RJ"
    assert vals["logradouro"] == "Praça da Bandeira"

    mock.hold(CEP_A)
    page.fill("#mu-cep", CEP_A)
    page.wait_for_timeout(400)
    assert mock.pending_count(CEP_A) == 1
    page.fill("#mu-cep", "70040902")
    page.click("#mu-buscar-cep")
    _wait_status(page, "CEP localizado. Complete os campos restantes.")
    mock.release(CEP_A)
    page.wait_for_timeout(250)
    assert page.input_value("#mu-cidade") == "Brasília"
    assert page.input_value("#mu-uf") == "DF"
    assert page.input_value("#mu-logradouro") == ""
    assert page.input_value("#mu-bairro") == ""


def test_browser_payload_keys_and_responsive_overflow(page):
    mock = ViaCepMock(page, responses={CEP_A: ENDERECO_A})
    _open(page, viewport={"width": 1280, "height": 900})
    page.fill("#mu-cep", CEP_A)
    page.click("#mu-buscar-cep")
    _wait_status(page, "CEP localizado.")
    keys = _payload_keys(page)
    assert set(keys) == set(COLETAR_KEYS)
    assert keys.count("csrf_token") == 1
    for width, height in ((1280, 900), (768, 1024), (375, 812)):
        page.set_viewport_size({"width": width, "height": height})
        page.wait_for_timeout(80)
        metrics = page.evaluate(
            """() => {
                const form = document.getElementById('form-multiuser');
                const lookup = form.querySelector('.mu-cep-lookup');
                const button = document.getElementById('mu-buscar-cep');
                const cep = document.getElementById('mu-cep');
                return {
                    docOverflow: document.documentElement.scrollWidth > window.innerWidth + 2,
                    formOverflow: form.scrollWidth > form.clientWidth + 2,
                    lookupOverflow: lookup.scrollWidth > lookup.clientWidth + 2,
                    buttonVisible: button.getBoundingClientRect().width > 0,
                    cepVisible: cep.getBoundingClientRect().width > 0,
                    stacked: button.getBoundingClientRect().top > cep.getBoundingClientRect().bottom - 1,
                    sideBySide: Math.abs(button.getBoundingClientRect().top - cep.getBoundingClientRect().top) < 8,
                };
            }"""
        )
        assert metrics["buttonVisible"]
        assert metrics["cepVisible"]
        assert not metrics["docOverflow"]
        assert not metrics["formOverflow"]
        assert not metrics["lookupOverflow"]
        if width >= 1280:
            assert metrics["sideBySide"]
        if width <= 375:
            assert metrics["stacked"]
    mock.assert_privacy()
