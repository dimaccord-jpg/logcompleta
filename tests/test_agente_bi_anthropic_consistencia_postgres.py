"""Consistência financeira do adaptador Anthropic em PostgreSQL descartável.

O cluster é o mesmo da fila do AgenteBI. Não aponta para banco operacional.

    AGENTE_BI_EXECUCAO_PG_URI=postgresql+psycopg2://pytest@127.0.0.1:55432/agente_bi_execucao_descartavel
"""
from __future__ import annotations

import os
import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db_operational_safety import parse_database_identity
from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiDataset,
    AgenteBiDatasetBloco,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    CleitonCostConfig,
    Conta,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    User,
    utcnow_naive,
)
from app.services.agente_bi_anthropic_service import (
    ErroTransporte,
    aplicar_motor_apos_ia_consumo_evento,
    chamar_anthropic_governado,
)
from app.services.agente_bi_execucao_service import adquirir, enfileirar
from tests.conftest import seed_cleiton_cost_config, seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

BANCO = "agente_bi_execucao_descartavel"


def _uri() -> str:
    uri = (os.environ.get("AGENTE_BI_EXECUCAO_PG_URI") or "").strip()
    ident = parse_database_identity(uri)
    if ident is None or not ident.is_postgres:
        pytest.skip("PostgreSQL descartável não configurado")
    if ident.database != BANCO or ident.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("URI recusada: só o cluster local descartável desta prova")
    return uri


@pytest.fixture
def pg_app(monkeypatch):
    uri = _uri()
    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    flask_app = __import__("flask").Flask("agente-bi-anthropic-pg")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    tabelas = [
        Conta.__table__,
        Franquia.__table__,
        User.__table__,
        CleitonCostConfig.__table__,
        IaChamadaTentativa.__table__,
        IaConsumoEvento.__table__,
        IaConsumoAbatimento.__table__,
        AgenteBiDataset.__table__,
        AgenteBiDatasetBloco.__table__,
        AgenteBiAnalise.__table__,
        AgenteBiExecucao.__table__,
        AgenteBiExecucaoEvento.__table__,
    ]
    with flask_app.app_context():
        db.metadata.drop_all(bind=db.engine, tables=list(reversed(tabelas)))
        db.metadata.create_all(bind=db.engine, tables=tabelas)
        yield flask_app
        db.session.remove()
        db.metadata.drop_all(bind=db.engine, tables=list(reversed(tabelas)))
        db.session.remove()


def _dono(email):
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = Decimal("100")
    franquia.consumo_acumulado = Decimal("0")
    franquia.reserva_pendente = Decimal("0")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email)
    return usuario, franquia


def _resposta():
    return {
        "texto": "duas rotas com frete distinto",
        "stop_reason": "end_turn",
        "input_tokens": 1000,
        "output_tokens": 1000,
        "recusa": False,
        "truncada": False,
    }


def _transporte(estado, resposta=None, erro=None, ao_entrar=None):
    def enviar(pedido):
        estado["chamadas"] = estado.get("chamadas", 0) + 1
        if ao_entrar is not None:
            ao_entrar()
        if erro is not None:
            raise erro
        return resposta or _resposta()

    return enviar


def _chamar(usuario, franquia, estado, chave, **extras):
    argumentos = {
        "usuario_id": usuario.id,
        "conta_id": usuario.conta_id,
        "franquia_id": franquia.id,
        "mensagem": "Frete SP 10 kg",
        "chave_idempotencia": chave,
        "transporte": extras.pop("transporte", _transporte(estado)),
        "model": "claude-sonnet-simulado",
        "max_tokens": 128,
    }
    argumentos.update(extras)
    return chamar_anthropic_governado(**argumentos)


def _consumo(franquia_id) -> Decimal:
    franquia = db.session.get(Franquia, franquia_id)
    db.session.refresh(franquia)
    return Decimal(franquia.consumo_acumulado or 0)


def test_falha_antes_da_persistencia_nao_cobra_nem_repete(pg_app, monkeypatch):
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._persistir_evento",
        lambda *_args, **_kwargs: None,
    )
    estado = {}
    usuario, franquia = _dono("pg-antes@test.com")
    resultado = _chamar(usuario, franquia, estado, "agente-bi.pg.antes.01")
    assert resultado["codigo"] == "persistencia_falhou"
    assert resultado["incerto"] is True
    assert estado["chamadas"] == 1
    assert IaConsumoEvento.query.count() == 0
    assert IaConsumoAbatimento.query.count() == 0
    assert _consumo(franquia.id) == 0
    de_novo = _chamar(usuario, franquia, estado, "agente-bi.pg.antes.01")
    assert de_novo["provedor_chamado"] is False
    assert estado["chamadas"] == 1
    assert IaConsumoEvento.query.count() == 0
    assert _consumo(franquia.id) == 0


def test_falha_entre_evento_e_vinculo_desfaz_a_transacao(pg_app, monkeypatch):
    visto = {}

    def quebra(atual, status, evento_id, motivo, encerrar):
        visto["eventos"] = IaConsumoEvento.query.count()
        visto["vinculo"] = atual.ia_consumo_evento_id
        raise RuntimeError("entre")

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._aplicar_estado_tentativa",
        quebra,
    )
    estado = {}
    usuario, franquia = _dono("pg-entre@test.com")
    resultado = _chamar(usuario, franquia, estado, "agente-bi.pg.entre.01")
    assert visto["eventos"] == 1
    assert visto["vinculo"] is None
    assert resultado["incerto"] is True
    assert estado["chamadas"] == 1
    assert IaConsumoEvento.query.count() == 0
    assert IaConsumoAbatimento.query.count() == 0
    tentativa = db.session.get(IaChamadaTentativa, resultado["tentativa_id"])
    assert tentativa.status == IaChamadaTentativa.STATUS_UNCERTAIN
    assert tentativa.ia_consumo_evento_id is None
    assert _consumo(franquia.id) == 0
    de_novo = _chamar(usuario, franquia, estado, "agente-bi.pg.entre.01")
    assert de_novo["provedor_chamado"] is False
    assert estado["chamadas"] == 1
    assert IaConsumoEvento.query.count() == 0
    assert IaConsumoAbatimento.query.count() == 0


def test_falha_antes_do_abatimento_recupera_na_mesma_chave(pg_app, monkeypatch):
    from app.services import agente_bi_anthropic_service as servico

    pulos = {"n": 0}
    motor_real = servico._aplicar_motor

    def pula_a_primeira(evento_id):
        pulos["n"] += 1
        if pulos["n"] == 1:
            return None
        return motor_real(evento_id)

    monkeypatch.setattr(servico, "_aplicar_motor", pula_a_primeira)
    estado = {}
    usuario, franquia = _dono("pg-antes-motor@test.com")
    primeira = _chamar(usuario, franquia, estado, "agente-bi.pg.antes.motor")
    assert primeira["ok"] is True
    assert IaConsumoEvento.query.count() == 1
    assert IaConsumoAbatimento.query.count() == 0
    assert _consumo(franquia.id) == 0
    tentativa = db.session.get(IaChamadaTentativa, primeira["tentativa_id"])
    assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
    assert tentativa.ia_consumo_evento_id == primeira["evento_id"]
    segunda = _chamar(usuario, franquia, estado, "agente-bi.pg.antes.motor")
    terceira = _chamar(usuario, franquia, estado, "agente-bi.pg.antes.motor")
    assert segunda["provedor_chamado"] is False
    assert terceira["provedor_chamado"] is False
    assert estado["chamadas"] == 1
    assert IaConsumoEvento.query.count() == 1
    assert IaConsumoAbatimento.query.count() == 1
    assert _consumo(franquia.id) == Decimal("2")


def test_falha_durante_o_abatimento_recupera_sem_duplicar(pg_app, monkeypatch):
    from app.services import agente_bi_anthropic_service as servico

    falhas = {"n": 0}

    def falha_uma_vez(evento_id, **kwargs):
        falhas["n"] += 1
        if falhas["n"] == 1:
            raise RuntimeError("abatimento")
        return aplicar_motor_apos_ia_consumo_evento(evento_id, **kwargs)

    monkeypatch.setattr(servico, "aplicar_motor_apos_ia_consumo_evento", falha_uma_vez)
    estado = {}
    usuario, franquia = _dono("pg-durante-motor@test.com")
    primeira = _chamar(usuario, franquia, estado, "agente-bi.pg.durante.motor")
    assert primeira["ok"] is True
    assert IaConsumoEvento.query.count() == 1
    assert IaConsumoAbatimento.query.count() == 0
    assert _consumo(franquia.id) == 0
    segunda = _chamar(usuario, franquia, estado, "agente-bi.pg.durante.motor")
    terceira = _chamar(usuario, franquia, estado, "agente-bi.pg.durante.motor")
    assert segunda["provedor_chamado"] is False
    assert terceira["provedor_chamado"] is False
    assert estado["chamadas"] == 1
    assert IaConsumoEvento.query.count() == 1
    assert IaConsumoAbatimento.query.count() == 1
    assert _consumo(franquia.id) == Decimal("2")


def test_efeito_incerto_recupera_abatimento_sem_segunda_chamada(pg_app, monkeypatch):
    from app.services.agente_bi_persistencia_service import criar_analise, criar_dataset, escopo_do_usuario
    from tests.test_agente_bi_execucao import _analise, _dataset

    falhas = {"n": 0}

    def falha_uma_vez(evento_id, **kwargs):
        falhas["n"] += 1
        if falhas["n"] == 1:
            raise RuntimeError("abatimento")
        return aplicar_motor_apos_ia_consumo_evento(evento_id, **kwargs)

    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.aplicar_motor_apos_ia_consumo_evento",
        falha_uma_vez,
    )
    usuario, franquia = _dono("pg-incerto@test.com")
    escopo = escopo_do_usuario(usuario)
    dataset = criar_dataset(escopo, _dataset())
    analise = criar_analise(escopo, _analise(dataset))
    enfileirar(escopo, analise["id"])
    adquirida = adquirir(worker_id="worker-pg")
    estado = {}
    resultado = _chamar(
        usuario,
        franquia,
        estado,
        "agente-bi.pg.incerto.01",
        execucao_id=adquirida["id"],
        worker_id="worker-pg",
        fencing=adquirida["fencing"],
        transporte=_transporte(
            estado,
            erro=ErroTransporte("servidor", incerto=True, status=503, input_tokens=1000, output_tokens=0),
        ),
    )
    assert resultado["incerto"] is True
    assert db.session.get(AgenteBiExecucao, adquirida["id"]).estado == "incerta"
    assert IaConsumoAbatimento.query.count() == 0
    assert _consumo(franquia.id) == 0
    de_novo = _chamar(
        usuario,
        franquia,
        estado,
        "agente-bi.pg.incerto.01",
        execucao_id=adquirida["id"],
        worker_id="worker-pg",
        fencing=adquirida["fencing"],
    )
    assert de_novo["provedor_chamado"] is False
    assert estado["chamadas"] == 1
    assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
    assert IaConsumoAbatimento.query.count() == 1
    assert _consumo(franquia.id) == Decimal("1")
    tentativa = db.session.get(IaChamadaTentativa, resultado["tentativa_id"])
    assert tentativa.ia_consumo_evento_id == resultado["evento_id"]
    assert Decimal(tentativa.reserved_credits) == 0
    assert Decimal(db.session.get(Franquia, franquia.id).reserva_pendente or 0) == 0


def test_concorrencia_da_mesma_chave_chama_e_cobra_uma_vez(pg_app):
    usuario, franquia = _dono("pg-corrida@test.com")
    ids = (usuario.id, usuario.conta_id, franquia.id)
    db.session.remove()
    estado = {"chamadas": 0}
    entrou = threading.Event()
    seguir = threading.Event()
    largada = threading.Barrier(2)
    erros = []
    respostas = []
    trava = threading.Lock()

    def enviar(pedido):
        with trava:
            estado["chamadas"] += 1
        entrou.set()
        assert seguir.wait(8)
        return _resposta()

    def worker():
        try:
            with pg_app.app_context():
                db.session.execute(text("SET lock_timeout = '4000ms'"))
                largada.wait(8)
                respostas.append(
                    chamar_anthropic_governado(
                        usuario_id=ids[0],
                        conta_id=ids[1],
                        franquia_id=ids[2],
                        mensagem="Frete SP 10 kg",
                        chave_idempotencia="agente-bi.pg.corrida.01",
                        transporte=enviar,
                        model="claude-sonnet-simulado",
                        max_tokens=128,
                    )
                )
        except Exception as exc:
            erros.append(exc)
            try:
                largada.abort()
            except Exception:
                pass
        finally:
            with pg_app.app_context():
                db.session.remove()

    primeira = threading.Thread(target=worker)
    segunda = threading.Thread(target=worker)
    primeira.start()
    segunda.start()
    try:
        assert entrou.wait(8)
    finally:
        seguir.set()
    primeira.join(10)
    segunda.join(10)
    assert erros == []
    assert estado["chamadas"] == 1
    assert len(respostas) == 2
    assert sum(1 for item in respostas if item["provedor_chamado"]) == 1
    assert IaChamadaTentativa.query.count() == 1
    assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
    assert IaConsumoAbatimento.query.count() == 1
    assert _consumo(ids[2]) == Decimal("2")
    tentativa = IaChamadaTentativa.query.one()
    assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
    assert tentativa.ia_consumo_evento_id is not None
    assert Decimal(tentativa.reserved_credits) == 0

    repetidas = []

    def worker_repetido():
        try:
            with pg_app.app_context():
                largada_repetida.wait(8)
                repetidas.append(
                    chamar_anthropic_governado(
                        usuario_id=ids[0],
                        conta_id=ids[1],
                        franquia_id=ids[2],
                        mensagem="Frete SP 10 kg",
                        chave_idempotencia="agente-bi.pg.corrida.01",
                        transporte=enviar,
                        model="claude-sonnet-simulado",
                        max_tokens=128,
                    )
                )
        except Exception as exc:
            erros.append(exc)
        finally:
            with pg_app.app_context():
                db.session.remove()

    largada_repetida = threading.Barrier(2)
    uma = threading.Thread(target=worker_repetido)
    outra = threading.Thread(target=worker_repetido)
    uma.start()
    outra.start()
    uma.join(10)
    outra.join(10)
    assert erros == []
    assert estado["chamadas"] == 1
    assert len(repetidas) == 2
    assert all(item["provedor_chamado"] is False for item in repetidas)
    assert IaConsumoAbatimento.query.count() == 1
    assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
    assert _consumo(ids[2]) == Decimal("2")


def test_gemini_e_agenteaudita_permanecem_no_motor_oficial(pg_app):
    import inspect

    from app.cleide_gemini_adapter import generate_cleide_ai_reply
    from app.run_cleiton_gemini_governance import _persist_event
    from app.services.cleiton_billable_ai_call import cleiton_governed_generate_content

    fonte_audita = inspect.getsource(generate_cleide_ai_reply)
    assert "cleiton_governed_generate_content" in fonte_audita
    assert "chamar_anthropic_governado" not in fonte_audita
    assert "_consolidar_evento_tentativa" not in fonte_audita
    fonte_fachada = inspect.getsource(cleiton_governed_generate_content)
    assert "_consolidar_evento_tentativa" not in fonte_fachada

    usuario, franquia = _dono("pg-gemini@test.com")
    evento_id, situacao = _persist_event(
        operation="generate_content",
        model="gemini-simulado",
        agent="cleide",
        flow_type="cleide_chat_auditoria_frete_ai",
        api_key_label="gemini",
        status="success",
        input_tokens=1000,
        output_tokens=1000,
        total_tokens=2000,
        identidade={
            "conta_id": usuario.conta_id,
            "franquia_id": franquia.id,
            "usuario_id": usuario.id,
            "tipo_origem": "cleide_ai",
            "origem_sistema": False,
        },
    )
    assert situacao == "persisted"
    assert evento_id is not None
    aplicar_motor_apos_ia_consumo_evento(int(evento_id))
    assert IaConsumoEvento.query.filter_by(provider="gemini").count() == 1
    assert IaConsumoAbatimento.query.count() == 1
    assert _consumo(franquia.id) == Decimal("2")
    assert IaChamadaTentativa.query.count() == 0
