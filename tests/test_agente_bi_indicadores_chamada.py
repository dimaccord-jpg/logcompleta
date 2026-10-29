"""SCRUM-272: indicadores de tentativa, transporte, evento e abatimento.

O transporte é simulado. Nenhum teste chama o Claude faturável.
A projeção não cria cobrança: só lê os registros oficiais.
"""
from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import Franquia, IaChamadaTentativa, IaConsumoAbatimento, IaConsumoEvento
from app.services.agente_bi_anthropic_service import (
    ErroTransporte,
    campos_publicos_ia,
    projetar_indicadores_ia,
)
from app.services.agente_bi_interpretacao_v2_service import InterpretacaoErro
from app.services.agente_bi_publicacao_v2_service import aplicar_pedido
from tests.test_agente_bi_anthropic import _chamar, _foto, _habilitar, _transporte
from tests.test_agente_bi_execucao import _seed_financeiro
from tests.test_agente_bi_ingestao import _cliente, _entrar
from tests.test_agente_bi_publicacao_v2 import TEXTO, _enviar, _media_e_filtro, _preparar


@pytest.fixture(autouse=True)
def _sem_sdk(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_MODEL", "claude-sonnet-simulado")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("sdk")

    monkeypatch.setattr("app.services.agente_bi_anthropic_service.montar_cliente_anthropic", proibido)
    monkeypatch.setattr("app.services.agente_bi_anthropic_service._transporte_sdk", proibido)


def test_projecao_nao_abre_motor_financeiro():
    fonte = inspect.getsource(projetar_indicadores_ia)
    assert "aplicar_motor_apos_ia_consumo_evento" not in fonte
    assert "consumo_acumulado" not in fonte
    assert "_persistir_abatimento" not in fonte


def _fatos(resultado):
    indicadores = projetar_indicadores_ia(resultado)
    _bate_com_registro(indicadores)
    return indicadores, campos_publicos_ia(indicadores)


def _bate_com_registro(indicadores: dict) -> None:
    tentativa = None
    if isinstance(indicadores.get("tentativa_id"), int):
        tentativa = db.session.get(IaChamadaTentativa, indicadores["tentativa_id"])
    assert indicadores["tentativa_registrada"] is (tentativa is not None)
    evento = None
    if isinstance(indicadores.get("evento_id"), int):
        evento = db.session.get(IaConsumoEvento, indicadores["evento_id"])
    assert indicadores["evento_consumo"] is (evento is not None)
    abatimento = None
    if evento is not None:
        abatimento = (
            IaConsumoAbatimento.query.filter_by(ia_consumo_evento_id=evento.id).one_or_none()
        )
    assert indicadores["abatimento_confirmado"] is (abatimento is not None)
    if indicadores["abatimento_confirmado"]:
        assert indicadores["evento_consumo"] is True
    if indicadores["chamada_externa"] is True and tentativa is not None:
        assert tentativa.status in {
            IaChamadaTentativa.STATUS_SETTLED,
            IaChamadaTentativa.STATUS_PROVIDER_FAILED,
            IaChamadaTentativa.STATUS_UNCERTAIN,
            IaChamadaTentativa.STATUS_CALLING,
            IaChamadaTentativa.STATUS_RELEASED,
        }
    if indicadores["chamada_externa"] is False and tentativa is not None:
        assert tentativa.status in {
            IaChamadaTentativa.STATUS_BLOCKED,
            IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED,
            IaChamadaTentativa.STATUS_RESERVED,
        }


def test_recusa_de_admissao_nao_declara_transporte(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="ind.admissao@test.com")
        franquia.bloqueio_manual = True
        db.session.commit()
        antes = _foto(franquia.id)
        resultado = _chamar(usuario, franquia, capturado, chave_idempotencia="agente-bi.ind.adm")
        indicadores, campos = _fatos(resultado)
        assert resultado["provedor_chamado"] is False
        assert capturado.get("chamadas", 0) == 0
        assert indicadores["tentativa_registrada"] is True
        assert indicadores["chamada_externa"] is False
        assert indicadores["evento_consumo"] is False
        assert indicadores["abatimento_confirmado"] is False
        assert campos["chamada_anthropic"] is False
        assert campos["cobranca"] is False
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_governanca_grava_evento_sem_transporte_nem_abatimento(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="ind.governanca@test.com")
        antes = _foto(franquia.id)
        resultado = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.ind.gov",
            mensagem="linha\x00bloqueio",
        )
        indicadores, campos = _fatos(resultado)
        assert capturado.get("chamadas", 0) == 0
        assert indicadores["tentativa_registrada"] is True
        assert indicadores["chamada_externa"] is False
        assert indicadores["evento_consumo"] is True
        assert indicadores["abatimento_confirmado"] is False
        assert campos["cobranca"] is False
        evento = db.session.get(IaConsumoEvento, indicadores["evento_id"])
        assert evento.provider == "internal"
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_transporte_seguido_de_falha_nao_nega_a_chamada(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="ind.falha@test.com")
        resultado = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.ind.falha",
            transporte=_transporte(
                capturado,
                erro=ErroTransporte("rate_limit", incerto=False, status=429),
            ),
        )
        indicadores, campos = _fatos(resultado)
        assert resultado["ok"] is False
        assert resultado["provedor_chamado"] is True
        assert capturado["chamadas"] == 1
        assert indicadores["tentativa_registrada"] is True
        assert indicadores["chamada_externa"] is True
        assert indicadores["evento_consumo"] is True
        assert indicadores["abatimento_confirmado"] is False
        assert campos["chamada_anthropic"] is True
        assert campos["cobranca"] is False
        assert db.session.get(IaChamadaTentativa, indicadores["tentativa_id"]).status == (
            IaChamadaTentativa.STATUS_PROVIDER_FAILED
        )


def test_resultado_incerto_nao_afirma_ausencia_nem_cobranca(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="ind.incerto@test.com")
        antes = _foto(franquia.id)
        resultado = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.ind.timeout",
            transporte=_transporte(capturado, erro=ErroTransporte("timeout", incerto=True)),
        )
        indicadores, campos = _fatos(resultado)
        assert resultado["incerto"] is True
        assert resultado["ok"] is False
        assert capturado["chamadas"] == 1
        assert indicadores["chamada_externa"] is True
        assert indicadores["evento_consumo"] is True
        assert indicadores["abatimento_confirmado"] is False
        assert campos["cobranca"] is False
        assert _foto(franquia.id) == antes

        repetida = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.ind.timeout",
            transporte=_transporte(capturado, erro=ErroTransporte("timeout", incerto=True)),
        )
        de_novo, campos_repetidos = _fatos(repetida)
        assert repetida["provedor_chamado"] is False
        assert repetida["repetida"] is True
        assert de_novo["chamada_externa"] is True
        assert de_novo["repetida"] is True
        assert de_novo["tentativa_id"] == indicadores["tentativa_id"]
        assert de_novo["evento_id"] == indicadores["evento_id"]
        assert campos_repetidos["chamada_anthropic"] is False
        assert campos_repetidos["cobranca"] is False
        assert capturado["chamadas"] == 1
        assert IaConsumoEvento.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 0


def test_incerto_sem_evento_nao_declara_transporte_nem_ausencia(app, monkeypatch):
    _habilitar(monkeypatch)
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._persistir_evento",
        lambda *_args, **_kwargs: None,
    )
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="ind.opaco@test.com")
        resultado = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.ind.opaco",
        )
        indicadores, campos = _fatos(resultado)
        assert resultado["incerto"] is True
        assert resultado["provedor_chamado"] is True
        assert capturado["chamadas"] == 1
        assert indicadores["tentativa_registrada"] is True
        assert indicadores["chamada_externa"] is True
        assert indicadores["evento_consumo"] is False
        assert indicadores["abatimento_confirmado"] is False
        assert campos["cobranca"] is False
        repetida = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.ind.opaco",
        )
        de_novo, campos_repetidos = _fatos(repetida)
        assert repetida["provedor_chamado"] is False
        assert de_novo["repetida"] is True
        assert de_novo["chamada_externa"] is None
        assert de_novo["evento_consumo"] is False
        assert de_novo["abatimento_confirmado"] is False
        assert campos_repetidos["chamada_anthropic"] is False
        assert campos_repetidos["cobranca"] is False
        assert capturado["chamadas"] == 1
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0


def test_evento_sem_uso_nao_confirma_abatimento(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="ind.semuso@test.com")
        antes = _foto(franquia.id)
        resultado = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.ind.semuso",
            transporte=_transporte(
                capturado,
                resposta={"texto": "sem metrica", "input_tokens": None, "output_tokens": None},
            ),
        )
        indicadores, campos = _fatos(resultado)
        assert resultado["provedor_chamado"] is True
        assert indicadores["tentativa_registrada"] is True
        assert indicadores["chamada_externa"] is True
        assert indicadores["evento_consumo"] is True
        assert indicadores["abatimento_confirmado"] is False
        assert campos["chamada_anthropic"] is True
        assert campos["cobranca"] is False
        assert db.session.get(IaConsumoEvento, indicadores["evento_id"]).total_tokens is None
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_sucesso_confirma_abatimento_uma_vez(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="ind.ok@test.com")
        resultado = _chamar(usuario, franquia, capturado, chave_idempotencia="agente-bi.ind.ok")
        indicadores, campos = _fatos(resultado)
        assert indicadores["tentativa_registrada"] is True
        assert indicadores["chamada_externa"] is True
        assert indicadores["evento_consumo"] is True
        assert indicadores["abatimento_confirmado"] is True
        assert indicadores["repetida"] is False
        assert campos["chamada_anthropic"] is True
        assert campos["cobranca"] is True
        assert IaChamadaTentativa.query.count() == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        db.session.refresh(franquia)
        consumo = Decimal(franquia.consumo_acumulado)

        repetida = _chamar(usuario, franquia, capturado, chave_idempotencia="agente-bi.ind.ok")
        de_novo, campos_repetidos = _fatos(repetida)
        assert repetida["provedor_chamado"] is False
        assert de_novo["repetida"] is True
        assert de_novo["tentativa_id"] == indicadores["tentativa_id"]
        assert de_novo["evento_id"] == indicadores["evento_id"]
        assert de_novo["abatimento_id"] == indicadores["abatimento_id"]
        assert de_novo["chamada_externa"] is True
        assert de_novo["abatimento_confirmado"] is True
        assert campos_repetidos["chamada_anthropic"] is False
        assert campos_repetidos["cobranca"] is False
        assert capturado["chamadas"] == 1
        assert IaChamadaTentativa.query.count() == 1
        assert IaConsumoEvento.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == consumo


def test_projecao_publica_no_pedido_de_evolucao(ctx):
    base = _preparar("ind.publico@test.com", "ind-publico")
    escopo = base["escopo"]
    identificador = base["dashboard_id"]
    transporte, estado = _enviar(_media_e_filtro)
    primeira = aplicar_pedido(escopo, identificador, {"texto": TEXTO}, transporte=transporte)
    indicadores = primeira["indicadores_ia"]
    _bate_com_registro(indicadores)
    assert primeira["situacao"] == "esclarecimento"
    assert primeira["chamada_anthropic"] is True
    assert primeira["cobranca"] is True
    assert indicadores["tentativa_registrada"] is True
    assert indicadores["chamada_externa"] is True
    assert indicadores["evento_consumo"] is True
    assert indicadores["abatimento_confirmado"] is True
    assert indicadores["repetida"] is False
    assert estado["n"] == 1
    tentativa_id = indicadores["tentativa_id"]
    evento_id = indicadores["evento_id"]
    abatimento_id = indicadores["abatimento_id"]
    assert IaChamadaTentativa.query.filter_by(franquia_id=escopo.franquia_id).count() == 1
    assert IaConsumoAbatimento.query.filter_by(id=abatimento_id).count() == 1

    repetida = aplicar_pedido(escopo, identificador, {"texto": TEXTO}, transporte=transporte)
    de_novo = repetida["indicadores_ia"]
    _bate_com_registro(de_novo)
    assert repetida["chamada_anthropic"] is False
    assert repetida["cobranca"] is False
    assert de_novo["repetida"] is True
    assert de_novo["tentativa_id"] == tentativa_id
    assert de_novo["evento_id"] == evento_id
    assert de_novo["abatimento_id"] == abatimento_id
    assert de_novo["chamada_externa"] is True
    assert de_novo["abatimento_confirmado"] is True
    assert estado["n"] == 1
    assert IaChamadaTentativa.query.filter_by(franquia_id=escopo.franquia_id).count() == 1
    assert IaConsumoEvento.query.filter_by(franquia_id=escopo.franquia_id).count() == 1
    assert IaConsumoAbatimento.query.filter_by(franquia_id=escopo.franquia_id).count() == 1


def test_rota_publica_recusa_antes_do_transporte(app, monkeypatch):
    client = _cliente(app, monkeypatch)
    with app.app_context():
        base = _preparar("ind.rota.adm@test.com", "ind-rota-adm")
        usuario_id = base["escopo"].usuario_id
        identificador = base["dashboard_id"]
        franquia = db.session.get(Franquia, base["escopo"].franquia_id)
        franquia.bloqueio_manual = True
        db.session.commit()
        antes = _foto(franquia.id)
    _entrar(client, usuario_id)

    def transporte(_pedido):
        raise AssertionError("transporte")

    import app.services.agente_bi_publicacao_v2_service as publicacao

    real = publicacao.aplicar_pedido

    def envolvido(escopo, dashboard_id, corpo, transporte=None):
        return real(escopo, dashboard_id, corpo, transporte=transporte or envolvido.transporte)

    envolvido.transporte = transporte
    monkeypatch.setattr(publicacao, "aplicar_pedido", envolvido)
    resposta = client.post(
        f"/api/agente-bi/dashboards/{identificador}/evolucao",
        json={"texto": TEXTO},
    )
    assert resposta.status_code == 403
    corpo = resposta.get_json()
    assert corpo["codigo"] == "admissao_recusada"
    assert corpo["chamada_anthropic"] is False
    assert corpo["cobranca"] is False
    indicadores = corpo["indicadores_ia"]
    assert indicadores["tentativa_registrada"] is True
    assert indicadores["chamada_externa"] is False
    assert indicadores["evento_consumo"] is False
    assert indicadores["abatimento_confirmado"] is False
    with app.app_context():
        assert IaConsumoEvento.query.filter_by(usuario_id=usuario_id).count() == 0
        assert IaConsumoAbatimento.query.filter_by(franquia_id=base["escopo"].franquia_id).count() == 0
        assert _foto(base["escopo"].franquia_id) == antes


def test_rota_publica_falha_posterior_preserva_a_chamada(app, monkeypatch):
    client = _cliente(app, monkeypatch)
    with app.app_context():
        base = _preparar("ind.rota.falha@test.com", "ind-rota-falha")
        usuario_id = base["escopo"].usuario_id
        identificador = base["dashboard_id"]
        franquia_id = base["escopo"].franquia_id
    _entrar(client, usuario_id)
    chamadas = {"n": 0}

    def transporte(_pedido):
        chamadas["n"] += 1
        raise ErroTransporte("rate_limit", incerto=False, status=429)

    import app.services.agente_bi_publicacao_v2_service as publicacao

    real = publicacao.aplicar_pedido

    def envolvido(escopo, dashboard_id, corpo, transporte=None):
        return real(escopo, dashboard_id, corpo, transporte=transporte or envolvido.transporte)

    envolvido.transporte = transporte
    monkeypatch.setattr(publicacao, "aplicar_pedido", envolvido)
    resposta = client.post(
        f"/api/agente-bi/dashboards/{identificador}/evolucao",
        json={"texto": TEXTO},
    )
    assert resposta.status_code == 422
    corpo = resposta.get_json()
    assert corpo["cobranca"] is False
    assert corpo["chamada_anthropic"] is True
    assert corpo["indicadores_ia"]["chamada_externa"] is True
    assert corpo["indicadores_ia"]["tentativa_registrada"] is True
    assert corpo["indicadores_ia"]["evento_consumo"] is True
    assert corpo["indicadores_ia"]["abatimento_confirmado"] is False
    assert chamadas["n"] == 1
    with app.app_context():
        assert IaChamadaTentativa.query.filter_by(franquia_id=franquia_id).count() == 1
        assert IaConsumoAbatimento.query.filter_by(franquia_id=franquia_id).count() == 0

    repetida = client.post(
        f"/api/agente-bi/dashboards/{identificador}/evolucao",
        json={"texto": TEXTO},
    )
    assert repetida.status_code == 422
    corpo_repetido = repetida.get_json()
    assert corpo_repetido["chamada_anthropic"] is False
    assert corpo_repetido["cobranca"] is False
    assert corpo_repetido["indicadores_ia"]["repetida"] is True
    assert corpo_repetido["indicadores_ia"]["chamada_externa"] is True
    assert corpo_repetido["indicadores_ia"]["tentativa_id"] == corpo["indicadores_ia"]["tentativa_id"]
    assert chamadas["n"] == 1
    with app.app_context():
        assert IaChamadaTentativa.query.filter_by(franquia_id=franquia_id).count() == 1
        assert IaConsumoEvento.query.filter_by(franquia_id=franquia_id).count() == 1


def test_explicacao_nao_trata_evento_como_chamada_nem_cobranca(ctx):
    base = _preparar("ind.explicacao@test.com", "ind-explicacao")
    escopo = base["escopo"]
    identificador = base["dashboard_id"]

    def montar(mensagem):
        return {
            "comprovado": [
                {
                    "ref": mensagem["agregados"][0]["ref"],
                    "campo": "resultado",
                    "leitura": "O agregado publicado registra o resultado.",
                }
            ],
            "hipotese": [],
            "insuficiente": [],
        }

    from tests.test_agente_bi_explicacao import _transporte

    transporte, estado = _transporte(montar)
    resposta = aplicar_pedido(escopo, identificador, {"texto": "O que esses números realmente dizem sobre minha operação logística?"}, transporte=transporte)
    assert resposta["situacao"] == "explicacao"
    assert resposta["chamada_anthropic"] is True
    ocorrencias = resposta["indicadores_ia"]["ocorrencias"]
    assert len(ocorrencias) == 2
    assert len({item["tentativa_id"] for item in ocorrencias}) == 2
    assert all(item["chamada_externa"] is True for item in ocorrencias)
    assert all(item["evento_consumo"] is True for item in ocorrencias)
    assert all(item["abatimento_confirmado"] is True for item in ocorrencias)
    assert all(item["repetida"] is False for item in ocorrencias)
    assert estado["n"] == 2
    assert IaChamadaTentativa.query.filter_by(franquia_id=escopo.franquia_id).count() == 2
    assert IaConsumoAbatimento.query.filter_by(franquia_id=escopo.franquia_id).count() == 2
    identificadores = {item["tentativa_id"] for item in ocorrencias}

    with pytest.raises(InterpretacaoErro) as repetida:
        aplicar_pedido(
            escopo,
            identificador,
            {"texto": "O que esses números realmente dizem sobre minha operação logística?"},
            transporte=transporte,
        )
    assert estado["n"] == 2
    assert repetida.value.chamada_anthropic is False
    assert repetida.value.cobranca is False
    de_novo = repetida.value.indicadores_ia
    assert de_novo["repetida"] is True
    assert de_novo["chamada_externa"] is True
    assert de_novo["abatimento_confirmado"] is True
    assert {item["tentativa_id"] for item in de_novo["ocorrencias"]} == identificadores
    assert IaChamadaTentativa.query.filter_by(franquia_id=escopo.franquia_id).count() == 2
    assert IaConsumoAbatimento.query.filter_by(franquia_id=escopo.franquia_id).count() == 2
