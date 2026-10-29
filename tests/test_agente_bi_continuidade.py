"""SCRUM-271: continuidade mínima depois da recarga, sem histórico paralelo.

O transporte é simulado. Nenhuma chamada usa o SDK da Anthropic.
O valor de filtro não entra na continuidade: a política já aprovada não o grava.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiAplicacao, AgenteBiDashboard, IaChamadaTentativa, IaConsumoEvento
from app.services.agente_bi_continuidade_service import recuperar_continuidade, texto_do_plano_compativel
from app.services.agente_bi_dashboard_service import consultar_dashboard, filtrar_dashboard
from app.services.agente_bi_persistencia_service import AgenteBiErro, escopo_do_usuario
from app.services.agente_bi_publicacao_v2_service import PublicacaoErro, aplicar_pedido
from tests.conftest import seed_usuario
from tests.test_agente_bi_execucao_v2 import _dividir
from tests.test_agente_bi_explicacao import _factual, _fato_de
from tests.test_agente_bi_ingestao import _cliente, _entrar, _par
from tests.test_agente_bi_interpretacao_v2 import _resposta
from tests.test_agente_bi_publicacao_v2 import (
    TEXTO,
    TEXTO_MEDIA,
    _enviar,
    _media_e_filtro,
    _preparar,
    _resultado,
    _so_media,
)

ROOT = Path(__file__).resolve().parents[1]
FONTE = (ROOT / "app" / "services" / "agente_bi_continuidade_service.py").read_text(encoding="utf-8")
EXPLICACAO = (ROOT / "app" / "services" / "agente_bi_explicacao_service.py").read_text(encoding="utf-8")
PUBLICACAO = (ROOT / "app" / "services" / "agente_bi_publicacao_v2_service.py").read_text(encoding="utf-8")
PERGUNTA = "O que o agregado publicado desta revisão mostra?"


@pytest.fixture(autouse=True)
def _transporte_simulado(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)


def test_modulo_nao_abre_chat_nem_classifica_linguagem():
    assert "import re" not in FONTE
    assert "db.Model" not in FONTE
    assert "db.Column" not in FONTE
    assert "criar_analise" not in FONTE
    assert "criar_dashboard" not in FONTE
    assert "localStorage" not in FONTE
    assert "explicar_resultados" not in FONTE
    assert "fato" in EXPLICACAO and "leitura" in EXPLICACAO
    assert "explicar_resultados" in PUBLICACAO
    assert "texto_do_plano_compativel" in PUBLICACAO
    assert "_classificar" not in PUBLICACAO


def test_caso_a_continuidade_depois_da_recarga(ctx):
    base = _preparar("continuidade.recarga@test.com", "continuidade-recarga")
    escopo = base["escopo"]
    original = base["dashboard_id"]
    antes = AgenteBiAnalise.query.count()
    assert recuperar_continuidade(escopo, original)["situacao"] == "ausente"
    assert AgenteBiAnalise.query.count() == antes

    transporte, estado = _enviar(_so_media)
    publicado = aplicar_pedido(escopo, original, {"texto": TEXTO_MEDIA}, transporte=transporte)
    assert publicado["situacao"] == "aplicada"
    novo = publicado["dashboard"]["id"]
    media = _dividir("5", "3")
    assert publicado["consulta"]["graficos"][0]["resultado"] == media
    assert estado["n"] == 1

    recarga = consultar_dashboard(escopo, novo)
    continuidade = recuperar_continuidade(escopo, novo, consulta=recarga)
    assert continuidade["situacao"] == "validado"
    assert continuidade["dashboard_id"] == novo
    assert continuidade["revisao"] == recarga["dashboard"]["revisao"]
    assert continuidade["revisao_publicada"] == recarga["dashboard"]["revisao"]
    assert continuidade["revisao_base"] == recarga["dashboard"]["revisao"] - 1
    assert continuidade["plano_id"] == publicado["plano_id"]
    assert continuidade["calculo_id"] == recarga["consulta"]["calculo_id"]
    assert continuidade["esclarecimento"] is None
    _sem_prosa(continuidade, TEXTO_MEDIA)
    assert recarga["consulta"]["graficos"][0]["resultado"] == media
    assert consultar_dashboard(escopo, original)["consulta"]["graficos"][0]["resultado"] == "5"

    explicado, explicacao = _explicar(escopo, novo)
    assert explicado["situacao"] == "explicacao"
    assert explicado["dashboard"]["id"] == novo
    assert set(explicado["explicacao"]["comprovado"][0]) == {"fato", "leitura"}
    assert explicado["explicacao"]["comprovado"][0]["fato"] == _fato_de(
        explicacao["agregados"][0], "resultado", media
    )
    assert explicacao["agregados"][0]["resultado"] == media
    assert "5" != explicacao["agregados"][0]["resultado"]
    depois = recuperar_continuidade(escopo, novo)
    assert depois["situacao"] == "validado"
    assert depois["plano_id"] == continuidade["plano_id"]
    _sem_prosa(depois, explicado["explicacao"]["comprovado"][0]["leitura"])
    _sem_prosa(depois, PERGUNTA)
    assert consultar_dashboard(escopo, novo)["consulta"]["graficos"][0]["resultado"] == media
    eventos = IaConsumoEvento.query.filter_by(usuario_id=escopo.usuario_id).all()
    assert eventos
    assert {evento.flow_type for evento in eventos} == {"bi_logistico"}


def test_caso_b_esclarecimento_retomado_sem_repetir_a_pergunta(ctx):
    base = _preparar("continuidade.esclarece@test.com", "continuidade-esclarece")
    escopo = base["escopo"]
    original = base["dashboard_id"]
    paineis = AgenteBiDashboard.query.count()
    transporte, estado = _enviar(_media_e_filtro)
    pendente = aplicar_pedido(escopo, original, {"texto": TEXTO}, transporte=transporte)
    assert pendente["situacao"] == "esclarecimento"
    assert pendente["esclarecimento"]["parametros"] == ["faixa"]
    assert AgenteBiDashboard.query.count() == paineis
    assert estado["n"] == 1

    recarga = recuperar_continuidade(escopo, original)
    assert recarga["situacao"] == "esclarecimento"
    assert recarga["revisao"] == consultar_dashboard(escopo, original)["dashboard"]["revisao"]
    assert recarga["revisao_publicada"] is None
    assert recarga["esclarecimento"]["motivo"] == "informar_parametro"
    assert recarga["esclarecimento"]["parametros"] == ["faixa"]
    assert recarga["plano_id"]
    _sem_prosa(recarga, TEXTO)
    _sem_prosa(recarga, "norte")
    assert "faixa" in recarga["esclarecimento"]["pergunta"]

    publicado = aplicar_pedido(
        escopo,
        original,
        {"plano_id": recarga["plano_id"], "parametros": {"faixa": "norte"}},
        transporte=transporte,
    )
    assert publicado["situacao"] == "aplicada"
    assert publicado["chamada_anthropic"] is False
    assert publicado["reusada"] is False
    assert estado["n"] == 1
    assert _resultado(publicado["consulta"], "media")["resultado"] == "2"
    assert _resultado(publicado["consulta"], "media")["elegiveis"] == 2
    novo = publicado["dashboard"]["id"]
    assert AgenteBiDashboard.query.count() == paineis + 1

    aberto = consultar_dashboard(escopo, novo)
    continuidade = recuperar_continuidade(escopo, novo, consulta=aberto)
    assert continuidade["situacao"] == "validado"
    assert continuidade["plano_id"] == recarga["plano_id"]
    assert continuidade["revisao_publicada"] == aberto["dashboard"]["revisao"]
    assert continuidade["revisao_base"] == recarga["revisao"]
    assert continuidade["esclarecimento"] is None
    _sem_prosa(continuidade, "norte")
    _sem_prosa(continuidade, TEXTO)
    assert _resultado(aberto["consulta"], "media")["resultado"] == "2"
    nomes = {item["nome"] for item in aberto["consulta"]["filtros"]}
    assert "faixa" in nomes
    assert "norte" not in json.dumps(aberto["consulta"]["filtros"], ensure_ascii=False)
    assert consultar_dashboard(escopo, original)["consulta"]["graficos"][0]["resultado"] == "5"

    explicado, explicacao = _explicar(escopo, novo)
    assert explicado["dashboard"]["id"] == novo
    assert explicacao["agregados"][0]["resultado"] == "2"
    assert "norte" not in json.dumps(explicacao["agregados"], ensure_ascii=False)

    repetido = aplicar_pedido(
        escopo,
        original,
        {"plano_id": recarga["plano_id"], "parametros": {"faixa": "sul"}},
        transporte=transporte,
    )
    assert repetido["reusada"] is True
    assert repetido["dashboard"]["id"] == novo
    assert _resultado(repetido["consulta"], "media")["resultado"] == "2"
    assert AgenteBiDashboard.query.count() == paineis + 1
    assert estado["n"] == 1


def test_caso_c_revisao_nova_nao_reusa_plano_anterior(ctx):
    base = _preparar("continuidade.revisao@test.com", "continuidade-revisao")
    escopo = base["escopo"]
    original = base["dashboard_id"]
    paineis = AgenteBiDashboard.query.count()
    transporte, estado = _alternar(_media_e_filtro, _so_media)
    pendente = aplicar_pedido(escopo, original, {"texto": TEXTO}, transporte=transporte)
    assert pendente["situacao"] == "esclarecimento"
    plano_antigo = recuperar_continuidade(escopo, original)["plano_id"]
    assert plano_antigo

    publicado = aplicar_pedido(escopo, original, {"texto": TEXTO_MEDIA}, transporte=transporte)
    assert publicado["situacao"] == "aplicada"
    novo = publicado["dashboard"]["id"]
    media = _dividir("5", "3")
    assert publicado["consulta"]["graficos"][0]["resultado"] == media
    assert AgenteBiDashboard.query.count() == paineis + 1

    atual = recuperar_continuidade(escopo, novo)
    assert atual["situacao"] == "validado"
    assert atual["plano_id"] != plano_antigo
    assert atual["plano_id"] == publicado["plano_id"]
    assert atual["revisao_publicada"] == publicado["dashboard"]["revisao"]
    assert atual["dashboard_id"] == novo
    assert recuperar_continuidade(escopo, original)["situacao"] == "ausente"
    assert recuperar_continuidade(escopo, original)["plano_id"] is None

    with pytest.raises(PublicacaoErro) as conflito:
        aplicar_pedido(
            escopo,
            novo,
            {"plano_id": plano_antigo, "parametros": {"faixa": "norte"}},
            transporte=transporte,
        )
    assert conflito.value.codigo == "conflito_versao"
    assert conflito.value.status == 409
    assert AgenteBiDashboard.query.count() == paineis + 1
    assert consultar_dashboard(escopo, novo)["consulta"]["graficos"][0]["resultado"] == media
    assert consultar_dashboard(escopo, original)["consulta"]["graficos"][0]["resultado"] == "5"

    with pytest.raises(PublicacaoErro) as na_base:
        aplicar_pedido(
            escopo,
            original,
            {"plano_id": plano_antigo, "parametros": {"faixa": "norte"}},
            transporte=transporte,
        )
    assert na_base.value.codigo == "conflito_versao"
    assert AgenteBiDashboard.query.count() == paineis + 1
    assert estado["n"] == 2


def test_caso_d_filtro_vigente_sobrevive_a_recarga_e_o_efemero_nao(ctx):
    base = _preparar("continuidade.filtro@test.com", "continuidade-filtro")
    escopo = base["escopo"]
    original = base["dashboard_id"]
    transporte, _estado = _enviar(_media_e_filtro)
    aplicar_pedido(escopo, original, {"texto": TEXTO}, transporte=transporte)
    plano = recuperar_continuidade(escopo, original)["plano_id"]
    publicado = aplicar_pedido(
        escopo,
        original,
        {"plano_id": plano, "parametros": {"faixa": "norte"}},
        transporte=transporte,
    )
    novo = publicado["dashboard"]["id"]
    assert _resultado(publicado["consulta"], "media")["resultado"] == "2"

    efemero = filtrar_dashboard(escopo, novo, {"parametros": {"faixa": "sul"}})
    assert efemero["persistido"] is False
    assert _resultado(efemero["consulta"], "media")["resultado"] == "1"
    reaberto = consultar_dashboard(escopo, novo)
    assert _resultado(reaberto["consulta"], "media")["resultado"] == "2"
    vigencia = recuperar_continuidade(escopo, novo, consulta=reaberto)
    assert vigencia["situacao"] == "validado"
    assert vigencia["dashboard_id"] == novo
    assert vigencia["calculo_id"] == reaberto["consulta"]["calculo_id"]
    _sem_prosa(vigencia, "sul")
    _sem_prosa(vigencia, "norte")
    assert {item["nome"]: item["operador"] for item in reaberto["consulta"]["filtros"]}["faixa"] == "igual"

    _explicado, explicacao = _explicar(escopo, novo)
    assert explicacao["agregados"][0]["resultado"] == "2"

    salvo = filtrar_dashboard(escopo, novo, {"parametros": {"faixa": "sul"}, "persistir": True})
    assert salvo["persistido"] is True
    assert salvo["dashboard"]["id"] != novo
    assert _resultado(salvo["consulta"], "media")["resultado"] == "1"
    recarga = consultar_dashboard(escopo, salvo["dashboard"]["id"])
    assert _resultado(recarga["consulta"], "media")["resultado"] == "1"
    seguinte = recuperar_continuidade(escopo, salvo["dashboard"]["id"], consulta=recarga)
    assert seguinte["dashboard_id"] == salvo["dashboard"]["id"]
    assert seguinte["revisao"] == recarga["dashboard"]["revisao"]
    assert seguinte["plano_id"] != plano
    _sem_prosa(seguinte, "sul")
    assert _resultado(consultar_dashboard(escopo, novo)["consulta"], "media")["resultado"] == "2"
    with pytest.raises(PublicacaoErro) as conflito:
        aplicar_pedido(escopo, salvo["dashboard"]["id"], {"plano_id": plano}, transporte=transporte)
    assert conflito.value.codigo == "conflito_versao"
    assert _resultado(consultar_dashboard(escopo, salvo["dashboard"]["id"])["consulta"], "media")["resultado"] == "1"

    _explicado_salvo, explicacao_salva = _explicar(escopo, salvo["dashboard"]["id"], "Qual é a visão filtrada agora?")
    assert explicacao_salva["agregados"][0]["resultado"] == "1"


def test_caso_e_isolamento_de_usuario_conta_e_franquia(ctx):
    base = _preparar("continuidade.dono@test.com", "continuidade-dono")
    escopo = base["escopo"]
    original = base["dashboard_id"]
    transporte, _estado = _enviar(_media_e_filtro)
    aplicar_pedido(escopo, original, {"texto": TEXTO}, transporte=transporte)
    plano = recuperar_continuidade(escopo, original)["plano_id"]
    colega = seed_usuario(escopo.franquia_id, escopo.conta_id, email="continuidade.colega@test.com")
    _outra_conta, _outra_franquia, intruso = _par("continuidade-intruso", "continuidade.intruso@test.com")

    for alheio in (escopo_do_usuario(colega), escopo_do_usuario(intruso)):
        with pytest.raises(AgenteBiErro) as erro:
            recuperar_continuidade(alheio, original)
        assert erro.value.codigo == "nao_encontrado"
        assert erro.value.status == 404
        with pytest.raises(PublicacaoErro) as recusa:
            aplicar_pedido(alheio, original, {"plano_id": plano, "parametros": {"faixa": "norte"}}, transporte=transporte)
        assert recusa.value.codigo == "nao_encontrado"
        assert recusa.value.status == 404
        with pytest.raises(PublicacaoErro):
            texto_do_plano_compativel(alheio, original, plano)

    assert recuperar_continuidade(escopo, original)["plano_id"] == plano
    assert consultar_dashboard(escopo, original)["consulta"]["graficos"][0]["resultado"] == "5"
    assert AgenteBiAplicacao.query.filter_by(usuario_id=colega.id).count() == 0
    assert AgenteBiAplicacao.query.filter_by(usuario_id=intruso.id).count() == 0


def test_caso_f_falha_e_conflito_nao_publicam_parcial(ctx, monkeypatch):
    base = _preparar("continuidade.falha@test.com", "continuidade-falha")
    escopo = base["escopo"]
    original = base["dashboard_id"]
    paineis = AgenteBiDashboard.query.count()
    aplicacoes = AgenteBiAplicacao.query.count()
    transporte, estado = _enviar(_media_e_filtro)
    aplicar_pedido(escopo, original, {"texto": TEXTO}, transporte=transporte)
    plano = recuperar_continuidade(escopo, original)["plano_id"]

    def estourar(*_args, **_kwargs):
        raise RuntimeError("continuidade")

    from app.services.agente_bi_aplicacao_service import concluir_aplicacao

    monkeypatch.setattr("app.services.agente_bi_publicacao_v2_service.concluir_aplicacao", estourar)
    with pytest.raises(RuntimeError, match="continuidade"):
        aplicar_pedido(
            escopo,
            original,
            {"plano_id": plano, "parametros": {"faixa": "norte"}},
            transporte=transporte,
        )
    assert AgenteBiDashboard.query.count() == paineis
    assert AgenteBiAplicacao.query.filter_by(usuario_id=escopo.usuario_id, estado="concluida").count() == 0
    assert AgenteBiAplicacao.query.count() == aplicacoes
    assert consultar_dashboard(escopo, original)["consulta"]["graficos"][0]["resultado"] == "5"
    assert recuperar_continuidade(escopo, original)["situacao"] == "esclarecimento"
    assert estado["n"] == 1

    monkeypatch.setattr("app.services.agente_bi_publicacao_v2_service.concluir_aplicacao", concluir_aplicacao)
    publicado = aplicar_pedido(
        escopo,
        original,
        {"plano_id": plano, "parametros": {"faixa": "norte"}},
        transporte=transporte,
    )
    assert publicado["situacao"] == "aplicada"
    assert _resultado(publicado["consulta"], "media")["resultado"] == "2"
    assert AgenteBiDashboard.query.count() == paineis + 1
    assert estado["n"] == 1

    with pytest.raises(PublicacaoErro) as mistura:
        aplicar_pedido(
            escopo,
            original,
            {"plano_id": plano, "texto": "outro pedido", "parametros": {"faixa": "sul"}},
            transporte=transporte,
        )
    assert mistura.value.codigo == "conflito_versao"
    assert AgenteBiDashboard.query.count() == paineis + 1
    assert consultar_dashboard(escopo, publicado["dashboard"]["id"])["consulta"]["graficos"][0]["resultado"] == "2"


def test_caso_g_rota_reabre_continuidade_sem_texto(app, monkeypatch):
    client = _cliente(app, monkeypatch)
    with app.app_context():
        base = _preparar("continuidade.rota@test.com", "continuidade-rota")
        usuario_id = base["escopo"].usuario_id
        identificador = base["dashboard_id"]
    transporte, estado = _enviar(_media_e_filtro)
    import app.services.agente_bi_publicacao_v2_service as publicacao

    real = publicacao.aplicar_pedido

    def envolvido(escopo, dashboard_id, corpo, transporte=None):
        return real(escopo, dashboard_id, corpo, transporte=transporte or envolvido.transporte)

    envolvido.transporte = transporte
    monkeypatch.setattr(publicacao, "aplicar_pedido", envolvido)

    assert client.get(f"/api/agente-bi/dashboards/{identificador}/consulta").status_code == 401
    _entrar(client, usuario_id)
    pedido = client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": TEXTO})
    assert pedido.status_code == 200, pedido.get_data(as_text=True)
    aberto = client.get(f"/api/agente-bi/dashboards/{identificador}/consulta")
    assert aberto.status_code == 200
    corpo = aberto.get_json()
    assert corpo["continuidade"]["situacao"] == "esclarecimento"
    assert corpo["continuidade"]["esclarecimento"]["parametros"] == ["faixa"]
    assert TEXTO not in json.dumps(corpo["continuidade"], ensure_ascii=False)
    plano_id = corpo["continuidade"]["plano_id"]
    retomado = client.post(
        f"/api/agente-bi/dashboards/{identificador}/evolucao",
        json={"plano_id": plano_id, "parametros": {"faixa": "norte"}},
    )
    assert retomado.status_code == 201, retomado.get_data(as_text=True)
    assert retomado.get_json()["situacao"] == "aplicada"
    assert retomado.get_json()["chamada_anthropic"] is False
    novo = retomado.get_json()["dashboard"]["id"]
    reaberto = client.get(f"/api/agente-bi/dashboards/{novo}/consulta")
    assert reaberto.status_code == 200
    assert reaberto.get_json()["continuidade"]["situacao"] == "validado"
    assert reaberto.get_json()["continuidade"]["plano_id"] == plano_id
    assert reaberto.get_json()["consulta"]["graficos"][0]["resultado"] == "2"
    assert "norte" not in json.dumps(reaberto.get_json()["continuidade"], ensure_ascii=False)
    assert estado["n"] == 1
    with app.app_context():
        assert IaChamadaTentativa.query.filter_by(usuario_id=usuario_id).count() == 1


def test_interface_retoma_esclarecimento_depois_da_recarga(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from tests.test_agente_bi_dashboard import _documento_browser
    from tests.test_agente_bi_page import _client, _launch_browser

    web = _client(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    documento = _documento_browser(web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True))
    documento = documento.replace(
        """consulta: consultaAberta()
            };""",
        """consulta: consultaAberta(),
                continuidade: {
                  situacao: "esclarecimento",
                  plano_id: 42,
                  esclarecimento: {
                    motivo: "informar_parametro",
                    pergunta: "Informe o valor de faixa. Não vou inventar esse valor.",
                    parametros: ["faixa"]
                  }
                }
            };""",
        1,
    )
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        pagina = browser.new_page(viewport={"width": 1280, "height": 900})
        pagina.set_content(documento, wait_until="domcontentloaded")
        pagina.get_by_role("button", name="Abrir revisão 1 de painel-fretes").click()
        pagina.wait_for_selector("[data-resultado='25.25']")
        assert "Informe o valor de faixa" in pagina.locator("#agente-bi-dashboard-evolucao-resposta").inner_text()
        pagina.get_by_role("textbox", name="Valor de faixa").fill("norte")
        pagina.get_by_role("button", name="Aplicar pedido").click()
        pagina.wait_for_function("() => window.__evolucaoPosts.length === 1")
        pedido = pagina.evaluate("() => window.__evolucaoPosts[0]")
        assert pedido == {"plano_id": 42, "parametros": {"faixa": "norte"}}
        assert "texto" not in pedido
        browser.close()


def _explicar(escopo, identificador, texto=PERGUNTA):
    from tests.test_agente_bi_explicacao import _transporte

    transporte, estado = _transporte(_factual)
    resposta = aplicar_pedido(escopo, identificador, {"texto": texto}, transporte=transporte)
    assert resposta["situacao"] == "explicacao"
    return resposta, estado["explicacao"]


def _alternar(primeiro, segundo):
    estado = {"n": 0}

    def transporte(pedido):
        estado["n"] += 1
        mensagem = json.loads(pedido["messages"][0]["content"])
        if mensagem.get("intencao") == TEXTO_MEDIA:
            return _resposta(segundo(mensagem))
        return _resposta(primeiro(mensagem))

    return transporte, estado


def _sem_prosa(documento, trecho: str) -> None:
    assert trecho not in json.dumps(documento, ensure_ascii=False)
