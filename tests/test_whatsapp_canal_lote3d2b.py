"""Lote WhatsApp 3D2-B: reconciliação de status Meta com a saída.

Relaciona sent, delivered e read pelo provider_message_id já gravado.
Não cobre reenvio, novo token, modelo, cobrança nem Growth.
"""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask
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
    InterpretacaoConversacionalCanal,
    MonetizacaoFato,
    OnboardingCanal,
    OnboardingCanalConclusao,
    utcnow_naive,
)
from app.services import canal_reconciliacao_status_service as reconciliacao
from app.services.canal_aquisicao_service import (
    PROVEDOR_WHATSAPP_META,
    iniciar_onboarding_canal,
    obter_ou_criar_identidade_externa,
)
from tests.conftest import PYTEST_DISPOSABLE_SQLALCHEMY_URI

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "u2v3w4x5y6z7_reconciliacao_status_canal.py"

MESSAGE_ID = "wamid.HBgL:3D2B"
OUTRO_ID = "wamid.OUTRO:3D2B"
TELEFONE = "16505551234"
PHONE_NUMBER_ID = "106540352242922"
CORRELATION = "corrstat3d2b"
TOKEN_BRUTO = "segredo-bruto-3d2b-nao-gravar"
HASH_CONCLUSAO = hashlib.sha256(TOKEN_BRUTO.encode("utf-8")).hexdigest()
CORPO_BRUTO = "CORPO_BRUTO_META_131026"
MARCA_SENT = 1600000000
MARCA_SENT_TARDE = 1600000099
MARCA_DELIVERED = 1600000060
MARCA_READ = 1600000120
MARCA_FAILED = 1600000180


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_reconciliacao_status", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _em(segundos: int) -> datetime:
    return datetime.fromtimestamp(segundos, timezone.utc).replace(tzinfo=None)


def _texto(*modelos) -> str:
    partes = []
    for modelo in modelos:
        for row in modelo.query.all():
            partes.append(
                " ".join(str(getattr(row, coluna.name) or "") for coluna in row.__table__.columns)
            )
    return "\n".join(partes)


def _proibir_nos_novos(*trechos: str) -> None:
    texto = _texto(EstadoEntregaCanalSaida, AplicacaoStatusCanal)
    for trecho in trechos:
        assert trecho not in texto


def _entrada() -> EventoCanalRecebido:
    marca = f"wamid.ENTRADA.{EventoCanalRecebido.query.count() + 1}"
    row = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=marca,
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=TELEFONE,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
        correlation_id="correntrada",
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(row)
    db.session.flush()
    return row


def _interpretacao(evento: EventoCanalRecebido, conclusao_id: int | None = None):
    row = InterpretacaoConversacionalCanal(
        evento_id=int(evento.id),
        codigo="resposta_registrada",
        acao="registrar_nome",
        conclusao_id=conclusao_id,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _saida(
    *,
    status: str,
    message_id: str | None,
    conclusao_id: int | None = None,
    enviado_em: datetime | None = None,
) -> EventoCanalSaida:
    evento = _entrada()
    interpretacao = _interpretacao(evento, conclusao_id=conclusao_id)
    row = EventoCanalSaida(
        evento_entrada_id=int(evento.id),
        interpretacao_id=int(interpretacao.id),
        provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
        chave_idempotencia=EventoCanalSaida.chave_resposta_principal(int(evento.id)),
        status_envio=status,
        provider_message_id=message_id,
        codigo_erro=None,
        criado_em=utcnow_naive(),
        enviado_em=enviado_em,
        correlation_id="corrsaida3d2b",
        conclusao_id=conclusao_id,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _aceita(message_id: str = MESSAGE_ID, conclusao_id: int | None = None) -> EventoCanalSaida:
    return _saida(
        status=EventoCanalSaida.STATUS_ACEITO,
        message_id=message_id,
        conclusao_id=conclusao_id,
        enviado_em=utcnow_naive(),
    )


def _status(
    status: str,
    marca: int,
    *,
    message_id: str = MESSAGE_ID,
    diagnostico: str | None = None,
    externo: str | None = None,
) -> EventoCanalRecebido:
    row = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=externo or f"{message_id}:{status}:{marca}",
        tipo_evento=EventoCanalRecebido.TIPO_STATUS_ENTREGA,
        sujeito_externo=TELEFONE,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id=CORRELATION,
        diagnostico_seguro=diagnostico or f"status_entrega:{status}",
    )
    db.session.add(row)
    db.session.commit()
    return row


def _estado(saida_id: int) -> EstadoEntregaCanalSaida:
    row = EstadoEntregaCanalSaida.query.filter_by(saida_id=saida_id).one()
    return row


def _foto(saida_id: int) -> tuple:
    row = db.session.get(EventoCanalSaida, saida_id)
    return (
        row.status_envio,
        row.provider_message_id,
        row.codigo_erro,
        row.enviado_em,
        row.conclusao_id,
    )


def _conclusao() -> OnboardingCanalConclusao:
    ident = obter_ou_criar_identidade_externa(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=TELEFONE,
        contexto_destino=PHONE_NUMBER_ID,
        commit=True,
    )
    iniciar_onboarding_canal(ident.id, commit=True)
    jornada = OnboardingCanal.query.filter_by(identidade_id=ident.id).one()
    agora = utcnow_naive()
    row = OnboardingCanalConclusao(
        onboarding_id=int(jornada.id),
        finalidade=OnboardingCanalConclusao.FINALIDADE_DEFINIR_SENHA,
        token_hash=HASH_CONCLUSAO,
        estado=OnboardingCanalConclusao.ESTADO_EMITIDO,
        emitido_em=agora,
        expira_em=agora,
        atualizada_em=agora,
    )
    db.session.add(row)
    db.session.commit()
    return row


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


def test_migration_separa_entrega_da_aceitacao_http():
    modulo = _migration_module()
    assert modulo.revision == "u2v3w4x5y6z7"
    assert modulo.down_revision == "t1u2v3w4x5y6"
    assert modulo._SQL_ESTADO_VERSAO == EstadoEntregaCanalSaida._SQL_VERSAO
    assert modulo._SQL_ESTADO_STATUS == EstadoEntregaCanalSaida._SQL_STATUS
    assert modulo._SQL_ESTADO_FALHA == EstadoEntregaCanalSaida._SQL_FALHA
    assert modulo._SQL_ESTADO_CLASSIFICACAO == EstadoEntregaCanalSaida._SQL_CLASSIFICACAO
    assert modulo._SQL_ESTADO_FORMA == EstadoEntregaCanalSaida._SQL_FORMA
    assert modulo._SQL_APLICACAO_RESULTADO == AplicacaoStatusCanal._SQL_RESULTADO
    assert modulo._SQL_APLICACAO_CORRELATION == AplicacaoStatusCanal._SQL_CORRELATION
    assert modulo._SQL_APLICACAO_COERENCIA == AplicacaoStatusCanal._SQL_COERENCIA
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
    assert "status_entrega" not in {coluna.name for coluna in EventoCanalSaida.__table__.columns}
    assert "sent_em" in {coluna.name for coluna in EstadoEntregaCanalSaida.__table__.columns}


def test_sent_encontra_saida_pelo_provider_message_id(ctx, caplog, _sem_rede):
    saida = _aceita()
    antes = _foto(saida.id)
    evento = _status("sent", MARCA_SENT)
    with caplog.at_level(logging.DEBUG):
        resultado = reconciliacao.reconciliar_status_saida(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    assert resultado.saida_id == saida.id
    assert resultado.status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
    assert resultado.provider_status_em == _em(MARCA_SENT)
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_SENT
    assert estado.sent_em == _em(MARCA_SENT)
    assert estado.delivered_em is None
    assert estado.read_em is None
    assert estado.provider_status_em == _em(MARCA_SENT)
    assert estado.sent_em != db.session.get(EventoCanalRecebido, evento.id).recebido_em
    assert _foto(saida.id) == antes
    assert db.session.get(EventoCanalRecebido, evento.id).status_processamento == (
        EventoCanalRecebido.STATUS_ROTEADO
    )
    assert AplicacaoStatusCanal.query.count() == 1
    _proibir_nos_novos(TELEFONE, TOKEN_BRUTO, CORPO_BRUTO, "Authorization", "Bearer")
    assert TELEFONE not in caplog.text
    assert MESSAGE_ID not in caplog.text
    assert TOKEN_BRUTO not in caplog.text
    assert _sem_rede == []


def test_delivered_avanca_estado(ctx):
    saida = _aceita()
    enviado = _foto(saida.id)[3]
    reconciliacao.reconciliar_status_saida(_status("sent", MARCA_SENT).id)
    resultado = reconciliacao.reconciliar_status_saida(_status("delivered", MARCA_DELIVERED).id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_DELIVERED
    assert estado.sent_em == _em(MARCA_SENT)
    assert estado.delivered_em == _em(MARCA_DELIVERED)
    assert estado.read_em is None
    assert estado.provider_status_em == _em(MARCA_DELIVERED)
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO
    assert _foto(saida.id)[3] == enviado


def test_read_avanca_estado(ctx):
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("sent", MARCA_SENT).id)
    reconciliacao.reconciliar_status_saida(_status("delivered", MARCA_DELIVERED).id)
    resultado = reconciliacao.reconciliar_status_saida(_status("read", MARCA_READ).id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_READ
    assert estado.sent_em == _em(MARCA_SENT)
    assert estado.delivered_em == _em(MARCA_DELIVERED)
    assert estado.read_em == _em(MARCA_READ)
    assert estado.provider_status_em == _em(MARCA_READ)
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO


def test_replay_de_sent_nao_duplica(ctx):
    saida = _aceita()
    evento = _status("sent", MARCA_SENT)
    primeiro = reconciliacao.reconciliar_status_saida(evento.id)
    segundo = reconciliacao.reconciliar_status_saida(evento.id)
    assert primeiro.codigo == reconciliacao.CODIGO_APLICADO
    assert segundo.codigo == reconciliacao.CODIGO_JA_TRATADO
    assert segundo.saida_id == saida.id
    assert EstadoEntregaCanalSaida.query.count() == 1
    assert AplicacaoStatusCanal.query.count() == 1
    estado = _estado(saida.id)
    assert estado.sent_em == _em(MARCA_SENT)
    assert estado.versao == 0
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_SENT


def test_sent_depois_de_delivered_nao_regride(ctx):
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("delivered", MARCA_DELIVERED).id)
    resultado = reconciliacao.reconciliar_status_saida(_status("sent", MARCA_SENT).id)
    assert resultado.codigo == reconciliacao.CODIGO_REGISTRADO_SEM_AVANCO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_DELIVERED
    assert estado.delivered_em == _em(MARCA_DELIVERED)
    assert estado.provider_status_em == _em(MARCA_DELIVERED)
    assert estado.sent_em == _em(MARCA_SENT)
    assert AplicacaoStatusCanal.query.count() == 2


def test_delivered_depois_de_read_nao_regride(ctx):
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("read", MARCA_READ).id)
    resultado = reconciliacao.reconciliar_status_saida(_status("delivered", MARCA_DELIVERED).id)
    assert resultado.codigo == reconciliacao.CODIGO_REGISTRADO_SEM_AVANCO
    estado = _estado(saida.id)
    assert estado.status_entrega == EstadoEntregaCanalSaida.STATUS_READ
    assert estado.read_em == _em(MARCA_READ)
    assert estado.provider_status_em == _em(MARCA_READ)
    assert estado.delivered_em == _em(MARCA_DELIVERED)
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO


def test_status_sem_saida_nao_inventa_vinculo(ctx):
    evento = _status("sent", MARCA_SENT, message_id=OUTRO_ID)
    resultado = reconciliacao.reconciliar_status_saida(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_SEM_SAIDA
    assert resultado.saida_id is None
    assert EventoCanalSaida.query.count() == 0
    assert EstadoEntregaCanalSaida.query.count() == 0
    aplicacao = AplicacaoStatusCanal.query.one()
    assert aplicacao.resultado == AplicacaoStatusCanal.RESULTADO_SEM_SAIDA
    assert aplicacao.saida_id is None
    assert db.session.get(EventoCanalRecebido, evento.id).status_processamento == (
        EventoCanalRecebido.STATUS_IGNORADO
    )
    replay = reconciliacao.reconciliar_status_saida(evento.id)
    assert replay.codigo == reconciliacao.CODIGO_JA_TRATADO
    assert AplicacaoStatusCanal.query.count() == 1


def test_status_desconhecido_e_seguro(ctx):
    saida = _aceita()
    antes = _foto(saida.id)
    played = _status("played", MARCA_SENT)
    resultado = reconciliacao.reconciliar_status_saida(played.id)
    assert resultado.codigo == reconciliacao.CODIGO_STATUS_IGNORADO
    assert resultado.saida_id is None
    desconhecido = _status(
        "deleted",
        MARCA_DELIVERED,
        diagnostico="status_entrega",
    )
    ignorado = reconciliacao.reconciliar_status_saida(desconhecido.id)
    assert ignorado.codigo == reconciliacao.CODIGO_STATUS_IGNORADO
    ilegivel = _status(
        "sent",
        MARCA_READ,
        externo="wamid.QUEBRADO",
        diagnostico="status_entrega:sent",
    )
    quebrado = reconciliacao.reconciliar_status_saida(ilegivel.id)
    assert quebrado.codigo == reconciliacao.CODIGO_ILEGIVEL
    assert EstadoEntregaCanalSaida.query.count() == 0
    assert _foto(saida.id) == antes
    for evento_id in (played.id, desconhecido.id, ilegivel.id):
        assert db.session.get(EventoCanalRecebido, evento_id).status_processamento == (
            EventoCanalRecebido.STATUS_IGNORADO
        )


def test_tentativas_concorrentes_nao_duplicam_efeito(_sem_rede):
    flask_app = Flask("corrida-status-3d2b")
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
            saida = _aceita()
            evento = _status("sent", MARCA_SENT)
            saida_id = int(saida.id)
            evento_id = int(evento.id)
            db.session.remove()
            barreira = threading.Barrier(2)
            resultados: dict[int, object] = {}

            def _worker(indice: int) -> None:
                try:
                    with flask_app.app_context():
                        barreira.wait(timeout=5)
                        resultados[indice] = reconciliacao.reconciliar_status_saida(evento_id)
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
            assert AplicacaoStatusCanal.query.count() == 1
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


def test_timestamps_meta_sao_preservados(ctx):
    saida = _aceita()
    primeiro = reconciliacao.reconciliar_status_saida(_status("sent", MARCA_SENT).id)
    assert primeiro.provider_status_em == _em(MARCA_SENT)
    assert _estado(saida.id).sent_em == _em(MARCA_SENT)
    assert _estado(saida.id).sent_em.year == 2020
    repetido = reconciliacao.reconciliar_status_saida(_status("sent", MARCA_SENT_TARDE).id)
    assert repetido.codigo == reconciliacao.CODIGO_SEM_EFEITO
    estado = _estado(saida.id)
    assert estado.sent_em == _em(MARCA_SENT)
    assert estado.provider_status_em == _em(MARCA_SENT)
    assert estado.sent_em != _em(MARCA_SENT_TARDE)


def test_saida_com_link_nao_reemite_token(ctx, _sem_rede):
    conclusao = _conclusao()
    hash_antes = conclusao.token_hash
    saida = _aceita(conclusao_id=int(conclusao.id))
    assert OnboardingCanalConclusao.query.count() == 1
    resultado = reconciliacao.reconciliar_status_saida(_status("read", MARCA_READ).id)
    assert resultado.codigo == reconciliacao.CODIGO_APLICADO
    assert resultado.status_entrega == EstadoEntregaCanalSaida.STATUS_READ
    atual = db.session.get(OnboardingCanalConclusao, conclusao.id)
    assert atual.token_hash == hash_antes
    assert atual.estado == OnboardingCanalConclusao.ESTADO_EMITIDO
    assert atual.consumido_em is None
    assert OnboardingCanalConclusao.query.count() == 1
    assert _foto(saida.id)[4] == conclusao.id
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_ACEITO
    _proibir_nos_novos(TOKEN_BRUTO, HASH_CONCLUSAO, "/onboarding/canal/concluir/")
    assert _sem_rede == []


def test_reservado_sem_provider_message_id_nao_casa_por_heuristica(ctx):
    saida = _saida(status=EventoCanalSaida.STATUS_RESERVADO, message_id=None)
    antes = _foto(saida.id)
    evento = _status("delivered", MARCA_DELIVERED, message_id=OUTRO_ID)
    resultado = reconciliacao.reconciliar_status_saida(evento.id)
    assert resultado.codigo == reconciliacao.CODIGO_SEM_SAIDA
    assert resultado.saida_id is None
    assert _foto(saida.id) == antes
    assert EstadoEntregaCanalSaida.query.count() == 0
    classificado = reconciliacao.classificar_resultado_incerto(saida.id)
    assert classificado.codigo == reconciliacao.CODIGO_CLASSIFICADO
    estado = _estado(saida.id)
    assert estado.classificacao_resultado == EstadoEntregaCanalSaida.CLASSIFICACAO_RESULTADO_INCERTO
    assert estado.status_entrega is None
    assert _foto(saida.id) == antes
    repetido = reconciliacao.classificar_resultado_incerto(saida.id)
    assert repetido.codigo == reconciliacao.CODIGO_JA_CLASSIFICADO
    assert EstadoEntregaCanalSaida.query.count() == 1
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_RESERVADO


def test_preparando_link_sem_provider_message_id_nao_casa_por_heuristica(ctx, _sem_rede):
    conclusao = _conclusao()
    hash_antes = conclusao.token_hash
    saida = _saida(
        status=EventoCanalSaida.STATUS_PREPARANDO_LINK,
        message_id=None,
        conclusao_id=int(conclusao.id),
    )
    antes = _foto(saida.id)
    resultado = reconciliacao.reconciliar_status_saida(
        _status("read", MARCA_READ, message_id=OUTRO_ID).id
    )
    assert resultado.codigo == reconciliacao.CODIGO_SEM_SAIDA
    assert resultado.saida_id is None
    assert _foto(saida.id) == antes
    assert db.session.get(OnboardingCanalConclusao, conclusao.id).token_hash == hash_antes
    assert OnboardingCanalConclusao.query.count() == 1
    classificado = reconciliacao.classificar_resultado_incerto(saida.id)
    assert classificado.codigo == reconciliacao.CODIGO_CLASSIFICADO
    assert _foto(saida.id) == antes
    assert _foto(saida.id)[0] == EventoCanalSaida.STATUS_PREPARANDO_LINK
    assert db.session.get(OnboardingCanalConclusao, conclusao.id).token_hash == hash_antes
    assert db.session.get(OnboardingCanalConclusao, conclusao.id).estado == (
        OnboardingCanalConclusao.ESTADO_EMITIDO
    )
    _proibir_nos_novos(TOKEN_BRUTO, HASH_CONCLUSAO)
    assert _sem_rede == []


def test_nenhuma_cloud_api_e_chamada(ctx, _sem_rede):
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("sent", MARCA_SENT).id)
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


def test_nenhum_retry_ocorre(ctx, _sem_rede):
    saida = _aceita()
    antes = _foto(saida.id)
    resultado = reconciliacao.reconciliar_status_saida(_status("failed", MARCA_FAILED).id)
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


def test_nenhuma_julia_gemini_ou_billing(ctx):
    saida = _aceita()
    reconciliacao.reconciliar_status_saida(_status("delivered", MARCA_DELIVERED).id)
    assert _estado(saida.id).status_entrega == EstadoEntregaCanalSaida.STATUS_DELIVERED
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    fonte = Path(reconciliacao.__file__).read_text(encoding="utf-8")
    for trecho in ("chat_julia", "gemini", "GenerativeModel", "run_julia", "growth"):
        assert trecho not in fonte
    for trecho in ("cleiton_monetizacao", "stripe", "IaConsumo", "billing"):
        assert trecho not in fonte
