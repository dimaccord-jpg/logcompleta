"""Falhas pré-chamada e recuperação excepcional, com transporte falso.

Não abre SDK, não chama a execução operacional 6 e não grava fora do
banco descartável do teste.
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
)
from app.services.agente_bi_colaboracao_service import (
    _avancar,
    _configuracao_operacional_valida,
    executar_descoberta,
    recuperar_falha_pre_chamada,
)
from app.services.agente_bi_execucao_service import (
    TRANSICOES,
    ExecucaoErro,
    executar_um_ciclo,
    falhar,
    transicao_permitida,
)
from tests.test_agente_bi_colaboracao import (
    _Claude,
    _Gemini,
    _condicional,
    _critica,
    _descobrir,
    _pedido,
    _preparar,
    _tecnicas,
)


ROOT = Path(__file__).resolve().parents[1]


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


def _contexto(analise) -> dict:
    valor = analise.contexto_json
    if isinstance(valor, dict):
        return valor
    return json.loads(valor)


def _contagens(usuario_id: int) -> dict:
    tentativas = IaChamadaTentativa.query.filter_by(usuario_id=usuario_id).all()
    return {
        "anthropic": sorted(item.id for item in tentativas if item.provider == "anthropic"),
        "gemini": sorted(item.id for item in tentativas if item.provider == "gemini"),
        "eventos": IaConsumoEvento.query.filter_by(usuario_id=usuario_id).count(),
        "abatimentos": IaConsumoAbatimento.query.count(),
    }


def _foto(escopo, execucao, analise) -> dict:
    db.session.refresh(execucao)
    db.session.refresh(analise)
    eventos = (
        AgenteBiExecucaoEvento.query.filter_by(execucao_id=execucao.id)
        .order_by(AgenteBiExecucaoEvento.id.asc())
        .all()
    )
    contexto = _contexto(analise)
    return {
        "estado": execucao.estado,
        "erro": execucao.ultimo_erro,
        "efeito": execucao.efeito_externo,
        "tentativas": int(execucao.tentativas),
        "recuperacoes": int(execucao.recuperacoes),
        "fencing": int(execucao.fencing),
        "operacao": execucao.operacao_id,
        "chave": execucao.chave_idempotencia,
        "analise_id": int(execucao.analise_id),
        "tentativa_id": execucao.ia_chamada_tentativa_id,
        "lease_worker": execucao.lease_worker,
        "lease_ate": execucao.lease_ate,
        "usuario_id": int(execucao.usuario_id),
        "conta_id": int(execucao.conta_id),
        "franquia_id": int(execucao.franquia_id),
        "analise_estado": analise.estado,
        "resultado": analise.resultado_json,
        "contexto": analise.contexto_json,
        "chave_analise": analise.chave_recuperacao,
        "pai": contexto.get("pai_id"),
        "pai_hash": contexto.get("pai_hash"),
        "eventos": [
            (evento.id, evento.estado_anterior, evento.estado_novo, int(evento.fencing), evento.codigo)
            for evento in eventos
        ],
        "updated_at": execucao.updated_at,
        "analise_updated_at": analise.updated_at,
        "consumo": _contagens(escopo.usuario_id),
    }


def _recarregar(execucao_id: int):
    execucao = db.session.get(AgenteBiExecucao, int(execucao_id))
    db.session.refresh(execucao)
    analise = db.session.get(AgenteBiAnalise, int(execucao.analise_id))
    db.session.refresh(analise)
    return execucao, analise


def _cravar(codigo: str):
    def _acao():
        linha = AgenteBiExecucao.query.filter_by(estado="adquirida").one()
        falhar(
            int(linha.id),
            worker_id=linha.lease_worker,
            fencing=int(linha.fencing),
            codigo=codigo,
            recuperavel=False,
        )

    return _acao


def _rodada_gemini(monkeypatch, email, *, codigo="modelo_ausente", definitiva=False, cliente_ausente=False):
    usuario, _franquia, escopo, dataset_id, perfil = _preparar(email)
    pedido = _pedido(perfil, ["A"])
    claude = _Claude([_condicional(perfil), _condicional(perfil)])
    gemini = _Gemini(json.dumps(_critica(perfil)))
    _descobrir(escopo, dataset_id, pedido, claude, gemini)
    assert claude.chamadas == 1
    assert len(gemini.geracoes) == 0
    controle = {"antes_da_chamada": _cravar(codigo)} if definitiva else None
    if codigo == "modelo_ausente":
        monkeypatch.delenv("AGENTE_BI_GEMINI_MODEL", raising=False)
    if cliente_ausente or codigo == "credencial_ausente":
        monkeypatch.setattr("app.services.agente_bi_gemini_service.montar_cliente_gemini", lambda: None)
        cliente = None
    else:
        cliente = gemini
    _descobrir(escopo, dataset_id, pedido, claude, cliente, controle)
    tecnicas = _tecnicas(dataset_id)
    assert len(tecnicas) == 2
    assert _contexto(tecnicas[1]).get("papel") == "gemini"
    execucao = AgenteBiExecucao.query.filter_by(analise_id=tecnicas[1].id).one()
    return {
        "usuario": usuario,
        "escopo": escopo,
        "dataset_id": dataset_id,
        "perfil": perfil,
        "pedido": pedido,
        "claude": claude,
        "gemini": gemini,
        "execucao_id": int(execucao.id),
        "claude_id": int(AgenteBiExecucao.query.filter_by(analise_id=tecnicas[0].id).one().id),
    }


def _assert_pre_chamada(caso, codigo, estado):
    execucao, analise = _recarregar(caso["execucao_id"])
    assert execucao.estado == estado
    assert execucao.ultimo_erro == codigo
    assert execucao.efeito_externo == "nenhum"
    assert execucao.ia_chamada_tentativa_id is None
    assert execucao.lease_worker is None
    assert execucao.lease_ate is None
    assert analise.resultado_json is None
    assert caso["claude"].chamadas == 1
    assert len(caso["gemini"].geracoes) == 0
    assert _contagens(caso["usuario"].id)["gemini"] == []
    assert IaChamadaTentativa.query.filter_by(attempt_key=execucao.chave_idempotencia).count() == 0
    return execucao, analise


def _recusar(caso, codigo):
    execucao, analise = _recarregar(caso["execucao_id"])
    antes = _foto(caso["escopo"], execucao, analise)
    with pytest.raises(ExecucaoErro) as erro:
        recuperar_falha_pre_chamada(caso["escopo"], caso["execucao_id"])
    assert erro.value.codigo == codigo
    execucao, analise = _recarregar(caso["execucao_id"])
    assert _foto(caso["escopo"], execucao, analise) == antes
    return antes


def test_modelo_ausente_antes_da_chamada_fica_recuperavel(app, monkeypatch):
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-modelo@test.com")
        execucao, analise = _assert_pre_chamada(caso, "modelo_ausente", "falha_recuperavel")
        assert int(execucao.tentativas) == 1
        assert int(execucao.recuperacoes) == 0
        assert int(execucao.fencing) == 1
        assert analise.estado == "em_execucao"


def test_credencial_ausente_antes_da_chamada_fica_recuperavel(app, monkeypatch):
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-credencial@test.com", codigo="credencial_ausente")
        _assert_pre_chamada(caso, "credencial_ausente", "falha_recuperavel")


def test_configuracao_corrigida_chama_gemini_uma_vez_sem_repetir_claude(app, monkeypatch):
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-corrigida@test.com")
        _assert_pre_chamada(caso, "modelo_ausente", "falha_recuperavel")
        antes = _contagens(caso["usuario"].id)
        analise_id = _recarregar(caso["execucao_id"])[1].id
        monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
        corpo = _descobrir(caso["escopo"], caso["dataset_id"], caso["pedido"], caso["claude"], caso["gemini"])
        assert caso["claude"].chamadas == 1
        assert len(caso["gemini"].geracoes) == 1
        assert corpo["colaboracao"]["continuar"] is True
        assert corpo["colaboracao"]["fase"] == "claude_consolidando"
        execucao, analise = _recarregar(caso["execucao_id"])
        assert analise.id == analise_id
        assert execucao.estado == "concluida"
        assert int(execucao.tentativas) == 2
        depois = _contagens(caso["usuario"].id)
        assert depois["anthropic"] == antes["anthropic"]
        assert len(depois["gemini"]) == 1
        assert depois["eventos"] == antes["eventos"] + 1
        assert depois["abatimentos"] == antes["abatimentos"] + 1


def test_outras_falhas_permanecem_fora_da_recuperacao(app, monkeypatch):
    with app.app_context():
        usuario, franquia, escopo, dataset_id, perfil = _preparar("rec-admissao@test.com")
        pedido = _pedido(perfil, ["A"])
        claude = _Claude([_condicional(perfil)])
        gemini = _Gemini(json.dumps(_critica(perfil)))
        _descobrir(escopo, dataset_id, pedido, claude, gemini)
        franquia = db.session.get(Franquia, franquia.id)
        franquia.bloqueio_manual = True
        db.session.commit()
        parado = _descobrir(escopo, dataset_id, pedido, claude, gemini)
        assert parado["colaboracao"]["continuar"] is False
        assert len(gemini.geracoes) == 0
        assert claude.chamadas == 1
        recusada = AgenteBiExecucao.query.filter_by(analise_id=_tecnicas(dataset_id)[1].id).one()
        assert recusada.estado == "falha_definitiva"
        assert recusada.ultimo_erro == "admissao_recusada"
        with pytest.raises(ExecucaoErro) as erro:
            recuperar_falha_pre_chamada(escopo, int(recusada.id))
        assert erro.value.codigo == "erro_nao_recuperavel"
        db.session.refresh(recusada)
        assert recusada.estado == "falha_definitiva"

    def devolver(codigo, **extra):
        def _envio(**_kwargs):
            return {
                "ok": False,
                "codigo": codigo,
                "incerto": False,
                "provedor": "gemini",
                "provedor_chamado": extra.get("chamado", False),
                "texto": None,
                "tentativa_id": extra.get("tentativa_id"),
            }

        return _envio

    with app.app_context():
        for indice, (codigo, extra) in enumerate(
            (
                ("mensagem_ausente", {}),
                ("contexto_nao_autorizado", {}),
                ("modelo_ausente", {"chamado": True}),
                ("modelo_ausente", {"tentativa_id": 1}),
            )
        ):
            _usuario, _franquia, escopo, dataset_id, perfil = _preparar(f"rec-fora-{indice}@test.com")
            pedido = _pedido(perfil, ["A"])
            claude = _Claude([_condicional(perfil)])
            gemini = _Gemini(json.dumps(_critica(perfil)))
            _descobrir(escopo, dataset_id, pedido, claude, gemini)
            monkeypatch.setattr(
                "app.services.agente_bi_colaboracao_service.chamar_gemini_governado",
                devolver(codigo, **extra),
            )
            corpo = _descobrir(escopo, dataset_id, pedido, claude, gemini)
            assert corpo["colaboracao"]["continuar"] is False
            assert len(gemini.geracoes) == 0
            linha = AgenteBiExecucao.query.filter_by(analise_id=_tecnicas(dataset_id)[1].id).one()
            assert linha.estado == "falha_definitiva"
            assert linha.ultimo_erro == codigo
            if codigo != "modelo_ausente":
                with pytest.raises(ExecucaoErro) as erro:
                    recuperar_falha_pre_chamada(escopo, int(linha.id))
                assert erro.value.codigo == "erro_nao_recuperavel"
                db.session.refresh(linha)
                assert linha.estado == "falha_definitiva"


def test_recuperacao_dedicada_aceita_caso_equivalente_a_execucao_6(app, monkeypatch):
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-exec6@test.com", definitiva=True)
        execucao, analise = _assert_pre_chamada(caso, "modelo_ausente", "falha_definitiva")
        assert int(execucao.tentativas) == 1
        assert int(execucao.recuperacoes) == 0
        assert int(execucao.fencing) == 1
        assert analise.estado == "falha_validacao"
        assert _contexto(analise)["pai_hash"]
        claude_antes = _foto(caso["escopo"], *_recarregar(caso["claude_id"]))
        with pytest.raises(ExecucaoErro) as alheio:
            _usuario, _franquia, outro, _dataset, _perfil = _preparar("rec-outro-escopo@test.com")
            recuperar_falha_pre_chamada(outro, caso["execucao_id"])
        assert alheio.value.codigo == "nao_encontrado"
        monkeypatch.delenv("AGENTE_BI_GEMINI_MODEL", raising=False)
        _recusar(caso, "configuracao_invalida")
        monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
        antes = _foto(caso["escopo"], *_recarregar(caso["execucao_id"]))
        devolvida = recuperar_falha_pre_chamada(caso["escopo"], caso["execucao_id"])
        execucao, analise = _recarregar(caso["execucao_id"])
        depois = _foto(caso["escopo"], execucao, analise)
        assert devolvida["estado"] == "falha_recuperavel"
        assert devolvida["tentativas"] == 1
        assert devolvida["recuperacoes"] == 0
        assert devolvida["fencing"] == 1
        assert depois["estado"] == "falha_recuperavel"
        assert depois["analise_estado"] == "em_execucao"
        assert depois["erro"] == "modelo_ausente"
        assert depois["consumo"] == antes["consumo"]
        for campo in (
            "efeito",
            "tentativas",
            "recuperacoes",
            "fencing",
            "operacao",
            "chave",
            "analise_id",
            "tentativa_id",
            "usuario_id",
            "conta_id",
            "franquia_id",
            "resultado",
            "contexto",
            "chave_analise",
            "pai",
            "pai_hash",
        ):
            assert depois[campo] == antes[campo]
        assert depois["eventos"][:-1] == antes["eventos"]
        assert depois["eventos"][-1][1:] == (
            "falha_definitiva",
            "falha_recuperavel",
            antes["fencing"],
            "recuperacao_pre_chamada",
        )
        assert _foto(caso["escopo"], *_recarregar(caso["claude_id"])) == claude_antes
        parado = _descobrir(caso["escopo"], caso["dataset_id"], caso["pedido"], caso["claude"], caso["gemini"])
        assert caso["claude"].chamadas == 1
        assert len(caso["gemini"].geracoes) == 1
        assert parado["colaboracao"]["continuar"] is True
        assert _contagens(caso["usuario"].id)["anthropic"] == antes["consumo"]["anthropic"]
        assert len(_contagens(caso["usuario"].id)["gemini"]) == 1


def test_recuperacao_dedicada_aceita_credencial_ausente(app, monkeypatch):
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-credencial-terminal@test.com", codigo="credencial_ausente", definitiva=True)
        _assert_pre_chamada(caso, "credencial_ausente", "falha_definitiva")
        _recusar(caso, "configuracao_invalida")
        monkeypatch.setenv("GEMINI_API_KEY", "chave-de-teste-local")
        devolvida = recuperar_falha_pre_chamada(caso["escopo"], caso["execucao_id"])
        assert devolvida["estado"] == "falha_recuperavel"
        assert devolvida["ultimo_erro"] == "credencial_ausente"
        assert len(caso["gemini"].geracoes) == 0
        assert caso["claude"].chamadas == 1


def test_recuperacao_recusa_tentativa_reserva_e_efeito(app, monkeypatch):
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-recusa-efeitos@test.com", definitiva=True)
        monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
        execucao, _analise = _recarregar(caso["execucao_id"])
        tentativa = IaChamadaTentativa(
            attempt_key=execucao.chave_idempotencia,
            conta_id=execucao.conta_id,
            franquia_id=execucao.franquia_id,
            usuario_id=execucao.usuario_id,
            agent="agente_bi",
            flow_type="bi_logistico",
            operation="analisar",
            provider="gemini",
            status=IaChamadaTentativa.STATUS_RELEASED,
            reserva_ativa=False,
            reserved_credits=0,
        )
        db.session.add(tentativa)
        db.session.commit()
        _recusar(caso, "tentativa_existente")
        tentativa.reserva_ativa = True
        db.session.commit()
        _recusar(caso, "reserva_existente")
        db.session.delete(tentativa)
        db.session.commit()
        execucao, _analise = _recarregar(caso["execucao_id"])
        execucao.reserva_em_andamento = True
        db.session.commit()
        _recusar(caso, "reserva_existente")
        execucao, _analise = _recarregar(caso["execucao_id"])
        execucao.reserva_em_andamento = False
        execucao.efeito_externo = "simulado"
        db.session.commit()
        _recusar(caso, "efeito_nao_recuperavel")


def test_recuperacao_recusa_estado_resultado_e_erro_fora_da_lista(app, monkeypatch):
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-incerta@test.com", definitiva=True)
        monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
        execucao, analise = _recarregar(caso["execucao_id"])
        execucao.estado = "incerta"
        analise.estado = "incerta"
        db.session.commit()
        _recusar(caso, "estado_invalido")

        caso = _rodada_gemini(monkeypatch, "rec-resultado@test.com", definitiva=True)
        monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
        _execucao, analise = _recarregar(caso["execucao_id"])
        analise.resultado_json = '{"contrato":"colaboracao.tecnica.v1"}'
        db.session.commit()
        _recusar(caso, "historico_preservado")

        caso = _rodada_gemini(monkeypatch, "rec-erro-fora@test.com", definitiva=True)
        monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
        execucao, _analise = _recarregar(caso["execucao_id"])
        execucao.ultimo_erro = "admissao_recusada"
        db.session.commit()
        _recusar(caso, "erro_nao_recuperavel")


def test_recuperacao_repetida_nao_modifica_registros(app, monkeypatch):
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-idempotente@test.com", definitiva=True)
        monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
        primeira = recuperar_falha_pre_chamada(caso["escopo"], caso["execucao_id"])
        foto = _foto(caso["escopo"], *_recarregar(caso["execucao_id"]))
        segunda = recuperar_falha_pre_chamada(caso["escopo"], caso["execucao_id"])
        assert segunda["id"] == primeira["id"]
        assert segunda["estado"] == "falha_recuperavel"
        assert _foto(caso["escopo"], *_recarregar(caso["execucao_id"])) == foto


def test_transicoes_terminais_continuam_proibidas_fora_da_funcao(app, monkeypatch):
    assert "falha_recuperavel" not in TRANSICOES["falha_definitiva"]
    assert transicao_permitida("falha_definitiva", "falha_recuperavel") is False
    assert transicao_permitida("falha_definitiva", "aguardando") is False
    assert transicao_permitida("falha_definitiva", "adquirida") is False
    assert '"aguardando"' not in inspect.getsource(recuperar_falha_pre_chamada)
    assert '"adquirida"' not in inspect.getsource(recuperar_falha_pre_chamada)
    assert "recuperar_falha_pre_chamada" not in inspect.getsource(_avancar)
    with app.app_context():
        caso = _rodada_gemini(monkeypatch, "rec-terminal-proibida@test.com", definitiva=True)
        execucao, _analise = _recarregar(caso["execucao_id"])
        antes = _foto(caso["escopo"], execucao, _analise)
        repetida = falhar(
            caso["execucao_id"],
            worker_id="worker-terminal",
            fencing=int(execucao.fencing),
            codigo="modelo_ausente",
            recuperavel=True,
        )
        assert repetida["estado"] == "falha_definitiva"
        ciclo = executar_um_ciclo(worker_id="worker-terminal", execucao_id=caso["execucao_id"])
        assert ciclo == {"adquirida": False}
        corpo = _descobrir(caso["escopo"], caso["dataset_id"], caso["pedido"], caso["claude"], caso["gemini"])
        assert corpo["colaboracao"]["estado"] == "recusada"
        assert corpo["colaboracao"]["continuar"] is False
        assert caso["claude"].chamadas == 1
        assert len(caso["gemini"].geracoes) == 0
        assert _foto(caso["escopo"], *_recarregar(caso["execucao_id"])) == antes


def test_recuperacao_nao_entra_na_continuacao_nem_na_api():
    assert "recuperar_falha_pre_chamada" not in inspect.getsource(_avancar)
    assert "recuperar_falha_pre_chamada" not in inspect.getsource(executar_descoberta)
    assert "montar_cliente" not in inspect.getsource(_configuracao_operacional_valida)
    assert "cleiton_governed_billable_ai_call" not in inspect.getsource(recuperar_falha_pre_chamada)
    assert _configuracao_operacional_valida("gemini", "admissao_recusada") is False
    servico = ROOT / "app" / "services" / "agente_bi_colaboracao_service.py"
    texto = servico.read_text(encoding="utf-8")
    assert texto.count("recuperar_falha_pre_chamada") == 1
    for caminho in (ROOT / "app").rglob("*.py"):
        if caminho.resolve() == servico.resolve():
            continue
        assert "recuperar_falha_pre_chamada" not in caminho.read_text(encoding="utf-8")


def test_postgres_descartavel_recupera_sob_bloqueio(monkeypatch):
    uri = (os.environ.get("AGENTE_BI_EXECUCAO_PG_URI") or "").strip()
    from app.db_operational_safety import parse_database_identity

    ident = parse_database_identity(uri)
    if ident is None or not ident.is_postgres:
        pytest.skip("PostgreSQL descartável não configurado")
    if ident.database != "agente_bi_execucao_descartavel" or ident.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("URI recusada: só o cluster local descartável desta prova")

    from flask import Flask

    flask_app = Flask("agente-bi-recuperacao-pg")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
        try:
            caso = _rodada_gemini(monkeypatch, "rec-pg@test.com", definitiva=True)
            monkeypatch.setenv("AGENTE_BI_GEMINI_MODEL", "gemini-teste")
            execucao, _analise = _recarregar(caso["execucao_id"])
            execucao.efeito_externo = "simulado"
            db.session.commit()
            _recusar(caso, "efeito_nao_recuperavel")
            execucao, _analise = _recarregar(caso["execucao_id"])
            execucao.efeito_externo = "nenhum"
            db.session.commit()
            antes = _foto(caso["escopo"], *_recarregar(caso["execucao_id"]))
            recuperar_falha_pre_chamada(caso["escopo"], caso["execucao_id"])
            foto = _foto(caso["escopo"], *_recarregar(caso["execucao_id"]))
            assert foto["estado"] == "falha_recuperavel"
            assert foto["analise_estado"] == "em_execucao"
            assert foto["consumo"] == antes["consumo"]
            assert foto["fencing"] == antes["fencing"]
            assert foto["tentativas"] == antes["tentativas"]
            segunda = _foto(caso["escopo"], *_recarregar(caso["execucao_id"]))
            recuperar_falha_pre_chamada(caso["escopo"], caso["execucao_id"])
            assert _foto(caso["escopo"], *_recarregar(caso["execucao_id"])) == segunda
            assert len(caso["gemini"].geracoes) == 0
        finally:
            db.session.rollback()
            db.drop_all()
            db.session.remove()
