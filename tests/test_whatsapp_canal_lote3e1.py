"""Lote WhatsApp 3E1: tentativa 1 persistida ao lado da saída lógica.

Não cobre retry, segunda tentativa, novo POST, novo segredo, worker nem fila.
"""
from __future__ import annotations

import importlib.util
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AplicacaoStatusCanal,
    EstadoEntregaCanalSaida,
    EventoCanalRecebido,
    EventoCanalSaida,
    InterpretacaoConversacionalCanal,
    OnboardingCanalConclusao,
    TentativaEnvioCanal,
    utcnow_naive,
)
from app.services import canal_reconciliacao_status_service as reconciliacao
from app.services import canal_saida_link_seguro_service as entrega
from app.services import canal_saida_whatsapp_service as saida
from app.services import canal_tentativa_envio_service as tentativas
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services import whatsapp_meta_config as config
from app.services.canal_aquisicao_service import (
    PROVEDOR_WHATSAPP_META,
    obter_ou_criar_identidade_externa,
)
from app.services.canal_interpretacao_conversacional_service import (
    ACAO_EMITIR_SENHA,
    CODIGO_LINK_EMITIDO,
    CODIGO_LINK_NAO_EMITIDO,
    TEXTO_EMAIL,
    TEXTO_SENHA,
)
from tests.test_whatsapp_canal_lote3d2a import (
    MESSAGE_ID as MESSAGE_LINK,
    TOKEN_META,
    _configurar as _configurar_link,
    _link_do_body,
    _mock as _mock_link,
    _ok as _ok_link,
    _preparar_nova,
    _token_do_link,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "w4x5y6z7a8b9_tentativa_envio_canal.py"

TOKEN = "EAAWHATSAPP3E1TOKENSECRETO"
VERSAO = "v26.0"
PHONE_NUMBER_ID = "106540352242922"
DESTINATARIO = "16505551234"
MESSAGE_ID = "wamid.HBgLACEITO3E1"
CORRELATION = "corr3e1texto01"
MARCADOR_CORPO = "CORPO_BRUTO_META_3E1"
URL_CONCLUSAO = "https://entrega.invalid/onboarding/canal/concluir/SEGREDO3E1"
MARCA_SENT = 1600000001


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_tentativa_envio", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configurar(monkeypatch):
    monkeypatch.setenv(config.ENV_ACCESS_TOKEN, TOKEN)
    monkeypatch.setenv(config.ENV_GRAPH_API_VERSION, VERSAO)
    monkeypatch.setenv(config.ENV_SEND_TIMEOUT_SECONDS, "4")
    monkeypatch.delenv(config.ENV_GRAPH_BASE_URL, raising=False)


class _Resposta:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = MARCADOR_CORPO

    def json(self):
        return self._payload

    def close(self):
        return None


def _mock(monkeypatch, efeito):
    chamadas = []

    def _post(url, **kwargs):
        chamadas.append((url, kwargs))
        return efeito

    monkeypatch.setattr(adapter.requests, "post", _post)
    return chamadas


def _ok(message_id=MESSAGE_ID):
    return _Resposta(
        200,
        {
            "messaging_product": "whatsapp",
            "messages": [{"id": message_id}],
            "corpo_bruto": MARCADOR_CORPO,
        },
    )


def _interpretacao(
    texto=TEXTO_EMAIL,
    *,
    codigo="resposta_registrada",
    acao="registrar_nome",
    etapa_final=None,
    conclusao_id=None,
    externo="wamid.ENTRADA.3E1",
):
    obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )
    evento = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=externo,
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
        correlation_id=CORRELATION,
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(evento)
    db.session.flush()
    row = InterpretacaoConversacionalCanal(
        evento_id=evento.id,
        codigo=codigo,
        acao=acao,
        etapa_final=etapa_final,
        texto_resposta=texto,
        conclusao_id=conclusao_id,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _unica() -> TentativaEnvioCanal:
    return TentativaEnvioCanal.query.one()


def _texto_tentativas() -> str:
    partes = []
    for row in TentativaEnvioCanal.query.all():
        partes.append(
            " ".join(str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns)
        )
    return "\n".join(partes)


def _status_entrega(message_id: str, marca: int) -> EventoCanalRecebido:
    row = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=f"{message_id}:sent:{marca}",
        tipo_evento=EventoCanalRecebido.TIPO_STATUS_ENTREGA,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id="corr3e1status",
        diagnostico_seguro="status_entrega:sent",
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_migration_backfill_cria_uma_tentativa_por_saida(tmp_path):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    modulo = _migration_module()
    assert modulo.revision == "w4x5y6z7a8b9"
    assert modulo.down_revision == "v3w4x5y6z7a8"
    assert modulo._SQL_NUMERO == "numero_tentativa = 1"
    assert TentativaEnvioCanal._SQL_NUMERO == (
        "numero_tentativa >= 1 AND numero_tentativa <= 3"
    )
    assert modulo._SQL_TIPO == TentativaEnvioCanal._SQL_TIPO
    assert modulo._SQL_ORIGEM == "origem_tentativa = 'envio_inicial'"
    assert TentativaEnvioCanal._SQL_ORIGEM == (
        "origem_tentativa IN ('envio_inicial', 'recuperacao_manual')"
    )
    assert modulo._SQL_STATUS == TentativaEnvioCanal._SQL_STATUS
    assert modulo._SQL_PROVIDER == TentativaEnvioCanal._SQL_PROVIDER
    assert modulo._SQL_ERRO == TentativaEnvioCanal._SQL_ERRO
    assert modulo._SQL_MENSAGEM == TentativaEnvioCanal._SQL_MENSAGEM
    assert modulo._SQL_CORRELATION == TentativaEnvioCanal._SQL_CORRELATION
    assert modulo._SQL_CONCLUSAO == TentativaEnvioCanal._SQL_CONCLUSAO
    assert modulo._SQL_CLASSIFICACAO == TentativaEnvioCanal._SQL_CLASSIFICACAO
    assert modulo._SQL_COERENCIA == TentativaEnvioCanal._SQL_COERENCIA
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
    assert "DROP TABLE evento_canal_saida" not in texto
    assert "drop_table(\"evento_canal_saida\")" not in texto

    banco = tmp_path / "tentativa_envio.sqlite"
    engine = create_engine(f"sqlite:///{banco}")
    meta = MetaData()
    interpretacao = Table(
        "interpretacao_conversacional_canal",
        meta,
        Column("id", Integer, primary_key=True),
        Column("acao", String(64), nullable=False),
        Column("codigo", String(64), nullable=False),
        Column("etapa_final", String(40)),
        Column("conclusao_id", Integer),
    )
    saidas = Table(
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
    estados = Table(
        "estado_entrega_canal_saida",
        meta,
        Column("id", Integer, primary_key=True),
        Column("saida_id", Integer, nullable=False),
        Column("classificacao_resultado", String(32)),
    )
    meta.create_all(engine)
    criado = datetime(2026, 10, 2, 12, 0, 0)
    enviado = datetime(2026, 10, 2, 12, 5, 0)
    with engine.begin() as conn:
        conn.execute(
            interpretacao.insert(),
            [
                {
                    "id": 1,
                    "acao": "registrar_nome",
                    "codigo": "resposta_registrada",
                    "etapa_final": None,
                    "conclusao_id": None,
                },
                {
                    "id": 2,
                    "acao": "emitir_link_senha",
                    "codigo": "link_emitido",
                    "etapa_final": "aguardando_senha",
                    "conclusao_id": 99,
                },
                {
                    "id": 3,
                    "acao": "emitir_link_senha",
                    "codigo": "link_emitido",
                    "etapa_final": "aguardando_senha",
                    "conclusao_id": None,
                },
                {
                    "id": 4,
                    "acao": "registrar_nome",
                    "codigo": "resposta_registrada",
                    "etapa_final": None,
                    "conclusao_id": None,
                },
                {
                    "id": 5,
                    "acao": "registrar_nome",
                    "codigo": "resposta_registrada",
                    "etapa_final": None,
                    "conclusao_id": None,
                },
                {
                    "id": 6,
                    "acao": "registrar_nome",
                    "codigo": "resposta_registrada",
                    "etapa_final": None,
                    "conclusao_id": None,
                },
                {
                    "id": 7,
                    "acao": "orientar_conta_existente",
                    "codigo": "existing_account_verification_required",
                    "etapa_final": None,
                    "conclusao_id": None,
                },
                {
                    "id": 8,
                    "acao": "registrar_nome",
                    "codigo": "resposta_registrada",
                    "etapa_final": "aguardando_senha",
                    "conclusao_id": 77,
                },
                {
                    "id": 9,
                    "acao": "emitir_link_senha",
                    "codigo": "link_nao_emitido",
                    "etapa_final": "aguardando_senha",
                    "conclusao_id": None,
                },
            ],
        )
        conn.execute(
            saidas.insert(),
            [
                {
                    "id": 1,
                    "interpretacao_id": 1,
                    "status_envio": "reservado",
                    "provider": "meta_whatsapp",
                    "provider_message_id": None,
                    "codigo_erro": None,
                    "conclusao_id": None,
                    "enviado_em": None,
                    "criado_em": criado,
                    "correlation_id": "corr-reservado",
                },
                {
                    "id": 2,
                    "interpretacao_id": 2,
                    "status_envio": "aguardando_link_seguro",
                    "provider": "meta_whatsapp",
                    "provider_message_id": None,
                    "codigo_erro": None,
                    "conclusao_id": None,
                    "enviado_em": None,
                    "criado_em": criado,
                    "correlation_id": "corr-aguardando",
                },
                {
                    "id": 3,
                    "interpretacao_id": 3,
                    "status_envio": "preparando_link",
                    "provider": "meta_whatsapp",
                    "provider_message_id": None,
                    "codigo_erro": None,
                    "conclusao_id": 4,
                    "enviado_em": None,
                    "criado_em": criado,
                    "correlation_id": "corr-preparando",
                },
                {
                    "id": 4,
                    "interpretacao_id": 4,
                    "status_envio": "aceito_provider",
                    "provider": "meta_whatsapp",
                    "provider_message_id": "wamid.TEXTO",
                    "codigo_erro": None,
                    "conclusao_id": None,
                    "enviado_em": enviado,
                    "criado_em": criado,
                    "correlation_id": "corr-aceito",
                },
                {
                    "id": 5,
                    "interpretacao_id": 5,
                    "status_envio": "erro",
                    "provider": "meta_whatsapp",
                    "provider_message_id": None,
                    "codigo_erro": "timeout",
                    "conclusao_id": None,
                    "enviado_em": None,
                    "criado_em": criado,
                    "correlation_id": "corr-erro",
                },
                {
                    "id": 6,
                    "interpretacao_id": 6,
                    "status_envio": "reservado",
                    "provider": "meta_whatsapp",
                    "provider_message_id": None,
                    "codigo_erro": None,
                    "conclusao_id": None,
                    "enviado_em": None,
                    "criado_em": criado,
                    "correlation_id": "corr-incerto",
                },
                {
                    "id": 7,
                    "interpretacao_id": 7,
                    "status_envio": "aceito_provider",
                    "provider": "meta_whatsapp",
                    "provider_message_id": "wamid.LINK",
                    "codigo_erro": None,
                    "conclusao_id": 8,
                    "enviado_em": enviado,
                    "criado_em": criado,
                    "correlation_id": "corr-link",
                },
                {
                    "id": 8,
                    "interpretacao_id": 8,
                    "status_envio": "aceito_provider",
                    "provider": "meta_whatsapp",
                    "provider_message_id": "wamid.ETAPA",
                    "codigo_erro": None,
                    "conclusao_id": None,
                    "enviado_em": enviado,
                    "criado_em": criado,
                    "correlation_id": "corr-etapa",
                },
                {
                    "id": 9,
                    "interpretacao_id": 9,
                    "status_envio": "preparando_link",
                    "provider": "meta_whatsapp",
                    "provider_message_id": None,
                    "codigo_erro": None,
                    "conclusao_id": 5,
                    "enviado_em": None,
                    "criado_em": criado,
                    "correlation_id": "corr-prep-incerto",
                },
            ],
        )
        conn.execute(
            estados.insert(),
            [
                {"saida_id": 6, "classificacao_resultado": "resultado_incerto"},
                {"saida_id": 9, "classificacao_resultado": "resultado_incerto"},
            ],
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
        linhas = {
            row["saida_id"]: row
            for row in conn.execute(text("SELECT * FROM tentativa_envio_canal")).mappings()
        }
        memoria = [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT id, status_envio, provider_message_id, codigo_erro, "
                    "conclusao_id FROM evento_canal_saida ORDER BY id"
                )
            )
        ]
    assert len(linhas) == 9
    assert {linha["numero_tentativa"] for linha in linhas.values()} == {1}
    assert {linha["origem_tentativa"] for linha in linhas.values()} == {"envio_inicial"}

    reservada = linhas[1]
    assert reservada["tipo_tentativa"] == "texto_comum"
    assert reservada["status_tentativa"] == "reservada"
    assert reservada["provider_message_id"] is None
    assert reservada["conclusao_id"] is None
    assert reservada["codigo_erro"] is None
    assert reservada["classificacao_resultado"] is None
    assert reservada["preparado_em"] is None
    assert reservada["enviado_em"] is None
    assert reservada["finalizado_em"] is None

    aguardando = linhas[2]
    assert aguardando["tipo_tentativa"] == "link_seguro"
    assert aguardando["status_tentativa"] == "aguardando_link_seguro"
    assert aguardando["conclusao_id"] is None
    assert aguardando["provider_message_id"] is None

    preparando = linhas[3]
    assert preparando["tipo_tentativa"] == "link_seguro"
    assert preparando["status_tentativa"] == "preparando"
    assert preparando["conclusao_id"] == 4
    assert preparando["provider_message_id"] is None
    assert preparando["preparado_em"] is None
    assert preparando["finalizado_em"] is None

    aceita = linhas[4]
    assert aceita["tipo_tentativa"] == "texto_comum"
    assert aceita["status_tentativa"] == "aceita_provider"
    assert aceita["provider_message_id"] == "wamid.TEXTO"
    assert aceita["conclusao_id"] is None
    assert aceita["enviado_em"] is not None
    assert aceita["finalizado_em"] is not None

    erro = linhas[5]
    assert erro["tipo_tentativa"] == "texto_comum"
    assert erro["status_tentativa"] == "erro"
    assert erro["codigo_erro"] == "timeout"
    assert erro["provider_message_id"] is None
    assert erro["conclusao_id"] is None
    assert erro["finalizado_em"] is None

    incerta = linhas[6]
    assert incerta["status_tentativa"] == "reservada"
    assert incerta["classificacao_resultado"] == "resultado_incerto"
    assert incerta["provider_message_id"] is None

    link = linhas[7]
    assert link["tipo_tentativa"] == "link_seguro"
    assert link["conclusao_id"] == 8
    assert link["provider_message_id"] == "wamid.LINK"

    etapa = linhas[8]
    assert etapa["tipo_tentativa"] == "link_seguro"
    assert etapa["conclusao_id"] is None
    assert etapa["provider_message_id"] == "wamid.ETAPA"

    preparo_incerto = linhas[9]
    assert preparo_incerto["tipo_tentativa"] == "link_seguro"
    assert preparo_incerto["status_tentativa"] == "preparando"
    assert preparo_incerto["conclusao_id"] == 5
    assert preparo_incerto["classificacao_resultado"] == "resultado_incerto"

    indices = {item["name"] for item in inspect(engine).get_indexes("tentativa_envio_canal")}
    assert "ix_tentativa_envio_canal_provider_message_id" in indices

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tentativa_envio_canal ("
                    "saida_id, numero_tentativa, tipo_tentativa, origem_tentativa, "
                    "status_tentativa, provider, criado_em, correlation_id"
                    ") VALUES (1, 2, 'texto_comum', 'envio_inicial', "
                    "'reservada', 'meta_whatsapp', :criado, 'corr-extra')"
                ),
                {"criado": criado},
            )
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM tentativa_envio_canal WHERE saida_id = 1"))
            conn.execute(
                text(
                    "INSERT INTO tentativa_envio_canal ("
                    "saida_id, numero_tentativa, tipo_tentativa, origem_tentativa, "
                    "status_tentativa, provider, criado_em, correlation_id"
                    ") VALUES (1, 1, 'texto_comum', 'recuperacao_manual', "
                    "'reservada', 'meta_whatsapp', :criado, 'corr-extra')"
                ),
                {"criado": criado},
            )
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tentativa_envio_canal")).scalar() == 9

    _run(modulo.downgrade)
    assert "tentativa_envio_canal" not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        restantes = [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT id, status_envio, provider_message_id, codigo_erro, "
                    "conclusao_id FROM evento_canal_saida ORDER BY id"
                )
            )
        ]
    assert restantes == memoria


def test_saida_textual_nova_cria_tentativa_1(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    tentativa = _unica()
    assert resultado.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert tentativa.saida_id == resultado.saida_id
    assert tentativa.numero_tentativa == 1
    assert tentativa.tipo_tentativa == TentativaEnvioCanal.TIPO_TEXTO_COMUM
    assert tentativa.origem_tentativa == TentativaEnvioCanal.ORIGEM_ENVIO_INICIAL
    assert tentativa.status_tentativa == TentativaEnvioCanal.STATUS_ACEITA
    assert tentativa.provider_message_id == MESSAGE_ID
    assert tentativa.conclusao_id is None
    assert tentativa.codigo_erro is None
    assert tentativa.enviado_em is not None
    assert tentativa.finalizado_em == tentativa.enviado_em
    assert len(chamadas) == 1
    assert TentativaEnvioCanal.query.count() == 1


def test_link_novo_cria_tentativa_1(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    resultado = saida.enviar_texto_da_interpretacao(
        _interpretacao(
            TEXTO_SENHA,
            codigo=CODIGO_LINK_NAO_EMITIDO,
            acao=ACAO_EMITIR_SENHA,
            externo="wamid.ENTRADA.3E1.LINK",
        ).id
    )
    tentativa = _unica()
    assert chamadas == []
    assert resultado.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert tentativa.tipo_tentativa == TentativaEnvioCanal.TIPO_LINK_SEGURO
    assert tentativa.status_tentativa == TentativaEnvioCanal.STATUS_AGUARDANDO_LINK
    assert tentativa.numero_tentativa == 1
    assert tentativa.conclusao_id is None
    assert tentativa.provider_message_id is None
    assert tentativa.preparado_em is None


def test_replay_nao_cria_segunda_tentativa(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    interpretacao = _interpretacao()
    primeiro = saida.enviar_texto_da_interpretacao(interpretacao.id)
    segundo = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert segundo.saida_id == primeiro.saida_id
    assert len(chamadas) == 1
    assert TentativaEnvioCanal.query.count() == 1
    assert _unica().numero_tentativa == 1


def test_reservado_mapeia_tentativa_sem_provider(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = []

    def _estoura(self, **_kwargs):
        chamadas.append("post")
        raise RuntimeError("queda-depois-da-reserva")

    monkeypatch.setattr(
        "app.services.canal_saida_whatsapp_service.WhatsAppMetaCloudApiAdapter.enviar_texto",
        _estoura,
    )
    with pytest.raises(RuntimeError):
        saida.enviar_texto_da_interpretacao(_interpretacao().id)
    row = EventoCanalSaida.query.one()
    tentativa = _unica()
    assert row.status_envio == EventoCanalSaida.STATUS_RESERVADO
    assert tentativa.status_tentativa == TentativaEnvioCanal.STATUS_RESERVADA
    assert tentativa.tipo_tentativa == TentativaEnvioCanal.TIPO_TEXTO_COMUM
    assert tentativa.provider_message_id is None
    assert tentativa.codigo_erro is None
    assert tentativa.conclusao_id is None
    assert tentativa.enviado_em is None
    assert tentativa.finalizado_em is None
    assert tentativa.preparado_em is None
    repetido = saida.enviar_texto_da_interpretacao(row.interpretacao_id)
    assert repetido.status_envio == EventoCanalSaida.STATUS_RESERVADO
    assert chamadas == ["post"]
    assert TentativaEnvioCanal.query.count() == 1


def test_aguardando_link_mapeia_tentativa(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    saida.enviar_texto_da_interpretacao(
        _interpretacao(
            "Seguimos com o cadastro.",
            codigo=CODIGO_LINK_EMITIDO,
            acao="registrar_nome",
        ).id
    )
    tentativa = _unica()
    assert tentativa.status_tentativa == TentativaEnvioCanal.STATUS_AGUARDANDO_LINK
    assert tentativa.tipo_tentativa == TentativaEnvioCanal.TIPO_LINK_SEGURO
    assert tentativa.conclusao_id is None


def test_atualizacao_3d1_sincroniza_aceite_e_erro(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    aceito = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    tentativa = _unica()
    assert tentativa.status_tentativa == TentativaEnvioCanal.STATUS_ACEITA
    assert tentativa.provider_message_id == aceito.provider_message_id == MESSAGE_ID
    assert tentativa.conclusao_id is None

    _mock(monkeypatch, _Resposta(400, {"error": "segredo-4xx", "corpo": MARCADOR_CORPO}))
    saida.enviar_texto_da_interpretacao(
        _interpretacao(externo="wamid.ENTRADA.3E1.ERRO").id
    )
    erro = TentativaEnvioCanal.query.filter_by(
        status_tentativa=TentativaEnvioCanal.STATUS_ERRO
    ).one()
    assert erro.codigo_erro == EventoCanalSaida.CODIGO_HTTP_4XX
    assert erro.provider_message_id is None
    assert erro.conclusao_id is None
    assert erro.finalizado_em is not None
    assert erro.enviado_em is None
    assert TentativaEnvioCanal.query.count() == 2


def test_entrega_3d2a_sincroniza_preparo_e_aceite(ctx, app, monkeypatch):
    _configurar_link(monkeypatch)
    visto = {}

    def _ao_chamar():
        tentativa = TentativaEnvioCanal.query.one()
        visto["status"] = tentativa.status_tentativa
        visto["conclusao_id"] = tentativa.conclusao_id
        visto["preparado_em"] = tentativa.preparado_em
        visto["tipo"] = tentativa.tipo_tentativa

    chamadas = _mock_link(monkeypatch, _ok_link(), ao_chamar=_ao_chamar)
    _antiga, _token_antigo, pendente = _preparar_nova(app)
    assert TentativaEnvioCanal.query.one().status_tentativa == (
        TentativaEnvioCanal.STATUS_AGUARDANDO_LINK
    )
    resultado = entrega.entregar_link_seguro(pendente.id)
    assert visto["status"] == TentativaEnvioCanal.STATUS_PREPARANDO
    assert visto["tipo"] == TentativaEnvioCanal.TIPO_LINK_SEGURO
    assert visto["conclusao_id"] == resultado.conclusao_id
    assert visto["preparado_em"] is not None
    tentativa = _unica()
    assert tentativa.status_tentativa == TentativaEnvioCanal.STATUS_ACEITA
    assert tentativa.provider_message_id == MESSAGE_LINK
    assert tentativa.conclusao_id == resultado.conclusao_id
    assert tentativa.conclusao_id is not None
    assert tentativa.enviado_em is not None
    assert tentativa.finalizado_em == tentativa.enviado_em
    assert tentativa.preparado_em == visto["preparado_em"]
    conclusoes = OnboardingCanalConclusao.query.count()
    de_novo = entrega.entregar_link_seguro(pendente.id)
    assert de_novo.saida_id == resultado.saida_id
    assert len(chamadas) == 1
    assert OnboardingCanalConclusao.query.count() == conclusoes
    assert TentativaEnvioCanal.query.count() == 1
    link = _link_do_body(chamadas)
    token = _token_do_link(link)
    assert token not in _texto_tentativas()
    assert link not in _texto_tentativas()
    assert TOKEN_META not in _texto_tentativas()


def test_resultado_incerto_nao_autoriza_retry(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = []

    def _estoura(self, **_kwargs):
        chamadas.append("post")
        raise RuntimeError("queda-depois-da-reserva")

    monkeypatch.setattr(
        "app.services.canal_saida_whatsapp_service.WhatsAppMetaCloudApiAdapter.enviar_texto",
        _estoura,
    )
    interpretacao = _interpretacao()
    with pytest.raises(RuntimeError):
        saida.enviar_texto_da_interpretacao(interpretacao.id)
    saida_id = EventoCanalSaida.query.one().id
    classificado = reconciliacao.classificar_resultado_incerto(saida_id)
    assert classificado.codigo == reconciliacao.CODIGO_CLASSIFICADO
    tentativa = _unica()
    assert tentativa.classificacao_resultado == (
        TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO
    )
    assert tentativa.status_tentativa == TentativaEnvioCanal.STATUS_RESERVADA
    assert tentativa.numero_tentativa == 1
    repetido = saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert repetido.status_envio == EventoCanalSaida.STATUS_RESERVADO
    assert chamadas == ["post"]
    assert TentativaEnvioCanal.query.count() == 1
    assert _unica().classificacao_resultado == (
        TentativaEnvioCanal.CLASSIFICACAO_RESULTADO_INCERTO
    )


def test_falha_ao_criar_tentativa_nao_deixa_saida(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())

    def _falha(_row):
        raise IntegrityError("INSERT", {}, Exception("uq"))

    monkeypatch.setattr(
        "app.services.canal_saida_whatsapp_service.anexar_tentativa_inicial",
        _falha,
    )
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert resultado.saida_id is None
    assert EventoCanalSaida.query.count() == 0
    assert TentativaEnvioCanal.query.count() == 0
    assert chamadas == []


def test_concorrencia_na_criacao_inicial_nao_duplica(ctx):
    evento = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id="wamid.ENTRADA.3E1.CORRIDA",
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
        correlation_id=CORRELATION,
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(evento)
    db.session.flush()
    interpretacao = InterpretacaoConversacionalCanal(
        evento_id=evento.id,
        codigo="resposta_registrada",
        acao="registrar_nome",
        texto_resposta=TEXTO_EMAIL,
    )
    db.session.add(interpretacao)
    db.session.flush()
    row = EventoCanalSaida(
        evento_entrada_id=evento.id,
        interpretacao_id=interpretacao.id,
        provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
        chave_idempotencia=EventoCanalSaida.chave_resposta_principal(evento.id),
        status_envio=EventoCanalSaida.STATUS_RESERVADO,
        criado_em=utcnow_naive(),
        correlation_id=CORRELATION,
    )
    db.session.add(row)
    db.session.commit()
    saida_id = int(row.id)
    primeira = tentativas.garantir_tentativa_inicial(saida_id)
    assert primeira is not None
    primeira_id = int(primeira.id)
    db.session.remove()

    chamadas = {"n": 0}
    original = tentativas._buscar_inicial

    def _buscar(valor):
        chamadas["n"] += 1
        if chamadas["n"] == 1:
            return None
        return original(valor)

    tentativas._buscar_inicial = _buscar
    try:
        segunda = tentativas.garantir_tentativa_inicial(saida_id)
    finally:
        tentativas._buscar_inicial = original
    assert segunda is not None
    assert int(segunda.id) == primeira_id
    assert chamadas["n"] >= 2
    assert TentativaEnvioCanal.query.count() == 1
    assert EventoCanalSaida.query.one().status_envio == EventoCanalSaida.STATUS_RESERVADO


def test_corrida_de_duas_threads_deixa_uma_tentativa(tmp_path):
    banco = tmp_path / "corrida_tentativa.sqlite"
    uri = "sqlite:///" + banco.as_posix()
    flask_app = Flask("corrida-tentativa-3e1")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 5},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        db.create_all()
        evento = EventoCanalRecebido(
            provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
            evento_externo_id="wamid.ENTRADA.3E1.THREADS",
            tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
            sujeito_externo=DESTINATARIO,
            contexto_destino=PHONE_NUMBER_ID,
            recebido_em=utcnow_naive(),
            status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
            correlation_id=CORRELATION,
            diagnostico_seguro="mensagem_textual",
        )
        db.session.add(evento)
        db.session.flush()
        interpretacao = InterpretacaoConversacionalCanal(
            evento_id=evento.id,
            codigo="resposta_registrada",
            acao="registrar_nome",
            texto_resposta=TEXTO_EMAIL,
        )
        db.session.add(interpretacao)
        db.session.flush()
        row = EventoCanalSaida(
            evento_entrada_id=evento.id,
            interpretacao_id=interpretacao.id,
            provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
            chave_idempotencia=EventoCanalSaida.chave_resposta_principal(evento.id),
            status_envio=EventoCanalSaida.STATUS_RESERVADO,
            criado_em=utcnow_naive(),
            correlation_id=CORRELATION,
        )
        db.session.add(row)
        db.session.commit()
        saida_id = int(row.id)
        db.session.remove()
        barreira = threading.Barrier(2)
        resultados: dict[int, object] = {}

        def _worker(indice: int) -> None:
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    resultados[indice] = tentativas.garantir_tentativa_inicial(saida_id)
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = exc

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert set(resultados) == {1, 2}
        falhas = {
            indice: item
            for indice, item in resultados.items()
            if not isinstance(item, TentativaEnvioCanal)
        }
        assert falhas == {}
        assert resultados[1].id == resultados[2].id
        assert TentativaEnvioCanal.query.count() == 1
        assert EventoCanalSaida.query.count() == 1
        db.session.remove()
        db.drop_all()


def test_banco_da_tentativa_nao_guarda_segredo(ctx, app, monkeypatch):
    _configurar_link(monkeypatch)
    chamadas = _mock_link(monkeypatch, _ok_link())
    _antiga, _token_antigo, pendente = _preparar_nova(app)
    entrega.entregar_link_seguro(pendente.id)
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    saida.enviar_texto_da_interpretacao(
        _interpretacao(externo="wamid.ENTRADA.3E1.TEXTO").id
    )
    link = _link_do_body(chamadas)
    token = _token_do_link(link)
    texto = _texto_tentativas()
    nomes = {coluna.name for coluna in TentativaEnvioCanal.__table__.columns}
    for proibido in (
        "token",
        "url",
        "texto",
        "body",
        "authorization",
        "telefone",
        "destinatario",
        "payload",
    ):
        assert all(proibido not in nome for nome in nomes)
    for proibido in (
        TOKEN,
        TOKEN_META,
        DESTINATARIO,
        TEXTO_EMAIL,
        TEXTO_SENHA,
        MARCADOR_CORPO,
        URL_CONCLUSAO,
        token,
        link,
        "Bearer",
        "Authorization",
    ):
        assert proibido not in texto


def test_reconciliacao_3d2b_continua_pela_saida(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    resultado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    antes = (
        _unica().id,
        _unica().provider_message_id,
        _unica().status_tentativa,
        _unica().numero_tentativa,
    )
    evento = _status_entrega(MESSAGE_ID, MARCA_SENT)
    reconciliado = reconciliacao.reconciliar_status_saida(evento.id)
    assert reconciliado.codigo == reconciliacao.CODIGO_APLICADO
    assert reconciliado.saida_id == resultado.saida_id
    assert reconciliado.status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
    estado = EstadoEntregaCanalSaida.query.filter_by(saida_id=resultado.saida_id).one()
    assert estado.sent_em == datetime.fromtimestamp(MARCA_SENT, timezone.utc).replace(tzinfo=None)
    assert EventoCanalSaida.query.one().provider_message_id == MESSAGE_ID
    assert EventoCanalSaida.query.one().status_envio == EventoCanalSaida.STATUS_ACEITO
    assert (_unica().id, _unica().provider_message_id, _unica().status_tentativa, _unica().numero_tentativa) == antes
    assert TentativaEnvioCanal.query.count() == 1
    assert AplicacaoStatusCanal.query.count() == 1
    assert len(chamadas) == 1
    assert OnboardingCanalConclusao.query.count() == 0


def test_reconciliacao_3d2b1_reaplica_orfao_sem_nova_tentativa_de_envio(ctx, monkeypatch):
    _configurar(monkeypatch)
    evento = _status_entrega(MESSAGE_ID, MARCA_SENT)
    primeiro = reconciliacao.reconciliar_status_saida(evento.id)
    assert primeiro.codigo == reconciliacao.CODIGO_SEM_SAIDA
    assert TentativaEnvioCanal.query.count() == 0
    chamadas = _mock(monkeypatch, _ok())
    enviado = saida.enviar_texto_da_interpretacao(_interpretacao().id)
    assert enviado.provider_message_id == MESSAGE_ID
    reaplicado = reconciliacao.reconciliar_status_orfao(evento.id)
    assert reaplicado.codigo == reconciliacao.CODIGO_APLICADO
    assert reaplicado.saida_id == enviado.saida_id
    assert EstadoEntregaCanalSaida.query.one().status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
    assert TentativaEnvioCanal.query.count() == 1
    assert _unica().provider_message_id == MESSAGE_ID
    assert _unica().numero_tentativa == 1
    assert AplicacaoStatusCanal.query.count() == 2
    assert len(chamadas) == 1
    assert OnboardingCanalConclusao.query.count() == 0


def test_texto_comum_nao_copia_conclusao_historica(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, _ok())
    agora = utcnow_naive()
    obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )
    from app.models import OnboardingCanal
    from app.services.canal_aquisicao_service import iniciar_onboarding_canal

    ident = obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )
    iniciar_onboarding_canal(ident.id, commit=True)
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    conclusao = OnboardingCanalConclusao(
        onboarding_id=jornada.id,
        finalidade=OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA,
        token_hash="ab" * 32,
        estado=OnboardingCanalConclusao.ESTADO_EMITIDO,
        emitido_em=agora,
        expira_em=agora + timedelta(hours=1),
        atualizada_em=agora,
    )
    db.session.add(conclusao)
    db.session.commit()
    interpretacao = _interpretacao(conclusao_id=None)
    assert interpretacao.conclusao_id is None
    saida.enviar_texto_da_interpretacao(interpretacao.id)
    assert _unica().tipo_tentativa == TentativaEnvioCanal.TIPO_TEXTO_COMUM
    assert _unica().conclusao_id is None
    assert "ab" * 32 not in _texto_tentativas()
