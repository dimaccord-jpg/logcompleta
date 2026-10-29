"""SCRUM-258: jornada A+B+C até a seleção aprovada, sem executor e sem Anthropic."""
from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest

from app.agente_bi_api_routes import agente_bi_api_bp
from app.extensions import db, login_manager
from app.infra import get_user_by_id
from app.models import AgenteBiAnalise, AgenteBiDashboard, AgenteBiExecucao, IaChamadaTentativa
from app.services.agente_bi_analisar_service import CONTRATO_ANALISAR
from app.services.agente_bi_descoberta_service import CONTRATO_SELECAO, DescobertaErro, _confirmacoes
from tests.agente_bi_fonte import fonte_sem_docstring_de_modulo
from tests.conftest import seed_usuario
from tests.test_agente_bi_analisar import FORMULA_SOMA, INJECAO, SENTINELA, _movimento
from tests.test_agente_bi_ingestao import _cliente as _cliente_ingestao
from tests.test_agente_bi_ingestao import _entrar, _par
from tests.test_agente_bi_page import (
    GUIDANCE,
    _browser_document,
    _client as _cliente_pagina,
    _launch_browser,
)

ROOT = Path(__file__).resolve().parents[1]


def _postar(client, conteudo: bytes, nome: str):
    tipo = "text/csv" if nome.lower().endswith(".csv") else (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    return client.post(
        "/api/agente-bi/descoberta/preparacao",
        data={"arquivos": (io.BytesIO(conteudo), nome, tipo)},
        content_type="multipart/form-data",
    )


def _json(client, caminho, corpo):
    return client.post(caminho, json=corpo)


def _medida(colunas, dimensao="valor"):
    return next(
        coluna for coluna in colunas if coluna["dimensao_principal"] == dimensao and coluna["arquivo"] == 0
    )


def _coordenada(coluna):
    return {chave: coluna[chave] for chave in ("arquivo", "aba", "regiao", "indice")}


def _referencia(medida):
    return {
        "afirmacoes": [
            {
                "operacao": "soma",
                "grafico": "indicador",
                "medida": _coordenada(medida),
                "agrupamento": None,
                "juncao": False,
                "texto": None,
            }
        ],
        "formulas": [FORMULA_SOMA],
    }


def _confirmadas(resultado):
    return [{"id": item["id"], "confirmada": True} for item in resultado["perguntas"]]


def _viaveis(resultado):
    return [
        item["id"]
        for item in resultado["sugestoes"]
        if item["status"] == "viavel" and item["grafico"] and item["juncao"] == "nao_executada"
    ]


def _descobrir(client, dataset_id, pedido):
    resposta = _json(client, "/api/agente-bi/descoberta", {"dataset_id": dataset_id, "pedido": pedido})
    assert resposta.status_code == 201, resposta.get_data(as_text=True)
    return resposta.get_json()


def _aprovar(client, analise_id, sugestoes, resultado, *, confirmacao=True):
    return _json(
        client,
        "/api/agente-bi/descoberta/selecao",
        {
            "analise_id": analise_id,
            "sugestoes": sugestoes,
            "perguntas": _confirmadas(resultado),
            "confirmacao": confirmacao,
        },
    )


def test_jornada_a_b_c_limite_persistencia_e_isolamento(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)
    with app.app_context():
        conta, franquia, usuario = _par("descoberta-jornada", "descoberta.jornada@test.com")
        outro_conta, _outro_franquia, outro = _par("descoberta-outro", "descoberta.outro@test.com")
        colega = seed_usuario(franquia.id, conta.id, email="descoberta.colega@test.com")
        usuario_id = usuario.id
        outro_id = outro.id
        colega_id = colega.id
        conta_id = conta.id
        outro_conta_id = outro_conta.id
        tentativas = IaChamadaTentativa.query.count()
        paineis = AgenteBiDashboard.query.count()
        execucoes = AgenteBiExecucao.query.count()

    def proibido(*_args, **_kwargs):
        raise AssertionError("anthropic")

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.chamar_anthropic_governado",
        proibido,
    )
    _entrar(client, usuario_id)

    preparacao = _postar(client, _movimento(), "movimento.xlsx")
    assert preparacao.status_code == 201
    preparado = preparacao.get_json()
    texto_preparado = preparacao.get_data(as_text=True)
    assert preparado["estado"] == "preparado"
    assert preparado["dataset"]["usuario_id"] == usuario_id
    assert preparado["dataset"]["versao"] >= 1
    assert "dados_tipados" not in texto_preparado
    assert SENTINELA not in texto_preparado
    assert FORMULA_SOMA not in texto_preparado
    dataset_id = preparado["dataset"]["id"]
    versao = preparado["dataset"]["versao"]
    medida = _medida(preparado["colunas"])
    assert medida["evidencias"]

    modalidade_a = _descobrir(client, dataset_id, {"modalidades": ["A"]})
    resultado_a = modalidade_a["resultado"]
    assert resultado_a["contrato"] == CONTRATO_ANALISAR
    assert resultado_a["provedor"] == "simulado"
    assert resultado_a["chamada_anthropic"] is False
    assert resultado_a["apto_anthropic"] is False
    assert resultado_a["modalidades"] == ["A"]
    assert modalidade_a["usuario_id"] == usuario_id
    assert modalidade_a["versao"] == versao
    assert resultado_a["descobertas"]
    assert any(item["evidencias"] for item in resultado_a["sugestoes"])
    assert all(item["origem"] == "dados" for item in resultado_a["sugestoes"])
    assert any(item["status"] == "viavel" for item in resultado_a["sugestoes"])
    assert resultado_a["perguntas"]
    assert SENTINELA not in json.dumps(modalidade_a)

    modalidade_b = _descobrir(
        client,
        dataset_id,
        {"modalidades": ["B"], "referencia": _referencia(medida)},
    )
    resultado_b = modalidade_b["resultado"]
    assert resultado_b["modalidades"] == ["B"]
    assert any(item["origem"] == "referencia" and item["status"] == "viavel" for item in resultado_b["sugestoes"])
    assert any(item["codigo"] == "referencia_compativel" for item in resultado_b["critica_referencia"])
    assert FORMULA_SOMA not in json.dumps(modalidade_b)
    assert SENTINELA not in json.dumps(modalidade_b)

    modalidade_c = _descobrir(client, dataset_id, {"modalidades": ["C"], "texto": "Quero a soma."})
    assert modalidade_c["resultado"]["modalidades"] == ["C"]
    assert any(item["operacao"] == "soma" and item["status"] == "viavel" for item in modalidade_c["resultado"]["sugestoes"])
    assert "Quero a soma." not in json.dumps(modalidade_c["resultado"])

    injecao = _descobrir(client, dataset_id, {"modalidades": ["C"], "texto": INJECAO})
    assert injecao["resultado"]["instrucoes_nao_confiaveis"]["detectadas"] is True
    assert injecao["resultado"]["instrucoes_nao_confiaveis"]["reproduzidas"] is False
    assert INJECAO not in json.dumps(injecao)

    combinada = _descobrir(
        client,
        dataset_id,
        {
            "modalidades": ["C", "A", "B"],
            "texto": INJECAO + " Quero a evolucao.",
            "referencia": {
                "afirmacoes": [{**_referencia(medida)["afirmacoes"][0], "juncao": True, "texto": INJECAO}],
                "formulas": [FORMULA_SOMA],
            },
        },
    )
    resultado_abc = combinada["resultado"]
    assert resultado_abc["modalidades"] == ["A", "B", "C"]
    assert any(item["status"] == "viavel" and item["origem"] == "dados" for item in resultado_abc["sugestoes"])
    assert all(item["juncao"] == "nao_executada" for item in resultado_abc["sugestoes"])
    assert "juncao_nao_confirmada" in {item["codigo"] for item in resultado_abc["critica_referencia"]}
    assert resultado_abc["instrucoes_nao_confiaveis"]["reproduzidas"] is False
    bruto_abc = json.dumps(combinada)
    assert INJECAO not in bruto_abc
    assert FORMULA_SOMA not in bruto_abc
    assert SENTINELA not in bruto_abc

    viaveis = _viaveis(resultado_a)
    assert viaveis
    excedente = _aprovar(client, modalidade_a["analise_id"], [f"s{indice}" for indice in range(1, 8)], resultado_a)
    assert excedente.status_code == 422
    assert excedente.get_json()["codigo"] == "limite_graficos"

    sem_pergunta = _json(
        client,
        "/api/agente-bi/descoberta/selecao",
        {
            "analise_id": modalidade_a["analise_id"],
            "sugestoes": viaveis[:1],
            "perguntas": [],
            "confirmacao": True,
        },
    )
    assert sem_pergunta.status_code == 422
    assert sem_pergunta.get_json()["codigo"] == "selecao_invalida"

    sem_confirmacao = _aprovar(client, modalidade_a["analise_id"], viaveis[:1], resultado_a, confirmacao=False)
    assert sem_confirmacao.status_code == 422
    assert sem_confirmacao.get_json()["codigo"] == "confirmacao_ausente"

    aprovada = _aprovar(client, modalidade_a["analise_id"], viaveis[:6], resultado_a)
    assert aprovada.status_code == 201, aprovada.get_data(as_text=True)
    selecao = aprovada.get_json()["selecao"]
    assert selecao["usuario_id"] == usuario_id
    assert selecao["dataset_id"] == dataset_id
    assert selecao["versao"] == versao
    assert selecao["etapa"] == "selecao"
    assert selecao["estado"] == "concluida"
    assert selecao["contrato_versao"] == CONTRATO_SELECAO
    assert 1 <= selecao["graficos"] <= 6
    assert all(item["status"] == "viavel" for item in selecao["resultado"]["graficos"])
    assert all(item["juncao"] == "nao_executada" for item in selecao["resultado"]["graficos"])
    assert selecao["resultado"]["executavel"] == "somente_viavel"
    assert selecao["resultado"]["chamada_anthropic"] is False
    assert INJECAO not in aprovada.get_data(as_text=True)

    repetida = _aprovar(client, selecao["id"], viaveis[:1], resultado_a)
    assert repetida.status_code == 422
    assert repetida.get_json()["codigo"] == "analise_indisponivel"

    with app.app_context():
        gravada = db.session.get(AgenteBiAnalise, selecao["id"])
        assert gravada.usuario_id == usuario_id
        assert gravada.dataset_versao == versao
        assert gravada.etapa_jornada == "selecao"
        assert gravada.contrato_versao == CONTRATO_SELECAO
        assert IaChamadaTentativa.query.count() == tentativas
        assert AgenteBiDashboard.query.count() == paineis
        assert AgenteBiExecucao.query.count() == execucoes

    leitura = client.get(f"/api/agente-bi/analises/{selecao['id']}")
    assert leitura.status_code == 200
    assert leitura.get_json()["analise"]["resultado"]["dataset_versao"] == versao
    lista = client.get("/api/agente-bi/analises")
    assert lista.status_code == 200
    persistida = next(item for item in lista.get_json()["analises"] if item["id"] == selecao["id"])
    assert persistida["usuario_id"] == usuario_id
    assert persistida["versao"] == versao
    assert persistida["resultado"]["dataset_versao"] == versao
    assert persistida["resultado"]["modalidades"]
    assert 1 <= len(persistida["resultado"]["graficos"]) <= 6

    for pessoa in (outro_id, colega_id):
        _entrar(client, pessoa)
        assert client.get(f"/api/agente-bi/analises/{selecao['id']}").status_code == 404
        assert client.get(f"/api/agente-bi/analises/{modalidade_a['analise_id']}").status_code == 404
        alheias = client.get("/api/agente-bi/analises")
        assert alheias.status_code == 200
        assert all(item["id"] not in {selecao["id"], modalidade_a["analise_id"]} for item in alheias.get_json()["analises"])
        alheia = _aprovar(client, modalidade_a["analise_id"], viaveis[:1], resultado_a)
        assert alheia.status_code == 404
        assert _json(client, "/api/agente-bi/descoberta", {"dataset_id": dataset_id, "pedido": {"modalidades": ["A"]}}).status_code == 404
    assert outro_conta_id != conta_id


def test_condicional_e_lacuna_nao_sao_aprovadas(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)
    with app.app_context():
        _conta, _franquia, usuario = _par("descoberta-lacuna", "descoberta.lacuna@test.com")
        usuario_id = usuario.id
    _entrar(client, usuario_id)

    lacuna = _postar(client, b"alfa,beta\nA,B\nC,D\nE,F\n", "lacuna.csv")
    assert lacuna.status_code == 201
    dataset_lacuna = lacuna.get_json()["dataset"]["id"]
    descoberta_lacuna = _descobrir(
        client,
        dataset_lacuna,
        {"modalidades": ["C"], "texto": "Quero a evolucao."},
    )
    dependentes = [
        item for item in descoberta_lacuna["resultado"]["sugestoes"] if item["status"] == "dependente_de_novos_dados"
    ]
    assert dependentes
    assert all(item["grafico"] is None for item in dependentes)
    recusa_lacuna = _aprovar(
        client,
        descoberta_lacuna["analise_id"],
        [dependentes[0]["id"]],
        descoberta_lacuna["resultado"],
    )
    assert recusa_lacuna.status_code == 422
    assert recusa_lacuna.get_json()["codigo"] == "sugestao_nao_executavel"

    condicional = _postar(client, b"dia,medida\n01/02/2024,10\n13/02/2024,4\n", "datas.csv")
    assert condicional.status_code == 201
    descoberta_condicional = _descobrir(
        client,
        condicional.get_json()["dataset"]["id"],
        {"modalidades": ["C"], "texto": "Quero a evolução do total."},
    )
    condicionais = [
        item for item in descoberta_condicional["resultado"]["sugestoes"] if item["status"] == "condicional"
    ]
    assert condicionais
    assert all(item["grafico"] != "linha" for item in descoberta_condicional["resultado"]["sugestoes"])
    recusa_condicional = _aprovar(
        client,
        descoberta_condicional["analise_id"],
        [condicionais[0]["id"]],
        descoberta_condicional["resultado"],
    )
    assert recusa_condicional.status_code == 422
    assert recusa_condicional.get_json()["codigo"] == "sugestao_nao_executavel"
    assert "evolução" not in json.dumps(descoberta_condicional["resultado"])


_CHAVES_DOCUMENTO_SELECAO = {
    "contrato",
    "origem_analise_id",
    "origem_contrato",
    "vinculo_preparacao",
    "dataset_id",
    "dataset_versao",
    "dataset_impressao",
    "modalidades",
    "limite_graficos",
    "graficos",
    "perguntas_confirmadas",
    "executavel",
    "juncao",
    "chamada_anthropic",
    "apto_anthropic",
}


def _marcar(resultado, *, confirmar_primeira=False):
    perguntas = resultado["perguntas"]
    return [
        {"id": item["id"], "confirmada": confirmar_primeira and indice == 0 and len(perguntas) > 1}
        for indice, item in enumerate(perguntas)
    ]


def test_viavel_aprova_com_pergunta_nao_relacionada_pendente(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)
    with app.app_context():
        _conta, _franquia, usuario = _par("descoberta-pendente", "descoberta.pendente@test.com")
        usuario_id = usuario.id
    _entrar(client, usuario_id)

    preparacao = _postar(client, _movimento(), "movimento.xlsx")
    assert preparacao.status_code == 201
    dataset_id = preparacao.get_json()["dataset"]["id"]
    descoberta = _descobrir(client, dataset_id, {"modalidades": ["A"]})
    resultado = descoberta["resultado"]
    viaveis = _viaveis(resultado)
    assert viaveis
    assert all(item["perguntas"] == [] for item in resultado["sugestoes"] if item["status"] == "viavel")
    identificadores = [item["id"] for item in resultado["perguntas"]]
    assert identificadores

    def selecionar(perguntas, sugestoes=None, *, confirmacao=True):
        return _json(
            client,
            "/api/agente-bi/descoberta/selecao",
            {
                "analise_id": descoberta["analise_id"],
                "sugestoes": viaveis[:1] if sugestoes is None else sugestoes,
                "perguntas": perguntas,
                "confirmacao": confirmacao,
            },
        )

    with app.app_context():
        antes = AgenteBiAnalise.query.filter_by(usuario_id=usuario_id).count()

    incompleta = selecionar(
        [{"id": item, "confirmada": False} for item in identificadores[:-1]] if len(identificadores) > 1 else []
    )
    assert incompleta.status_code == 422
    assert incompleta.get_json()["codigo"] == "selecao_invalida"

    if len(identificadores) > 1:
        duplicada = [{"id": identificadores[0], "confirmada": False}, {"id": identificadores[0], "confirmada": True}]
        duplicada.extend({"id": item, "confirmada": False} for item in identificadores[2:])
    else:
        duplicada = [
            {"id": identificadores[0], "confirmada": False},
            {"id": identificadores[0], "confirmada": False},
        ]
    assert selecionar(duplicada).status_code == 422
    assert selecionar(duplicada).get_json()["codigo"] == "selecao_invalida"

    desconhecida = [{"id": item, "confirmada": False} for item in identificadores]
    desconhecida[0] = {"id": "p999", "confirmada": False}
    assert selecionar(desconhecida).status_code == 422
    assert selecionar(desconhecida).get_json()["codigo"] == "selecao_invalida"

    nao_booleana = [{"id": item, "confirmada": False} for item in identificadores]
    nao_booleana[0] = {"id": identificadores[0], "confirmada": 1}
    assert selecionar(nao_booleana).status_code == 422
    assert selecionar(nao_booleana).get_json()["codigo"] == "selecao_invalida"

    sem_confirmacao = selecionar(_marcar(resultado), confirmacao=False)
    assert sem_confirmacao.status_code == 422
    assert sem_confirmacao.get_json()["codigo"] == "confirmacao_ausente"

    excedente = selecionar(_marcar(resultado), [f"s{indice}" for indice in range(1, 8)])
    assert excedente.status_code == 422
    assert excedente.get_json()["codigo"] == "limite_graficos"

    with app.app_context():
        assert AgenteBiAnalise.query.filter_by(usuario_id=usuario_id).count() == antes

    marcadas = _marcar(resultado, confirmar_primeira=True)
    aprovada = selecionar(marcadas, viaveis[:1])
    assert aprovada.status_code == 201, aprovada.get_data(as_text=True)
    selecao = aprovada.get_json()["selecao"]
    confirmadas = [item["id"] for item in marcadas if item["confirmada"] is True]
    pendentes = [item["id"] for item in marcadas if item["confirmada"] is False]
    assert selecao["rodada"] == descoberta["rodada"] + 1
    assert selecao["resultado"]["perguntas_confirmadas"] == confirmadas
    assert set(selecao["resultado"]) == _CHAVES_DOCUMENTO_SELECAO
    assert "pendentes" not in selecao["resultado"]
    assert all(item["status"] == "viavel" for item in selecao["resultado"]["graficos"])

    leitura = client.get(f"/api/agente-bi/analises/{selecao['id']}")
    assert leitura.status_code == 200
    esclarecimentos = leitura.get_json()["analise"]["esclarecimentos"]
    assert esclarecimentos == {
        "perguntas": confirmadas,
        "pendentes": pendentes,
        "confirmadas": not pendentes,
    }
    assert pendentes
    assert set(confirmadas).isdisjoint(pendentes)

    definicao = _json(client, "/api/agente-bi/definicao", {"analise_id": selecao["id"]})
    assert definicao.status_code == 201, definicao.get_data(as_text=True)
    publicada = definicao.get_json()["definicao"]
    assert publicada["contrato_versao"] == "definir.v1"
    assert publicada["rodada"] == selecao["rodada"] + 1
    assert publicada["resultado"]["contrato"] == "definir.v1"
    assert publicada["resultado"]["perguntas_confirmadas"] == confirmadas
    assert publicada["resultado"]["origem_selecao_id"] == selecao["id"]


def test_pergunta_obrigatoria_da_sugestao_nao_confirmada_bloqueia():
    resultado = {
        "perguntas": [{"id": "p1"}, {"id": "p2"}],
        "sugestoes": [
            {"id": "s1", "status": "viavel", "perguntas": []},
            {"id": "s2", "status": "condicional", "perguntas": ["p1"]},
        ],
    }
    bruto = [{"id": "p1", "confirmada": False}, {"id": "p2", "confirmada": False}]
    liberada = _confirmacoes(resultado, ["s1"], bruto)
    assert liberada == {"confirmadas": [], "pendentes": ["p1", "p2"]}

    with pytest.raises(DescobertaErro) as erro:
        _confirmacoes(resultado, ["s2"], bruto)
    assert erro.value.codigo == "perguntas_pendentes"

    confirmada = _confirmacoes(
        resultado,
        ["s2"],
        [{"id": "p1", "confirmada": True}, {"id": "p2", "confirmada": False}],
    )
    assert confirmada == {"confirmadas": ["p1"], "pendentes": ["p2"]}

    with pytest.raises(DescobertaErro) as garantia:
        _confirmacoes(
            {
                "perguntas": [{"id": "p1"}],
                "sugestoes": [{"id": "s1", "status": "viavel", "perguntas": ["p1"]}],
            },
            ["s1"],
            [{"id": "p1", "confirmada": True}],
        )
    assert garantia.value.codigo == "analise_indisponivel"


def test_portas_auth_flag_e_contrato_local(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch, api=False, ingestao=False)
    assert client.post("/api/agente-bi/descoberta/preparacao").status_code == 404
    assert client.post("/api/agente-bi/descoberta", json={}).status_code == 404
    assert client.post("/api/agente-bi/descoberta/selecao", json={}).status_code == 404

    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    assert _postar(client, b"alfa,valor\nA,1\n", "a.csv").status_code == 404
    anonimo = _json(client, "/api/agente-bi/descoberta", {"dataset_id": 1, "pedido": {"modalidades": ["A"]}})
    assert anonimo.status_code == 401

    with app.app_context():
        _conta, _franquia, usuario = _par("descoberta-porta", "descoberta.porta@test.com")
        usuario_id = usuario.id
    monkeypatch.setattr(
        "app.agente_bi_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario: {"permitido": False},
    )
    _entrar(client, usuario_id)
    assert _json(client, "/api/agente-bi/descoberta", {"dataset_id": 1, "pedido": {"modalidades": ["A"]}}).status_code == 403

    fonte_servico = fonte_sem_docstring_de_modulo(ROOT / "app" / "services" / "agente_bi_descoberta_service.py")
    fonte_js = fonte_sem_docstring_de_modulo(ROOT / "app" / "static" / "js" / "agente_bi_descoberta.js")
    fonte_rotas = fonte_sem_docstring_de_modulo(ROOT / "app" / "agente_bi_api_routes.py")
    assert "def " in fonte_rotas
    for fonte in (fonte_servico, fonte_js, fonte_rotas):
        assert "chamar_anthropic" not in fonte
        assert "definir.v1" not in fonte
        assert "AGENTE_BI_ANTHROPIC" not in fonte
    assert "criar_dashboard" not in fonte_servico
    assert "dashboard" not in fonte_js.lower()
    html = (ROOT / "app" / "templates" / "agente_bi.html").read_text(encoding="utf-8")
    assert 'type="file"' not in html
    assert "/api/agente-bi/ingestao" not in html


def test_pagina_preserva_bloqueio_e_so_abre_descoberta_com_flags(monkeypatch):
    web = _cliente_pagina(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_INGESTAO_ENABLED", "1")
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert 'id="agente-bi-descoberta"' in html
    assert "agente_bi_descoberta.js" in html
    assert "catalogo.analitico.v1" in html
    assert "juncoes_executaveis" in html
    assert "O chat continua bloqueado." in html
    assert GUIDANCE in html
    assert "readonly" in html
    assert re.search(r'<button\b[^>]*\bid="agente-bi-send"[^>]*\bdisabled\b', html, re.S)
    upload = re.search(r'<button\b[^>]*\bid="agente-bi-upload-item"[^>]*>', html, re.S).group(0)
    assert "disabled" not in upload
    assert 'type="file"' not in html
    assert "<canvas" not in html
    assert "agentefrete-theme.css" in html
    assert "AgenteBI" in html
    assert 'href="/cleide-bi-frete"' in html

    visitante = _cliente_pagina(monkeypatch, authenticated=False)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_INGESTAO_ENABLED", "1")
    html_visitante = visitante.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert 'id="agente-bi-descoberta"' not in html_visitante
    assert "Autenticação necessária para utilizar o AgenteBI. Faça login para continuar." in html_visitante
    assert 'href="/login?next=/cleide-bi-frete"' in html_visitante
    assert "O envio e a análise de arquivos estarão disponíveis em uma próxima etapa." not in html_visitante
    assert "Envie uma planilha" not in html_visitante
    assert re.search(r'<button\b[^>]*\bid="agente-bi-upload-item"[^>]*\bdisabled\b', html_visitante, re.S)

    bloqueado = _cliente_pagina(monkeypatch, authenticated=True, permitido=False)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_INGESTAO_ENABLED", "1")
    resposta = bloqueado.app.test_client().get("/cleide-bi-frete")
    assert resposta.status_code == 403
    assert 'id="agente-bi-descoberta"' not in resposta.get_data(as_text=True)


def _documento_generico(preparado, **alteracoes):
    dataset = preparado["dataset"]
    corpo = {
        "chave": dataset["chave"],
        "rodada": 40,
        "dataset_id": dataset["id"],
        "versao": dataset["versao"],
        "impressao": dataset["impressao"],
        "etapa": "dados",
        "estado": "preparada",
        "contexto": {"recorte": "uf"},
        "regras_negocio": {"medida": "valor"},
        "esclarecimentos": {},
        "contrato_versao": "persistencia.v1",
    }
    corpo.update(alteracoes)
    return corpo


def _descoberta_fabricada(**alteracoes):
    resultado = {
        "contrato": "analisar.v1",
        "modalidades": ["A"],
        "vinculo_preparacao": "fabricado",
        "chamada_anthropic": False,
        "apto_anthropic": False,
        "trilha": {
            "dataset_id": 1,
            "dataset_versao": 1,
            "dataset_impressao": "a" * 64,
        },
        "sugestoes": [
            {
                "id": "s999",
                "status": "viavel",
                "leitura": "indicador_de_medida",
                "grafico": "barra",
                "operacao": "soma",
                "origem": "dados",
                "regiao": {"arquivo": 0, "aba": 0, "regiao": 0},
                "medida": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 99999},
                "agrupamento": None,
                "segunda_medida": None,
                "evidencias": ["evidencia_inventada"],
                "juncao": "nao_executada",
                "perguntas": [],
                "lacuna": None,
            }
        ],
        "perguntas": [],
    }
    corpo = {
        "etapa": "descoberta",
        "estado": "concluida",
        "contrato_versao": "analisar.v1",
        "resultado": resultado,
    }
    corpo.update(alteracoes)
    return corpo


def test_api_generica_nao_publica_descoberta_fabricada(app, monkeypatch):
    client = _cliente_ingestao(app, monkeypatch)
    with app.app_context():
        _conta, _franquia, usuario = _par("descoberta-ataque", "descoberta.ataque@test.com")
        _outra_conta, _outra_franquia, outro = _par("descoberta-ataque-outro", "descoberta.ataque.outro@test.com")
        usuario_id = usuario.id
        outro_id = outro.id
    _entrar(client, usuario_id)
    preparacao = _postar(client, b"alfa,valor\nA,10\nB,20\nC,30\n", "ataque.csv")
    assert preparacao.status_code == 201
    preparado = preparacao.get_json()
    with app.app_context():
        antes = AgenteBiAnalise.query.count()

    ataque = _json(
        client,
        "/api/agente-bi/analises",
        _documento_generico(preparado, rodada=41, **_descoberta_fabricada()),
    )
    assert ataque.status_code == 422
    assert ataque.get_json()["codigo"] == "contrato_reservado"
    assert "s999" not in ataque.get_data(as_text=True)
    assert "99999" not in ataque.get_data(as_text=True)
    assert "evidencia_inventada" not in ataque.get_data(as_text=True)

    sem_perguntas = _descoberta_fabricada()
    del sem_perguntas["resultado"]["perguntas"]
    assert _json(
        client,
        "/api/agente-bi/analises",
        _documento_generico(preparado, rodada=42, **sem_perguntas),
    ).get_json()["codigo"] == "contrato_reservado"
    assert _json(
        client,
        "/api/agente-bi/analises",
        _documento_generico(
            preparado,
            rodada=43,
            contrato_versao="persistencia.v1",
            resultado={"contrato": "analisar.v1", "sugestoes": [{"id": "s999", "indice": 99999}]},
        ),
    ).get_json()["codigo"] == "contrato_reservado"
    assert _json(
        client,
        "/api/agente-bi/analises",
        _documento_generico(
            preparado,
            rodada=44,
            contexto={"contrato": "contexto.analisar.v1"},
        ),
    ).get_json()["codigo"] == "contrato_reservado"
    assert _json(
        client,
        "/api/agente-bi/analises",
        _documento_generico(preparado, rodada=45, contrato_versao="selecao.descoberta.v1"),
    ).get_json()["codigo"] == "contrato_reservado"
    with app.app_context():
        assert AgenteBiAnalise.query.count() == antes

    legitima = _json(client, "/api/agente-bi/analises", _documento_generico(preparado, rodada=46))
    assert legitima.status_code == 201
    assert legitima.get_json()["analise"]["contrato_versao"] == "persistencia.v1"
    aprovacao = _json(
        client,
        "/api/agente-bi/descoberta/selecao",
        {
            "analise_id": legitima.get_json()["analise"]["id"],
            "sugestoes": ["s999"],
            "perguntas": [],
            "confirmacao": True,
        },
    )
    assert aprovacao.status_code == 422
    assert aprovacao.get_json()["codigo"] == "analise_indisponivel"
    inexistente = _json(
        client,
        "/api/agente-bi/descoberta/selecao",
        {"analise_id": 999999, "sugestoes": ["s999"], "perguntas": [], "confirmacao": True},
    )
    assert inexistente.status_code == 404
    assert "s999" not in client.get("/api/agente-bi/analises").get_data(as_text=True)

    _entrar(client, outro_id)
    assert client.get(f"/api/agente-bi/analises/{legitima.get_json()['analise']['id']}").status_code == 404
    assert all(
        item["id"] != legitima.get_json()["analise"]["id"]
        for item in client.get("/api/agente-bi/analises").get_json()["analises"]
    )


def test_jornada_visual_seleciona_seis_e_mantem_o_chat_bloqueado(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    web = _cliente_pagina(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_INGESTAO_ENABLED", "1")
    renderizado = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    documento = _browser_document(renderizado)
    script = (ROOT / "app" / "static" / "js" / "agente_bi_descoberta.js").read_text(encoding="utf-8")
    sugestoes = []
    for indice in range(1, 8):
        sugestoes.append(
            {
                "id": f"s{indice}",
                "status": "viavel",
                "leitura": "indicador_de_medida",
                "grafico": "indicador",
                "operacao": "soma",
                "origem": "dados",
                "regiao": {"arquivo": 0, "aba": 0, "regiao": 0},
                "medida": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1},
                "agrupamento": None,
                "segunda_medida": None,
                "evidencias": ["tipo_numero_fracionado"],
                "juncao": "nao_executada",
                "perguntas": [],
                "lacuna": None,
            }
        )
    sugestoes.append(
        {
            "id": "s8",
            "status": "condicional",
            "leitura": "esclarecimento_necessario",
            "grafico": None,
            "operacao": None,
            "origem": "dados",
            "regiao": {"arquivo": 0, "aba": 0, "regiao": 0},
            "medida": None,
            "agrupamento": None,
            "segunda_medida": None,
            "evidencias": [],
            "juncao": "nao_executada",
            "perguntas": ["p1"],
            "lacuna": None,
        }
    )
    sugestoes.append(
        {
            "id": "s9",
            "status": "dependente_de_novos_dados",
            "leitura": "lacuna_de_dados",
            "grafico": None,
            "operacao": None,
            "origem": "dados",
            "regiao": {"arquivo": 0, "aba": 0, "regiao": 0},
            "medida": None,
            "agrupamento": None,
            "segunda_medida": None,
            "evidencias": [],
            "juncao": "nao_executada",
            "perguntas": ["p2"],
            "lacuna": "dimensao_temporal_ausente",
        }
    )
    preparacao = {
        "ok": True,
        "estado": "preparado",
        "persistido": True,
        "reutilizado": False,
        "motivo": None,
        "dataset": {"id": 9, "usuario_id": 3, "chave": "ing-teste", "versao": 2, "impressao": "ab"},
        "resumo": {
            "arquivos": 1,
            "abas": 1,
            "regioes": 1,
            "celulas": 4,
            "linhas_elegiveis": 2,
            "formulas": 0,
            "ambigua": True,
        },
        "arquivos": [{"indice": 0, "nome": "fretes.csv", "alertas": []}],
        "abas": [{"arquivo": 0, "indice": 0, "nome": "csv", "estado": "visivel", "linhas": 2, "colunas": 2, "alertas": []}],
        "ambiguidades": [{"codigo": "cabecalho_ambiguo", "arquivo": 0, "aba": 0, "regiao": 0}],
        "alertas": ["regioes_multiplas"],
        "colunas": [
            {
                "arquivo": 0,
                "aba": 0,
                "regiao": 0,
                "indice": 1,
                "coluna_planilha": 2,
                "dimensao_principal": "valor",
                "tipos": ["numero"],
                "evidencias": ["tipo_numero_fracionado"],
                "dominio_inconsistente": False,
            }
        ],
        "regioes": [],
        "qualidade": {"datas_ambiguas": 0, "formulas_sem_cache": 0, "dominios_inconsistentes": 0, "valores_ausentes": 1},
    }
    descoberta = {
        "ok": True,
        "analise_id": 15,
        "usuario_id": 3,
        "dataset_id": 9,
        "versao": 2,
        "impressao": "ab",
        "rodada": 1,
        "etapa": "descoberta",
        "contrato_versao": "analisar.v1",
        "resultado": {
            "contrato": "analisar.v1",
            "modalidades": ["A", "B", "C"],
            "descobertas": [
                {"id": "d1", "codigo": "regiao_com_medida", "regiao": {"arquivo": 0, "aba": 0, "regiao": 0}, "relacao": None}
            ],
            "sugestoes": sugestoes,
            "perguntas": [
                {
                    "id": "p1",
                    "codigo": "confirmar_relacao_candidata",
                    "sobre": "relacionamento",
                    "regiao": {"arquivo": 0, "aba": 0, "regiao": 0},
                    "coluna": None,
                    "relacao": 0,
                }
            ],
            "critica_referencia": [{"id": "c1", "codigo": "referencia_compativel", "reproduzida": False}],
            "instrucoes_nao_confiaveis": {"tratadas_como_dado": True, "detectadas": True, "reproduzidas": False},
        },
    }
    selecao = {
        "ok": True,
        "selecao": {
            "id": 16,
            "usuario_id": 3,
            "dataset_id": 9,
            "versao": 2,
            "graficos": 6,
            "etapa": "selecao",
            "contrato_versao": "selecao.descoberta.v1",
        },
    }
    erros = []

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda erro: erros.append(str(erro)))
        page.set_content(documento, wait_until="domcontentloaded")
        page.evaluate(
            """(pacotes) => {
              window.__bi = [];
              window.fetch = (url, opts) => {
                const alvo = String(url);
                let payload = { ok: false, codigo: "nao_encontrado" };
                if (alvo.indexOf("/analises") !== -1) payload = pacotes.analises;
                else if (alvo.indexOf("/descoberta/preparacao") !== -1) payload = pacotes.preparacao;
                else if (alvo.indexOf("/descoberta/selecao") !== -1) payload = pacotes.selecao;
                else if (alvo.indexOf("/descoberta") !== -1) payload = pacotes.descoberta;
                window.__bi.push({ url: alvo, body: opts && typeof opts.body === "string" ? opts.body : "" });
                return Promise.resolve({
                  ok: true,
                  status: 200,
                  json: () => Promise.resolve(payload)
                });
              };
            }""",
            {
                "preparacao": preparacao,
                "descoberta": descoberta,
                "selecao": selecao,
                "analises": {"ok": True, "analises": []},
            },
        )
        page.add_script_tag(content=script)
        page.wait_for_function("() => window.__bi.some(item => item.url.indexOf('/analises') !== -1)")

        assert page.locator("#agente-bi-input").evaluate("el => el.readOnly") is True
        assert page.locator("#agente-bi-send").is_disabled()
        assert page.locator("#agente-bi-modalidades").evaluate("el => el.disabled") is True
        assert page.locator("#agente-bi-arquivo").count() == 1

        page.locator("#agente-bi-arquivo").set_input_files(
            files=[{"name": "fretes.csv", "mimeType": "text/csv", "buffer": b"alfa,valor\nA,10\n"}]
        )
        page.wait_for_function("() => document.getElementById('agente-bi-modalidades').disabled === false")
        assert page.locator("#agente-bi-descoberta-form").is_hidden()
        page.locator("#agente-bi-prosseguir").click()
        page.wait_for_selector("#agente-bi-descoberta-form:not([hidden])")
        texto_lista = page.locator("#agente-bi-preparacao-lista").inner_text()
        assert "Versão do dataset" in texto_lista
        assert "2" in texto_lista
        assert "cabecalho ambiguo" in texto_lista
        assert "Versão 2" in page.locator("#agente-bi-descoberta-status").inner_text()
        assert page.locator("#agente-bi-input").evaluate("el => el.readOnly") is True

        page.locator("#agente-bi-modalidade-b").check()
        page.locator("#agente-bi-modalidade-c").check()
        page.locator('[data-campo="operacao"]').select_option("soma")
        page.locator('[data-campo="grafico"]').select_option("indicador")
        page.locator('[data-campo="medida"]').select_option("0")
        page.locator("#agente-bi-formulas").fill(FORMULA_SOMA)
        page.locator("#agente-bi-texto").fill("INSTRUCAO-SIGILOSA")
        page.locator("#agente-bi-examinar").click()
        page.wait_for_selector("[data-status='viavel']")

        assert page.evaluate("() => document.activeElement.id") == "agente-bi-resultado-titulo"
        assert page.locator("[data-status='viavel']").count() == 7
        assert page.locator("[data-status='condicional'] input").count() == 0
        assert "não é uma análise executável aprovada" in page.locator("[data-status='condicional']").inner_text()
        assert page.locator("[data-status='dependente_de_novos_dados'] input").count() == 0
        assert "não é um gráfico aprovado" in page.locator("[data-status='dependente_de_novos_dados']").inner_text()
        assert "tipo numero fracionado" in page.locator("[data-status='viavel']").first.inner_text()
        assert "não executa junção" in page.locator("#agente-bi-perguntas").inner_text()
        assert "não foi reproduzida" in page.locator("#agente-bi-instrucao").inner_text()
        assert "INSTRUCAO-SIGILOSA" not in page.locator("#agente-bi-resultado").inner_text()
        assert FORMULA_SOMA not in page.locator("#agente-bi-resultado").inner_text()
        assert page.locator("#agente-bi-aprovar").is_disabled()

        caixas = page.locator(".agente-bi-grafico")
        for indice in range(6):
            caixas.nth(indice).check()
        assert caixas.nth(6).is_disabled()
        assert "6 de 6" in page.locator("#agente-bi-selecao-contagem").inner_text()
        assert page.locator("#agente-bi-aprovar").is_disabled()
        assert page.locator("#agente-bi-perguntas h4").inner_text() == "Esclarecimentos pendentes"
        assert page.locator(".agente-bi-pergunta").get_attribute("aria-required") is None
        assert page.locator(".agente-bi-pergunta").is_checked() is False
        page.locator("#agente-bi-confirmacao").check()
        assert page.locator("#agente-bi-aprovar").is_enabled()
        page.locator("#agente-bi-confirmacao").uncheck()
        assert page.locator("#agente-bi-aprovar").is_disabled()
        page.locator(".agente-bi-pergunta").check()
        assert page.locator("#agente-bi-aprovar").is_disabled()
        page.locator("#agente-bi-confirmacao").check()
        assert page.locator("#agente-bi-aprovar").is_enabled()
        page.locator("#agente-bi-aprovar").click()
        page.wait_for_selector("#agente-bi-aprovada:not([hidden])")
        assert page.locator("#agente-bi-aprovada").get_attribute("data-versao") == "2"
        assert page.locator("#agente-bi-aprovada").get_attribute("data-graficos") == "6"
        assert "versão 2" in page.locator("#agente-bi-aprovada-texto").inner_text()
        assert page.evaluate("() => document.activeElement.id") == "agente-bi-aprovada-titulo"

        chamadas = page.evaluate("() => window.__bi")
        pedido = json.loads(next(item["body"] for item in chamadas if item["url"].rstrip("/").endswith("/descoberta")))
        assert pedido["pedido"]["modalidades"] == ["A", "B", "C"]
        assert pedido["pedido"]["texto"] == "INSTRUCAO-SIGILOSA"
        assert pedido["pedido"]["referencia"]["formulas"] == [FORMULA_SOMA]
        aprovacao = json.loads(next(item["body"] for item in chamadas if "/descoberta/selecao" in item["url"]))
        assert aprovacao["sugestoes"] == [f"s{indice}" for indice in range(1, 7)]
        assert aprovacao["perguntas"] == [{"id": "p1", "confirmada": True}]
        assert aprovacao["confirmacao"] is True
        assert any("/analises" in item["url"] and item["body"] == "" for item in chamadas)
        assert all("/analises" in item["url"] or "/descoberta" in item["url"] for item in chamadas)
        assert all("ingestao" not in item["url"] and "anthropic" not in item["url"] for item in chamadas)
        quantidade = len(chamadas)

        page.locator("#agente-bi-input").click()
        page.keyboard.type("mensagem que nao pode sair")
        assert page.locator("#agente-bi-input").input_value() == ""
        assert page.locator("#agente-bi-send").is_disabled()
        assert page.evaluate("() => window.__bi.length") == quantidade

        page.set_viewport_size({"width": 390, "height": 844})
        caixa = page.locator("#agente-bi-descoberta").bounding_box()
        assert caixa is not None
        assert caixa["width"] <= 390
        assert caixa["x"] >= 0
        ajuste = page.evaluate(
            """() => {
              const el = document.getElementById('agente-bi-descoberta');
              return el.scrollWidth <= el.clientWidth + 1;
            }"""
        )
        assert ajuste is True
        assert page.locator("#agente-bi-aprovar").bounding_box()["height"] >= 44
        relevantes = [erro for erro in erros if "$ is not defined" not in erro]
        assert relevantes == []
        browser.close()


def test_recarga_recupera_selecao_sem_novo_upload(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    web = _cliente_pagina(monkeypatch, authenticated=True, permitido=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_INGESTAO_ENABLED", "1")
    renderizado = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    documento = _browser_document(renderizado)
    script = (ROOT / "app" / "static" / "js" / "agente_bi_descoberta.js").read_text(encoding="utf-8")
    grafico = {
        "id": "s1",
        "status": "viavel",
        "leitura": "indicador_de_medida",
        "grafico": "indicador",
        "operacao": "soma",
        "origem": "dados",
        "regiao": {"arquivo": 0, "aba": 0, "regiao": 0},
        "medida": {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1},
        "agrupamento": None,
        "segunda_medida": None,
        "evidencias": ["tipo_numero_fracionado"],
        "juncao": "nao_executada",
    }
    selecao = {
        "id": 16,
        "usuario_id": 3,
        "dataset_id": 9,
        "versao": 4,
        "impressao": "ab",
        "etapa": "selecao",
        "estado": "concluida",
        "contrato_versao": "selecao.descoberta.v1",
        "resultado": {
            "contrato": "selecao.descoberta.v1",
            "dataset_id": 9,
            "dataset_versao": 4,
            "dataset_impressao": "ab",
            "modalidades": ["A", "B", "C"],
            "graficos": [grafico, {**grafico, "id": "s2", "grafico": "barra"}],
            "perguntas_confirmadas": ["p1"],
            "executavel": "somente_viavel",
            "juncao": "nao_executada",
            "chamada_anthropic": False,
            "apto_anthropic": False,
        },
    }
    fabricada = {
        "id": 99,
        "etapa": "descoberta",
        "estado": "concluida",
        "contrato_versao": "analisar.v1",
        "dataset_id": 9,
        "versao": 4,
        "resultado": {
            "contrato": "analisar.v1",
            "sugestoes": [{"id": "s999", "medida": {"indice": 99999}, "evidencias": ["evidencia_inventada"]}],
        },
    }
    erros = []

    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.on("pageerror", lambda erro: erros.append(str(erro)))
        page.set_content(documento, wait_until="domcontentloaded")
        page.evaluate(
            """(pacote) => {
              window.__bi = [];
              window.fetch = (url, opts) => {
                window.__bi.push({ url: String(url), method: (opts && opts.method) || "GET" });
                return Promise.resolve({
                  ok: true,
                  status: 200,
                  json: () => Promise.resolve(pacote)
                });
              };
            }""",
            {"ok": True, "analises": [fabricada, selecao]},
        )
        page.add_script_tag(content=script)
        page.wait_for_selector("#agente-bi-descoberta[data-restaurada='1']")

        assert page.locator("#agente-bi-input").evaluate("el => el.readOnly") is True
        assert page.locator("#agente-bi-send").is_disabled()
        assert 'href="/cleide-bi-frete"' in page.content()
        texto = page.locator("#agente-bi-descoberta").inner_text()
        assert "versão 4" in page.locator("#agente-bi-aprovada-texto").inner_text()
        assert "Não é necessário enviar a planilha outra vez." in texto
        assert page.locator("#agente-bi-aprovada").get_attribute("data-versao") == "4"
        assert page.locator("#agente-bi-aprovada").get_attribute("data-graficos") == "2"
        assert page.locator("[data-status='viavel']").count() == 2
        assert "indicador" in texto
        assert "barra" in texto
        assert "A, B, C" in page.locator("#agente-bi-preparacao-lista").inner_text()
        assert "p1" in page.locator("#agente-bi-perguntas").inner_text()
        assert "s999" not in texto
        assert "99999" not in texto
        assert "evidencia_inventada" not in texto
        assert page.locator(".agente-bi-grafico").count() == 0
        assert page.locator("#agente-bi-aprovar").is_disabled()
        assert page.locator("#agente-bi-modalidades").evaluate("el => el.disabled") is True
        chamadas = page.evaluate("() => window.__bi")
        assert chamadas
        assert all("/analises" in item["url"] for item in chamadas)
        assert all("/descoberta" not in item["url"] for item in chamadas)

        page.locator("#agente-bi-input").click()
        page.keyboard.type("mensagem que nao pode sair")
        assert page.locator("#agente-bi-input").input_value() == ""

        for largura in (1280, 390):
            page.set_viewport_size({"width": largura, "height": 900})
            caixa = page.locator("#agente-bi-descoberta").bounding_box()
            assert caixa is not None
            assert caixa["width"] <= largura
            assert caixa["x"] >= 0
            assert page.evaluate(
                """() => {
                  const el = document.getElementById('agente-bi-descoberta');
                  return el.scrollWidth <= el.clientWidth + 1;
                }"""
            ) is True
        assert page.locator("#agente-bi-aprovar").bounding_box()["height"] >= 44
        relevantes = [erro for erro in erros if "$ is not defined" not in erro]
        assert relevantes == []
        browser.close()
