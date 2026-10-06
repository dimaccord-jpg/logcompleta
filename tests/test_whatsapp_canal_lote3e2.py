"""Lote WhatsApp 3E2: recuperação explícita de texto comum.

Não cobre retry automático, link seguro, token novo, scheduler, fila nem cobrança.
"""
from __future__ import annotations

import contextlib
import importlib.util
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AplicacaoStatusCanal,
    CleitonBillingApropriacao,
    EstadoEntregaCanalSaida,
    EstadoEntregaTentativaCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    IaConsumoEvento,
    MonetizacaoFato,
    OnboardingCanalConclusao,
    TentativaEnvioCanal,
    utcnow_naive,
)
from app.services import canal_reconciliacao_status_service as reconciliacao
from app.services import canal_recuperacao_texto_service as recuperacao
from app.services import canal_saida_whatsapp_service as saida
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_EMITIR_SENHA,
    CODIGO_LINK_NAO_EMITIDO,
)
from app.services.whatsapp_meta_cloud_api_adapter import ResultadoAdapterWhatsApp
from tests.test_whatsapp_canal_lote3e1 import (
    CORRELATION,
    DESTINATARIO,
    PHONE_NUMBER_ID,
    TOKEN,
    _configurar,
    _interpretacao,
    _migration_module as _migration_3e1,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "x5y6z7a8b9c0_recuperacao_manual_texto.py"
MIGRATION_CHAVE = ROOT / "migrations" / "versions" / "y6z7a8b9c0d1_chave_recuperacao_tentativa.py"
MIGRATION_3E1 = ROOT / "migrations" / "versions" / "w4x5y6z7a8b9_tentativa_envio_canal.py"

TEXTO = "Texto original 3E2 sem reescrita."
MESSAGE_1 = "wamid.TENTATIVA1.3E2"
MESSAGE_2 = "wamid.TENTATIVA2.3E2"
MESSAGE_3 = "wamid.TENTATIVA3.3E2"
MARCADOR = "CORPO_BRUTO_META_3E2"
MARCA_SENT = 1710000001
MARCA_DELIVERED = 1710000060
MARCA_READ = 1710000120
MARCA_FAILED = 1710000180


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_recuperacao_texto", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_chave():
    spec = importlib.util.spec_from_file_location("mig_chave_recuperacao", MIGRATION_CHAVE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _chave() -> str:
    return str(uuid.uuid4())


class _Resposta:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = MARCADOR

    def json(self):
        return self._payload

    def close(self):
        return None


def _ok(message_id):
    return _Resposta(200, {"messaging_product": "whatsapp", "messages": [{"id": message_id}]})


def _http_500():
    return _Resposta(500, {"error": {"message": MARCADOR}})


def _mock(monkeypatch, respostas):
    chamadas = []
    fila = list(respostas)

    def _post(url, **kwargs):
        chamadas.append((url, kwargs))
        if not fila:
            raise AssertionError("post_extra")
        return fila.pop(0)

    monkeypatch.setattr(adapter.requests, "post", _post)
    return chamadas


def _foto(tentativa: TentativaEnvioCanal):
    return (
        tentativa.id,
        tentativa.numero_tentativa,
        tentativa.origem_tentativa,
        tentativa.status_tentativa,
        tentativa.provider_message_id,
        tentativa.codigo_erro,
        tentativa.enviado_em,
        tentativa.finalizado_em,
        tentativa.criado_em,
        tentativa.motivo_recuperacao,
        tentativa.classificacao_resultado,
        tentativa.chave_recuperacao,
    )


def _tentativa(numero: int) -> TentativaEnvioCanal:
    return TentativaEnvioCanal.query.filter_by(numero_tentativa=numero).one()


def _recarregar(tentativa_id: int) -> TentativaEnvioCanal:
    db.session.expire_all()
    return db.session.get(TentativaEnvioCanal, tentativa_id)


def _erro_inicial(monkeypatch, texto=TEXTO, message_id=MESSAGE_1):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, [_http_500(), _ok(message_id)])
    interpretacao = _interpretacao(texto, externo="wamid.ENTRADA.3E2." + message_id)
    resultado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert resultado.status_envio == EventoCanalSaida.STATUS_ERRO
    assert resultado.codigo_erro == EventoCanalSaida.CODIGO_HTTP_5XX
    return chamadas, interpretacao


def _status(message_id: str, nome: str, marca: int) -> EventoCanalRecebido:
    row = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=f"{message_id}:{nome}:{marca}",
        tipo_evento=EventoCanalRecebido.TIPO_STATUS_ENTREGA,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id="corr3e2status",
        diagnostico_seguro=f"status_entrega:{nome}",
    )
    db.session.add(row)
    db.session.commit()
    return row


def _entrega_da(tentativa_id: int) -> EstadoEntregaTentativaCanal | None:
    return EstadoEntregaTentativaCanal.query.filter_by(
        tentativa_envio_id=tentativa_id
    ).one_or_none()


def _aceita_e_falha(monkeypatch, message_id=MESSAGE_1):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, [_ok(message_id)])
    interpretacao = _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.OK." + message_id)
    enviado = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert enviado.provider_message_id == message_id
    falha = reconciliacao.reconciliar_status_saida(_status(message_id, "failed", MARCA_FAILED).id)
    assert falha.codigo == reconciliacao.CODIGO_APLICADO
    return chamadas


def test_migration_preserva_tentativa_1_e_libera_ate_3(tmp_path):
    modulo = _migration_module()
    anterior = _migration_3e1()
    assert modulo.revision == "x5y6z7a8b9c0"
    assert modulo.down_revision == "w4x5y6z7a8b9"
    assert modulo._SQL_NUMERO == TentativaEnvioCanal._SQL_NUMERO
    assert modulo._SQL_ORIGEM == TentativaEnvioCanal._SQL_ORIGEM
    assert modulo._SQL_MOTIVO == TentativaEnvioCanal._SQL_MOTIVO
    assert modulo._SQL_ESTADO_FORMA == EstadoEntregaTentativaCanal._SQL_FORMA
    texto = MIGRATION.read_text(encoding="utf-8")
    for proibido in (
        "access_token",
        "authorization",
        "telefone",
        "texto_resposta",
        "https://",
        "Bearer",
    ):
        assert proibido not in texto
    assert "INSERT INTO tentativa_envio_canal" not in texto

    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    banco = tmp_path / "recuperacao_texto.sqlite"
    engine = create_engine(f"sqlite:///{banco}")
    meta = MetaData()
    Table(
        "onboarding_canal_conclusao",
        meta,
        Column("id", Integer, primary_key=True),
    )
    Table(
        "interpretacao_conversacional_canal",
        meta,
        Column("id", Integer, primary_key=True),
        Column("acao", String(64), nullable=False),
        Column("codigo", String(64), nullable=False),
        Column("etapa_final", String(40)),
        Column("conclusao_id", Integer),
    )
    Table(
        "evento_canal_saida",
        meta,
        Column("id", Integer, primary_key=True),
        Column("interpretacao_id", Integer, nullable=False),
        Column("status_envio", String(32), nullable=False),
        Column("provider", String(32), nullable=False),
        Column("provider_message_id", String(200)),
        Column("codigo_erro", String(32)),
        Column("conclusao_id", Integer),
        Column("criado_em", DateTime, nullable=False),
        Column("enviado_em", DateTime),
        Column("correlation_id", String(64), nullable=False),
    )
    Table(
        "estado_entrega_canal_saida",
        meta,
        Column("id", Integer, primary_key=True),
        Column("saida_id", Integer, nullable=False),
        Column("versao", Integer, nullable=False),
        Column("status_entrega", String(16)),
        Column("provider_status_em", DateTime),
        Column("sent_em", DateTime),
        Column("delivered_em", DateTime),
        Column("read_em", DateTime),
        Column("failed_em", DateTime),
        Column("codigo_falha_entrega", String(32)),
        Column("classificacao_resultado", String(32)),
    )
    Table(
        "aplicacao_status_canal",
        meta,
        Column("id", Integer, primary_key=True),
        Column("evento_recebido_id", Integer, nullable=False),
        Column("numero_tentativa", Integer, nullable=False),
        Column("saida_id", Integer),
        Column("resultado", String(32), nullable=False),
        Column("provider_status_em", DateTime),
        Column("correlation_id", String(32)),
        Column("criado_em", DateTime, nullable=False),
    )
    meta.create_all(engine)
    criado = datetime(2026, 10, 2, 12, 0, 0)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO interpretacao_conversacional_canal "
                "(id, acao, codigo) VALUES (1, 'registrar_nome', 'resposta_registrada')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO evento_canal_saida "
                "(id, interpretacao_id, status_envio, provider, codigo_erro, "
                "criado_em, correlation_id) "
                "VALUES (1, 1, 'erro', 'meta_whatsapp', 'timeout', :criado, 'corr-3e2')"
            ),
            {"criado": criado},
        )
        conn.execute(
            text(
                "INSERT INTO aplicacao_status_canal "
                "(id, evento_recebido_id, numero_tentativa, saida_id, resultado, criado_em) "
                "VALUES (1, 9, 1, 1, 'aplicado', :criado)"
            ),
            {"criado": criado},
        )

    def _run(alvo, fn):
        with engine.connect() as conn:
            context = MigrationContext.configure(conn, opts={"render_as_batch": True})
            ops = Operations(context)
            original = alvo.op
            try:
                alvo.op = ops
                with conn.begin():
                    fn()
            finally:
                alvo.op = original

    _run(anterior, anterior.upgrade)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tentativa_envio_canal")).scalar() == 1
    _run(modulo, modulo.upgrade)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tentativa_envio_canal")).scalar() == 1
        linha = conn.execute(text("SELECT numero_tentativa, origem_tentativa, motivo_recuperacao FROM tentativa_envio_canal")).one()
        assert tuple(linha) == (1, "envio_inicial", None)
        assert conn.execute(
            text("SELECT tentativa_envio_id FROM aplicacao_status_canal")
        ).scalar() == conn.execute(text("SELECT id FROM tentativa_envio_canal")).scalar()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tentativa_envio_canal ("
                "saida_id, numero_tentativa, tipo_tentativa, origem_tentativa, "
                "status_tentativa, provider, criado_em, correlation_id, motivo_recuperacao"
                ") VALUES (1, 2, 'texto_comum', 'recuperacao_manual', "
                "'reservada', 'meta_whatsapp', :criado, 'corr-3e2', 'falha_http')"
            ),
            {"criado": criado},
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tentativa_envio_canal ("
                    "saida_id, numero_tentativa, tipo_tentativa, origem_tentativa, "
                    "status_tentativa, provider, criado_em, correlation_id, motivo_recuperacao"
                    ") VALUES (1, 4, 'texto_comum', 'recuperacao_manual', "
                    "'reservada', 'meta_whatsapp', :criado, 'corr-3e2', 'falha_http')"
                ),
                {"criado": criado},
            )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tentativa_envio_canal")).scalar() == 2
    assert MIGRATION_3E1.name == "w4x5y6z7a8b9_tentativa_envio_canal.py"

    chave = _migration_chave()
    assert chave.revision == "y6z7a8b9c0d1"
    assert chave.down_revision == "x5y6z7a8b9c0"
    assert chave._SQL_IDENTIDADE == TentativaEnvioCanal._SQL_IDENTIDADE
    texto_chave = MIGRATION_CHAVE.read_text(encoding="utf-8")
    for proibido in (
        "access_token",
        "authorization",
        "telefone",
        "texto_resposta",
        "https://",
        "Bearer",
    ):
        assert proibido not in texto_chave
    _run(chave, chave.upgrade)
    with engine.connect() as conn:
        inicial = conn.execute(
            text(
                "SELECT chave_recuperacao FROM tentativa_envio_canal "
                "WHERE numero_tentativa = 1"
            )
        ).scalar()
        assert inicial is None
        legada = conn.execute(
            text(
                "SELECT id, chave_recuperacao FROM tentativa_envio_canal "
                "WHERE numero_tentativa = 2"
            )
        ).one()
        assert legada.chave_recuperacao == chave._chave_legada(legada.id)
        assert conn.execute(text("SELECT COUNT(*) FROM tentativa_envio_canal")).scalar() == 2
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tentativa_envio_canal ("
                    "saida_id, numero_tentativa, tipo_tentativa, origem_tentativa, "
                    "status_tentativa, provider, criado_em, correlation_id, "
                    "motivo_recuperacao, chave_recuperacao"
                    ") VALUES (1, 3, 'texto_comum', 'recuperacao_manual', "
                    "'reservada', 'meta_whatsapp', :criado, 'corr-3e2', "
                    "'falha_http', :chave)"
                ),
                {"criado": criado, "chave": legada.chave_recuperacao},
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tentativa_envio_canal ("
                    "saida_id, numero_tentativa, tipo_tentativa, origem_tentativa, "
                    "status_tentativa, provider, criado_em, correlation_id, "
                    "motivo_recuperacao"
                    ") VALUES (1, 3, 'texto_comum', 'recuperacao_manual', "
                    "'reservada', 'meta_whatsapp', :criado, 'corr-3e2', 'falha_http')"
                ),
                {"criado": criado},
            )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tentativa_envio_canal ("
                "saida_id, numero_tentativa, tipo_tentativa, origem_tentativa, "
                "status_tentativa, provider, criado_em, correlation_id, "
                "motivo_recuperacao, chave_recuperacao"
                ") VALUES (1, 3, 'texto_comum', 'recuperacao_manual', "
                "'reservada', 'meta_whatsapp', :criado, 'corr-3e2', "
                "'operacao_manual', :chave)"
            ),
            {"criado": criado, "chave": _chave()},
        )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tentativa_envio_canal")).scalar() == 3
        assert conn.execute(
            text(
                "SELECT chave_recuperacao FROM tentativa_envio_canal "
                "WHERE numero_tentativa = 1"
            )
        ).scalar() is None


def test_erro_elegivel_cria_tentativa_2_com_texto_original(ctx, monkeypatch, caplog):
    chamadas, _interpretacao_row = _erro_inicial(monkeypatch)
    antes = _foto(_tentativa(1))
    chave = _chave()
    with caplog.at_level(logging.INFO):
        resultado = recuperacao.recuperar_saida_textual(
            _tentativa(1).saida_id,
            TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
            chave,
            operador_contexto="comentario livre que nao pode ser gravado",
        )
    assert resultado.codigo == EventoCanalSaida.STATUS_ACEITO
    assert resultado.numero_tentativa == 2
    primeira = _recarregar(antes[0])
    assert _foto(primeira) == antes
    segunda = _tentativa(2)
    assert segunda.origem_tentativa == TentativaEnvioCanal.ORIGEM_RECUPERACAO_MANUAL
    assert segunda.motivo_recuperacao == TentativaEnvioCanal.MOTIVO_FALHA_HTTP
    assert segunda.chave_recuperacao == chave
    assert primeira.chave_recuperacao is None
    assert segunda.provider_message_id == MESSAGE_1
    assert segunda.status_tentativa == TentativaEnvioCanal.STATUS_ACEITA
    assert segunda.enviado_em is not None
    assert segunda.finalizado_em == segunda.enviado_em
    saida_row = EventoCanalSaida.query.one()
    assert saida_row.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert saida_row.provider_message_id == MESSAGE_1
    assert saida_row.codigo_erro is None
    assert saida_row.enviado_em == segunda.enviado_em
    assert len(chamadas) == 2
    _url, kwargs = chamadas[1]
    assert kwargs["json"]["text"]["body"] == TEXTO
    assert kwargs["json"]["to"] == DESTINATARIO
    assert PHONE_NUMBER_ID in _url
    assert "comentario livre" not in caplog.text
    assert IaConsumoEvento.query.count() == 0
    replay = recuperacao.recuperar_saida_textual(
        resultado.saida_id,
        TentativaEnvioCanal.MOTIVO_OPERACAO_MANUAL,
        chave,
    )
    assert replay.tentativa_id == resultado.tentativa_id
    assert replay.numero_tentativa == 2
    assert replay.codigo == EventoCanalSaida.STATUS_ACEITO
    assert TentativaEnvioCanal.query.count() == 2
    assert len(chamadas) == 2


def test_erro_da_tentativa_2_preserva_a_primeira(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, [_http_500(), _http_500()])
    saida.enviar_texto_da_interpretacao(_interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.ERRO2").id)
    antes = _foto(_tentativa(1))
    resultado = recuperacao.recuperar_saida_textual(
        _tentativa(1).saida_id,
        TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
        _chave(),
    )
    assert resultado.codigo == EventoCanalSaida.CODIGO_HTTP_5XX
    assert resultado.status_tentativa == TentativaEnvioCanal.STATUS_ERRO
    assert _foto(_recarregar(antes[0])) == antes
    segunda = _tentativa(2)
    assert segunda.provider_message_id is None
    assert segunda.codigo_erro == EventoCanalSaida.CODIGO_HTTP_5XX
    assert segunda.finalizado_em is not None
    saida_row = EventoCanalSaida.query.one()
    assert saida_row.status_envio == EventoCanalSaida.STATUS_ERRO
    assert saida_row.provider_message_id is None
    assert saida_row.codigo_erro == EventoCanalSaida.CODIGO_HTTP_5XX
    assert len(chamadas) == 2
    assert primeira_inalterada(antes)


def test_tentativa_3_permitida_e_quarta_bloqueada(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, [_http_500(), _http_500(), _ok(MESSAGE_3)])
    saida.enviar_texto_da_interpretacao(_interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.T3").id)
    saida_id = _tentativa(1).saida_id
    segunda = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
        _chave(),
    )
    assert segunda.numero_tentativa == 2
    terceira = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_RESULTADO_INCERTO,
        _chave(),
    )
    assert terceira.numero_tentativa == 3
    assert terceira.codigo == EventoCanalSaida.STATUS_ACEITO
    assert _tentativa(3).provider_message_id == MESSAGE_3
    assert EventoCanalSaida.query.one().provider_message_id == MESSAGE_3
    quarta = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_OPERACAO_MANUAL,
        _chave(),
    )
    assert quarta.codigo == recuperacao.CODIGO_LIMITE_TENTATIVAS
    assert TentativaEnvioCanal.query.count() == 3
    assert len(chamadas) == 3


def test_sent_delivered_e_read_historicos_bloqueiam(ctx, monkeypatch):
    for nome, marca in (
        ("sent", MARCA_SENT),
        ("delivered", MARCA_DELIVERED),
        ("read", MARCA_READ),
    ):
        message_id = f"{MESSAGE_1}.{nome}"
        _configurar(monkeypatch)
        chamadas = _mock(monkeypatch, [_ok(message_id)])
        enviado = saida.enviar_texto_da_interpretacao(
            _interpretacao(TEXTO, externo=f"wamid.ENTRADA.3E2.{nome}").id
        )
        aplicado = reconciliacao.reconciliar_status_saida(_status(message_id, nome, marca).id)
        assert aplicado.codigo == reconciliacao.CODIGO_APLICADO
        resultado = recuperacao.recuperar_saida_textual(
            enviado.saida_id,
            TentativaEnvioCanal.MOTIVO_OPERACAO_MANUAL,
            _chave(),
        )
        assert resultado.codigo == recuperacao.CODIGO_EVIDENCIA_POSITIVA
        assert TentativaEnvioCanal.query.filter_by(saida_id=enviado.saida_id).count() == 1
        assert len(chamadas) == 1


def test_failed_sem_evidencia_permite_e_com_sent_bloqueia(ctx, monkeypatch):
    chamadas = _aceita_e_falha(monkeypatch)
    saida_id = EventoCanalSaida.query.one().id
    assert _entrega_da(_tentativa(1).id).sent_em is None
    extra = _mock(monkeypatch, [_ok(MESSAGE_2)])
    resultado = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_FALHA_ENTREGA_PROVIDER,
        _chave(),
    )
    assert resultado.numero_tentativa == 2
    assert resultado.provider_message_id == MESSAGE_2
    assert TentativaEnvioCanal.query.filter_by(saida_id=saida_id, numero_tentativa=1).one().provider_message_id == MESSAGE_1
    assert len(chamadas) == 1
    assert len(extra) == 1

    message_id = "wamid.TENTATIVA1.3E2.SENTFAILED"
    _configurar(monkeypatch)
    chamadas_sent = _mock(monkeypatch, [_ok(message_id)])
    enviado = saida.enviar_texto_da_interpretacao(
        _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.SENTFAILED").id
    )
    reconciliacao.reconciliar_status_saida(_status(message_id, "sent", MARCA_SENT).id)
    reconciliacao.reconciliar_status_saida(_status(message_id, "failed", MARCA_FAILED).id)
    tentativa = TentativaEnvioCanal.query.filter_by(saida_id=enviado.saida_id, numero_tentativa=1).one()
    estado = _entrega_da(tentativa.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_FAILED
    assert estado.sent_em is not None
    bloqueado = recuperacao.recuperar_saida_textual(
        enviado.saida_id,
        TentativaEnvioCanal.MOTIVO_FALHA_ENTREGA_PROVIDER,
        _chave(),
    )
    assert bloqueado.codigo == recuperacao.CODIGO_EVIDENCIA_POSITIVA
    assert TentativaEnvioCanal.query.filter_by(saida_id=enviado.saida_id).count() == 1
    assert len(chamadas_sent) == 1


def test_reservado_e_resultado_incerto_permitem_recuperacao(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = []

    def _estoura(self, **_kwargs):
        chamadas.append("queda")
        raise RuntimeError("queda-depois-da-reserva")

    monkeypatch.setattr(adapter.WhatsAppMetaCloudApiAdapter, "enviar_texto", _estoura)
    interpretacao = _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.RESERVADO")
    with pytest.raises(RuntimeError):
        saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert _tentativa(1).status_tentativa == TentativaEnvioCanal.STATUS_RESERVADA

    def _envia(self, **kwargs):
        chamadas.append(kwargs["texto"])
        return ResultadoAdapterWhatsApp(provider_message_id=MESSAGE_2)

    monkeypatch.setattr(adapter.WhatsAppMetaCloudApiAdapter, "enviar_texto", _envia)
    resultado = recuperacao.recuperar_saida_textual(
        _tentativa(1).saida_id,
        TentativaEnvioCanal.MOTIVO_RESULTADO_INCERTO,
        _chave(),
    )
    assert resultado.numero_tentativa == 2
    assert chamadas[-1] == TEXTO
    primeira = TentativaEnvioCanal.query.filter_by(saida_id=resultado.saida_id, numero_tentativa=1).one()
    assert primeira.status_tentativa == TentativaEnvioCanal.STATUS_RESERVADA

    _configurar(monkeypatch)
    monkeypatch.setattr(adapter.WhatsAppMetaCloudApiAdapter, "enviar_texto", _estoura)
    interpretacao = _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.INCERTO")
    with pytest.raises(RuntimeError):
        saida.enviar_texto_da_interpretacao(interpretacao.id)
    reservada = EventoCanalSaida.query.filter_by(interpretacao_id=interpretacao.id).one()
    classificado = reconciliacao.classificar_resultado_incerto(reservada.id)
    assert classificado.codigo == reconciliacao.CODIGO_CLASSIFICADO
    tentativa_incerta = TentativaEnvioCanal.query.filter_by(saida_id=reservada.id, numero_tentativa=1).one()
    assert tentativa_incerta.classificacao_resultado == TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO
    monkeypatch.setattr(adapter.WhatsAppMetaCloudApiAdapter, "enviar_texto", _envia)
    incerto = recuperacao.recuperar_saida_textual(
        reservada.id,
        TentativaEnvioCanal.MOTIVO_RESULTADO_INCERTO,
        _chave(),
    )
    assert incerto.numero_tentativa == 2
    assert tentativa_incerta.classificacao_resultado == TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO
    assert (
        TentativaEnvioCanal.query.filter_by(saida_id=reservada.id, numero_tentativa=2).one().origem_tentativa
        == TentativaEnvioCanal.ORIGEM_RECUPERACAO_MANUAL
    )


def test_duas_recuperacoes_concorrentes_geram_um_unico_post(tmp_path):
    banco = tmp_path / "corrida_recuperacao.sqlite"
    uri = "sqlite:///" + banco.as_posix()
    flask_app = Flask("corrida-recuperacao-3e2")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 5},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    posts = []
    original = adapter.WhatsAppMetaCloudApiAdapter.enviar_texto
    trava_original = recuperacao._trava
    try:
        with flask_app.app_context():
            import app.models  # noqa: F401

            db.create_all()
            _configurar_env()
            interpretacao = _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.CORRIDA")

            def _falha(self, **_kwargs):
                return ResultadoAdapterWhatsApp(codigo_erro=EventoCanalSaida.CODIGO_HTTP_5XX)

            adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = _falha
            enviado = saida.enviar_texto_da_interpretacao(interpretacao.id)
            assert enviado.codigo_erro == EventoCanalSaida.CODIGO_HTTP_5XX
            saida_id = int(EventoCanalSaida.query.one().id)
            chave = _chave()

            def _envia(self, **kwargs):
                posts.append(kwargs["texto"])
                return ResultadoAdapterWhatsApp(provider_message_id=MESSAGE_2)

            adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = _envia
            db.session.remove()
            barreira = threading.Barrier(2)
            resultados = {}
            trava_original = recuperacao._trava
            recuperacao._trava = lambda _saida_id: contextlib.nullcontext()

            def _worker(indice: int) -> None:
                try:
                    with flask_app.app_context():
                        barreira.wait(timeout=5)
                        resultados[indice] = recuperacao.recuperar_saida_textual(
                            saida_id,
                            TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
                            chave,
                        )
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
                isinstance(item, recuperacao.ResultadoRecuperacaoTexto) for item in resultados.values()
            )
            codigos = {item.codigo for item in resultados.values()}
            assert EventoCanalSaida.STATUS_ACEITO in codigos
            assert posts == [TEXTO]
            assert TentativaEnvioCanal.query.count() == 2
            assert TentativaEnvioCanal.query.filter_by(numero_tentativa=3).count() == 0
            ids = {item.tentativa_id for item in resultados.values()}
            assert ids == {TentativaEnvioCanal.query.filter_by(numero_tentativa=2).one().id}
            db.session.remove()
            db.drop_all()
    finally:
        recuperacao._trava = trava_original
        adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = original


def test_mesma_acao_concorrente_erro_nao_cria_tentativa_3(tmp_path):
    banco = tmp_path / "corrida_erro.sqlite"
    uri = "sqlite:///" + banco.as_posix()
    flask_app = Flask("corrida-erro-3e2")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 15},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    posts = []
    original = adapter.WhatsAppMetaCloudApiAdapter.enviar_texto
    trava_original = recuperacao._trava
    try:
        with flask_app.app_context():
            import app.models  # noqa: F401

            db.create_all()
            _configurar_env()
            interpretacao = _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.CORRIDA.ERRO")

            def _falha(self, **_kwargs):
                return ResultadoAdapterWhatsApp(codigo_erro=EventoCanalSaida.CODIGO_HTTP_5XX)

            adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = _falha
            enviado = saida.enviar_texto_da_interpretacao(interpretacao.id)
            assert enviado.codigo_erro == EventoCanalSaida.CODIGO_HTTP_5XX
            saida_id = int(EventoCanalSaida.query.one().id)
            chave = _chave()

            def _envia(self, **kwargs):
                posts.append(kwargs["texto"])
                return ResultadoAdapterWhatsApp(codigo_erro=EventoCanalSaida.CODIGO_HTTP_5XX)

            adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = _envia
            db.session.remove()
            barreira = threading.Barrier(2)
            resultados = {}
            recuperacao._trava = lambda _saida_id: contextlib.nullcontext()

            def _worker(indice: int) -> None:
                try:
                    with flask_app.app_context():
                        barreira.wait(timeout=5)
                        resultados[indice] = recuperacao.recuperar_saida_textual(
                            saida_id,
                            TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
                            chave,
                        )
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
                isinstance(item, recuperacao.ResultadoRecuperacaoTexto) for item in resultados.values()
            )
            assert posts == [TEXTO]
            assert TentativaEnvioCanal.query.filter_by(numero_tentativa=2).count() == 1
            assert TentativaEnvioCanal.query.filter_by(numero_tentativa=3).count() == 0
            tentativa = TentativaEnvioCanal.query.filter_by(numero_tentativa=2).one()
            assert tentativa.status_tentativa == TentativaEnvioCanal.STATUS_ERRO
            assert tentativa.codigo_erro == EventoCanalSaida.CODIGO_HTTP_5XX
            assert tentativa.chave_recuperacao == chave
            assert {item.tentativa_id for item in resultados.values()} == {tentativa.id}
            db.session.remove()
            db.drop_all()
    finally:
        recuperacao._trava = trava_original
        adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = original


def test_replay_da_mesma_chave_depois_do_erro_nao_reenvia(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, [_http_500(), _http_500()])
    saida.enviar_texto_da_interpretacao(
        _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.REPLAY").id
    )
    saida_id = _tentativa(1).saida_id
    chave = _chave()
    primeiro = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
        chave,
    )
    assert primeiro.numero_tentativa == 2
    assert primeiro.status_tentativa == TentativaEnvioCanal.STATUS_ERRO
    segundo = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_OPERACAO_MANUAL,
        chave,
    )
    assert segundo.tentativa_id == primeiro.tentativa_id
    assert segundo.numero_tentativa == 2
    assert segundo.codigo == EventoCanalSaida.CODIGO_HTTP_5XX
    assert TentativaEnvioCanal.query.filter_by(numero_tentativa=3).count() == 0
    assert len(chamadas) == 2


def test_nova_chave_depois_do_erro_cria_tentativa_3(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, [_http_500(), _http_500(), _ok(MESSAGE_3)])
    saida.enviar_texto_da_interpretacao(
        _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.NOVA").id
    )
    saida_id = _tentativa(1).saida_id
    primeiro = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
        _chave(),
    )
    assert primeiro.numero_tentativa == 2
    assert primeiro.status_tentativa == TentativaEnvioCanal.STATUS_ERRO
    segundo = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_RESULTADO_INCERTO,
        _chave(),
    )
    assert segundo.numero_tentativa == 3
    assert segundo.codigo == EventoCanalSaida.STATUS_ACEITO
    assert segundo.provider_message_id == MESSAGE_3
    assert TentativaEnvioCanal.query.count() == 3
    assert len(chamadas) == 3


def test_chaves_diferentes_simultaneas_uma_reivindica(tmp_path):
    banco = tmp_path / "corrida_chaves.sqlite"
    uri = "sqlite:///" + banco.as_posix()
    flask_app = Flask("corrida-chaves-3e2")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 15},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    posts = []
    original = adapter.WhatsAppMetaCloudApiAdapter.enviar_texto
    trava_original = recuperacao._trava
    iniciou = threading.Event()
    liberar = threading.Event()
    try:
        with flask_app.app_context():
            import app.models  # noqa: F401

            db.create_all()
            _configurar_env()
            interpretacao = _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.DUAS.CHAVES")

            def _falha(self, **_kwargs):
                return ResultadoAdapterWhatsApp(codigo_erro=EventoCanalSaida.CODIGO_HTTP_5XX)

            adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = _falha
            enviado = saida.enviar_texto_da_interpretacao(interpretacao.id)
            assert enviado.codigo_erro == EventoCanalSaida.CODIGO_HTTP_5XX
            saida_id = int(EventoCanalSaida.query.one().id)
            chave_x = _chave()
            chave_y = _chave()

            def _envia(self, **kwargs):
                posts.append(kwargs["texto"])
                iniciou.set()
                assert liberar.wait(timeout=5)
                return ResultadoAdapterWhatsApp(provider_message_id=MESSAGE_2)

            adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = _envia
            db.session.remove()
            resultados = {}
            recuperacao._trava = lambda _saida_id: contextlib.nullcontext()

            def _primeira() -> None:
                try:
                    with flask_app.app_context():
                        resultados["x"] = recuperacao.recuperar_saida_textual(
                            saida_id,
                            TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
                            chave_x,
                        )
                        db.session.remove()
                except Exception as exc:
                    resultados["x"] = exc

            def _segunda() -> None:
                try:
                    assert iniciou.wait(timeout=5)
                    with flask_app.app_context():
                        resultados["y"] = recuperacao.recuperar_saida_textual(
                            saida_id,
                            TentativaEnvioCanal.MOTIVO_OPERACAO_MANUAL,
                            chave_y,
                        )
                        db.session.remove()
                except Exception as exc:
                    resultados["y"] = exc
                finally:
                    liberar.set()

            primeira = threading.Thread(target=_primeira)
            segunda = threading.Thread(target=_segunda)
            primeira.start()
            segunda.start()
            primeira.join(timeout=15)
            segunda.join(timeout=15)
            assert isinstance(resultados["x"], recuperacao.ResultadoRecuperacaoTexto)
            assert isinstance(resultados["y"], recuperacao.ResultadoRecuperacaoTexto)
            assert resultados["x"].numero_tentativa == 2
            assert resultados["x"].codigo == EventoCanalSaida.STATUS_ACEITO
            assert resultados["y"].codigo == recuperacao.CODIGO_CONFLITO
            assert posts == [TEXTO]
            assert TentativaEnvioCanal.query.filter_by(numero_tentativa=2).count() == 1
            assert TentativaEnvioCanal.query.filter_by(numero_tentativa=3).count() == 0
            db.session.remove()
            db.drop_all()
    finally:
        liberar.set()
        recuperacao._trava = trava_original
        adapter.WhatsAppMetaCloudApiAdapter.enviar_texto = original


def test_webhook_tardio_associa_cada_tentativa_e_bloqueia_a_terceira(ctx, monkeypatch):
    _aceita_e_falha(monkeypatch, MESSAGE_1)
    extra = _mock(monkeypatch, [_ok(MESSAGE_2)])
    saida_id = EventoCanalSaida.query.one().id
    recuperado = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_FALHA_ENTREGA_PROVIDER,
        _chave(),
    )
    assert recuperado.provider_message_id == MESSAGE_2
    tentativa_1 = _tentativa(1)
    tentativa_2 = _tentativa(2)
    entrega_1_antes = (
        _entrega_da(tentativa_1.id).status_entrega,
        _entrega_da(tentativa_1.id).sent_em,
        _entrega_da(tentativa_1.id).failed_em,
    )
    sent_1 = reconciliacao.reconciliar_status_saida(_status(MESSAGE_1, "sent", MARCA_SENT).id)
    assert sent_1.codigo == reconciliacao.CODIGO_REGISTRADO_SEM_AVANCO
    assert sent_1.saida_id == saida_id
    entrega_1 = _entrega_da(tentativa_1.id)
    assert entrega_1.sent_em is not None
    assert entrega_1.failed_em == entrega_1_antes[2]
    assert _entrega_da(tentativa_2.id) is None
    assert _tentativa(2).provider_message_id == MESSAGE_2
    assert _tentativa(2).status_tentativa == TentativaEnvioCanal.STATUS_ACEITA
    assert EventoCanalSaida.query.one().provider_message_id == MESSAGE_2
    assert EventoCanalSaida.query.one().status_envio == EventoCanalSaida.STATUS_ACEITO
    projecao = EstadoEntregaCanalSaida.query.filter_by(saida_id=saida_id).one()
    assert projecao.sent_em is None
    bloqueada = recuperacao.recuperar_saida_textual(
        saida_id,
        TentativaEnvioCanal.MOTIVO_OPERACAO_MANUAL,
        _chave(),
    )
    assert bloqueada.codigo == recuperacao.CODIGO_EVIDENCIA_POSITIVA
    assert TentativaEnvioCanal.query.count() == 2
    assert len(extra) == 1

    sent_2 = reconciliacao.reconciliar_status_saida(_status(MESSAGE_2, "sent", MARCA_SENT + 5).id)
    assert sent_2.codigo == reconciliacao.CODIGO_APLICADO
    assert _entrega_da(tentativa_2.id).sent_em is not None
    assert _entrega_da(tentativa_1.id).sent_em == entrega_1.sent_em
    assert AplicacaoStatusCanal.query.filter_by(tentativa_envio_id=tentativa_1.id).count() >= 1
    assert AplicacaoStatusCanal.query.filter_by(tentativa_envio_id=tentativa_2.id).count() == 1


def test_provider_id_ambiguo_nao_escolhe(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, [_ok(MESSAGE_1), _ok(MESSAGE_1)])
    saida.enviar_texto_da_interpretacao(_interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.AMB").id)
    reconciliacao.reconciliar_status_saida(_status(MESSAGE_1, "failed", MARCA_FAILED).id)
    recuperacao.recuperar_saida_textual(
        EventoCanalSaida.query.one().id,
        TentativaEnvioCanal.MOTIVO_FALHA_ENTREGA_PROVIDER,
        _chave(),
    )
    assert _tentativa(1).provider_message_id == MESSAGE_1
    assert _tentativa(2).provider_message_id == MESSAGE_1
    antes_1 = _foto(_tentativa(1))
    antes_2 = _foto(_tentativa(2))
    entrega_antes = _entrega_da(_tentativa(1).id).status_entrega
    resultado = reconciliacao.reconciliar_status_saida(_status(MESSAGE_1, "sent", MARCA_SENT).id)
    assert resultado.codigo == reconciliacao.CODIGO_SAIDA_AMBIGUA
    assert resultado.saida_id is None
    assert _foto(_recarregar(antes_1[0])) == antes_1
    assert _foto(_recarregar(antes_2[0])) == antes_2
    assert _entrega_da(_tentativa(1).id).status_entrega == entrega_antes
    assert _entrega_da(_tentativa(1).id).sent_em is None
    assert _entrega_da(_tentativa(2).id) is None


def test_link_seguro_nao_recupera_nem_emite_token(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, [_ok(MESSAGE_1)])

    def _emitir(*_args, **_kwargs):
        raise AssertionError("token")

    monkeypatch.setattr(
        "app.services.onboarding_canal_conclusao_service.emitir_link_conclusao_onboarding",
        _emitir,
    )
    monkeypatch.setattr(
        "app.services.onboarding_canal_conclusao_service.emitir_link_vinculo_conta_existente",
        _emitir,
    )
    enviado = saida.enviar_texto_da_interpretacao(
        _interpretacao(
            "Seguimos com o cadastro.",
            codigo=CODIGO_LINK_NAO_EMITIDO,
            acao=ACAO_EMITIR_SENHA,
            externo="wamid.ENTRADA.3E2.LINK",
        ).id
    )
    assert enviado.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    resultado = recuperacao.recuperar_saida_textual(
        enviado.saida_id,
        TentativaEnvioCanal.MOTIVO_OPERACAO_MANUAL,
        _chave(),
    )
    assert resultado.codigo == recuperacao.CODIGO_TIPO_NAO_SUPORTADO
    assert TentativaEnvioCanal.query.count() == 1
    assert chamadas == []
    assert OnboardingCanalConclusao.query.count() == 0


def test_sent_tardio_sem_provider_id_permanece_orfao(ctx, monkeypatch):
    _configurar(monkeypatch)

    def _timeout(*_args, **_kwargs):
        raise adapter.requests.Timeout()

    monkeypatch.setattr(adapter.requests, "post", _timeout)
    enviado = saida.enviar_texto_da_interpretacao(
        _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.TIMEOUT").id
    )
    tentativa = _tentativa(1)
    assert tentativa.codigo_erro == EventoCanalSaida.CODIGO_TIMEOUT
    assert tentativa.provider_message_id is None
    antes = _foto(tentativa)
    saida_antes = (
        EventoCanalSaida.query.one().status_envio,
        EventoCanalSaida.query.one().provider_message_id,
        EventoCanalSaida.query.one().codigo_erro,
    )
    orfao = reconciliacao.reconciliar_status_saida(
        _status("wamid.DESCONHECIDO.3E2", "sent", MARCA_SENT).id
    )
    assert orfao.codigo == reconciliacao.CODIGO_SEM_SAIDA
    assert orfao.saida_id is None
    assert _foto(_recarregar(antes[0])) == antes
    assert _entrega_da(tentativa.id) is None
    assert (
        EstadoEntregaTentativaCanal.query.filter(
            EstadoEntregaTentativaCanal.sent_em.isnot(None)
        ).count()
        == 0
    )
    projecao = EstadoEntregaCanalSaida.query.filter_by(saida_id=enviado.saida_id).one_or_none()
    assert projecao is None or (
        projecao.sent_em is None
        and projecao.delivered_em is None
        and projecao.read_em is None
    )
    saida_row = EventoCanalSaida.query.one()
    assert (
        saida_row.status_envio,
        saida_row.provider_message_id,
        saida_row.codigo_erro,
    ) == saida_antes
    aplicacao = AplicacaoStatusCanal.query.filter_by(
        resultado=AplicacaoStatusCanal.RESULTADO_SEM_SAIDA
    ).one()
    assert aplicacao.saida_id is None
    assert aplicacao.tentativa_envio_id is None

    def _ok_post(*_args, **_kwargs):
        return _ok(MESSAGE_2)

    monkeypatch.setattr(adapter.requests, "post", _ok_post)
    recuperado = recuperacao.recuperar_saida_textual(
        enviado.saida_id,
        TentativaEnvioCanal.MOTIVO_RESULTADO_INCERTO,
        _chave(),
    )
    assert recuperado.numero_tentativa == 2
    assert recuperado.codigo == EventoCanalSaida.STATUS_ACEITO
    assert _tentativa(1).codigo_erro == EventoCanalSaida.CODIGO_TIMEOUT
    assert _entrega_da(_tentativa(1).id) is None
    assert (
        EventoCanalSaida.query.one().provider_message_id == MESSAGE_2
    )
    db.session.expire_all()
    assert (
        AplicacaoStatusCanal.query.filter_by(id=aplicacao.id).one().resultado
        == AplicacaoStatusCanal.RESULTADO_SEM_SAIDA
    )
    assert (
        AplicacaoStatusCanal.query.filter_by(id=aplicacao.id).one().saida_id is None
    )


def test_configuracao_ausente_so_cria_tentativa_depois_da_config(ctx, monkeypatch):
    from app.services.whatsapp_meta_config import configuracao_envio as config_real

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, [_ok(MESSAGE_2)])
    monkeypatch.setattr(adapter, "configuracao_envio", lambda: None)
    enviado = saida.enviar_texto_da_interpretacao(
        _interpretacao(TEXTO, externo="wamid.ENTRADA.3E2.CONFIG").id
    )
    assert enviado.codigo_erro == EventoCanalSaida.CODIGO_CONFIGURACAO_AUSENTE
    assert TentativaEnvioCanal.query.one().codigo_erro == EventoCanalSaida.CODIGO_CONFIGURACAO_AUSENTE
    monkeypatch.setattr(recuperacao, "configuracao_envio", lambda: None)
    chave_config = _chave()
    bloqueado = recuperacao.recuperar_saida_textual(
        enviado.saida_id,
        TentativaEnvioCanal.MOTIVO_CONFIGURACAO_CORRIGIDA,
        chave_config,
    )
    assert bloqueado.codigo == recuperacao.CODIGO_CONFIGURACAO_AUSENTE
    assert TentativaEnvioCanal.query.count() == 1
    assert chamadas == []
    monkeypatch.setattr(adapter, "configuracao_envio", config_real)
    monkeypatch.setattr(recuperacao, "configuracao_envio", config_real)
    liberado = recuperacao.recuperar_saida_textual(
        enviado.saida_id,
        TentativaEnvioCanal.MOTIVO_CONFIGURACAO_CORRIGIDA,
        chave_config,
    )
    assert liberado.numero_tentativa == 2
    assert len(chamadas) == 1


def test_sem_segredo_billing_ia_scheduler_ou_token(ctx, monkeypatch, caplog):
    chamadas, _row = _erro_inicial(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        recuperacao.recuperar_saida_textual(
            EventoCanalSaida.query.one().id,
            TentativaEnvioCanal.MOTIVO_FALHA_HTTP,
            _chave(),
        )
    texto_banco = "\n".join(
        " ".join(str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns)
        for modelo in (
            TentativaEnvioCanal,
            EstadoEntregaCanalSaida,
            EstadoEntregaTentativaCanal,
            AplicacaoStatusCanal,
            EventoCanalSaida,
        )
        for row in modelo.query.all()
    )
    for proibido in (TOKEN, DESTINATARIO, TEXTO, MARCADOR, "Bearer", "Authorization", "https://"):
        assert proibido not in texto_banco
        assert proibido not in caplog.text
    nomes = {coluna.name for coluna in TentativaEnvioCanal.__table__.columns}
    for proibido in ("token", "url", "texto", "body", "authorization", "telefone", "destinatario"):
        assert all(proibido not in nome for nome in nomes)
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    assert OnboardingCanalConclusao.query.count() == 0
    fonte = Path(recuperacao.__file__).read_text(encoding="utf-8")
    for trecho in (
        "gemini",
        "GenerativeModel",
        "chat_julia",
        "apscheduler",
        "celery",
        "cleiton_monetizacao",
        "billing",
        "emitir_link",
    ):
        assert trecho not in fonte
    assert len(chamadas) == 2


def primeira_inalterada(antes) -> bool:
    return _foto(_recarregar(antes[0])) == antes


def _configurar_env():
    import os

    from app.services import whatsapp_meta_config as config

    os.environ[config.ENV_ACCESS_TOKEN] = TOKEN
    os.environ[config.ENV_GRAPH_API_VERSION] = "v26.0"
    os.environ[config.ENV_SEND_TIMEOUT_SECONDS] = "4"
    os.environ.pop(config.ENV_GRAPH_BASE_URL, None)
