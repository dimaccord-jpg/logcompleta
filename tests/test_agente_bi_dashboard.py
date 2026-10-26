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
    consultar_detalhe_grafico,
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
from tests.test_agente_bi_page import _client, _launch_browser, _user

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
    assert "card.appendChild(tabela(grafico))" not in script
    assert "Os valores exatos estão na tabela." not in script
    assert "Os valores exatos estão em Ver dados." in script
    assert 'ancora.rel = "noopener noreferrer"' in script
    assert 'ancora.target = "_blank"' in script
    trecho = script[script.index("function ligacaoDados"):script.index("function desenharGraficos")]
    assert "impressao" not in trecho
    assert "parametros" not in trecho


def test_pagina_so_carrega_chartjs_com_api_e_sem_canvas_estatico(monkeypatch):
    web = _client(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert 'id="agente-bi-dashboard"' in html
    assert 'data-detalhe-base="/cleide-bi-frete/dashboards"' in html
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
        assert pagina.locator("#agente-bi-dashboard-grade table").count() == 0
        assert "10.5" not in pagina.locator("#agente-bi-dashboard-grade").inner_text()
        assert "31.5" not in pagina.locator("#agente-bi-dashboard-grade").inner_text()
        assert "42" not in pagina.locator("[data-ref='g2']").inner_text()
        for ref_card in ("g1", "g2", "g3", "g4", "g5"):
            link = pagina.locator(f"[data-ref='{ref_card}'] a")
            assert link.count() == 1
            assert link.inner_text().strip() == "Ver dados"
            assert link.get_attribute("target") == "_blank"
            relacao = link.get_attribute("rel") or ""
            assert "noopener" in relacao and "noreferrer" in relacao
            href = link.get_attribute("href") or ""
            assert f"/calculos/3/graficos/{ref_card}" in href
            assert "sul" not in href
            assert "25.25" not in href
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
        href_filtrado = pagina.locator("[data-ref='g1'] a").get_attribute("href") or ""
        assert "/calculos/9/graficos/g1" in href_filtrado
        assert "/calculos/3/" not in href_filtrado
        assert "sul" not in href_filtrado
        assert "4.25" not in href_filtrado
        assert pagina.locator("#agente-bi-dashboard-grade table").count() == 0
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


def _base_detalhe(email, slug):
    usuario, franquia, escopo, perfil, pacote, _aprovada = _jornada(email, slug)
    definicao = definir_selecao(escopo, {"analise_id": pacote["id"]})
    calculo = _calcular(escopo, definicao["definicao"]["id"])
    painel_cheio = montar_dashboard(escopo, {"analise_id": calculo["calculo"]["id"]})
    beta = _coluna(perfil, indice=1, aba=0)
    valor = _coluna(perfil, dimensao_principal="valor", aba=0)
    pacote_filtro = _indicador(pacote, "soma", leitura="indicador_de_medida")
    pacote_filtro["graficos"][0]["medida"] = _ref(valor)
    documento = definir(perfil, pacote_filtro, provedor=_filtrar(beta, "igual", "faixa", "texto"))
    salva = _gravar(escopo, pacote["id"], documento)
    base = _calcular(escopo, salva["id"], parametros={"faixa": "sul"})
    painel = montar_dashboard(escopo, {"analise_id": base["calculo"]["id"]})
    filtrado = filtrar_dashboard(escopo, painel["dashboard"]["id"], {"parametros": {"faixa": "norte"}})
    outro_doc = definir(perfil, _indicador(pacote, "contagem", leitura="contagem_de_registros"))
    outra = _gravar(escopo, pacote["id"], outro_doc)
    outro = _calcular(escopo, outra["id"])
    return {
        "usuario": usuario,
        "franquia": franquia,
        "escopo": escopo,
        "painel_cheio": painel_cheio,
        "painel": painel,
        "filtrado": filtrado,
        "ref": painel["consulta"]["graficos"][0]["ref"],
        "outro_calculo_id": outro["calculo"]["id"],
    }


def _caminho_detalhe(dashboard_id, revisao, calculo_id, ref):
    return (
        f"/api/agente-bi/dashboards/{dashboard_id}/revisoes/{revisao}"
        f"/calculos/{calculo_id}/graficos/{ref}"
    )


def test_detalhe_le_o_calculo_exibido_sem_recalcular_nem_nova_revisao(ctx, monkeypatch):
    base = _base_detalhe("dashboard.detalhe@test.com", "dashboard-detalhe")
    escopo = base["escopo"]
    painel = base["painel"]
    filtrado = base["filtrado"]
    ref = base["ref"]
    identificador = painel["dashboard"]["id"]
    revisao = painel["dashboard"]["revisao"]
    assert filtrado["consulta"]["calculo_id"] != painel["dashboard"]["analise_id"]
    assert filtrado["consulta"]["graficos"][0]["resultado"] == "21"
    assert filtrado["persistido"] is False

    cheio = base["painel_cheio"]
    soma = _painel_soma(cheio["consulta"]["graficos"])
    outros_refs = [item["ref"] for item in cheio["consulta"]["graficos"] if item["ref"] != soma["ref"]]
    assert outros_refs

    antes_analises = AgenteBiAnalise.query.count()
    antes_dashboards = AgenteBiDashboard.query.count()
    antes_tentativas = IaChamadaTentativa.query.count()
    antes_consumo = IaConsumoEvento.query.count()

    def proibido(*_args, **_kwargs):
        raise AssertionError("recalculo")

    monkeypatch.setattr("app.services.agente_bi_dashboard_service.calcular_definicao", proibido)
    monkeypatch.setattr("app.services.agente_bi_dashboard_service.filtrar_dashboard", proibido)
    monkeypatch.setattr("app.services.agente_bi_dashboard_service.montar_dashboard", proibido)
    monkeypatch.setattr("app.services.agente_bi_persistencia_service.criar_analise", proibido)
    monkeypatch.setattr("app.services.agente_bi_persistencia_service.criar_dashboard", proibido)
    monkeypatch.setattr("app.services.agente_bi_persistencia_service.obter_dataset", proibido)

    detalhe = consultar_detalhe_grafico(
        escopo,
        identificador,
        revisao,
        filtrado["consulta"]["calculo_id"],
        ref,
    )
    grafico = detalhe["detalhe"]["grafico"]
    assert detalhe["detalhe"]["calculo_id"] == filtrado["consulta"]["calculo_id"]
    assert detalhe["detalhe"]["definicao_id"] == filtrado["consulta"]["definicao_id"]
    assert detalhe["detalhe"]["dataset_id"] == filtrado["consulta"]["dataset_id"]
    assert detalhe["detalhe"]["versao"] == filtrado["consulta"]["versao"]
    assert detalhe["detalhe"]["impressao"] == filtrado["consulta"]["impressao"]
    assert detalhe["detalhe"]["revisao"] == revisao
    assert detalhe["detalhe"]["dashboard_id"] == identificador
    assert detalhe["detalhe"]["chamada_anthropic"] is False
    assert detalhe["detalhe"]["ia_calculo"] is False
    assert "graficos" not in detalhe["detalhe"]
    assert grafico["resultado"] == "21"
    assert grafico["elegiveis"] == 2
    assert grafico["serie"] == filtrado["consulta"]["graficos"][0]["serie"]
    assert grafico["ref"] == ref

    original = consultar_detalhe_grafico(
        escopo,
        identificador,
        revisao,
        painel["consulta"]["calculo_id"],
        ref,
    )
    assert original["detalhe"]["grafico"]["resultado"] == "4.25"
    assert original["detalhe"]["calculo_id"] == painel["dashboard"]["analise_id"]

    unico = consultar_detalhe_grafico(
        escopo,
        cheio["dashboard"]["id"],
        cheio["dashboard"]["revisao"],
        cheio["consulta"]["calculo_id"],
        soma["ref"],
    )
    bruto = json.dumps(unico["detalhe"])
    assert unico["detalhe"]["grafico"]["ref"] == soma["ref"]
    assert unico["detalhe"]["grafico"]["resultado"] == soma["resultado"]
    assert unico["detalhe"]["grafico"]["serie"] == soma["serie"]
    for outro_ref in outros_refs:
        assert f'"{outro_ref}"' not in bruto

    with pytest.raises(AgenteBiErro) as revisao_errada:
        consultar_detalhe_grafico(escopo, identificador, revisao + 9, filtrado["consulta"]["calculo_id"], ref)
    assert revisao_errada.value.status == 409
    assert revisao_errada.value.codigo == "identidade_invalida"

    with pytest.raises(AgenteBiErro) as ref_errado:
        consultar_detalhe_grafico(escopo, identificador, revisao, filtrado["consulta"]["calculo_id"], "inexistente")
    assert ref_errado.value.status == 404

    with pytest.raises(AgenteBiErro) as calculo_ausente:
        consultar_detalhe_grafico(escopo, identificador, revisao, 999999, ref)
    assert calculo_ausente.value.status == 404

    with pytest.raises(AgenteBiErro) as painel_ausente:
        consultar_detalhe_grafico(escopo, 999999, revisao, filtrado["consulta"]["calculo_id"], ref)
    assert painel_ausente.value.status == 404

    with pytest.raises(AgenteBiErro) as incompativel:
        consultar_detalhe_grafico(escopo, identificador, revisao, base["outro_calculo_id"], ref)
    assert incompativel.value.status == 409
    assert incompativel.value.codigo == "identidade_invalida"

    colega = seed_usuario(base["franquia"].id, base["usuario"].conta_id, email="dashboard.detalhe.colega@test.com")
    _outra_conta, _outra_franquia, outro_usuario = _par("dashboard-detalhe-outro", "dashboard.detalhe.outro@test.com")
    for intruso in (escopo_do_usuario(colega), escopo_do_usuario(outro_usuario)):
        with pytest.raises(AgenteBiErro) as leitura:
            consultar_detalhe_grafico(intruso, identificador, revisao, filtrado["consulta"]["calculo_id"], ref)
        assert leitura.value.status == 404

    assert AgenteBiAnalise.query.count() == antes_analises
    assert AgenteBiDashboard.query.count() == antes_dashboards
    assert db.session.get(AgenteBiDashboard, identificador).revisao == revisao
    assert IaChamadaTentativa.query.count() == antes_tentativas
    assert IaConsumoEvento.query.count() == antes_consumo


def test_api_detalhe_isola_e_nao_recalcula(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)
    caminho_generico = "/api/agente-bi/dashboards/1/revisoes/1/calculos/1/graficos/g1"
    monkeypatch.delenv("AGENTE_BI_API_ENABLED", raising=False)
    desligada = client.get(caminho_generico)
    assert desligada.status_code == 404
    assert desligada.headers["Cache-Control"] == "no-store"
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    anonimo = client.get(caminho_generico)
    assert anonimo.status_code == 401
    assert anonimo.get_json()["codigo"] == "autenticacao_necessaria"
    assert anonimo.headers["Cache-Control"] == "no-store"
    assert "serie" not in anonimo.get_data(as_text=True)

    with app.app_context():
        base = _base_detalhe("dashboard.detalhe.api@test.com", "dashboard-detalhe-api")
        usuario_id = base["escopo"].usuario_id
        conta_id = base["usuario"].conta_id
        franquia_id = base["franquia"].id
        dashboard_id = base["painel"]["dashboard"]["id"]
        revisao = base["painel"]["dashboard"]["revisao"]
        calculo_id = base["filtrado"]["consulta"]["calculo_id"]
        ref = base["ref"]
        outro_calculo_id = base["outro_calculo_id"]
    caminho = _caminho_detalhe(dashboard_id, revisao, calculo_id, ref)
    sem_sessao = client.get(caminho)
    assert sem_sessao.status_code == 401
    assert "serie" not in sem_sessao.get_data(as_text=True)

    with app.app_context():
        antes_analises = AgenteBiAnalise.query.count()
        antes_dashboards = AgenteBiDashboard.query.count()
        antes_tentativas = IaChamadaTentativa.query.count()
        antes_consumo = IaConsumoEvento.query.count()
    _entrar(client, usuario_id)
    resposta = client.get(caminho)
    assert resposta.status_code == 200
    assert resposta.headers["Cache-Control"] == "no-store"
    corpo = resposta.get_json()
    assert corpo["detalhe"]["grafico"]["resultado"] == "21"
    assert corpo["detalhe"]["grafico"]["ref"] == ref
    assert corpo["detalhe"]["calculo_id"] == calculo_id
    assert "graficos" not in corpo["detalhe"]
    assert "dados_tipados" not in resposta.get_data(as_text=True)

    incompativel = client.get(
        _caminho_detalhe(dashboard_id, revisao, outro_calculo_id, ref)
    )
    assert incompativel.status_code == 409
    assert incompativel.get_json()["codigo"] == "identidade_invalida"
    assert incompativel.headers["Cache-Control"] == "no-store"
    assert "serie" not in incompativel.get_data(as_text=True)

    ausente = client.get(_caminho_detalhe(999999, revisao, calculo_id, ref))
    assert ausente.status_code == 404
    assert ausente.headers["Cache-Control"] == "no-store"

    with client.session_transaction() as sess:
        sess.clear()
    with app.app_context():
        colega = seed_usuario(franquia_id, conta_id, email="dashboard.detalhe.api.colega@test.com")
        colega_id = colega.id
    _entrar(client, colega_id)
    do_colega = client.get(caminho)
    assert do_colega.status_code == 404
    assert "serie" not in do_colega.get_data(as_text=True)

    with client.session_transaction() as sess:
        sess.clear()
    with app.app_context():
        _outra_conta, _outra_franquia, outro = _par(
            "dashboard-detalhe-api-outro",
            "dashboard.detalhe.api.outro@test.com",
        )
        outro_id = outro.id
    _entrar(client, outro_id)
    do_outro = client.get(caminho)
    assert do_outro.status_code == 404

    with client.session_transaction() as sess:
        sess.clear()
    _entrar(client, usuario_id)
    monkeypatch.setattr(
        "app.agente_bi_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario: {"permitido": False},
    )
    bloqueado = client.get(caminho)
    assert bloqueado.status_code == 403
    assert bloqueado.get_json()["codigo"] == "operacao_nao_permitida"
    assert bloqueado.headers["Cache-Control"] == "no-store"
    assert "serie" not in bloqueado.get_data(as_text=True)

    with app.app_context():
        assert AgenteBiAnalise.query.count() == antes_analises
        assert AgenteBiDashboard.query.count() == antes_dashboards
        assert IaChamadaTentativa.query.count() == antes_tentativas
        assert IaConsumoEvento.query.count() == antes_consumo
        assert db.session.get(AgenteBiDashboard, dashboard_id).revisao == revisao


def _abrir_pagina_detalhe(monkeypatch, *, authenticated, permitido=True, flag=True):
    import flask_login.utils

    web = _client(monkeypatch, authenticated=authenticated, permitido=permitido)
    usuario = _user(authenticated=authenticated)
    usuario.id = 1
    monkeypatch.setattr(flask_login.utils, "_get_user", lambda: usuario)
    monkeypatch.setattr(
        "app.cleide_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario: {
            "permitido": permitido,
            "mensagem_usuario": "Sua franquia atingiu o limite operacional.",
        },
    )
    if flag:
        monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    else:
        monkeypatch.delenv("AGENTE_BI_API_ENABLED", raising=False)
    return web.app.test_client()


def test_pagina_de_detalhe_nao_entrega_serie_nem_abre_para_visitante(monkeypatch):
    caminho = "/cleide-bi-frete/dashboards/7/revisoes/1/calculos/9/graficos/g2"
    desligada = _abrir_pagina_detalhe(monkeypatch, authenticated=True, flag=False).get(caminho)
    assert desligada.status_code == 404
    assert desligada.headers["Cache-Control"] == "no-store"
    assert "g2" not in desligada.get_data(as_text=True)

    visitante = _abrir_pagina_detalhe(monkeypatch, authenticated=False).get(caminho)
    assert visitante.status_code == 401
    assert visitante.headers["Cache-Control"] == "no-store"
    assert visitante.get_data(as_text=True) == "Recurso indisponível."
    assert "10.5" not in visitante.get_data(as_text=True)
    assert "agente-bi-dashboard-detalhe" not in visitante.get_data(as_text=True)

    bloqueada = _abrir_pagina_detalhe(monkeypatch, authenticated=True, permitido=False).get(caminho)
    assert bloqueada.status_code == 403
    assert bloqueada.headers["Cache-Control"] == "no-store"
    assert "10.5" not in bloqueada.get_data(as_text=True)
    assert "agente-bi-dashboard-detalhe" not in bloqueada.get_data(as_text=True)

    invalida = _abrir_pagina_detalhe(monkeypatch, authenticated=True).get(
        "/cleide-bi-frete/dashboards/7/revisoes/1/calculos/9/graficos/*ruim"
    )
    assert invalida.status_code == 404
    assert "10.5" not in invalida.get_data(as_text=True)

    pagina = _abrir_pagina_detalhe(monkeypatch, authenticated=True).get(caminho)
    assert pagina.status_code == 200
    assert pagina.headers["Cache-Control"] == "no-store"
    html = pagina.get_data(as_text=True)
    assert 'id="agente-bi-dashboard-detalhe"' in html
    assert "/api/agente-bi/dashboards/7/revisoes/1/calculos/9/graficos/g2" in html
    assert "agente_bi_dashboard.js" in html
    assert "chart.js" not in html
    assert "agente-bi-dashboard-tabela" not in html
    assert "<table" not in html
    assert "10.5" not in html
    assert "31.5" not in html
    assert "25.25" not in html
    assert "dados_tipados" not in html


def _documento_detalhe(html):
    css = (ROOT / "app" / "static" / "css" / "agente_bi.css").read_text(encoding="utf-8")
    tema = (ROOT / "app" / "static" / "css" / "agentefrete-theme.css").read_text(encoding="utf-8")
    script = (ROOT / "app" / "static" / "js" / "agente_bi_dashboard.js").read_text(encoding="utf-8")
    html = re.sub(r"<link\b[^>]*>", "", html, flags=re.I)
    html = re.sub(r"<script\b[^>]*\bsrc\s*=[^>]*>\s*</script>", "", html, flags=re.I)
    html = html.replace("</head>", f"<style>{tema}\n{css}</style></head>", 1)
    html = html.replace(
        "<head>",
        """<head><script>
        window.__detalheChamadas = [];
        window.fetch = function (input, opcoes) {
          var metodo = (opcoes && opcoes.method) || "GET";
          window.__detalheChamadas.push({ url: String(input), metodo: metodo });
          return Promise.resolve({
            ok: true,
            status: 200,
            json: function () {
              return Promise.resolve({
                ok: true,
                detalhe: {
                  contrato: "dashboard.v1",
                  chamada_anthropic: false,
                  ia_calculo: false,
                  dashboard_id: 7,
                  revisao: 1,
                  chave: "painel-fretes",
                  calculo_id: 9,
                  definicao_id: 2,
                  dataset_id: 4,
                  versao: 1,
                  impressao: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                  grafico: {
                    ref: "g2",
                    titulo: "barra-media",
                    tipo: "barra",
                    status: "calculado",
                    motivo: null,
                    operacao: "media",
                    unidade: "valor",
                    resultado: null,
                    total: "21",
                    serie: [
                      { chave: "a", resultado: "10.5", elegiveis: 1 },
                      { chave: "b", resultado: "31.5", elegiveis: 1 }
                    ],
                    avisos: ["ha_exclusoes"],
                    elegiveis: 2,
                    grupos: 2
                  }
                }
              });
            }
          });
        };
        </script>""",
        1,
    )
    html = html.replace("</body>", f"<script>{script}</script></body>", 1)
    return html


def test_pagina_de_detalhe_abre_so_a_tabela_do_grafico(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    html = _abrir_pagina_detalhe(monkeypatch, authenticated=True).get(
        "/cleide-bi-frete/dashboards/7/revisoes/1/calculos/9/graficos/g2"
    ).get_data(as_text=True)
    documento = _documento_detalhe(html)
    erros = []

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        pagina = browser.new_page(viewport={"width": 1280, "height": 900})
        pagina.on("pageerror", lambda erro: erros.append(str(erro)))
        pagina.set_content(documento, wait_until="domcontentloaded")
        pagina.wait_for_selector("#agente-bi-detalhe-conteudo table")
        assert pagina.locator("#agente-bi-detalhe-conteudo table").count() == 1
        assert pagina.locator("#agente-bi-dashboard-detalhe canvas").count() == 0
        assert pagina.locator("#agente-bi-dashboard-grade").count() == 0
        texto = pagina.locator("#agente-bi-detalhe-conteudo table").inner_text()
        assert "10.5" in texto
        assert "31.5" in texto
        assert "norte" not in pagina.locator("#agente-bi-dashboard-detalhe").inner_text()
        assert "25.25" not in pagina.locator("#agente-bi-dashboard-detalhe").inner_text()
        assert "Revisão 1 de painel-fretes" in pagina.locator("#agente-bi-detalhe-identidade").inner_text()
        assert "Barras · Média" in pagina.locator("#agente-bi-detalhe-identidade").inner_text()
        assert "aaaaaaaa" not in pagina.locator("#agente-bi-dashboard-detalhe").inner_text()
        chamadas = pagina.evaluate("() => window.__detalheChamadas")
        nossas = [
            item for item in chamadas
            if "agente-bi" in item["url"] or item["url"].endswith("/graficos/g2")
        ]
        assert nossas == [{
            "url": "/api/agente-bi/dashboards/7/revisoes/1/calculos/9/graficos/g2",
            "metodo": "GET",
        }], chamadas
        assert not any(item["metodo"] == "POST" for item in nossas)
        relevantes = [erro for erro in erros if "$ is not defined" not in erro]
        assert relevantes == []
        browser.close()
