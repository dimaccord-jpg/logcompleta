"""SCRUM-269 etapa 2: plano conversacional v2, sem execução e sem Anthropic real."""
from __future__ import annotations

import copy
import inspect
import json
import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiDashboard,
    AgenteBiExecucao,
    IaChamadaTentativa,
    IaConsumoEvento,
    utcnow_naive,
)
from app.services.agente_bi_contrato_v2_service import CONTRATO_PATCH_V1, CONTRATO_PATCH_V2, reconhecer_historico
from app.services.agente_bi_evolucao_service import evoluir_dashboard
from app.services.agente_bi_execucao_service import ExecucaoErro, _documento_analisar_revalidado, _resultado_confirmado, confirmar
from app.services.agente_bi_interpretacao_v2_service import (
    DEPENDENCIA_POLITICA_PLACAS,
    InterpretacaoErro,
    interpretar_plano,
)
from app.services.cleiton_ai_data_governance import DECISION_ALLOW, DECISION_MINIMIZE, GovernanceResult
from tests.conftest import seed_cleiton_cost_config, seed_sistema_interno
from tests.test_agente_bi_analisar import SENTINELA
from tests.test_agente_bi_evolucao import _painel

ROOT = Path(__file__).resolve().parents[1]
FONTE = (ROOT / "app" / "services" / "agente_bi_interpretacao_v2_service.py").read_text(encoding="utf-8")
TELEFONE = "11987654321"
TEXTO = "não agrupe por categoria e não use média; consulte o gráfico e crie outro"


@pytest.fixture(autouse=True)
def _sdk_bloqueado(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)


@pytest.fixture(autouse=True)
def _sem_execucao(monkeypatch):
    import app.services.agente_bi_calculo_service as calculo
    import app.services.agente_bi_dashboard_service as painel
    import app.services.agente_bi_evolucao_service as evolucao

    originais = (
        evolucao._aplicar,
        evolucao.calcular_definicao,
        evolucao.montar_dashboard,
        evolucao.filtrar_dashboard,
        calculo.calcular_definicao,
        painel.montar_dashboard,
        painel.filtrar_dashboard,
    )

    def proibido(*_args, **_kwargs):
        raise AssertionError("execucao")

    for modulo, nome in (
        (evolucao, "_aplicar"),
        (evolucao, "calcular_definicao"),
        (evolucao, "montar_dashboard"),
        (evolucao, "filtrar_dashboard"),
        (calculo, "calcular_definicao"),
        (painel, "montar_dashboard"),
        (painel, "filtrar_dashboard"),
    ):
        monkeypatch.setattr(modulo, nome, proibido)
    return originais


def _creditar(email, slug):
    franquia, escopo, painel = _painel(email, slug)
    from app.models import CleitonCostConfig, Conta

    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    franquia.limite_total = Decimal("100")
    franquia.consumo_acumulado = Decimal("0")
    franquia.reserva_pendente = Decimal("0")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    return franquia, escopo, painel


def _resposta(plano, *, entrada=1000, saida=1000):
    return {
        "texto": json.dumps(plano, ensure_ascii=False),
        "stop_reason": "end_turn",
        "input_tokens": entrada,
        "output_tokens": saida,
        "recusa": False,
        "truncada": False,
    }


def _mensagem(pedido):
    return json.loads(pedido["messages"][0]["content"])


def _operacao(**extras):
    base = {
        "id": "o1",
        "acao": "consultar",
        "grafico": "g1",
        "temporario": None,
        "dependencias": [],
        "coluna": None,
        "segunda_coluna": None,
        "operacao": None,
        "apresentacao": None,
        "operador": None,
        "parametros": [],
        "esclarecimento": None,
    }
    base.update(extras)
    return base


def _plano(operacoes, situacao="proposto"):
    return {"contrato": CONTRATO_PATCH_V2, "versao": 2, "situacao": situacao, "operacoes": operacoes}


def _coluna_numerica(mensagem):
    for item in mensagem["colunas"]:
        papel = item.get("papel")
        tipos = item.get("tipos") or []
        if papel in {"valor", "quantidade"} or "numero" in tipos:
            return item["referencia"]
    raise AssertionError("coluna")


def _sem_segredo(documento):
    copia = copy.deepcopy(documento)
    if isinstance(copia, dict):
        copia.pop("intencao", None)
    bruto = json.dumps(copia, ensure_ascii=False)
    for segredo in (SENTINELA, "norte", "sul", "alfa", "beta", "quando", "qtd", "10.5", "4.25", "movimento.xlsx"):
        assert segredo not in bruto
    chaves = set()

    def andar(no):
        if isinstance(no, dict):
            chaves.update(no)
            for valor in no.values():
                andar(valor)
        elif isinstance(no, list):
            for item in no:
                andar(item)

    andar(documento)
    assert {"nome", "nome_arquivo", "nome_aba"} & chaves == set()


def _governado(decisao, conteudo, codigos):
    minimizado = decisao == DECISION_MINIMIZE
    return GovernanceResult(
        decision=decisao,
        safe_content=conteudo,
        reason_codes=list(codigos),
        categories_detected=["person_name"] if minimizado else [],
        categories_removed=["person_name"] if minimizado else [],
        issued_by_cleiton=True,
    )


def _classificador_proibido(monkeypatch):
    def proibido(*_args, **_kwargs):
        raise AssertionError("classificador")

    for nome in ("_classificar", "_periodo", "_filtro", "_operacao_pedida", "_casar", "_vaza_rotulo"):
        monkeypatch.setattr(f"app.services.agente_bi_evolucao_service.{nome}", proibido)


def test_modulo_nao_classifica_nem_executa():
    for nome in (
        "_classificar",
        "_SINAIS_SEMANTICOS",
        "_vaza_rotulo",
        "_operacao_pedida",
        "_casar",
        "calcular_definicao",
        "montar_dashboard",
        "_aplicar",
    ):
        assert nome not in FONTE
    assert re.search(r"\b_periodo\b", FONTE) is None
    assert re.search(r"\b_filtro\b", FONTE) is None
    assert "import re" not in FONTE
    assert "placas em texto livre" in DEPENDENCIA_POLITICA_PLACAS


def test_texto_livre_preserva_negacao_e_instrucoes(ctx, monkeypatch):
    _classificador_proibido(monkeypatch)
    _franquia, escopo, painel = _creditar("plano.livre@test.com", "plano-livre")
    dashboards = AgenteBiDashboard.query.count()
    estado = {"chamadas": 0}

    def enviar(pedido):
        estado["chamadas"] += 1
        estado["mensagem"] = _mensagem(pedido)
        return _resposta(_plano([_operacao()]))

    saida = interpretar_plano(escopo, painel["dashboard"]["id"], {"texto": TEXTO}, transporte=enviar)
    assert estado["chamadas"] == 1
    assert estado["mensagem"]["intencao"] == TEXTO
    assert "não" in estado["mensagem"]["intencao"]
    assert ";" in estado["mensagem"]["intencao"]
    assert estado["mensagem"]["graficos"] == [{"id": "g1"}]
    assert estado["mensagem"]["associacoes"] == []
    assert estado["mensagem"]["rotulos"] is False
    assert estado["mensagem"]["valores_de_celula"] is False
    _sem_segredo(estado["mensagem"])
    assert saida["executado"] is False
    assert saida["situacao"] == "proposto"
    assert saida["plano"]["operacoes"][0]["grafico"] == "g1"
    assert saida["chamada_anthropic"] is True
    assert AgenteBiDashboard.query.count() == dashboards
    analise = db.session.get(AgenteBiAnalise, saida["analise_id"])
    assert TEXTO not in (analise.contexto_json or "")
    assert SENTINELA not in (analise.contexto_json or "")
    assert SENTINELA not in (analise.regras_negocio_json or "")


def test_planos_validos_e_recusas(ctx, monkeypatch):
    _classificador_proibido(monkeypatch)
    _franquia, escopo, painel = _creditar("plano.casos@test.com", "plano-casos")
    dashboards = AgenteBiDashboard.query.count()
    identificador = painel["dashboard"]["id"]
    vistos = {}

    def enviar(pedido):
        vistos["mensagem"] = _mensagem(pedido)
        return _resposta(vistos["plano"])

    coluna = None

    def consultar(pedido):
        mensagem = _mensagem(pedido)
        nonlocal coluna
        coluna = _coluna_numerica(mensagem)
        vistos["mensagem"] = mensagem
        return _resposta(_plano([_operacao()]))

    consulta = interpretar_plano(escopo, identificador, {"texto": "consulte o gráfico existente"}, transporte=consultar)
    assert consulta["plano"]["operacoes"][0]["grafico"] == "g1"
    assert coluna is not None

    def composto(_pedido):
        return _resposta(
            _plano(
                [
                    _operacao(id="o1", acao="consultar", grafico="g1"),
                    _operacao(
                        id="o2",
                        acao="criar_grafico",
                        grafico=None,
                        temporario="t1",
                        coluna=coluna,
                        operacao="soma",
                        apresentacao="indicador",
                    ),
                    _operacao(
                        id="o3",
                        acao="alterar_indicador",
                        grafico=None,
                        temporario="t1",
                        dependencias=["o2"],
                        operacao="media",
                    ),
                ]
            )
        )

    junto = interpretar_plano(escopo, identificador, {"texto": "consulte e depois crie outro indicador"}, transporte=composto)
    assert [item["acao"] for item in junto["plano"]["operacoes"]] == ["consultar", "criar_grafico", "alterar_indicador"]
    assert junto["plano"]["operacoes"][2]["temporario"] == "t1"
    assert junto["executado"] is False

    def temporario_invalido(_pedido):
        return _resposta(_plano([_operacao(grafico=None, temporario="t1", acao="consultar")]))

    with pytest.raises(InterpretacaoErro) as temporario:
        interpretar_plano(escopo, identificador, {"texto": "use uma referência temporária solta"}, transporte=temporario_invalido)
    assert temporario.value.codigo == "referencia_temporaria_invalida"

    def inexistente(_pedido):
        return _resposta(_plano([_operacao(grafico="g9")]))

    with pytest.raises(InterpretacaoErro) as grafico:
        interpretar_plano(escopo, identificador, {"texto": "consulte um gráfico que não está no painel"}, transporte=inexistente)
    assert grafico.value.codigo == "referencia_inexistente"

    def malformado(_pedido):
        return _resposta({"contrato": CONTRATO_PATCH_V2, "versao": 2})

    with pytest.raises(InterpretacaoErro) as quebrado:
        interpretar_plano(escopo, identificador, {"texto": "devolva um plano incompleto"}, transporte=malformado)
    assert quebrado.value.codigo == "contrato_saida_invalido"

    def esclarecido(_pedido):
        return _resposta(
            _plano(
                [_operacao(grafico=None, esclarecimento={"motivo": "confirmar_rotulos"})],
                situacao="esclarecimento",
            )
        )

    claro = interpretar_plano(escopo, identificador, {"texto": "o que significa beta nesse painel"}, transporte=esclarecido)
    assert claro["situacao"] == "esclarecimento"
    assert claro["plano"]["operacoes"][0]["esclarecimento"]["motivo"] == "confirmar_rotulos"
    assert claro["executado"] is False

    def esclarecimento_invalido(_pedido):
        return _resposta(_plano([_operacao(esclarecimento={"motivo": "placa"})], situacao="esclarecimento"))

    with pytest.raises(InterpretacaoErro) as motivo:
        interpretar_plano(escopo, identificador, {"texto": "esclarecimento com motivo fora do catálogo"}, transporte=esclarecimento_invalido)
    assert motivo.value.codigo == "contrato_saida_invalido"

    def literal(_pedido):
        operacao = _operacao()
        operacao["valor"] = "10.5"
        return _resposta(_plano([operacao]))

    with pytest.raises(InterpretacaoErro) as valor:
        interpretar_plano(escopo, identificador, {"texto": "filtre pelo valor literal da célula"}, transporte=literal)
    assert valor.value.codigo == "contrato_saida_invalido"

    def opaco(_pedido):
        return _resposta(
            _plano(
                [
                    _operacao(
                        acao="definir_filtro",
                        coluna=coluna,
                        operador="maior",
                        parametros=[{"forma": "opaca", "referencia": "p9"}],
                    )
                ]
            )
        )

    with pytest.raises(InterpretacaoErro) as externo:
        interpretar_plano(escopo, identificador, {"texto": "filtre com parâmetro que o servidor não vinculou"}, transporte=opaco)
    assert externo.value.codigo == "referencia_inexistente"
    assert vistos["mensagem"]["parametros_opacos"] == []
    assert vistos["mensagem"]["associacoes"] == []
    _sem_segredo(vistos["mensagem"])
    assert AgenteBiDashboard.query.count() == dashboards


def test_governanca_allow_minimize_block_e_falha(ctx, monkeypatch):
    _classificador_proibido(monkeypatch)
    _franquia, escopo, painel = _creditar("plano.gov@test.com", "plano-gov")
    identificador = painel["dashboard"]["id"]
    dashboards = AgenteBiDashboard.query.count()

    def transporte(_pedido):
        raise AssertionError("transporte")

    chamadas = {"n": 0}

    def tecnico(_conteudo, **_kwargs):
        chamadas["n"] += 1
        return _governado("blocked", None, ["classifier_unavailable"])

    monkeypatch.setattr("app.services.agente_bi_interpretacao_v2_service.govern_outbound_content", tecnico)
    with pytest.raises(InterpretacaoErro) as bloqueio:
        interpretar_plano(escopo, identificador, {"texto": TEXTO}, transporte=transporte)
    assert bloqueio.value.codigo == "contexto_nao_autorizado"
    assert chamadas["n"] == 1

    def quebra(_conteudo, **_kwargs):
        raise RuntimeError("governanca")

    monkeypatch.setattr("app.services.agente_bi_interpretacao_v2_service.govern_outbound_content", quebra)
    with pytest.raises(InterpretacaoErro) as falha:
        interpretar_plano(escopo, identificador, {"texto": TEXTO}, transporte=transporte)
    assert falha.value.codigo == "contexto_nao_autorizado"

    def remove(conteudo, **_kwargs):
        chamadas["remove"] = chamadas.get("remove", 0) + 1
        if chamadas["remove"] == 1:
            return _governado("blocked", None, ["conteudo_nao_autorizado"])
        seguro = copy.deepcopy(conteudo)
        seguro["intencao"] = None
        return _governado(DECISION_ALLOW, seguro, ["no_incidental_categories"])

    monkeypatch.setattr("app.services.agente_bi_interpretacao_v2_service.govern_outbound_content", remove)
    with pytest.raises(InterpretacaoErro) as removida:
        interpretar_plano(escopo, identificador, {"texto": TEXTO}, transporte=transporte)
    assert removida.value.codigo == "intencao_nao_transportada"

    def troca(conteudo, **_kwargs):
        seguro = copy.deepcopy(conteudo)
        seguro["colunas"][0]["referencia"]["indice"] += 1
        return _governado(DECISION_MINIMIZE, seguro, ["incidental_data_minimized"])

    monkeypatch.setattr("app.services.agente_bi_interpretacao_v2_service.govern_outbound_content", troca)
    with pytest.raises(InterpretacaoErro) as indice:
        interpretar_plano(escopo, identificador, {"texto": TEXTO}, transporte=transporte)
    assert indice.value.codigo == "contexto_nao_autorizado"

    monkeypatch.setattr("app.services.agente_bi_interpretacao_v2_service.govern_outbound_content", _governanca_real())
    estado = {}

    def minimizar(conteudo, **_kwargs):
        if "antes" not in estado:
            estado["antes"] = copy.deepcopy(conteudo)
        if TELEFONE in str(conteudo.get("intencao")):
            seguro = copy.deepcopy(conteudo)
            seguro["intencao"] = "consulta estrutural"
            return _governado(DECISION_MINIMIZE, seguro, ["incidental_data_minimized"])
        return _governado(DECISION_ALLOW, copy.deepcopy(conteudo), ["no_incidental_categories"])

    monkeypatch.setattr("app.services.agente_bi_interpretacao_v2_service.govern_outbound_content", minimizar)

    def enviar(pedido):
        estado["mensagem"] = _mensagem(pedido)
        return _resposta(_plano([_operacao()]))

    frase = f"telefone {TELEFONE} consulte o gráfico"
    saida = interpretar_plano(escopo, identificador, {"texto": frase}, transporte=enviar)
    assert saida["executado"] is False
    assert estado["mensagem"]["intencao"] == "consulta estrutural"
    assert estado["mensagem"]["colunas"] == estado["antes"]["colunas"]
    assert TELEFONE not in json.dumps(estado["mensagem"], ensure_ascii=False)
    assert AgenteBiDashboard.query.count() == dashboards


def _permitir_estrutura(conteudo, **_kwargs):
    return _governado(DECISION_ALLOW, copy.deepcopy(conteudo), ["no_incidental_categories"])


def _governar_fronteiras(envelope):
    """A estrutura passa intacta. Só o envelope do adaptador muda."""

    def governar(conteudo, **kwargs):
        if isinstance(conteudo, dict) and "messages" in conteudo:
            return envelope(conteudo, **kwargs)
        return _permitir_estrutura(conteudo)

    return governar


def _envelope_minimizado(conteudo, intencao):
    dados = json.loads(conteudo["messages"][0]["content"])
    dados["intencao"] = intencao
    seguro = {
        "system": conteudo.get("system") or "",
        "messages": [
            {
                "role": "user",
                "content": json.dumps(dados, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            }
        ],
    }
    return _governado(DECISION_MINIMIZE, seguro, ["incidental_data_minimized"])


def _observar_governanca_real(monkeypatch):
    from app.services.cleiton_ai_data_governance import govern_outbound_content as real

    estado = {"envelopes": [], "sistemas": [], "antes": None}

    def governar(conteudo, **kwargs):
        resultado = real(conteudo, **kwargs)
        if isinstance(conteudo, dict) and "messages" in conteudo:
            seguro = resultado.safe_content
            mensagens = seguro.get("messages") if isinstance(seguro, dict) else None
            if isinstance(mensagens, list) and mensagens and isinstance(mensagens[0], dict):
                estado["envelopes"].append(mensagens[0].get("content"))
                estado["sistemas"].append(seguro.get("system"))
        elif estado["antes"] is None and isinstance(conteudo, dict) and "colunas" in conteudo:
            estado["antes"] = copy.deepcopy(conteudo)
        return resultado

    monkeypatch.setattr("app.services.agente_bi_interpretacao_v2_service.govern_outbound_content", governar)
    return estado


def _sem_efeito(escopo, franquia_id, transporte, *, tentativas, eventos, dashboards):
    assert transporte["n"] == 0
    assert IaChamadaTentativa.query.count() == tentativas
    assert IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia_id).count() == eventos
    assert AgenteBiDashboard.query.count() == dashboards
    linha = (
        AgenteBiAnalise.query.filter_by(usuario_id=escopo.usuario_id).order_by(AgenteBiAnalise.id.desc()).first()
    )
    assert linha is not None
    assert linha.resultado_json is None
    assert linha.estado != "concluida"


def test_transporte_confere_representacao_final(ctx, monkeypatch):
    _classificador_proibido(monkeypatch)
    franquia, escopo, painel = _creditar("plano.transporte@test.com", "plano-transporte")
    identificador = painel["dashboard"]["id"]
    dashboards = AgenteBiDashboard.query.count()
    transporte = {"n": 0, "pedido": None}

    def enviar(pedido):
        transporte["n"] += 1
        transporte["pedido"] = pedido
        return _resposta(_plano([_operacao()]))

    def contagens():
        return (
            IaChamadaTentativa.query.count(),
            IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia.id).count(),
        )

    observado = _observar_governanca_real(monkeypatch)
    tentativas, eventos = contagens()
    with pytest.raises(InterpretacaoErro) as corrompido:
        interpretar_plano(escopo, identificador, {"texto": "agrupar por cliente_id"}, transporte=enviar)
    assert corrompido.value.codigo == "contexto_nao_autorizado"
    with pytest.raises(json.JSONDecodeError):
        json.loads(observado["envelopes"][-1])
    _sem_efeito(escopo, franquia.id, transporte, tentativas=tentativas, eventos=eventos, dashboards=dashboards)

    def zerar_intencao(conteudo, **_kwargs):
        return _envelope_minimizado(conteudo, "")

    monkeypatch.setattr(
        "app.services.agente_bi_interpretacao_v2_service.govern_outbound_content",
        _governar_fronteiras(zerar_intencao),
    )
    tentativas, eventos = contagens()
    with pytest.raises(InterpretacaoErro) as removida:
        interpretar_plano(escopo, identificador, {"texto": "retire a intencao antes do transporte"}, transporte=enviar)
    assert removida.value.codigo == "intencao_nao_transportada"
    _sem_efeito(escopo, franquia.id, transporte, tentativas=tentativas, eventos=eventos, dashboards=dashboards)

    def trocar_indice(conteudo, **_kwargs):
        dados = json.loads(conteudo["messages"][0]["content"])
        dados["colunas"][0]["referencia"]["indice"] += 1
        seguro = {
            "system": conteudo.get("system") or "",
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(dados, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                }
            ],
        }
        return _governado(DECISION_MINIMIZE, seguro, ["incidental_data_minimized"])

    monkeypatch.setattr(
        "app.services.agente_bi_interpretacao_v2_service.govern_outbound_content",
        _governar_fronteiras(trocar_indice),
    )
    tentativas, eventos = contagens()
    with pytest.raises(InterpretacaoErro) as indice:
        interpretar_plano(escopo, identificador, {"texto": "preserve a referencia tecnica da coluna"}, transporte=enviar)
    assert indice.value.codigo == "contexto_nao_autorizado"
    _sem_efeito(escopo, franquia.id, transporte, tentativas=tentativas, eventos=eventos, dashboards=dashboards)

    def bloquear(conteudo, **_kwargs):
        return _governado("blocked", None, ["classifier_unavailable"])

    monkeypatch.setattr(
        "app.services.agente_bi_interpretacao_v2_service.govern_outbound_content",
        _governar_fronteiras(bloquear),
    )
    tentativas, eventos = contagens()
    with pytest.raises(InterpretacaoErro) as bloqueio:
        interpretar_plano(escopo, identificador, {"texto": "bloqueie a fronteira final do transporte"}, transporte=enviar)
    assert bloqueio.value.codigo == "contexto_nao_autorizado"
    _sem_efeito(escopo, franquia.id, transporte, tentativas=tentativas, eventos=eventos, dashboards=dashboards)

    def quebrar(conteudo, **_kwargs):
        raise RuntimeError("governanca")

    monkeypatch.setattr(
        "app.services.agente_bi_interpretacao_v2_service.govern_outbound_content",
        _governar_fronteiras(quebrar),
    )
    tentativas, eventos = contagens()
    with pytest.raises(InterpretacaoErro) as falha:
        interpretar_plano(escopo, identificador, {"texto": "falha tecnica na fronteira final do transporte"}, transporte=enviar)
    assert falha.value.codigo == "contexto_nao_autorizado"
    _sem_efeito(escopo, franquia.id, transporte, tentativas=tentativas, eventos=eventos, dashboards=dashboards)

    observado = _observar_governanca_real(monkeypatch)
    frase = f"telefone {TELEFONE} consulte o grafico"
    minimizada = interpretar_plano(escopo, identificador, {"texto": frase}, transporte=enviar)
    mensagem = _mensagem(transporte["pedido"])
    assert minimizada["executado"] is False
    assert minimizada["situacao"] == "proposto"
    assert minimizada["chamada_anthropic"] is True
    assert mensagem["intencao"].strip()
    assert mensagem["intencao"] != frase
    assert TELEFONE not in json.dumps(mensagem, ensure_ascii=False)
    assert mensagem["colunas"] == observado["antes"]["colunas"]
    assert mensagem["graficos"] == observado["antes"]["graficos"]
    assert transporte["pedido"]["messages"][0]["content"] == observado["envelopes"][-1]
    assert transporte["pedido"]["system"] == observado["sistemas"][-1]
    assert json.loads(observado["envelopes"][-1])["intencao"] == mensagem["intencao"]

    observado = _observar_governanca_real(monkeypatch)
    integra = interpretar_plano(escopo, identificador, {"texto": TEXTO}, transporte=enviar)
    mensagem = _mensagem(transporte["pedido"])
    assert integra["executado"] is False
    assert integra["plano"]["operacoes"][0]["acao"] == "consultar"
    assert mensagem["intencao"] == TEXTO
    assert "não" in mensagem["intencao"]
    assert mensagem["colunas"] == observado["antes"]["colunas"]
    assert transporte["pedido"]["messages"][0]["content"] == observado["envelopes"][-1]
    assert transporte["pedido"]["system"] == observado["sistemas"][-1]
    _sem_segredo(mensagem)

    autorizados = []

    def minimizar_texto(conteudo, **_kwargs):
        resultado = _envelope_minimizado(conteudo, "consulta estrutural")
        autorizados.append(resultado.safe_content["messages"][0]["content"])
        return resultado

    governar = _governar_fronteiras(minimizar_texto)
    monkeypatch.setattr("app.services.agente_bi_interpretacao_v2_service.govern_outbound_content", governar)
    monkeypatch.setattr("app.services.cleiton_ai_data_governance.govern_outbound_content", governar)
    antes = transporte["n"]
    saida = interpretar_plano(escopo, identificador, {"texto": "minimize so o texto autorizado"}, transporte=enviar)
    mensagem = _mensagem(transporte["pedido"])
    assert saida["executado"] is False
    assert saida["chamada_anthropic"] is True
    assert transporte["n"] == antes + 1
    assert mensagem["intencao"] == "consulta estrutural"
    assert len(autorizados) == 2
    assert autorizados[0] == autorizados[1]
    assert transporte["pedido"]["messages"][0]["content"] == autorizados[0]
    assert AgenteBiDashboard.query.count() == dashboards


def _governanca_real():
    from app.services.cleiton_ai_data_governance import govern_outbound_content

    return govern_outbound_content


def test_repeticao_concorrencia_lease_e_fencing(ctx, monkeypatch):
    _classificador_proibido(monkeypatch)
    franquia, escopo, painel = _creditar("plano.fila@test.com", "plano-fila")
    identificador = painel["dashboard"]["id"]
    texto = "consulte o gráfico sem publicar"
    estado = {"chamadas": 0, "concorrente": None}
    dashboards = AgenteBiDashboard.query.count()

    def enviar(pedido):
        estado["chamadas"] += 1
        estado["mensagem"] = _mensagem(pedido)
        if estado["chamadas"] == 1:
            try:
                interpretar_plano(escopo, identificador, {"texto": texto}, transporte=enviar)
            except InterpretacaoErro as exc:
                estado["concorrente"] = exc.codigo
        return _resposta(_plano([_operacao()]))

    primeira = interpretar_plano(escopo, identificador, {"texto": texto}, transporte=enviar)
    assert estado["concorrente"] == "operacao_em_andamento"
    assert estado["chamadas"] == 1
    assert primeira["chamada_anthropic"] is True
    assert IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia.id).count() == 1
    repetida = interpretar_plano(escopo, identificador, {"texto": texto}, transporte=enviar)
    assert repetida["analise_id"] == primeira["analise_id"]
    assert repetida["chamada_anthropic"] is False
    assert repetida["plano"] == primeira["plano"]
    assert estado["chamadas"] == 1
    assert IaChamadaTentativa.query.filter_by(attempt_key=db.session.get(AgenteBiExecucao, db.session.query(AgenteBiExecucao.id).filter_by(analise_id=primeira["analise_id"]).scalar()).chave_idempotencia).count() == 1
    assert IaConsumoEvento.query.filter_by(provider="anthropic", franquia_id=franquia.id).count() == 1

    with pytest.raises(InterpretacaoErro) as curto:
        interpretar_plano(
            escopo,
            identificador,
            {"texto": "outro pedido com lease curto"},
            transporte=enviar,
            lease_segundos=1,
        )
    assert curto.value.codigo == "orcamento_temporal"
    assert estado["chamadas"] == 1

    analise = db.session.get(AgenteBiAnalise, primeira["analise_id"])
    execucao = AgenteBiExecucao.query.filter_by(analise_id=analise.id).one()
    original = analise.resultado_json
    visto = {}

    def analisar_proibido(*_args, **_kwargs):
        visto["analisar"] = True
        raise AssertionError("analisar")

    monkeypatch.setattr("app.services.agente_bi_execucao_service._documento_analisar_revalidado", analisar_proibido)
    with pytest.raises(ExecucaoErro) as arbitrario:
        confirmar(
            execucao.id,
            worker_id=execucao.lease_worker or "worker-plano",
            fencing=int(execucao.fencing),
            codigo_resultado="plano_v2",
            documento={"contrato": "livre.v9", "versao": 2, "situacao": "proposto", "operacoes": []},
        )
    assert arbitrario.value.codigo == "contrato_saida_invalido"
    assert "analisar" not in visto
    db.session.refresh(analise)
    assert analise.resultado_json == original

    outro = _plano([_operacao(acao="alterar_apresentacao", apresentacao="barra", grafico="g1")])
    with pytest.raises(ExecucaoErro) as trocado:
        confirmar(
            execucao.id,
            worker_id="worker-plano",
            fencing=int(execucao.fencing) + 9,
            codigo_resultado="plano_v2",
            documento=outro,
        )
    assert trocado.value.codigo == "historico_preservado"
    db.session.refresh(analise)
    assert analise.resultado_json == original
    with pytest.raises(ExecucaoErro) as fence:
        confirmar(
            execucao.id,
            worker_id="worker-plano",
            fencing=int(execucao.fencing) + 9,
            codigo_resultado="plano_v2",
            documento=json.loads(original),
        )
    assert fence.value.codigo == "fencing_recusado"
    db.session.refresh(analise)
    assert analise.resultado_json == original
    assert AgenteBiDashboard.query.count() == dashboards
    _sem_segredo(estado["mensagem"])


def test_compatibilidade_historica_e_analisar(ctx, monkeypatch, _sem_execucao):
    import app.services.agente_bi_calculo_service as calculo
    import app.services.agente_bi_dashboard_service as painel_mod
    import app.services.agente_bi_evolucao_service as evolucao

    for modulo, nome, original in (
        (evolucao, "_aplicar", _sem_execucao[0]),
        (evolucao, "calcular_definicao", _sem_execucao[1]),
        (evolucao, "montar_dashboard", _sem_execucao[2]),
        (evolucao, "filtrar_dashboard", _sem_execucao[3]),
        (calculo, "calcular_definicao", _sem_execucao[4]),
        (painel_mod, "montar_dashboard", _sem_execucao[5]),
        (painel_mod, "filtrar_dashboard", _sem_execucao[6]),
    ):
        monkeypatch.setattr(modulo, nome, original)

    historico = reconhecer_historico(
        {"contrato": CONTRATO_PATCH_V1, "acao": "alterar_indicador", "operacao": "soma"}
    )
    assert historico["contrato"] == CONTRATO_PATCH_V1
    assert "validar_saida" in inspect.getsource(_documento_analisar_revalidado)
    assert "_documento_analisar_revalidado" in inspect.getsource(_resultado_confirmado)
    assert "_documento_plano_v2_revalidado" in inspect.getsource(_resultado_confirmado)

    _franquia, escopo, painel = _painel("plano.hist@test.com", "plano-hist")
    filtrado = evoluir_dashboard(escopo, painel["dashboard"]["id"], {"texto": "filtre beta igual a sul"})
    assert filtrado["situacao"] == "aplicada"
    assert filtrado["via"] == "deterministica"
    assert filtrado["chamada_anthropic"] is False

    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_ENABLED", raising=False)
    with pytest.raises(InterpretacaoErro) as desligado:
        interpretar_plano(escopo, filtrado["dashboard"]["id"], {"texto": TEXTO}, transporte=lambda _pedido: (_ for _ in ()).throw(AssertionError("transporte")))
    assert desligado.value.codigo == "interpretacao_indisponivel"
