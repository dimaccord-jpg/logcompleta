"""SCRUM-256: adaptador Anthropic governado, sem chamada real e sem reserva."""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from types import SimpleNamespace

from app.extensions import db
from app.models import AgenteBiExecucao, Franquia, IaChamadaTentativa, IaConsumoAbatimento, IaConsumoEvento
from app.services.agente_bi_anthropic_service import (
    RETRIES,
    ErroTransporte,
    chamar_anthropic_governado,
    classificar_excecao_transporte,
    interpretar_mensagem,
    montar_cliente_anthropic,
)
from app.services.agente_bi_execucao_service import adquirir, enfileirar
from app.services.cleiton_ai_data_governance import PURPOSE_BI_LOGISTICO, purpose_from_flow_type
from app.services.cleiton_ai_privacy_classifier import SUPPORTED_PURPOSES
from tests.conftest import seed_usuario
from tests.test_agente_bi_execucao import _seed_financeiro

CPF = "529.982.247-25"
MARCADOR = "MARCADOR_NAO_PODE_VAZAR"


def _habilitar(monkeypatch):
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_MODEL", raising=False)


def _transporte(capturado, resposta=None, erro=None):
    def enviar(pedido):
        capturado["chamadas"] = capturado.get("chamadas", 0) + 1
        capturado["pedido"] = pedido
        if erro is not None:
            raise erro
        return resposta or {
            "texto": "duas rotas com frete distinto",
            "stop_reason": "end_turn",
            "input_tokens": 1000,
            "output_tokens": 1000,
            "recusa": False,
            "truncada": False,
        }

    return enviar


def _chamar(usuario, franquia, capturado, **extras):
    argumentos = {
        "usuario_id": usuario.id,
        "conta_id": usuario.conta_id,
        "franquia_id": franquia.id,
        "mensagem": "Frete SP 10 kg",
        "chave_idempotencia": "agente-bi.chamada.01",
        "transporte": _transporte(capturado),
        "model": "claude-sonnet-simulado",
        "max_tokens": 128,
    }
    argumentos.update(extras)
    return chamar_anthropic_governado(**argumentos)


def _foto(franquia_id):
    franquia = db.session.get(Franquia, franquia_id)
    db.session.refresh(franquia)
    return (
        Decimal(franquia.consumo_acumulado or 0),
        Decimal(franquia.reserva_pendente or 0),
    )


def test_finalidade_bi_logistico_entra_na_governanca_oficial():
    assert PURPOSE_BI_LOGISTICO in SUPPORTED_PURPOSES
    assert purpose_from_flow_type("bi_logistico", "agente_bi") == PURPOSE_BI_LOGISTICO
    assert purpose_from_flow_type("cleide_chat_auditoria_frete_ai", "cleide") != PURPOSE_BI_LOGISTICO


def test_cliente_oficial_usa_retries_zero(monkeypatch):
    capturado = {}

    class Anthropic:
        def __init__(self, **kwargs):
            capturado.update(kwargs)

    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=Anthropic))
    montar_cliente_anthropic("chave-de-teste", 12.0)
    assert capturado["max_retries"] == 0
    assert capturado["timeout"] == 12.0
    assert RETRIES == 0


def test_classifica_timeout_429_e_5xx_sem_repetir():
    class APITimeoutError(Exception):
        pass

    class RateLimitError(Exception):
        status_code = 429

    class InternalServerError(Exception):
        status_code = 503

    assert classificar_excecao_transporte(APITimeoutError()).incerto is True
    assert classificar_excecao_transporte(APITimeoutError()).codigo == "timeout"
    limite = classificar_excecao_transporte(RateLimitError())
    assert limite.codigo == "rate_limit"
    assert limite.incerto is False
    servidor = classificar_excecao_transporte(InternalServerError())
    assert servidor.codigo == "servidor"
    assert servidor.incerto is True


def test_interpreta_usage_truncamento_e_recusa():
    recusa = interpretar_mensagem(
        SimpleNamespace(
            stop_reason="refusal",
            usage=SimpleNamespace(input_tokens=4, output_tokens=0),
            content=[SimpleNamespace(type="refusal", text="")],
        )
    )
    assert recusa["recusa"] is True
    assert recusa["input_tokens"] == 4
    truncada = interpretar_mensagem(
        SimpleNamespace(
            stop_reason="max_tokens",
            usage=SimpleNamespace(input_tokens=3, output_tokens=8),
            content=[SimpleNamespace(type="text", text="parcial")],
        )
    )
    assert truncada["truncada"] is True
    assert truncada["texto"] == "parcial"


def test_desligada_por_padrao_nao_chama_nem_grava(app, monkeypatch):
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_ENABLED", raising=False)
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "talvez")
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-off@test.com")
        antes = _foto(franquia.id)
        monkeypatch.delenv("AGENTE_BI_ANTHROPIC_ENABLED", raising=False)
        ausente = _chamar(usuario, franquia, capturado)
        assert ausente["codigo"] == "desabilitado"
        monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "talvez")
        resultado = _chamar(usuario, franquia, capturado)
        assert resultado["codigo"] == "desabilitado"
        assert resultado["retries"] == 0
        assert capturado.get("chamadas", 0) == 0
        assert IaConsumoEvento.query.count() == 0
        assert IaChamadaTentativa.query.count() == 0
        assert _foto(franquia.id) == antes


def test_sucesso_registra_usage_no_motor_oficial_sem_reserva(app, monkeypatch):
    _habilitar(monkeypatch)

    def reserva_proibida(*_args, **_kwargs):
        raise AssertionError("reserva")

    monkeypatch.setattr("app.services.cleiton_billable_ai_call._reservar", reserva_proibida)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-ok@test.com")
        antes_reserva = Decimal(franquia.reserva_pendente or 0)
        resultado = _chamar(usuario, franquia, capturado)
        assert resultado["ok"] is True
        assert resultado["codigo"] == "ok"
        assert resultado["provedor"] == "anthropic"
        assert resultado["provedor_chamado"] is True
        assert resultado["usage"]["total_tokens"] == 2000
        assert capturado["pedido"]["max_tokens"] == 128
        assert capturado["pedido"]["model"] == "claude-sonnet-simulado"
        evento = db.session.get(IaConsumoEvento, resultado["evento_id"])
        assert evento.provider == "anthropic"
        assert evento.flow_type == PURPOSE_BI_LOGISTICO
        assert evento.agent == "agente_bi"
        assert evento.status == "success"
        assert evento.usuario_id == usuario.id
        assert evento.conta_id == usuario.conta_id
        assert evento.franquia_id == franquia.id
        assert evento.origem_sistema is False
        assert evento.regime_abatimento == IaConsumoEvento.REGIME_NOVO
        tentativa = db.session.get(IaChamadaTentativa, resultado["tentativa_id"])
        assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
        assert tentativa.ia_consumo_evento_id == evento.id
        assert Decimal(tentativa.reserved_credits) == 0
        assert tentativa.reserva_ativa is False
        assert tentativa.provider == "anthropic"
        abatimento = IaConsumoAbatimento.query.one()
        assert abatimento.ia_consumo_evento_id == evento.id
        assert Decimal(abatimento.creditos) == Decimal("2")
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == Decimal("2")
        assert Decimal(franquia.reserva_pendente or 0) == antes_reserva


def test_idempotencia_nao_repete_chamada_nem_abatimento(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-idem@test.com")
        primeira = _chamar(usuario, franquia, capturado)
        segunda = _chamar(usuario, franquia, capturado)
        assert primeira["evento_id"] == segunda["evento_id"]
        assert segunda["repetida"] is True
        assert segunda["provedor_chamado"] is False
        assert capturado["chamadas"] == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == Decimal("2")


def test_minimizacao_impede_cpf_de_seguir_ao_transporte(app, monkeypatch, caplog):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-pii@test.com")
        with caplog.at_level("INFO"):
            resultado = _chamar(
                usuario,
                franquia,
                capturado,
                mensagem=f"Frete SP 10 kg CPF {CPF}",
            )
        assert resultado["ok"] is True
        enviado = json.dumps(capturado["pedido"])
        assert CPF not in enviado
        assert "Frete" in enviado
        assert CPF not in caplog.text
        assert CPF not in (db.session.get(IaConsumoEvento, resultado["evento_id"]).error_summary or "")


def test_conteudo_nao_autorizado_nao_chama_provedor(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-block@test.com")
        antes = _foto(franquia.id)
        resultado = _chamar(usuario, franquia, capturado, mensagem=f"linha\x00{MARCADOR}")
        assert resultado["codigo"] == "conteudo_nao_autorizado"
        assert resultado["provedor_chamado"] is False
        assert capturado.get("chamadas", 0) == 0
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 0
        evento = IaConsumoEvento.query.one()
        assert evento.provider == "internal"
        assert MARCADOR not in (evento.error_summary or "")
        assert _foto(franquia.id) == antes
        tentativa = db.session.get(IaChamadaTentativa, resultado["tentativa_id"])
        assert tentativa.status == IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED
        assert Decimal(tentativa.reserved_credits) == 0


def test_timeout_5xx_e_429_sem_repeticao_insegura(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        usuario, franquia, escopo, analise = _seed_financeiro(email="anthropic-erro@test.com")
        criada = enfileirar(escopo, analise["id"])
        adquirida = adquirir(worker_id="worker-a")
        capturado = {}
        timeout = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia=criada["chave_idempotencia"],
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
            mensagem=f"Frete pendente {MARCADOR}",
            transporte=_transporte(
                capturado,
                erro=ErroTransporte("timeout", incerto=True),
            ),
        )
        assert timeout["codigo"] == "timeout"
        assert timeout["incerto"] is True
        assert capturado["chamadas"] == 1
        linha = db.session.get(AgenteBiExecucao, adquirida["id"])
        assert linha.estado == "incerta"
        assert linha.efeito_externo == "incerto"
        assert linha.ia_chamada_tentativa_id == timeout["tentativa_id"]
        evento = db.session.get(IaConsumoEvento, timeout["evento_id"])
        assert evento.status == "failure"
        assert MARCADOR not in (evento.error_summary or "")
        de_novo = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia=criada["chave_idempotencia"],
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
            transporte=_transporte(capturado, erro=ErroTransporte("timeout", incerto=True)),
        )
        assert de_novo["provedor_chamado"] is False
        assert capturado["chamadas"] == 1
        db.session.refresh(franquia)
        assert Decimal(franquia.reserva_pendente or 0) == 0

        _usuario_b, franquia_b, escopo_b, analise_b = _seed_financeiro(email="anthropic-429@test.com")
        enfileirar(escopo_b, analise_b["id"])
        adquirida_b = adquirir(worker_id="worker-b")
        capturado_b = {}
        limite = _chamar(
            _usuario_b,
            franquia_b,
            capturado_b,
            chave_idempotencia="agente-bi.limite.429",
            execucao_id=adquirida_b["id"],
            worker_id="worker-b",
            fencing=adquirida_b["fencing"],
            transporte=_transporte(
                capturado_b,
                erro=ErroTransporte("rate_limit", incerto=False, status=429),
            ),
        )
        assert limite["codigo"] == "rate_limit"
        assert limite["incerto"] is False
        assert db.session.get(AgenteBiExecucao, adquirida_b["id"]).estado == "adquirida"
        repetida = _chamar(
            _usuario_b,
            franquia_b,
            capturado_b,
            chave_idempotencia="agente-bi.limite.429",
            execucao_id=adquirida_b["id"],
            worker_id="worker-b",
            fencing=adquirida_b["fencing"],
        )
        assert repetida["provedor_chamado"] is False
        assert capturado_b["chamadas"] == 1

        servidor = classificar_excecao_transporte(type("Falha", (Exception,), {"status_code": 500})())
        assert servidor.incerto is True


def test_5xx_com_usage_permanece_incerto_e_nao_repete(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, escopo, analise = _seed_financeiro(email="anthropic-5xx@test.com")
        enfileirar(escopo, analise["id"])
        adquirida = adquirir(worker_id="worker-a")
        resultado = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.servidor.01",
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
            transporte=_transporte(
                capturado,
                erro=ErroTransporte("servidor", incerto=True, status=503, input_tokens=1000, output_tokens=0),
            ),
        )
        assert resultado["incerto"] is True
        assert resultado["usage"]["total_tokens"] == 1000
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == Decimal("1")
        assert db.session.get(AgenteBiExecucao, adquirida["id"]).estado == "incerta"
        _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.servidor.01",
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
        )
        assert capturado["chamadas"] == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1


def test_truncada_recusa_e_ausencia_de_usage(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-forma@test.com")
        truncada_cap = {}
        truncada = _chamar(
            usuario,
            franquia,
            truncada_cap,
            chave_idempotencia="agente-bi.truncada.01",
            transporte=_transporte(
                truncada_cap,
                resposta={
                    "texto": "parcial",
                    "input_tokens": 1000,
                    "output_tokens": 1000,
                    "truncada": True,
                    "recusa": False,
                },
            ),
        )
        assert truncada["codigo"] == "truncada"
        assert truncada["truncada"] is True
        assert truncada["ok"] is True
        recusa_cap = {}
        recusa = _chamar(
            usuario,
            franquia,
            recusa_cap,
            chave_idempotencia="agente-bi.recusa.01",
            transporte=_transporte(
                recusa_cap,
                resposta={
                    "texto": "",
                    "input_tokens": 500,
                    "output_tokens": 0,
                    "truncada": False,
                    "recusa": True,
                },
            ),
        )
        assert recusa["codigo"] == "recusa"
        assert recusa["ok"] is False
        assert recusa["recusa"] is True
        sem_cap = {}
        sem = _chamar(
            usuario,
            franquia,
            sem_cap,
            chave_idempotencia="agente-bi.semuso.01",
            transporte=_transporte(
                sem_cap,
                resposta={"texto": "sem metrica", "input_tokens": None, "output_tokens": None},
            ),
        )
        assert sem["codigo"] == "sem_usage"
        evento = db.session.get(IaConsumoEvento, sem["evento_id"])
        assert evento.status == "success_no_metrics"
        assert evento.total_tokens is None
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == Decimal("2.5")
        assert Decimal(franquia.reserva_pendente or 0) == 0


def test_falha_de_persistencia_nao_repete_a_chamada(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._persistir_evento",
        lambda *_args, **_kwargs: None,
    )
    with app.app_context():
        usuario, franquia, escopo, analise = _seed_financeiro(email="anthropic-persist@test.com")
        antes = _foto(franquia.id)
        enfileirar(escopo, analise["id"])
        adquirida = adquirir(worker_id="worker-a")
        resultado = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.persist.01",
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
        )
        assert resultado["codigo"] == "persistencia_falhou"
        assert resultado["incerto"] is True
        assert capturado["chamadas"] == 1
        assert db.session.get(AgenteBiExecucao, adquirida["id"]).estado == "incerta"
        assert IaConsumoEvento.query.count() == 0
        assert _foto(franquia.id) == antes
        de_novo = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.persist.01",
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=linha_fencing(adquirida["id"]),
        )
        assert de_novo["provedor_chamado"] is False
        assert capturado["chamadas"] == 1


def linha_fencing(execucao_id):
    return db.session.get(AgenteBiExecucao, execucao_id).fencing


def test_isolamento_de_usuario_conta_e_franquia(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        dono, franquia, escopo, analise = _seed_financeiro(email="anthropic-dono@test.com")
        enfileirar(escopo, analise["id"])
        adquirida = adquirir(worker_id="worker-a")
        outro_conta, outra_franquia = None, None
        from tests.conftest import seed_conta_franquia_cliente

        outro_conta, outra_franquia = seed_conta_franquia_cliente(slug="conta-anthropic-outro")
        outro = seed_usuario(outra_franquia.id, outro_conta.id, email="anthropic-outro@test.com")
        alheio = _chamar(
            outro,
            outra_franquia,
            capturado,
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
        )
        assert alheio["codigo"] == "isolamento"
        assert capturado.get("chamadas", 0) == 0
        assert IaConsumoEvento.query.count() == 0
        incoerente = _chamar(
            dono,
            franquia,
            capturado,
            conta_id=outro_conta.id,
        )
        assert incoerente["codigo"] == "identidade_incoerente"
        assert capturado.get("chamadas", 0) == 0
        propria = _chamar(
            dono,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.dono.01",
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
        )
        assert propria["ok"] is True
        evento = db.session.get(IaConsumoEvento, propria["evento_id"])
        assert evento.usuario_id == dono.id
        assert evento.franquia_id == franquia.id
        assert evento.conta_id == dono.conta_id
        repetida_por_outro = _chamar(
            outro,
            outra_franquia,
            capturado,
            chave_idempotencia="agente-bi.dono.01",
        )
        assert repetida_por_outro["codigo"] == "isolamento"
        assert capturado["chamadas"] == 1


def test_admissao_recusada_nao_chama_provedor(app, monkeypatch):
    _habilitar(monkeypatch)
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-adm@test.com")
        franquia.bloqueio_manual = True
        db.session.commit()
        antes = _foto(franquia.id)
        resultado = _chamar(usuario, franquia, capturado)
        assert resultado["codigo"] == "admissao_recusada"
        assert resultado["provedor_chamado"] is False
        assert capturado.get("chamadas", 0) == 0
        assert IaConsumoEvento.query.count() == 0
        assert _foto(franquia.id) == antes
        tentativa = db.session.get(IaChamadaTentativa, resultado["tentativa_id"])
        assert tentativa.reserva_ativa is False
        assert Decimal(tentativa.reserved_credits) == 0


def test_falha_entre_evento_e_vinculo_nao_deixa_consumo_orfao(app, monkeypatch):
    _habilitar(monkeypatch)
    visto = {}

    def quebra(atual, status, evento_id, motivo, encerrar):
        visto["eventos_na_transacao"] = IaConsumoEvento.query.count()
        visto["vinculo_na_transacao"] = atual.ia_consumo_evento_id
        raise RuntimeError("entre")

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._aplicar_estado_tentativa",
        quebra,
    )
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-entre@test.com")
        antes = _foto(franquia.id)
        resultado = _chamar(usuario, franquia, capturado, chave_idempotencia="agente-bi.entre.01")
        assert visto["eventos_na_transacao"] == 1
        assert visto["vinculo_na_transacao"] is None
        assert resultado["codigo"] == "persistencia_falhou"
        assert resultado["incerto"] is True
        assert capturado["chamadas"] == 1
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        tentativa = db.session.get(IaChamadaTentativa, resultado["tentativa_id"])
        assert tentativa.status == IaChamadaTentativa.STATUS_UNCERTAIN
        assert tentativa.ia_consumo_evento_id is None
        assert _foto(franquia.id) == antes
        de_novo = _chamar(usuario, franquia, capturado, chave_idempotencia="agente-bi.entre.01")
        assert de_novo["provedor_chamado"] is False
        assert capturado["chamadas"] == 1
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_falha_durante_abatimento_recupera_na_mesma_chave(app, monkeypatch):
    _habilitar(monkeypatch)
    from app.services.agente_bi_anthropic_service import aplicar_motor_apos_ia_consumo_evento

    estado = {"n": 0}

    def uma_vez(evento_id, **kwargs):
        estado["n"] += 1
        if estado["n"] == 1:
            raise RuntimeError("abatimento")
        return aplicar_motor_apos_ia_consumo_evento(evento_id, **kwargs)

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.aplicar_motor_apos_ia_consumo_evento",
        uma_vez,
    )
    capturado = {}
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-motor@test.com")
        primeira = _chamar(usuario, franquia, capturado, chave_idempotencia="agente-bi.motor.01")
        assert primeira["ok"] is True
        assert capturado["chamadas"] == 1
        tentativa = db.session.get(IaChamadaTentativa, primeira["tentativa_id"])
        assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
        assert tentativa.ia_consumo_evento_id == primeira["evento_id"]
        assert IaConsumoEvento.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 0
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == 0
        segunda = _chamar(usuario, franquia, capturado, chave_idempotencia="agente-bi.motor.01")
        assert segunda["provedor_chamado"] is False
        assert segunda["repetida"] is True
        assert capturado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 1
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == Decimal("2")
        _chamar(usuario, franquia, capturado, chave_idempotencia="agente-bi.motor.01")
        assert capturado["chamadas"] == 1
        assert IaConsumoEvento.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == Decimal("2")


def test_efeito_incerto_reaplica_o_motor_sem_nova_chamada(app, monkeypatch):
    _habilitar(monkeypatch)
    from app.services.agente_bi_anthropic_service import aplicar_motor_apos_ia_consumo_evento

    estado = {"n": 0}

    def uma_vez(evento_id, **kwargs):
        estado["n"] += 1
        if estado["n"] == 1:
            raise RuntimeError("abatimento")
        return aplicar_motor_apos_ia_consumo_evento(evento_id, **kwargs)

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.aplicar_motor_apos_ia_consumo_evento",
        uma_vez,
    )
    capturado = {}
    with app.app_context():
        usuario, franquia, escopo, analise = _seed_financeiro(email="anthropic-incerto-motor@test.com")
        enfileirar(escopo, analise["id"])
        adquirida = adquirir(worker_id="worker-a")
        resultado = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.incerto.motor",
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
            transporte=_transporte(
                capturado,
                erro=ErroTransporte("servidor", incerto=True, status=503, input_tokens=1000, output_tokens=0),
            ),
        )
        assert resultado["incerto"] is True
        assert capturado["chamadas"] == 1
        assert db.session.get(AgenteBiExecucao, adquirida["id"]).estado == "incerta"
        tentativa = db.session.get(IaChamadaTentativa, resultado["tentativa_id"])
        assert tentativa.status == IaChamadaTentativa.STATUS_UNCERTAIN
        assert tentativa.ia_consumo_evento_id == resultado["evento_id"]
        assert IaConsumoAbatimento.query.count() == 0
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == 0
        de_novo = _chamar(
            usuario,
            franquia,
            capturado,
            chave_idempotencia="agente-bi.incerto.motor",
            execucao_id=adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
        )
        assert de_novo["provedor_chamado"] is False
        assert de_novo["incerto"] is True
        assert capturado["chamadas"] == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        db.session.refresh(franquia)
        assert Decimal(franquia.consumo_acumulado) == Decimal("1")
        assert Decimal(franquia.reserva_pendente or 0) == 0


def test_sem_credencial_nao_abre_transporte_real(app, monkeypatch):
    _habilitar(monkeypatch)
    with app.app_context():
        usuario, franquia, _escopo, _analise = _seed_financeiro(email="anthropic-key@test.com")
        resultado = chamar_anthropic_governado(
            usuario_id=usuario.id,
            conta_id=usuario.conta_id,
            franquia_id=franquia.id,
            mensagem="Frete SP 10 kg",
            chave_idempotencia="agente-bi.semchave.1",
            model="claude-sonnet-simulado",
        )
        assert resultado["codigo"] == "credencial_ausente"
        assert resultado["provedor_chamado"] is False
        assert IaConsumoEvento.query.count() == 0
