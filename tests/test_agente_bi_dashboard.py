"""SCRUM-262: dashboard persistente, reabertura sem IA e filtro determinístico."""
from __future__ import annotations

import copy
import json
import re
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiDashboard, IaChamadaTentativa, IaConsumoEvento
from app.services.agente_bi_calculo_service import CONTRATO_CALCULO
from app.services.agente_bi_dashboard_service import (
    consultar_dashboard,
    filtrar_dashboard,
    montar_dashboard,
)
from app.services.agente_bi_definir_service import definir, definir_selecao
from app.services.agente_bi_persistencia_service import AgenteBiErro, criar_analise, escopo_do_usuario, obter_analise
from tests.conftest import seed_usuario
from tests.test_agente_bi_calculo import (
    _calcular,
    _coluna,
    _filtrar,
    _gravar,
    _indicador,
    _jornada,
    _jornada_de,
    _por_operacao,
    _ref,
)
from tests.test_agente_bi_ingestao import _cliente as _cliente_ingestao
from tests.test_agente_bi_ingestao import _entrar, _par, _xlsx
from tests.test_agente_bi_page import _client, _launch_browser

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _sem_anthropic(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.chamar_anthropic_governado",
        proibido,
    )


def _pronto(email, slug):
    usuario, franquia, escopo, _perfil, pacote, _aprovada = _jornada(email, slug)
    definicao = definir_selecao(escopo, {"analise_id": pacote["id"]})
    calculo = _calcular(escopo, definicao["definicao"]["id"])
    return usuario, franquia, escopo, definicao, calculo


def _painel_soma(graficos):
    return next(
        item
        for item in graficos
        if item["operacao"] == "soma" and item["tipo"] == "indicador" and item["status"] == "calculado"
    )


def _bate(grafico, indicador):
    assert grafico["status"] == indicador["status"]
    assert grafico["tipo"] == indicador["grafico"]
    assert grafico["resultado"] == indicador["resultado"]
    assert grafico["total"] == indicador["total"]
    assert grafico["unidade"] == indicador["unidade"]
    assert grafico["avisos"] == indicador["avisos"]
    assert grafico["elegiveis"] == indicador["elegiveis"]
    assert grafico["serie"] == [
        {"chave": item["chave"], "resultado": item["resultado"], "elegiveis": item["elegiveis"]}
        for item in indicador["serie"]
    ]


def _sintetico(escopo, calculo, quantidade):
    origem = obter_analise(escopo, calculo["calculo"]["id"])
    memoria = copy.deepcopy(calculo["calculo"]["resultado"])
    base = _por_operacao(memoria, "soma")
    indicadores = []
    for indice in range(1, quantidade + 1):
        item = copy.deepcopy(base)
        item["id"] = f"g{indice}"
        item["definicao_id"] = f"g{indice}"
        item["grafico"] = "indicador"
        item["operacao"] = "soma"
        item["status"] = "calculado"
        item["motivo"] = None
        item["resultado"] = "1"
        item["total"] = "1"
        item["serie"] = []
        item["avisos"] = []
        item["filtros"] = []
        item["grupos"] = 0
        item["elegiveis"] = 1
        indicadores.append(item)
    memoria["indicadores"] = indicadores
    rodada = (
        db.session.query(func.max(AgenteBiAnalise.rodada))
        .filter_by(
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
            chave=origem["chave"],
        )
        .scalar()
    )
    return criar_analise(
        escopo,
        {
            "chave": origem["chave"],
            "rodada": int(rodada) + 1,
            "dataset_id": origem["dataset_id"],
            "versao": origem["versao"],
            "impressao": origem["impressao"],
            "etapa": "execucao",
            "estado": "concluida",
            "contexto": {
                "contrato": "contexto.calculo.v1",
                "definicao_id": memoria["origem_definicao_id"],
                "dataset_id": origem["dataset_id"],
                "versao": origem["versao"],
                "impressao": origem["impressao"],
            },
            "regras_negocio": {"executor": "executor.deterministico.v1", "ia_calculo": False},
            "esclarecimentos": {"bloqueios": []},
            "contrato_versao": CONTRATO_CALCULO,
            "resultado": memoria,
        },
    )


def test_cria_ate_seis_graficos_e_rejeita_o_setimo(ctx):
    _usuario, _franquia, escopo, _definicao, calculo = _pronto("dashboard.seis@test.com", "dashboard-seis")
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    graficos = painel["consulta"]["graficos"]
    assert 1 <= len(graficos) <= 6
    assert painel["dashboard"]["revisao"] == 1
    assert painel["consulta"]["chamada_anthropic"] is False
    assert painel["consulta"]["ia_calculo"] is False

    seis = _sintetico(escopo, calculo, 6)
    salvo = montar_dashboard(escopo, {"analise_id": seis["id"]})
    assert len(salvo["consulta"]["graficos"]) == 6
    assert [item["resultado"] for item in salvo["consulta"]["graficos"]] == ["1"] * 6
    quantidade = AgenteBiDashboard.query.count()
    with pytest.raises(AgenteBiErro) as erro:
        montar_dashboard(escopo, {"analise_id": _sintetico(escopo, calculo, 7)["id"]})
    assert erro.value.codigo == "limite_graficos"
    assert erro.value.status == 422
    assert AgenteBiDashboard.query.count() == quantidade


def test_grafico_tabela_e_memoria_coincidem(ctx):
    _usuario, _franquia, escopo, _definicao, calculo = _pronto("dashboard.memoria@test.com", "dashboard-memoria")
    memoria = calculo["calculo"]["resultado"]
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    assert len(painel["consulta"]["graficos"]) == len(memoria["indicadores"])
    for grafico, indicador in zip(painel["consulta"]["graficos"], memoria["indicadores"]):
        _bate(grafico, indicador)
    soma = _por_operacao(memoria, "soma")
    assert soma["resultado"] == "25.25"
    assert _painel_soma(painel["consulta"]["graficos"])["resultado"] == "25.25"


def test_persiste_apos_sessao_e_reabre_sem_recalcular_nem_anthropic(ctx, monkeypatch):
    _usuario, _franquia, escopo, _definicao, calculo = _pronto("dashboard.sessao@test.com", "dashboard-sessao")
    antes_analises = AgenteBiAnalise.query.count()
    antes_tentativas = IaChamadaTentativa.query.count()
    antes_consumo = IaConsumoEvento.query.count()
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    identificador = painel["dashboard"]["id"]
    esperado = painel["consulta"]["graficos"]

    def proibido(*_args, **_kwargs):
        raise AssertionError("recalculo")

    monkeypatch.setattr("app.services.agente_bi_dashboard_service.calcular_definicao", proibido)
    monkeypatch.setattr("app.services.agente_bi_persistencia_service.obter_dataset", proibido)
    db.session.remove()
    reaberto = consultar_dashboard(escopo, identificador)
    assert reaberto["consulta"]["graficos"] == esperado
    assert reaberto["consulta"]["calculo_id"] == calculo["calculo"]["id"]
    assert reaberto["consulta"]["chamada_anthropic"] is False
    assert AgenteBiAnalise.query.count() == antes_analises
    assert AgenteBiAnalise.query.filter_by(id=calculo["calculo"]["id"]).count() == 1
    assert IaChamadaTentativa.query.count() == antes_tentativas
    assert IaConsumoEvento.query.count() == antes_consumo


def test_filtro_deterministico_bate_com_a_memoria_e_nao_grava_o_valor(ctx):
    _usuario, _franquia, escopo, perfil, pacote, _aprovada = _jornada(
        "dashboard.filtro@test.com",
        "dashboard-filtro",
    )
    beta = _coluna(perfil, indice=1, aba=0)
    valor = _coluna(perfil, dimensao_principal="valor", aba=0)
    pacote_filtro = _indicador(pacote, "soma", leitura="indicador_de_medida")
    pacote_filtro["graficos"][0]["medida"] = _ref(valor)
    documento = definir(perfil, pacote_filtro, provedor=_filtrar(beta, "igual", "faixa", "texto"))
    salva = _gravar(escopo, pacote["id"], documento)
    base = _calcular(escopo, salva["id"], parametros={"faixa": "sul"})
    painel = montar_dashboard(escopo, {"analise_id": base["calculo"]["id"]})
    assert painel["consulta"]["graficos"][0]["resultado"] == "4.25"
    antes = AgenteBiAnalise.query.count()
    filtrado = filtrar_dashboard(
        escopo,
        painel["dashboard"]["id"],
        {"parametros": {"faixa": "norte"}},
    )
    assert filtrado["persistido"] is False
    assert filtrado["dashboard"]["id"] == painel["dashboard"]["id"]
    assert filtrado["consulta"]["graficos"][0]["resultado"] == "21"
    assert filtrado["consulta"]["graficos"][0]["elegiveis"] == 2
    memoria = obter_analise(escopo, filtrado["consulta"]["calculo_id"])["resultado"]
    _bate(filtrado["consulta"]["graficos"][0], memoria["indicadores"][0])
    assert AgenteBiAnalise.query.count() == antes + 1
    assert "norte" not in db.session.get(AgenteBiDashboard, painel["dashboard"]["id"]).definicoes_json
    assert "sul" not in db.session.get(AgenteBiDashboard, painel["dashboard"]["id"]).definicoes_json
    original = consultar_dashboard(escopo, painel["dashboard"]["id"])
    assert original["consulta"]["graficos"][0]["resultado"] == "4.25"

    salvo = filtrar_dashboard(
        escopo,
        painel["dashboard"]["id"],
        {"parametros": {"faixa": "norte"}, "persistir": True},
    )
    assert salvo["persistido"] is True
    assert salvo["dashboard"]["revisao"] == 2
    assert salvo["consulta"]["graficos"][0]["resultado"] == "21"
    assert "norte" not in db.session.get(AgenteBiDashboard, salvo["dashboard"]["id"]).definicoes_json
    db.session.remove()
    reaberto = consultar_dashboard(escopo, salvo["dashboard"]["id"])
    assert reaberto["consulta"]["graficos"][0]["resultado"] == "21"
    assert reaberto["consulta"]["calculo_id"] == salvo["consulta"]["calculo_id"]
    assert IaChamadaTentativa.query.count() == 0
    assert IaConsumoEvento.query.count() == 0


def test_isolamento_entre_usuario_e_conta(ctx):
    usuario, franquia, escopo, _definicao, calculo = _pronto("dashboard.dono@test.com", "dashboard-dono")
    _outra, _outra_franquia, outro = _par("dashboard-outro", "dashboard.outro@test.com")
    colega = seed_usuario(franquia.id, usuario.conta_id, email="dashboard.colega@test.com")
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    identificador = painel["dashboard"]["id"]
    for intruso in (escopo_do_usuario(outro), escopo_do_usuario(colega)):
        with pytest.raises(AgenteBiErro) as leitura:
            consultar_dashboard(intruso, identificador)
        assert leitura.value.status == 404
        with pytest.raises(AgenteBiErro) as filtro:
            filtrar_dashboard(intruso, identificador, {"parametros": {}})
        assert filtro.value.status == 404
        with pytest.raises(AgenteBiErro) as montagem:
            montar_dashboard(intruso, {"analise_id": calculo["calculo"]["id"]})
        assert montagem.value.status == 404


def test_definicao_bloqueada_nao_fabrica_numero(ctx):
    _usuario, _franquia, escopo, perfil, pacote = _jornada_de(
        "dashboard.bloqueio@test.com",
        "dashboard-bloqueio",
        _xlsx(
            {
                "movimento": [
                    ["alfa", "quando", "valor"],
                    ["norte", date(2024, 1, 2), 10.5],
                    ["sul", "01/02/2024", 4.25],
                    ["norte", "02/01/2024", 10.5],
                ]
            }
        ),
    )
    quando = _coluna(perfil, indice=1, aba=0)
    valor = _coluna(perfil, indice=2, aba=0)
    pacote_data = _indicador(
        pacote,
        "soma",
        leitura="medida_por_categoria",
        agrupamento=_ref(quando),
        grafico="barra",
    )
    pacote_data["graficos"][0]["medida"] = _ref(valor)
    documento = definir(perfil, pacote_data)
    assert documento["definicoes"][0]["status"] == "bloqueada"
    salva = _gravar(escopo, pacote["id"], documento)
    calculo = _calcular(escopo, salva["id"])
    painel = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    grafico = painel["consulta"]["graficos"][0]
    assert grafico["status"] == "bloqueado"
    assert grafico["resultado"] is None
    assert grafico["total"] is None
    assert grafico["serie"] == []
    bruto = json.dumps(grafico)
    assert "10.5" not in bruto
    assert "4.25" not in bruto
    assert "2024-01-02" not in bruto


def test_api_reabre_depois_da_sessao_sem_credito(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)
    with app.app_context():
        _conta, _franquia, usuario = _par("dashboard-api", "dashboard.api@test.com")
        usuario_id = usuario.id
        antes_tentativas = IaChamadaTentativa.query.count()
        antes_consumo = IaConsumoEvento.query.count()
    assert client.post("/api/agente-bi/dashboards/montar", json={"analise_id": 1}).status_code == 401
    _entrar(client, usuario_id)
    from tests.test_agente_bi_descoberta import _aprovar, _descobrir, _postar, _viaveis
    from tests.test_agente_bi_analisar import _movimento

    preparacao = _postar(client, _movimento(), "movimento.xlsx")
    assert preparacao.status_code == 201
    dataset_id = preparacao.get_json()["dataset"]["id"]
    descoberta = _descobrir(client, dataset_id, {"modalidades": ["A"]})
    aprovacao = _aprovar(
        client,
        descoberta["analise_id"],
        _viaveis(descoberta["resultado"])[:6],
        descoberta["resultado"],
    )
    definicao = client.post("/api/agente-bi/definicao", json={"analise_id": aprovacao.get_json()["selecao"]["id"]})
    assert definicao.status_code == 201
    calculo = client.post("/api/agente-bi/calculo", json={"analise_id": definicao.get_json()["definicao"]["id"]})
    assert calculo.status_code == 201
    montado = client.post("/api/agente-bi/dashboards/montar", json={"analise_id": calculo.get_json()["calculo"]["id"]})
    assert montado.status_code == 201, montado.get_data(as_text=True)
    corpo = montado.get_json()
    identificador = corpo["dashboard"]["id"]
    soma = _painel_soma(corpo["consulta"]["graficos"])
    assert soma["resultado"] == "25.25"
    assert len(corpo["consulta"]["graficos"]) <= 6

    with client.session_transaction() as sess:
        sess.clear()
    assert client.get(f"/api/agente-bi/dashboards/{identificador}/consulta").status_code == 401
    _entrar(client, usuario_id)
    reaberto = client.get(f"/api/agente-bi/dashboards/{identificador}/consulta")
    assert reaberto.status_code == 200
    assert _painel_soma(reaberto.get_json()["consulta"]["graficos"])["resultado"] == "25.25"
    with app.app_context():
        assert IaChamadaTentativa.query.count() == antes_tentativas
        assert IaConsumoEvento.query.count() == antes_consumo


def test_fontes_nao_chamam_modelo_nem_executam_codigo():
    servico = (ROOT / "app" / "services" / "agente_bi_dashboard_service.py").read_text(encoding="utf-8")
    script = (ROOT / "app" / "static" / "js" / "agente_bi_dashboard.js").read_text(encoding="utf-8")
    assert "chamar_anthropic" not in servico
    assert "chamar_anthropic" not in script
    assert "eval(" not in script
    assert "exec(" not in servico
    assert "innerHTML" not in script


def test_pagina_so_carrega_chartjs_com_api_e_sem_canvas_estatico(monkeypatch):
    web = _client(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert 'id="agente-bi-dashboard"' in html
    assert "chart.js@4.4.1" in html
    assert "agente_bi_dashboard.js" in html
    assert "<canvas" not in html
    assert 'type="file"' not in html
    assert "Carregando dashboards salvos." in html

    visitante = _client(monkeypatch, authenticated=False)
    html_visitante = visitante.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert 'id="agente-bi-dashboard"' not in html_visitante
    assert "chart.js" not in html_visitante
    assert "<canvas" not in html_visitante


def _documento_browser(html):
    css = (ROOT / "app" / "static" / "css" / "agente_bi.css").read_text(encoding="utf-8")
    tema = (ROOT / "app" / "static" / "css" / "agentefrete-theme.css").read_text(encoding="utf-8")
    menu = (ROOT / "app" / "static" / "js" / "agente_bi.js").read_text(encoding="utf-8")
    script = (ROOT / "app" / "static" / "js" / "agente_bi_dashboard.js").read_text(encoding="utf-8")
    html = re.sub(r"<link\b[^>]*>", "", html, flags=re.I)
    html = re.sub(r"<script\b[^>]*\bsrc\s*=[^>]*>\s*</script>", "", html, flags=re.I)
    html = html.replace("</head>", f"<style>{tema}\n{css}</style></head>", 1)
    html = html.replace(
        "<head>",
        """<head><script>
        window.Chart = function (canvas, config) {
          canvas.setAttribute("data-chart-type", config.type);
          this.destroy = function () {};
        };
        window.__evolucaoPosts = [];
        window.fetch = function (input, opcoes) {
          var url = String(input);
          var metodo = (opcoes && opcoes.method) || "GET";
          var corpo;
          if (metodo === "POST" && url.indexOf("/evolucao") !== -1) {
            var pedido = JSON.parse((opcoes && opcoes.body) || "{}");
            window.__evolucaoPosts.push(pedido);
            if (pedido.texto && pedido.texto.indexOf("média") !== -1 && !pedido.parametros) {
              corpo = {
                ok: true,
                situacao: "esclarecimento",
                esclarecimento: {
                  motivo: "informar_parametro",
                  pergunta: "Ao recarregar a página, informe esses valores outra vez."
                },
                dashboard: { id: 7, analise_id: 3, chave: "painel-fretes", revisao: 1 },
                consulta: consultaAberta()
              };
            } else if (pedido.parametros) {
              var media = consultaFiltrada();
              media.graficos[0].operacao = "media";
              corpo = {
                ok: true,
                situacao: "aplicada",
                parametros: pedido.parametros,
                dashboard: { id: 9, analise_id: 11, chave: "painel-fretes", revisao: 3 },
                consulta: media
              };
            } else {
              corpo = {
                ok: true,
                situacao: "aplicada",
                parametros: { faixa: "sul" },
                dashboard: { id: 8, analise_id: 10, chave: "painel-fretes", revisao: 2 },
                consulta: consultaFiltrada()
              };
            }
          } else if (metodo === "POST" && url.indexOf("/filtros") !== -1) {
            corpo = {
              ok: true,
              persistido: false,
              dashboard: { id: 7, analise_id: 3, chave: "painel-fretes", revisao: 1 },
              consulta: consultaFiltrada()
            };
          } else if (url.indexOf("/consulta") !== -1) {
            corpo = {
              ok: true,
              persistido: true,
              dashboard: { id: 7, analise_id: 3, chave: "painel-fretes", revisao: 1 },
              consulta: consultaAberta()
            };
          } else if (url.indexOf("/analises") !== -1) {
            corpo = { ok: true, analises: [] };
          } else {
            corpo = { ok: true, dashboards: [{ id: 7, chave: "painel-fretes", revisao: 1, analise_id: 3 }] };
          }
          return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(corpo); } });
          function consultaAberta() {
            return {
              contrato: "dashboard.v1",
              chamada_anthropic: false,
              ia_calculo: false,
              dashboard_id: 7,
              revisao: 1,
              chave: "painel-fretes",
              calculo_id: 3,
              definicao_id: 2,
              dataset_id: 4,
              versao: 1,
              impressao: "a",
              filtros: [{ nome: "faixa", tipo: "texto", obrigatorio: true, operador: "igual", indicador: "g1" }],
              graficos: [
                { ref: "g1", titulo: "indicador-soma", tipo: "indicador", status: "calculado", motivo: null, operacao: "soma", unidade: "valor", resultado: "25.25", total: "25.25", serie: [], avisos: [], elegiveis: 3, grupos: 0 },
                { ref: "g2", titulo: "barra-media", tipo: "barra", status: "calculado", motivo: null, operacao: "media", unidade: "valor", resultado: null, total: "21", serie: [{ chave: "a", resultado: "10.5", elegiveis: 1 }, { chave: "b", resultado: "31.5", elegiveis: 1 }], avisos: ["ha_exclusoes"], elegiveis: 2, grupos: 2 },
                { ref: "g3", titulo: "tabela-soma", tipo: "tabela", status: "calculado", motivo: null, operacao: "soma", unidade: "quantidade", resultado: null, total: "14", serie: [{ chave: "norte", resultado: "10", elegiveis: 2 }, { chave: "sul", resultado: "4", elegiveis: 1 }], avisos: [], elegiveis: 3, grupos: 2 },
                { ref: "g4", titulo: "dispersao-par_numerico", tipo: "dispersao", status: "calculado", motivo: null, operacao: "par_numerico", unidade: "contagem", resultado: "2", total: "2", serie: [], avisos: ["pares_completos"], elegiveis: 2, grupos: 0 },
                { ref: "g5", titulo: "barra-soma", tipo: "barra", status: "bloqueado", motivo: "limite_volume", operacao: "soma", unidade: "valor", resultado: null, total: null, serie: [], avisos: [], elegiveis: 0, grupos: 0 }
              ]
            };
          }
          function consultaFiltrada() {
            var consulta = consultaAberta();
            consulta.calculo_id = 9;
            consulta.graficos[0].resultado = "4.25";
            consulta.graficos[0].total = "4.25";
            consulta.graficos[0].elegiveis = 1;
            return consulta;
          }
        };
        </script>""",
        1,
    )
    html = html.replace("</body>", f"<script>{menu}\n{script}</script></body>", 1)
    return html


def test_layout_mobile_tema_e_filtro_no_navegador(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    web = _client(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    renderizado = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    documento = _documento_browser(renderizado)
    erros = []

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        pagina = browser.new_page(viewport={"width": 1280, "height": 900})
        pagina.on("pageerror", lambda erro: erros.append(str(erro)))
        pagina.set_content(documento, wait_until="domcontentloaded")
        pagina.get_by_role("button", name="Abrir revisão 1 de painel-fretes").click()
        pagina.wait_for_selector("[data-resultado='25.25']")
        assert pagina.locator("[data-ref='g1'] [data-resultado]").inner_text() == "25.25"
        assert "Unidade: valor" in pagina.locator("[data-ref='g1']").inner_text()
        assert "Total do cálculo: 21" in pagina.locator("[data-ref='g2']").inner_text()
        assert "10.5" in pagina.locator("[data-ref='g2'] table").inner_text()
        assert "31.5" in pagina.locator("[data-ref='g2'] table").inner_text()
        assert "42" not in pagina.locator("[data-ref='g2']").inner_text()
        assert pagina.locator("[data-ref='g3'] table").inner_text().count("10") >= 1
        assert pagina.locator("[data-ref='g3'] canvas").count() == 0
        assert pagina.locator("[data-ref='g4'] canvas").get_attribute("data-chart-type") == "scatter"
        assert pagina.locator("[data-ref='g4'] [data-resultado]").inner_text() == "2"
        assert "pares_completos" in pagina.locator("[data-ref='g4']").inner_text()
        assert "limite_volume" in pagina.locator("[data-ref='g5']").inner_text()
        assert pagina.locator("[data-ref='g5'] canvas").count() == 0
        assert pagina.locator("[data-ref='g5'] [data-resultado]").count() == 0
        assert pagina.locator("[data-ref='g1'] canvas").get_attribute("data-chart-type") == "bar"
        assert pagina.locator("[data-ref='g2'] canvas").get_attribute("data-chart-type") == "bar"

        colunas_largas = pagina.evaluate(
            """() => getComputedStyle(document.getElementById('agente-bi-dashboard-grade')).gridTemplateColumns.split(' ').filter(Boolean).length"""
        )
        assert colunas_largas == 2
        primeiro = pagina.locator("[data-ref='g1']").bounding_box()
        segundo = pagina.locator("[data-ref='g2']").bounding_box()
        assert primeiro["x"] + primeiro["width"] <= segundo["x"] + 2
        assert abs(primeiro["y"] - segundo["y"]) < 8

        pagina.locator("input[name='faixa']").fill("sul")
        pagina.get_by_role("button", name="Aplicar filtros").click()
        pagina.wait_for_selector("[data-resultado='4.25']")
        assert pagina.locator("[data-ref='g1'] [data-resultado]").inner_text() == "4.25"
        assert pagina.get_by_role("button", name="Salvar consulta filtrada").is_visible()

        escuro = pagina.locator("[data-ref='g1']").evaluate("el => getComputedStyle(el).backgroundColor")
        cor_escura = pagina.locator("[data-ref='g1'] canvas").get_attribute("data-cor")
        pagina.evaluate("() => document.documentElement.setAttribute('data-theme', 'light')")
        pagina.wait_for_function(
            "() => getComputedStyle(document.querySelector('[data-ref=g1]')).backgroundColor === 'rgb(255, 255, 255)'"
        )
        claro = pagina.locator("[data-ref='g1']").evaluate("el => getComputedStyle(el).backgroundColor")
        cor_clara = pagina.locator("[data-ref='g1'] canvas").get_attribute("data-cor")
        assert escuro != claro
        assert cor_escura != cor_clara
        assert "00BFFF" in cor_escura.upper()
        assert "0077CC" in cor_clara.upper()

        pagina.set_viewport_size({"width": 390, "height": 844})
        colunas = pagina.evaluate(
            """() => getComputedStyle(document.getElementById('agente-bi-dashboard-grade')).gridTemplateColumns.split(' ').filter(Boolean).length"""
        )
        assert colunas == 1
        movel_1 = pagina.locator("[data-ref='g1']").bounding_box()
        movel_2 = pagina.locator("[data-ref='g2']").bounding_box()
        assert movel_1["width"] <= 390
        assert movel_1["x"] >= 0
        assert movel_2["x"] < movel_1["x"] + 40
        assert movel_2["y"] >= movel_1["y"] + 40
        cabe = pagina.evaluate(
            """() => {
              const el = document.getElementById('agente-bi-dashboard');
              const grade = document.getElementById('agente-bi-dashboard-grade');
              return {
                painel: el.scrollWidth - el.clientWidth,
                grade: grade.scrollWidth - grade.clientWidth,
                pagina: document.documentElement.scrollWidth - window.innerWidth
              };
            }"""
        )
        assert cabe["painel"] <= 1, cabe
        assert cabe["grade"] <= 1, cabe
        assert cabe["pagina"] <= 1, cabe
        assert pagina.get_by_role("button", name="Aplicar filtros").bounding_box()["height"] >= 44
        assert pagina.locator("#agente-bi-dashboard-atualizacao input[type='file']").count() == 0
        pagina.evaluate(
            "() => document.getElementById('agente-bi-dashboard').setAttribute('data-atualizacao', '1')"
        )
        pagina.get_by_role("button", name="Abrir revisão 1 de painel-fretes").click()
        pagina.wait_for_selector("#agente-bi-dashboard-atualizacao input[type='file']")
        assert pagina.locator("#agente-bi-dashboard-atualizacao select[name='modo']").locator("option").count() == 2
        assert pagina.get_by_role("button", name="Atualizar dados").bounding_box()["height"] >= 44
        relevantes = [erro for erro in erros if "$ is not defined" not in erro]
        assert relevantes == []
        browser.close()


def test_evolucao_guarda_parametros_so_na_memoria_da_pagina(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    web = _client(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    documento = _documento_browser(web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True))
    erros = []

    def abrir(pagina):
        pagina.set_content(documento, wait_until="domcontentloaded")
        pagina.get_by_role("button", name="Abrir revisão 1 de painel-fretes").click()
        pagina.wait_for_selector("[data-resultado='25.25']")

    def pedir(pagina, texto):
        pagina.get_by_role("textbox", name="Pedido para alterar o dashboard").fill(texto)
        pagina.get_by_role("button", name="Aplicar pedido").click()

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        pagina = browser.new_page(viewport={"width": 1280, "height": 900})
        pagina.on("pageerror", lambda erro: erros.append(str(erro)))
        abrir(pagina)
        pedir(pagina, "filtre beta igual a sul")
        pagina.wait_for_selector("[data-resultado='4.25']")
        pedir(pagina, "troque para média")
        pagina.wait_for_function("() => window.__evolucaoPosts.length === 2")
        pedidos = pagina.evaluate("() => window.__evolucaoPosts")
        assert pedidos[0] == {"texto": "filtre beta igual a sul"}
        assert pedidos[1]["texto"] == "troque para média"
        assert pedidos[1]["parametros"] == {"faixa": "sul"}
        armazenamento = pagina.evaluate(
            """() => {
              var local = 0;
              var sessao = 0;
              var cookie = "";
              try { local = localStorage.length; } catch (e) { local = 0; }
              try { sessao = sessionStorage.length; } catch (e) { sessao = 0; }
              try { cookie = document.cookie || ""; } catch (e) { cookie = ""; }
              return { local: local, sessao: sessao, cookie: cookie };
            }"""
        )
        assert armazenamento["local"] == 0
        assert armazenamento["sessao"] == 0
        assert "sul" not in armazenamento["cookie"]

        abrir(pagina)
        pedir(pagina, "troque para média")
        pagina.wait_for_selector("#agente-bi-dashboard-evolucao-resposta")
        assert "recarregar" in pagina.locator("#agente-bi-dashboard-evolucao-resposta").inner_text()
        recarga = pagina.evaluate("() => window.__evolucaoPosts")
        assert recarga == [{"texto": "troque para média"}]
        assert "parametros" not in recarga[0]
        relevantes = [erro for erro in erros if "$ is not defined" not in erro]
        assert relevantes == []
        browser.close()
