"""SCRUM-259 no PostgreSQL descartável: concorrência da fila com transporte falso.

Não usa o banco operacional. Sem a URI local, o módulo inteiro é ignorado.

    AGENTE_BI_EXECUCAO_PG_URI=postgresql+psycopg2://pytest@127.0.0.1:55432/agente_bi_execucao_descartavel
"""
from __future__ import annotations

import json
import os
import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from app.db_operational_safety import parse_database_identity
from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiDataset,
    AgenteBiDatasetBloco,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    CleitonCostConfig,
    ConfigRegras,
    Conta,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    User,
    utcnow_naive,
)
from app.services.agente_bi_analisar_service import analisar, provedor_simulado
from app.services.agente_bi_anthropic_service import ErroTransporte, chamar_anthropic_governado
from app.services.agente_bi_execucao_service import (
    ExecucaoErro,
    adquirir,
    confirmar,
    executar_um_ciclo,
    listar_eventos,
    recuperar_leases,
)
from app.services.agente_bi_ingestao_service import ArquivoBruto, LimitesIngestao, ingerir
from app.services.agente_bi_integracao_service import (
    abrir_operacao,
    executar_operacao,
    processar_transporte_simulado,
)
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset
from app.services.agente_bi_persistencia_service import escopo_do_usuario, obter_analise
from tests.conftest import seed_cleiton_cost_config, seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario
from tests.test_agente_bi_lease_timeout import (
    _assert_sem_documento,
    _foto,
    _transporte_ok,
)
from tests.test_agente_bi_integracao import (
    _documento_referencia_inexistente,
    _documento_sugestoes_invalidas,
    _documento_trilha_forjada,
    _ids_de_dataset,
    _recusar,
    _tomar,
    _transporte_contando,
)

BANCO = "agente_bi_execucao_descartavel"
SENTINELA = "NF8842109"


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
    flask_app = __import__("flask").Flask("agente-bi-integracao-pg")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    tabelas = [
        Conta.__table__,
        Franquia.__table__,
        User.__table__,
        CleitonCostConfig.__table__,
        ConfigRegras.__table__,
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


def _limites() -> LimitesIngestao:
    return LimitesIngestao(
        max_arquivos=3,
        max_bytes_xlsx=5_000_000,
        max_bytes_csv=2_000_000,
        max_bytes_total=10_000_000,
        max_celulas=200_000,
        max_segundos=20,
        max_descompactado=40_000_000,
    )


def _base(email):
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
    escopo = escopo_do_usuario(usuario)
    ingerido = ingerir(
        escopo,
        [ArquivoBruto(nome="pg.csv", mime=None, conteudo=f"alfa,beta\n{SENTINELA},norte\n".encode("utf-8"))],
        chave="lote-pg-integracao",
        limites=_limites(),
    )
    return usuario, franquia, escopo, ingerido["dataset"]["id"]


def _transporte(estado, trava):
    def enviar(pedido):
        with trava:
            estado["chamadas"] += 1
            estado["texto"] = pedido["messages"][0]["content"]
        payload = json.loads(pedido["messages"][0]["content"])
        proposta = provedor_simulado(payload)
        return {
            "texto": json.dumps(proposta, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "stop_reason": "end_turn",
            "input_tokens": None,
            "output_tokens": None,
            "recusa": False,
            "truncada": False,
        }

    return enviar


def test_dois_workers_uma_chamada_simulada_sem_debito(pg_app):
    with pg_app.app_context():
        _usuario, franquia, escopo, dataset_id = _base("pg-integracao@test.com")
        franquia_id = franquia.id
        antes = Decimal(franquia.consumo_acumulado or 0)
        abrir_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            chave_analise="bi-pg-integracao",
        )
        db.session.remove()

    estado = {"chamadas": 0, "texto": ""}
    trava = threading.Lock()
    vistos = {}
    erros = []

    def worker(nome):
        try:
            with pg_app.app_context():
                vistos[nome] = executar_um_ciclo(
                    worker_id=nome,
                    processador=lambda refs: processar_transporte_simulado(
                        refs,
                        transporte=_transporte(estado, trava),
                    ),
                )
        except Exception as exc:
            erros.append(exc)
        finally:
            with pg_app.app_context():
                db.session.remove()

    primeiro = threading.Thread(target=worker, args=("worker-pg-a",))
    segundo = threading.Thread(target=worker, args=("worker-pg-b",))
    primeiro.start()
    segundo.start()
    primeiro.join(10)
    segundo.join(10)
    assert erros == []
    adquiridos = [item for item in vistos.values() if item.get("adquirida")]
    assert len(adquiridos) == 1
    assert adquiridos[0]["execucao"]["estado"] == "concluida"
    assert estado["chamadas"] == 1
    assert SENTINELA not in estado["texto"]
    with pg_app.app_context():
        assert AgenteBiExecucao.query.filter_by(estado="concluida").count() == 1
        assert IaConsumoAbatimento.query.count() == 0
        atual = db.session.get(Franquia, franquia_id)
        assert Decimal(atual.consumo_acumulado or 0) == antes
        repetida = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-pg-c",
            chave_analise="bi-pg-integracao",
            transporte=_transporte(estado, trava),
        )
        assert repetida["ciclo"]["adquirida"] is False
        assert estado["chamadas"] == 1
        analise = AgenteBiAnalise.query.one()
        assert json.loads(analise.resultado_json)["contrato"] == "analisar.v1"


def test_analisar_v1_persiste_em_nova_sessao(pg_app):
    with pg_app.app_context():
        usuario, _franquia, escopo, dataset_id = _base("pg-duravel@test.com")
        dataset = db.session.get(AgenteBiDataset, dataset_id)
        versao = dataset.versao
        impressao = dataset.impressao
        usuario_id = usuario.id
        perfil = perfilar_dataset(escopo, dataset_id)
        saida_fence = analisar(perfil, {"modalidades": ["A"]})
        abertura = abrir_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            chave_analise="bi-pg-fence",
        )
        tomada = adquirir(worker_id="worker-pg-fence")
        with pytest.raises(ExecucaoErro) as fence:
            confirmar(
                tomada["id"],
                worker_id="worker-pg-fence",
                fencing=tomada["fencing"] + 9,
                documento=saida_fence,
            )
        assert fence.value.codigo == "fencing_recusado"
        assert db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json is None

        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-pg-duravel",
            chave_analise="bi-pg-duravel",
            transporte=_transporte({"chamadas": 0, "texto": ""}, threading.Lock()),
        )
        saida = resultado["saida"]
        execucao_id = resultado["ciclo"]["execucao"]["id"]
        analise_id = resultado["abertura"]["analise_id"]
        fencing = resultado["ciclo"]["execucao"]["fencing"]
        assert resultado["ciclo"]["execucao"]["estado"] == "concluida"
        db.session.remove()

        vista = obter_analise(escopo, analise_id)
        assert vista["resultado"] == saida
        assert vista["resultado"]["contrato"] == "analisar.v1"
        assert vista["usuario_id"] == usuario_id
        assert vista["dataset_id"] == dataset_id
        assert vista["versao"] == versao
        assert vista["impressao"] == impressao
        assert vista["resultado"]["trilha"]["dataset_id"] == dataset_id
        assert vista["resultado"]["trilha"]["dataset_versao"] == versao
        assert [item["codigo"] for item in listar_eventos(escopo, execucao_id)].count("concluida") == 1

        de_novo = confirmar(
            execucao_id,
            worker_id="worker-pg-duravel",
            fencing=fencing,
            documento=saida,
        )
        assert de_novo["estado"] == "concluida"
        assert [item["codigo"] for item in listar_eventos(escopo, execucao_id)].count("concluida") == 1

        divergente_valido = json.loads(json.dumps(saida))
        divergente_valido["descobertas"] = []
        divergente_valido["sugestoes"] = []
        divergente_valido["perguntas"] = []
        divergente_valido["critica_referencia"] = []
        assert divergente_valido != saida
        with pytest.raises(ExecucaoErro) as preservado:
            confirmar(
                execucao_id,
                worker_id="worker-pg-duravel",
                fencing=fencing,
                documento=divergente_valido,
            )
        assert preservado.value.codigo == "historico_preservado"
        assert [item["codigo"] for item in listar_eventos(escopo, execucao_id)].count("concluida") == 1

        alterado = json.loads(json.dumps(saida))
        alterado["modalidades"] = ["Z"]
        alterado["contrato_saida_validado"] = True
        with pytest.raises(ExecucaoErro) as divergente:
            confirmar(
                execucao_id,
                worker_id="worker-pg-duravel",
                fencing=fencing,
                documento=alterado,
            )
        assert divergente.value.codigo == "contrato_saida_invalido"
        assert [item["codigo"] for item in listar_eventos(escopo, execucao_id)].count("concluida") == 1
        db.session.remove()
        preservada = obter_analise(escopo, analise_id)
        assert preservada["resultado"] == saida
        assert db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json is None


def test_confirmacao_rejeita_documento_invalido_e_trilha_forjada(pg_app):
    with pg_app.app_context():
        _usuario, _franquia, escopo, dataset_id = _base("pg-fronteira@test.com")
        pedido = {"modalidades": ["A"]}
        saida = analisar(perfilar_dataset(escopo, dataset_id), pedido)
        assert saida["contrato"] == "analisar.v1"
        assert saida["contrato_saida_validado"] is True
        assert saida["trilha"]["dataset_id"] == dataset_id
        assert saida["trilha"]["dataset_impressao"] != "forjada"
        invalido = _documento_sugestoes_invalidas(saida)
        forjada = _documento_trilha_forjada(saida)

        abertura, tomada = _tomar(escopo, dataset_id, "bi-pg-fronteira-invalido", "worker-fronteira", pedido)
        assert _recusar(escopo, tomada, abertura["analise_id"], invalido) == "contrato_saida_invalido"
        assert db.session.get(AgenteBiExecucao, tomada["id"]).estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json is None
        eventos = listar_eventos(escopo, tomada["id"])
        assert any(item["codigo"] == "adquirida" for item in eventos)
        assert all(item["codigo"] != "concluida" and item["estado_novo"] != "concluida" for item in eventos)

        gravada = confirmar(
            tomada["id"],
            worker_id="worker-fronteira",
            fencing=tomada["fencing"],
            documento=saida,
        )
        assert gravada["estado"] == "concluida"
        db.session.remove()
        assert obter_analise(escopo, abertura["analise_id"])["resultado"] == saida
        repetida = confirmar(
            tomada["id"],
            worker_id="worker-fronteira",
            fencing=tomada["fencing"],
            documento=saida,
        )
        assert repetida["estado"] == "concluida"
        assert [item["codigo"] for item in listar_eventos(escopo, tomada["id"])].count("concluida") == 1
        assert _recusar(escopo, tomada, abertura["analise_id"], invalido) == "contrato_saida_invalido"
        assert _recusar(escopo, tomada, abertura["analise_id"], forjada) == "identidade_inconsistente"
        preservada = db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json
        assert json.loads(preservada) == saida
        assert "INVALIDO" not in preservada
        assert "forjada" not in preservada
        assert [item["codigo"] for item in listar_eventos(escopo, tomada["id"])].count("concluida") == 1

        abertura_trilha, tomada_trilha = _tomar(
            escopo,
            dataset_id,
            "bi-pg-fronteira-trilha",
            "worker-fronteira",
            pedido,
        )
        assert _recusar(escopo, tomada_trilha, abertura_trilha["analise_id"], forjada) == "identidade_inconsistente"
        assert db.session.get(AgenteBiExecucao, tomada_trilha["id"]).estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, abertura_trilha["analise_id"]).resultado_json is None
        eventos_trilha = listar_eventos(escopo, tomada_trilha["id"])
        assert any(item["codigo"] == "adquirida" for item in eventos_trilha)
        assert all(item["codigo"] != "concluida" and item["estado_novo"] != "concluida" for item in eventos_trilha)


def test_confirmacao_rejeita_referencia_interna_inconsistente(pg_app):
    with pg_app.app_context():
        _usuario, _franquia, escopo, dataset_id = _base("pg-referencia@test.com")
        pedido = {"modalidades": ["A"]}
        saida = analisar(perfilar_dataset(escopo, dataset_id), pedido)
        inconsistente = _documento_referencia_inexistente(saida)
        assert set(inconsistente) == set(saida)
        abertura, tomada = _tomar(escopo, dataset_id, "bi-pg-fronteira-referencia", "worker-fronteira", pedido)
        assert _recusar(escopo, tomada, abertura["analise_id"], inconsistente) == "referencia_inexistente"
        assert db.session.get(AgenteBiExecucao, tomada["id"]).estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, abertura["analise_id"]).resultado_json is None
        eventos = listar_eventos(escopo, tomada["id"])
        assert any(item["codigo"] == "adquirida" for item in eventos)
        assert all(item["codigo"] != "concluida" and item["estado_novo"] != "concluida" for item in eventos)


def _sdk_proibido(*_args, **_kwargs):
    raise AssertionError("sdk")


def test_executar_b_nao_processa_a_no_postgres(pg_app, monkeypatch):
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.montar_cliente_anthropic",
        _sdk_proibido,
    )
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service._transporte_sdk",
        _sdk_proibido,
    )
    estado = {"chamadas": 0}
    with pg_app.app_context():
        usuario_a, franquia_a, escopo_a, dataset_a = _base("pg-iso-a@test.com")
        usuario_b, franquia_b, escopo_b, dataset_b = _base("pg-iso-b@test.com")
        assert usuario_a.id != usuario_b.id
        assert usuario_a.conta_id != usuario_b.conta_id
        antes_a = Decimal(franquia_a.consumo_acumulado or 0)
        antes_b = Decimal(franquia_b.consumo_acumulado or 0)
        pedido_a = {"modalidades": ["A"]}
        pedido_b = {"modalidades": ["C"]}
        abertura_a = abrir_operacao(escopo_a, dataset_a, pedido_a, chave_analise="bi-pg-iso-a")
        abertura_b = abrir_operacao(escopo_b, dataset_b, pedido_b, chave_analise="bi-pg-iso-b")
        id_a = abertura_a["execucao"]["id"]
        id_b = abertura_b["execucao"]["id"]
        assert id_a < id_b

        resultado = executar_operacao(
            escopo_b,
            dataset_b,
            pedido_b,
            worker_id="worker-pg-b",
            chave_analise="bi-pg-iso-b",
            transporte=_transporte_contando(estado),
        )
        saida = resultado["saida"]
        assert resultado["ciclo"]["adquirida"] is True
        assert resultado["ciclo"]["execucao"]["id"] == id_b
        assert resultado["ciclo"]["execucao"]["usuario_id"] == usuario_b.id
        assert resultado["ciclo"]["execucao"]["conta_id"] == usuario_b.conta_id
        assert resultado["abertura"]["analise_id"] == abertura_b["analise_id"]
        assert saida["contrato"] == "analisar.v1"
        assert saida["modalidades"] == ["C"]
        assert saida["trilha"]["dataset_id"] == dataset_b
        assert SENTINELA not in json.dumps(saida)
        referencias = []
        _ids_de_dataset(saida, referencias)
        assert dataset_a not in referencias
        assert estado["chamadas"] == 1
        fencing = resultado["ciclo"]["execucao"]["fencing"]
        assert fencing >= 1
        assert confirmar(
            id_b,
            worker_id="worker-pg-b",
            fencing=fencing,
            documento=saida,
        )["estado"] == "concluida"
        with pytest.raises(ExecucaoErro) as fence:
            confirmar(id_b, worker_id="worker-pg-b", fencing=fencing + 9, documento=saida)
        assert fence.value.codigo == "fencing_recusado"

        linha_a = db.session.get(AgenteBiExecucao, id_a)
        linha_b = db.session.get(AgenteBiExecucao, id_b)
        analise_a = db.session.get(AgenteBiAnalise, abertura_a["analise_id"])
        analise_b = db.session.get(AgenteBiAnalise, abertura_b["analise_id"])
        assert linha_a.estado == "aguardando"
        assert linha_a.tentativas == 0
        assert linha_a.usuario_id == usuario_a.id
        assert linha_a.ia_chamada_tentativa_id is None
        assert analise_a.resultado_json is None
        assert linha_b.estado == "concluida"
        assert linha_b.usuario_id == usuario_b.id
        assert linha_b.analise_id != linha_a.analise_id
        assert linha_b.operacao_id != linha_a.operacao_id
        assert json.loads(analise_b.resultado_json) == saida
        assert IaConsumoAbatimento.query.count() == 0
        assert Decimal(db.session.get(Franquia, franquia_a.id).consumo_acumulado or 0) == antes_a
        assert Decimal(db.session.get(Franquia, franquia_b.id).consumo_acumulado or 0) == antes_b

        repetida = executar_operacao(
            escopo_b,
            dataset_b,
            pedido_b,
            worker_id="worker-pg-b",
            chave_analise="bi-pg-iso-b",
            transporte=_transporte_contando(estado),
        )
        assert repetida["ciclo"]["adquirida"] is False
        assert repetida["saida"] == saida
        assert estado["chamadas"] == 1
        db.session.remove()
        recuperada = executar_operacao(
            escopo_b,
            dataset_b,
            pedido_b,
            worker_id="worker-pg-sessao",
            chave_analise="bi-pg-iso-b",
            transporte=_transporte_contando(estado),
        )
        assert recuperada["ciclo"]["adquirida"] is False
        assert recuperada["saida"] == saida
        assert recuperada["saida"]["trilha"]["dataset_id"] == dataset_b
        assert estado["chamadas"] == 1
        assert obter_analise(escopo_b, abertura_b["analise_id"])["resultado"] == saida
        assert obter_analise(escopo_a, abertura_a["analise_id"])["resultado"] is None
        assert db.session.get(AgenteBiExecucao, id_a).estado == "aguardando"


def test_b_em_andamento_nao_consome_a_no_postgres(pg_app, monkeypatch):
    monkeypatch.setattr(
        "app.services.agente_bi_anthropic_service.montar_cliente_anthropic",
        _sdk_proibido,
    )
    estado = {"chamadas": 0}
    with pg_app.app_context():
        usuario_a, _franquia_a, escopo_a, dataset_a = _base("pg-hold-a@test.com")
        _usuario_b, _franquia_b, escopo_b, dataset_b = _base("pg-hold-b@test.com")
        abertura_a = abrir_operacao(
            escopo_a,
            dataset_a,
            {"modalidades": ["A"]},
            chave_analise="bi-pg-hold-a",
        )
        abertura_b = abrir_operacao(
            escopo_b,
            dataset_b,
            {"modalidades": ["C"]},
            chave_analise="bi-pg-hold-b",
        )
        id_a = abertura_a["execucao"]["id"]
        id_b = abertura_b["execucao"]["id"]
        tomada = adquirir(worker_id="worker-pg-outro", execucao_id=id_b)
        assert tomada["id"] == id_b
        assert tomada["lease_worker"] == "worker-pg-outro"
        assert db.session.get(AgenteBiExecucao, id_b).lease_ate > utcnow_naive()
        assert adquirir(worker_id="worker-pg-b", execucao_id=id_b) is None
        resultado = executar_operacao(
            escopo_b,
            dataset_b,
            {"modalidades": ["C"]},
            worker_id="worker-pg-b",
            chave_analise="bi-pg-hold-b",
            transporte=_transporte_contando(estado),
        )
        assert resultado["ciclo"]["adquirida"] is False
        assert resultado["ciclo"]["em_andamento"] is True
        assert resultado["ciclo"]["execucao"]["id"] == id_b
        assert resultado["ciclo"]["execucao"]["estado"] == "adquirida"
        assert resultado["ciclo"]["execucao"]["lease_worker"] == "worker-pg-outro"
        assert resultado["saida"] is None
        assert estado["chamadas"] == 0
        assert db.session.get(AgenteBiExecucao, id_a).estado == "aguardando"
        assert db.session.get(AgenteBiExecucao, id_a).usuario_id == usuario_a.id
        assert db.session.get(AgenteBiExecucao, id_b).estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, abertura_a["analise_id"]).resultado_json is None
        assert db.session.get(AgenteBiAnalise, abertura_b["analise_id"]).resultado_json is None
        assert IaConsumoAbatimento.query.count() == 0


def _perder_no_postgres(escopo, dataset_id, chave, estado, *, ao_enviar=None):
    from tests.test_agente_bi_lease_timeout import _expirar_lease_adquirido

    return executar_operacao(
        escopo,
        dataset_id,
        {"modalidades": ["A"]},
        worker_id="worker-pg-lease",
        chave_analise=chave,
        transporte=_transporte_ok(
            estado,
            entrada=1000,
            saida=1000,
            ao_enviar=ao_enviar or _expirar_lease_adquirido,
        ),
    )


def test_pg_lease_suficiente_persiste_analisar(pg_app):
    with pg_app.app_context():
        _usuario, franquia, escopo, dataset_id = _base("pg-lease-ok@test.com")
        estado = {"chamadas": 0}
        antes = _foto(franquia.id)
        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-pg-lease-ok",
            chave_analise="bi-pg-lease-ok",
            transporte=_transporte_ok(estado),
        )
        assert resultado["ciclo"]["execucao"]["estado"] == "concluida"
        assert resultado["saida"]["contrato"] == "analisar.v1"
        assert estado["chamadas"] == 1
        assert _foto(franquia.id) == antes


def test_pg_orcamento_insuficiente_nao_cria_tentativa_nem_cobra(pg_app, monkeypatch):
    with pg_app.app_context():
        _usuario, franquia, escopo, dataset_id = _base("pg-lease-curto@test.com")
        estado = {"chamadas": 0}
        antes = _foto(franquia.id)

        def relogio_curto():
            linha = AgenteBiExecucao.query.filter_by(estado="adquirida").one()
            return linha.lease_ate - timedelta(seconds=2)

        monkeypatch.setattr("app.services.agente_bi_anthropic_service.utcnow_naive", relogio_curto)
        recusado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-pg-lease-curto",
            chave_analise="bi-pg-lease-curto",
            transporte=_transporte_ok(estado),
        )
        assert recusado["ciclo"]["execucao"]["estado"] == "falha_recuperavel"
        assert recusado["ciclo"]["execucao"]["ultimo_erro"] == "orcamento_temporal"
        assert recusado["saida"] is None
        assert estado["chamadas"] == 0
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes

        from app.models import utcnow_naive as relogio_real

        monkeypatch.setattr("app.services.agente_bi_anthropic_service.utcnow_naive", relogio_real)
        recuperado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-pg-lease-curto-2",
            chave_analise="bi-pg-lease-curto",
            transporte=_transporte_ok(estado),
        )
        assert recuperado["saida"]["contrato"] == "analisar.v1"
        assert estado["chamadas"] == 1
        assert IaChamadaTentativa.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_pg_resposta_contabilizada_sem_segunda_chamada(pg_app):
    with pg_app.app_context():
        usuario, franquia, escopo, dataset_id = _base("pg-lease-perdido@test.com")
        estado = {"chamadas": 0}
        resultado = _perder_no_postgres(escopo, dataset_id, "bi-pg-lease-perdido", estado)
        execucao_id = resultado["ciclo"]["execucao"]["id"]
        assert resultado["saida"] is None
        assert estado["chamadas"] == 1
        _assert_sem_documento(execucao_id)
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert Decimal(db.session.get(Franquia, franquia.id).consumo_acumulado) == Decimal("2")
        linha = db.session.get(AgenteBiExecucao, execucao_id)
        tentativa = db.session.get(IaChamadaTentativa, linha.ia_chamada_tentativa_id)
        assert tentativa.status == IaChamadaTentativa.STATUS_SETTLED
        extra = {"chamadas": 0}
        repetida = chamar_anthropic_governado(
            usuario_id=usuario.id,
            conta_id=usuario.conta_id,
            franquia_id=franquia.id,
            mensagem="Frete SP 10 kg",
            chave_idempotencia=linha.chave_idempotencia,
            execucao_id=linha.id,
            worker_id="worker-pg-lease-reentrada",
            fencing=int(linha.fencing),
            transporte=_transporte_ok(extra, entrada=1000, saida=1000),
            model="claude-sonnet-simulado",
            max_tokens=128,
        )
        assert repetida["provedor_chamado"] is False
        assert repetida["incerto"] is True
        assert extra["chamadas"] == 0
        for indice in range(3):
            assert recuperar_leases() == 0
            ciclo = executar_um_ciclo(
                worker_id=f"worker-pg-lease-ciclo-{indice}",
                processador=lambda refs: processar_transporte_simulado(
                    refs,
                    transporte=_transporte_ok(estado, entrada=1000, saida=1000),
                ),
            )
            assert ciclo["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert Decimal(db.session.get(Franquia, franquia.id).consumo_acumulado) == Decimal("2")
        _assert_sem_documento(execucao_id)


def test_pg_timeout_expirado_nao_repete(pg_app):
    with pg_app.app_context():
        _usuario, franquia, escopo, dataset_id = _base("pg-lease-timeout@test.com")
        estado = {"chamadas": 0}
        antes = _foto(franquia.id)

        def enviar(_pedido):
            from tests.test_agente_bi_lease_timeout import _expirar_lease_adquirido

            estado["chamadas"] += 1
            _expirar_lease_adquirido()
            raise ErroTransporte("timeout", incerto=True)

        resultado = executar_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            worker_id="worker-pg-lease-timeout",
            chave_analise="bi-pg-lease-timeout",
            transporte=enviar,
        )
        assert resultado["ciclo"]["execucao"]["estado"] == "incerta"
        assert estado["chamadas"] == 1
        _assert_sem_documento(resultado["ciclo"]["execucao"]["id"])
        segundo = executar_um_ciclo(
            worker_id="worker-pg-lease-timeout-2",
            processador=lambda refs: processar_transporte_simulado(refs, transporte=enviar),
        )
        assert segundo["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 0
        assert _foto(franquia.id) == antes


def test_pg_dois_workers_lease_expirado_durante_transporte(pg_app):
    with pg_app.app_context():
        _usuario, franquia, escopo, dataset_id = _base("pg-lease-corrida@test.com")
        franquia_id = franquia.id
        abertura = abrir_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            chave_analise="bi-pg-lease-corrida",
        )
        execucao_id = abertura["execucao"]["id"]
        db.session.remove()

    entrou = threading.Event()
    seguir = threading.Event()
    estado = {"chamadas": 0}
    vistos = {}
    erros = []

    def enviar(pedido):
        estado["chamadas"] += 1
        linha = db.session.get(AgenteBiExecucao, execucao_id)
        linha.lease_ate = utcnow_naive() - timedelta(seconds=1)
        db.session.commit()
        entrou.set()
        assert seguir.wait(10)
        payload = json.loads(pedido["messages"][0]["content"])
        texto = json.dumps(
            provedor_simulado(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return {
            "texto": texto,
            "stop_reason": "end_turn",
            "input_tokens": 1000,
            "output_tokens": 1000,
            "recusa": False,
            "truncada": False,
        }

    def worker_a():
        try:
            with pg_app.app_context():
                vistos["a"] = executar_um_ciclo(
                    worker_id="worker-pg-lease-a",
                    execucao_id=execucao_id,
                    processador=lambda refs: processar_transporte_simulado(refs, transporte=enviar),
                )
        except Exception as exc:
            erros.append(exc)
        finally:
            with pg_app.app_context():
                db.session.remove()

    def worker_b():
        try:
            assert entrou.wait(10)
            with pg_app.app_context():
                vistos["recuperadas"] = recuperar_leases()
        except Exception as exc:
            erros.append(exc)
        finally:
            seguir.set()
            with pg_app.app_context():
                db.session.remove()

    primeiro = threading.Thread(target=worker_a)
    segundo = threading.Thread(target=worker_b)
    primeiro.start()
    segundo.start()
    primeiro.join(15)
    segundo.join(15)
    assert erros == []
    assert estado["chamadas"] == 1
    assert vistos["recuperadas"] == 1
    assert vistos["a"]["execucao"]["estado"] == "incerta"
    with pg_app.app_context():
        _assert_sem_documento(execucao_id)
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert Decimal(db.session.get(Franquia, franquia_id).consumo_acumulado) == Decimal("2")
        outro = executar_um_ciclo(
            worker_id="worker-pg-lease-c",
            processador=lambda refs: processar_transporte_simulado(
                refs,
                transporte=_transporte_ok(estado, entrada=1000, saida=1000),
            ),
        )
        assert outro["adquirida"] is False
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert Decimal(db.session.get(Franquia, franquia_id).consumo_acumulado) == Decimal("2")


def test_pg_expiracao_apos_liquidacao_nao_recoloca(pg_app, monkeypatch):
    from tests.test_agente_bi_lease_timeout import (
        _foto_esperada_pos_liquidacao,
        fotografar_janela_pos_liquidacao,
    )

    with pg_app.app_context():
        usuario, franquia, escopo, dataset_id = _base("pg-janela@test.com")
        antes = _foto(franquia.id)
        foto = fotografar_janela_pos_liquidacao(
            monkeypatch,
            escopo=escopo,
            dataset_id=dataset_id,
            usuario=usuario,
            franquia_id=franquia.id,
            antes=antes,
            chave="bi-pg-janela",
            worker="worker-pg-janela",
            ceder=False,
        )
        assert foto == _foto_esperada_pos_liquidacao(ceder=False, reserva_inicial=antes[1])


def test_pg_dois_workers_apos_liquidacao_nao_refilam(pg_app, monkeypatch):
    from tests.test_agente_bi_lease_timeout import (
        _foto_esperada_pos_liquidacao,
        fotografar_janela_pos_liquidacao,
    )

    with pg_app.app_context():
        usuario, franquia, escopo, dataset_id = _base("pg-janela-2@test.com")
        antes = _foto(franquia.id)
        foto = fotografar_janela_pos_liquidacao(
            monkeypatch,
            escopo=escopo,
            dataset_id=dataset_id,
            usuario=usuario,
            franquia_id=franquia.id,
            antes=antes,
            chave="bi-pg-janela-2",
            worker="worker-pg-janela-2",
            ceder=True,
        )
        assert foto == _foto_esperada_pos_liquidacao(ceder=True, reserva_inicial=antes[1])


def test_pg_dois_workers_thread_apos_liquidacao(pg_app, monkeypatch):
    import app.services.agente_bi_execucao_service as fila

    with pg_app.app_context():
        _usuario, franquia, escopo, dataset_id = _base("pg-janela-thread@test.com")
        franquia_id = franquia.id
        abertura = abrir_operacao(
            escopo,
            dataset_id,
            {"modalidades": ["A"]},
            chave_analise="bi-pg-janela-thread",
        )
        execucao_id = abertura["execucao"]["id"]
        db.session.remove()

    entrou = threading.Event()
    seguir = threading.Event()
    estado = {"chamadas": 0}
    vistos = {}
    erros = []
    original = fila.confirmar

    def confirmar(execucao_id_arg, **kwargs):
        linha = db.session.get(AgenteBiExecucao, int(execucao_id_arg))
        db.session.refresh(linha)
        vistos["efeito"] = linha.efeito_externo
        linha.lease_ate = utcnow_naive() - timedelta(seconds=1)
        db.session.commit()
        entrou.set()
        assert seguir.wait(10)
        return original(execucao_id_arg, **kwargs)

    monkeypatch.setattr(fila, "confirmar", confirmar)

    def worker_a():
        try:
            with pg_app.app_context():
                vistos["a"] = executar_um_ciclo(
                    worker_id="worker-pg-janela-a",
                    execucao_id=execucao_id,
                    processador=lambda refs: processar_transporte_simulado(
                        refs,
                        transporte=_transporte_ok(estado, entrada=1000, saida=1000),
                    ),
                )
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            with pg_app.app_context():
                db.session.remove()

    def worker_b():
        try:
            assert entrou.wait(10)
            with pg_app.app_context():
                vistos["recuperadas"] = recuperar_leases()
        except Exception as exc:
            erros.append(repr(exc))
        finally:
            seguir.set()
            with pg_app.app_context():
                db.session.remove()

    primeiro = threading.Thread(target=worker_a)
    segundo = threading.Thread(target=worker_b)
    primeiro.start()
    segundo.start()
    primeiro.join(20)
    segundo.join(20)
    assert erros == []
    assert vistos["efeito"] == "incerto"
    assert vistos["recuperadas"] == 1
    assert vistos["a"]["execucao"]["estado"] == "incerta"
    assert estado["chamadas"] == 1
    with pg_app.app_context():
        _assert_sem_documento(execucao_id)
        assert IaChamadaTentativa.query.count() == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert Decimal(db.session.get(Franquia, franquia_id).consumo_acumulado) == Decimal("2")
        for indice in range(3):
            ciclo = executar_um_ciclo(
                worker_id=f"worker-pg-janela-depois-{indice}",
                processador=lambda refs: processar_transporte_simulado(
                    refs,
                    transporte=_transporte_ok(estado, entrada=1000, saida=1000),
                ),
            )
            assert ciclo["adquirida"] is False
        linha = db.session.get(AgenteBiExecucao, execucao_id)
        assert int(linha.tentativas) == 1
        assert estado["chamadas"] == 1
        assert IaConsumoAbatimento.query.count() == 1
