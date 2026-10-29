"""SCRUM-270: explicação logística a partir dos agregados já calculados.

O transporte é simulado. A resposta simulada não comprova a qualidade
analítica do Claude real. O teste confere o caminho governado, a evidência
estampada pelo executor e a ausência de explicação fabricada.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.extensions import db
from app.models import AgenteBiDashboard, Franquia, IaConsumoEvento
from app.services.agente_bi_analisar_service import TransferenciaRecusada
from app.services.agente_bi_dashboard_service import consultar_dashboard
from app.services.agente_bi_interpretacao_v2_service import InterpretacaoErro
from app.services.agente_bi_persistencia_service import AgenteBiErro, escopo_do_usuario
from app.services.agente_bi_publicacao_v2_service import aplicar_pedido
from tests.conftest import seed_usuario
from tests.test_agente_bi_ingestao import _cliente, _entrar
from tests.test_agente_bi_interpretacao_v2 import _operacao, _plano, _resposta
from tests.test_agente_bi_publicacao_v2 import _por_indice, _preparar

ROOT = Path(__file__).resolve().parents[1]
FONTE = (ROOT / "app" / "services" / "agente_bi_explicacao_service.py").read_text(encoding="utf-8")
PUBLICACAO = (ROOT / "app" / "services" / "agente_bi_publicacao_v2_service.py").read_text(encoding="utf-8")
PERGUNTA = "O que esses números realmente dizem sobre minha operação logística?"
CAUSA = "Existe evidência de que o aumento de custo foi causado por determinada rota?"
PRODUTIVIDADE = "O que podemos concluir sobre a produtividade dos motoristas?"


@pytest.fixture(autouse=True)
def _transporte_simulado(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)


def _consultar(mensagem):
    return _plano([_operacao(id="o1", acao="consultar", grafico=mensagem["graficos"][0]["id"])])


def _transporte(montar, plano=None):
    estado = {"n": 0, "explicacao": None}

    def transporte(pedido):
        estado["n"] += 1
        mensagem = json.loads(pedido["messages"][0]["content"])
        if "agregados" in mensagem:
            estado["explicacao"] = mensagem
            estado["system"] = pedido.get("system")
            return _resposta(montar(mensagem))
        return _resposta((plano or _consultar)(mensagem))

    return transporte, estado


def _fato_de(agregado, campo, valor):
    partes = [f"ref {agregado['ref']}"]
    for rotulo in ("operacao", "papel", "unidade"):
        if agregado.get(rotulo):
            partes.append(f"{rotulo} {agregado[rotulo]}")
    partes.append(f"campo {campo}")
    partes.append(f"valor {valor}")
    return ", ".join(partes) + "."


def _factual(mensagem):
    ref = mensagem["agregados"][0]["ref"]
    return {
        "comprovado": [
            {
                "ref": ref,
                "campo": "resultado",
                "leitura": "O agregado publicado e o resultado da operacao autorizada neste periodo.",
            }
        ],
        "hipotese": [],
        "insuficiente": [],
    }


def _causal(mensagem):
    ref = mensagem["agregados"][0]["ref"]
    return {
        "comprovado": [
            {
                "ref": ref,
                "campo": "resultado",
                "leitura": "O agregado publicado registra o resultado da operacao.",
            }
        ],
        "hipotese": ["A concentracao pode estar associada a uma rota, mas os agregados nao demonstram a causa."],
        "insuficiente": ["Nao ha evidencia no calculo que confirme a causa apontada."],
    }


def _insuficiente(_mensagem):
    return {
        "comprovado": [],
        "hipotese": [],
        "insuficiente": ["Nao ha metrica de disponibilidade nem comparabilidade para concluir produtividade."],
    }


def _relevancia(mensagem):
    ref = mensagem["agregados"][0]["ref"]
    return {
        "comprovado": [
            {
                "ref": ref,
                "campo": "resultado",
                "leitura": "O executor publicou este agregado.",
            }
        ],
        "hipotese": [],
        "insuficiente": [
            "O agregado esta presente, mas nao sustenta sozinho uma conclusao gerencial da operacao."
        ],
    }


def _pedir(escopo, identificador, texto, montar):
    transporte, estado = _transporte(montar)
    antes = AgenteBiDashboard.query.count()
    resposta = aplicar_pedido(escopo, identificador, {"texto": texto}, transporte=transporte)
    return resposta, estado, antes


def test_modulo_nao_classifica_linguagem_nem_especializa_planilha():
    assert "import re" not in FONTE
    assert "import re" not in PUBLICACAO
    assert "_classificar" not in PUBLICACAO
    for termo in ("fabricacao", "opergel", "transportadora", "ano_fabricacao"):
        assert termo not in FONTE
    assert "conclusao gerencial" in FONTE
    assert "Evidência" not in FONTE
    assert "{leitura}" not in FONTE
    assert (
        "Se a pergunta pressupõe métrica, variação, unidade ou causa que os agregados não demonstram, "
        "registre a limitação como informação insuficiente."
    ) in FONTE


def test_caso_a_explicacao_factual_cita_o_agregado_autorizado(ctx):
    base = _preparar("explicacao.fato@test.com", "explicacao-fato")
    escopo = base["escopo"]
    identificador = base["dashboard_id"]
    numero = consultar_dashboard(escopo, identificador)["consulta"]["graficos"][0]["resultado"]
    resposta, estado, antes = _pedir(escopo, identificador, PERGUNTA, _factual)

    assert resposta["situacao"] == "explicacao"
    assert resposta["chamada_anthropic"] is True
    assert resposta["dashboard"]["id"] == identificador
    assert resposta["revisao_anterior_id"] is None
    agregado = estado["explicacao"]["agregados"][0]
    item = resposta["explicacao"]["comprovado"][0]
    assert set(item) == {"fato", "leitura"}
    assert item["fato"] == _fato_de(agregado, "resultado", numero)
    assert item["leitura"] not in item["fato"]
    assert resposta["explicacao"]["hipotese"] == []
    assert resposta["explicacao"]["nao_verificavel"] == []
    assert numero == consultar_dashboard(escopo, identificador)["consulta"]["graficos"][0]["resultado"]
    assert AgenteBiDashboard.query.count() == antes
    eventos = IaConsumoEvento.query.filter_by(usuario_id=escopo.usuario_id).all()
    assert eventos
    assert {evento.flow_type for evento in eventos} == {"bi_logistico"}
    assert {evento.franquia_id for evento in eventos} == {escopo.franquia_id}
    assert {evento.conta_id for evento in eventos} == {escopo.conta_id}
    assert estado["explicacao"]["intencao"] == PERGUNTA
    assert estado["explicacao"]["agregados"][0]["operacao"]
    assert estado["explicacao"]["agregados"][0]["resultado"] == numero
    assert estado["explicacao"]["valores_de_celula"] is False
    assert "sql" in estado["system"].lower()
    assert "informação insuficiente" in estado["system"]


def test_caso_b_hipotese_nao_entra_como_causa_confirmada(ctx):
    base = _preparar("explicacao.causa@test.com", "explicacao-causa")
    resposta, _estado, antes = _pedir(base["escopo"], base["dashboard_id"], CAUSA, _causal)

    fatos = " ".join(item["fato"] for item in resposta["explicacao"]["comprovado"])
    leituras = " ".join(item["leitura"] for item in resposta["explicacao"]["comprovado"])
    hipotese = " ".join(resposta["explicacao"]["hipotese"])
    assert "causa" not in fatos
    assert "rota" not in fatos
    assert leituras not in fatos
    assert "rota" in hipotese
    assert "nao demonstram a causa" in hipotese
    assert resposta["explicacao"]["nao_verificavel"]
    assert AgenteBiDashboard.query.count() == antes


def test_caso_c_dado_insuficiente_nao_inventa_resultado(ctx):
    base = _preparar("explicacao.limite@test.com", "explicacao-limite")
    numero = consultar_dashboard(base["escopo"], base["dashboard_id"])["consulta"]["graficos"][0]["resultado"]
    resposta, _estado, antes = _pedir(base["escopo"], base["dashboard_id"], PRODUTIVIDADE, _insuficiente)

    assert resposta["explicacao"]["comprovado"] == []
    assert resposta["explicacao"]["hipotese"] == []
    assert "produtividade" in resposta["explicacao"]["nao_verificavel"][0]
    assert numero not in json.dumps(resposta["explicacao"])
    assert consultar_dashboard(base["escopo"], base["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == numero
    assert AgenteBiDashboard.query.count() == antes


def test_caso_d_relevancia_operacional_preserva_o_calculo(ctx):
    base = _preparar("explicacao.relevancia@test.com", "explicacao-relevancia")
    numero = consultar_dashboard(base["escopo"], base["dashboard_id"])["consulta"]["graficos"][0]["resultado"]
    resposta, estado, antes = _pedir(
        base["escopo"],
        base["dashboard_id"],
        "Qual e o principal problema que esses indicadores revelam?",
        _relevancia,
    )

    item = resposta["explicacao"]["comprovado"][0]
    assert item["fato"] == _fato_de(estado["explicacao"]["agregados"][0], "resultado", numero)
    assert "conclusao gerencial" not in item["fato"]
    assert "conclusao gerencial" in resposta["explicacao"]["nao_verificavel"][0]
    assert consultar_dashboard(base["escopo"], base["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == numero
    assert estado["explicacao"]["agregados"][0]["resultado"] == numero
    assert AgenteBiDashboard.query.count() == antes


def test_caso_e_governanca_impede_explicacao_fabricada(ctx, monkeypatch):
    base = _preparar("explicacao.governanca@test.com", "explicacao-governanca")
    escopo = base["escopo"]
    identificador = base["dashboard_id"]
    numero = consultar_dashboard(escopo, identificador)["consulta"]["graficos"][0]["resultado"]
    franquia = db.session.get(Franquia, escopo.franquia_id)
    franquia.bloqueio_manual = True
    db.session.commit()
    chamadas = {"n": 0}

    def transporte(_pedido):
        chamadas["n"] += 1
        raise AssertionError("provedor")

    with pytest.raises(InterpretacaoErro) as recusa:
        aplicar_pedido(escopo, identificador, {"texto": PERGUNTA}, transporte=transporte)
    assert recusa.value.codigo == "admissao_recusada"
    assert recusa.value.status == 403
    assert chamadas["n"] == 0
    assert consultar_dashboard(escopo, identificador)["consulta"]["graficos"][0]["resultado"] == numero

    franquia = db.session.get(Franquia, escopo.franquia_id)
    franquia.bloqueio_manual = False
    franquia.limite_total = Decimal("100")
    db.session.commit()

    def bloquear(*_args, **_kwargs):
        raise TransferenciaRecusada()

    monkeypatch.setattr("app.services.agente_bi_explicacao_service.governar_representacao", bloquear)
    with pytest.raises(InterpretacaoErro) as transferencia:
        aplicar_pedido(
            escopo,
            identificador,
            {"texto": "Quais informacoes estao faltando para chegar a uma conclusao?"},
            transporte=_transporte(_factual)[0],
        )
    assert transferencia.value.codigo == "contexto_nao_autorizado"
    assert transferencia.value.status == 403
    assert consultar_dashboard(escopo, identificador)["consulta"]["graficos"][0]["resultado"] == numero


def test_caso_f_isolamento_de_usuario_conta_e_franquia(ctx):
    dono = _preparar("explicacao.dono@test.com", "explicacao-dono")
    outro = _preparar("explicacao.outro@test.com", "explicacao-outro")
    colega = seed_usuario(dono["escopo"].franquia_id, dono["escopo"].conta_id, email="explicacao.colega@test.com")
    intruso_conta = escopo_do_usuario(colega)
    _resposta_dono, estado, _antes = _pedir(dono["escopo"], dono["dashboard_id"], PERGUNTA, _factual)
    bruto = json.dumps(estado["explicacao"])
    assert outro["escopo"].usuario_id != dono["escopo"].usuario_id
    assert "explicacao.outro@test.com" not in bruto
    assert "usuario_id" not in estado["explicacao"]
    assert "conta_id" not in estado["explicacao"]
    assert "franquia_id" not in estado["explicacao"]

    def vazou(_pedido):
        raise AssertionError("vazou")

    for intruso in (outro["escopo"], intruso_conta):
        with pytest.raises(AgenteBiErro) as erro:
            aplicar_pedido(intruso, dono["dashboard_id"], {"texto": PERGUNTA}, transporte=vazou)
        assert erro.value.status == 404
    assert consultar_dashboard(dono["escopo"], dono["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == "5"


def test_caso_g_falha_de_provedor_nao_fabrica_explicacao(ctx):
    base = _preparar("explicacao.falha@test.com", "explicacao-falha")
    escopo = base["escopo"]
    identificador = base["dashboard_id"]
    numero = consultar_dashboard(escopo, identificador)["consulta"]["graficos"][0]["resultado"]
    antes = AgenteBiDashboard.query.count()

    def indisponivel(pedido):
        mensagem = json.loads(pedido["messages"][0]["content"])
        if "agregados" in mensagem:
            raise ConnectionError("simulado")
        return _resposta(_consultar(mensagem))

    with pytest.raises(InterpretacaoErro) as queda:
        aplicar_pedido(escopo, identificador, {"texto": PERGUNTA}, transporte=indisponivel)
    assert queda.value.codigo == "efeito_incerto"
    assert numero not in str(queda.value)

    def invalido(pedido):
        mensagem = json.loads(pedido["messages"][0]["content"])
        if "agregados" in mensagem:
            return _resposta("o custo inventado e 999")
        return _resposta(_consultar(mensagem))

    with pytest.raises(InterpretacaoErro) as texto:
        aplicar_pedido(escopo, identificador, {"texto": "Explique o resultado deste dashboard."}, transporte=invalido)
    assert texto.value.codigo == "contrato_saida_invalido"
    assert "999" not in str(texto.value)

    def numero_inventado(pedido):
        mensagem = json.loads(pedido["messages"][0]["content"])
        if "agregados" not in mensagem:
            return _resposta(_consultar(mensagem))
        ref = mensagem["agregados"][0]["ref"]
        return _resposta(
            {
                "comprovado": [{"ref": ref, "campo": "resultado", "leitura": "o valor e 999"}],
                "hipotese": [],
                "insuficiente": [],
            }
        )

    with pytest.raises(InterpretacaoErro) as inventado:
        aplicar_pedido(
            escopo,
            identificador,
            {"texto": "Qual transportadora merece atencao e por que?"},
            transporte=numero_inventado,
        )
    assert inventado.value.codigo == "contrato_saida_invalido"

    transporte, _estado = _transporte(_factual)
    pergunta_ok = "Explique o resultado deste dashboard de forma operacional."
    aplicar_pedido(escopo, identificador, {"texto": pergunta_ok}, transporte=transporte)
    with pytest.raises(InterpretacaoErro) as repetida:
        aplicar_pedido(escopo, identificador, {"texto": pergunta_ok}, transporte=transporte)
    assert repetida.value.codigo == "repetida"
    assert repetida.value.status == 409
    assert consultar_dashboard(escopo, identificador)["consulta"]["graficos"][0]["resultado"] == numero
    assert AgenteBiDashboard.query.count() == antes


def test_caso_h_caminho_conversacional_devolve_a_explicacao(app, monkeypatch):
    client = _cliente(app, monkeypatch)
    with app.app_context():
        base = _preparar("explicacao.http@test.com", "explicacao-http")
        usuario_id = base["escopo"].usuario_id
        identificador = base["dashboard_id"]
        numero = consultar_dashboard(base["escopo"], identificador)["consulta"]["graficos"][0]["resultado"]
    transporte, estado = _transporte(_factual)
    import app.services.agente_bi_publicacao_v2_service as publicacao

    real = publicacao.aplicar_pedido

    def envolvido(escopo, dashboard_id, corpo, transporte=None):
        return real(escopo, dashboard_id, corpo, transporte=transporte or envolvido.transporte)

    envolvido.transporte = transporte
    monkeypatch.setattr(publicacao, "aplicar_pedido", envolvido)

    assert client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": PERGUNTA}).status_code == 401
    _entrar(client, usuario_id)
    resposta = client.post(f"/api/agente-bi/dashboards/{identificador}/evolucao", json={"texto": PERGUNTA})
    assert resposta.status_code == 200, resposta.get_data(as_text=True)
    corpo = resposta.get_json()
    assert corpo["situacao"] == "explicacao"
    assert corpo["dashboard"]["id"] == identificador
    assert corpo["explicacao"]["comprovado"][0]["fato"] == _fato_de(
        estado["explicacao"]["agregados"][0], "resultado", numero
    )
    assert estado["explicacao"]["agregados"][0]["resultado"] == numero
    assert estado["n"] >= 2
    with app.app_context():
        aberto = consultar_dashboard(base["escopo"], identificador)
    assert aberto["consulta"]["graficos"][0]["resultado"] == numero


FALSA = "O custo aumentou 5% por causa da rota Sul."
EXTENSO = "O custo aumentou vinte por cento por causa da rota Sul."


def _leitura(texto):
    def montar(mensagem):
        return {
            "comprovado": [
                {"ref": mensagem["agregados"][0]["ref"], "campo": "resultado", "leitura": texto}
            ],
            "hipotese": [],
            "insuficiente": [],
        }

    return montar


def _segundo_grafico(mensagem):
    return _plano(
        [
            _operacao(
                id="o1",
                acao="criar_grafico",
                grafico=None,
                temporario="t1",
                coluna=_por_indice(mensagem, 2),
                operacao="soma",
                apresentacao="indicador",
            )
        ]
    )


def _consultar_ambos(mensagem):
    return _plano(
        [
            _operacao(id="o1", acao="consultar", grafico=mensagem["graficos"][0]["id"]),
            _operacao(id="o2", acao="consultar", grafico=mensagem["graficos"][1]["id"]),
        ]
    )


def _leitura_com_numero_alheio(mensagem):
    agregados = mensagem["agregados"]
    origem = next(item for item in agregados if item.get("resultado") == "5")
    alheio = next(item for item in agregados if item.get("resultado") == "10")
    return {
        "comprovado": [
            {
                "ref": origem["ref"],
                "campo": "resultado",
                "leitura": f"Esse recorte corresponde ao valor {alheio['resultado']}.",
            }
        ],
        "hipotese": [],
        "insuficiente": [],
    }


def test_afirmacao_falsa_nao_entra_no_fato_certificado(ctx):
    base = _preparar("explicacao.falsa@test.com", "explicacao-falsa")
    numero = consultar_dashboard(base["escopo"], base["dashboard_id"])["consulta"]["graficos"][0]["resultado"]
    assert numero == "5"
    resposta, estado, antes = _pedir(
        base["escopo"],
        base["dashboard_id"],
        "O custo aumentou 5% por causa da rota Sul?",
        _leitura(FALSA),
    )

    agregado = estado["explicacao"]["agregados"][0]
    item = resposta["explicacao"]["comprovado"][0]
    assert agregado["operacao"] == "soma"
    assert agregado["papel"] == "quantidade"
    assert agregado["unidade"] == "quantidade"
    assert item["fato"] == _fato_de(agregado, "resultado", "5")
    assert item["leitura"] == FALSA
    for termo in ("aumentou", "custo", "%", "Sul", "rota", "causa"):
        assert termo not in item["fato"]
    assert item["leitura"] not in item["fato"]
    assert AgenteBiDashboard.query.count() == antes


def test_numero_por_extenso_nao_entra_no_fato_certificado(ctx):
    base = _preparar("explicacao.extenso@test.com", "explicacao-extenso")
    resposta, estado, antes = _pedir(
        base["escopo"],
        base["dashboard_id"],
        "Quanto o custo aumentou?",
        _leitura(EXTENSO),
    )

    agregado = estado["explicacao"]["agregados"][0]
    item = resposta["explicacao"]["comprovado"][0]
    assert item["fato"] == _fato_de(agregado, "resultado", agregado["resultado"])
    assert item["leitura"] == EXTENSO
    for termo in ("vinte", "aumentou", "custo", "Sul", "rota"):
        assert termo not in item["fato"]
    assert "valor 5" in item["fato"]
    assert AgenteBiDashboard.query.count() == antes


def test_numero_de_g2_na_leitura_de_g1_e_rejeitado(ctx):
    base = _preparar("explicacao.cruzada@test.com", "explicacao-cruzada")
    escopo = base["escopo"]
    antes = AgenteBiDashboard.query.count()
    transporte_criar, _estado_criar = _transporte(lambda _mensagem: {}, _segundo_grafico)
    publicado = aplicar_pedido(
        escopo,
        base["dashboard_id"],
        {"texto": "adicione a soma da outra medida"},
        transporte=transporte_criar,
    )
    assert publicado["situacao"] == "aplicada"
    assert {item["resultado"] for item in publicado["consulta"]["graficos"]} == {"5", "10"}
    identificador = publicado["dashboard"]["id"]
    transporte, estado = _transporte(_leitura_com_numero_alheio, _consultar_ambos)
    with pytest.raises(InterpretacaoErro) as erro:
        aplicar_pedido(
            escopo,
            identificador,
            {"texto": "O que o primeiro agregado mostra em relacao ao segundo?"},
            transporte=transporte,
        )
    assert erro.value.codigo == "contrato_saida_invalido"
    assert {item["resultado"] for item in estado["explicacao"]["agregados"]} == {"5", "10"}
    assert AgenteBiDashboard.query.count() == antes + 1
    assert consultar_dashboard(escopo, base["dashboard_id"])["consulta"]["graficos"][0]["resultado"] == "5"
    aberto = consultar_dashboard(escopo, identificador)["consulta"]["graficos"]
    assert next(item["resultado"] for item in aberto if item["operacao"] == "soma" and item["resultado"] == "5") == "5"
