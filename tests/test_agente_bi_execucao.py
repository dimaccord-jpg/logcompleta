"""SCRUM-253: execução durável simulada do AgenteBI.

Concorrência real de PostgreSQL fica em test_agente_bi_execucao_postgres.py.
Estes testes cobrem a máquina de estados, o isolamento e a ausência de
qualquer efeito financeiro, já que a fila não chama provedor.
"""
from __future__ import annotations

import importlib
import logging
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiDashboard,
    AgenteBiDataset,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    utcnow_naive,
)
from app.run_agente_bi_worker import main, processo_habilitado
from app.services import agente_bi_execucao_service as fila
from app.services.agente_bi_execucao_service import (
    MAX_RECUPERACOES,
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
    listar_eventos,
    marcar_efeito_incerto,
    obter,
    promover_esgotadas,
    recuperar_leases,
    transicao_permitida,
)
from app.services.agente_bi_persistencia_service import (
    calcular_impressao_dataset,
    criar_analise,
    criar_dashboard,
    criar_dataset,
    escopo_do_usuario,
)
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)


def _dataset(**alteracoes):
    corpo = {
        "chave": "fretes-kips",
        "origem": "sintetica",
        "origem_ref": "sintetico-01",
        "versao": 1,
        "estrutura": {"colunas": ["uf", "valor"]},
        "perfil": {"linhas": 1},
        "dados_tipados": [{"uf": "NAOLOGAR", "valor": 10}],
        "metadados_preparacao": {"linhas": 1, "avisos": []},
    }
    corpo.update(alteracoes)
    corpo["impressao"] = calcular_impressao_dataset(
        chave=corpo["chave"],
        origem=corpo["origem"],
        origem_ref=corpo["origem_ref"],
        versao=corpo["versao"],
        estrutura=corpo["estrutura"],
        perfil=corpo["perfil"],
        dados_tipados=corpo["dados_tipados"],
        metadados_preparacao=corpo["metadados_preparacao"],
    )
    return corpo


def _analise(dataset, **alteracoes):
    corpo = {
        "chave": "analise-fretes",
        "rodada": 1,
        "dataset_id": dataset["id"],
        "versao": dataset["versao"],
        "impressao": dataset["impressao"],
        "etapa": "descoberta",
        "estado": "preparada",
        "contexto": {"recorte": "NAOLOGAR"},
        "regras_negocio": {"medida": "valor"},
        "esclarecimentos": {},
        "contrato_versao": "analisar.v1",
    }
    corpo.update(alteracoes)
    return corpo


def _dashboard(dataset, **alteracoes):
    corpo = {
        "chave": "painel-fretes",
        "revisao": 1,
        "estado_publicacao": "rascunho",
        "contrato_versao": "definir.v1",
        "executor_versao": "executor.v1",
        "definicoes_graficos": [
            {
                "ref": "g1",
                "titulo": "Frete por UF",
                "tipo": "barra",
                "definicao": {"eixo": "uf", "medida": "valor"},
            }
        ],
        "datasets": [
            {
                "dataset_id": dataset["id"],
                "versao": dataset["versao"],
                "impressao": dataset["impressao"],
            }
        ],
    }
    corpo.update(alteracoes)
    return corpo


def _par(slug, email):
    conta, franquia = seed_conta_franquia_cliente(slug=slug)
    usuario = seed_usuario(franquia.id, conta.id, email=email)
    return usuario


def _preparar(email="exec@test.com"):
    usuario = _par(f"conta-{email}", email)
    escopo = escopo_do_usuario(usuario)
    dataset = criar_dataset(escopo, _dataset())
    analise = criar_analise(escopo, _analise(dataset))
    return usuario, escopo, dataset, analise


def _seed_financeiro(email="fin-exec@test.com", limite="100", consumo="0"):
    from app.models import CleitonCostConfig, Conta

    if Conta.query.filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = Decimal(limite)
    franquia.consumo_acumulado = Decimal(consumo)
    franquia.reserva_pendente = Decimal("0")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email)
    escopo = escopo_do_usuario(usuario)
    dataset = criar_dataset(escopo, _dataset())
    analise = criar_analise(escopo, _analise(dataset))
    return usuario, franquia, escopo, analise


def _queda():
    raise RuntimeError("queda")


def _processador_instavel(_refs):
    raise RuntimeError("instavel")


def test_sql_de_aquisicao_usa_skip_locked():
    assert "FOR UPDATE SKIP LOCKED" in SQL_ADQUIRIR
    assert "FOR UPDATE SKIP LOCKED" in SQL_ADQUIRIR_DIRETA
    assert "id = :execucao_id" in SQL_ADQUIRIR_DIRETA
    assert ":execucao_id" not in SQL_ADQUIRIR
    assert "anthropic" not in SQL_ADQUIRIR.lower()
    assert "anthropic" not in SQL_ADQUIRIR_DIRETA.lower()


def test_transicoes_explicitas_e_terminais():
    assert transicao_permitida("criada", "aguardando")
    assert transicao_permitida("aguardando", "adquirida")
    assert transicao_permitida("adquirida", "concluida")
    assert transicao_permitida("adquirida", "falha_recuperavel")
    assert transicao_permitida("adquirida", "falha_definitiva")
    assert transicao_permitida("adquirida", "incerta")
    assert transicao_permitida("adquirida", "aguardando")
    assert transicao_permitida("falha_recuperavel", "adquirida")
    assert transicao_permitida("aguardando", "cancelada")
    for terminal in ("concluida", "falha_definitiva", "incerta", "cancelada"):
        assert transicao_permitida(terminal, "aguardando") is False
        assert transicao_permitida(terminal, "adquirida") is False


def test_criacao_aquisicao_e_idempotencia(app, caplog):
    with app.app_context():
        _usuario, escopo, dataset, analise = _preparar()
        painel = criar_dashboard(escopo, _dashboard(dataset))
        dataset_antes = db.session.get(AgenteBiDataset, dataset["id"]).dados_tipados_json
        painel_antes = db.session.get(AgenteBiDashboard, painel["id"]).definicoes_json
        with caplog.at_level(logging.INFO):
            primeira = enfileirar(escopo, analise["id"])
            segunda = enfileirar(escopo, analise["id"])
        assert primeira["id"] == segunda["id"]
        assert primeira["estado"] == "aguardando"
        assert AgenteBiExecucao.query.count() == 1
        assert AgenteBiAnalise.query.count() == 1
        eventos = listar_eventos(escopo, primeira["id"])
        assert [item["estado_novo"] for item in eventos] == ["criada", "aguardando"]
        adquirida = adquirir(worker_id="worker-a")
        assert adquirida["estado"] == "adquirida"
        assert adquirida["fencing"] == 1
        assert adquirida["tentativas"] == 1
        concluida = confirmar(
            adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
        )
        de_novo = confirmar(
            adquirida["id"],
            worker_id="worker-a",
            fencing=adquirida["fencing"],
        )
        assert concluida["estado"] == "concluida"
        assert de_novo["estado"] == "concluida"
        assert "desfecho_financeiro" not in de_novo
        rodada = db.session.get(AgenteBiAnalise, analise["id"])
        assert rodada.estado == "concluida"
        assert rodada.rodada == 1
        assert rodada.resultado_json is not None
        assert "execucao_simulada.v1" in rodada.resultado_json
        assert "analisar.v1" not in rodada.resultado_json
        assert "NAOLOGAR" not in rodada.resultado_json
        assert "NAOLOGAR" not in caplog.text
        assert db.session.get(AgenteBiDataset, dataset["id"]).dados_tipados_json == dataset_antes
        assert db.session.get(AgenteBiDashboard, painel["id"]).definicoes_json == painel_antes
        assert [item["codigo"] for item in listar_eventos(escopo, primeira["id"])].count("concluida") == 1
        repetida = enfileirar(escopo, analise["id"])
        assert repetida["id"] == concluida["id"]
        assert repetida["estado"] == "concluida"
        assert AgenteBiExecucao.query.count() == 1
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json == rodada.resultado_json
        with pytest.raises(ExecucaoErro) as exc:
            enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.outra.chave")
        assert exc.value.codigo == "historico_preservado"


def test_worker_obsoleto_nao_confirma_e_lease_recupera(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar()
        enfileirar(escopo, analise["id"])
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora, lease_segundos=30)
        with pytest.raises(ExecucaoErro) as expirado:
            confirmar(
                adquirida["id"],
                worker_id="worker-a",
                fencing=adquirida["fencing"],
                agora=agora + timedelta(seconds=31),
            )
        assert expirado.value.codigo == "lease_expirado"
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json is None
        recuperar_leases(agora=agora + timedelta(seconds=31))
        linha = db.session.get(AgenteBiExecucao, adquirida["id"])
        assert linha.estado == "aguardando"
        assert linha.recuperacoes == 1
        assert linha.fencing == 2
        with pytest.raises(ExecucaoErro) as obsoleto:
            confirmar(
                adquirida["id"],
                worker_id="worker-a",
                fencing=1,
                agora=agora + timedelta(seconds=32),
            )
        assert obsoleto.value.codigo == "fencing_recusado"
        ciclo = executar_um_ciclo(worker_id="worker-b", agora=agora + timedelta(seconds=32))
        assert ciclo["execucao"]["estado"] == "concluida"
        assert AgenteBiAnalise.query.count() == 1
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json.count("simulado") == 1


def test_falha_apos_aquisicao_e_durante_confirmacao(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar()
        enfileirar(escopo, analise["id"])
        with pytest.raises(RuntimeError):
            executar_um_ciclo(worker_id="worker-a", antes_de_confirmar=_queda)
        linha = AgenteBiExecucao.query.one()
        assert linha.estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json is None
        confirmar(linha.id, worker_id="worker-a", fencing=linha.fencing)
        assert db.session.get(AgenteBiExecucao, linha.id).estado == "concluida"

        _usuario2, escopo2, _dataset2, analise2 = _preparar("falha@test.com")
        enfileirar(escopo2, analise2["id"])

        def processador(_refs):
            raise RuntimeError("queda-no-processamento")

        falha = executar_um_ciclo(worker_id="worker-b", processador=processador)
        assert falha["execucao"]["estado"] == "falha_recuperavel"
        assert db.session.get(AgenteBiAnalise, analise2["id"]).resultado_json is None
        assert AgenteBiAnalise.query.filter_by(id=analise2["id"]).count() == 1


def test_reinicio_enxerga_lease_persistido(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("restart@test.com")
        enfileirar(escopo, analise["id"])
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        db.session.remove()
        linha = AgenteBiExecucao.query.one()
        assert linha.estado == "adquirida"
        assert linha.lease_worker == "worker-a"
        vazio = executar_um_ciclo(worker_id="worker-b", agora=agora)
        assert vazio["adquirida"] is False
        recuperar_leases(agora=agora + timedelta(seconds=31))
        db.session.remove()
        ciclo = executar_um_ciclo(worker_id="worker-b", agora=agora + timedelta(seconds=32))
        assert ciclo["execucao"]["estado"] == "concluida"
        assert ciclo["execucao"]["id"] == adquirida["id"]


def test_retry_permitido_ate_o_teto_e_proibido_depois(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("retry@test.com")
        enfileirar(escopo, analise["id"])

        def processador(_refs):
            raise RuntimeError("instavel")

        for _ in range(MAX_TENTATIVAS):
            executar_um_ciclo(worker_id="worker-a", processador=processador)
        linha = AgenteBiExecucao.query.one()
        assert linha.estado == "falha_definitiva"
        assert linha.tentativas == MAX_TENTATIVAS
        assert executar_um_ciclo(worker_id="worker-a", processador=processador)["adquirida"] is False
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json is None
        assert AgenteBiAnalise.query.count() == 1


def test_efeito_incerto_nao_repete_operacao(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("incerto@test.com")
        enfileirar(escopo, analise["id"])

        def processador(refs):
            atual = db.session.get(AgenteBiExecucao, refs["execucao_id"])
            marcar_efeito_incerto(atual.id, worker_id="worker-a", fencing=atual.fencing)
            raise ExecucaoErro("efeito_incerto")

        ciclo = executar_um_ciclo(worker_id="worker-a", processador=processador)
        assert ciclo["execucao"]["estado"] == "incerta"
        assert ciclo["execucao"]["efeito_externo"] == "incerto"
        assert executar_um_ciclo(worker_id="worker-b")["adquirida"] is False
        rodada = db.session.get(AgenteBiAnalise, analise["id"])
        assert rodada.estado == "incerta"
        assert rodada.resultado_json is None
        assert AgenteBiAnalise.query.count() == 1


def test_recuperacoes_esgotadas_nao_recolocam_na_fila(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("recup@test.com")
        enfileirar(escopo, analise["id"])
        agora = utcnow_naive()
        for indice in range(MAX_RECUPERACOES):
            adquirir(worker_id="worker-a", agora=agora + timedelta(seconds=indice * 40))
            recuperar_leases(agora=agora + timedelta(seconds=indice * 40 + 31))
        linha = AgenteBiExecucao.query.one()
        assert linha.estado == "falha_definitiva"
        assert linha.recuperacoes == MAX_RECUPERACOES
        assert executar_um_ciclo(worker_id="worker-b")["adquirida"] is False


def test_terceira_tentativa_interrompida_encerra_definitivamente(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("p1-teto@test.com")
        criada = enfileirar(escopo, analise["id"])
        for _ in range(MAX_TENTATIVAS - 1):
            ciclo = executar_um_ciclo(worker_id="worker-a", processador=_processador_instavel)
            assert ciclo["execucao"]["estado"] == "falha_recuperavel"
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        assert adquirida["id"] == criada["id"]
        assert adquirida["tentativas"] == MAX_TENTATIVAS
        assert recuperar_leases(agora=agora + timedelta(seconds=31)) == 1
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "falha_definitiva"
        assert linha.tentativas == MAX_TENTATIVAS
        assert listar_eventos(escopo, criada["id"])[-1]["codigo"] == "tentativas_esgotadas"
        assert db.session.get(AgenteBiAnalise, analise["id"]).estado == "falha_validacao"
        assert executar_um_ciclo(worker_id="worker-b")["adquirida"] is False
        assert adquirir(worker_id="worker-c") is None
        assert IaConsumoEvento.query.count() == 0


@pytest.mark.parametrize(
    "passos",
    [
        ("queda", "falha", "queda"),
        ("falha", "queda", "queda"),
    ],
)
def test_sequencias_mistas_nao_deixam_aguardando_permanente(app, passos):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar(f"{'-'.join(passos)}@test.com")
        criada = enfileirar(escopo, analise["id"])
        agora = utcnow_naive()
        for indice, passo in enumerate(passos):
            momento = agora + timedelta(seconds=indice * 40)
            if passo == "falha":
                ciclo = executar_um_ciclo(
                    worker_id="worker-a",
                    agora=momento,
                    processador=_processador_instavel,
                )
                assert ciclo["execucao"]["estado"] in {"falha_recuperavel", "falha_definitiva"}
            else:
                reivindicada = adquirir(worker_id="worker-a", agora=momento)
                assert reivindicada is not None
                assert recuperar_leases(agora=momento + timedelta(seconds=31)) == 1
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "falha_definitiva"
        assert linha.estado != "aguardando"
        assert linha.tentativas == MAX_TENTATIVAS
        assert listar_eventos(escopo, criada["id"])[-1]["codigo"] == "tentativas_esgotadas"
        assert db.session.get(AgenteBiAnalise, analise["id"]).estado == "falha_validacao"
        assert executar_um_ciclo(worker_id="worker-b")["adquirida"] is False
        assert adquirir(worker_id="worker-c") is None


def test_recuperacao_com_tentativas_disponiveis_recoloca_na_fila(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("recup-ok@test.com")
        criada = enfileirar(escopo, analise["id"])
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        assert adquirida["tentativas"] == 1
        assert recuperar_leases(agora=agora + timedelta(seconds=31)) == 1
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "aguardando"
        assert linha.tentativas == 1
        assert listar_eventos(escopo, criada["id"])[-1]["codigo"] == "lease_recuperado"
        ciclo = executar_um_ciclo(worker_id="worker-b", agora=agora + timedelta(seconds=32))
        assert ciclo["execucao"]["id"] == criada["id"]
        assert ciclo["execucao"]["estado"] == "concluida"


def test_cancelamento_so_antes_da_aquisicao(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("cancela@test.com")
        criada = enfileirar(escopo, analise["id"])
        cancelada = cancelar(escopo, criada["id"])
        assert cancelada["estado"] == "cancelada"
        assert db.session.get(AgenteBiAnalise, analise["id"]).estado == "preparada"
        repetida = cancelar(escopo, criada["id"])
        assert repetida["estado"] == "cancelada"
        _usuario2, escopo2, _dataset2, analise2 = _preparar("cancela-2@test.com")
        criada2 = enfileirar(escopo2, analise2["id"])
        adquirir(worker_id="worker-a")
        with pytest.raises(ExecucaoErro) as exc:
            cancelar(escopo2, criada2["id"])
        assert exc.value.codigo == "estado_invalido"


def test_isolamento_entre_usuarios(app):
    with app.app_context():
        _um, escopo_um, _dataset_um, analise_um = _preparar("um@test.com")
        _dois, escopo_dois, _dataset_dois, _analise_dois = _preparar("dois@test.com")
        criada = enfileirar(escopo_um, analise_um["id"])
        with pytest.raises(ExecucaoErro) as leitura:
            obter(escopo_dois, criada["id"])
        assert leitura.value.codigo == "nao_encontrado"
        with pytest.raises(ExecucaoErro) as fila:
            enfileirar(escopo_dois, analise_um["id"])
        assert fila.value.codigo == "nao_encontrado"
        assert AgenteBiExecucao.query.filter_by(usuario_id=escopo_dois.usuario_id).count() == 0
        with pytest.raises(ExecucaoErro) as eventos:
            listar_eventos(escopo_dois, criada["id"])
        assert eventos.value.codigo == "nao_encontrado"


def test_diario_e_imutable_e_historico_da_analise_permanece(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("diario@test.com")
        criada = enfileirar(escopo, analise["id"])
        evento_id = AgenteBiExecucaoEvento.query.filter_by(codigo="criada").one().id
        with pytest.raises((IntegrityError, Exception)):
            db.session.execute(
                text("UPDATE agente_bi_execucao_evento SET codigo = 'outro' WHERE id = :id"),
                {"id": evento_id},
            )
            db.session.commit()
        db.session.rollback()
        assert db.session.get(AgenteBiExecucaoEvento, evento_id).codigo == "criada"
        executar_um_ciclo(worker_id="worker-a")
        rodada = db.session.get(AgenteBiAnalise, analise["id"])
        original = rodada.resultado_json
        with pytest.raises(Exception):
            db.session.execute(
                text("UPDATE agente_bi_analise SET resultado_json = :valor WHERE id = :id"),
                {"id": rodada.id, "valor": '{"codigo":"substituido"}'},
            )
            db.session.commit()
        db.session.rollback()
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json == original
        assert criada["analise_id"] == analise["id"]


def test_worker_nao_sobe_sem_flag_e_nao_chama_ia(app, monkeypatch):
    assert processo_habilitado() is False
    monkeypatch.delenv("AGENTE_BI_WORKER_HABILITADO", raising=False)

    def proibido(*_args, **_kwargs):
        raise AssertionError("chamada de IA")

    monkeypatch.setattr(
        "app.services.cleiton_billable_ai_call.cleiton_governed_billable_ai_call",
        proibido,
    )
    monkeypatch.setattr(
        "app.services.cleiton_billable_ai_call.cleiton_governed_generate_content",
        proibido,
    )
    assert main(["--once"]) == 2
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("sem-ia@test.com")
        enfileirar(escopo, analise["id"])
        executar_um_ciclo(worker_id="worker-a")
        assert IaConsumoEvento.query.count() == 0
        assert AgenteBiExecucao.query.one().codigo_resultado == "simulado"


_ALVOS_PROIBIDOS = (
    "app.services.cleiton_billable_ai_call._reservar",
    "app.services.cleiton_billable_ai_call.liberar_reserva_confirmada",
    "app.services.cleiton_billable_ai_call.liquidar_tentativa_ia",
    "app.services.cleiton_billable_ai_call.cleiton_governed_billable_ai_call",
    "app.services.cleiton_billable_ai_call.cleiton_governed_generate_content",
    "app.run_cleiton_gemini_governance.cleiton_governed_generate_content",
    "app.run_cleiton_gemini_governance.cleiton_governed_generate_content_observed",
    "app.services.cleiton_franquia_operacional_service.aplicar_motor_apos_ia_consumo_evento",
    "app.services.cleiton_franquia_operacional_service.lancar_creditos_convertidos",
    "app.services.cleiton_franquia_operacional_service.decidir_admissao_chamada_ia",
)


def _proibir_financeiro_e_provedor(monkeypatch) -> list[str]:
    chamadas: list[str] = []

    def _proibido(nome):
        def _chamada(*_args, **_kwargs):
            chamadas.append(nome)
            raise AssertionError(f"efeito financeiro ou provedor indevido: {nome}")

        return _chamada

    for alvo in _ALVOS_PROIBIDOS:
        modulo, nome = alvo.rsplit(".", 1)
        if hasattr(importlib.import_module(modulo), nome):
            monkeypatch.setattr(alvo, _proibido(nome))
    return chamadas


def _foto_financeira(franquia_id: int) -> dict:
    franquia = db.session.get(Franquia, franquia_id)
    db.session.refresh(franquia)
    return {
        "tentativas": IaChamadaTentativa.query.count(),
        "eventos": IaConsumoEvento.query.count(),
        "abatimentos": IaConsumoAbatimento.query.count(),
        "consumo": franquia.consumo_acumulado,
        "reserva": franquia.reserva_pendente,
    }


def _sem_vinculo_financeiro(execucao_id: int) -> None:
    linha = db.session.get(AgenteBiExecucao, execucao_id)
    db.session.refresh(linha)
    assert linha.ia_chamada_tentativa_id is None
    assert linha.desfecho_financeiro is None
    assert linha.financeiro_encerrado is False
    assert linha.reserva_em_andamento is False
    assert db.session.get(AgenteBiAnalise, linha.analise_id).ia_chamada_tentativa_id is None


def _outra_analise(escopo, analise, chave):
    dataset = {"id": analise["dataset_id"], "versao": analise["versao"], "impressao": analise["impressao"]}
    return criar_analise(escopo, _analise(dataset, chave=chave))


def test_ciclo_de_vida_completo_sem_efeito_financeiro(app, monkeypatch):
    chamadas = _proibir_financeiro_e_provedor(monkeypatch)
    with app.app_context():
        _usuario, franquia, escopo, analise = _seed_financeiro(email="ciclo-fin@test.com", consumo="3")
        franquia.reserva_pendente = Decimal("2.000000")
        db.session.commit()
        antes = _foto_financeira(franquia.id)
        assert antes["consumo"] == Decimal("3.000000")
        assert antes["reserva"] == Decimal("2.000000")

        criada = enfileirar(escopo, analise["id"])
        assert _foto_financeira(franquia.id) == antes
        concluida = executar_um_ciclo(worker_id="worker-a")["execucao"]
        assert concluida["estado"] == "concluida"
        assert _foto_financeira(franquia.id) == antes

        cancelavel = enfileirar(escopo, _outra_analise(escopo, analise, "analise-cancela")["id"])
        assert cancelar(escopo, cancelavel["id"])["estado"] == "cancelada"
        assert _foto_financeira(franquia.id) == antes

        instavel = enfileirar(escopo, _outra_analise(escopo, analise, "analise-instavel")["id"])

        def quebra(_refs):
            raise RuntimeError("instavel")

        primeira = executar_um_ciclo(worker_id="worker-a", processador=quebra)["execucao"]
        assert primeira["estado"] == "falha_recuperavel"
        assert _foto_financeira(franquia.id) == antes
        for _ in range(MAX_TENTATIVAS - 1):
            executar_um_ciclo(worker_id="worker-a", processador=quebra)
        assert db.session.get(AgenteBiExecucao, instavel["id"]).estado == "falha_definitiva"
        assert _foto_financeira(franquia.id) == antes

        recuperavel = enfileirar(escopo, _outra_analise(escopo, analise, "analise-lease")["id"])
        agora = utcnow_naive()
        adquirida = adquirir(worker_id="worker-a", agora=agora)
        assert adquirida["id"] == recuperavel["id"]
        assert recuperar_leases(agora=agora + timedelta(seconds=31)) == 1
        assert _foto_financeira(franquia.id) == antes
        retomada = executar_um_ciclo(worker_id="worker-b", agora=agora + timedelta(seconds=32))["execucao"]
        assert retomada["id"] == recuperavel["id"]
        assert retomada["estado"] == "concluida"
        assert _foto_financeira(franquia.id) == antes

        duvidosa = enfileirar(escopo, _outra_analise(escopo, analise, "analise-incerta")["id"])
        tomada = adquirir(worker_id="worker-a")
        marcar_efeito_incerto(duvidosa["id"], worker_id="worker-a", fencing=tomada["fencing"])
        assert promover_esgotadas() == 0
        assert executar_um_ciclo(worker_id="worker-b")["adquirida"] is False
        assert db.session.get(AgenteBiExecucao, duvidosa["id"]).estado == "incerta"
        assert _foto_financeira(franquia.id) == antes

        for execucao in (criada, cancelavel, instavel, recuperavel, duvidosa):
            _sem_vinculo_financeiro(execucao["id"])
            assert set(obter(escopo, execucao["id"])).isdisjoint(
                {"desfecho_financeiro", "financeiro_encerrado", "reserva_em_andamento", "ia_chamada_tentativa_id"}
            )
    assert chamadas == []


def test_erros_e_rollback_sem_efeito_financeiro(app, monkeypatch):
    chamadas = _proibir_financeiro_e_provedor(monkeypatch)
    with app.app_context():
        _usuario, franquia, escopo, analise = _seed_financeiro(email="erro-fin@test.com", consumo="1")
        antes = _foto_financeira(franquia.id)
        criada = enfileirar(escopo, analise["id"])

        with pytest.raises(RuntimeError):
            executar_um_ciclo(worker_id="worker-a", antes_de_confirmar=_queda)
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        assert linha.estado == "adquirida"
        assert db.session.get(AgenteBiAnalise, analise["id"]).resultado_json is None
        assert _foto_financeira(franquia.id) == antes

        with pytest.raises(ExecucaoErro) as obsoleto:
            confirmar(linha.id, worker_id="worker-a", fencing=linha.fencing - 1)
        assert obsoleto.value.codigo == "fencing_recusado"
        with pytest.raises(ExecucaoErro) as tarde:
            cancelar(escopo, linha.id)
        assert tarde.value.codigo == "estado_invalido"
        assert _foto_financeira(franquia.id) == antes

        definitiva = falhar(
            linha.id,
            worker_id="worker-a",
            fencing=linha.fencing,
            codigo="identidade_inconsistente",
            recuperavel=False,
        )
        assert definitiva["estado"] == "falha_definitiva"
        assert _foto_financeira(franquia.id) == antes

        _outro, _escopo_outro, _dataset_outro, analise_outra = _preparar("estranho-fin@test.com")
        with pytest.raises(ExecucaoErro) as alheia:
            enfileirar(escopo, analise_outra["id"])
        assert alheia.value.codigo == "nao_encontrado"
        assert _foto_financeira(franquia.id) == antes
        _sem_vinculo_financeiro(criada["id"])
    assert chamadas == []


def test_fila_nao_tem_responsabilidade_financeira():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.services.agente_bi_execucao_financeira")
    for nome in (
        "reservar_simulada",
        "fechar_pendencias_financeiras",
        "fechar_desfecho_simulado",
        "liquidar_vinculada_uma_vez",
        "vincular_tentativa_confirmada",
        "marcacao_financeira",
        "classificar_tentativa",
    ):
        assert not hasattr(fila, nome), nome
    fonte = Path(fila.__file__).read_text(encoding="utf-8")
    worker = Path("app/run_agente_bi_worker.py").read_text(encoding="utf-8")
    for proibido in (
        "cleiton_billable_ai_call",
        "_reservar",
        "pg_advisory",
        "IaChamadaTentativa",
        "IaConsumoEvento(",
        "reserva_pendente",
        "consumo_acumulado",
        "reserva_em_andamento",
        "desfecho_financeiro",
        "financeiro_encerrado",
        "ia_chamada_tentativa_id",
    ):
        assert proibido not in fonte, proibido
        assert proibido not in worker, proibido


def test_colunas_legadas_nao_interferem_na_fila(app, monkeypatch):
    chamadas = _proibir_financeiro_e_provedor(monkeypatch)
    with app.app_context():
        _usuario, franquia, escopo, analise = _seed_financeiro(email="legado-fin@test.com")
        criada = enfileirar(escopo, analise["id"])
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        linha.reserva_em_andamento = True
        linha.desfecho_financeiro = "reservado"
        db.session.commit()
        antes = _foto_financeira(franquia.id)
        assert executar_um_ciclo(worker_id="worker-a")["execucao"]["estado"] == "concluida"
        linha = db.session.get(AgenteBiExecucao, criada["id"])
        db.session.refresh(linha)
        assert linha.reserva_em_andamento is True
        assert linha.desfecho_financeiro == "reservado"
        assert linha.financeiro_encerrado is False
        assert _foto_financeira(franquia.id) == antes
    assert chamadas == []


def test_falha_direta_recusada_sem_lease(app):
    with app.app_context():
        _usuario, escopo, _dataset_criado, analise = _preparar("sem-lease@test.com")
        criada = enfileirar(escopo, analise["id"])
        with pytest.raises(ExecucaoErro) as exc:
            falhar(criada["id"], worker_id="worker-a", fencing=0, codigo="nao_adquirida")
        assert exc.value.codigo == "fencing_recusado"


def test_migration_e_head_da_fila():
    cfg = Config()
    cfg.set_main_option("script_location", "migrations")
    script = ScriptDirectory.from_config(cfg)
    revisao = script.get_revision("l0m1n2o3p4q5")
    assert revisao is not None
    assert revisao.down_revision == "k9l0m1n2o3p4"
    intencao = script.get_revision("m1n2o3p4q5r6")
    assert intencao is not None
    assert intencao.down_revision == "l0m1n2o3p4q5"
    assert script.get_heads() == ["o3p4q5r6s7t8"]
    fonte = (
        __import__("pathlib").Path("migrations/versions/l0m1n2o3p4q5_agente_bi_execucao_duravel.py")
        .read_text(encoding="utf-8")
    )
    assert "op.drop_table" not in fonte.split("def downgrade")[0]
    assert "ia_chamada_tentativa" in fonte
    assert "dados_tipados" not in fonte


def test_migration_sqlite_e_reversivel():
    from importlib.util import module_from_spec, spec_from_file_location

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    spec = spec_from_file_location(
        "mig_execucao",
        "migrations/versions/l0m1n2o3p4q5_agente_bi_execucao_duravel.py",
    )
    mig = module_from_spec(spec)
    spec.loader.exec_module(mig)
    engine = create_engine("sqlite://")
    AgenteBiDataset.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE agente_bi_execucao_evento"))
        conn.execute(text("DROP TABLE agente_bi_execucao"))

    def _run(fn):
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"render_as_batch": True})
            ops = Operations(ctx)
            original = mig.op
            try:
                mig.op = ops
                with conn.begin():
                    fn()
            finally:
                mig.op = original

    _run(mig.upgrade)
    assert "agente_bi_execucao" in inspect(engine).get_table_names()
    assert "agente_bi_execucao_evento" in inspect(engine).get_table_names()
    _run(mig.downgrade)
    nomes = set(inspect(engine).get_table_names())
    assert "agente_bi_execucao" not in nomes
    assert "agente_bi_execucao_evento" not in nomes
    assert "agente_bi_analise" in nomes


def test_aquisicao_direcionada_nao_substitui_e_preserva_a_global(app):
    with app.app_context():
        usuario_a, escopo_a, _dataset_a, analise_a = _preparar("dir-a@test.com")
        usuario_b, escopo_b, _dataset_b, analise_b = _preparar("dir-b@test.com")
        fila_a = enfileirar(escopo_a, analise_a["id"])
        fila_b = enfileirar(escopo_b, analise_b["id"])
        assert fila_a["id"] < fila_b["id"]
        assert fila_a["usuario_id"] == usuario_a.id
        assert fila_b["usuario_id"] == usuario_b.id
        assert fila_a["conta_id"] != fila_b["conta_id"]

        tomada = adquirir(worker_id="worker-b", execucao_id=fila_b["id"])
        assert tomada["id"] == fila_b["id"]
        assert tomada["fencing"] == 1
        assert tomada["lease_worker"] == "worker-b"
        assert tomada["usuario_id"] == usuario_b.id
        linha_b = db.session.get(AgenteBiExecucao, fila_b["id"])
        assert linha_b.estado == "adquirida"
        assert linha_b.lease_ate > utcnow_naive()
        assert db.session.get(AgenteBiExecucao, fila_a["id"]).estado == "aguardando"
        assert db.session.get(AgenteBiExecucao, fila_a["id"]).tentativas == 0
        assert adquirir(worker_id="worker-c", execucao_id=fila_b["id"]) is None
        assert db.session.get(AgenteBiExecucao, fila_b["id"]).lease_worker == "worker-b"
        assert db.session.get(AgenteBiExecucao, fila_a["id"]).estado == "aguardando"

        concluida = confirmar(fila_b["id"], worker_id="worker-b", fencing=tomada["fencing"])
        assert concluida["estado"] == "concluida"
        assert concluida["id"] == fila_b["id"]
        with pytest.raises(ExecucaoErro) as exc:
            confirmar(fila_b["id"], worker_id="worker-b", fencing=tomada["fencing"] + 9)
        assert exc.value.codigo == "fencing_recusado"
        assert db.session.get(AgenteBiExecucao, fila_b["id"]).estado == "concluida"
        assert db.session.get(AgenteBiAnalise, analise_a["id"]).resultado_json is None

        vistos = []

        def processador(refs):
            vistos.append(int(refs["execucao_id"]))
            return "simulado"

        repetido = executar_um_ciclo(
            worker_id="worker-b",
            processador=processador,
            execucao_id=fila_b["id"],
        )
        assert repetido["adquirida"] is False
        assert vistos == []
        assert db.session.get(AgenteBiExecucao, fila_a["id"]).estado == "aguardando"

        global_a = adquirir(worker_id="worker-a")
        assert global_a["id"] == fila_a["id"]
        assert global_a["usuario_id"] == usuario_a.id
        assert global_a["fencing"] == 1
        assert db.session.get(AgenteBiExecucao, fila_b["id"]).estado == "concluida"


def test_ciclo_direcionado_processa_so_a_execucao_pedida(app):
    with app.app_context():
        _usuario_a, escopo_a, _dataset_a, analise_a = _preparar("ciclo-a@test.com")
        usuario_b, escopo_b, _dataset_b, analise_b = _preparar("ciclo-b@test.com")
        fila_a = enfileirar(escopo_a, analise_a["id"])
        fila_b = enfileirar(escopo_b, analise_b["id"])
        vistos = []

        def processador(refs):
            vistos.append(int(refs["execucao_id"]))
            linha = db.session.get(AgenteBiExecucao, refs["execucao_id"])
            assert linha.lease_worker == "worker-b"
            assert linha.lease_ate > utcnow_naive()
            assert linha.fencing >= 1
            assert linha.usuario_id == usuario_b.id
            return "simulado"

        ciclo = executar_um_ciclo(
            worker_id="worker-b",
            processador=processador,
            execucao_id=fila_b["id"],
        )
        assert vistos == [fila_b["id"]]
        assert ciclo["adquirida"] is True
        assert ciclo["execucao"]["id"] == fila_b["id"]
        assert ciclo["execucao"]["estado"] == "concluida"
        assert ciclo["execucao"]["usuario_id"] == usuario_b.id
        linha_a = db.session.get(AgenteBiExecucao, fila_a["id"])
        assert linha_a.estado == "aguardando"
        assert linha_a.tentativas == 0
        assert db.session.get(AgenteBiAnalise, analise_a["id"]).resultado_json is None
        assert "execucao_simulada.v1" in db.session.get(AgenteBiAnalise, analise_b["id"]).resultado_json
        repetido = executar_um_ciclo(
            worker_id="worker-c",
            processador=processador,
            execucao_id=fila_b["id"],
        )
        assert repetido["adquirida"] is False
        assert vistos == [fila_b["id"]]
        assert db.session.get(AgenteBiExecucao, fila_a["id"]).estado == "aguardando"
