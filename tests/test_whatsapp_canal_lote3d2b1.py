"""Lote WhatsApp 3D2-B.1: reaplicação tardia de status órfão.

Um status que terminou em sem_saida pode ser reavaliado quando a saída
já tiver o mesmo provider_message_id. A primeira aplicação permanece.
"""
from __future__ import annotations

import importlib.util
import logging
import threading
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.pool import StaticPool

from app.db_operational_safety import run_test_schema_operation
from app.extensions import db
from app.models import (
    AplicacaoStatusCanal,
    CleitonBillingApropriacao,
    EstadoEntregaCanalSaida,
    EventoCanalRecebido,
    EventoCanalSaida,
    IaConsumoEvento,
    MonetizacaoFato,
    OnboardingCanalConclusao,
    utcnow_naive,
)
from app.services import canal_reconciliacao_status_service as reconciliacao
from tests.conftest import PYTEST_DISPOSABLE_SQLALCHEMY_URI
from tests.test_whatsapp_canal_lote3d2b import (
    CORPO_BRUTO,
    HASH_CONCLUSAO,
    MARCA_DELIVERED,
    MARCA_FAILED,
    MARCA_READ,
    MARCA_SENT,
    MESSAGE_ID,
    TELEFONE,
    TOKEN_BRUTO,
    _aceita,
    _conclusao,
    _em,
    _estado,
    _foto,
    _proibir_nos_novos,
    _saida,
    _status,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "v3w4x5y6z7a8_reconciliacao_status_orfao.py"


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_status_orfao", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tentativas(evento_id: int) -> list[AplicacaoStatusCanal]:
    return (
        AplicacaoStatusCanal.query.filter_by(evento_recebido_id=evento_id)
        .order_by(AplicacaoStatusCanal.numero_tentativa.asc())
        .all()
    )


def _entrega(saida_id: int) -> tuple:
    row = _estado(saida_id)
    return (
        row.versao,
        row.status_entrega,
        row.provider_status_em,
        row.sent_em,
        row.delivered_em,
        row.read_em,
        row.failed_em,
        row.codigo_falha_entrega,
    )


def _orfao(status: str, marca: int) -> EventoCanalRecebido:
    evento = _status(status, marca)
    resultado = reconciliacao.reconciliar_status_saida(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_SEM_SAIDA
    return evento


@pytest.fixture(autouse=True)
def _sem_rede(monkeypatch):
    chamadas = []

    def _post(*_args, **_kwargs):
        chamadas.append("post")
        raise AssertionError("cloud_api")

    def _emitir(*_args, **_kwargs):
        chamadas.append("emitir")
        raise AssertionError("token")

    monkeypatch.setattr(
        "app.services.whatsapp_meta_cloud_api_adapter.requests.post",
        _post,
    )
    monkeypatch.setattr(
        "app.services.whatsapp_meta_cloud_api_adapter.WhatsAppMetaCloudApiAdapter.enviar_texto",
        _emitir,
    )
    monkeypatch.setattr(
        "app.services.onboarding_canal_conclusao_service.emitir_link_conclusao_onboarding",
        _emitir,
    )
    monkeypatch.setattr(
        "app.services.onboarding_canal_conclusao_service.emitir_link_vinculo_conta_existente",
        _emitir,
    )
    return chamadas


def test_migration_preserva_aplicacao_e_numera_tentativa(tmp_path):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    modulo = _migration_module()
    assert modulo.revision == "v3w4x5y6z7a8"
    assert modulo.down_revision == "u2v3w4x5y6z7"
    assert modulo._SQL_TENTATIVA == AplicacaoStatusCanal._SQL_TENTATIVA
    texto = MIGRATION.read_text(encoding="utf-8")
    for proibido in (
        "payload",
        "access_token",
        "authorization",
        "telefone",
        "token_hash",
        "texto_resposta",
        "user",
    ):
        assert proibido not in texto
    nomes = {item.name for item in AplicacaoStatusCanal.__table__.constraints}
    assert "uq_aplicacao_status_canal_evento_tentativa" in nomes
    assert "uq_aplicacao_status_canal_evento" not in nomes

    banco = tmp_path / "status_orfao.sqlite"
    engine = create_engine(f"sqlite:///{banco}")
    meta = MetaData()
    tabela = Table(
        "aplicacao_status_canal",
        meta,
        Column("id", Integer, primary_key=True),
        Column("evento_recebido_id", Integer, nullable=False),
        Column("saida_id", Integer, nullable=True),
        Column("resultado", String(32), nullable=False),
        Column("provider_status_em", DateTime, nullable=True),
        Column("correlation_id", String(32), nullable=True),
        UniqueConstraint("evento_recebido_id", name="uq_aplicacao_status_canal_evento"),
        CheckConstraint(
            AplicacaoStatusCanal._SQL_RESULTADO,
            name="ck_aplicacao_status_canal_resultado",
        ),
        CheckConstraint(
            AplicacaoStatusCanal._SQL_CORRELATION,
            name="ck_aplicacao_status_canal_correlation",
        ),
        CheckConstraint(
            AplicacaoStatusCanal._SQL_COERENCIA,
            name="ck_aplicacao_status_canal_coerencia",
        ),
    )
    meta.create_all(engine)
    quando = datetime(2020, 9, 13, 12, 26, 40)
    with engine.begin() as conn:
        conn.execute(
            tabela.insert().values(
                evento_recebido_id=7,
                saida_id=None,
                resultado="sem_saida",
                provider_status_em=quando,
                correlation_id="corrorfao",
            )
        )

    def _run(fn):
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"render_as_batch": True})
            ops = Operations(context)
            original = modulo.op
            try:
                modulo.op = ops
                with conn.begin():
                    fn()
            finally:
                modulo.op = original

    _run(modulo.upgrade)
    with engine.connect() as conn:
        linha = conn.execute(text("SELECT * FROM aplicacao_status_canal")).mappings().one()
    assert linha["resultado"] == "sem_saida"
    assert linha["saida_id"] is None
    assert linha["evento_recebido_id"] == 7
    assert linha["numero_tentativa"] == 1
    assert linha["provider_status_em"] is not None
    assert linha["criado_em"] is not None
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO aplicacao_status_canal ("
                "evento_recebido_id, numero_tentativa, saida_id, resultado, "
                "provider_status_em, correlation_id, criado_em"
                ") VALUES (7, 2, 4, 'aplicado', :quando, 'corrorfao', :quando)"
            ),
            {"quando": quando},
        )
    _run(modulo.downgrade)
    colunas = {coluna["name"] for coluna in inspect(engine).get_columns("aplicacao_status_canal")}
    assert "numero_tentativa" not in colunas
    assert "criado_em" not in colunas
    with engine.connect() as conn:
        restantes = conn.execute(text("SELECT resultado, saida_id FROM aplicacao_status_canal")).all()
    assert restantes == [("aplicado", 4)]
    unicos = {
        item["name"]
        for item in inspect(engine).get_unique_constraints("aplicacao_status_canal")
    }
    assert "uq_aplicacao_status_canal_evento" in unicos


def test_sent_orfao_reaplica_quando_a_saida_aparece(ctx, caplog, _sem_rede):
    evento = _orfao("sent", MARCA_SENT)
    primeira = _tentativas(evento.id)[0]
    memoria = (
        primeira.id,
        primeira.resultado,
        primeira.saida_id,
        primeira.numero_tentativa,
        primeira.provider_status_em,
        primeira.criado_em,
    )
    saida = _aceita()
    antes = _foto(saida.id)
    with caplog.at_level(logging.DEBUG):
        resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    assert resultado.saida_id == saida.id
    assert resultado.status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
    assert estado.sent_em == _em(MARCA_SENT)
    assert estado.provider_status_em == _em(MARCA_SENT)
    assert estado.versao == 0
    assert _foto(saida.id) == antes
    db.session.expire_all()
    linhas = _tentativas(evento.id)
    assert [(linha.numero_tentativa, linha.resultado, linha.saida_id) for linha in linhas] == [
        (1, AplicacaoStatusCanal.RESULTADO_SEM_SAIDA, None),
        (2, AplicacaoStatusCanal.RESULTADO_APLICADO, saida.id),
    ]
    assert (
        linhas[0].id,
        linhas[0].resultado,
        linhas[0].saida_id,
        linhas[0].numero_tentativa,
        linhas[0].provider_status_em,
        linhas[0].criado_em,
    ) == memoria
    assert db.session.get(EventoCanalRecebido, evento.id).status_processamento == (
        EventoCanalRecebido.STATUS_ROTEADO
    )
    _proibir_nos_novos(TELEFONE, TOKEN_BRUTO, CORPO_BRUTO, "Authorization", "Bearer")
    assert TELEFONE not in caplog.text
    assert MESSAGE_ID not in caplog.text
    assert TOKEN_BRUTO not in caplog.text
    assert _sem_rede == []


def test_segunda_reaplicacao_nao_tem_efeito(ctx):
    evento = _orfao("sent", MARCA_SENT)
    saida = _aceita()
    primeiro = reconciliacao.reconciliar_status_orfao(evento.id)
    entrega = _entrega(saida.id)
    segundo = reconciliacao.reconciliar_status_orfao(evento.id)
    assert primeiro.codigo == reconciliacao.CODIGO_APLICADO
    assert segundo.codigo == reconciliacao.CODIGO_JA_TRATADO
    assert segundo.saida_id == saida.id
    assert _entrega(saida.id) == entrega
    assert len(_tentativas(evento.id)) == 2
    assert _estado(saida.id).versao == 0


def test_delivered_orfao_reaplica(ctx):
    evento = _orfao("delivered", MARCA_DELIVERED)
    saida = _aceita()
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_DELIVERED
    assert estado.delivered_em == _em(MARCA_DELIVERED)
    assert estado.provider_status_em == _em(MARCA_DELIVERED)
    assert estado.sent_em is None
    assert estado.read_em is None
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO


def test_read_orfao_reaplica(ctx):
    evento = _orfao("read", MARCA_READ)
    saida = _aceita()
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_READ
    assert estado.read_em == _em(MARCA_READ)
    assert estado.provider_status_em == _em(MARCA_READ)
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO


def test_failed_orfao_reaplica_sem_mexer_no_envio(ctx, _sem_rede):
    evento = _orfao("failed", MARCA_FAILED)
    saida = _aceita()
    antes = _foto(saida.id)
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_FAILED
    assert estado.failed_em == _em(MARCA_FAILED)
    assert estado.codigo_falha_entrega == EstadoEntregaCanalSaida.CODIGO_FALHA_ENTREGA
    assert estado.provider_status_em == _em(MARCA_FAILED)
    assert _foto(saida.id) == antes
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO
    assert _foto(saida.id)[2] is None
    _proibir_nos_novos(CORPO_BRUTO, "131026", TELEFONE)
    assert _sem_rede == []
    fonte = Path(reconciliacao.__file__).read_text(encoding="utf-8")
    assert "time.sleep" not in fonte
    assert "enviar_texto" not in fonte
    assert "emitir_link" not in fonte


def test_failed_orfao_nao_apaga_delivered(ctx):
    evento = _orfao("failed", MARCA_FAILED)
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("delivered", MARCA_DELIVERED).id)
    antes = _foto(saida.id)
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_SEM_EFEITO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_DELIVERED
    assert estado.delivered_em == _em(MARCA_DELIVERED)
    assert estado.provider_status_em == _em(MARCA_DELIVERED)
    assert estado.read_em is None
    assert estado.failed_em is None
    assert estado.codigo_falha_entrega is None
    assert _foto(saida.id) == antes
    repetido = reconciliacao.reconciliar_status_orfao(evento.id)
    assert repetido.codigo == reconciliacao.CODIGO_JA_TRATADO
    assert len(_tentativas(evento.id)) == 2


def test_sent_orfao_nao_regride_delivered(ctx):
    evento = _orfao("sent", MARCA_SENT)
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("delivered", MARCA_DELIVERED).id)
    antes = _foto(saida.id)
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_REGISTRADO_SEM_AVANCO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_DELIVERED
    assert estado.delivered_em == _em(MARCA_DELIVERED)
    assert estado.provider_status_em == _em(MARCA_DELIVERED)
    assert estado.sent_em == _em(MARCA_SENT)
    assert _foto(saida.id) == antes


def test_delivered_orfao_nao_regride_read(ctx):
    evento = _orfao("delivered", MARCA_DELIVERED)
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("read", MARCA_READ).id)
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_REGISTRADO_SEM_AVANCO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_READ
    assert estado.read_em == _em(MARCA_READ)
    assert estado.provider_status_em == _em(MARCA_READ)
    assert estado.delivered_em == _em(MARCA_DELIVERED)
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO


def test_ainda_sem_saida_nao_inventa_vinculo(ctx):
    evento = _orfao("sent", MARCA_SENT)
    reservada = _saida(status=EventoCanalSaida.STATUS_RESERVADO, message_id=None)
    antes = _foto(reservada.id)
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_AINDA_SEM_SAIDA
    assert resultado.saida_id is None
    repetido = reconciliacao.reconciliar_status_orfao(evento.id)
    assert repetido.codigo == reconciliacao.CODIGO_AINDA_SEM_SAIDA
    assert len(_tentativas(evento.id)) == 1
    assert _tentativas(evento.id)[0].resultado == AplicacaoStatusCanal.RESULTADO_SEM_SAIDA
    assert EstadoEntregaCanalSaida.query.count() == 0
    assert _foto(reservada.id) == antes
    assert db.session.get(EventoCanalRecebido, evento.id).status_processamento == (
        EventoCanalRecebido.STATUS_IGNORADO
    )


def test_saida_ambigua_nao_escolhe(ctx):
    evento = _orfao("delivered", MARCA_DELIVERED)
    primeira = _aceita()
    segunda = _aceita()
    antes = (_foto(primeira.id), _foto(segunda.id))
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_SAIDA_AMBIGUA
    assert resultado.saida_id is None
    assert EstadoEntregaCanalSaida.query.count() == 0
    assert (_foto(primeira.id), _foto(segunda.id)) == antes
    linhas = _tentativas(evento.id)
    assert [(linha.numero_tentativa, linha.resultado, linha.saida_id) for linha in linhas] == [
        (1, AplicacaoStatusCanal.RESULTADO_SEM_SAIDA, None),
        (2, AplicacaoStatusCanal.RESULTADO_SAIDA_AMBIGUA, None),
    ]
    repetido = reconciliacao.reconciliar_status_orfao(evento.id)
    assert repetido.codigo == reconciliacao.CODIGO_SAIDA_AMBIGUA
    assert len(_tentativas(evento.id)) == 2


def test_ilegivel_nao_reaplica_como_orfao(ctx):
    saida = _aceita()
    antes = _foto(saida.id)
    ilegivel = _status(
        "sent",
        MARCA_READ,
        externo="wamid.QUEBRADO",
        diagnostico="status_entrega:sent",
    )
    assert reconciliacao.reconciliar_status_saida(ilegivel.id).codigo == (
        reconciliacao.CODIGO_ILEGIVEL
    )
    resultado = reconciliacao.reconciliar_status_orfao(ilegivel.id)
    assert resultado.codigo == reconciliacao.CODIGO_REAPLICACAO_NAO_PERMITIDA
    assert len(_tentativas(ilegivel.id)) == 1
    assert _tentativas(ilegivel.id)[0].resultado == AplicacaoStatusCanal.RESULTADO_ILEGIVEL
    assert EstadoEntregaCanalSaida.query.count() == 0
    assert _foto(saida.id) == antes


def test_status_desconhecido_nao_reaplica(ctx):
    saida = _aceita()
    antes = _foto(saida.id)
    played = _status("played", MARCA_SENT)
    assert reconciliacao.reconciliar_status_saida(played.id).codigo == (
        reconciliacao.CODIGO_STATUS_IGNORADO
    )
    resultado = reconciliacao.reconciliar_status_orfao(played.id)
    assert resultado.codigo == reconciliacao.CODIGO_REAPLICACAO_NAO_PERMITIDA
    assert len(_tentativas(played.id)) == 1
    assert _tentativas(played.id)[0].resultado == AplicacaoStatusCanal.RESULTADO_STATUS_IGNORADO
    assert EstadoEntregaCanalSaida.query.count() == 0
    assert _foto(saida.id) == antes


def test_evento_ja_aplicado_nao_reaplica(ctx):
    saida = _aceita()
    evento = _status("sent", MARCA_SENT)
    assert reconciliacao.reconciliar_status_saida(evento.id).codigo == reconciliacao.CODIGO_APLICADO
    entrega = _entrega(saida.id)
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_JA_TRATADO
    assert _entrega(saida.id) == entrega
    assert len(_tentativas(evento.id)) == 1
    assert _estado(saida.id).versao == 0
    assert _estado(saida.id).sent_em == _em(MARCA_SENT)


def test_reaplicacoes_concorrentes_tem_um_efeito(_sem_rede):
    flask_app = Flask("corrida-status-3d2b1")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = PYTEST_DISPOSABLE_SQLALCHEMY_URI
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="create_all",
        )
        try:
            evento = _orfao("sent", MARCA_SENT)
            saida = _aceita()
            saida_id = int(saida.id)
            evento_id = int(evento.id)
            db.session.remove()
            barreira = threading.Barrier(2)
            resultados: dict[int, object] = {}

            def _worker(indice: int) -> None:
                try:
                    with flask_app.app_context():
                        barreira.wait(timeout=5)
                        resultados[indice] = reconciliacao.reconciliar_status_orfao(evento_id)
                        db.session.remove()
                except Exception as exc:
                    resultados[indice] = exc

            threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            assert set(resultados) == {1, 2}
            assert all(
                isinstance(item, reconciliacao.ResultadoReconciliacaoStatus)
                for item in resultados.values()
            )
            codigos = {item.codigo for item in resultados.values()}
            assert codigos == {reconciliacao.CODIGO_APLICADO, reconciliacao.CODIGO_JA_TRATADO}
            assert EstadoEntregaCanalSaida.query.count() == 1
            linhas = _tentativas(evento_id)
            assert [(linha.numero_tentativa, linha.resultado) for linha in linhas] == [
                (1, AplicacaoStatusCanal.RESULTADO_SEM_SAIDA),
                (2, AplicacaoStatusCanal.RESULTADO_APLICADO),
            ]
            estado = _estado(saida_id)
            assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
            assert estado.sent_em == _em(MARCA_SENT)
            assert estado.versao == 0
            assert _sem_rede == []
        finally:
            db.session.remove()
            run_test_schema_operation(
                db,
                PYTEST_DISPOSABLE_SQLALCHEMY_URI,
                testing=True,
                operation="drop_all",
            )


def test_exclusao_postgres_nao_e_o_lock_do_sqlite(ctx):
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects import sqlite as dialeto_sqlite

    postgres = str(
        reconciliacao._query_evento_exclusivo(1).statement.compile(
            dialect=postgresql.dialect()
        )
    )
    sqlite_sql = str(
        reconciliacao._query_evento_exclusivo(1).statement.compile(
            dialect=dialeto_sqlite.dialect()
        )
    )
    assert "FOR UPDATE" in postgres
    assert "FOR UPDATE" not in sqlite_sql


def _gravar_tentativa_2(evento_id: int, *, resultado: str, saida_id: int | None) -> None:
    db.session.add(
        AplicacaoStatusCanal(
            evento_recebido_id=evento_id,
            numero_tentativa=2,
            saida_id=saida_id,
            resultado=resultado,
            provider_status_em=_em(MARCA_DELIVERED),
            correlation_id="ganhou3d2b1",
            criado_em=utcnow_naive(),
        )
    )
    db.session.commit()


def test_revalidacao_ambigua_nao_abre_tentativa_3(ctx, monkeypatch):
    evento = _orfao("delivered", MARCA_DELIVERED)
    _aceita()
    _aceita()
    original = reconciliacao._bloquear_evento

    def _outro_processo(evento_id: int):
        bloqueado = original(evento_id)
        _gravar_tentativa_2(
            int(bloqueado.id),
            resultado=AplicacaoStatusCanal.RESULTADO_SAIDA_AMBIGUA,
            saida_id=None,
        )
        return db.session.get(EventoCanalRecebido, int(bloqueado.id))

    monkeypatch.setattr(reconciliacao, "_bloquear_evento", _outro_processo)
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_SAIDA_AMBIGUA
    linhas = _tentativas(evento.id)
    assert [(linha.numero_tentativa, linha.resultado) for linha in linhas] == [
        (1, AplicacaoStatusCanal.RESULTADO_SEM_SAIDA),
        (2, AplicacaoStatusCanal.RESULTADO_SAIDA_AMBIGUA),
    ]
    assert linhas[0].saida_id is None
    assert linhas[1].correlation_id == "ganhou3d2b1"
    assert linhas[1].saida_id is None
    assert EstadoEntregaCanalSaida.query.count() == 0


def test_revalidacao_de_sucesso_nao_altera_entrega(ctx, monkeypatch):
    evento = _orfao("sent", MARCA_SENT)
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("delivered", MARCA_DELIVERED).id)
    entrega = _entrega(saida.id)
    original = reconciliacao._bloquear_evento

    def _outro_processo(evento_id: int):
        bloqueado = original(evento_id)
        _gravar_tentativa_2(
            int(bloqueado.id),
            resultado=AplicacaoStatusCanal.RESULTADO_APLICADO,
            saida_id=int(saida.id),
        )
        return db.session.get(EventoCanalRecebido, int(bloqueado.id))

    monkeypatch.setattr(reconciliacao, "_bloquear_evento", _outro_processo)
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_JA_TRATADO
    assert _entrega(saida.id) == entrega
    linhas = _tentativas(evento.id)
    assert [(linha.numero_tentativa, linha.resultado, linha.saida_id) for linha in linhas] == [
        (1, AplicacaoStatusCanal.RESULTADO_SEM_SAIDA, None),
        (2, AplicacaoStatusCanal.RESULTADO_APLICADO, saida.id),
    ]
    assert linhas[1].correlation_id == "ganhou3d2b1"
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO


def test_reaplicacoes_concorrentes_saida_ambigua(_sem_rede):
    """Corrida disponível no SQLite. Não demonstra FOR UPDATE de PostgreSQL."""
    flask_app = Flask("corrida-status-3d2b1-ambigua")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = PYTEST_DISPOSABLE_SQLALCHEMY_URI
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": StaticPool,
        "connect_args": {"check_same_thread": False},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="create_all",
        )
        try:
            evento = _orfao("delivered", MARCA_DELIVERED)
            _aceita()
            _aceita()
            evento_id = int(evento.id)
            db.session.remove()
            barreira = threading.Barrier(2)
            resultados: dict[int, object] = {}

            def _worker(indice: int) -> None:
                try:
                    with flask_app.app_context():
                        barreira.wait(timeout=5)
                        resultados[indice] = reconciliacao.reconciliar_status_orfao(evento_id)
                        db.session.remove()
                except Exception as exc:
                    resultados[indice] = exc

            threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            assert set(resultados) == {1, 2}
            assert all(
                isinstance(item, reconciliacao.ResultadoReconciliacaoStatus)
                for item in resultados.values()
            )
            assert {item.codigo for item in resultados.values()} == {
                reconciliacao.CODIGO_SAIDA_AMBIGUA
            }
            assert EstadoEntregaCanalSaida.query.count() == 0
            linhas = _tentativas(evento_id)
            assert [(linha.numero_tentativa, linha.resultado, linha.saida_id) for linha in linhas] == [
                (1, AplicacaoStatusCanal.RESULTADO_SEM_SAIDA, None),
                (2, AplicacaoStatusCanal.RESULTADO_SAIDA_AMBIGUA, None),
            ]
            repetido = reconciliacao.reconciliar_status_orfao(evento_id)
            assert repetido.codigo == reconciliacao.CODIGO_SAIDA_AMBIGUA
            assert len(_tentativas(evento_id)) == 2
            assert _sem_rede == []
        finally:
            db.session.remove()
            run_test_schema_operation(
                db,
                PYTEST_DISPOSABLE_SQLALCHEMY_URI,
                testing=True,
                operation="drop_all",
            )


def test_processar_de_novo_nao_reaplica_sozinho(ctx):
    evento = _orfao("sent", MARCA_SENT)
    _aceita()
    repetido = reconciliacao.reconciliar_status_saida(evento.id)
    assert repetido.codigo == reconciliacao.CODIGO_JA_TRATADO
    assert len(_tentativas(evento.id)) == 1
    assert EstadoEntregaCanalSaida.query.count() == 0
    ocorrencias = []
    for caminho in (ROOT / "app").rglob("*.py"):
        if caminho.name == "canal_reconciliacao_status_service.py":
            continue
        if "reconciliar_status_orfao" in caminho.read_text(encoding="utf-8"):
            ocorrencias.append(caminho.name)
    assert ocorrencias == []


def test_nenhuma_cloud_api_e_chamada(ctx, _sem_rede):
    evento = _orfao("sent", MARCA_SENT)
    saida = _aceita()
    reconciliacao.reconciliar_status_orfao(evento.id)
    assert _estado(saida.id).status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
    assert _sem_rede == []
    fonte = Path(reconciliacao.__file__).read_text(encoding="utf-8")
    for trecho in (
        "requests",
        "graph.facebook",
        "WhatsAppMetaCloudApiAdapter",
        "enviar_texto",
    ):
        assert trecho not in fonte


def test_saida_com_link_nao_reemite_token(ctx, _sem_rede):
    conclusao = _conclusao()
    hash_antes = conclusao.token_hash
    evento = _orfao("read", MARCA_READ)
    saida = _aceita(conclusao_id=int(conclusao.id))
    resultado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    atual = db.session.get(OnboardingCanalConclusao, conclusao.id)
    assert atual.token_hash == hash_antes
    assert atual.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert atual.consumido_em is None
    assert OnboardingCanalConclusao.query.count() == 1
    assert _foto(saida.id)[4] == conclusao.id
    _proibir_nos_novos(TOKEN_BRUTO, HASH_CONCLUSAO, "/onboarding/canal/concluir/")
    assert _sem_rede == []


def test_nenhuma_julia_gemini_ou_billing(ctx):
    evento = _orfao("delivered", MARCA_DELIVERED)
    saida = _aceita()
    reconciliacao.reconciliar_status_orfao(evento.id)
    assert _estado(saida.id).status_entrega == EstadoEntregaCanalSaida.STATUS_DELIVERED
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    fonte = Path(reconciliacao.__file__).read_text(encoding="utf-8")
    for trecho in ("chat_julia", "gemini", "GenerativeModel", "run_julia", "growth"):
        assert trecho not in fonte
    for trecho in ("cleiton_monetizacao", "stripe", "IaConsumo", "billing"):
        assert trecho not in fonte
