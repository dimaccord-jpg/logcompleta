"""SCRUM-273: reinício físico do AgenteBI no escopo autenticado.

A autorização vive só na transação. DELETE direto continua proibido.
O upload que já estava em leitura pode gravar depois; o teste que mostra
isso registra o risco, não uma trava nova.
"""
from __future__ import annotations

import importlib.util
import io
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, insert, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app.agente_bi_api_routes import agente_bi_api_bp
from app.agente_bi_aplicacao_integridade import (
    CONTRATO_PLANO_APLICACAO,
    CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO,
)
from app.agente_bi_integridade import TABELA_REINICIO, instalar_protecao_agente_bi
from app.db_operational_safety import parse_database_identity
from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiAplicacao,
    AgenteBiDashboard,
    AgenteBiDashboardDataset,
    AgenteBiDataset,
    AgenteBiDatasetBloco,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    AgenteBiReinicioAutorizacao,
    Conta,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    User,
    utcnow_naive,
)
from app.services.agente_bi_execucao_service import adquirir, enfileirar
from app.services.agente_bi_ingestao_service import ArquivoBruto, ingerir
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    criar_analise,
    criar_dashboard,
    criar_dataset,
    escopo_do_usuario,
    listar_analises,
    listar_dashboards,
    listar_datasets,
    obter_analise,
    obter_dashboard,
    obter_dataset,
)
from app.services.agente_bi_reinicio_service import reiniciar
from tests.test_agente_bi_fundacao import (
    _analise,
    _cliente,
    _dashboard,
    _dataset,
    _entrar,
    _par,
    _run,
)

def _postar_csv(client, chave="lote-novo"):
    return client.post(
        "/api/agente-bi/ingestao",
        data={
            "chave": chave,
            "arquivos": (io.BytesIO(_CSV), "sintetico.csv", "text/csv"),
        },
        content_type="multipart/form-data",
    )


def _inserir_vinculo_minimo(conn) -> None:
    momento = "2026-01-01 00:00:00"
    conn.execute(
        text(
            "INSERT INTO agente_bi_dataset ("
            "conta_id, franquia_id, usuario_id, chave, origem, origem_ref, "
            "versao, impressao, estrutura_json, perfil_json, dados_tipados_json, "
            "metadados_preparacao_json, created_at, updated_at"
            ") VALUES ("
            "7, 8, 9, 'lote', 'preparada', 'ref', "
            "1, 'abc', '{}', '{}', '{}', '{}', :momento, :momento)"
        ),
        {"momento": momento},
    )
    conn.execute(
        text(
            "INSERT INTO agente_bi_dashboard ("
            "conta_id, franquia_id, usuario_id, chave, revisao, estado_publicacao, "
            "contrato_versao, executor_versao, definicoes_json, datasets_json, "
            "created_at, updated_at"
            ") VALUES ("
            "7, 8, 9, 'painel', 1, 'publicado', "
            "'v1', 'v1', '{}', '{}', :momento, :momento)"
        ),
        {"momento": momento},
    )
    conn.execute(
        text(
            "INSERT INTO agente_bi_dashboard_dataset "
            "(dashboard_id, dataset_id, conta_id, franquia_id, usuario_id, "
            "dataset_versao, dataset_impressao, created_at) "
            "VALUES (1, 1, 7, 8, 9, 1, 'abc', :momento)"
        ),
        {"momento": momento},
    )


def _pagina_com_rede(html: str, *, status: int, corpo: str, rejeitar: bool = False) -> str:
    """O reload nativo esvazia a página de set_content. O script de prova conta a chamada."""
    from tests.test_agente_bi_page import _browser_document

    script = (ROOT / "app" / "static" / "js" / "agente_bi_reinicio.js").read_text(encoding="utf-8")
    script = script.replace(
        "window.location.reload();",
        "window.__recargas = (window.__recargas || 0) + 1;",
    )
    if rejeitar:
        retorno = "return Promise.reject(new Error('rede'));"
    else:
        retorno = (
            "return Promise.resolve({status:"
            f"{status}, json:function(){{return Promise.resolve({corpo});}}}});"
        )
    return _browser_document(html).replace(
        "</body>",
        "<script>window.__posts=[]; window.__recargas=0;"
        "window.fetch=function(url, opcoes){"
        "var cabecalhos=(opcoes&&opcoes.headers)||{};"
        "window.__posts.push({url:String(url), method:(opcoes&&opcoes.method)||'GET',"
        "body:opcoes?opcoes.body:null,"
        "contentType:cabecalhos['Content-Type']||cabecalhos['content-type']||''});"
        f"{retorno}"
        "};"
        "</script>"
        f"<script>{script}</script></body>",
        1,
    )


ROOT = Path(__file__).resolve().parents[1]
_MIGRATION = ROOT / "migrations" / "versions" / "o3p4q5r6s7t8_agente_bi_reinicio_autorizacao.py"
_CSV = b"origem,destino,peso\nnorte,sul,12\nleste,oeste,4\n"
_GATILHOS = (
    "trg_agente_bi_dashboard_dataset_preservar",
    "trg_agente_bi_execucao_preservar",
    "trg_agente_bi_execucao_evento_preservar",
    "trg_agente_bi_aplicacao_preservar",
)
BANCO_PG = "agente_bi_reinicio_descartavel"


def _migration():
    spec = importlib.util.spec_from_file_location("mig_reinicio_bi", _MIGRATION)
    mig = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mig)
    return mig


def _contagem(modelo, escopo) -> int:
    return modelo.query.filter_by(
        usuario_id=escopo.usuario_id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
    ).count()


def _operacional(escopo) -> dict[str, int]:
    eventos = (
        AgenteBiExecucaoEvento.query.join(
            AgenteBiExecucao,
            AgenteBiExecucao.id == AgenteBiExecucaoEvento.execucao_id,
        )
        .filter(
            AgenteBiExecucao.usuario_id == escopo.usuario_id,
            AgenteBiExecucao.conta_id == escopo.conta_id,
            AgenteBiExecucao.franquia_id == escopo.franquia_id,
        )
        .count()
    )
    return {
        "datasets": _contagem(AgenteBiDataset, escopo),
        "blocos": _contagem(AgenteBiDatasetBloco, escopo),
        "analises": _contagem(AgenteBiAnalise, escopo),
        "dashboards": _contagem(AgenteBiDashboard, escopo),
        "vinculos": _contagem(AgenteBiDashboardDataset, escopo),
        "aplicacoes": _contagem(AgenteBiAplicacao, escopo),
        "execucoes": _contagem(AgenteBiExecucao, escopo),
        "eventos": eventos,
    }


def _bloco(escopo, dataset_id: int) -> None:
    db.session.add(
        AgenteBiDatasetBloco(
            dataset_id=dataset_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
            indice=0,
            registros=1,
            sha256="ab" * 32,
            conteudo_json='[{"uf":"SP"}]',
            created_at=utcnow_naive(),
        )
    )
    db.session.commit()


def _financeiro(escopo, franquia) -> dict:
    franquia.limite_total = Decimal("100")
    franquia.consumo_acumulado = Decimal("7.500000")
    franquia.reserva_pendente = Decimal("1.250000")
    db.session.commit()
    tentativa = IaChamadaTentativa(
        attempt_key=f"abi-reinicio-{escopo.usuario_id}",
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
        agent="agente_bi",
        flow_type="bi_logistico",
        operation="analisar",
        provider="nenhum",
        status=IaChamadaTentativa.STATUS_SETTLED,
        reserved_credits=Decimal("1.250000"),
        actual_credits=Decimal("1.250000"),
    )
    db.session.add(tentativa)
    db.session.flush()
    evento = IaConsumoEvento(
        occurred_at=utcnow_naive(),
        provider="nenhum",
        operation="analisar",
        model="",
        agent="agente_bi",
        flow_type="bi_logistico",
        api_key_label="teste",
        status="settled",
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
        regime_abatimento=IaConsumoEvento.REGIME_NOVO,
    )
    db.session.add(evento)
    db.session.flush()
    abatimento = IaConsumoAbatimento(
        ia_consumo_evento_id=evento.id,
        franquia_id=escopo.franquia_id,
        creditos=Decimal("1.250000"),
    )
    db.session.add(abatimento)
    db.session.commit()
    return {
        "tentativa_id": tentativa.id,
        "evento_id": evento.id,
        "abatimento_id": abatimento.id,
        "limite": franquia.limite_total,
        "consumo": franquia.consumo_acumulado,
        "reserva": franquia.reserva_pendente,
    }


def _foto_financeira(franquia_id: int, foto: dict) -> None:
    franquia = db.session.get(Franquia, franquia_id)
    db.session.refresh(franquia)
    assert franquia.limite_total == foto["limite"]
    assert franquia.consumo_acumulado == foto["consumo"]
    assert franquia.reserva_pendente == foto["reserva"]
    assert db.session.get(IaChamadaTentativa, foto["tentativa_id"]) is not None
    assert db.session.get(IaConsumoEvento, foto["evento_id"]) is not None
    assert db.session.get(IaConsumoAbatimento, foto["abatimento_id"]) is not None


def _montar(email: str):
    conta, franquia, usuario = _par(f"conta-{email.split('@')[0]}", email)
    escopo = escopo_do_usuario(usuario)
    primeiro = criar_dataset(escopo, _dataset(chave="fretes-kips", origem_ref="sintetico-01"))
    segundo = criar_dataset(
        escopo,
        _dataset(
            chave="fretes-kips",
            origem_ref="sintetico-02",
            versao=2,
            versao_anterior_id=primeiro["id"],
            dados_tipados=[{"uf": "RJ", "valor": 20}],
        ),
    )
    _bloco(escopo, primeiro["id"])
    _bloco(escopo, segundo["id"])
    definicao = criar_analise(
        escopo,
        _analise(
            segundo,
            chave="definicao-bi",
            etapa="definicao",
            estado="concluida",
            contrato_versao=CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO[0],
            resultado={"contrato": "definir.v1"},
        ),
    )
    plano = criar_analise(
        escopo,
        _analise(
            segundo,
            chave="plano-bi",
            etapa="execucao",
            estado="concluida",
            contrato_versao=CONTRATO_PLANO_APLICACAO,
            resultado={"situacao": "proposto"},
        ),
    )
    fila = criar_analise(escopo, _analise(segundo, chave="fila-bi"))
    painel = criar_dashboard(escopo, _dashboard(segundo, analise_id=plano["id"]))
    agora = utcnow_naive()
    aplicacao = AgenteBiAplicacao(
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
        plano_id=plano["id"],
        plano_contrato=CONTRATO_PLANO_APLICACAO,
        dataset_id=segundo["id"],
        dataset_versao=segundo["versao"],
        dataset_impressao=segundo["impressao"],
        dashboard_id=painel["id"],
        dashboard_chave=painel["chave"],
        revisao_base=painel["revisao"],
        definicao_anterior_id=definicao["id"],
        definicao_anterior_contrato=CONTRATOS_DEFINICAO_ANTERIOR_APLICACAO[0],
        estado="preparada",
        preparada_em=agora,
        created_at=agora,
        updated_at=agora,
    )
    db.session.add(aplicacao)
    db.session.commit()
    execucao = enfileirar(escopo, fila["id"], chave_idempotencia=f"reinicio.{usuario.id}")
    foto = _financeiro(escopo, franquia)
    fila_linha = db.session.get(AgenteBiAnalise, fila["id"])
    fila_linha.ia_chamada_tentativa_id = foto["tentativa_id"]
    execucao_linha = db.session.get(AgenteBiExecucao, execucao["id"])
    execucao_linha.ia_chamada_tentativa_id = foto["tentativa_id"]
    db.session.commit()
    vinculo = AgenteBiDashboardDataset.query.filter_by(dashboard_id=painel["id"]).one()
    return {
        "usuario": usuario,
        "conta": conta,
        "franquia": franquia,
        "escopo": escopo,
        "dataset_ids": [primeiro["id"], segundo["id"]],
        "analise_ids": [definicao["id"], plano["id"], fila["id"]],
        "dashboard_id": painel["id"],
        "vinculo_id": vinculo.id,
        "aplicacao_id": aplicacao.id,
        "execucao_id": execucao["id"],
        "foto": foto,
    }


def _outro(email: str):
    _conta, _franquia, usuario = _par(f"conta-{email.split('@')[0]}", email)
    escopo = escopo_do_usuario(usuario)
    dataset = criar_dataset(escopo, _dataset(chave="fretes-alheio", origem_ref="alheio-01"))
    _bloco(escopo, dataset["id"])
    analise = criar_analise(escopo, _analise(dataset, chave="analise-alheia"))
    painel = criar_dashboard(escopo, _dashboard(dataset, chave="painel-alheio"))
    return escopo, dataset["id"], analise["id"], painel["id"]


def _recusa(sql: str, params: dict | None = None) -> None:
    with pytest.raises((IntegrityError, OperationalError)) as exc:
        db.session.execute(text(sql), params or {})
        db.session.commit()
    db.session.rollback()
    assert "imutavel" in str(exc.value).lower() or "referenc" in str(exc.value).lower()


def _vazio(escopo) -> None:
    assert _operacional(escopo) == {
        "datasets": 0,
        "blocos": 0,
        "analises": 0,
        "dashboards": 0,
        "vinculos": 0,
        "aplicacoes": 0,
        "execucoes": 0,
        "eventos": 0,
    }
    assert AgenteBiReinicioAutorizacao.query.count() == 0


def test_reinicio_apaga_o_escopo_e_preserva_o_resto(ctx):
    jornada = _montar("reinicio.dono@test.com")
    escopo = jornada["escopo"]
    outro, dataset_alheio, analise_alheia, painel_alheio = _outro("reinicio.outro@test.com")
    antes_outro = _operacional(outro)
    assert all(valor >= 1 for valor in _operacional(escopo).values())
    usuarios = User.query.count()
    contas = Conta.query.count()
    franquias = Franquia.query.count()
    usuario_id = jornada["usuario"].id
    franquia_id = jornada["franquia"].id

    reiniciar(escopo)

    _vazio(escopo)
    assert _operacional(outro) == antes_outro
    assert db.session.get(AgenteBiDataset, dataset_alheio) is not None
    assert db.session.get(AgenteBiAnalise, analise_alheia) is not None
    assert db.session.get(AgenteBiDashboard, painel_alheio) is not None
    for identificador in jornada["dataset_ids"]:
        assert db.session.get(AgenteBiDataset, identificador) is None
        with pytest.raises(AgenteBiErro) as ausente:
            obter_dataset(escopo, identificador)
        assert ausente.value.status == 404
    for identificador in jornada["analise_ids"]:
        with pytest.raises(AgenteBiErro):
            obter_analise(escopo, identificador)
    with pytest.raises(AgenteBiErro):
        obter_dashboard(escopo, jornada["dashboard_id"])
    assert listar_datasets(escopo) == []
    assert listar_analises(escopo) == []
    assert listar_dashboards(escopo) == []
    assert User.query.count() == usuarios
    assert Conta.query.count() == contas
    assert Franquia.query.count() == franquias
    assert db.session.get(User, usuario_id) is not None
    _foto_financeira(franquia_id, jornada["foto"])

    reiniciar(escopo)
    _vazio(escopo)
    assert _operacional(outro) == antes_outro
    _foto_financeira(franquia_id, jornada["foto"])


def test_delete_direto_continua_proibido_fora_da_transacao(ctx):
    jornada = _montar("reinicio.direto@test.com")
    escopo = jornada["escopo"]
    evento_id = AgenteBiExecucaoEvento.query.filter_by(execucao_id=jornada["execucao_id"]).first().id
    _recusa(
        "DELETE FROM agente_bi_dashboard_dataset WHERE id = :id",
        {"id": jornada["vinculo_id"]},
    )
    _recusa("DELETE FROM agente_bi_aplicacao WHERE id = :id", {"id": jornada["aplicacao_id"]})
    _recusa("DELETE FROM agente_bi_execucao_evento WHERE id = :id", {"id": evento_id})
    _recusa("DELETE FROM agente_bi_execucao WHERE id = :id", {"id": jornada["execucao_id"]})
    _recusa(
        "DELETE FROM agente_bi_dataset WHERE id = :id",
        {"id": jornada["dataset_ids"][1]},
    )

    db.session.execute(
        insert(AgenteBiReinicioAutorizacao.__table__).values(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
        )
    )
    with pytest.raises((IntegrityError, OperationalError)):
        db.session.execute(
            text("DELETE FROM agente_bi_dataset WHERE id = :id"),
            {"id": jornada["dataset_ids"][1]},
        )
    db.session.rollback()
    db.session.execute(
        insert(AgenteBiReinicioAutorizacao.__table__).values(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
        )
    )
    db.session.execute(
        text("DELETE FROM agente_bi_dashboard_dataset WHERE id = :id"),
        {"id": jornada["vinculo_id"]},
    )
    db.session.rollback()
    assert db.session.get(AgenteBiDashboardDataset, jornada["vinculo_id"]) is not None
    assert AgenteBiReinicioAutorizacao.query.count() == 0
    _recusa(
        "DELETE FROM agente_bi_dashboard_dataset WHERE id = :id",
        {"id": jornada["vinculo_id"]},
    )


def test_falha_intermediaria_desfaz_a_exclusao(ctx):
    jornada = _montar("reinicio.falha@test.com")
    escopo = jornada["escopo"]
    antes = _operacional(escopo)

    def parar(etapa):
        if etapa == "execucoes":
            raise RuntimeError("parada")

    with pytest.raises(AgenteBiErro) as exc:
        reiniciar(escopo, ponto_de_controle=parar)
    assert exc.value.codigo == "reinicio_indisponivel"
    assert exc.value.status == 500
    db.session.rollback()
    assert _operacional(escopo) == antes
    assert AgenteBiReinicioAutorizacao.query.count() == 0
    _foto_financeira(jornada["franquia"].id, jornada["foto"])


def test_lease_vigente_responde_409_e_lease_expirado_permite(ctx):
    jornada = _montar("reinicio.lease@test.com")
    escopo = jornada["escopo"]
    franquia_id = jornada["franquia"].id
    tomada = adquirir(worker_id="worker-reinicio", execucao_id=jornada["execucao_id"])
    assert tomada["estado"] == "adquirida"
    antes = _operacional(escopo)
    with pytest.raises(AgenteBiErro) as exc:
        reiniciar(escopo)
    assert exc.value.status == 409
    assert exc.value.codigo == "execucao_em_andamento"
    assert _operacional(escopo) == antes
    assert AgenteBiReinicioAutorizacao.query.count() == 0
    _foto_financeira(franquia_id, jornada["foto"])

    linha = db.session.get(AgenteBiExecucao, jornada["execucao_id"])
    linha.lease_ate = utcnow_naive() - timedelta(seconds=1)
    db.session.commit()
    reiniciar(escopo)
    _vazio(escopo)
    _foto_financeira(franquia_id, jornada["foto"])


def test_efeito_incerto_com_lease_vencido_preserva_a_reconciliacao(app, monkeypatch):
    """O livro é a tentativa. Apagar a execução com efeito incerto não o desfaz.

    O transporte simulado vence o lease enquanto o efeito já está incerto e a
    execução ainda está adquirida. O reinício apaga essa execução depois do
    fechamento incerto. A mesma chave, sem nova chamada, aplica o abatimento
    que a primeira passagem não conseguiu gravar.
    """
    from app.services.agente_bi_anthropic_service import (
        ErroTransporte,
        chamar_anthropic_governado,
    )
    from app.services.agente_bi_reinicio_service import _lease_vigente
    from app.services.cleiton_franquia_operacional_service import (
        aplicar_motor_apos_ia_consumo_evento,
    )
    from tests.test_agente_bi_execucao import _seed_financeiro

    monkeypatch.setenv("AGENTE_BI_ANTHROPIC_ENABLED", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AGENTE_BI_ANTHROPIC_MODEL", raising=False)
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
    with app.app_context():
        usuario, franquia, escopo, analise = _seed_financeiro(email="reinicio.incerto@test.com")
        outro_conta, outra_franquia, _outro_usuario = _par(
            "reinicio-vizinho-incerto",
            "reinicio.vizinho.incerto@test.com",
        )
        outra_franquia.limite_total = Decimal("40")
        outra_franquia.consumo_acumulado = Decimal("3")
        outra_franquia.reserva_pendente = Decimal("0")
        db.session.commit()
        outro_escopo = escopo_do_usuario(_outro_usuario)
        alheio = criar_dataset(outro_escopo, _dataset(chave="vizinho-incerto"))["id"]
        usuario_id = usuario.id
        conta_id = usuario.conta_id
        franquia_id = franquia.id
        outra_franquia_id = outra_franquia.id
        outro_conta_id = outro_conta.id
        enfileirar(escopo, analise["id"], chave_idempotencia="agente-bi.reinicio.incerto")
        adquirida = adquirir(worker_id="worker-reinicio-incerto")
        execucao_id = int(adquirida["id"])
        fencing = adquirida["fencing"]
        visto = {}
        chamadas = {"n": 0}

        def enviar(_pedido):
            chamadas["n"] += 1
            linha = db.session.get(AgenteBiExecucao, execucao_id)
            db.session.refresh(linha)
            visto["efeito"] = linha.efeito_externo
            visto["estado"] = linha.estado
            linha.lease_ate = utcnow_naive() - timedelta(seconds=5)
            db.session.commit()
            db.session.refresh(linha)
            visto["lease_vencido"] = linha.lease_ate <= utcnow_naive()
            visto["tentativa_no_lease"] = linha.ia_chamada_tentativa_id
            visto["lease_vigente"] = _lease_vigente(escopo)
            raise ErroTransporte("timeout", incerto=True, input_tokens=1000, output_tokens=0)

        def proibido(_pedido):
            raise AssertionError("transporte repetido")

        antes = (
            Decimal(db.session.get(Franquia, franquia_id).consumo_acumulado or 0),
            Decimal(db.session.get(Franquia, franquia_id).reserva_pendente or 0),
        )
        resultado = chamar_anthropic_governado(
            usuario_id=usuario_id,
            conta_id=conta_id,
            franquia_id=franquia_id,
            mensagem="Frete SP 10 kg",
            chave_idempotencia="agente-bi.reinicio.incerto",
            execucao_id=execucao_id,
            worker_id="worker-reinicio-incerto",
            fencing=fencing,
            transporte=enviar,
            model="claude-sonnet-simulado",
            max_tokens=128,
        )
        assert visto["efeito"] == "incerto"
        assert visto["estado"] == "adquirida"
        assert visto["lease_vencido"] is True
        assert visto["lease_vigente"] is False
        assert isinstance(visto["tentativa_no_lease"], int)
        assert resultado["incerto"] is True
        assert resultado["provedor_chamado"] is True
        assert chamadas["n"] == 1
        linha = db.session.get(AgenteBiExecucao, execucao_id)
        assert linha.estado == "incerta"
        assert linha.efeito_externo == "incerto"
        assert linha.lease_ate is None or linha.lease_ate <= utcnow_naive()
        reiniciar(escopo)
        assert db.session.get(AgenteBiExecucao, execucao_id) is None
        assert db.session.get(AgenteBiDataset, alheio) is not None
        tentativa = db.session.get(IaChamadaTentativa, resultado["tentativa_id"])
        assert tentativa.id == visto["tentativa_no_lease"]
        assert tentativa.status == IaChamadaTentativa.STATUS_UNCERTAIN
        assert tentativa.ia_consumo_evento_id == resultado["evento_id"]
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 0
        dono = db.session.get(Franquia, franquia_id)
        assert (Decimal(dono.consumo_acumulado or 0), Decimal(dono.reserva_pendente or 0)) == antes
        vizinha = db.session.get(Franquia, outra_franquia_id)
        assert Decimal(vizinha.consumo_acumulado) == Decimal("3")
        assert Decimal(vizinha.reserva_pendente or 0) == 0
        assert db.session.get(Conta, outro_conta_id) is not None

        de_novo = chamar_anthropic_governado(
            usuario_id=usuario_id,
            conta_id=conta_id,
            franquia_id=franquia_id,
            mensagem="Frete SP 10 kg",
            chave_idempotencia="agente-bi.reinicio.incerto",
            transporte=proibido,
            model="claude-sonnet-simulado",
            max_tokens=128,
        )
        assert de_novo["provedor_chamado"] is False
        assert de_novo["incerto"] is True
        assert chamadas["n"] == 1
        assert IaConsumoEvento.query.filter_by(provider="anthropic").count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        dono = db.session.get(Franquia, franquia_id)
        assert Decimal(dono.consumo_acumulado) == Decimal("1")
        assert Decimal(dono.reserva_pendente or 0) == 0
        terceira = chamar_anthropic_governado(
            usuario_id=usuario_id,
            conta_id=conta_id,
            franquia_id=franquia_id,
            mensagem="Frete SP 10 kg",
            chave_idempotencia="agente-bi.reinicio.incerto",
            transporte=proibido,
            model="claude-sonnet-simulado",
            max_tokens=128,
        )
        assert terceira["provedor_chamado"] is False
        assert chamadas["n"] == 1
        assert IaConsumoAbatimento.query.count() == 1
        dono = db.session.get(Franquia, franquia_id)
        assert Decimal(dono.consumo_acumulado) == Decimal("1")
        vizinha = db.session.get(Franquia, outra_franquia_id)
        assert Decimal(vizinha.consumo_acumulado) == Decimal("3")
        assert db.session.get(AgenteBiDataset, alheio) is not None
        assert AgenteBiReinicioAutorizacao.query.count() == 0


def test_apis_ficam_vazias_e_o_mesmo_arquivo_comeca_de_novo(app, monkeypatch):
    client = _cliente(app, monkeypatch)
    with app.app_context():
        _conta, _franquia, usuario = _par("reinicio-api", "reinicio.api@test.com")
        _conta_b, _franquia_b, outro = _par("reinicio-api-b", "reinicio.api.b@test.com")
        usuario_id = usuario.id
        outro_id = outro.id
        escopo = escopo_do_usuario(usuario)
        dataset = criar_dataset(escopo, _dataset())
        analise = criar_analise(escopo, _analise(dataset))
        painel = criar_dashboard(escopo, _dashboard(dataset))
        ids = (dataset["id"], analise["id"], painel["id"])
        escopo_b = escopo_do_usuario(outro)
        alheio = criar_dataset(escopo_b, _dataset(chave="lote-alheio"))["id"]
    _entrar(client, usuario_id)
    assert client.get("/api/agente-bi/datasets").status_code == 200
    assert client.get("/api/agente-bi/datasets").get_json()["datasets"]

    recusado = client.post(
        "/api/agente-bi/reinicio",
        json={"usuario_id": outro_id, "conta_id": 1, "franquia_id": 1},
    )
    assert recusado.status_code == 200
    assert recusado.get_json() == {"ok": True}
    with app.app_context():
        assert db.session.get(AgenteBiDataset, alheio) is not None
        assert AgenteBiReinicioAutorizacao.query.count() == 0
    for caminho, identificador in (
        ("datasets", ids[0]),
        ("analises", ids[1]),
        ("dashboards", ids[2]),
    ):
        assert client.get(f"/api/agente-bi/{caminho}").get_json()[caminho] == []
        assert client.get(f"/api/agente-bi/{caminho}/{identificador}").status_code == 404
    repetido = client.post("/api/agente-bi/reinicio", json={})
    assert repetido.status_code == 200

    monkeypatch.setenv("AGENTE_BI_INGESTAO_ENABLED", "1")
    primeiro = _postar_csv(client)
    assert primeiro.status_code == 201, primeiro.get_json()
    primeiro_id = primeiro.get_json()["dataset"]["id"]
    assert client.post("/api/agente-bi/reinicio", json={}).status_code == 200
    assert client.get("/api/agente-bi/datasets").get_json()["datasets"] == []
    assert client.get(f"/api/agente-bi/datasets/{primeiro_id}").status_code == 404
    segundo = _postar_csv(client)
    assert segundo.status_code == 201, segundo.get_json()
    corpo = segundo.get_json()
    assert corpo["reutilizado"] is False
    assert corpo["dataset"]["versao"] == 1
    assert len(client.get("/api/agente-bi/datasets").get_json()["datasets"]) == 1
    if corpo["dataset"]["id"] != primeiro_id:
        assert client.get(f"/api/agente-bi/datasets/{primeiro_id}").status_code == 404


def test_upload_em_leitura_pode_gravar_depois_do_reinicio(ctx, monkeypatch):
    """Janela real: a ingestão só grava depois de ler o arquivo."""
    _conta, _franquia, usuario = _par("reinicio-corrida", "reinicio.corrida@test.com")
    escopo = escopo_do_usuario(usuario)
    arquivo = ArquivoBruto(nome="sintetico.csv", mime=None, conteudo=_CSV)
    primeiro = ingerir(escopo, [arquivo], chave="lote-corrida")
    assert primeiro["dataset"]["id"]

    import app.services.agente_bi_ingestao_service as ingestao

    real = ingestao._persistir_se_permitido
    visto = {}

    def no_meio(escopo_persistido, *args, **kwargs):
        visto["antes"] = AgenteBiDataset.query.filter_by(
            usuario_id=escopo_persistido.usuario_id
        ).count()
        reiniciar(escopo_persistido)
        visto["depois"] = AgenteBiDataset.query.filter_by(
            usuario_id=escopo_persistido.usuario_id
        ).count()
        return real(escopo_persistido, *args, **kwargs)

    monkeypatch.setattr(ingestao, "_persistir_se_permitido", no_meio)
    segundo = ingerir(escopo, [arquivo], chave="lote-corrida")
    assert visto == {"antes": 1, "depois": 0}
    assert segundo["persistido"] is True
    assert segundo["reutilizado"] is False
    assert segundo["dataset"]["versao"] == 1
    assert AgenteBiDataset.query.filter_by(usuario_id=escopo.usuario_id).count() == 1


def test_rota_exige_flag_e_autenticacao(app, monkeypatch):
    client = _cliente(app, monkeypatch, habilitada=False)
    assert client.post("/api/agente-bi/reinicio").status_code == 404
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    assert client.post("/api/agente-bi/reinicio").status_code == 401
    with app.app_context():
        _conta, _franquia, usuario = _par("reinicio-auth", "reinicio.auth@test.com")
        usuario_id = usuario.id
    _entrar(client, usuario_id)
    sem_json = client.post("/api/agente-bi/reinicio")
    assert sem_json.status_code == 422
    assert sem_json.get_json()["codigo"] == "payload_invalido"
    assert client.post("/api/agente-bi/reinicio", json={}).status_code == 200


def test_reinicio_sem_json_nao_apaga(app, monkeypatch):
    client = _cliente(app, monkeypatch)
    with app.app_context():
        _conta, _franquia, usuario = _par("reinicio-json", "reinicio.json@test.com")
        usuario_id = usuario.id
        escopo = escopo_do_usuario(usuario)
        dataset_id = criar_dataset(escopo, _dataset(chave="lote-json"))["id"]
        foto = _financeiro(escopo, _franquia)
        franquia_id = _franquia.id
    _entrar(client, usuario_id)
    ausente = client.post("/api/agente-bi/reinicio")
    assert ausente.status_code == 422
    assert ausente.get_json() == {"ok": False, "codigo": "payload_invalido"}
    texto = client.post(
        "/api/agente-bi/reinicio",
        data="{}",
        content_type="text/plain",
    )
    assert texto.status_code == 422
    assert texto.get_json()["codigo"] == "payload_invalido"
    with app.app_context():
        assert db.session.get(AgenteBiDataset, dataset_id) is not None
        _foto_financeira(franquia_id, foto)
        assert AgenteBiReinicioAutorizacao.query.count() == 0
    aceito = client.post("/api/agente-bi/reinicio", json={})
    assert aceito.status_code == 200
    assert aceito.get_json() == {"ok": True}
    with app.app_context():
        assert db.session.get(AgenteBiDataset, dataset_id) is None
        _foto_financeira(franquia_id, foto)
        assert AgenteBiReinicioAutorizacao.query.count() == 0


def test_rota_exige_flag_e_autenticacao_fonte():
    fonte = (ROOT / "app" / "services" / "agente_bi_reinicio_service.py").read_text(encoding="utf-8")
    assert "ia_chamada_tentativa" not in fonte
    assert "ia_consumo" not in fonte
    assert "consumo_acumulado" not in fonte
    assert "agente_bi_ingestao_service" not in fonte
    assert "agente_bi_execucao_service" not in fonte
    assert "import re" not in fonte


def test_migration_e_head_e_atualiza_gatilhos_sqlite():
    cfg = Config()
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    revisao = script.get_revision("o3p4q5r6s7t8")
    assert revisao is not None
    assert revisao.down_revision == "n2o3p4q5r6s7"
    assert script.get_heads() == ["o3p4q5r6s7t8"]

    engine = create_engine("sqlite://")
    import app.models  # noqa: F401

    AgenteBiDataset.metadata.create_all(engine)
    with engine.begin() as conn:
        instalar_protecao_agente_bi(conn, reinicio=False)
        from app.agente_bi_aplicacao_integridade import instalar_protecao_aplicacao

        instalar_protecao_aplicacao(conn, reinicio=False)
        for nome in _GATILHOS:
            sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :nome"),
                {"nome": nome},
            ).scalar()
            assert sql
            assert TABELA_REINICIO not in sql
        conn.execute(text("DROP TABLE agente_bi_reinicio_autorizacao"))

    mig = _migration()
    _run(engine, mig, mig.upgrade)
    assert TABELA_REINICIO in inspect(engine).get_table_names()
    with engine.connect() as conn:
        for nome in _GATILHOS:
            sql = conn.execute(
                text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = :nome"),
                {"nome": nome},
            ).scalar()
            assert TABELA_REINICIO in sql
    with engine.begin() as conn:
        _inserir_vinculo_minimo(conn)
    with pytest.raises((IntegrityError, OperationalError)):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM agente_bi_dashboard_dataset"))
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM agente_bi_dashboard_dataset")).scalar() == 1
        conn.execute(
            text(
                "INSERT INTO agente_bi_reinicio_autorizacao (usuario_id, conta_id, franquia_id) "
                "VALUES (9, 7, 8)"
            )
        )
        conn.execute(text("DELETE FROM agente_bi_dashboard_dataset"))
        conn.execute(text("DELETE FROM agente_bi_reinicio_autorizacao"))
        assert conn.execute(text("SELECT COUNT(*) FROM agente_bi_dashboard_dataset")).scalar() == 0
        assert conn.execute(text("SELECT COUNT(*) FROM agente_bi_reinicio_autorizacao")).scalar() == 0
    _run(engine, mig, mig.downgrade)
    with engine.connect() as conn:
        assert TABELA_REINICIO not in inspect(engine).get_table_names()
        sql = conn.execute(
            text(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name = 'trg_agente_bi_dashboard_dataset_preservar'"
            )
        ).scalar()
        assert sql
        assert TABELA_REINICIO not in sql


def _uri_pg() -> str:
    uri = (os.environ.get("AGENTE_BI_REINICIO_PG_URI") or "").strip()
    ident = parse_database_identity(uri)
    if ident is None or not ident.is_postgres:
        pytest.skip("PostgreSQL descartável não configurado")
    if ident.database != BANCO_PG or ident.host not in {"127.0.0.1", "localhost"}:
        pytest.fail("URI recusada: só o cluster local descartável desta prova")
    return uri


def test_migration_e_exclusao_no_postgresql_descartavel():
    uri = _uri_pg()
    from flask import Flask

    flask_app = Flask("agente-bi-reinicio-pg")
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
        AgenteBiDashboard.__table__,
        AgenteBiDashboardDataset.__table__,
        AgenteBiAplicacao.__table__,
        AgenteBiExecucao.__table__,
        AgenteBiExecucaoEvento.__table__,
        AgenteBiReinicioAutorizacao.__table__,
    ]
    with flask_app.app_context():
        db.metadata.drop_all(bind=db.engine, tables=list(reversed(tabelas)))
        db.metadata.create_all(bind=db.engine, tables=tabelas)
        with db.engine.begin() as conn:
            instalar_protecao_agente_bi(conn, reinicio=False)
            from app.agente_bi_aplicacao_integridade import instalar_protecao_aplicacao

            instalar_protecao_aplicacao(conn, reinicio=False)
            conn.execute(text("DROP TABLE agente_bi_reinicio_autorizacao"))
        mig = _migration()
        _run(db.engine, mig, mig.upgrade, batch=False)
        with db.engine.connect() as conn:
            for nome in (
                "agente_bi_proteger",
                "agente_bi_execucao_proteger",
                "agente_bi_aplicacao_proteger",
            ):
                corpo = conn.execute(
                    text("SELECT pg_get_functiondef(CAST(:nome AS regprocedure))"),
                    {"nome": nome + "()"},
                ).scalar()
                assert TABELA_REINICIO in corpo
        try:
            jornada = _montar("reinicio.pg@test.com")
            outro, _dataset_id, _analise_id, _painel_id = _outro("reinicio.pg.outro@test.com")
            antes = _operacional(outro)
            reiniciar(jornada["escopo"])
            _vazio(jornada["escopo"])
            assert _operacional(outro) == antes
            _foto_financeira(jornada["franquia"].id, jornada["foto"])
            assert AgenteBiReinicioAutorizacao.query.count() == 0
        finally:
            db.session.remove()
            db.metadata.drop_all(bind=db.engine, tables=list(reversed(tabelas)))
            db.session.remove()


def test_interface_mostra_confirmacao_sem_apagar_no_cancelamento(monkeypatch):
    from tests.test_agente_bi_page import _client

    web = _client(monkeypatch, authenticated=True)
    sem_flag = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert 'id="agente-bi-reinicio"' not in sem_flag
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    assert 'id="agente-bi-reinicio"' in html
    assert "excluídos permanentemente" in html
    assert 'id="agente-bi-reinicio-cancelar"' in html
    assert 'id="agente-bi-reinicio-confirmar"' in html
    script = (ROOT / "app" / "static" / "js" / "agente_bi_reinicio.js").read_text(encoding="utf-8")
    assert "location.reload" in script
    assert "enviando" in script
    assert "fetch(" in script
    assert '"Content-Type": "application/json"' in script
    assert 'body: "{}"' in script
    assert "Não foi possível confirmar o resultado. Recarregue a página para verificar." in script
    css = (ROOT / "app" / "static" / "css" / "agente_bi.css").read_text(encoding="utf-8")
    assert "#" not in css

    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from tests.test_agente_bi_page import _launch_browser

    documento = _pagina_com_rede(html, status=200, corpo="{ok:true}")
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.set_content(documento, wait_until="domcontentloaded")
        page.locator("#agente-bi-reinicio").evaluate("el => { el.hidden = false; }")
        page.locator("#agente-bi-actions-trigger").click()
        page.locator("#agente-bi-reinicio").click()
        assert page.locator("#agente-bi-reinicio-dialogo").is_visible()
        assert "permanentemente" in page.locator("#agente-bi-reinicio-texto").inner_text()
        page.locator("#agente-bi-reinicio-cancelar").click()
        assert page.locator("#agente-bi-reinicio-dialogo").is_hidden()
        assert page.evaluate("() => window.__posts.length") == 0

        page.locator("#agente-bi-actions-trigger").click()
        page.locator("#agente-bi-reinicio").click()
        page.locator("#agente-bi-reinicio-confirmar").click()
        page.wait_for_function("() => window.__posts.length === 1 && window.__recargas === 1")
        post = page.evaluate("() => window.__posts[0]")
        assert post["method"] == "POST"
        assert post["contentType"] == "application/json"
        assert post["body"] == "{}"
        page.set_viewport_size({"width": 1280, "height": 900})
        assert page.locator("#agente-bi-reinicio").count() == 1
        browser.close()


def test_interface_mostra_falha_sem_recarregar(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from tests.test_agente_bi_page import _client, _launch_browser

    web = _client(monkeypatch, authenticated=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    documento = _pagina_com_rede(
        html,
        status=409,
        corpo="{ok:false, codigo:'execucao_em_andamento'}",
    )
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        page.locator("#agente-bi-reinicio").evaluate("el => { el.hidden = false; }")
        page.locator("#agente-bi-actions-trigger").click()
        page.locator("#agente-bi-reinicio").click()
        confirmar = page.locator("#agente-bi-reinicio-confirmar")
        confirmar.click()
        page.locator("#agente-bi-reinicio-erro").wait_for(state="visible")
        assert "mantidos" in page.locator("#agente-bi-reinicio-erro").inner_text()
        assert page.evaluate("() => window.__recargas") == 0
        assert page.evaluate("() => window.__posts.length") == 1
        assert confirmar.is_enabled()
        browser.close()


def test_erro_de_rede_nao_afirma_preservacao(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from tests.test_agente_bi_page import _client, _launch_browser

    web = _client(monkeypatch, authenticated=True)
    monkeypatch.setenv("AGENTE_BI_API_ENABLED", "1")
    html = web.app.test_client().get("/cleide-bi-frete").get_data(as_text=True)
    documento = _pagina_com_rede(html, status=0, corpo="{}", rejeitar=True)
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.set_content(documento, wait_until="domcontentloaded")
        page.locator("#agente-bi-reinicio").evaluate("el => { el.hidden = false; }")
        page.locator("#agente-bi-actions-trigger").click()
        page.locator("#agente-bi-reinicio").click()
        confirmar = page.locator("#agente-bi-reinicio-confirmar")
        confirmar.click()
        erro = page.locator("#agente-bi-reinicio-erro")
        erro.wait_for(state="visible")
        assert erro.inner_text() == (
            "Não foi possível confirmar o resultado. Recarregue a página para verificar."
        )
        assert page.evaluate("() => window.__recargas") == 0
        assert page.evaluate("() => window.__posts.length") == 1
        assert confirmar.is_enabled()
        browser.close()


def test_blueprint_publica_o_reinicio():
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(agente_bi_api_bp)
    regras = {regra.rule for regra in app.url_map.iter_rules()}
    assert "/api/agente-bi/reinicio" in regras
