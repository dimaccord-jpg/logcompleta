"""Concorrência real da fila do AgenteBI em PostgreSQL descartável.

SQLite não prova SKIP LOCKED. Este módulo só roda contra o cluster
local criado para a prova, nunca contra o banco operacional.

    AGENTE_BI_EXECUCAO_PG_URI=postgresql+psycopg2://pytest@127.0.0.1:55432/agente_bi_execucao_descartavel
"""
from __future__ import annotations

import os
import threading
import time
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
    Conta,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    User,
    utcnow_naive,
)
from app.services.agente_bi_execucao_service import (
    MAX_TENTATIVAS,
    SQL_ADQUIRIR,
    SQL_ADQUIRIR_DIRETA,
    ExecucaoErro,
    adquirir,
    cancelar,
    confirmar,
    enfileirar,
    executar_um_ciclo,
    falhar,
    marcar_efeito_incerto,
    obter,
    recuperar_leases,
)
from app.services.agente_bi_persistencia_service import (
    criar_analise,
    criar_dataset,
    escopo_do_usuario,
)
from tests.test_agente_bi_execucao import (
    _analise,
    _dataset,
    _foto_financeira,
    _outra_analise,
    _proibir_financeiro_e_provedor,
    _sem_vinculo_financeiro,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario

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
def pg_app():
    uri = _uri()
    flask_app = __import__("flask").Flask("agente-bi-execucao-pg")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    tabelas = [
        Conta.__table__,
        Franquia.__table__,
        User.__table__,
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


def _base(email):
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    usuario = seed_usuario(franquia.id, conta.id, email=email)
    escopo = escopo_do_usuario(usuario)
    dataset = criar_dataset(escopo, _dataset())
    analise = criar_analise(escopo, _analise(dataset, chave=f"analise-{email.split('@')[0]}"))
    return escopo, analise


def test_dois_workers_um_so_leva_o_trabalho(pg_app):
    with pg_app.app_context():
        escopo, analise = _base("pg-um@test.com")
        enfileirar(escopo, analise["id"])
        db.session.remove()

    liberar = threading.Event()
    dentro = threading.Event()
    erros = []
    vistos = {}

    def worker_a():
        try:
            with pg_app.app_context():
                def pausa():
                    dentro.set()
                    assert liberar.wait(5)

                vistos["a"] = adquirir(worker_id="worker-a", antes_do_commit=pausa)
        except Exception as exc:
            erros.append(exc)
            liberar.set()
        finally:
            with pg_app.app_context():
                db.session.remove()

    def worker_b():
        try:
            assert dentro.wait(5)
            with pg_app.app_context():
                db.session.execute(text("SET lock_timeout = '1500ms'"))
                inicio = time.perf_counter()
                vistos["b"] = adquirir(worker_id="worker-b")
                vistos["decorrido"] = time.perf_counter() - inicio
        except Exception as exc:
            erros.append(exc)
        finally:
            liberar.set()
            with pg_app.app_context():
                db.session.remove()

    primeira = threading.Thread(target=worker_a)
    segunda = threading.Thread(target=worker_b)
    primeira.start()
    segunda.start()
    primeira.join(10)
    segunda.join(10)
    assert erros == []
    assert vistos["a"] is not None
    assert vistos["b"] is None
    assert vistos["decorrido"] < 1.2
    with pg_app.app_context():
        assert AgenteBiExecucao.query.filter_by(estado="adquirida").count() == 1
        assert AgenteBiAnalise.query.count() == 1


def test_dois_workers_dois_trabalhos(pg_app):
    with pg_app.app_context():
        escopo, analise = _base("pg-dois@test.com")
        enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.job.um")
        outra = criar_analise(
            escopo,
            _analise(
                {"id": analise["dataset_id"], "versao": analise["versao"], "impressao": analise["impressao"]},
                chave="analise-segunda",
                rodada=1,
            ),
        )
        enfileirar(escopo, outra["id"], chave_idempotencia="agente-bi.job.dois")
        db.session.remove()

    liberar = threading.Event()
    dentro = threading.Event()
    erros = []
    vistos = {}

    def worker_a():
        try:
            with pg_app.app_context():
                def pausa():
                    dentro.set()
                    assert liberar.wait(5)

                vistos["a"] = adquirir(worker_id="worker-a", antes_do_commit=pausa)
        except Exception as exc:
            erros.append(exc)
            liberar.set()
        finally:
            with pg_app.app_context():
                db.session.remove()

    def worker_b():
        try:
            assert dentro.wait(5)
            with pg_app.app_context():
                db.session.execute(text("SET lock_timeout = '1500ms'"))
                inicio = time.perf_counter()
                vistos["b"] = adquirir(worker_id="worker-b")
                vistos["decorrido"] = time.perf_counter() - inicio
        except Exception as exc:
            erros.append(exc)
        finally:
            liberar.set()
            with pg_app.app_context():
                db.session.remove()

    threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert erros == []
    assert vistos["a"]["id"] != vistos["b"]["id"]
    assert vistos["decorrido"] < 1.2
    with pg_app.app_context():
        assert AgenteBiExecucao.query.filter_by(estado="adquirida").count() == 2


def test_fencing_obsoleto_e_recuperacao_em_outra_conexao(pg_app):
    with pg_app.app_context():
        escopo, analise = _base("pg-fence@test.com")
        enfileirar(escopo, analise["id"])
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        db.session.remove()
        engine = db.engine
    with engine.connect() as conexao:
        estado = conexao.execute(text("SELECT estado, fencing FROM agente_bi_execucao")).one()
        assert estado.estado == "adquirida"
        assert estado.fencing == 1
    with pg_app.app_context():
        recuperar_leases(agora=agora + timedelta(seconds=31))
        with pytest.raises(ExecucaoErro) as exc:
            confirmar(adquirida["id"], worker_id="worker-a", fencing=1, agora=agora + timedelta(seconds=32))
        assert exc.value.codigo == "fencing_recusado"
        ciclo = executar_um_ciclo(worker_id="worker-b", agora=agora + timedelta(seconds=32))
        assert ciclo["execucao"]["estado"] == "concluida"
        assert AgenteBiAnalise.query.count() == 1
        rodada = db.session.get(AgenteBiAnalise, analise["id"])
        assert rodada.resultado_json is not None
        assert "execucao_simulada.v1" in rodada.resultado_json
        assert "analisar.v1" not in rodada.resultado_json
        assert "NAOLOGAR" not in rodada.resultado_json


@pytest.mark.parametrize(
    "passos",
    [
        ("falha", "falha", "queda"),
        ("queda", "falha", "queda"),
        ("falha", "queda", "queda"),
    ],
)
def test_lease_expirado_no_teto_nao_fica_aguardando(pg_app, passos):
    with pg_app.app_context():
        escopo, analise = _base(f"pg-{'-'.join(passos)}@test.com")
        criada = enfileirar(escopo, analise["id"], chave_idempotencia=f"agente-bi.pg.{'-'.join(passos)}")
        agora = utcnow_naive()

        def quebra(_refs):
            raise RuntimeError("instavel")

        for indice, passo in enumerate(passos):
            momento = agora + timedelta(seconds=indice * 40)
            if passo == "falha":
                ciclo = executar_um_ciclo(worker_id="worker-a", agora=momento, processador=quebra)
                assert ciclo["execucao"]["estado"] in {"falha_recuperavel", "falha_definitiva"}
            else:
                reivindicada = adquirir(worker_id="worker-a", agora=momento)
                assert reivindicada is not None
                assert recuperar_leases(agora=momento + timedelta(seconds=31)) == 1
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "falha_definitiva"
        assert linha.tentativas == MAX_TENTATIVAS
        assert _eventos(linha.id)[-1][2] == "tentativas_esgotadas"
        assert db.session.get(AgenteBiAnalise, analise["id"]).estado == "falha_validacao"
        assert executar_um_ciclo(worker_id="worker-b")["adquirida"] is False


def _esperar(evento, segundos=8):
    assert evento.wait(segundos)


def _financeiro(email):
    import app.services.conta_franquia_service as contas

    contas._sistema_ids_cache = None
    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        from tests.conftest import seed_sistema_interno

        seed_sistema_interno()
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = Decimal("100")
    franquia.consumo_acumulado = Decimal("3")
    franquia.reserva_pendente = Decimal("2")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email)
    escopo = escopo_do_usuario(usuario)
    dataset = criar_dataset(escopo, _dataset())
    analise = criar_analise(escopo, _analise(dataset, chave=f"analise-{email.split('@')[0]}"))
    return escopo, analise, int(franquia.id)


def _eventos(execucao_id):
    return [
        (evento.estado_novo, evento.fencing, evento.codigo)
        for evento in (
            AgenteBiExecucaoEvento.query.filter_by(execucao_id=execucao_id)
            .order_by(AgenteBiExecucaoEvento.id.asc())
            .all()
        )
    ]


def test_confirmacao_antiga_nao_sobrescreve_lease_recuperado(pg_app):
    with pg_app.app_context():
        escopo, analise = _base("corrida-lease@test.com")
        criada = enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.corrida.lease")
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        db.session.remove()

    pronto = threading.Event()
    seguir = threading.Event()
    erros = []
    vistos = {}

    def worker_a():
        try:
            with pg_app.app_context():
                def pausa():
                    pronto.set()
                    _esperar(seguir)

                confirmar(
                    adquirida["id"],
                    worker_id="worker-a",
                    fencing=adquirida["fencing"],
                    agora=agora + timedelta(seconds=5),
                    antes_da_gravacao=pausa,
                )
        except ExecucaoErro as exc:
            erros.append(exc.codigo)
        finally:
            with pg_app.app_context():
                db.session.remove()

    def worker_b():
        try:
            _esperar(pronto)
            with pg_app.app_context():
                assert recuperar_leases(agora=agora + timedelta(seconds=31)) == 1
                vistos["b"] = adquirir(worker_id="worker-b", agora=agora + timedelta(seconds=32))
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            seguir.set()
            with pg_app.app_context():
                db.session.remove()

    threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert erros == ["fencing_recusado"]
    assert vistos["b"]["fencing"] == 3
    assert vistos["b"]["lease_worker"] == "worker-b"
    with pg_app.app_context():
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "adquirida"
        assert linha.fencing == 3
        assert linha.lease_worker == "worker-b"
        assert [item for item in _eventos(linha.id) if item[0] == "concluida"] == []
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json is None
        concluida = confirmar(
            linha.id,
            worker_id="worker-b",
            fencing=3,
            agora=agora + timedelta(seconds=33),
        )
        assert concluida["estado"] == "concluida"
        assert concluida["fencing"] == 3
        assert [item for item in _eventos(linha.id) if item[0] == "concluida"] == [("concluida", 3, "concluida")]


def test_confirmacao_antiga_nao_sobrescreve_incerta(pg_app):
    with pg_app.app_context():
        escopo, analise = _base("corrida-incerta@test.com")
        criada = enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.corrida.incerta")
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        db.session.remove()

    pronto = threading.Event()
    seguir = threading.Event()
    erros = []

    def worker_a():
        try:
            with pg_app.app_context():
                def pausa():
                    pronto.set()
                    _esperar(seguir)

                confirmar(
                    adquirida["id"],
                    worker_id="worker-a",
                    fencing=adquirida["fencing"],
                    agora=agora + timedelta(seconds=1),
                    antes_da_gravacao=pausa,
                )
        except ExecucaoErro as exc:
            erros.append(exc.codigo)
        finally:
            with pg_app.app_context():
                db.session.remove()

    def worker_b():
        try:
            _esperar(pronto)
            with pg_app.app_context():
                marcar_efeito_incerto(
                    adquirida["id"],
                    worker_id="worker-a",
                    fencing=adquirida["fencing"],
                    agora=agora + timedelta(seconds=1),
                )
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            seguir.set()
            with pg_app.app_context():
                db.session.remove()

    threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert erros == ["fencing_recusado"]
    with pg_app.app_context():
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        rodada = db.session.get(AgenteBiAnalise, analise["id"])
        assert linha.estado == "incerta"
        assert linha.efeito_externo == "incerto"
        assert linha.fencing == 2
        assert rodada.resultado_json is None
        assert [item for item in _eventos(linha.id) if item[0] == "concluida"] == []
        assert ("incerta", 2, "efeito_incerto") in _eventos(linha.id)


def test_duas_confirmacoes_simultaneas_gravam_um_evento(pg_app):
    with pg_app.app_context():
        escopo, analise = _base("simultanea@test.com")
        criada = enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.simultanea.um")
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        db.session.remove()

    barreira = threading.Barrier(2)
    erros = []
    vistos = []

    def confirmar_um():
        try:
            with pg_app.app_context():
                def pausa():
                    barreira.wait(8)

                vistos.append(
                    confirmar(
                        adquirida["id"],
                        worker_id="worker-a",
                        fencing=adquirida["fencing"],
                        agora=agora + timedelta(seconds=1),
                        antes_da_gravacao=pausa,
                    )
                )
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            with pg_app.app_context():
                db.session.remove()

    threads = [threading.Thread(target=confirmar_um), threading.Thread(target=confirmar_um)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert erros == []
    assert [item["estado"] for item in vistos] == ["concluida", "concluida"]
    with pg_app.app_context():
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "concluida"
        assert linha.fencing == 1
        assert [item for item in _eventos(linha.id) if item[0] == "concluida"] == [("concluida", 1, "concluida")]
        repetida = confirmar(linha.id, worker_id="worker-a", fencing=1, agora=agora + timedelta(seconds=2))
        assert repetida["id"] == linha.id
        assert [item for item in _eventos(linha.id) if item[0] == "concluida"] == [("concluida", 1, "concluida")]


def test_rollback_da_disputa_nao_grava_evento(pg_app):
    with pg_app.app_context():
        escopo, analise = _base("rollback@test.com")
        criada = enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.rollback.um")
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        db.session.remove()

    segurou = threading.Event()
    erros = []
    vistos = {}

    def worker_a():
        try:
            with pg_app.app_context():
                def pausa():
                    segurou.set()
                    time.sleep(0.6)
                    raise RuntimeError("disputa")

                confirmar(
                    adquirida["id"],
                    worker_id="worker-a",
                    fencing=adquirida["fencing"],
                    agora=agora + timedelta(seconds=1),
                    antes_do_commit=pausa,
                )
        except RuntimeError:
            erros.append("rollback")
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            with pg_app.app_context():
                db.session.remove()

    def worker_b():
        try:
            _esperar(segurou)
            with pg_app.app_context():
                db.session.execute(text("SET lock_timeout = '3000ms'"))
                vistos["b"] = confirmar(
                    adquirida["id"],
                    worker_id="worker-a",
                    fencing=adquirida["fencing"],
                    agora=agora + timedelta(seconds=1),
                )
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            with pg_app.app_context():
                db.session.remove()

    threads = [threading.Thread(target=worker_a), threading.Thread(target=worker_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert erros == ["rollback"]
    assert vistos["b"]["estado"] == "concluida"
    with pg_app.app_context():
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "concluida"
        assert [item for item in _eventos(linha.id) if item[0] == "concluida"] == [("concluida", 1, "concluida")]
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json is not None


def test_cancelamento_concorrente_com_aquisicao(pg_app, monkeypatch):
    chamadas = _proibir_financeiro_e_provedor(monkeypatch)
    with pg_app.app_context():
        escopo, analise, franquia_id = _financeiro("cancela-corrida@test.com")
        criada = enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.cancela.corrida")
        antes = _foto_financeira(franquia_id)
        db.session.remove()

    dentro = threading.Event()
    liberar = threading.Event()
    erros = []
    vistos = {}

    def worker():
        try:
            with pg_app.app_context():
                def pausa():
                    dentro.set()
                    _esperar(liberar)

                vistos["adquirida"] = adquirir(worker_id="worker-a", antes_do_commit=pausa)
        except Exception as exc:
            erros.append(repr(exc))
            liberar.set()
        finally:
            with pg_app.app_context():
                db.session.remove()

    def usuario():
        try:
            _esperar(dentro)
            threading.Timer(0.5, liberar.set).start()
            with pg_app.app_context():
                db.session.execute(text("SET lock_timeout = '5000ms'"))
                inicio = time.perf_counter()
                try:
                    cancelar(escopo, criada["id"])
                except ExecucaoErro as exc:
                    vistos["cancelamento"] = exc.codigo
                vistos["esperou"] = time.perf_counter() - inicio
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            liberar.set()
            with pg_app.app_context():
                db.session.remove()

    threads = [threading.Thread(target=worker), threading.Thread(target=usuario)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(15)
    assert erros == []
    assert vistos["adquirida"]["id"] == criada["id"]
    assert vistos["cancelamento"] == "estado_invalido"
    assert vistos["esperou"] >= 0.3
    with pg_app.app_context():
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "adquirida"
        assert [item for item in _eventos(linha.id) if item[0] == "cancelada"] == []
        assert _foto_financeira(franquia_id) == antes
        _sem_vinculo_financeiro(criada["id"])
    assert chamadas == []


def test_workers_concorrentes_concluem_tudo_sem_efeito_financeiro(pg_app, monkeypatch):
    chamadas = _proibir_financeiro_e_provedor(monkeypatch)
    with pg_app.app_context():
        escopo, analise, franquia_id = _financeiro("enxame@test.com")
        ids = [enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.enxame.0")["id"]]
        for indice in range(1, 6):
            outra = _outra_analise(escopo, analise, f"analise-enxame-{indice}")
            ids.append(enfileirar(escopo, outra["id"], chave_idempotencia=f"agente-bi.enxame.{indice}")["id"])
        antes = _foto_financeira(franquia_id)
        db.session.remove()

    largada = threading.Barrier(4)
    erros = []
    concluidas = []
    guarda = threading.Lock()

    def worker(nome):
        try:
            largada.wait(8)
            with pg_app.app_context():
                while True:
                    ciclo = executar_um_ciclo(worker_id=nome)
                    if not ciclo["adquirida"]:
                        break
                    with guarda:
                        concluidas.append((nome, ciclo["execucao"]["id"], ciclo["execucao"]["estado"]))
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            with pg_app.app_context():
                db.session.remove()

    threads = [threading.Thread(target=worker, args=(f"worker-{letra}",)) for letra in "abcd"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert erros == []
    assert sorted(item[1] for item in concluidas) == sorted(ids)
    assert {item[2] for item in concluidas} == {"concluida"}
    with pg_app.app_context():
        for execucao_id in ids:
            assert [item for item in _eventos(execucao_id) if item[0] == "concluida"] == [("concluida", 1, "concluida")]
            _sem_vinculo_financeiro(execucao_id)
        assert _foto_financeira(franquia_id) == antes
        assert antes["consumo"] == Decimal("3.000000")
        assert antes["reserva"] == Decimal("2.000000")
        assert antes["tentativas"] == antes["eventos"] == antes["abatimentos"] == 0
    assert chamadas == []


def test_reinicio_falhas_incerteza_e_isolamento_sem_efeito_financeiro(pg_app, monkeypatch):
    chamadas = _proibir_financeiro_e_provedor(monkeypatch)
    with pg_app.app_context():
        escopo, analise, franquia_id = _financeiro("reinicio@test.com")
        estranho, _analise_estranha = _base("estranho-pg@test.com")
        antes = _foto_financeira(franquia_id)

        criada = enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.reinicio.um")
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        motor = db.engine
        db.session.remove()
    motor.dispose()

    with pg_app.app_context():
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "adquirida"
        assert linha.lease_worker == "worker-a"
        assert executar_um_ciclo(worker_id="worker-b", agora=agora)["adquirida"] is False
        assert recuperar_leases(agora=agora + timedelta(seconds=31)) == 1
        assert _foto_financeira(franquia_id) == antes
        retomada = executar_um_ciclo(worker_id="worker-b", agora=agora + timedelta(seconds=32))["execucao"]
        assert retomada["id"] == adquirida["id"]
        assert retomada["estado"] == "concluida"
        assert retomada["recuperacoes"] == 1
        assert _foto_financeira(franquia_id) == antes

        instavel = enfileirar(
            escopo,
            _outra_analise(escopo, analise, "analise-reinicio-falha")["id"],
            chave_idempotencia="agente-bi.reinicio.falha",
        )

        def quebra(_refs):
            raise RuntimeError("instavel")

        estados = [
            executar_um_ciclo(worker_id="worker-c", processador=quebra)["execucao"]["estado"]
            for _ in range(MAX_TENTATIVAS)
        ]
        assert estados == ["falha_recuperavel"] * (MAX_TENTATIVAS - 1) + ["falha_definitiva"]
        assert executar_um_ciclo(worker_id="worker-c")["adquirida"] is False
        assert _foto_financeira(franquia_id) == antes

        cancelavel = enfileirar(
            escopo,
            _outra_analise(escopo, analise, "analise-reinicio-cancela")["id"],
            chave_idempotencia="agente-bi.reinicio.cancela",
        )
        assert cancelar(escopo, cancelavel["id"])["estado"] == "cancelada"
        assert _foto_financeira(franquia_id) == antes

        duvidosa = enfileirar(
            escopo,
            _outra_analise(escopo, analise, "analise-reinicio-incerta")["id"],
            chave_idempotencia="agente-bi.reinicio.incerta",
        )
        tomada = adquirir(worker_id="worker-d")
        marcar_efeito_incerto(duvidosa["id"], worker_id="worker-d", fencing=tomada["fencing"])
        with pytest.raises(ExecucaoErro) as tardia:
            falhar(duvidosa["id"], worker_id="worker-d", fencing=tomada["fencing"], codigo="tarde")
        assert tardia.value.codigo == "fencing_recusado"
        db.session.remove()
    motor.dispose()

    with pg_app.app_context():
        assert executar_um_ciclo(worker_id="worker-e")["adquirida"] is False
        assert recuperar_leases(agora=utcnow_naive() + timedelta(minutes=10)) == 0
        assert db.session.get(AgenteBiExecucao, duvidosa["id"]).estado == "incerta"
        assert db.session.get(AgenteBiAnalise, db.session.get(AgenteBiExecucao, duvidosa["id"]).analise_id).resultado_json is None

        for execucao in (criada, instavel, cancelavel, duvidosa):
            with pytest.raises(ExecucaoErro) as alheia:
                obter(estranho, execucao["id"])
            assert alheia.value.codigo == "nao_encontrado"
            _sem_vinculo_financeiro(execucao["id"])

        evento_id = AgenteBiExecucaoEvento.query.filter_by(execucao_id=criada["id"]).first().id
        with pytest.raises(Exception):
            db.session.execute(
                text("UPDATE agente_bi_execucao_evento SET codigo = 'outro' WHERE id = :id"),
                {"id": evento_id},
            )
        db.session.rollback()
        with pytest.raises(Exception):
            db.session.execute(text("DELETE FROM agente_bi_execucao WHERE id = :id"), {"id": criada["id"]})
        db.session.rollback()
        assert db.session.get(AgenteBiExecucaoEvento, evento_id).codigo == "criada"
        assert _foto_financeira(franquia_id) == antes
    assert chamadas == []


def test_skip_locked_esta_no_plano(pg_app):
    with pg_app.app_context():
        agora = utcnow_naive()
        for sentenca, extra in (
            (SQL_ADQUIRIR, {}),
            (SQL_ADQUIRIR_DIRETA, {"execucao_id": 1}),
        ):
            plano = db.session.execute(
                text("EXPLAIN " + sentenca),
                {
                    "max_tentativas": 3,
                    "worker": "worker-a",
                    "expira": agora,
                    "agora": agora,
                    **extra,
                },
            ).scalars().all()
            texto = " ".join(str(linha) for linha in plano).lower()
            assert "lock" in texto


def test_coluna_de_intencao_e_aditiva(pg_app):
    with pg_app.app_context():
        escopo, analise = _base("coluna-intencao@test.com")
        criada = enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.coluna.intencao")
        db.session.execute(text("ALTER TABLE agente_bi_execucao DROP COLUMN reserva_em_andamento"))
        db.session.commit()
        db.session.execute(
            text(
                "ALTER TABLE agente_bi_execucao "
                "ADD COLUMN reserva_em_andamento BOOLEAN NOT NULL DEFAULT false"
            )
        )
        db.session.commit()
        valor = db.session.execute(
            text("SELECT reserva_em_andamento FROM agente_bi_execucao WHERE id = :id"),
            {"id": criada["id"]},
        ).scalar()
        assert valor is False


def test_dois_workers_disputam_b_sem_levar_a(pg_app):
    with pg_app.app_context():
        escopo_a, analise_a = _base("pg-dir-a@test.com")
        escopo_b, analise_b = _base("pg-dir-b@test.com")
        fila_a = enfileirar(escopo_a, analise_a["id"])
        fila_b = enfileirar(escopo_b, analise_b["id"])
        id_a = fila_a["id"]
        id_b = fila_b["id"]
        assert id_a < id_b
        assert fila_a["usuario_id"] != fila_b["usuario_id"]
        assert fila_a["conta_id"] != fila_b["conta_id"]
        db.session.remove()

    liberar = threading.Event()
    dentro = threading.Event()
    erros = []
    vistos = {}

    def worker(nome, pausar):
        try:
            with pg_app.app_context():
                def pausa():
                    if not pausar:
                        return
                    dentro.set()
                    assert liberar.wait(5)

                if not pausar:
                    assert dentro.wait(5)
                    db.session.execute(text("SET lock_timeout = '1500ms'"))
                inicio = time.perf_counter()
                vistos[nome] = adquirir(
                    worker_id=nome,
                    execucao_id=id_b,
                    antes_do_commit=pausa if pausar else None,
                )
                vistos[f"decorrido-{nome}"] = time.perf_counter() - inicio
        except Exception as exc:
            erros.append(exc)
            liberar.set()
        finally:
            if not pausar:
                liberar.set()
            with pg_app.app_context():
                db.session.remove()

    primeira = threading.Thread(target=worker, args=("worker-b1", True))
    segunda = threading.Thread(target=worker, args=("worker-b2", False))
    primeira.start()
    segunda.start()
    primeira.join(10)
    segunda.join(10)
    assert erros == []
    assert vistos["worker-b1"]["id"] == id_b
    assert vistos["worker-b2"] is None
    assert vistos["decorrido-worker-b2"] < 1.2
    with pg_app.app_context():
        linha_a = db.session.get(AgenteBiExecucao, id_a)
        linha_b = db.session.get(AgenteBiExecucao, id_b)
        assert linha_a.estado == "aguardando"
        assert linha_a.tentativas == 0
        usuario_a = linha_a.usuario_id
        assert linha_b.estado == "adquirida"
        assert linha_b.lease_worker == "worker-b1"
        assert linha_b.fencing == 1
        assert linha_b.lease_ate > utcnow_naive()
        global_a = adquirir(worker_id="worker-global")
        assert global_a["id"] == id_a
        assert global_a["usuario_id"] == usuario_a
        assert db.session.get(AgenteBiExecucao, id_b).estado == "adquirida"
        assert db.session.get(AgenteBiExecucao, id_b).lease_worker == "worker-b1"


def test_pg_recuperacao_de_efeito_liberado_nao_incrementa(pg_app):
    from tests.test_agente_bi_lease_timeout import cenario_efeito_liberado

    with pg_app.app_context():
        escopo, analise = _base("pg-liberado@test.com")
        assert cenario_efeito_liberado(escopo, analise["id"]) == {
            "estado": "incerta",
            "efeito": "incerto",
            "tentativas": 1,
            "tentativas_iniciais": 1,
            "adquiridas": 0,
            "tentativas_ia": 1,
            "documento": None,
        }
