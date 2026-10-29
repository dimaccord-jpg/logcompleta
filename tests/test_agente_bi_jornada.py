"""Jornada visual progressiva do AgenteBI, sem chamada de IA, cálculo ou montagem reais."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_agente_bi_page import GUIDANCE, _browser_document, _client, _launch_browser

ROOT = Path(__file__).resolve().parents[1]
DESCOBERTA = (ROOT / "app" / "static" / "js" / "agente_bi_descoberta.js").read_text(encoding="utf-8")
DASHBOARD = (ROOT / "app" / "static" / "js" / "agente_bi_dashboard.js").read_text(encoding="utf-8")
REINICIO = (ROOT / "app" / "static" / "js" / "agente_bi_reinicio.js").read_text(encoding="utf-8").replace(
    "window.location.reload();",
    "window.__recargas = (window.__recargas || 0) + 1;",
)


def _html(monkeypatch):
    web = _client(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_INGESTAO_ENABLED", "1")
    return web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)


def _sugestao(indice, status="viavel"):
    return {
        "id": f"s{indice}",
        "status": status,
        "leitura": "indicador_de_medida" if status == "viavel" else "esclarecimento_necessario",
        "grafico": "indicador" if status == "viavel" else None,
        "operacao": "soma" if status == "viavel" else None,
        "origem": "dados",
        "regiao": {"arquivo": 0, "aba": 0, "regiao": 0},
        "medida": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1} if status == "viavel" else None,
        "agrupamento": None,
        "segunda_medida": None,
        "evidencias": ["tipo_numero_fracionado"] if status == "viavel" else [],
        "juncao": "nao_executada",
        "perguntas": [],
        "lacuna": None,
    }


def _preparacao(*, estado="preparado", motivo=None):
    return {
        "ok": True,
        "estado": estado,
        "motivo": motivo,
        "reutilizado": False,
        "dataset": {"id": 9, "versao": 2},
        "resumo": {
            "arquivos": 1,
            "abas": 1,
            "regioes": 1,
            "celulas": 4,
            "linhas_elegiveis": 2,
            "formulas": 0,
            "ambigua": False,
        },
        "alertas": [],
        "ambiguidades": [],
        "colunas": [{
            "arquivo": 0,
            "aba": 0,
            "regiao": 0,
            "indice": 1,
            "dimensao_principal": "valor",
        }],
        "qualidade": {},
    }


def _descoberta(sugestoes):
    return {
        "ok": True,
        "analise_id": 15,
        "resultado": {
            "descobertas": [{"id": "d1", "codigo": "regiao_com_medida", "regiao": {"arquivo": 0, "aba": 0, "regiao": 0}}],
            "sugestoes": sugestoes,
            "perguntas": [],
            "critica_referencia": [],
            "instrucoes_nao_confiaveis": {"detectadas": False},
        },
    }


def _instalar(page, *, segurar=False):
    page.evaluate(
        """(segurar) => {
          window.__bi = [];
          window.__recargas = 0;
          window.__datasets = [];
          window.__dashboards = [];
          window.__analises = [];
          window.__analisesErro = null;
          window.__dashboardsErro = null;
          window.__preparacao = { ok: false, codigo: "arquivo_corrompido" };
          window.__descoberta = { ok: false };
          window.__selecao = { ok: false };
          window.__definicao = { ok: false, codigo: "analise_indisponivel" };
          window.__calculo = { ok: false, codigo: "analise_indisponivel" };
          window.__montar = { ok: false, codigo: "analise_indisponivel" };
          window.__reinicio = { ok: true };
          window.__falhar = "";
          window.__segurar = segurar;
          window.__fila = [];
          window.Chart = function (canvas) { this.destroy = function () {}; canvas.setAttribute("data-chart", "1"); };
          function responder(url, metodo) {
            let corpo = { ok: true, analises: [], dashboards: [], datasets: [] };
            if (window.__falhar && url.indexOf(window.__falhar) !== -1) {
              return Promise.reject(new Error("rede"));
            }
            if (url.indexOf("/datasets") !== -1) corpo = { ok: true, datasets: window.__datasets };
            else if (url.indexOf("/consulta") !== -1) corpo = window.__consulta || { ok: false };
            else if (url.indexOf("/dashboards") !== -1 && metodo === "GET") {
              corpo = window.__dashboardsErro || { ok: true, dashboards: window.__dashboards };
            }
            else if (url.indexOf("/analises") !== -1) {
              corpo = window.__analisesErro || { ok: true, analises: window.__analises };
            }
            else if (url.indexOf("/descoberta/preparacao") !== -1) corpo = window.__preparacao;
            else if (url.indexOf("/descoberta/selecao") !== -1) corpo = window.__selecao;
            else if (url.indexOf("/descoberta") !== -1) corpo = window.__descoberta;
            else if (url.indexOf("/definicao") !== -1) corpo = window.__definicao;
            else if (url.indexOf("/calculo") !== -1) corpo = window.__calculo;
            else if (url.indexOf("/montar") !== -1) corpo = window.__montar;
            else if (url.indexOf("/reinicio") !== -1) corpo = window.__reinicio;
            const status = corpo && corpo.__status ? corpo.__status : 200;
            return Promise.resolve({ status: status, json: () => Promise.resolve(corpo) });
          }
          window.fetch = (url, opts) => {
            const alvo = String(url);
            const metodo = (opts && opts.method) || "GET";
            window.__bi.push({ url: alvo, method: metodo, body: opts && typeof opts.body === "string" ? opts.body : "" });
            if (window.__segurar && metodo === "GET") {
              return new Promise((resolve, reject) => {
                window.__fila.push(() => responder(alvo, metodo).then(resolve, reject));
              });
            }
            return responder(alvo, metodo);
          };
          window.__soltar = () => {
            window.__segurar = false;
            window.__fila.splice(0).forEach((soltar) => soltar());
          };
        }""",
        segurar,
    )


def _scripts(page, *nomes):
    fontes = {"descoberta": DESCOBERTA, "dashboard": DASHBOARD, "reinicio": REINICIO}
    for nome in nomes:
        page.add_script_tag(content=fontes[nome])


def _posts(page, trecho):
    return page.evaluate(
        """(trecho) => window.__bi.filter((item) => item.method === "POST" && item.url.indexOf(trecho) !== -1).length""",
        trecho,
    )


def _registro(identificador, etapa, contrato, origem=None, campo=None):
    item = {
        "id": identificador,
        "etapa": etapa,
        "estado": "concluida",
        "contrato_versao": contrato,
        "dataset_id": 9,
        "versao": 2,
    }
    if origem is not None:
        item["resultado"] = {campo: origem, "dataset_id": 9}
    return item


def _marcar_aprovada(page, selecao, versao):
    page.locator("#agente-bi-aprovada").evaluate(
        """(el, dados) => {
          el.setAttribute("data-selecao", String(dados.selecao));
          el.setAttribute("data-versao", String(dados.versao));
        }""",
        {"selecao": selecao, "versao": versao},
    )


def _esperar_origens(page, presente, ausente):
    page.evaluate(
        "(dados) => { window.__esperado = dados; }",
        {"presente": presente, "ausente": ausente},
    )
    page.wait_for_function(
        """() => {
          const textos = [...document.querySelectorAll("#agente-bi-dashboard-origens button")].map((botao) => botao.textContent);
          const tem = (texto) => textos.some((item) => item.indexOf(texto) !== -1);
          const naoTem = (texto) => textos.every((item) => item.indexOf(texto) === -1);
          return window.__esperado.presente.every(tem) && window.__esperado.ausente.every(naoTem);
        }"""
    )


def _sem_execucao(page):
    assert _posts(page, "/definicao") == 0
    assert _posts(page, "/calculo") == 0
    assert _posts(page, "/montar") == 0
    assert _posts(page, "/descoberta") == 0


def test_marcacao_inicial_so_mostra_identidade_compositor_e_menu(monkeypatch):
    html = _html(monkeypatch)
    assert "Enviar dados logísticos" not in html
    assert 'id="agente-bi-enviar"' not in html
    assert 'id="agente-bi-descoberta"' in html
    assert 'id="agente-bi-prosseguir"' in html
    assert "A — Receber sugestões da IA" in html
    assert "B — Utilizar referências analíticas da planilha" in html
    assert "C — Descrever livremente o que deseja analisar" in html
    assert "O chat continua bloqueado." in html
    assert GUIDANCE in html
    assert 'data-authenticated="true"' in html
    assert 'id="agente-bi-login"' not in html
    assert "Autenticação necessária para utilizar o AgenteBI." not in html
    assert "Carregando dashboards salvos." in html
    assert 'id="agente-bi-reinicio"' in html
    assert 'type="file"' not in html
    descoberta = html[html.index('id="agente-bi-descoberta"'):html.index('id="agente-bi-dashboard"')]
    assert "hidden" in descoberta
    dashboard = html[html.index('id="agente-bi-dashboard"'):html.index("agente-bi-composer-area")]
    assert "hidden" in dashboard
    reinicio = html[html.index('id="agente-bi-reinicio"'):html.index('id="agente-bi-reinicio"') + 280]
    assert "hidden" in reinicio


def test_primeiro_acesso_upload_preparacao_e_sem_execucao_automatica(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    documento = _browser_document(_html(monkeypatch))
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page, segurar=True)
        _scripts(page, "descoberta", "dashboard", "reinicio")

        assert page.locator("#agente-bi-descoberta").is_hidden()
        assert page.locator("#agente-bi-dashboard").is_hidden()
        assert page.locator("#agente-bi-reinicio").is_hidden()
        assert page.locator("#agente-bi-input").evaluate("el => el.readOnly") is True
        assert page.locator("#agente-bi-send").is_disabled()
        assert page.locator("#agente-bi-enviar").count() == 0
        assert _posts(page, "/definicao") == 0
        assert _posts(page, "/calculo") == 0
        assert _posts(page, "/montar") == 0
        assert _posts(page, "/descoberta") == 0

        page.locator("#agente-bi-actions-trigger").click()
        page.locator("#agente-bi-upload-item").evaluate("el => { el.click = () => {}; }")
        page.evaluate("() => { const arquivo = document.getElementById('agente-bi-arquivo'); arquivo.click = () => { window.__abriuArquivo = 1; }; }")
        page.locator("#agente-bi-upload-item").click()
        assert page.evaluate("() => window.__abriuArquivo") == 1

        page.evaluate("() => window.__soltar()")
        page.wait_for_function(
            """() => window.__bi.filter((item) => item.method === "GET").length >= 4
              && document.getElementById("agente-bi-reinicio").hidden"""
        )
        assert _posts(page, "/definicao") == 0
        assert _posts(page, "/calculo") == 0
        assert _posts(page, "/montar") == 0

        page.evaluate("() => { window.__preparacao = { ok: false, codigo: 'arquivo_corrompido' }; }")
        page.locator("#agente-bi-arquivo").set_input_files(
            files=[{"name": "falha.csv", "mimeType": "text/csv", "buffer": b"a,b\n1,2\n"}]
        )
        page.wait_for_selector("#agente-bi-descoberta-erro:not([hidden])")
        assert "não pôde ser lido" in page.locator("#agente-bi-descoberta-erro").inner_text()
        assert page.locator("#agente-bi-descoberta-form").is_hidden()
        assert page.locator("#agente-bi-prosseguir").is_hidden()
        assert page.locator("#agente-bi-modalidades").evaluate("el => el.disabled") is True

        page.evaluate("(corpo) => { window.__preparacao = corpo; }", _preparacao(estado="nao_persistido", motivo="arquivo_indevido"))
        page.locator("#agente-bi-arquivo").set_input_files(
            files=[{"name": "recusa.csv", "mimeType": "text/csv", "buffer": b"a,b\n1,2\n"}]
        )
        page.wait_for_function("() => document.getElementById('agente-bi-preparacao-lista').textContent.indexOf('não persistido') !== -1")
        assert page.locator("#agente-bi-prosseguir").is_hidden()

        page.evaluate("(corpo) => { window.__preparacao = corpo; }", _preparacao())
        page.locator("#agente-bi-arquivo").set_input_files(
            files=[{"name": "fretes.csv", "mimeType": "text/csv", "buffer": b"alfa,valor\nA,10\n"}]
        )
        page.wait_for_selector("#agente-bi-prosseguir:not([hidden])")
        texto = page.locator("#agente-bi-preparacao-lista").inner_text()
        assert "preparado" in texto
        assert "Versão do dataset" in texto
        assert "2" in texto
        assert page.locator("#agente-bi-descoberta-form").is_hidden()
        assert page.locator("#agente-bi-reinicio").is_visible()
        assert _posts(page, "/definicao") == 0

        page.locator("#agente-bi-prosseguir").click()
        assert page.locator("#agente-bi-descoberta-form").is_visible()
        assert page.locator("#agente-bi-modalidade-a").is_checked()

        for largura in (1280, 390):
            page.set_viewport_size({"width": largura, "height": 900})
            caixa = page.locator("#agente-bi-shell").bounding_box()
            assert caixa is not None
            assert caixa["width"] <= largura
            assert page.locator("#agente-bi-actions-trigger").bounding_box()["height"] >= 44
            assert page.locator("#agente-bi-examinar").bounding_box()["height"] >= 44
        browser.close()


def test_combinacoes_revisao_e_selecao_de_zero_um_e_limite(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    documento = _browser_document(_html(monkeypatch))
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page)
        page.evaluate("(corpo) => { window.__preparacao = corpo; }", _preparacao())
        page.evaluate("(corpo) => { window.__descoberta = corpo; }", _descoberta([_sugestao(1, "condicional")]))
        page.evaluate(
            """() => { window.__selecao = { ok: true, selecao: { id: 16, versao: 2, graficos: 1 } }; }"""
        )
        _scripts(page, "descoberta", "dashboard")
        page.locator("#agente-bi-arquivo").set_input_files(
            files=[{"name": "fretes.csv", "mimeType": "text/csv", "buffer": b"alfa,valor\nA,10\n"}]
        )
        page.wait_for_selector("#agente-bi-prosseguir:not([hidden])")
        page.locator("#agente-bi-prosseguir").click()

        page.locator("#agente-bi-examinar").click()
        page.wait_for_selector("#agente-bi-resultado:not([hidden])")
        pedido_a = json.loads(page.evaluate(
            """() => window.__bi.filter((item) => item.method === "POST" && item.url.replace(/\\/$/, "").endsWith("/descoberta")).pop().body"""
        ))
        assert pedido_a["pedido"]["modalidades"] == ["A"]
        assert "referencia" not in pedido_a["pedido"]
        assert "texto" not in pedido_a["pedido"]
        assert page.locator("#agente-bi-revisao").is_visible()
        assert page.locator("#agente-bi-selecao-painel").is_visible()
        assert page.locator("#agente-bi-revisao .agente-bi-grafico").count() == 0
        assert page.locator("[data-status='condicional'] .agente-bi-grafico").count() == 0
        assert "Não há gráfico viável" in page.locator("#agente-bi-selecao-contagem").inner_text()
        assert page.locator("#agente-bi-aprovar").is_disabled()
        assert "não é uma análise executável" in page.locator("[data-status='condicional']").inner_text()

        page.evaluate("(corpo) => { window.__descoberta = corpo; }", _descoberta([_sugestao(1)]))
        page.locator("#agente-bi-modalidade-a").uncheck()
        page.locator("#agente-bi-modalidade-b").check()
        page.locator('[data-campo="operacao"]').select_option("soma")
        page.locator('[data-campo="medida"]').select_option("0")
        page.locator("#agente-bi-examinar").click()
        page.wait_for_function("() => document.querySelectorAll('.agente-bi-grafico').length === 1")
        pedido_b = json.loads(page.evaluate(
            """() => window.__bi.filter((item) => item.method === "POST" && item.url.replace(/\\/$/, "").endsWith("/descoberta")).pop().body"""
        ))
        assert pedido_b["pedido"]["modalidades"] == ["B"]
        assert pedido_b["pedido"]["referencia"]["afirmacoes"][0]["operacao"] == "soma"
        assert pedido_b["pedido"]["referencia"]["afirmacoes"][0]["juncao"] is False

        page.locator("#agente-bi-modalidade-b").uncheck()
        page.locator("#agente-bi-modalidade-c").check()
        page.locator("#agente-bi-texto").fill("quero o total")
        page.evaluate(
            "(corpo) => { window.__descoberta = corpo; }",
            _descoberta([_sugestao(indice) for indice in range(1, 8)]),
        )
        page.locator("#agente-bi-examinar").click()
        page.wait_for_function("() => document.querySelectorAll('.agente-bi-grafico').length === 7")
        identificacao = page.locator(".agente-bi-grafico").evaluate_all(
            """(caixas) => caixas.map((caixa) => {
              const card = caixa.closest("article");
              const rotulo = (caixa.getAttribute("aria-label") || "").trim();
              const visivel = (caixa.labels && caixa.labels[0] && caixa.labels[0].textContent || "").trim();
              return {
                id: caixa.getAttribute("data-sugestao"),
                card: card && card.getAttribute("data-id"),
                rotulo: rotulo,
                visivel: visivel,
                noCartao: !!(card && card.contains(caixa)),
                naListaSeparada: !!caixa.closest("#agente-bi-selecao-graficos")
              };
            })"""
        )
        assert len(identificacao) == 7
        assert len({item["rotulo"] for item in identificacao}) == 7
        for item in identificacao:
            assert item["noCartao"] is True
            assert item["naListaSeparada"] is False
            assert item["id"] == item["card"]
            assert item["id"] in item["rotulo"]
            assert item["rotulo"] == item["visivel"]
            assert "indicador" in item["rotulo"]
            assert "soma" in item["rotulo"]
        assert page.get_by_role("checkbox", name="Incluir indicador · soma (s1)").count() == 1
        assert page.get_by_role("checkbox", name="Incluir indicador · soma (s7)").count() == 1
        page.locator(".agente-bi-grafico").nth(0).focus()
        assert page.evaluate("() => document.activeElement.getAttribute('data-sugestao')") == "s1"
        pedido_c = json.loads(page.evaluate(
            """() => window.__bi.filter((item) => item.method === "POST" && item.url.replace(/\\/$/, "").endsWith("/descoberta")).pop().body"""
        ))
        assert pedido_c["pedido"]["modalidades"] == ["C"]
        assert pedido_c["pedido"]["texto"] == "quero o total"
        caixas = page.locator(".agente-bi-grafico")
        caixas.nth(0).check()
        assert page.locator("#agente-bi-aprovar").is_disabled()
        page.locator("#agente-bi-confirmacao").check()
        assert page.locator("#agente-bi-aprovar").is_enabled()
        page.locator("#agente-bi-confirmacao").uncheck()
        for indice in range(6):
            caixas.nth(indice).check()
        assert caixas.nth(6).is_disabled()
        assert "6 de 6" in page.locator("#agente-bi-selecao-contagem").inner_text()
        page.locator("#agente-bi-confirmacao").check()
        page.evaluate(
            """() => {
              window.__analises = [{
                id: 16, etapa: "selecao", estado: "concluida", contrato_versao: "selecao.descoberta.v1"
              }];
            }"""
        )
        page.locator("#agente-bi-aprovar").click()
        page.wait_for_selector("#agente-bi-aprovada:not([hidden])")
        assert page.locator("#agente-bi-aprovada").get_attribute("data-selecao") == "16"
        assert page.locator("#agente-bi-descoberta-form").is_hidden()
        page.get_by_role("button", name="Montar dashboard da seleção").wait_for()
        assert _posts(page, "/definicao") == 0
        browser.close()


def test_montagem_so_segue_com_acao_e_interrompe_na_falha(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    documento = _browser_document(_html(monkeypatch))
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page)
        page.evaluate(
            """() => {
              window.__analises = [{ id: 16, etapa: "selecao", estado: "concluida", contrato_versao: "selecao.descoberta.v1" }];
            }"""
        )
        _scripts(page, "descoberta", "dashboard")
        page.locator("#agente-bi-aprovada").evaluate(
            """(el) => { el.setAttribute("data-selecao", "16"); el.setAttribute("data-versao", "2"); }"""
        )
        botao = page.get_by_role("button", name="Montar dashboard da seleção")
        botao.wait_for()
        assert _posts(page, "/definicao") == 0
        assert _posts(page, "/calculo") == 0
        assert _posts(page, "/montar") == 0

        botao.click()
        page.wait_for_function("() => document.getElementById('agente-bi-montagem-progresso').textContent.indexOf('Definição interrompida') !== -1")
        assert _posts(page, "/calculo") == 0
        assert _posts(page, "/montar") == 0
        assert page.locator("#agente-bi-dashboard-aberto").is_hidden()

        page.evaluate("() => { window.__definicao = { ok: true, definicao: { id: 21 } }; }")
        botao.click()
        page.wait_for_function("() => document.getElementById('agente-bi-montagem-progresso').textContent.indexOf('Cálculo interrompido') !== -1")
        assert "Definição 21 concluída." in page.locator("#agente-bi-montagem-progresso").inner_text()
        assert _posts(page, "/montar") == 0

        page.evaluate("() => { window.__calculo = { ok: true, calculo: { id: 22 } }; }")
        botao.click()
        page.wait_for_function("() => document.getElementById('agente-bi-montagem-progresso').textContent.indexOf('Montagem interrompida') !== -1")
        assert "Cálculo 22 concluído." in page.locator("#agente-bi-montagem-progresso").inner_text()
        assert page.locator("#agente-bi-dashboard-aberto").is_hidden()

        page.evaluate(
            """() => {
              window.__montar = {
                ok: true,
                dashboard: { id: 30, analise_id: 22, chave: "painel", revisao: 1 },
                consulta: {
                  contrato: "dashboard.v1",
                  dashboard_id: 30,
                  revisao: 1,
                  chave: "painel",
                  calculo_id: 22,
                  definicao_id: 21,
                  dataset_id: 9,
                  versao: 2,
                  filtros: [],
                  graficos: [{
                    ref: "g1", titulo: "indicador", tipo: "indicador", status: "calculado",
                    operacao: "soma", unidade: "valor", resultado: "10", total: "10",
                    serie: [], avisos: [], elegiveis: 1, grupos: 0
                  }]
                }
              };
            }"""
        )
        botao.click()
        page.wait_for_selector("[data-resultado='10']")
        assert page.locator("#agente-bi-descoberta").is_hidden()
        assert page.locator("#agente-bi-dashboard").is_visible()
        assert page.locator("#agente-bi-dashboard-filtros").count() == 1
        browser.close()


def test_recuperacao_nao_escolhe_id_maior_sem_vinculo_e_reabre_dashboard(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    documento = _browser_document(_html(monkeypatch))
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page)
        page.evaluate(
            """() => {
              window.__analises = [
                { id: 40, etapa: "execucao", estado: "concluida", contrato_versao: "calculo.v1", dataset_id: 9,
                  resultado: { origem_definicao_id: 99, dataset_id: 9 } },
                { id: 30, etapa: "execucao", estado: "concluida", contrato_versao: "calculo.v1", dataset_id: 9,
                  resultado: { origem_definicao_id: 21, dataset_id: 9 } },
                { id: 21, etapa: "definicao", estado: "concluida", contrato_versao: "definir.v1", dataset_id: 9,
                  resultado: { origem_selecao_id: 16, dataset_id: 9 } },
                { id: 16, etapa: "selecao", estado: "concluida", contrato_versao: "selecao.descoberta.v1", dataset_id: 9, versao: 2 }
              ];
              window.__dashboards = [{ id: 7, chave: "painel-fretes", revisao: 1, analise_id: 30 }];
              window.__consulta = {
                ok: true,
                dashboard: { id: 7, analise_id: 30, chave: "painel-fretes", revisao: 1 },
                consulta: {
                  contrato: "dashboard.v1", dashboard_id: 7, revisao: 1, chave: "painel-fretes",
                  calculo_id: 30, definicao_id: 21, dataset_id: 9, versao: 2, filtros: [],
                  graficos: [{
                    ref: "g1", titulo: "indicador", tipo: "indicador", status: "calculado",
                    operacao: "soma", unidade: "valor", resultado: "8", total: "8",
                    serie: [], avisos: [], elegiveis: 1, grupos: 0
                  }]
                }
              };
            }"""
        )
        _scripts(page, "dashboard")
        page.locator("#agente-bi-aprovada").evaluate(
            """(el) => { el.setAttribute("data-selecao", "16"); el.setAttribute("data-versao", "2"); }"""
        )
        page.get_by_role("button", name="Salvar dashboard do cálculo 30").wait_for()
        assert page.get_by_role("button", name="Salvar dashboard do cálculo 40").count() == 0
        assert _posts(page, "/definicao") == 0
        assert _posts(page, "/calculo") == 0
        assert _posts(page, "/montar") == 0
        page.get_by_role("button", name="Abrir revisão 1 de painel-fretes").click()
        page.wait_for_selector("[data-resultado='8']")
        assert page.locator("#agente-bi-dashboard-aberto").is_visible()
        assert page.locator("#agente-bi-descoberta").is_hidden()
        browser.close()


def test_gerar_novo_bi_depende_da_leitura_e_do_reinicio(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    documento = _browser_document(_html(monkeypatch))
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page, segurar=True)
        _scripts(page, "dashboard", "reinicio")
        assert page.locator("#agente-bi-reinicio").is_hidden()
        page.evaluate("() => { window.__datasets = [{ id: 9, versao: 2 }]; window.__soltar(); }")
        page.wait_for_function("() => document.getElementById('agente-bi-reinicio').hidden === false")
        page.close()

        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page)
        page.evaluate("() => { window.__falhar = '/datasets'; window.__analises = [{ id: 4, etapa: 'dados', estado: 'concluida' }]; }")
        _scripts(page, "dashboard")
        page.wait_for_function("() => document.getElementById('agente-bi-reinicio').hidden === false")
        assert "Nenhum dashboard salvo" in page.locator("#agente-bi-dashboard-status").inner_text()
        page.close()

        page2 = browser.new_page(viewport={"width": 390, "height": 844})
        page2.set_content(documento, wait_until="domcontentloaded")
        _instalar(page2)
        page2.evaluate("() => { window.__falhar = 'agente-bi'; }")
        _scripts(page2, "dashboard", "reinicio")
        page2.wait_for_function(
            """() => {
              const status = document.getElementById('agente-bi-dashboard-status');
              return status && status.textContent.indexOf('Não foi possível ler os dashboards salvos') !== -1;
            }"""
        )
        assert page2.locator("#agente-bi-dashboard").is_visible()
        assert page2.locator("#agente-bi-dashboard-erro").is_visible()
        assert "Não foi possível consultar o dashboard." in page2.locator("#agente-bi-dashboard-erro").inner_text()
        assert page2.locator("#agente-bi-dashboard-aberto").is_hidden()
        assert page2.locator("#agente-bi-montagem-progresso").is_hidden()
        assert page2.locator("#agente-bi-reinicio").is_hidden()
        assert "Nenhum dashboard salvo" not in page2.locator("#agente-bi-dashboard-status").inner_text()
        assert _posts(page2, "/definicao") == 0
        assert _posts(page2, "/calculo") == 0
        assert _posts(page2, "/montar") == 0
        page2.evaluate("() => { window.__falhar = ''; }")

        page2.locator("#agente-bi-reinicio").evaluate("el => { el.hidden = false; }")
        page2.locator("#agente-bi-actions-trigger").click()
        page2.locator("#agente-bi-reinicio").click()
        page2.locator("#agente-bi-reinicio-cancelar").click()
        assert page2.locator("#agente-bi-reinicio-dialogo").is_hidden()
        assert page2.evaluate("() => window.__bi.filter((item) => item.url.indexOf('/reinicio') !== -1).length") == 0
        assert page2.locator("#agente-bi-reinicio").evaluate("el => el.hidden") is False

        page2.locator("#agente-bi-actions-trigger").click()
        page2.locator("#agente-bi-reinicio").click()
        page2.evaluate("() => { window.__reinicio = { ok: false, codigo: 'execucao_em_andamento', __status: 409 }; }")
        page2.locator("#agente-bi-reinicio-confirmar").click()
        page2.locator("#agente-bi-reinicio-erro").wait_for(state="visible")
        assert "mantidos" in page2.locator("#agente-bi-reinicio-erro").inner_text()
        assert page2.evaluate("() => window.__recargas") == 0
        assert page2.locator("#agente-bi-reinicio").evaluate("el => el.hidden") is False

        page2.evaluate("() => { window.__reinicio = { ok: true }; }")
        page2.locator("#agente-bi-reinicio-confirmar").click()
        page2.wait_for_function("() => window.__recargas === 1")
        assert page2.locator("#agente-bi-reinicio").is_hidden()
        assert page2.evaluate("() => document.documentElement.getAttribute('data-agente-bi-reiniciado')") == "1"
        browser.close()


def test_falha_de_recuperacao_fica_visivel_sem_montar(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    documento = _browser_document(_html(monkeypatch))
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page)
        page.evaluate(
            """() => {
              window.__dashboardsErro = { ok: false, codigo: "nao_encontrado" };
              window.__analisesErro = { ok: false, codigo: "autenticacao_necessaria" };
            }"""
        )
        _scripts(page, "descoberta", "dashboard")
        page.wait_for_selector("#agente-bi-dashboard-erro:not([hidden])")
        page.wait_for_selector("#agente-bi-descoberta-erro:not([hidden])")
        assert page.locator("#agente-bi-dashboard").is_visible()
        assert page.locator("#agente-bi-descoberta").is_visible()
        assert page.locator("#agente-bi-dashboard-erro").is_visible()
        assert page.locator("#agente-bi-descoberta-erro").is_visible()
        assert "não está disponível nesta conta" in page.locator("#agente-bi-dashboard-erro").inner_text()
        assert "entrar na conta" in page.locator("#agente-bi-descoberta-erro").inner_text()
        assert "Não foi possível ler os dashboards salvos." in page.locator("#agente-bi-dashboard-status").inner_text()
        assert "Não foi possível ler a seleção já gravada." in page.locator("#agente-bi-descoberta-status").inner_text()
        assert "Nenhum dashboard salvo" not in page.locator("#agente-bi-dashboard").inner_text()
        assert "Não há gráfico viável" not in page.locator("#agente-bi-descoberta").inner_text()
        assert page.locator("#agente-bi-descoberta-form").is_hidden()
        assert page.locator("#agente-bi-prosseguir").is_hidden()
        assert page.locator("#agente-bi-dashboard-aberto").is_hidden()
        assert page.locator("#agente-bi-montagem-progresso").is_hidden()
        _sem_execucao(page)
        page.close()

        rede = browser.new_page(viewport={"width": 1280, "height": 900})
        rede.set_content(documento, wait_until="domcontentloaded")
        _instalar(rede)
        rede.evaluate("() => { window.__falhar = 'agente-bi'; }")
        _scripts(rede, "descoberta", "dashboard")
        rede.wait_for_selector("#agente-bi-dashboard-erro:not([hidden])")
        rede.wait_for_selector("#agente-bi-descoberta-erro:not([hidden])")
        assert rede.locator("#agente-bi-dashboard").is_visible()
        assert rede.locator("#agente-bi-descoberta").is_visible()
        assert "Não foi possível consultar o dashboard." in rede.locator("#agente-bi-dashboard-erro").inner_text()
        assert "Não foi possível ler a seleção já gravada." in rede.locator("#agente-bi-descoberta-erro").inner_text()
        assert "Nenhum dashboard salvo" not in rede.locator("#agente-bi-dashboard").inner_text()
        assert rede.locator("#agente-bi-descoberta-form").is_hidden()
        assert rede.locator("#agente-bi-montagem-progresso").is_hidden()
        assert rede.locator("#agente-bi-dashboard-aberto").is_hidden()
        _sem_execucao(rede)
        browser.close()


def test_s2_sem_definicao_nao_oferece_calculo_de_s1(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    documento = _browser_document(_html(monkeypatch))
    selecoes = [
        _registro(11, "selecao", "selecao.descoberta.v1"),
        _registro(12, "selecao", "selecao.descoberta.v1"),
    ]
    definicao_s1 = _registro(21, "definicao", "definir.v1", 11, "origem_selecao_id")
    calculo_s1 = _registro(31, "execucao", "calculo.v1", 21, "origem_definicao_id")
    calculo_maior = _registro(99, "execucao", "calculo.v1", 99, "origem_definicao_id")
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page)
        page.evaluate(
            "(analises) => { window.__analises = analises; }",
            selecoes + [definicao_s1, calculo_s1, calculo_maior],
        )
        _marcar_aprovada(page, 11, "1")
        _scripts(page, "dashboard")
        _esperar_origens(
            page,
            ["Salvar dashboard do cálculo 31", "Calcular e salvar dashboard", "Montar dashboard da seleção"],
            ["cálculo 99"],
        )
        _sem_execucao(page)

        page.evaluate(
            "(analises) => { window.__analises = analises; }",
            selecoes + [definicao_s1, calculo_s1],
        )
        _marcar_aprovada(page, 12, "2")
        _esperar_origens(page, ["Montar dashboard da seleção"], ["cálculo", "Calcular e salvar"])
        _sem_execucao(page)

        definicao_s2 = _registro(22, "definicao", "definir.v1", 12, "origem_selecao_id")
        calculo_s2 = _registro(32, "execucao", "calculo.v1", 22, "origem_definicao_id")
        page.evaluate(
            "(analises) => { window.__analises = analises; }",
            selecoes + [definicao_s1, definicao_s2, calculo_s1, calculo_maior, calculo_s2],
        )
        _marcar_aprovada(page, 12, "3")
        _esperar_origens(
            page,
            ["Salvar dashboard do cálculo 32", "Calcular e salvar dashboard", "Montar dashboard da seleção"],
            ["cálculo 31", "cálculo 99"],
        )
        _sem_execucao(page)

        ambiguo = [
            _registro(11, "selecao", "selecao.descoberta.v1"),
            _registro(12, "selecao", "selecao.descoberta.v1"),
            _registro(21, "definicao", "definir.v1", 11, "origem_selecao_id"),
            _registro(22, "definicao", "definir.v1", 12, "origem_selecao_id"),
            _registro(31, "execucao", "calculo.v1", 21, "origem_definicao_id"),
            _registro(32, "execucao", "calculo.v1", 22, "origem_definicao_id"),
            _registro(80, "execucao", "calculo.v1", 22, "origem_definicao_id"),
        ]
        page.evaluate("(analises) => { window.__analises = analises; }", ambiguo)
        _marcar_aprovada(page, 12, "4")
        _esperar_origens(
            page,
            ["Calcular e salvar dashboard", "Montar dashboard da seleção"],
            ["Salvar dashboard do cálculo"],
        )
        assert page.get_by_role("button", name="Salvar dashboard do cálculo 80").count() == 0
        assert page.get_by_role("button", name="Salvar dashboard do cálculo 32").count() == 0
        _sem_execucao(page)
        browser.close()


def test_nova_preparacao_limpa_data_selecao(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    documento = _browser_document(_html(monkeypatch))
    arquivo = {"name": "fretes.csv", "mimeType": "text/csv", "buffer": b"alfa,valor\nA,10\n"}
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        _instalar(page)
        page.evaluate("(corpo) => { window.__preparacao = corpo; }", _preparacao())
        page.evaluate("(corpo) => { window.__descoberta = corpo; }", _descoberta([_sugestao(1)]))
        page.evaluate(
            """() => {
              window.__selecao = { ok: true, selecao: { id: 16, versao: 2, graficos: 1 } };
              window.__analises = [{
                id: 16, etapa: "selecao", estado: "concluida", contrato_versao: "selecao.descoberta.v1"
              }];
            }"""
        )
        _scripts(page, "descoberta", "dashboard", "reinicio")
        page.locator("#agente-bi-arquivo").set_input_files(files=[arquivo])
        page.wait_for_selector("#agente-bi-prosseguir:not([hidden])")
        page.locator("#agente-bi-prosseguir").click()
        page.locator("#agente-bi-examinar").click()
        page.wait_for_function("() => document.querySelectorAll('.agente-bi-grafico').length === 1")
        page.locator(".agente-bi-grafico").check()
        page.locator("#agente-bi-confirmacao").check()
        page.locator("#agente-bi-aprovar").click()
        page.wait_for_selector("#agente-bi-aprovada:not([hidden])")
        assert page.locator("#agente-bi-aprovada").get_attribute("data-selecao") == "16"

        page.evaluate(
            """() => {
              window.__selecao = { ok: true, selecao: { id: 18, versao: 3, graficos: 1 } };
              window.__analises = [{
                id: 18, etapa: "selecao", estado: "concluida", contrato_versao: "selecao.descoberta.v1"
              }];
            }"""
        )
        page.locator("#agente-bi-arquivo").set_input_files(files=[arquivo])
        page.wait_for_selector("#agente-bi-prosseguir:not([hidden])")
        assert page.locator("#agente-bi-aprovada").get_attribute("data-selecao") is None
        assert page.locator("#agente-bi-aprovada").get_attribute("data-versao") is None
        assert page.locator("#agente-bi-aprovada").get_attribute("data-graficos") is None

        page.locator("#agente-bi-prosseguir").click()
        page.locator("#agente-bi-examinar").click()
        page.wait_for_function("() => document.querySelectorAll('.agente-bi-grafico').length === 1")
        page.locator(".agente-bi-grafico").check()
        page.locator("#agente-bi-confirmacao").check()
        page.locator("#agente-bi-aprovar").click()
        page.wait_for_function(
            "() => document.getElementById('agente-bi-aprovada').getAttribute('data-selecao') === '18'"
        )

        page.wait_for_function("() => document.getElementById('agente-bi-reinicio').hidden === false")
        page.evaluate(
            """() => {
              const arquivo = document.getElementById("agente-bi-arquivo");
              arquivo.click = () => { window.__abriuArquivo = 1; };
            }"""
        )
        page.locator("#agente-bi-actions-trigger").click()
        assert page.locator("#agente-bi-upload-item").is_visible()
        page.locator("#agente-bi-upload-item").click()
        assert page.evaluate("() => window.__abriuArquivo") == 1
        if page.locator("#agente-bi-actions-menu").is_hidden():
            page.locator("#agente-bi-actions-trigger").click()
        page.locator("#agente-bi-reinicio").click()
        page.locator("#agente-bi-reinicio-dialogo").wait_for(state="visible")
        page.locator("#agente-bi-reinicio-cancelar").click()
        assert page.locator("#agente-bi-reinicio-dialogo").is_hidden()
        assert page.evaluate("() => window.__bi.filter((item) => item.url.indexOf('/reinicio') !== -1).length") == 0
        browser.close()
