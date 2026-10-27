"""SCRUM-268: descoberta A+B+C no adaptador governado, com transporte falso.

Não abre o cliente Anthropic, não grava migration e não liga o worker.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agente_bi_api_routes import agente_bi_api_bp
from app.extensions import db, login_manager
from app.models import (
    AgenteBiAnalise,
    AgenteBiDataset,
    AgenteBiExecucao,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
)
from app.services.agente_bi_analisar_service import (
    _validar_pedido,
    instrucao_de_rascunho,
    montar_contexto,
    provedor_simulado,
)
from app.services.agente_bi_anthropic_service import ErroTransporte
from app.services.agente_bi_descoberta_service import (
    DescobertaErro,
    _identidade_operacao,
    _proxima_rodada,
    aprovar_selecao,
    descobrir,
)
from app.services.agente_bi_integracao_service import IntegracaoErro, abrir_operacao, executar_operacao
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import AgenteBiErro, escopo_do_usuario, obter_analise
from app.services.cleiton_ai_data_governance import DECISION_ALLOW, DECISION_MINIMIZE, GovernanceResult
from tests.test_agente_bi_analisar import FORMULA_SOMA, SENTINELA, _movimento
from tests.test_agente_bi_execucao import _seed_financeiro
from tests.test_agente_bi_ingestao import _cliente, _entrar, _ingerir


ROOT = Path(__file__).resolve().parents[1]
TELEFONE = "11987654321"
FORMULA_LIVRE = "=1+2"


@pytest.fixture(autouse=True)
def _sem_sdk(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-4-5")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENTE_BI_WORKER_HABILITADO", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)


def _foto(franquia_id):
    franquia = db.session.get(Franquia, franquia_id)
    db.session.refresh(franquia)
    return Decimal(franquia.consumo_acumulado or 0), Decimal(franquia.reserva_pendente or 0)


def _preparar(email):
    usuario, franquia, escopo, _analise = _seed_financeiro(email=email)
    ingerido = _ingerir(usuario, _movimento(), "movimento.xlsx", chave=f"lote-{email.split('@')[0]}")
    dataset_id = ingerido["dataset"]["id"]
    perfil = perfilar_dataset(escopo, dataset_id)
    return usuario, franquia, escopo, dataset_id, perfil


def _resposta(texto, *, entrada=1000, saida=1000):
    return {
        "texto": texto,
        "stop_reason": "end_turn",
        "input_tokens": entrada,
        "output_tokens": saida,
        "recusa": False,
        "truncada": False,
    }


def _regiao(perfil):
    item = perfil["regioes"][0]
    return {"arquivo": item["arquivo"], "aba": item["aba"], "regiao": item["regiao"]}


def _coord(coluna):
    return {chave: coluna[chave] for chave in ("arquivo", "aba", "regiao", "indice")}


def _medida(perfil, dimensao="valor"):
    return next(coluna for coluna in perfil["colunas"] if coluna["dimensao_principal"] == dimensao)


def _condicional(perfil, duvida="escolher_medida"):
    return {
        "descobertas": [],
        "sugestoes": [
            {
                "status": "condicional",
                "leitura": "esclarecimento_necessario",
                "grafico": None,
                "operacao": None,
                "origem": "dados",
                "regiao": _regiao(perfil),
                "medida": None,
                "agrupamento": None,
                "segunda_medida": None,
                "evidencias": [],
                "juncao": "nao_executada",
                "lacuna": None,
                "duvidas": [duvida],
            }
        ],
    }


def _referencia(perfil, *, operacao="soma", indice=None, texto=None, formulas=None):
    medida = _coord(_medida(perfil))
    if indice is not None:
        medida = {**medida, "indice": indice}
    return {
        "afirmacoes": [
            {
                "operacao": operacao,
                "grafico": "indicador",
                "medida": medida,
                "agrupamento": None,
                "juncao": False,
                "texto": texto,
            }
        ],
        "formulas": list(formulas or []),
    }


def _pedido(perfil, modalidades, *, texto=None, referencia=None):
    pedido = {"modalidades": list(modalidades)}
    if "C" in modalidades:
        pedido["texto"] = "descrever a estrutura" if texto is None else texto
    if "B" in modalidades:
        pedido["referencia"] = referencia if referencia is not None else _referencia(perfil, formulas=[FORMULA_SOMA])
    return pedido


def _transporte(estado, proposta):
    def enviar(pedido):
        estado["chamadas"] = estado.get("chamadas", 0) + 1
        estado["pedido"] = pedido
        return _resposta(json.dumps(proposta, ensure_ascii=False))

    return enviar


def _descobrir(escopo, dataset_id, pedido, transporte):
    return descobrir(escopo, {"dataset_id": dataset_id, "pedido": pedido}, transporte=transporte)


def _mensagem(estado):
    return json.loads(estado["pedido"]["messages"][0]["content"])


def _confirmadas(resultado):
    return [{"id": item["id"], "confirmada": True} for item in resultado["perguntas"]]


def _viaveis(resultado):
    return [
        item["id"]
        for item in resultado["sugestoes"]
        if item["status"] == "viavel" and item["grafico"] and item["juncao"] == "nao_executada"
    ]


@pytest.mark.parametrize(
    "modalidades",
    [
        ["A"],
        ["B"],
        ["C"],
        ["A", "B"],
        ["A", "C"],
        ["B", "C"],
        ["A", "B", "C"],
    ],
)
def test_modalidades_persistem_analisar_validado(app, modalidades):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar(f"mod-{''.join(modalidades)}@test.com")
        pedido = _pedido(perfil, modalidades)
        estado = {}
        corpo = _descobrir(escopo, dataset_id, pedido, _transporte(estado, _condicional(perfil)))
        resultado = corpo["resultado"]
        assert resultado["contrato"] == "analisar.v1"
        assert resultado["provedor"] == "anthropic"
        assert resultado["chamada_anthropic"] is True
        assert resultado["apto_anthropic"] is False
        assert resultado["contrato_saida_validado"] is True
        assert resultado["modalidades"] == [item for item in ("A", "B", "C") if item in modalidades]
        gravado = obter_analise(escopo, corpo["analise_id"])
        assert gravado["estado"] == "concluida"
        assert gravado["resultado"]["contrato"] == "analisar.v1"
        assert gravado["resultado"]["provedor"] == "anthropic"
        assert estado["chamadas"] == 1
        assert "nao comprovam significado de negocio" in estado["pedido"]["system"]
        assert "Nao invente" in estado["pedido"]["system"]
        mensagem = _mensagem(estado)
        assert mensagem["intencoes"] == []
        assert all(coluna.get("nome") is None for coluna in mensagem["colunas"])
        assert SENTINELA not in estado["pedido"]["messages"][0]["content"]
        assert "qtd" not in estado["pedido"]["messages"][0]["content"]


def test_referencia_b_entra_no_payload_autorizado_e_nao_e_executada(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("ref-b@test.com")
        medida = _coord(_medida(perfil))
        pedido = _pedido(
            perfil,
            ["B", "C"],
            texto="usar a referencia recebida",
            referencia=_referencia(perfil, texto="soma da coluna", formulas=[FORMULA_SOMA, FORMULA_LIVRE]),
        )
        estado = {}
        corpo = _descobrir(escopo, dataset_id, pedido, _transporte(estado, _condicional(perfil)))
        mensagem = _mensagem(estado)
        referencia = mensagem["referencia"]
        assert referencia["afirmacoes"][0]["medida"] == medida
        assert referencia["afirmacoes"][0]["texto"] == "soma da coluna"
        textos = [item["texto"] for item in referencia["formulas"]]
        assert FORMULA_SOMA in textos
        assert FORMULA_LIVRE in textos
        assert mensagem["dataset"]["id"] == perfil["dataset"]["id"]
        assert mensagem["dataset"]["versao"] == perfil["dataset"]["versao"]
        assert mensagem["dataset"]["impressao"] == perfil["dataset"]["impressao"]
        assert mensagem["intencao"] == "usar a referencia recebida"
        saida = json.dumps(corpo["resultado"], ensure_ascii=False)
        assert FORMULA_SOMA not in saida
        assert FORMULA_LIVRE not in saida
        codigos = {item["codigo"] for item in corpo["resultado"]["critica_referencia"]}
        assert "formula_nao_catalogada" in codigos
        assert all(item["origem"] != "referencia" for item in corpo["resultado"]["sugestoes"])


def test_intencao_c_minimizada_e_a_versao_enviada(app, monkeypatch):
    """A versão minimizada que ainda contém texto é a que segue ao modelo.

    A persistência oficial recusa gravar o pedido se ele próprio não puder
    permanecer intacto. O envelope de gravação continua autorizado; só a
    representação de transferência troca a intenção.
    """

    def governa(conteudo, **_kwargs):
        if isinstance(conteudo, dict) and set(conteudo) == {"conteudo"}:
            return GovernanceResult(
                decision=DECISION_ALLOW,
                safe_content=copy.deepcopy(conteudo),
                issued_by_cleiton=True,
            )
        seguro = copy.deepcopy(conteudo)
        if isinstance(seguro, dict) and isinstance(seguro.get("intencao"), str):
            seguro["intencao"] = "comparar a estrutura"
        return GovernanceResult(
            decision=DECISION_MINIMIZE,
            safe_content=seguro,
            reason_codes=["incidental_data_minimized"],
            categories_detected=["phone"],
            categories_removed=["phone"],
            issued_by_cleiton=True,
        )

    monkeypatch.setattr("app.services.agente_bi_integracao_service.govern_outbound_content", governa)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("intencao-c@test.com")
        frase = "comparar a estrutura reservada"
        estado = {}
        _descobrir(
            escopo,
            dataset_id,
            _pedido(perfil, ["C"], texto=frase),
            _transporte(estado, _condicional(perfil)),
        )
        assert _mensagem(estado)["intencao"] == "comparar a estrutura"
        assert frase not in estado["pedido"]["messages"][0]["content"]
        assert estado["chamadas"] == 1


def test_telefone_com_pista_nao_e_gravado_nem_chamado(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("telefone-c@test.com")
        antes = IaChamadaTentativa.query.count()
        execucoes = AgenteBiExecucao.query.count()
        estado = {"chamadas": 0}
        frase = f"comparar telefone {TELEFONE}"
        with pytest.raises(DescobertaErro) as erro:
            _descobrir(
                escopo,
                dataset_id,
                _pedido(perfil, ["C"], texto=frase),
                _transporte(estado, _condicional(perfil)),
            )
        assert erro.value.codigo == "conteudo_nao_permitido"
        assert TELEFONE not in erro.value.codigo
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == antes
        assert IaConsumoEvento.query.count() == 0
        assert AgenteBiExecucao.query.count() == execucoes


def test_intencao_c_removida_nao_chama_nem_registra_consumo(app, monkeypatch):
    def zera(conteudo, **_kwargs):
        seguro = copy.deepcopy(conteudo)
        if isinstance(seguro, dict):
            seguro["intencao"] = None
        return GovernanceResult(
            decision=DECISION_MINIMIZE,
            safe_content=seguro,
            reason_codes=["incidental_data_minimized"],
            categories_detected=["person_name"],
            categories_removed=["person_name"],
            issued_by_cleiton=True,
        )

    monkeypatch.setattr("app.services.agente_bi_integracao_service.govern_outbound_content", zera)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("intencao-removida@test.com")
        antes = IaChamadaTentativa.query.count()
        execucoes = AgenteBiExecucao.query.count()
        estado = {"chamadas": 0}
        frase = "quero comparar a quantidade transportada"
        with pytest.raises(DescobertaErro) as erro:
            _descobrir(
                escopo,
                dataset_id,
                _pedido(perfil, ["C"], texto=frase),
                _transporte(estado, _condicional(perfil)),
            )
        assert erro.value.codigo == "intencao_nao_transportada"
        assert "intencao_nao_transportada" in erro.value.evidencias
        assert frase not in json.dumps(erro.value.evidencias, ensure_ascii=False)
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == antes
        assert IaConsumoEvento.query.count() == 0
        assert AgenteBiExecucao.query.count() == execucoes


def test_caminho_real_nao_acrescenta_conclusao_lexical(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("lexical@test.com")
        pedido = _pedido(perfil, ["C"], texto="evolucao do total")
        estado = {}
        corpo = _descobrir(escopo, dataset_id, pedido, _transporte(estado, _condicional(perfil)))
        assert _mensagem(estado)["intencoes"] == []
        assert _mensagem(estado)["intencao"] == "evolucao do total"
        sugestoes = corpo["resultado"]["sugestoes"]
        assert len(sugestoes) == 1
        assert sugestoes[0]["status"] == "condicional"
        assert sugestoes[0]["operacao"] is None
        assert corpo["resultado"]["descobertas"] == []
        assert all(item["codigo"] != "escolher_interpretacao" for item in corpo["resultado"]["perguntas"])
        assert all(item.get("lacuna") != "dimensao_temporal_ausente" for item in sugestoes)
        with pytest.raises(DescobertaErro) as recusa:
            aprovar_selecao(
                escopo,
                {
                    "analise_id": corpo["analise_id"],
                    "sugestoes": [sugestoes[0]["id"]],
                    "perguntas": _confirmadas(corpo["resultado"]),
                    "confirmacao": True,
                },
            )
        assert recusa.value.codigo == "sugestao_nao_executavel"


def test_referencias_inexistentes_e_alteradas_sao_recusadas(app, monkeypatch):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("ref-ruim@test.com")
        pedido = _pedido(
            perfil,
            ["B"],
            referencia=_referencia(perfil, indice=99, formulas=[FORMULA_LIVRE]),
        )
        estado = {}
        corpo = _descobrir(escopo, dataset_id, pedido, _transporte(estado, _condicional(perfil)))
        codigos = {item["codigo"] for item in corpo["resultado"]["critica_referencia"]}
        assert "referencia_inexistente" in codigos
        assert "formula_nao_catalogada" in codigos
        assert all(item["origem"] != "referencia" for item in corpo["resultado"]["sugestoes"])
        assert estado["chamadas"] == 1

    def troca_indice(conteudo, **_kwargs):
        seguro = copy.deepcopy(conteudo)
        seguro["referencia"]["afirmacoes"][0]["medida"]["indice"] += 1
        return GovernanceResult(
            decision=DECISION_MINIMIZE,
            safe_content=seguro,
            reason_codes=["incidental_data_minimized"],
            issued_by_cleiton=True,
        )

    monkeypatch.setattr("app.services.agente_bi_integracao_service.govern_outbound_content", troca_indice)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("ref-trocada@test.com")
        antes = IaChamadaTentativa.query.count()
        estado = {"chamadas": 0}
        with pytest.raises(DescobertaErro) as erro:
            _descobrir(
                escopo,
                dataset_id,
                _pedido(perfil, ["B"]),
                _transporte(estado, _condicional(perfil)),
            )
        assert erro.value.codigo == "contexto_nao_autorizado"
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == antes


def test_selecao_de_ate_seis_graficos_nao_herda_chamada(app):
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("selecao@test.com")
        _outro, _franquia_b, escopo_b, _dataset_b, _perfil_b = _preparar("selecao-b@test.com")
        pedido = _pedido(perfil, ["A"])
        proposta = provedor_simulado(montar_contexto(perfil, pedido))
        estado = {}
        corpo = _descobrir(escopo, dataset_id, pedido, _transporte(estado, proposta))
        viaveis = _viaveis(corpo["resultado"])
        assert viaveis
        escolhidas = viaveis[:6]
        with pytest.raises(DescobertaErro) as limite:
            aprovar_selecao(
                escopo,
                {
                    "analise_id": corpo["analise_id"],
                    "sugestoes": ["s1", "s2", "s3", "s4", "s5", "s6", "s7"],
                    "perguntas": _confirmadas(corpo["resultado"]),
                    "confirmacao": True,
                },
            )
        assert limite.value.codigo == "limite_graficos"
        with pytest.raises(DescobertaErro) as inexistente:
            aprovar_selecao(
                escopo,
                {
                    "analise_id": corpo["analise_id"],
                    "sugestoes": ["s99"],
                    "perguntas": _confirmadas(corpo["resultado"]),
                    "confirmacao": True,
                },
            )
        assert inexistente.value.codigo == "sugestao_inexistente"
        aprovada = aprovar_selecao(
            escopo,
            {
                "analise_id": corpo["analise_id"],
                "sugestoes": escolhidas,
                "perguntas": _confirmadas(corpo["resultado"]),
                "confirmacao": True,
            },
        )
        selecao = aprovada["selecao"]["resultado"]
        assert selecao["contrato"] == "selecao.descoberta.v1"
        assert selecao["chamada_anthropic"] is False
        assert selecao["apto_anthropic"] is False
        assert selecao["origem_analise_id"] == corpo["analise_id"]
        assert len(selecao["graficos"]) == len(escolhidas) <= 6
        recuperada = obter_analise(escopo, aprovada["selecao"]["id"])
        assert recuperada["resultado"]["chamada_anthropic"] is False
        assert recuperada["resultado"]["origem_analise_id"] == corpo["analise_id"]
        with pytest.raises(AgenteBiErro) as alheio:
            obter_analise(escopo_b, corpo["analise_id"])
        assert alheio.value.codigo == "nao_encontrado"
        assert usuario.id != _outro.id


def test_pedido_identico_reutiliza_sem_segunda_chamada(app):
    with app.app_context():
        _usuario, franquia, escopo, dataset_id, perfil = _preparar("idempotente@test.com")
        pedido = _pedido(perfil, ["A", "C"], texto="apenas a estrutura")
        estado = {}
        antes = _foto(franquia.id)
        primeira = _descobrir(escopo, dataset_id, pedido, _transporte(estado, _condicional(perfil)))
        cobrado = _foto(franquia.id)
        assert cobrado[0] > antes[0]
        assert IaConsumoEvento.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert IaChamadaTentativa.query.count() == 1
        segunda = _descobrir(escopo, dataset_id, pedido, _transporte(estado, _condicional(perfil)))
        assert segunda["analise_id"] == primeira["analise_id"]
        assert estado["chamadas"] == 1
        assert IaChamadaTentativa.query.count() == 1
        assert IaConsumoEvento.query.count() == 1
        assert _foto(franquia.id) == cobrado
        outra = _descobrir(
            escopo,
            dataset_id,
            _pedido(perfil, ["C"], texto="um pedido diferente"),
            _transporte(estado, _condicional(perfil)),
        )
        assert outra["analise_id"] != primeira["analise_id"]
        assert estado["chamadas"] == 2
        from app.models import AgenteBiDataset

        linha = db.session.get(AgenteBiDataset, dataset_id)
        base = _identidade_operacao(escopo, linha, pedido)
        outra_versao = SimpleNamespace(id=linha.id, versao=int(linha.versao) + 1, impressao=linha.impressao)
        assert _identidade_operacao(escopo, outra_versao, pedido) != base


def _contagens():
    return (
        AgenteBiAnalise.query.count(),
        AgenteBiExecucao.query.count(),
        IaChamadaTentativa.query.count(),
        IaConsumoEvento.query.count(),
    )


def _pedido_gravado(escopo, analise_id):
    gravado = obter_analise(escopo, analise_id)
    return gravado["regras_negocio"]["pedido"], gravado["chave_recuperacao"]


def test_pedidos_distintos_nao_compartilham_analise(app, monkeypatch):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colisao@test.com")
        primeiro = _pedido(perfil, ["C"], texto="primeiro pedido")
        segundo = _pedido(perfil, ["C"], texto="segundo pedido")
        usos = {"n": 0}
        real = _proxima_rodada

        def mesma_rodada(escopo_rodada, chave):
            usos["n"] += 1
            if usos["n"] <= 2:
                return 1
            return real(escopo_rodada, chave)

        monkeypatch.setattr(
            "app.services.agente_bi_descoberta_service._proxima_rodada",
            mesma_rodada,
        )
        estado = {"chamadas": 0}
        transporte = _transporte(estado, _condicional(perfil))
        corpo_a = _descobrir(escopo, dataset_id, primeiro, transporte)
        corpo_b = _descobrir(escopo, dataset_id, segundo, transporte)
        assert corpo_b["analise_id"] != corpo_a["analise_id"]
        assert corpo_b["resultado"] is not corpo_a["resultado"]
        pedido_a, chave_a = _pedido_gravado(escopo, corpo_a["analise_id"])
        pedido_b, chave_b = _pedido_gravado(escopo, corpo_b["analise_id"])
        assert pedido_a["texto"] == "primeiro pedido"
        assert pedido_b["texto"] == "segundo pedido"
        assert chave_a != chave_b
        dataset = db.session.get(AgenteBiDataset, dataset_id)
        assert chave_a == _identidade_operacao(escopo, dataset, _validar_pedido(primeiro))
        assert chave_b == _identidade_operacao(escopo, dataset, _validar_pedido(segundo))
        assert AgenteBiExecucao.query.filter_by(analise_id=corpo_a["analise_id"]).count() == 1
        assert AgenteBiExecucao.query.filter_by(analise_id=corpo_b["analise_id"]).count() == 1
        assert estado["chamadas"] == 2
        repetida = _descobrir(escopo, dataset_id, primeiro, transporte)
        assert repetida["analise_id"] == corpo_a["analise_id"]
        assert estado["chamadas"] == 2
        assert IaChamadaTentativa.query.count() == 2
        assert IaConsumoEvento.query.count() == 2


def test_colisao_persistente_devolve_conflito_sem_analise_alheia(app, monkeypatch):
    monkeypatch.setattr("app.services.agente_bi_descoberta_service._proxima_rodada", lambda _escopo, _chave: 1)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colisao-limite@test.com")
        estado = {"chamadas": 0}
        transporte = _transporte(estado, _condicional(perfil))
        primeira = _descobrir(escopo, dataset_id, _pedido(perfil, ["C"], texto="pedido um"), transporte)
        with pytest.raises(DescobertaErro) as erro:
            _descobrir(escopo, dataset_id, _pedido(perfil, ["C"], texto="pedido dois"), transporte)
        assert erro.value.codigo == "rodada_existente"
        assert erro.value.status == 409
        dataset = db.session.get(AgenteBiDataset, dataset_id)
        assert AgenteBiAnalise.query.filter_by(usuario_id=escopo.usuario_id, chave=dataset.chave).count() == 1
        assert AgenteBiExecucao.query.filter_by(analise_id=primeira["analise_id"]).count() == 1
        assert estado["chamadas"] == 1
        pedido, _chave = _pedido_gravado(escopo, primeira["analise_id"])
        assert pedido["texto"] == "pedido um"


def test_abertura_nao_enfileira_rodada_de_outro_pedido(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("abertura-colisao@test.com")
        dataset = db.session.get(AgenteBiDataset, dataset_id)
        pedido_a = _pedido(perfil, ["C"], texto="pedido da rodada")
        pedido_b = _pedido(perfil, ["C"], texto="outro pedido")
        chave_a = _identidade_operacao(escopo, dataset, pedido_a)
        chave_b = _identidade_operacao(escopo, dataset, pedido_b)
        abertura = abrir_operacao(
            escopo,
            dataset_id,
            pedido_a,
            chave_analise=dataset.chave,
            rodada=1,
            chave_idempotencia=chave_a,
            chave_recuperacao=chave_a,
        )
        with pytest.raises(IntegracaoErro) as erro:
            abrir_operacao(
                escopo,
                dataset_id,
                pedido_b,
                chave_analise=dataset.chave,
                rodada=1,
                chave_idempotencia=chave_b,
                chave_recuperacao=chave_b,
            )
        assert erro.value.codigo == "rodada_existente"
        assert AgenteBiExecucao.query.filter_by(analise_id=abertura["analise_id"]).count() == 1
        assert AgenteBiAnalise.query.filter_by(usuario_id=escopo.usuario_id, chave=dataset.chave).count() == 1
        assert AgenteBiAnalise.query.filter_by(chave_recuperacao=chave_b).count() == 0


def test_chamador_sem_chave_recuperacao_reutiliza_a_rodada(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, _perfil = _preparar("legado-rodada@test.com")
        dataset = db.session.get(AgenteBiDataset, dataset_id)
        primeira = abrir_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            chave_analise=dataset.chave,
            rodada=1,
        )
        segunda = abrir_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            chave_analise=dataset.chave,
            rodada=1,
        )
        assert segunda["analise_id"] == primeira["analise_id"]
        assert segunda["reusada"] is True
        assert AgenteBiExecucao.query.filter_by(analise_id=primeira["analise_id"]).count() == 1


def test_modalidade_c_sem_intencao_nao_cria_operacao(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("c-vazia@test.com")
        referencia = _referencia(perfil)
        pedidos = [
            {"modalidades": ["C"]},
            {"modalidades": ["C"], "texto": "   "},
            {"modalidades": ["A", "C"]},
            {"modalidades": ["A", "C"], "texto": " \t "},
            {"modalidades": ["B", "C"], "referencia": referencia},
            {"modalidades": ["B", "C"], "texto": "  ", "referencia": referencia},
        ]
        for pedido in pedidos:
            antes = _contagens()
            estado = {"chamadas": 0}
            with pytest.raises(DescobertaErro) as erro:
                _descobrir(escopo, dataset_id, pedido, _transporte(estado, _condicional(perfil)))
            assert erro.value.codigo == "pedido_invalido"
            assert erro.value.status == 422
            assert estado["chamadas"] == 0
            assert _contagens() == antes


@pytest.mark.parametrize(("texto", "email"), [("ok", "c-curta-ok@test.com"), ("a", "c-curta-a@test.com"), ("§?", "c-curta-simbolos@test.com")])
def test_intencao_curta_ou_com_simbolos_segue(app, texto, email):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar(email)
        estado = {}
        _descobrir(escopo, dataset_id, _pedido(perfil, ["C"], texto=texto), _transporte(estado, _condicional(perfil)))
        assert _mensagem(estado)["intencao"] == texto
        assert estado["chamadas"] == 1


def test_execucao_recuperada_sem_intencao_nao_chama(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("c-fronteira@test.com")
        dataset = db.session.get(AgenteBiDataset, dataset_id)
        pedido = {"modalidades": ["A", "C"], "texto": "   "}
        identidade = _identidade_operacao(escopo, dataset, pedido)
        estado = {"chamadas": 0}
        antes = IaChamadaTentativa.query.count()
        resultado = executar_operacao(
            escopo,
            dataset_id,
            pedido,
            worker_id=f"descoberta-{escopo.usuario_id}-fronteira",
            chave_analise=dataset.chave,
            transporte=_transporte(estado, _condicional(perfil)),
            autorizado=True,
            chave_idempotencia=identidade,
            chave_recuperacao=identidade,
        )
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == antes
        assert IaConsumoEvento.query.count() == 0
        assert resultado["ciclo"]["execucao"]["ultimo_erro"] == "pedido_invalido"
        assert resultado["saida"] is None


def test_rota_recusa_c_sem_texto(app, monkeypatch):
    cliente = _cliente(app, monkeypatch)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, _perfil = _preparar("c-rota@test.com")
        usuario_id = escopo.usuario_id
        antes = _contagens()
    _entrar(cliente, usuario_id)
    resposta = cliente.post(
        "/api/agente-bi/descoberta",
        json={"dataset_id": dataset_id, "pedido": {"modalidades": ["C"], "texto": "   "}},
    )
    assert resposta.status_code == 422
    assert resposta.get_json()["codigo"] == "pedido_invalido"
    with app.app_context():
        assert _contagens() == antes


def test_requisicao_concorrente_nao_abre_segunda_chamada(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("concorrente@test.com")
        pedido = _pedido(perfil, ["A"])
        proposta = _condicional(perfil)
        estado = {"chamadas": 0, "concorrente": None}

        def enviar(requisicao):
            estado["chamadas"] += 1
            estado["pedido"] = requisicao
            if estado["chamadas"] == 1:
                try:
                    descobrir(
                        escopo,
                        {"dataset_id": dataset_id, "pedido": pedido},
                        transporte=enviar,
                    )
                except DescobertaErro as exc:
                    estado["concorrente"] = exc.codigo
            return _resposta(json.dumps(proposta, ensure_ascii=False))

        corpo = _descobrir(escopo, dataset_id, pedido, enviar)
        assert estado["concorrente"] == "operacao_em_andamento"
        assert estado["chamadas"] == 1
        assert corpo["resultado"]["provedor"] == "anthropic"
        repetida = _descobrir(escopo, dataset_id, pedido, enviar)
        assert repetida["analise_id"] == corpo["analise_id"]
        assert estado["chamadas"] == 1
        assert AgenteBiExecucao.query.filter_by(analise_id=corpo["analise_id"]).count() == 1


def test_timeout_e_resposta_invalida_nao_repetem_chamada_fisica(app):
    with app.app_context():
        _usuario, franquia, escopo, dataset_id, perfil = _preparar("timeout@test.com")
        pedido = _pedido(perfil, ["A"])
        estado = {"chamadas": 0}

        def estoura(_pedido):
            estado["chamadas"] += 1
            raise ErroTransporte("timeout", incerto=True)

        with pytest.raises(DescobertaErro) as timeout:
            _descobrir(escopo, dataset_id, pedido, estoura)
        assert timeout.value.codigo == "efeito_incerto"
        with pytest.raises(DescobertaErro):
            _descobrir(escopo, dataset_id, pedido, estoura)
        assert estado["chamadas"] == 1
        assert IaChamadaTentativa.query.count() == 1

        _usuario_b, franquia_b, escopo_b, dataset_b, perfil_b = _preparar("invalida@test.com")
        antes = _foto(franquia_b.id)
        invalido = {"chamadas": 0}

        def quebra(_pedido):
            invalido["chamadas"] += 1
            return _resposta("{")

        with pytest.raises(DescobertaErro) as recusa:
            _descobrir(escopo_b, dataset_b, _pedido(perfil_b, ["A"]), quebra)
        assert recusa.value.codigo == "contrato_saida_invalido"
        with pytest.raises(DescobertaErro):
            _descobrir(escopo_b, dataset_b, _pedido(perfil_b, ["A"]), quebra)
        assert invalido["chamadas"] == 1
        assert _foto(franquia_b.id)[0] > antes[0]
        assert IaConsumoEvento.query.filter_by(franquia_id=franquia_b.id).count() == 1
        assert franquia.id != franquia_b.id


def test_falha_antes_da_chamada_permite_repeticao_segura(app, monkeypatch):
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_MODEL", raising=False)
    with app.app_context():
        _usuario, franquia, escopo, dataset_id, perfil = _preparar("modelo-ausente@test.com")
        antes = _foto(franquia.id)
        estado = {"chamadas": 0}
        with pytest.raises(DescobertaErro) as erro:
            _descobrir(escopo, dataset_id, _pedido(perfil, ["A"]), _transporte(estado, _condicional(perfil)))
        assert erro.value.codigo == "modelo_ausente"
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == 0
        assert _foto(franquia.id) == antes
        monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-4-5")
        corpo = _descobrir(escopo, dataset_id, _pedido(perfil, ["A"]), _transporte(estado, _condicional(perfil)))
        assert corpo["resultado"]["chamada_anthropic"] is True
        assert estado["chamadas"] == 1


def test_flag_desligada_preserva_o_simulador(app, monkeypatch):
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_ENABLED", raising=False)
    with app.app_context():
        _usuario, franquia, escopo, dataset_id, perfil = _preparar("simulador@test.com")
        antes_tentativas = IaChamadaTentativa.query.count()
        antes_execucoes = AgenteBiExecucao.query.count()
        antes = _foto(franquia.id)
        estado = {"chamadas": 0}
        corpo = _descobrir(escopo, dataset_id, _pedido(perfil, ["A", "C"], texto="soma do total"), _transporte(estado, _condicional(perfil)))
        assert corpo["resultado"]["provedor"] == "simulado"
        assert corpo["resultado"]["chamada_anthropic"] is False
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == antes_tentativas
        assert AgenteBiExecucao.query.count() == antes_execucoes
        assert _foto(franquia.id) == antes
        assert any(item.get("operacao") == "soma" for item in corpo["resultado"]["sugestoes"])


def test_rota_publica_resultado_persistido_sem_sdk(app, monkeypatch):
    cliente = _cliente(app, monkeypatch)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("rota@test.com")
        usuario_id = escopo.usuario_id
        proposta = _condicional(perfil)

    def enviar(pedido):
        assert "nao comprovam significado de negocio" in pedido["system"]
        return _resposta(json.dumps(proposta, ensure_ascii=False))

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._transporte_sdk",
        lambda _timeout: enviar,
    )
    _entrar(cliente, usuario_id)
    resposta = cliente.post(
        "/api/agente-bi/descoberta",
        json={"dataset_id": dataset_id, "pedido": {"modalidades": ["A"]}},
    )
    assert resposta.status_code == 201
    corpo = resposta.get_json()
    assert corpo["resultado"]["provedor"] == "anthropic"
    assert corpo["resultado"]["contrato"] == "analisar.v1"
    fonte = (ROOT / "app" / "static" / "js" / "agente_bi_descoberta.js").read_text(encoding="utf-8")
    assert "Na modalidade C, escreva a intenção." in fonte
    assert "intencao_nao_transportada" in fonte
    assert "operacao_em_andamento" in fonte
    assert "chamar_anthropic" not in fonte
    assert agente_bi_api_bp.name == "agente_bi_api"
    assert login_manager.login_view


def test_instrucao_nao_trata_tipo_fisico_como_kpi():
    instrucao = instrucao_de_rascunho()
    assert "nao comprovam significado de negocio" in instrucao
    assert "KPI validado" in instrucao
    assert "Nao execute formula" in instrucao
    assert "intencoes e classificacao lexical" in instrucao
    assert "Nao invente" in instrucao
