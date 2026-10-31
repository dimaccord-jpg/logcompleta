"""SCRUM-144: colaboração guardada no navegador depois de reinício, 404 e estados terminais.

Os scripts reais de descoberta, reinício e jornada rodam em navegador headless.
Todas as rotas são simuladas por `window.fetch`; nenhuma chamada de IA sai do teste.
"""
from __future__ import annotations

import json

import pytest

from tests.test_agente_bi_colaboracao import _posts_descoberta, _segurar_descoberta, _servir
from tests.test_agente_bi_jornada import DESCOBERTA, JORNADA, ROOT, _html, _instalar, _preparacao
from tests.test_agente_bi_page import _browser_document, _launch_browser

CHAVE = "agente-bi-colaboracao"
PEDIDO = {"modalidades": ["A"], "afirmacoes": [], "formulas": [], "texto": ""}
NEUTRA = "A análise anterior não pode mais ser recuperada. Envie os dados para começar uma nova análise."
INDISPONIVEL = "O recurso não está disponível para esta conta."
REINICIO = (ROOT / "app" / "static" / "js" / "agente_bi_reinicio.js").read_text(encoding="utf-8").replace(
    "window.location.reload();",
    "window.__recargas = (window.__recargas || 0) + 1;"
    " window.__chaveNoReload = localStorage.getItem('agente-bi-colaboracao');",
)


def _guardado(dataset_id=7, fase="claude_analisando"):
    return {"datasetId": dataset_id, "pedido": PEDIDO, "fase": fase}


def _colaboracao(estado, fase=None, continuar=False):
    return {"ok": True, "colaboracao": {"estado": estado, "fase": fase, "continuar": continuar}}


@pytest.fixture
def navegador(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    servidor, url = _servir(_browser_document(_html(monkeypatch)))
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(url, wait_until="domcontentloaded")
        page.evaluate("() => localStorage.clear()")
        try:
            yield page, url
        finally:
            browser.close()
            servidor.shutdown()


def _carregar(page, url, *, descoberta=None, falhar="", segurar=False):
    """Uma carga da página: a recarga nativa apaga o mock, então cada carga o reinstala."""
    page.goto(url, wait_until="domcontentloaded")
    _instalar(page)
    if segurar:
        _segurar_descoberta(page)
    page.evaluate(
        "([corpo, falhar]) => { if (corpo) window.__descoberta = corpo; window.__falhar = falhar; }",
        [descoberta, falhar],
    )
    for fonte in (DESCOBERTA, REINICIO, JORNADA):
        page.add_script_tag(content=fonte)
    page.wait_for_function("() => window.__bi.some((item) => item.url.indexOf('/analises') !== -1)")
    if not segurar:
        page.wait_for_function("() => !document.getElementById('agente-bi-descoberta').classList.contains('is-ocupado')")
    page.wait_for_timeout(250)


def _guardar(page, valor):
    page.evaluate("([k, v]) => localStorage.setItem(k, v)", [CHAVE, json.dumps(valor)])


def _chave(page):
    bruto = page.evaluate("(k) => localStorage.getItem(k)", CHAVE)
    return json.loads(bruto) if bruto else None


def _erro(page):
    return page.evaluate(
        "() => { const e = document.getElementById('agente-bi-descoberta-erro'); return e.hidden ? null : e.textContent; }"
    )


def _fase(page):
    return page.evaluate("() => document.getElementById('agente-bi-shell').getAttribute('data-jornada-fase')")


def _reiniciar(page):
    page.locator("#agente-bi-reinicio").evaluate("(el) => { el.hidden = false; el.click(); }")
    page.locator("#agente-bi-reinicio-confirmar").evaluate("(el) => el.click()")


@pytest.mark.parametrize("fase", ["claude_analisando", "gemini_revisando"])
def test_dataset_obsoleto_gera_um_404_limpa_a_chave_e_nao_repete(navegador, fase):
    page, url = navegador
    _guardar(page, _guardado(7, fase))
    nao_encontrado = {"__status": 404, "ok": False, "codigo": "nao_encontrado"}
    _carregar(page, url, descoberta=nao_encontrado)
    posts = _posts_descoberta(page)
    assert len(posts) == 1
    assert json.loads(posts[0]["body"]) == {"dataset_id": 7, "pedido": PEDIDO}
    assert _chave(page) is None
    assert _erro(page) == NEUTRA
    assert INDISPONIVEL not in page.locator("body").inner_text()
    assert _fase(page) == "upload"
    assert page.evaluate("() => document.getElementById('agente-bi-upload-item').disabled") is False
    assert page.locator("#agente-bi-prosseguir").is_hidden()
    assert page.locator("#agente-bi-resultado").is_hidden()
    for _ in range(2):
        _carregar(page, url, descoberta=nao_encontrado)
        assert _posts_descoberta(page) == []
        assert _erro(page) is None
        assert _fase(page) == "upload"


def test_upload_depois_de_recuperacao_invalida_segue_com_o_novo_dataset(navegador):
    page, url = navegador
    _guardar(page, _guardado(7))
    _carregar(page, url, descoberta={"__status": 404, "ok": False, "codigo": "nao_encontrado"})
    assert _chave(page) is None
    page.evaluate(
        "(corpo) => { window.__preparacao = corpo; window.__descoberta = { ok: true, colaboracao: "
        "{ estado: 'em_andamento', fase: 'gemini_revisando', continuar: false } }; }",
        _preparacao(),
    )
    page.locator("#agente-bi-arquivo").set_input_files(
        files=[{"name": "fretes.csv", "mimeType": "text/csv", "buffer": b"alfa,valor\nA,10\n"}]
    )
    page.wait_for_selector("#agente-bi-prosseguir:not([hidden])")
    assert _erro(page) is None
    page.locator("#agente-bi-prosseguir").click()
    page.locator("#agente-bi-examinar").click()
    page.wait_for_function("() => !document.getElementById('agente-bi-descoberta').classList.contains('is-ocupado')")
    posts = _posts_descoberta(page)
    assert [json.loads(item["body"])["dataset_id"] for item in posts] == [7, 9]
    assert _chave(page)["datasetId"] == 9


def test_recuperacao_valida_reenvia_o_pedido_original_uma_vez(navegador):
    page, url = navegador
    _guardar(page, _guardado(9))
    _carregar(page, url, descoberta=_colaboracao("em_andamento", "gemini_revisando"))
    posts = _posts_descoberta(page)
    assert len(posts) == 1
    assert json.loads(posts[0]["body"]) == {"dataset_id": 9, "pedido": PEDIDO}
    assert _chave(page) == _guardado(9, "gemini_revisando")
    assert _erro(page) is None
    _carregar(page, url, descoberta=_colaboracao("em_andamento", "gemini_revisando"))
    assert len(_posts_descoberta(page)) == 1


@pytest.mark.parametrize(
    "descoberta, falhar",
    [
        ({"__status": 401, "ok": False, "codigo": "autenticacao_necessaria"}, ""),
        ({"__status": 403, "ok": False, "codigo": "operacao_nao_permitida"}, ""),
        ({"__status": 409, "ok": False, "codigo": "operacao_em_andamento"}, ""),
        ({"__status": 500, "ok": False, "codigo": "provedor_indisponivel"}, ""),
        ({"__status": 503}, ""),
        ({"__status": 404, "ok": False}, ""),
        (None, "/descoberta"),
    ],
    ids=["401", "403", "409", "500", "503-sem-corpo", "404-sem-codigo", "rede"],
)
def test_erros_recuperaveis_preservam_a_referencia(navegador, descoberta, falhar):
    page, url = navegador
    _guardar(page, _guardado(9))
    _carregar(page, url, descoberta=descoberta, falhar=falhar)
    assert len(_posts_descoberta(page)) == 1
    assert _chave(page) == _guardado(9)
    assert _erro(page) not in (None, NEUTRA)


def test_solicitacao_nova_com_404_nao_e_tratada_como_recuperacao(navegador):
    page, url = navegador
    _carregar(page, url)
    page.evaluate(
        "(corpo) => { window.__preparacao = corpo; window.__descoberta = { __status: 404, ok: false, codigo: 'nao_encontrado' }; }",
        _preparacao(),
    )
    page.locator("#agente-bi-arquivo").set_input_files(
        files=[{"name": "fretes.csv", "mimeType": "text/csv", "buffer": b"alfa,valor\nA,10\n"}]
    )
    page.wait_for_selector("#agente-bi-prosseguir:not([hidden])")
    page.locator("#agente-bi-prosseguir").click()
    page.locator("#agente-bi-examinar").click()
    page.wait_for_function("() => !document.getElementById('agente-bi-descoberta').classList.contains('is-ocupado')")
    assert len(_posts_descoberta(page)) == 1
    assert _erro(page) == INDISPONIVEL
    guardado = _chave(page)
    assert (guardado["datasetId"], guardado["fase"]) == (9, "claude_analisando")


@pytest.mark.parametrize("estado", ["encerrada", "recusada"])
def test_estado_terminal_definitivo_mostra_a_parada_e_nao_volta_a_enviar(navegador, estado):
    page, url = navegador
    _guardar(page, _guardado(9))
    corpo = _colaboracao(estado)
    corpo["diagnosticos"] = [{"codigo": "cobertura_parcial", "mensagem": "Parte das sugestões foi descartada."}]
    _carregar(page, url, descoberta=corpo)
    assert len(_posts_descoberta(page)) == 1
    assert _chave(page) is None
    texto = page.locator("#agente-bi-descobertas").inner_text()
    assert texto.strip()
    assert "Parte das sugestões foi descartada." in page.locator("#agente-bi-diagnosticos-descobertas").inner_text()
    _carregar(page, url, descoberta=corpo)
    assert _posts_descoberta(page) == []


def test_estado_incerto_preserva_a_referencia_para_a_reconciliacao(navegador):
    page, url = navegador
    _guardar(page, _guardado(9, "gemini_revisando"))
    _carregar(page, url, descoberta=_colaboracao("incerta", "gemini_revisando"))
    assert _chave(page) == _guardado(9, "gemini_revisando")
    _carregar(page, url, descoberta=_colaboracao("incerta", "gemini_revisando"))
    assert len(_posts_descoberta(page)) == 1


def test_descoberta_pronta_preserva_a_referencia_ate_a_selecao(navegador):
    page, url = navegador
    _guardar(page, _guardado(9, "claude_consolidando"))
    pronta = {
        "ok": True,
        "analise_id": 15,
        "resultado": {"descobertas": [], "sugestoes": [], "perguntas": [], "critica_referencia": []},
        "colaboracao": {"estado": "concluida", "fase": "pronto", "continuar": False},
    }
    _carregar(page, url, descoberta=pronta)
    assert _chave(page) == _guardado(9, "pronto")
    _carregar(page, url, descoberta=pronta)
    assert len(_posts_descoberta(page)) == 1


def test_reinicio_confirmado_apaga_so_a_colaboracao_antes_da_recarga(navegador):
    page, url = navegador
    _guardar(page, _guardado(9))
    page.evaluate("() => localStorage.setItem('agentefrete-theme', 'dark')")
    _carregar(page, url, descoberta=_colaboracao("em_andamento", "gemini_revisando"))
    assert _chave(page) is not None
    _reiniciar(page)
    page.wait_for_function("() => window.__recargas === 1")
    assert page.evaluate("() => window.__chaveNoReload") is None
    assert page.evaluate("() => localStorage.getItem('agentefrete-theme')") == "dark"
    _carregar(page, url, descoberta=_colaboracao("em_andamento", "gemini_revisando"))
    assert _posts_descoberta(page) == []
    assert _erro(page) is None


@pytest.mark.parametrize(
    "reinicio, falhar",
    [
        ({"__status": 409, "ok": False, "codigo": "execucao_em_andamento"}, ""),
        ({"__status": 500, "ok": False, "codigo": "reinicio_indisponivel"}, ""),
        ({"__status": 200, "ok": False}, ""),
        (None, "/reinicio"),
    ],
    ids=["409", "500", "200-sem-ok", "rede"],
)
def test_reinicio_recusado_preserva_a_colaboracao(navegador, reinicio, falhar):
    page, url = navegador
    _guardar(page, _guardado(9))
    _carregar(page, url, descoberta=_colaboracao("em_andamento", "gemini_revisando"))
    page.evaluate(
        "([corpo, falhar]) => { if (corpo) window.__reinicio = corpo; window.__falhar = falhar; }",
        [reinicio, falhar],
    )
    _reiniciar(page)
    page.wait_for_function("() => !document.getElementById('agente-bi-reinicio-erro').hidden")
    assert page.evaluate("() => window.__recargas") == 0
    assert _chave(page) == _guardado(9, "gemini_revisando")


@pytest.mark.parametrize("continuar", [False, True], ids=["parada", "continuacao"])
def test_resposta_antiga_depois_do_reinicio_nao_restaura_a_colaboracao(navegador, continuar):
    page, url = navegador
    _guardar(page, _guardado(9))
    _carregar(page, url, descoberta=_colaboracao("em_andamento", "claude_consolidando", continuar), segurar=True)
    page.wait_for_function("() => typeof window.__soltarDescoberta === 'function'")
    assert len(_posts_descoberta(page)) == 1
    _reiniciar(page)
    page.wait_for_function("() => window.__recargas === 1")
    assert page.evaluate("() => window.__chaveNoReload") is None
    page.evaluate("() => window.__soltarDescoberta()")
    page.wait_for_timeout(400)
    assert _chave(page) is None
    assert len(_posts_descoberta(page)) == 1
    _carregar(page, url)
    assert _posts_descoberta(page) == []
