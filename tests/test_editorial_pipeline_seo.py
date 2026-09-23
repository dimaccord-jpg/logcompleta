"""Pipeline editorial 207A: intenção, SEO e compatibilidade. Sem chamada real a Gemini."""
import importlib
import inspect
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

from app.editorial_metadata import (
    anexar_editorial_em_assets,
    apresentacao_editorial,
    completar_metadados_editoriais,
    derivar_meta_description,
    rebaixar_h1,
    resolver_habilidade,
    resolver_intencao_editorial,
)
from app.run_julia_agente_pipeline import _montar_prompt_imagem_contextual
from app.run_julia_agente_qualidade import validar_artigo
from app.run_julia_agente_redacao import gerar_artigo_completo, gerar_conteudo, gerar_noticia_curta
from app.services.agent_service import executar_artigo_manual_sincrono


def _artigo_modelo(**extra):
    base = {
        "titulo_julia": "Impacto pratico na malha de frete",
        "subtitulo": "O que muda na operacao quando o prazo oscila",
        "resumo_julia": "A variacao de prazo pede revisao de janela, capacidade e custo por rota.",
        "conteudo_completo": "<h1>Impacto</h1><p>Contexto.</p><p>Efeito.</p><p>Implicacao.</p><p>Observar.</p>",
        "prompt_imagem": "Warehouse dock with freight planners reviewing a route board, no text",
        "cta": "Receba um diagnostico gratuito",
        "objetivo_lead": "contato_comercial",
        "referencias": "Fonte operacional",
        "meta_description": "Como a oscilacao de prazo altera janela, capacidade e custo logistico.",
        "alt_imagem": "Equipe logística revisando um painel de rotas no armazém",
        "tema": "prazo de frete",
        "perfil_interesse": "Analista",
        "intencao_busca": "",
        "habilidade_relacionada": "",
    }
    base.update(extra)
    return base


def test_noticia_automatica_permanece_news_mesmo_com_titulo_de_busca():
    assert resolver_intencao_editorial(
        tipo_missao="noticia",
        titulo="O que é cubagem?",
        explicita="evergreen",
    ) == "news"


def test_artigo_sem_marcador_cai_em_analysis_e_busca_em_evergreen():
    assert resolver_intencao_editorial(
        tipo_missao="artigo",
        titulo="Revisão de capacidade na malha",
    ) == "analysis"
    assert resolver_intencao_editorial(
        tipo_missao="artigo",
        titulo="Como funciona o GRIS?",
    ) == "evergreen"
    assert resolver_intencao_editorial(
        tipo_missao="artigo",
        titulo="O que é ad valorem?",
        explicita="analysis",
    ) == "analysis"
    assert resolver_intencao_editorial(
        tipo_missao="artigo",
        titulo="[evergreen] Rotina de conferencia",
    ) == "evergreen"


def test_habilidade_valida_vem_do_shell_e_invalida_nao_entra():
    valida = resolver_habilidade("auditar_cobrancas")
    assert valida is not None
    assert valida["path"] == "/auditoria-frete"
    assert resolver_habilidade("contratar_frete_magico") is None
    assert resolver_habilidade("/fretes")["id"] == "analisar_fretes"


def test_conteudo_antigo_sem_metadados_usa_titulo_e_resumo():
    antigo = SimpleNamespace(
        tipo="artigo",
        titulo_julia="Título já publicado",
        resumo_julia="Resumo já publicado da matéria.",
        subtitulo="Sub",
        assets_canais_json='{"imagem_status":"sucesso","imagem_url_final":"/media/generated/x.png"}',
        cta="Fale com um especialista",
    )
    seo = apresentacao_editorial(antigo)
    assert seo["titulo"] == "Título já publicado"
    assert seo["meta_description"] == "Resumo já publicado da matéria."
    assert seo["schema_type"] == "NewsArticle"
    assert seo["habilidade_href"] == ""
    assert seo["alt"].startswith("Imagem ilustrativa de")
    assert "Título já publicado" in seo["alt"]


def test_news_analysis_evergreen_preenchem_seo_sem_nova_chamada(monkeypatch):
    chamadas = {"n": 0}

    def _contar(client, prompt, tipo):
        chamadas["n"] += 1
        chamadas["tipo"] = tipo
        chamadas["prompt"] = prompt
        if tipo == "noticia":
            return (
                {
                    "titulo_julia": "Porto atrasa janela de atracacao",
                    "resumo_julia": "O atraso foi confirmado pela fonte. A fila de navios aumenta o prazo. Operadores revisam a janela do dia.",
                    "prompt_imagem": "Port queue at dusk, no text",
                    "meta_description": "Atraso confirmado na atracação e efeito imediato no prazo portuário.",
                    "alt_imagem": "Fila de navios aguardando atracação",
                    "tema": "atracação",
                    "perfil_interesse": "operador",
                    "intencao_busca": "nao usar",
                    "habilidade_relacionada": "auditar_cobrancas",
                },
                None,
            )
        return (_artigo_modelo(), None)

    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", lambda _tipo: object())
    monkeypatch.setattr("app.run_julia_agente_redacao._chamar_modelo", _contar)

    news = gerar_noticia_curta("O que é cubagem?", "Fonte", "https://example.com/n")
    assert chamadas["n"] == 1
    assert chamadas["tipo"] == "noticia"
    assert "NOTÍCIA RÁPIDA" in chamadas["prompt"]
    assert news["intencao_editorial"] == "news"
    assert news["editorial"]["intencao_busca"] == ""
    assert news["editorial"]["habilidade_id"] == "auditar_cobrancas"
    assert news["cta"] == "Auditar cobranças de frete"
    assert news["alt_imagem"] == "Fila de navios aguardando atracação"

    analysis = gerar_artigo_completo("Revisão de capacidade", "Fonte", "https://example.com/a")
    assert chamadas["n"] == 2
    assert "aplicação prática" in chamadas["prompt"]
    assert analysis["intencao_editorial"] == "analysis"
    assert analysis["editorial"]["perfil"] == "analista"
    assert "<h1" not in analysis["conteudo_completo"].lower()
    assert analysis["conteudo_completo"].lower().startswith("<h2")
    assert analysis["cta"] == ""
    assert analysis["editorial"]["meta_description"]
    assert analysis["editorial"]["meta_description"] != analysis["titulo_julia"]

    evergreen = gerar_artigo_completo(
        "Como funciona a cubagem?",
        "Fonte",
        "https://example.com/e",
        intencao_editorial="evergreen",
    )
    assert chamadas["n"] == 3
    assert "EVERGREEN" in chamadas["prompt"]
    assert evergreen["editorial"]["intencao"] == "evergreen"
    assert "cubagem" in evergreen["editorial"]["intencao_busca"].lower()


def test_artigo_sem_habilidade_nao_ganha_cta_e_habilidade_invalida_sai(monkeypatch):
    respostas = iter([
        _artigo_modelo(habilidade_relacionada="", cta="Fale com um especialista agora"),
        _artigo_modelo(habilidade_relacionada="contratar_frete_magico", cta="Compre agora"),
    ])

    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", lambda _tipo: object())
    monkeypatch.setattr(
        "app.run_julia_agente_redacao._chamar_modelo",
        lambda *args, **kwargs: (next(respostas), None),
    )

    sem_relacao = gerar_artigo_completo("Capacidade da malha", "Fonte", "https://example.com/1")
    assert sem_relacao["cta"] == ""
    assert sem_relacao["editorial"]["habilidade_id"] == ""

    invalida = gerar_artigo_completo("Outra capacidade", "Fonte", "https://example.com/2")
    assert invalida["cta"] == ""
    assert invalida["editorial"]["habilidade_id"] == ""


def test_artigo_com_habilidade_valida_aponta_para_destino_existente(monkeypatch):
    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", lambda _tipo: object())
    monkeypatch.setattr(
        "app.run_julia_agente_redacao._chamar_modelo",
        lambda *args, **kwargs: (
            _artigo_modelo(habilidade_relacionada="comparar_tabelas", objetivo_lead=""),
            None,
        ),
    )
    out = gerar_artigo_completo("Como analisar uma tabela de frete?", "Fonte", "https://example.com/3")
    assert out["editorial"]["habilidade_id"] == "comparar_tabelas"
    assert out["cta"] == "Comparar tabelas"
    assert resolver_habilidade(out["editorial"]["habilidade_id"])["path"] == "/agente-compara"


def test_cta_ausente_passa_na_qualidade_e_headings_descem_para_h2():
    conteudo = _artigo_modelo(cta="", objetivo_lead="")
    conteudo["resumo_julia"] = (
        "A oscilacao de prazo pede revisao de janela, capacidade e custo por rota antes da proxima saida."
    )
    conteudo["conteudo_completo"] = (
        "<h1>Impacto</h1>"
        "<p>O contexto operacional mostra variacao de prazo nas rotas de maior volume, com efeito direto na janela de expedicao e na ocupacao da frota.</p>"
        "<p>O impacto aparece no custo por embarque e no nivel de servico quando a mesma oscilacao se repete ao longo da semana.</p>"
        "<p>Na pratica, a operacao precisa revisar capacidade, priorizar rotas criticas e registrar onde o prazo deixa de cumprir o combinado.</p>"
        "<p>Os pontos de observacao sao cumprimento de janela, custo por rota e volume represado antes de concluir qualquer ajuste.</p>"
    )
    conteudo["url_imagem"] = "https://example.com/img.png"
    conteudo["link"] = "https://example.com/artigo"
    conteudo = completar_metadados_editoriais(
        conteudo,
        tipo_missao="artigo",
        titulo_pauta="Revisão operacional",
    )
    ok, erros = validar_artigo(conteudo)
    assert ok is True, erros
    assert rebaixar_h1("<h1 class=\"x\">Secao</h1>") == "<h2 class=\"x\">Secao</h2>"


def test_prompt_de_imagem_fica_semantico_e_nao_dispara_geracao():
    pauta = SimpleNamespace(titulo_original="Cubagem de carga", fonte="Portal")
    conteudo = {
        "titulo_julia": "Como funciona a cubagem",
        "resumo_julia": "A cubagem converte volume em peso taxado.",
        "conteudo_completo": "Explica o cálculo de cubagem na tabela.",
        "intencao_editorial": "evergreen",
        "tema": "cubagem",
        "prompt_imagem": "Freight cartons measured on a warehouse scale, realistic photo",
    }
    prompt = _montar_prompt_imagem_contextual(conteudo, pauta, "artigo")
    assert "semantically tied to the theme" in prompt
    assert "No written text" in prompt
    assert "evergreen logistics explainer" in prompt
    assert "cubagem" in prompt.lower()


def test_assets_preservam_observabilidade_ao_anexar_editorial():
    bruto = json.dumps({"imagem_status": "sucesso", "imagem_provider": "gemini"})
    editorial = {
        "intencao": "analysis",
        "tema": "prazo",
        "perfil": "gestor",
        "intencao_busca": "",
        "habilidade_id": "",
        "titulo": "Titulo",
        "meta_description": "Descricao util do prazo logistico.",
        "alt_imagem": "Painel de prazos",
    }
    saida = json.loads(anexar_editorial_em_assets(bruto, editorial))
    assert saida["imagem_status"] == "sucesso"
    assert saida["imagem_provider"] == "gemini"
    assert saida["editorial"]["intencao"] == "analysis"


def test_automacao_de_noticia_e_artigo_manual_nao_trocam_de_tipo(monkeypatch):
    vistos = []

    def _client(tipo):
        vistos.append(tipo)
        return None

    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", _client)
    news = gerar_conteudo(
        "O que é cubagem?",
        "Fonte",
        "https://example.com/n",
        "noticia",
        intencao_editorial="evergreen",
    )
    artigo = gerar_conteudo(
        "Revisão de capacidade",
        "Fonte",
        "https://example.com/a",
        "artigo",
    )
    assert vistos == ["noticia", "artigo"]
    assert news["intencao_editorial"] == "news"
    assert artigo["intencao_editorial"] == "analysis"
    assert artigo["redacao_fallback"] is True
    assert 'tipo_missao_forcado="artigo"' in inspect.getsource(executar_artigo_manual_sincrono)


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    sys.modules.pop("app.web", None)
    return importlib.import_module("app.web")


class _FakeQuery:
    def __init__(self, noticia=None, noticias=None):
        self._noticia = noticia
        self._noticias = noticias or []

    def get_or_404(self, _id):
        return self._noticia

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._noticias


def _noticia_pagina(**extra):
    base = dict(
        id=7,
        tipo="artigo",
        titulo_julia="Título estável",
        titulo_original="[evergreen] O que é ad valorem?",
        link="https://example.com/fonte",
        fonte="Fonte",
        status_publicacao="publicado",
        publicado_em=datetime.now(timezone.utc).replace(tzinfo=None),
        data_publicacao=datetime(2024, 5, 2, 10, 0),
        resumo_julia="Resumo estável já salvo.",
        conteudo_completo="<h1>Secao interna</h1><p>Corpo antigo.</p>",
        subtitulo="Sub",
        referencias="Ref",
        cta="CTA legado da matéria",
        objetivo_lead="contato",
        url_imagem="https://example.com/capa.jpg",
        assets_canais_json=None,
    )
    base.update(extra)
    return SimpleNamespace(**base)


def test_pagina_individual_renderiza_antigo_e_novo(monkeypatch):
    web = _load_web_module()
    antigo = _noticia_pagina()
    novo = _noticia_pagina(
        id=8,
        titulo_julia="Como funciona a cubagem",
        titulo_original="Como funciona a cubagem",
        resumo_julia="Resumo longo que não deve substituir a meta nova.",
        conteudo_completo="<h1>Conceito</h1><p>A cubagem converte volume.</p>",
        cta="",
        objetivo_lead="",
        assets_canais_json=json.dumps(
            {
                "imagem_status": "sucesso",
                "editorial": {
                    "intencao": "evergreen",
                    "tema": "cubagem",
                    "perfil": "analista",
                    "intencao_busca": "como funciona a cubagem",
                    "habilidade_id": "analisar_fretes",
                    "titulo": "Como funciona a cubagem",
                    "meta_description": "A cubagem transforma o volume da carga em peso para a tabela de frete.",
                    "alt_imagem": "Caixas medidas para cálculo de cubagem",
                },
            }
        ),
    )

    def _get_or_404(noticia_id):
        return antigo if int(noticia_id) == 7 else novo

    query = _FakeQuery()
    query.get_or_404 = _get_or_404
    monkeypatch.setattr(web, "NoticiaPortal", SimpleNamespace(query=query))
    client = web.app.test_client()

    html_antigo = client.get("/noticia/7").get_data(as_text=True)
    assert client.get("/noticia/7").status_code == 200
    assert "Título estável" in html_antigo
    assert "Resumo estável já salvo." in html_antigo
    assert "NewsArticle" in html_antigo
    assert "CTA legado da matéria" in html_antigo
    assert html_antigo.lower().count("<h1") == 1
    assert "<h2" in html_antigo.lower()
    assert "O que é ad valorem?" in html_antigo
    assert "[evergreen]" not in html_antigo
    assert "/noticia/7" in html_antigo

    html_novo = client.get("/noticia/8").get_data(as_text=True)
    assert "Como funciona a cubagem" in html_novo
    assert "A cubagem transforma o volume da carga em peso para a tabela de frete." in html_novo
    assert "Caixas medidas para cálculo de cubagem" in html_novo
    assert '"@type": "Article"' in html_novo
    assert html_novo.count('href="/fretes"') == html_antigo.count('href="/fretes"') + 1
    assert html_novo.count("Analisar fretes") == html_antigo.count("Analisar fretes") + 1
    assert html_novo.lower().count("<h1") == 1
    assert "<h2" in html_novo.lower()
    assert "/noticia/8" in html_novo


def test_feed_lista_conteudo_antigo_e_novo(monkeypatch):
    web = _load_web_module()
    antigo = SimpleNamespace(
        id=3,
        tipo="noticia",
        titulo_julia="Notícia antiga do feed",
        resumo_julia="Resumo antigo",
        subtitulo=None,
        fonte="Fonte A",
        link="https://example.com/antiga",
        data_publicacao=datetime(2024, 1, 2, 9, 0),
    )
    novo = SimpleNamespace(
        id=4,
        tipo="artigo",
        titulo_julia="Guia novo de cubagem",
        resumo_julia="Resumo novo",
        subtitulo=None,
        fonte="Fonte B",
        link="https://example.com/nova",
        data_publicacao=datetime(2026, 1, 2, 9, 0),
    )
    monkeypatch.setattr(web, "_load_feed_editorial", lambda: [novo, antigo])
    html = web.app.test_client().get("/feed").get_data(as_text=True)
    assert "Notícia antiga do feed" in html
    assert "Guia novo de cubagem" in html
    assert "/noticia/3" in html
    assert "/noticia/4" in html


def test_fallback_nao_publica_marcador_editorial_no_titulo(monkeypatch):
    monkeypatch.setattr("app.run_julia_agente_redacao._client_for_tipo", lambda _tipo: None)

    evergreen = gerar_artigo_completo(
        "[evergreen] Como funciona a cubagem no transporte?",
        "Fonte",
        "https://example.com/fallback-evergreen",
    )
    assert evergreen["redacao_fallback"] is True
    assert evergreen["intencao_editorial"] == "evergreen"
    assert evergreen["titulo_julia"] == "Como funciona a cubagem no transporte?"
    assert evergreen["editorial"]["titulo"] == "Como funciona a cubagem no transporte?"
    assert "[evergreen]" not in evergreen["titulo_julia"]
    assert "[analysis]" not in evergreen["titulo_julia"]

    analysis = gerar_artigo_completo(
        "[analysis] O que é cubagem?",
        "Fonte",
        "https://example.com/fallback-analysis",
    )
    assert analysis["redacao_fallback"] is True
    assert analysis["intencao_editorial"] == "analysis"
    assert analysis["titulo_julia"] == "O que é cubagem?"
    assert "[analysis]" not in analysis["titulo_julia"]
    assert "[evergreen]" not in analysis["editorial"]["titulo"]


def test_meta_description_nao_repete_titulo():
    titulo = "Como funciona a cubagem"
    resumo_util = "A cubagem converte volume em peso taxado."
    subtitulo_util = "Conversão de volume em peso de frete."
    padrao = "Notícia sobre logística e transporte rodoviário."

    meta = derivar_meta_description(titulo, resumo_util, titulo)
    assert meta == resumo_util
    assert meta.casefold() != titulo.casefold()

    meta = derivar_meta_description(
        titulo,
        "  COMO funciona   a cubagem ",
        titulo,
        subtitulo=subtitulo_util,
    )
    assert meta == subtitulo_util

    meta = derivar_meta_description(titulo, titulo, titulo, subtitulo=titulo)
    assert meta == padrao

    pagina = SimpleNamespace(
        tipo="artigo",
        titulo_julia=titulo,
        resumo_julia=titulo,
        subtitulo=titulo,
        assets_canais_json=json.dumps(
            {
                "editorial": {
                    "intencao": "evergreen",
                    "meta_description": titulo,
                    "titulo": titulo,
                }
            }
        ),
    )
    assert apresentacao_editorial(pagina)["meta_description"] == padrao

    pagina.resumo_julia = resumo_util
    assert apresentacao_editorial(pagina)["meta_description"] == resumo_util

    pagina.resumo_julia = titulo
    pagina.subtitulo = subtitulo_util
    assert apresentacao_editorial(pagina)["meta_description"] == subtitulo_util
