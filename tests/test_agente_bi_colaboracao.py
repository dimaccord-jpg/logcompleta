"""Colaboração Claude–Gemini na descoberta, com transportes falsos.

Não abre SDK, não grava migration e não liga worker. A flag desligada
deixa o fluxo legado de uma chamada Anthropic.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Query

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiExecucao,
    Franquia,
    IaChamadaTentativa,
    IaConsumoEvento,
)
from app.services.agente_bi_analisar_service import (
    OPERACOES,
    AnalisarErro,
    _EVIDENCIAS,
    analisar,
    instrucao_de_rascunho,
    montar_contexto,
    validar_saida,
)
from app.services.agente_bi_anthropic_service import ErroTransporte
from app.services.agente_bi_calculo_service import CalculoErro, calcular_definicao
from app.services.agente_bi_colaboracao_service import (
    CONTRATO_TECNICO,
    _CHECKPOINT,
    _CHAVES_CRITICA,
    _pai_vinculado,
    _representar_proposta,
    esquema_critica,
    identidade_rodada,
    instrucao_de_critica,
    instrucao_de_revisao,
    minimizar_critica,
    revalidar_contrato_tecnico,
)
from app.services.agente_bi_execucao_service import ExecucaoErro, _referencias
from app.services.cleiton_ai_data_governance import PURPOSE_BI_LOGISTICO, govern_provider_config
from tests.test_agente_bi_analisar import SENTINELA
from app.services.agente_bi_descoberta_service import DescobertaErro, aprovar_selecao, descobrir
from app.services.agente_bi_definir_service import DefinirErro, definir_selecao
from app.services.agente_bi_persistencia_service import AgenteBiErro, criar_dashboard, obter_analise
from tests.test_agente_bi_descoberta_claude import (
    _condicional,
    _coord,
    _medida,
    _pedido,
    _preparar,
    _regiao,
    _resposta,
)
from tests.test_agente_bi_fundacao import _dashboard
from tests.test_agente_bi_ingestao import _cliente, _entrar


ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "app" / "static" / "js" / "agente_bi_descoberta.js"


@pytest.fixture(autouse=True)
def _colaboracao_falsa(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-4-5")
    monkeypatch.setenv("AGENTE_BI_COLABORACAO_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY_1", raising=False)
    monkeypatch.delenv("AGENTE_BI_WORKER_HABILITADO", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)
    monkeypatch.setattr("app.services.agente_bi_gemini_service.montar_cliente_gemini", proibido)


class _Claude:
    def __init__(self, propostas):
        self.propostas = list(propostas)
        self.chamadas = 0
        self.pedidos = []

    def enviar(self, pedido):
        self.chamadas += 1
        self.pedidos.append(pedido)
        proposta = self.propostas[min(self.chamadas - 1, len(self.propostas) - 1)]
        if isinstance(proposta, BaseException):
            raise proposta
        if callable(proposta):
            proposta = proposta(pedido)
        return _resposta(json.dumps(proposta, ensure_ascii=False), entrada=20, saida=20)


class _Gemini:
    def __init__(self, texto):
        self.geracoes = []
        self.contagens = []
        self.http_options = SimpleNamespace(retry_options=SimpleNamespace(attempts=1))
        self.models = self
        self._texto = texto

    def count_tokens(self, *, model, contents, config=None):
        if isinstance(config, dict) and (
            config.get("system_instruction") is not None or config.get("systemInstruction") is not None
        ):
            raise ValueError("system_instruction parameter is not supported in Gemini API.")
        if config is not None and not isinstance(config, dict):
            if getattr(config, "system_instruction", None) is not None:
                raise ValueError("system_instruction parameter is not supported in Gemini API.")
        self.contagens.append({"model": model, "contents": contents, "config": config})
        from app.services.cleiton_billable_ai_call import estimar_tokens_entrada

        tokens, erro = estimar_tokens_entrada(contents, config)
        if erro:
            raise RuntimeError("count")
        return SimpleNamespace(total_tokens=tokens, cached_content_token_count=None)

    def generate_content(self, *, model, contents, config=None):
        self.geracoes.append({"model": model, "contents": contents, "config": config})
        return SimpleNamespace(
            text=self._texto,
            usage_metadata=SimpleNamespace(
                prompt_token_count=20,
                candidates_token_count=20,
                total_token_count=40,
            ),
        )


def _critica(perfil):
    return {
        "referencias": [_regiao(perfil)],
        "medidas": [_coord(_medida(perfil))],
        "operacoes": ["soma"],
        "evidencias": [],
        "objecoes": [{"codigo": "evidencia_ausente", "referencia": _coord(_medida(perfil))}],
    }


def _ausente(perfil):
    return {
        "descobertas": [],
        "sugestoes": [
            {
                "status": "viavel",
                "leitura": "indicador_de_medida",
                "grafico": "indicador",
                "operacao": "soma",
                "origem": "dados",
                "regiao": _regiao(perfil),
                "medida": _coord(_medida(perfil)),
                "agrupamento": None,
                "segunda_medida": None,
                "evidencias": ["aba_oculta"],
                "juncao": "nao_executada",
                "lacuna": None,
                "duvidas": [],
            }
        ],
    }


def _descobrir(escopo, dataset_id, pedido, claude, gemini, controle=None):
    return descobrir(
        escopo,
        {"dataset_id": dataset_id, "pedido": pedido},
        transporte=claude.enviar,
        cliente_gemini=gemini,
        controle=controle,
    )


def _ate(escopo, dataset_id, pedido, claude, gemini, limite=6):
    corpo = None
    for _ in range(limite):
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        colaboracao = corpo.get("colaboracao") or {}
        if corpo.get("resultado") or colaboracao.get("continuar") is not True:
            return corpo
    raise AssertionError(corpo)


def _tecnicas(dataset_id):
    return (
        AgenteBiAnalise.query.filter_by(dataset_id=dataset_id, contrato_versao=CONTRATO_TECNICO)
        .order_by(AgenteBiAnalise.id.asc())
        .all()
    )


def _finais(dataset_id):
    return AgenteBiAnalise.query.filter_by(dataset_id=dataset_id, contrato_versao="analisar.v1").all()


def _eventos(usuario_id):
    return IaConsumoEvento.query.filter_by(usuario_id=usuario_id).all()


def _tentativas(usuario_id):
    return IaChamadaTentativa.query.filter_by(usuario_id=usuario_id).all()


def test_ciclo_valido_publica_analisar_sem_nova_chamada(app):
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-ciclo@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        assert corpo["resultado"]["contrato_saida_validado"] is True
        assert corpo["resultado"]["provedor"] == "anthropic"
        assert corpo["colaboracao"] == {"estado": "concluida", "fase": "pronto", "continuar": False}
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert len(_tecnicas(dataset_id)) == 3
        assert len(_finais(dataset_id)) == 1
        repetido = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert repetido["analise_id"] == corpo["analise_id"]
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert len(_eventos(usuario.id)) == len(_tentativas(usuario.id)) == 3
        provedores = {item.provider for item in _tentativas(usuario.id)}
        assert provedores == {"anthropic", "gemini"}


def test_evidencia_ausente_fica_interna_e_a_revisao_aprova(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-ausente@test.com")
        pedido = _pedido(perfil, ["A"])
        with pytest.raises(AnalisarErro) as erro:
            analisar(perfil, pedido, provedor=lambda _contexto: _ausente(perfil))
        assert erro.value.codigo == "evidencia_ausente"
        claude = _Claude([_ausente(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        primeira = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert primeira["colaboracao"]["continuar"] is True
        assert primeira["colaboracao"]["fase"] == "gemini_revisando"
        assert "resultado" not in primeira
        tecnicas = _tecnicas(dataset_id)
        assert len(tecnicas) == 1
        documento = json.loads(tecnicas[0].resultado_json)
        assert documento["contrato"] == CONTRATO_TECNICO
        assert documento["verificacao"] == {"aprovado": False, "codigo": "evidencia_ausente"}
        assert _finais(dataset_id) == []
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        assert corpo["colaboracao"]["fase"] == "pronto"
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        publicada = db.session.get(AgenteBiAnalise, corpo["analise_id"])
        assert publicada.contrato_versao == "analisar.v1"
        assert publicada.estado == "concluida"
        assert all(item.contrato_versao == CONTRATO_TECNICO for item in _tecnicas(dataset_id))


def test_proposta_nao_minimizavel_nao_grava_texto(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-bruto@test.com")
        pedido = _pedido(perfil, ["A"])
        bruto = "texto bruto do provedor que nao pode ser persistido"
        claude = _Claude([bruto])
        claude.enviar = lambda pedido_chamada: (
            claude.pedidos.append(pedido_chamada),
            setattr(claude, "chamadas", claude.chamadas + 1),
            _resposta(bruto, entrada=20, saida=20),
        )[-1]
        gemini = _Gemini("{}")
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert corpo["colaboracao"]["continuar"] is False
        tecnica = _tecnicas(dataset_id)[0]
        assert tecnica.resultado_json is None
        assert tecnica.estado == "falha_validacao"
        gravado = json.dumps(
            {
                "contexto": tecnica.contexto_json,
                "regras": tecnica.regras_negocio_json,
                "resultado": tecnica.resultado_json,
            },
            default=str,
        )
        assert bruto not in gravado
        assert len(gemini.geracoes) == 0


def test_contrato_tecnico_nao_passa_pela_api_publica(app, monkeypatch):
    cliente = _cliente(app, monkeypatch)
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-api@test.com")
        usuario_id = usuario.id
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        tecnica = _tecnicas(dataset_id)[0]
        tecnica_id = int(tecnica.id)
        dataset = {
            "id": tecnica.dataset_id,
            "versao": tecnica.dataset_versao,
            "impressao": tecnica.dataset_impressao,
        }
    _entrar(cliente, usuario_id)
    lista = cliente.get("/api/agente-bi/analises")
    assert lista.status_code == 200
    assert CONTRATO_TECNICO not in lista.get_data(as_text=True)
    assert cliente.get(f"/api/agente-bi/analises/{tecnica_id}").status_code == 404
    criado = cliente.post(
        "/api/agente-bi/analises",
        json={
            "chave": "colab-publica",
            "rodada": 1,
            "dataset_id": dataset["id"],
            "versao": dataset["versao"],
            "impressao": dataset["impressao"],
            "etapa": "descoberta",
            "estado": "preparada",
            "contrato_versao": CONTRATO_TECNICO,
        },
    )
    assert criado.status_code == 422
    assert criado.get_json()["codigo"] == "contrato_reservado"


def test_artefato_tecnico_nao_entra_em_selecao_definicao_calculo_ou_dashboard(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-uso@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_ausente(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        tecnica = _tecnicas(dataset_id)[0]
        with pytest.raises(DescobertaErro) as selecao:
            aprovar_selecao(
                escopo,
                {
                    "analise_id": tecnica.id,
                    "sugestoes": ["s1"],
                    "perguntas": [],
                    "confirmacao": True,
                },
            )
        assert selecao.value.codigo == "analise_indisponivel"
        with pytest.raises(DefinirErro):
            definir_selecao(escopo, {"analise_id": int(tecnica.id)})
        with pytest.raises(CalculoErro):
            calcular_definicao(escopo, {"analise_id": int(tecnica.id)})
        with pytest.raises(AgenteBiErro) as painel:
            criar_dashboard(
                escopo,
                _dashboard(
                    {
                        "id": tecnica.dataset_id,
                        "versao": tecnica.dataset_versao,
                        "impressao": tecnica.dataset_impressao,
                    },
                    analise_id=int(tecnica.id),
                ),
            )
        assert painel.value.codigo == "identidade_invalida"
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        assert int(corpo["analise_id"]) != int(tecnica.id)


def test_referencia_juncao_e_evidencia_inventada_continuam_rejeitadas(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-ref@test.com")
        pedido = _pedido(perfil, ["A"])
        invalida = _ausente(perfil)
        invalida["sugestoes"][0]["medida"] = {**_coord(_medida(perfil)), "indice": 99}
        with pytest.raises(AnalisarErro) as referencia:
            analisar(perfil, pedido, provedor=lambda _contexto: invalida)
        assert referencia.value.codigo == "referencia_inexistente"
        claude = _Claude([invalida])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["colaboracao"]["continuar"] is True
        documento = json.loads(_tecnicas(dataset_id)[0].resultado_json)
        assert documento["verificacao"]["aprovado"] is False
        assert documento["verificacao"]["codigo"] == "referencia_inexistente"
        assert _finais(dataset_id) == []

        juncao = _ausente(perfil)
        juncao["sugestoes"][0]["juncao"] = "executada"
        assert claude.chamadas == 1
        outro = _Claude([juncao])
        recusa = _descobrir(escopo, dataset_id, _pedido(perfil, ["C"], texto="outra leitura"), outro, gemini)
        assert recusa["colaboracao"]["continuar"] is True
        assert recusa["colaboracao"]["fase"] == "gemini_revisando"
        assert outro.chamadas == 1
        assert _finais(dataset_id) == []
        documento_juncao = json.loads(_tecnicas(dataset_id)[-1].resultado_json)
        assert documento_juncao["verificacao"]["aprovado"] is False
        assert documento_juncao["proposta"] == {"descobertas": [], "sugestoes": []}
        assert documento_juncao["descartes"] == [
            {"lista": "sugestoes", "posicao": 0, "campo": "juncao", "motivo": "juncao_nao_confirmada"}
        ]
        assert "executada" not in _tecnicas(dataset_id)[-1].resultado_json
        with pytest.raises(AnalisarErro) as evidencia:
            analisar(perfil, pedido, provedor=lambda _contexto: _ausente(perfil))
        assert evidencia.value.codigo == "evidencia_ausente"
        with pytest.raises(AnalisarErro):
            validar_saida(montar_contexto(perfil, pedido), {"contrato": "analisar.v1"})


def test_reentrada_durante_a_chamada_nao_dispara_outra(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-reentrada@test.com")
        pedido = _pedido(perfil, ["A"])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        vistos = {}
        claude = _Claude([_condicional(perfil)])

        def enviar(pedido_chamada):
            claude.chamadas += 1
            if claude.chamadas == 1:
                vistos["interno"] = _descobrir(escopo, dataset_id, pedido, claude, gemini)
            return _resposta(json.dumps(_condicional(perfil)), entrada=20, saida=20)

        claude.enviar = enviar
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert claude.chamadas == 1
        assert len(gemini.geracoes) == 0
        assert vistos["interno"]["colaboracao"]["estado"] == "em_andamento"
        assert vistos["interno"]["colaboracao"]["continuar"] is True
        assert corpo["colaboracao"]["fase"] == "gemini_revisando"
        assert IaChamadaTentativa.query.filter_by(usuario_id=escopo.usuario_id, provider="anthropic").count() == 1


def test_reenvio_reusa_a_identidade_persistida(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-chave@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        tecnica = _tecnicas(dataset_id)[0]
        execucao = AgenteBiExecucao.query.filter_by(analise_id=tecnica.id).one()
        contexto = json.loads(tecnica.contexto_json)
        esperada = identidade_rodada(contexto["colaboracao_id"], None, "propor", "claude")
        assert execucao.chave_idempotencia == tecnica.chave_recuperacao == esperada
        assert "fencing" not in esperada
        tentativa = db.session.get(IaChamadaTentativa, execucao.ia_chamada_tentativa_id)
        assert tentativa.attempt_key == esperada
        _ate(escopo, dataset_id, pedido, claude, gemini)
        assert (
            IaChamadaTentativa.query.filter_by(attempt_key=esperada).count() == 1
        )
        antes = IaChamadaTentativa.query.count()
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert IaChamadaTentativa.query.count() == antes


def test_queda_antes_depois_e_na_materializacao(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-queda@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        controle = {"antes_da_chamada": lambda: (_ for _ in ()).throw(RuntimeError("antes"))}
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini, controle)
        assert claude.chamadas == 0
        assert corpo["colaboracao"]["continuar"] is True
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert claude.chamadas == 1
        assert corpo["colaboracao"]["fase"] == "gemini_revisando"

        def depois():
            raise RuntimeError("depois")

        controle = {"depois_da_resposta": depois}
        interrompida = _descobrir(escopo, dataset_id, pedido, claude, gemini, controle)
        assert len(gemini.geracoes) == 1
        assert interrompida["colaboracao"]["estado"] == "incerta"
        assert interrompida["colaboracao"]["continuar"] is False
        repetida = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert len(gemini.geracoes) == 1
        assert repetida["colaboracao"]["continuar"] is False

    with app.app_context():
        db.session.rollback()
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-material@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        quedas = {"n": 0}

        def antes():
            quedas["n"] += 1
            if quedas["n"] == 1:
                raise RuntimeError("materializacao")

        with pytest.raises(RuntimeError):
            _descobrir(escopo, dataset_id, pedido, claude, gemini, {"antes_de_materializar": antes})
        db.session.rollback()
        assert claude.chamadas == 2
        assert _finais(dataset_id) == []
        pronta = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert pronta["resultado"]["contrato"] == "analisar.v1"
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1


def test_tentativa_incerta_nao_reenvia(app):
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-incerta@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([ErroTransporte("timeout", incerto=True)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert claude.chamadas == 1
        assert corpo["colaboracao"]["estado"] == "incerta"
        assert corpo["colaboracao"]["continuar"] is False
        de_novo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert claude.chamadas == 1
        assert len(gemini.geracoes) == 0
        assert de_novo["colaboracao"]["continuar"] is False
        tentativa = _tentativas(usuario.id)[0]
        assert tentativa.status == IaChamadaTentativa.STATUS_UNCERTAIN
        assert _finais(dataset_id) == []


def test_retomada_usa_o_estado_persistido(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-retoma@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        primeira = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert primeira["colaboracao"]["continuar"] is True
        db.session.remove()
        segunda = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert segunda["colaboracao"]["fase"] == "claude_consolidando"
        assert claude.chamadas == 1
        assert len(gemini.geracoes) == 1
        pronta = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert pronta["resultado"]["contrato"] == "analisar.v1"
        recarregada = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert recarregada["analise_id"] == pronta["analise_id"]
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1


def test_isolamento_de_usuario_conta_franquia_e_dataset(app):
    with app.app_context():
        _um, _franquia, escopo, dataset_id, perfil = _preparar("colab-iso-a@test.com")
        _outro, _outra, alheio, outro_dataset, outro_perfil = _preparar("colab-iso-b@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        tecnica = _tecnicas(dataset_id)[0]
        with pytest.raises(AgenteBiErro) as erro:
            obter_analise(alheio, int(tecnica.id))
        assert erro.value.status == 404
        assert _tecnicas(outro_dataset) == []
        pedido_alheio = _pedido(outro_perfil, ["A"])
        outro_claude = _Claude([_condicional(outro_perfil)])
        _descobrir(alheio, outro_dataset, pedido_alheio, outro_claude, gemini)
        assert _tecnicas(outro_dataset)[0].usuario_id == alheio.usuario_id
        assert _tecnicas(outro_dataset)[0].conta_id == alheio.conta_id
        assert _tecnicas(outro_dataset)[0].franquia_id == alheio.franquia_id
        assert json.loads(_tecnicas(dataset_id)[0].contexto_json)["colaboracao_id"] != json.loads(
            _tecnicas(outro_dataset)[0].contexto_json
        )["colaboracao_id"]


def test_admissao_recusada_entre_rodadas_nao_gera(app):
    with app.app_context():
        _usuario, franquia, escopo, dataset_id, perfil = _preparar("colab-admissao@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert claude.chamadas == 1
        franquia = db.session.get(Franquia, franquia.id)
        franquia.bloqueio_manual = True
        db.session.commit()
        parado = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert len(gemini.geracoes) == 0
        assert parado["colaboracao"]["continuar"] is False
        assert claude.chamadas == 1
        assert _finais(dataset_id) == []


def test_consumo_de_claude_e_gemini_nao_duplica(app):
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-consumo@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _ate(escopo, dataset_id, pedido, claude, gemini)
        tentativas = _tentativas(usuario.id)
        eventos = _eventos(usuario.id)
        assert {item.provider for item in tentativas} == {"anthropic", "gemini"}
        assert len([item for item in tentativas if item.provider == "anthropic"]) == 2
        assert len([item for item in tentativas if item.provider == "gemini"]) == 1
        assert len({item.ia_consumo_evento_id for item in tentativas}) == len(tentativas)
        assert len(eventos) == len(tentativas)
        assert all(item.status == IaChamadaTentativa.STATUS_SETTLED for item in tentativas)
        fonte = (ROOT / "app" / "services" / "agente_bi_colaboracao_service.py").read_text(encoding="utf-8")
        assert "IaConsumoAbatimento" not in fonte
        antes = len(eventos)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert len(_eventos(usuario.id)) == antes


def test_pendencia_financeira_impede_nova_geracao(app):
    with app.app_context():
        usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-pendencia@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        execucao = AgenteBiExecucao.query.filter_by(analise_id=_tecnicas(dataset_id)[0].id).one()
        tentativa = db.session.get(IaChamadaTentativa, execucao.ia_chamada_tentativa_id)
        tentativa.status = IaChamadaTentativa.STATUS_UNCERTAIN
        tentativa.reserva_ativa = True
        execucao.estado = "incerta"
        execucao.efeito_externo = "incerto"
        db.session.commit()
        parado = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert parado["colaboracao"]["estado"] == "incerta"
        assert parado["colaboracao"]["continuar"] is False
        assert claude.chamadas == 1
        assert len(gemini.geracoes) == 0
        assert len(_tentativas(usuario.id)) == 1


def test_ausencia_de_progresso_encerra_sem_teto_numerico(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-progresso@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_ausente(perfil), _ausente(perfil), _ausente(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini, limite=8)
        assert corpo["colaboracao"]["estado"] == "encerrada"
        assert corpo["colaboracao"]["continuar"] is False
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert _finais(dataset_id) == []
        extra = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert extra["colaboracao"]["continuar"] is False
        assert claude.chamadas == 2
        fonte = (ROOT / "app" / "services" / "agente_bi_colaboracao_service.py").read_text(encoding="utf-8")
        assert "MAX_RODADAS" not in fonte
        assert "teto_rodadas" not in fonte


def test_validar_saida_e_analisar_v1_permanecem(app):
    fonte = inspect.getsource(validar_saida)
    assert "colaboracao.tecnica" not in fonte
    assert "CONTRATO_ANALISAR" in fonte
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-contrato@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        contexto = montar_contexto(perfil, pedido)
        with pytest.raises(AnalisarErro) as erro:
            validar_saida(contexto, dict(corpo["resultado"], contrato="outro"))
        assert erro.value.codigo == "contrato_saida_invalido"


def test_selecao_de_grafico_usa_o_resultado_aprovado(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-grafico@test.com")
        pedido = _pedido(perfil, ["A"])
        proposta = _proposta_viavel(perfil, pedido)
        claude = _Claude([proposta, proposta])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        viaveis = [
            item["id"]
            for item in corpo["resultado"]["sugestoes"]
            if item["status"] == "viavel" and item["grafico"] and item["juncao"] == "nao_executada"
        ]
        assert viaveis
        publicada = aprovar_selecao(
            escopo,
            {
                "analise_id": corpo["analise_id"],
                "sugestoes": viaveis[:1],
                "perguntas": [{"id": item["id"], "confirmada": True} for item in corpo["resultado"]["perguntas"]],
                "confirmacao": True,
            },
        )
        assert publicada["selecao"]["etapa"] == "selecao"
        assert publicada["selecao"]["resultado"]["graficos"][0]["id"] == viaveis[0]
        tecnica = _tecnicas(dataset_id)[0]
        with pytest.raises(DescobertaErro):
            aprovar_selecao(
                escopo,
                {
                    "analise_id": tecnica.id,
                    "sugestoes": viaveis[:1],
                    "perguntas": [],
                    "confirmacao": True,
                },
            )


def test_flag_desligada_preserva_o_fluxo_legado(app, monkeypatch):
    monkeypatch.delenv("AGENTE_BI_COLABORACAO_ENABLED", raising=False)
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-legado@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        assert "colaboracao" not in corpo
        assert claude.chamadas == 1
        assert len(gemini.geracoes) == 0
        assert _tecnicas(dataset_id) == []


def test_suite_nao_abre_sdk(app):
    fonte = (ROOT / "app" / "services" / "agente_bi_gemini_service.py").read_text(encoding="utf-8")
    assert "from google import genai" not in fonte.split("def montar_cliente_gemini", 1)[0]
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-sdk@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _ate(escopo, dataset_id, pedido, claude, gemini)
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1


def test_interface_retoma_sem_criar_identidade(app):
    fonte = JS.read_text(encoding="utf-8")
    assert "Claude está analisando os dados." in fonte
    assert "Gemini está revisando as evidências." in fonte
    assert "Claude está consolidando as sugestões." in fonte
    assert "Descobertas prontas para seleção." in fonte
    assert "Conteúdo sensível protegido." in fonte
    assert "localStorage" in fonte
    assert "chave_idempotencia" not in fonte
    assert "execucao_id" not in fonte
    assert "dashboard" not in fonte.lower()
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-ui@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert set(corpo["colaboracao"]) == {"estado", "fase", "continuar"}
        assert "execucao_id" not in corpo["colaboracao"]


def _proposta_viavel(perfil, pedido):
    for coluna in perfil["colunas"]:
        evidencias = [codigo for codigo in (coluna.get("evidencias") or []) if codigo != "aba_oculta"]
        if not evidencias:
            continue
        for operacao, grafico, leitura in (
            ("soma", "indicador", "indicador_de_medida"),
            ("contagem", "indicador", "contagem_de_registros"),
        ):
            item = {
                "status": "viavel",
                "leitura": leitura,
                "grafico": grafico,
                "operacao": operacao,
                "origem": "dados",
                "regiao": _regiao(perfil),
                "medida": _coord(coluna),
                "agrupamento": None,
                "segunda_medida": None,
                "evidencias": evidencias[:1],
                "juncao": "nao_executada",
                "lacuna": None,
                "duvidas": [],
            }
            proposta = {"descobertas": [], "sugestoes": [item]}
            try:
                analisar(perfil, pedido, provedor=lambda _contexto, proposta=proposta: proposta)
            except AnalisarErro:
                continue
            return proposta
    raise AssertionError("nenhuma sugestão viável no perfil de teste")


def _critica_minima():
    regiao = {"arquivo": 0, "aba": 0, "regiao": 0}
    coluna = {"arquivo": 0, "aba": 0, "regiao": 0, "indice": 1}
    return {
        "referencias": [regiao],
        "medidas": [coluna],
        "operacoes": ["soma"],
        "evidencias": ["tipo_numero_fracionado"],
        "objecoes": [{"codigo": "evidencia_ausente", "referencia": coluna}],
    }, {
        "colunas": [{**coluna, "evidencias": ["tipo_numero_fracionado"]}],
        "regioes": [{**regiao, "evidencias": ["tipo_numero_fracionado"]}],
    }


def test_protocolo_de_critica_sem_chamada_faturavel(monkeypatch):
    chamadas = {"n": 0}

    def proibido(*_args, **_kwargs):
        chamadas["n"] += 1
        raise AssertionError("faturavel")

    monkeypatch.setattr("app.services.agente_bi_gemini_service.cleiton_governed_billable_ai_call", proibido)
    monkeypatch.setattr("app.services.cleiton_billable_ai_call.cleiton_governed_billable_ai_call", proibido)
    instrucao = instrucao_de_critica()
    esquema = esquema_critica()
    for termo in (SENTINELA, "norte", "K200", "10.5", "NF-88421"):
        assert termo not in instrucao
    assert "revisor critico" in instrucao
    assert "nao e o autor da proposta final" in instrucao
    assert set(esquema["required"]) == _CHAVES_CRITICA
    assert esquema["additionalProperties"] is False
    propriedades = esquema["properties"]
    assert propriedades["operacoes"]["maxItems"] == 8
    assert propriedades["operacoes"]["items"]["enum"] == sorted(OPERACOES)
    assert propriedades["objecoes"]["maxItems"] == 8
    assert propriedades["objecoes"]["items"]["properties"]["codigo"]["enum"] == sorted(_CHECKPOINT)
    assert propriedades["evidencias"]["items"]["enum"] == sorted(_EVIDENCIAS)
    assert propriedades["referencias"]["maxItems"] == 8
    assert propriedades["medidas"]["maxItems"] == 8
    critica, contexto = _critica_minima()
    assert minimizar_critica(critica, contexto) == critica
    rejeitada = dict(critica)
    rejeitada["cliente"] = "Acme"
    assert minimizar_critica(rejeitada, contexto) is None
    config = {
        "system_instruction": instrucao,
        "response_mime_type": "application/json",
        "response_json_schema": esquema,
    }
    seguro = govern_provider_config(
        config,
        purpose=PURPOSE_BI_LOGISTICO,
        agent="agente_bi",
        provider="gemini",
    )
    assert seguro["system_instruction"] == instrucao
    assert seguro["response_mime_type"] == "application/json"
    assert seguro["response_json_schema"] == esquema
    assert "response_schema" not in seguro
    assert chamadas["n"] == 0


def test_gemini_recebe_instrucao_e_esquema_da_critica(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-protocolo@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert len(gemini.geracoes) == 1
        enviado = gemini.geracoes[0]
        config = enviado["config"]
        assert config["system_instruction"] == instrucao_de_critica()
        assert SENTINELA not in config["system_instruction"]
        assert config["response_mime_type"] == "application/json"
        assert config["response_json_schema"] == esquema_critica()
        assert "response_schema" not in config
        assert "proposta" in enviado["contents"]
        assert claude.chamadas == 1
        assert len(gemini.contagens) == 1
        contagem = gemini.contagens[0]
        assert "system_instruction" not in (contagem["config"] or {})
        assert "systemInstruction" not in (contagem["config"] or {})
        bloco = contagem["contents"][0]
        assert bloco.role == "user"
        assert [parte.text for parte in bloco.parts] == [instrucao_de_critica()]
        assert contagem["contents"][1] == enviado["contents"]
        assert contagem["contents"] is not enviado["contents"]


def _conteudo_claude(pedido):
    return json.loads(pedido["messages"][0]["content"])


def test_revisao_recupera_a_proposta_claude(app, monkeypatch):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-revisao@test.com")
        pedido = _pedido(perfil, ["A"])
        proposta = _condicional(perfil)
        claude = _Claude([proposta, proposta])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        tecnicas = _tecnicas(dataset_id)
        doc_claude = json.loads(tecnicas[0].resultado_json)
        doc_gemini = json.loads(tecnicas[1].resultado_json)
        assert doc_gemini["proposta"] is None
        assert doc_gemini["pai_id"] == tecnicas[0].id
        assert doc_gemini["pai_hash"] == doc_claude["hash"]
        assert doc_gemini["colaboracao_id"] == doc_claude["colaboracao_id"]
        assert tecnicas[0].usuario_id == tecnicas[1].usuario_id
        assert tecnicas[0].conta_id == tecnicas[1].conta_id
        assert tecnicas[0].franquia_id == tecnicas[1].franquia_id
        assert tecnicas[0].dataset_id == tecnicas[1].dataset_id
        assert tecnicas[0].dataset_versao == tecnicas[1].dataset_versao
        assert tecnicas[0].dataset_impressao == tecnicas[1].dataset_impressao
        assert tecnicas[0].contrato_versao == tecnicas[1].contrato_versao == CONTRATO_TECNICO

        from app.services import agente_bi_analisar_service as modulo

        original = modulo.validar_saida
        vistas = {"n": 0}

        def embrulho(contexto, documento):
            vistas["n"] += 1
            return original(contexto, documento)

        monkeypatch.setattr(modulo, "validar_saida", embrulho)
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        enviado = _conteudo_claude(claude.pedidos[1])
        assert enviado["proposta_anterior"] == doc_claude["proposta"] == proposta
        assert enviado["critica_autorizada"] == doc_gemini["critica"]
        assert enviado["verificacao_anterior"] == doc_claude["verificacao"]
        assert "descartes" not in doc_claude
        assert claude.pedidos[0]["system"] == instrucao_de_rascunho()
        assert instrucao_de_rascunho() in claude.pedidos[1]["system"]
        assert instrucao_de_revisao() in claude.pedidos[1]["system"]
        assert vistas["n"] >= 1
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        assert claude.chamadas == 2


def _leitura_do_pai(monkeypatch, analise_id, transformar):
    """O histórico persistido é imutável. A leitura da rodada devolve o pai adulterado."""
    original = Query.filter_by

    def filter_by(self, **kwargs):
        consulta = original(self, **kwargs)
        if kwargs.get("id") != analise_id or kwargs.get("contrato_versao") != CONTRATO_TECNICO:
            return consulta
        real = consulta.one_or_none

        def one_or_none():
            linha = real()
            if linha is None:
                return None
            return transformar(linha)

        consulta.one_or_none = one_or_none
        return consulta

    monkeypatch.setattr(Query, "filter_by", filter_by)


def test_revisao_recusa_pai_adulterado_antes_da_chamada(app, monkeypatch):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-pai-hash@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        pai = _tecnicas(dataset_id)[0]

        def transformar(linha):
            documento = json.loads(linha.resultado_json)
            documento["hash"] = "f" * 64
            return SimpleNamespace(resultado_json=json.dumps(documento, ensure_ascii=False))

        _leitura_do_pai(monkeypatch, pai.id, transformar)
        antes = claude.chamadas
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert claude.chamadas == antes
        assert len(claude.pedidos) == 1
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert corpo.get("resultado") is None


def test_revisao_recusa_pai_de_outro_escopo_antes_da_chamada(app, monkeypatch):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-pai-escopo@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        tecnicas = _tecnicas(dataset_id)
        gemini_linha = tecnicas[1]
        contexto = json.loads(gemini_linha.contexto_json)
        alheio = SimpleNamespace(
            usuario_id=int(gemini_linha.usuario_id) + 1000,
            conta_id=int(gemini_linha.conta_id),
            franquia_id=int(gemini_linha.franquia_id),
            dataset_id=int(gemini_linha.dataset_id),
            dataset_versao=int(gemini_linha.dataset_versao),
            dataset_impressao=gemini_linha.dataset_impressao,
        )
        with pytest.raises(ExecucaoErro) as erro:
            _pai_vinculado(alheio, contexto)
        assert erro.value.codigo == "identidade_inconsistente"

        def transformar(_linha):
            return None

        _leitura_do_pai(monkeypatch, tecnicas[0].id, transformar)
        antes = claude.chamadas
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert claude.chamadas == antes
        assert len(claude.pedidos) == 1
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert corpo.get("resultado") is None


def _segurar_descoberta(page):
    page.evaluate(
        """() => {
          const base = window.fetch;
          window.__segurarDescoberta = true;
          window.fetch = (url, opts) => {
            const alvo = String(url);
            const metodo = (opts && opts.method) || "GET";
            const descoberta = metodo === "POST" && alvo.indexOf("/descoberta") !== -1
              && alvo.indexOf("/preparacao") === -1 && alvo.indexOf("/selecao") === -1;
            if (descoberta && window.__segurarDescoberta) {
              window.__bi.push({
                url: alvo,
                method: metodo,
                body: opts && typeof opts.body === "string" ? opts.body : ""
              });
              return new Promise((resolve) => {
                window.__soltarDescoberta = () => {
                  window.__segurarDescoberta = false;
                  resolve({ status: 200, json: () => Promise.resolve(window.__descoberta) });
                };
              });
            }
            return base(url, opts);
          };
        }"""
    )


def _posts_descoberta(page):
    return page.evaluate(
        """() => window.__bi.filter((item) => item.method === "POST" && item.url.indexOf("/descoberta") !== -1
          && item.url.indexOf("/preparacao") === -1 && item.url.indexOf("/selecao") === -1)"""
    )


def _servir(documento):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    corpo = documento.encode("utf-8")

    class _Pagina(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

        def log_message(self, _formato, *_args):
            return

    servidor = ThreadingHTTPServer(("127.0.0.1", 0), _Pagina)
    fio = threading.Thread(target=servidor.serve_forever, daemon=True)
    fio.start()
    return servidor, f"http://127.0.0.1:{servidor.server_address[1]}/"


def _abrir_exame(page):
    from tests.test_agente_bi_jornada import _preparacao

    page.evaluate("(corpo) => { window.__preparacao = corpo; }", _preparacao())
    page.locator("#agente-bi-arquivo").set_input_files(
        files=[{"name": "fretes.csv", "mimeType": "text/csv", "buffer": b"alfa,valor\nA,10\n"}]
    )
    page.wait_for_selector("#agente-bi-prosseguir:not([hidden])")
    page.locator("#agente-bi-prosseguir").click()
    page.locator("#agente-bi-examinar").click()


def test_pedido_fica_guardado_antes_do_post_e_retoma_igual(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from tests.test_agente_bi_jornada import _html, _instalar, _scripts
    from tests.test_agente_bi_page import _browser_document, _launch_browser

    documento = _browser_document(_html(monkeypatch))
    servidor, url = _servir(documento)
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(url, wait_until="domcontentloaded")
            _instalar(page)
            _segurar_descoberta(page)
            _scripts(page, "descoberta")
            _abrir_exame(page)
            page.wait_for_function("() => typeof window.__soltarDescoberta === 'function'")
            guardado = page.evaluate("() => JSON.parse(localStorage.getItem('agente-bi-colaboracao'))")
            primeiro = _posts_descoberta(page)
            assert guardado["datasetId"] == 9
            assert guardado["pedido"]["modalidades"] == ["A"]
            assert len(primeiro) == 1
            assert json.loads(primeiro[0]["body"]) == {"dataset_id": 9, "pedido": guardado["pedido"]}
            page.goto(url, wait_until="domcontentloaded")
            _instalar(page)
            page.evaluate(
                """() => {
                  window.__descoberta = {
                    ok: true,
                    colaboracao: { estado: "em_andamento", fase: "claude_analisando", continuar: false }
                  };
                }"""
            )
            _scripts(page, "descoberta")
            page.wait_for_function(
                """() => window.__bi.some((item) => item.method === "POST" && item.url.indexOf("/descoberta") !== -1
                  && item.url.indexOf("/preparacao") === -1 && item.url.indexOf("/selecao") === -1)"""
            )
            retomado = _posts_descoberta(page)
            assert len(retomado) == 1
            assert json.loads(retomado[0]["body"]) == json.loads(primeiro[0]["body"])
        finally:
            browser.close()
            servidor.shutdown()


def test_pedido_legado_e_limpo_e_a_recarga_nao_reenvia(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from tests.test_agente_bi_jornada import _html, _instalar, _scripts
    from tests.test_agente_bi_page import _browser_document, _launch_browser

    documento = _browser_document(_html(monkeypatch))
    servidor, url = _servir(documento)
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(url, wait_until="domcontentloaded")
            _instalar(page)
            _segurar_descoberta(page)
            page.evaluate(
                """() => {
                  window.__descoberta = {
                    ok: true,
                    resultado: { descobertas: [], sugestoes: [], perguntas: [], critica_referencia: [] }
                  };
                }"""
            )
            _scripts(page, "descoberta")
            _abrir_exame(page)
            page.wait_for_function("() => typeof window.__soltarDescoberta === 'function'")
            assert page.evaluate("() => localStorage.getItem('agente-bi-colaboracao')") is not None
            page.evaluate("() => window.__soltarDescoberta()")
            page.wait_for_function("() => localStorage.getItem('agente-bi-colaboracao') === null")
            page.goto(url, wait_until="domcontentloaded")
            _instalar(page)
            _scripts(page, "descoberta")
            page.wait_for_function("() => window.__bi.some((item) => item.url.indexOf('/analises') !== -1)")
            page.wait_for_timeout(400)
            assert _posts_descoberta(page) == []
        finally:
            browser.close()
            servidor.shutdown()


def test_proposta_parcial_segue_ao_gemini_sem_o_trecho_invalido(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-parcial@test.com")
        pedido = _pedido(perfil, ["A"])
        boa = _condicional(perfil)["sugestoes"][0]
        ruim = dict(boa)
        ruim["grafico"] = "grafico_inventado"
        ruim["medida"] = {**_coord(_medida(perfil)), "indice": 97}
        proposta = {"descobertas": [], "sugestoes": [boa, ruim]}
        claude = _Claude([proposta, _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        primeira = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert primeira["colaboracao"]["fase"] == "gemini_revisando"
        assert len(gemini.geracoes) == 0
        documento = json.loads(_tecnicas(dataset_id)[0].resultado_json)
        assert documento["verificacao"] == {"aprovado": False, "codigo": "grafico_incompativel"}
        assert documento["descartes"] == [
            {"lista": "sugestoes", "posicao": 1, "campo": "grafico", "motivo": "grafico_incompativel"}
        ]
        assert len(documento["proposta"]["sugestoes"]) == 1
        assert documento["referencias"] == [boa["regiao"]]
        assert "grafico_inventado" not in _tecnicas(dataset_id)[0].resultado_json
        assert '"indice": 97' not in _tecnicas(dataset_id)[0].resultado_json
        assert '"indice":97' not in _tecnicas(dataset_id)[0].resultado_json
        segunda = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert segunda["colaboracao"]["fase"] == "claude_consolidando"
        assert len(gemini.geracoes) == 1
        enviado = gemini.geracoes[0]["contents"]
        assert "grafico_inventado" not in enviado
        assert '"indice":97' not in enviado.replace(" ", "")
        assert json.loads(enviado)["descartes"] == documento["descartes"]
        assert json.loads(enviado)["proposta"]["sugestoes"] == documento["proposta"]["sugestoes"]
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        assert validar_saida(montar_contexto(perfil, pedido), corpo["resultado"])["contrato_saida_validado"] is True


def test_formato_recuperavel_abre_a_revisao_sem_gravar_a_cerca(app, caplog):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-formato@test.com")
        pedido = _pedido(perfil, ["A"])
        proposta = _condicional(perfil)
        cerca = "```json\n" + json.dumps(proposta) + "\n```"
        claude = _Claude([proposta, proposta])

        def enviar(pedido_chamada):
            claude.pedidos.append(pedido_chamada)
            claude.chamadas += 1
            texto = cerca if claude.chamadas == 1 else json.dumps(proposta)
            return _resposta(texto, entrada=20, saida=20)

        claude.enviar = enviar
        gemini = _Gemini(json.dumps(_critica(perfil)))
        caplog.set_level(logging.INFO)
        primeira = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert primeira["colaboracao"]["fase"] == "gemini_revisando"
        assert "agente_bi_colaboracao_formato_recuperado" in caplog.text
        tecnica = _tecnicas(dataset_id)[0]
        assert "```" not in (tecnica.resultado_json or "")
        assert len(gemini.geracoes) == 0
        segunda = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert segunda["colaboracao"]["fase"] == "claude_consolidando"
        assert len(gemini.geracoes) == 1
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["resultado"]["contrato"] == "analisar.v1"


def test_autorizacao_indevida_nao_vira_falha_analitica(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-auth@test.com")
        pedido = _pedido(perfil, ["A"])

        def recusa(_perfil, _pedido, **_kwargs):
            raise AnalisarErro("autorizacao_indevida")

        from app.services import agente_bi_colaboracao_service as colaboracao

        original = colaboracao.analisar
        colaboracao.analisar = recusa
        try:
            claude = _Claude([_condicional(perfil)])
            gemini = _Gemini(json.dumps(_critica(perfil)))
            corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        finally:
            colaboracao.analisar = original
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert len(gemini.geracoes) == 0
        execucao = AgenteBiExecucao.query.filter_by(analise_id=_tecnicas(dataset_id)[0].id).one()
        assert execucao.ultimo_erro == "autorizacao_indevida"
        assert execucao.estado == "falha_definitiva"


def test_planilha_logistica_com_cpf_prepara_descobre_seleciona_e_reabre(app):
    from app.services.agente_bi_ingestao_service import ArquivoBruto, ingerir
    from app.services.agente_bi_perfil_semantico_service import perfilar_dataset

    with app.app_context():
        usuario, _franquia, escopo, _dataset_id, _perfil = _preparar("colab-logistica@test.com")
        documento = "529.982.247-25"
        outro = "111.444.777-35"
        opkips = (
            "codigo,documento,peso,destino\n"
            f"C1,{documento},10,sul\n"
            f"C2,{outro},10,norte\n"
            f"C3,{documento},12,sul\n"
        ).encode()
        opergel = (
            "codigo,documento,prazo\n"
            f"C1,{documento},2024-01-02\n"
            f"C2,{outro},2024-01-03\n"
            f"C3,{documento},2024-01-04\n"
        ).encode()
        ingerido = ingerir(
            escopo,
            [
                ArquivoBruto(nome="opkips.csv", mime=None, conteudo=opkips),
                ArquivoBruto(nome="opergel.csv", mime=None, conteudo=opergel),
            ],
            chave="lote-logistico",
        )
        assert ingerido["persistido"] is True, ingerido["motivo"]
        bruto = json.dumps(ingerido["preparacao"], default=str)
        assert documento not in bruto
        assert outro not in bruto
        assert '"valor": 10' in json.dumps(ingerido["preparacao"]["dados_tipados"]) or '"valor":10' in bruto
        dataset_id = ingerido["dataset"]["id"]
        perfil = perfilar_dataset(escopo, dataset_id)
        mascaradas = {
            (item["arquivo"], item["aba"], item["regiao"], item["indice"])
            for item in ingerido["preparacao"]["metadados_preparacao"]["protecao"]["colunas"]
        }
        assert mascaradas
        for relacao in perfil["relacoes"]:
            for lado in (relacao["lado_unico"], relacao["lado_associado"]):
                assert (lado["arquivo"], lado["aba"], lado["regiao"], lado["coluna"]) not in mascaradas
        for coluna in perfil["colunas"]:
            coordenada = (coluna["arquivo"], coluna["aba"], coluna["regiao"], coluna["indice"])
            if coordenada in mascaradas:
                assert coluna["dimensao_principal"] not in {"valor", "quantidade"}
                assert coluna["chave_candidata"] is False
        pedido = _pedido(perfil, ["A"])
        proposta = _proposta_viavel(perfil, pedido)
        medida = next(
            coluna for coluna in perfil["colunas"] if coluna["dimensao_principal"] in {"valor", "quantidade"}
        )
        critica = {
            "referencias": [_regiao(perfil)],
            "medidas": [_coord(medida)],
            "operacoes": ["soma"],
            "evidencias": [],
            "objecoes": [{"codigo": "evidencia_ausente", "referencia": _coord(medida)}],
        }
        claude = _Claude([proposta, proposta])
        gemini = _Gemini(json.dumps(critica))
        antes = len(_eventos(usuario.id))
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert validar_saida(montar_contexto(perfil, pedido), corpo["resultado"])["contrato"] == "analisar.v1"
        viaveis = [
            item["id"]
            for item in corpo["resultado"]["sugestoes"]
            if item["status"] == "viavel" and item["grafico"] and item["juncao"] == "nao_executada"
        ]
        assert viaveis
        assert len(viaveis) <= 6
        publicada = aprovar_selecao(
            escopo,
            {
                "analise_id": corpo["analise_id"],
                "sugestoes": viaveis[:6],
                "perguntas": [{"id": item["id"], "confirmada": True} for item in corpo["resultado"]["perguntas"]],
                "confirmacao": True,
            },
        )
        assert publicada["selecao"]["etapa"] == "selecao"
        definicao = definir_selecao(escopo, {"analise_id": publicada["selecao"]["id"]})
        assert definicao["definicao"]["etapa"] == "definicao"
        reaberta = obter_analise(escopo, corpo["analise_id"])
        assert documento not in json.dumps(reaberta, default=str)
        repetida = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert repetida["analise_id"] == corpo["analise_id"]
        assert claude.chamadas == 2
        assert len(gemini.geracoes) == 1
        assert len(_eventos(usuario.id)) == antes + 3


def test_representacao_recusa_forma_irrecuperavel_sem_copiar_valor():
    assert _representar_proposta("texto livre") == (None, None)
    assert _representar_proposta({"descobertas": [], "sugestoes": [], "extra": "nao-enviar"}) == (None, None)
    assert _representar_proposta({"descobertas": [], "sugestoes": ["item-opaco"]}) == (None, None)
    assert _representar_proposta({"descobertas": {}, "sugestoes": []}) == (None, None)
    item = {"status": "viavel", "segredo": "529.982.247-25"}
    assert _representar_proposta({"descobertas": [], "sugestoes": [item]}) == (None, None)


def test_status_invalido_registra_descarte_e_nao_aprova(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-status@test.com")
        pedido = _pedido(perfil, ["A"])
        boa = _condicional(perfil)["sugestoes"][0]
        ruim = dict(boa)
        ruim["status"] = "publicado_silencioso"
        ruim["medida"] = {**_coord(_medida(perfil)), "indice": 97}
        proposta = {"descobertas": [], "sugestoes": [boa, ruim]}
        claude = _Claude([proposta, _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        primeira = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert primeira["colaboracao"]["fase"] == "gemini_revisando"
        documento = json.loads(_tecnicas(dataset_id)[0].resultado_json)
        assert documento["verificacao"]["aprovado"] is False
        assert documento["descartes"] == [
            {
                "lista": "sugestoes",
                "posicao": 1,
                "campo": "status",
                "motivo": "valor_fora_do_catalogo",
            }
        ]
        assert "publicado_silencioso" not in _tecnicas(dataset_id)[0].resultado_json
        assert '"indice":97' not in _tecnicas(dataset_id)[0].resultado_json
        assert documento["referencias"] == [boa["regiao"]]
        assert esquema_critica()["properties"]["objecoes"]["items"]["properties"]["codigo"]["enum"] == sorted(
            _CHECKPOINT
        )
        assert "valor_fora_do_catalogo" not in esquema_critica()["properties"]["objecoes"]["items"]["properties"]["codigo"]["enum"]
        segunda = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert segunda["colaboracao"]["fase"] == "claude_consolidando"
        enviado = json.loads(gemini.geracoes[0]["contents"])
        assert enviado["descartes"] == documento["descartes"]
        assert "publicado_silencioso" not in gemini.geracoes[0]["contents"]
        corpo = _ate(escopo, dataset_id, pedido, claude, gemini)
        assert validar_saida(montar_contexto(perfil, pedido), corpo["resultado"])["contrato_saida_validado"] is True
        chamadas = claude.chamadas
        geracoes = len(gemini.geracoes)
        repetido = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert repetido["analise_id"] == corpo["analise_id"]
        assert claude.chamadas == chamadas
        assert len(gemini.geracoes) == geracoes


def test_sugestao_unica_invalida_chega_vazia_ao_gemini(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-vazia@test.com")
        pedido = _pedido(perfil, ["A"])
        ruim = dict(_condicional(perfil)["sugestoes"][0])
        ruim["grafico"] = "grafico_inventado"
        claude = _Claude([{"descobertas": [], "sugestoes": [ruim]}, _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        primeira = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert primeira["colaboracao"]["fase"] == "gemini_revisando"
        documento = json.loads(_tecnicas(dataset_id)[0].resultado_json)
        assert documento["proposta"] == {"descobertas": [], "sugestoes": []}
        assert documento["verificacao"]["aprovado"] is False
        assert documento["descartes"] == [
            {"lista": "sugestoes", "posicao": 0, "campo": "grafico", "motivo": "grafico_incompativel"}
        ]
        assert "grafico_inventado" not in _tecnicas(dataset_id)[0].resultado_json
        segunda = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert len(gemini.geracoes) == 1
        enviado = json.loads(gemini.geracoes[0]["contents"])
        assert enviado["proposta"] == {"descobertas": [], "sugestoes": []}
        assert enviado["descartes"] == documento["descartes"]
        assert "grafico_inventado" not in gemini.geracoes[0]["contents"]
        assert segunda["colaboracao"]["fase"] == "claude_consolidando"


def test_chave_extra_item_opaco_e_json_ilegivel_recusam(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-extra@test.com")
        pedido = _pedido(perfil, ["A"])
        boa = _condicional(perfil)
        extra = dict(boa)
        extra["extra"] = "nao-enviar"
        claude = _Claude([extra])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert _tecnicas(dataset_id)[0].resultado_json is None
        assert len(gemini.geracoes) == 0
        assert _finais(dataset_id) == []

        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-opaco@test.com")
        pedido = _pedido(perfil, ["A"])
        opaco = {"descobertas": [], "sugestoes": ["nao-e-objeto", _condicional(perfil)["sugestoes"][0]]}
        claude = _Claude([opaco])
        gemini = _Gemini("{}")
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert _tecnicas(dataset_id)[0].resultado_json is None
        assert "nao-e-objeto" not in json.dumps(
            {
                "contexto": _tecnicas(dataset_id)[0].contexto_json,
                "resultado": _tecnicas(dataset_id)[0].resultado_json,
            },
            default=str,
        )


def test_consolidacao_parcial_nao_materializa_e_revisa_com_diagnostico(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-consolida@test.com")
        pedido = _pedido(perfil, ["A"])
        valida = _condicional(perfil)
        ruim = dict(valida["sugestoes"][0])
        ruim["status"] = "publicado_silencioso"
        parcial = {"descobertas": [], "sugestoes": [valida["sugestoes"][0], ruim]}
        claude = _Claude([valida, parcial, valida])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        consolidada = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert consolidada.get("resultado") is None
        assert consolidada["colaboracao"]["continuar"] is True
        assert consolidada["colaboracao"]["fase"] == "gemini_revisando"
        assert _finais(dataset_id) == []
        documento = json.loads(_tecnicas(dataset_id)[-1].resultado_json)
        assert documento["acao"] == "revisar"
        assert documento["verificacao"]["aprovado"] is False
        assert documento["descartes"][0]["motivo"] == "valor_fora_do_catalogo"
        assert "publicado_silencioso" not in _tecnicas(dataset_id)[-1].resultado_json
        pedido_revisao = _conteudo_claude(claude.pedidos[1])
        assert pedido_revisao["verificacao_anterior"]["aprovado"] is True
        assert "descartes" not in pedido_revisao["verificacao_anterior"]
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert json.loads(gemini.geracoes[-1]["contents"])["descartes"] == documento["descartes"]
        assert "publicado_silencioso" not in gemini.geracoes[-1]["contents"]
        corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert corpo["resultado"]["contrato"] == "analisar.v1"
        revisao = _conteudo_claude(claude.pedidos[-1])
        assert revisao["contexto"]
        assert revisao["proposta_anterior"] == documento["proposta"]
        assert revisao["critica_autorizada"]["objecoes"]
        assert revisao["verificacao_anterior"]["descartes"] == documento["descartes"]
        assert "publicado_silencioso" not in claude.pedidos[-1]["messages"][0]["content"]
        assert validar_saida(montar_contexto(perfil, pedido), corpo["resultado"])["contrato_saida_validado"] is True


def test_envelope_anterior_segue_valido_e_hash_cubre_descartes(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-hash@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil), _condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        tecnica = _tecnicas(dataset_id)[0]
        execucao = AgenteBiExecucao.query.filter_by(analise_id=tecnica.id).one()
        documento = json.loads(tecnica.resultado_json)
        assert "descartes" not in documento
        assert revalidar_contrato_tecnico(tecnica, _referencias(execucao, tecnica), documento) == documento
        adulterado = dict(documento)
        adulterado["descartes"] = [
            {"lista": "sugestoes", "posicao": 0, "campo": "status", "motivo": "valor_fora_do_catalogo"}
        ]
        with pytest.raises(ExecucaoErro) as erro:
            revalidar_contrato_tecnico(tecnica, _referencias(execucao, tecnica), adulterado)
        assert erro.value.codigo == "contrato_saida_invalido"

        ruim = dict(_condicional(perfil)["sugestoes"][0])
        ruim["grafico"] = "grafico_inventado"
        outro = _Claude([{"descobertas": [], "sugestoes": [ruim]}])
        _descobrir(escopo, dataset_id, _pedido(perfil, ["C"], texto="leitura nova"), outro, gemini)
        parcial = _tecnicas(dataset_id)[-1]
        execucao_parcial = AgenteBiExecucao.query.filter_by(analise_id=parcial.id).one()
        corpo = json.loads(parcial.resultado_json)
        assert revalidar_contrato_tecnico(parcial, _referencias(execucao_parcial, parcial), corpo) == corpo
        sem_diagnostico = {chave: valor for chave, valor in corpo.items() if chave != "descartes"}
        with pytest.raises(ExecucaoErro):
            revalidar_contrato_tecnico(parcial, _referencias(execucao_parcial, parcial), sem_diagnostico)
        aprovado = dict(corpo)
        aprovado["verificacao"] = {"aprovado": True, "codigo": None}
        with pytest.raises(ExecucaoErro):
            revalidar_contrato_tecnico(parcial, _referencias(execucao_parcial, parcial), aprovado)


def test_descarte_nao_contorna_bloqueio_de_governanca(app):
    with app.app_context():
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-bloco@test.com")
        pedido = _pedido(perfil, ["A"])
        ruim = dict(_condicional(perfil)["sugestoes"][0])
        ruim["status"] = "publicado_silencioso"
        proposta = {"descobertas": [], "sugestoes": [_condicional(perfil)["sugestoes"][0], ruim]}

        def recusa(_perfil, _pedido, **_kwargs):
            raise AnalisarErro("autorizacao_indevida")

        from app.services import agente_bi_colaboracao_service as colaboracao

        original = colaboracao.analisar
        colaboracao.analisar = recusa
        try:
            claude = _Claude([proposta])
            gemini = _Gemini(json.dumps(_critica(perfil)))
            corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        finally:
            colaboracao.analisar = original
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert len(gemini.geracoes) == 0
        execucao = AgenteBiExecucao.query.filter_by(analise_id=_tecnicas(dataset_id)[0].id).one()
        assert execucao.ultimo_erro == "autorizacao_indevida"
        assert execucao.estado == "falha_definitiva"
        assert _finais(dataset_id) == []


def test_postgres_duas_requisicoes_uma_chamada():
    uri = (os.environ.get("AGENTE_BI_EXECUCAO_PG_URI") or "").strip()
    from app.db_operational_safety import parse_database_identity

    ident = parse_database_identity(uri)
    if ident is None or not ident.is_postgres:
        pytest.skip("PostgreSQL descartável não configurado")
    if ident.database != "agente_bi_execucao_descartavel" or ident.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("URI recusada: só o cluster local descartável desta prova")

    from flask import Flask

    flask_app = Flask("agente-bi-colaboracao-pg")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        _usuario, _franquia, escopo, dataset_id, perfil = _preparar("colab-pg@test.com")
        pedido = _pedido(perfil, ["A"])
        db.session.commit()
        usuario_id = escopo.usuario_id
        db.session.remove()

    barreira = threading.Barrier(2)
    dentro = threading.Event()
    liberar = threading.Event()
    erros = []
    chamadas = {"n": 0}
    trava = threading.Lock()

    def enviar(pedido_chamada):
        with trava:
            chamadas["n"] += 1
        dentro.set()
        assert liberar.wait(8)
        return _resposta(json.dumps(_condicional(perfil)), entrada=20, saida=20)

    def worker():
        try:
            with flask_app.app_context():
                from app.models import User
                from app.services.agente_bi_persistencia_service import escopo_do_usuario

                usuario = db.session.get(User, usuario_id)
                escopo_local = escopo_do_usuario(usuario)
                barreira.wait(8)
                descobrir(
                    escopo_local,
                    {"dataset_id": dataset_id, "pedido": pedido},
                    transporte=enviar,
                    cliente_gemini=_Gemini(json.dumps(_critica(perfil))),
                )
        except Exception as exc:
            erros.append(exc)
            liberar.set()
        finally:
            with flask_app.app_context():
                db.session.remove()

    fios = [threading.Thread(target=worker) for _ in range(2)]
    for fio in fios:
        fio.start()
    assert dentro.wait(8)
    liberar.set()
    for fio in fios:
        fio.join(30)
    try:
        assert erros == []
        assert chamadas["n"] == 1
        with flask_app.app_context():
            assert IaChamadaTentativa.query.filter_by(usuario_id=usuario_id).count() == 1
            assert AgenteBiExecucao.query.count() == 1
    finally:
        with flask_app.app_context():
            db.session.remove()
            db.drop_all()
            db.session.remove()
