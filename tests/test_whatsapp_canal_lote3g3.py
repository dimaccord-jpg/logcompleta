"""Lote WhatsApp 3G3: franquia indisponível não chama a Júlia.

O estado vem do Gerenciador. Uso permitido segue o chat e o consumo
já existentes. Uso impedido devolve a orientação de contratação.
"""
from __future__ import annotations

import importlib.util
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.session import SessionTransactionState

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    ConsumoInteracaoCanal,
    ConteudoTextualCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    ExecucaoOperacionalCanal,
    Franquia,
    IaConsumoEvento,
    InterpretacaoConversacionalCanal,
    OnboardingCanal,
    TentativaEnvioCanal,
    utcnow_naive,
)
from app.services import canal_operacao_whatsapp_service as operacao
from app.services import canal_orientacao_franquia_service as orientacao
from app.services import canal_orquestracao_whatsapp_service as orquestracao
from app.services.canal_interpretacao_conversacional_service import (
    CODIGO_ONBOARDING_INICIADO,
    CODIGO_ORIENTACAO_GUEST,
)
from app.services.cleiton_franquia_operacional_service import (
    classificar_estado_operacional_franquia,
)
from app.services.cleiton_mensageria_operacao_service import UPGRADE_PATH_DEFAULT
from app.services.cleiton_plano_resolver import resolver_plano_operacional_para_franquia
from tests.conftest import seed_cleiton_cost_config
from tests.test_whatsapp_canal_lote3g1 import (
    PERGUNTA,
    PHONE,
    PHONE_NUMBER_ID,
    RESPOSTA,
    _cliente,
    _configurar,
    _midia,
    _mock_julia,
    _mock_meta,
    _proibir_julia,
    _status,
    _texto,
    _vincular,
)

BASE = "https://example.test"


def _corpo(chamada) -> str:
    return chamada[1]["json"]["text"]["body"]


def _base(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", BASE)


def _vigiar(monkeypatch):
    chamadas = []
    original = orientacao.avaliar_autorizacao_operacao_por_franquia

    def _wrap(*args, **kwargs):
        chamadas.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(orientacao, "avaliar_autorizacao_operacao_por_franquia", _wrap)
    return chamadas


def _estado(franquia) -> str:
    plano = resolver_plano_operacional_para_franquia(franquia.id)
    status, _motivo = classificar_estado_operacional_franquia(franquia, plano)
    return status


def _esgotar(franquia):
    franquia.limite_total = Decimal("10")
    franquia.consumo_acumulado = Decimal("10")
    franquia.bloqueio_manual = False
    db.session.commit()
    db.session.refresh(franquia)
    assert _estado(franquia) == Franquia.STATUS_BLOCKED


def _taxa():
    cfg = seed_cleiton_cost_config()
    cfg.interacoes_whatsapp_por_credito = 10
    db.session.commit()
    return cfg


def _sem_consumo(franquia, antes: Decimal):
    db.session.refresh(franquia)
    assert franquia.consumo_acumulado == antes
    assert ConsumoInteracaoCanal.query.count() == 0
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert IaConsumoEvento.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0


def test_chave_de_orientacao_e_deterministica_e_cabe_no_contrato(ctx):
    chave = EventoCanalSaida.chave_orientacao_franquia(1)
    assert chave == "meta_whatsapp:1:orientacao_franquia:resposta_principal"
    assert chave != EventoCanalSaida.chave_resposta_principal(1)
    assert 34 <= len(chave) <= 80
    assert chave.startswith("meta_whatsapp:")
    assert chave[len(chave) - 18 :] == "resposta_principal"
    assert chave[len(chave) - 19] == ":"
    assert chave[len(chave) - 39 : len(chave) - 18] == ":orientacao_franquia:"
    longa = EventoCanalSaida.chave_orientacao_franquia(10**12)
    assert len(longa) <= 80
    migracao = (
        Path(orientacao.__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "c0d1e2f3g4h5_orientacao_franquia_canal.py"
    )
    texto = migracao.read_text(encoding="utf-8")
    assert "':orientacao_franquia:'" in texto
    assert "ck_evento_canal_saida_origem" in texto
    assert "length(chave_idempotencia) - 38, 21" in texto


def test_franquia_permitida_chama_julia_e_consome(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    _taxa()
    client = _cliente(app)
    _user, _admin, franquia, _outra, _ident = _vincular()
    antes = Decimal(str(franquia.consumo_acumulado))
    assert _estado(franquia) == Franquia.STATUS_ACTIVE
    assert _texto(client, "wamid.G3OK", PERGUNTA).status_code == 200
    assert len(consultas) == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    assert _corpo(meta[0]) == RESPOSTA
    assert "/contrate-um-plano" not in _corpo(meta[0])
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.conclusao_util == 1
    assert execucao.texto_resposta == RESPOSTA
    consumo = ConsumoInteracaoCanal.query.one()
    assert consumo.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA
    assert consumo.creditos_apropriados is not None
    assert Decimal(str(consumo.creditos_apropriados)) > 0
    db.session.refresh(franquia)
    assert Decimal(str(franquia.consumo_acumulado)) == antes + Decimal(str(consumo.creditos_apropriados))
    saida = EventoCanalSaida.query.one()
    assert saida.chave_idempotencia == EventoCanalSaida.chave_resposta_principal(execucao.evento_id)
    assert saida.execucao_operacional_id == execucao.id


def test_franquia_blocked_nao_chama_julia_e_orienta(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    client = _cliente(app)
    _user, _admin, franquia, _outra, _ident = _vincular()
    _esgotar(franquia)
    antes = Decimal(str(franquia.consumo_acumulado))
    assert _texto(client, "wamid.G3BLOQ", PERGUNTA).status_code == 200
    assert len(consultas) == 1
    assert len(meta) == 1
    corpo = _corpo(meta[0])
    assert "limite" in corpo
    assert "AgenteFrete" in corpo
    assert f"{BASE}{UPGRADE_PATH_DEFAULT}" in corpo
    assert UPGRADE_PATH_DEFAULT == "/contrate-um-plano"
    assert "www.agentefrete.com.br" not in corpo
    assert "checkout" not in corpo.lower()
    evento = EventoCanalRecebido.query.filter_by(evento_externo_id="wamid.G3BLOQ").one()
    saida = EventoCanalSaida.query.one()
    assert saida.evento_entrada_id == evento.id
    assert saida.chave_idempotencia == EventoCanalSaida.chave_orientacao_franquia(evento.id)
    assert saida.execucao_operacional_id is None
    assert saida.interpretacao_id is None
    assert saida.status_envio == EventoCanalSaida.STATUS_ACEITO
    tentativa = TentativaEnvioCanal.query.one()
    assert tentativa.saida_id == saida.id
    assert tentativa.numero_tentativa == TentativaEnvioCanal.NUMERO_INICIAL
    assert tentativa.origem_tentativa == TentativaEnvioCanal.ORIGEM_ENVIO_INICIAL
    assert tentativa.tipo_tentativa == TentativaEnvioCanal.TIPO_TEXTO_COMUM
    _sem_consumo(franquia, antes)
    assert PHONE not in corpo
    assert PERGUNTA not in corpo


def test_transacao_abortada_na_leitura_ainda_orienta(ctx, app, monkeypatch):
    """Leitura que falha e engole o erro não pode calar a orientação."""
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    import app.services.cleiton_monetizacao_service as monetizacao

    def _aborta(_conta_id, **_kwargs):
        # Postgres aborta a transação inteira quando a leitura falha e o
        # erro é engolido. O SQLite do teste não faz isso; o estado
        # DEACTIVE reproduz o mesmo bloqueio da saída.
        transacao = db.session().get_transaction()
        if transacao is not None:
            transacao._state = SessionTransactionState.DEACTIVE
        return {"falha_mensal_vigente": False}

    monkeypatch.setattr(
        monetizacao,
        "resolver_falha_mensal_vigente_conta",
        _aborta,
    )
    client = _cliente(app)
    _user, _admin, franquia, _outra, _ident = _vincular()
    _esgotar(franquia)
    antes = Decimal(str(franquia.consumo_acumulado))
    assert _texto(client, "wamid.G3ABORT", PERGUNTA).status_code == 200
    assert len(meta) == 1
    assert "AgenteFrete" in _corpo(meta[0])
    assert f"{BASE}{UPGRADE_PATH_DEFAULT}" in _corpo(meta[0])
    saida = EventoCanalSaida.query.one()
    assert saida.chave_idempotencia == EventoCanalSaida.chave_orientacao_franquia(
        saida.evento_entrada_id
    )
    assert saida.status_envio == EventoCanalSaida.STATUS_ACEITO
    _sem_consumo(franquia, antes)


def test_replay_do_evento_bloqueado_nao_repete(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    client = _cliente(app)
    _user, _admin, franquia, _outra, _ident = _vincular()
    _esgotar(franquia)
    antes = Decimal(str(franquia.consumo_acumulado))
    assert _texto(client, "wamid.G3REPLAY", PERGUNTA).status_code == 200
    evento_id = EventoCanalRecebido.query.one().id
    replay = _texto(client, "wamid.G3REPLAY", PERGUNTA)
    assert replay.get_json()["replays"] == 1
    assert orquestracao.processar_evento_whatsapp(evento_id).saida_id is not None
    assert operacao.executar_operacao_canal(evento_id).status_envio == EventoCanalSaida.STATUS_ACEITO
    assert len(consultas) == 1
    assert len(meta) == 1
    assert EventoCanalSaida.query.count() == 1
    assert TentativaEnvioCanal.query.count() == 1
    _sem_consumo(franquia, antes)


def test_nova_mensagem_bloqueada_orienta_de_novo_sem_consumo(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    client = _cliente(app)
    _user, _admin, franquia, _outra, _ident = _vincular()
    _esgotar(franquia)
    antes = Decimal(str(franquia.consumo_acumulado))
    assert _texto(client, "wamid.G3N1", PERGUNTA).status_code == 200
    assert _texto(client, "wamid.G3N2", "outra pergunta operacional").status_code == 200
    assert len(meta) == 2
    assert EventoCanalSaida.query.count() == 2
    assert _corpo(meta[0]) == _corpo(meta[1])
    assert f"{BASE}/contrate-um-plano" in _corpo(meta[1])
    _sem_consumo(franquia, antes)
    assert _estado(franquia) == Franquia.STATUS_BLOCKED


def test_franquia_reativada_volta_a_chamar_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    client = _cliente(app)
    _user, _admin, franquia, _outra, _ident = _vincular()
    _esgotar(franquia)
    assert _texto(client, "wamid.G3ANTES", PERGUNTA).status_code == 200
    assert modelos.chamadas == []
    assert ConsumoInteracaoCanal.query.count() == 0
    franquia.consumo_acumulado = Decimal("0")
    db.session.commit()
    db.session.refresh(franquia)
    assert _estado(franquia) == Franquia.STATUS_ACTIVE
    assert _texto(client, "wamid.G3DEPOIS", PERGUNTA).status_code == 200
    assert len(modelos.chamadas) == 1
    assert len(meta) == 2
    assert _corpo(meta[1]) == RESPOSTA
    execucao = ExecucaoOperacionalCanal.query.one()
    assert execucao.evento.evento_externo_id == "wamid.G3DEPOIS"
    assert execucao.conclusao_util == 1
    assert ConsumoInteracaoCanal.query.count() == 1
    assert IaConsumoEvento.query.count() == 1


def test_url_usa_base_configurada_e_rota_existente():
    fonte = Path(orientacao.__file__).read_text(encoding="utf-8")
    assert "agentefrete.com.br" not in fonte
    assert "stripe" not in fonte.lower()
    assert "PUBLIC_BASE_URL" in fonte
    assert UPGRADE_PATH_DEFAULT == "/contrate-um-plano"
    assert "interacoes_whatsapp_por_credito" not in fonte
    assert "CleitonBillingApropriacao" not in fonte
    assert "ConsumoInteracaoCanal" not in fonte


def test_url_publica_respeita_ambiente(monkeypatch):
    _base(monkeypatch)
    decisao = {"upgrade_cta": {"upgrade_url": "/contrate-um-plano"}}
    url = orientacao.url_publica_contratacao(decisao)
    assert url == f"{BASE}/contrate-um-plano"
    assert "agentefrete.com.br" not in url
    assert "www.agentefrete.com.br" not in url


def test_guest_nao_consulta_franquia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    client = _cliente(app)
    assert _texto(client, "wamid.G3GUEST", PERGUNTA).status_code == 200
    assert consultas == []
    assert InterpretacaoConversacionalCanal.query.one().codigo == CODIGO_ORIENTACAO_GUEST
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0
    assert len(meta) == 1
    assert "/contrate-um-plano" not in _corpo(meta[0])


def test_onboarding_nao_consulta_franquia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    client = _cliente(app)
    assert _texto(client, "wamid.G3ONB", "quero me cadastrar").status_code == 200
    assert consultas == []
    assert InterpretacaoConversacionalCanal.query.one().codigo == CODIGO_ONBOARDING_INICIADO
    assert OnboardingCanal.query.count() == 1
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0
    assert len(meta) == 1


def test_status_nao_consulta_franquia_nem_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    client = _cliente(app)
    assert _status(client, "wamid.G3STATUS", "delivered").status_code == 200
    assert consultas == []
    assert meta == []
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0
    assert IaConsumoEvento.query.count() == 0


def test_midia_nao_consulta_franquia_nem_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    client = _cliente(app)
    assert _midia(client, "wamid.G3MIDIA").status_code == 200
    assert consultas == []
    assert meta == []
    assert EventoCanalRecebido.query.one().tipo_evento == EventoCanalRecebido.TIPO_MIDIA
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0


def test_falha_ao_consultar_franquia_nao_chama_julia(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)

    def _falha(*_args, **_kwargs):
        raise RuntimeError("leitura-indisponivel")

    monkeypatch.setattr(orientacao, "avaliar_autorizacao_operacao_por_franquia", _falha)
    client = _cliente(app)
    _user, _admin, franquia, _outra, _ident = _vincular()
    antes = Decimal(str(franquia.consumo_acumulado))
    assert _estado(franquia) == Franquia.STATUS_ACTIVE
    assert _texto(client, "wamid.G3FALHA", PERGUNTA).status_code == 200
    assert meta == []
    _sem_consumo(franquia, antes)


def test_gate_nao_altera_a_regua(ctx):
    fonte_operacao = Path(operacao.__file__).read_text(encoding="utf-8")
    fonte_gate = Path(orientacao.__file__).read_text(encoding="utf-8")
    assert "interacoes_whatsapp_por_credito" not in fonte_operacao
    assert "interacoes_whatsapp_por_credito" not in fonte_gate
    assert "aplicar_motor_apos_ia_consumo_evento" not in fonte_operacao
    assert "WhatsAppMetaCloudApiAdapter" not in fonte_operacao
    assert "avaliar_autorizacao_operacao_por_franquia" in fonte_gate


def _evento(evento_id: int, externo: str) -> EventoCanalRecebido:
    agora = utcnow_naive()
    evento = EventoCanalRecebido(
        id=evento_id,
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=externo,
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=PHONE,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=agora,
        status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
        correlation_id=f"constraint-3g3-{evento_id}",
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(evento)
    db.session.flush()
    return evento


def _saida(
    evento: EventoCanalRecebido,
    chave: str,
    *,
    interpretacao_id: int | None = None,
    execucao_id: int | None = None,
) -> EventoCanalSaida:
    return EventoCanalSaida(
        evento_entrada_id=int(evento.id),
        interpretacao_id=interpretacao_id,
        execucao_operacional_id=execucao_id,
        provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
        chave_idempotencia=chave,
        status_envio=EventoCanalSaida.STATUS_RESERVADO,
        criado_em=utcnow_naive(),
        correlation_id=evento.correlation_id,
    )


def _rejeitar(row: EventoCanalSaida) -> None:
    with pytest.raises(IntegrityError) as erro:
        db.session.add(row)
        db.session.commit()
    db.session.rollback()
    assert "ck_evento_canal_saida_origem" in str(erro.value)


def _reservar(mensagem_id: str):
    user, _admin, franquia, _outra, ident = _vincular()
    agora = utcnow_naive()
    evento = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=mensagem_id,
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=PHONE,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=agora,
        status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
        correlation_id="retomada-3g3",
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(evento)
    db.session.flush()
    db.session.add(ConteudoTextualCanal(evento_id=int(evento.id), texto=PERGUNTA))
    execucao = ExecucaoOperacionalCanal(
        evento_id=int(evento.id),
        identidade_id=int(ident.id),
        user_id=int(user.id),
        codigo=ExecucaoOperacionalCanal.CODIGO_EM_TRATAMENTO,
        estado=ExecucaoOperacionalCanal.ESTADO_RESERVADA,
        texto_resposta=None,
        execution_id=str(uuid4()),
        conclusao_util=0,
        correlation_id="retomada-3g3",
        criada_em=agora,
        atualizada_em=agora,
    )
    db.session.add(execucao)
    db.session.commit()
    return user, franquia, ident, evento, execucao


def test_retomada_reservada_com_franquia_permitida_chama_julia_uma_vez(
    ctx, app, monkeypatch
):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    _taxa()
    _user, franquia, ident, evento, execucao = _reservar("wamid.G3RET.OK")
    antes = Decimal(str(franquia.consumo_acumulado))
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_RESERVADA
    resultado = orquestracao.processar_evento_whatsapp(int(evento.id))
    assert resultado.codigo == EventoCanalSaida.STATUS_ACEITO
    assert len(consultas) == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    assert _corpo(meta[0]) == RESPOSTA
    db.session.refresh(execucao)
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_CONCLUIDA
    assert execucao.conclusao_util == 1
    assert IaConsumoEvento.query.count() == 1
    consumo = ConsumoInteracaoCanal.query.one()
    assert consumo.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA
    db.session.refresh(franquia)
    assert Decimal(str(franquia.consumo_acumulado)) == antes + Decimal(
        str(consumo.creditos_apropriados)
    )
    saida = EventoCanalSaida.query.one()
    assert saida.chave_idempotencia == EventoCanalSaida.chave_resposta_principal(evento.id)
    assert saida.execucao_operacional_id == execucao.id
    assert saida.interpretacao_id is None
    replay = operacao.executar_operacao_canal(int(evento.id), int(ident.id))
    assert replay.codigo == EventoCanalSaida.STATUS_ACEITO
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1
    assert IaConsumoEvento.query.count() == 1
    assert ConsumoInteracaoCanal.query.count() == 1


def test_retomada_reservada_com_franquia_blocked_nao_chama_julia(
    ctx, app, monkeypatch
):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    consultas = _vigiar(monkeypatch)
    _user, franquia, ident, evento, execucao = _reservar("wamid.G3RET.BLOQ")
    _esgotar(franquia)
    antes = Decimal(str(franquia.consumo_acumulado))
    resultado = orquestracao.processar_evento_whatsapp(int(evento.id))
    assert resultado.codigo == orientacao.CODIGO_FRANQUIA_INDISPONIVEL
    assert len(consultas) == 1
    assert len(meta) == 1
    assert f"{BASE}{UPGRADE_PATH_DEFAULT}" in _corpo(meta[0])
    db.session.refresh(execucao)
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_RESERVADA
    assert execucao.conclusao_util == 0
    saida = EventoCanalSaida.query.one()
    assert saida.evento_entrada_id == evento.id
    assert saida.chave_idempotencia == EventoCanalSaida.chave_orientacao_franquia(evento.id)
    assert saida.interpretacao_id is None
    assert saida.execucao_operacional_id is None
    assert TentativaEnvioCanal.query.count() == 1
    assert IaConsumoEvento.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    db.session.refresh(franquia)
    assert franquia.consumo_acumulado == antes
    replay = orquestracao.processar_evento_whatsapp(int(evento.id))
    assert replay.saida_id == saida.id
    direto = operacao.executar_operacao_canal(int(evento.id), int(ident.id))
    assert direto.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert len(consultas) == 1
    assert len(meta) == 1
    assert EventoCanalSaida.query.count() == 1
    assert TentativaEnvioCanal.query.count() == 1
    assert IaConsumoEvento.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0
    db.session.refresh(execucao)
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_RESERVADA


def test_retomada_reservada_com_falha_do_gerenciador_nao_chama_julia(
    ctx, app, monkeypatch
):
    _configurar(monkeypatch)
    _base(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)

    def _falha(*_args, **_kwargs):
        raise RuntimeError("leitura-indisponivel")

    monkeypatch.setattr(orientacao, "avaliar_autorizacao_operacao_por_franquia", _falha)
    _user, franquia, ident, evento, execucao = _reservar("wamid.G3RET.FALHA")
    antes = Decimal(str(franquia.consumo_acumulado))
    resultado = orquestracao.processar_evento_whatsapp(int(evento.id))
    assert resultado.codigo == "erro_tecnico"
    assert modelos.chamadas == []
    assert meta == []
    assert EventoCanalSaida.query.count() == 0
    assert TentativaEnvioCanal.query.count() == 0
    assert IaConsumoEvento.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    db.session.refresh(franquia)
    assert franquia.consumo_acumulado == antes
    db.session.refresh(execucao)
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_RESERVADA
    assert execucao.codigo == ExecucaoOperacionalCanal.CODIGO_EM_TRATAMENTO
    with pytest.raises(RuntimeError, match="leitura-indisponivel"):
        operacao.executar_operacao_canal(int(evento.id), int(ident.id))
    db.session.rollback()
    db.session.refresh(execucao)
    assert execucao.estado == ExecucaoOperacionalCanal.ESTADO_RESERVADA
    assert modelos.chamadas == []
    assert EventoCanalSaida.query.count() == 0


def test_constraint_so_aceita_orientacao_com_chave_do_proprio_evento(ctx):
    migracao = (
        Path(orientacao.__file__).resolve().parents[2]
        / "migrations"
        / "versions"
        / "d1e2f3g4h5i6_orientacao_franquia_chave_canonica.py"
    )
    texto = migracao.read_text(encoding="utf-8")
    spec = importlib.util.spec_from_file_location("mig_chave_canonica_3g3", migracao)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    assert modulo._SQL_ORIGEM == EventoCanalSaida._SQL_ORIGEM
    assert "CAST(evento_entrada_id AS TEXT)" in texto
    assert "ck_evento_canal_saida_origem" in texto
    assert "Não reescreve chaves" in texto
    assert "não prova que a chave pertence ao" in texto
    evento = _evento(10, "wamid.G3CONSTRAINT.10")
    db.session.commit()
    canonica = EventoCanalSaida.chave_orientacao_franquia(10)
    assert canonica == "meta_whatsapp:10:orientacao_franquia:resposta_principal"
    _rejeitar(_saida(evento, EventoCanalSaida.chave_orientacao_franquia(99)))
    _rejeitar(
        _saida(
            evento,
            "meta_whatsapp:qualquer:orientacao_franquia:resposta_principal",
        )
    )
    interpretacao = InterpretacaoConversacionalCanal(
        evento_id=evento.id,
        codigo="resposta_registrada",
        acao="registrar_nome",
        texto_resposta="Orientacao interna sem segredo.",
    )
    db.session.add(interpretacao)
    db.session.commit()
    _rejeitar(_saida(evento, canonica, interpretacao_id=int(interpretacao.id)))
    user, _admin, _franquia, _outra, ident = _vincular(email="constraint.3g3@example.com")
    agora = utcnow_naive()
    execucao = ExecucaoOperacionalCanal(
        evento_id=int(evento.id),
        identidade_id=int(ident.id),
        user_id=int(user.id),
        codigo=ExecucaoOperacionalCanal.CODIGO_EM_TRATAMENTO,
        estado=ExecucaoOperacionalCanal.ESTADO_RESERVADA,
        texto_resposta=None,
        execution_id=str(uuid4()),
        conclusao_util=0,
        correlation_id="constraint-3g3-10",
        criada_em=agora,
        atualizada_em=agora,
    )
    db.session.add(execucao)
    db.session.commit()
    _rejeitar(_saida(evento, canonica, execucao_id=int(execucao.id)))
    db.session.add(_saida(evento, canonica))
    db.session.commit()
    assert EventoCanalSaida.query.filter_by(chave_idempotencia=canonica).one()

    outro = _evento(11, "wamid.G3CONSTRAINT.11")
    leitura = InterpretacaoConversacionalCanal(
        evento_id=outro.id,
        codigo="resposta_registrada",
        acao="registrar_nome",
        texto_resposta="Resposta da interpretacao.",
    )
    db.session.add(leitura)
    db.session.flush()
    db.session.add(
        _saida(
            outro,
            EventoCanalSaida.chave_resposta_principal(outro.id),
            interpretacao_id=int(leitura.id),
        )
    )
    db.session.commit()

    operacional = _evento(12, "wamid.G3CONSTRAINT.12")
    agora = utcnow_naive()
    execucao_op = ExecucaoOperacionalCanal(
        evento_id=int(operacional.id),
        identidade_id=int(ident.id),
        user_id=int(user.id),
        codigo=ExecucaoOperacionalCanal.CODIGO_EM_TRATAMENTO,
        estado=ExecucaoOperacionalCanal.ESTADO_RESERVADA,
        texto_resposta=None,
        execution_id=str(uuid4()),
        conclusao_util=0,
        correlation_id="constraint-3g3-12",
        criada_em=agora,
        atualizada_em=agora,
    )
    db.session.add(execucao_op)
    db.session.flush()
    db.session.add(
        _saida(
            operacional,
            EventoCanalSaida.chave_resposta_principal(operacional.id),
            execucao_id=int(execucao_op.id),
        )
    )
    db.session.commit()
    origens = {
        row.chave_idempotencia: (
            row.interpretacao_id,
            row.execucao_operacional_id,
        )
        for row in EventoCanalSaida.query.all()
    }
    assert origens[canonica] == (None, None)
    assert origens[EventoCanalSaida.chave_resposta_principal(11)][0] is not None
    assert origens[EventoCanalSaida.chave_resposta_principal(12)][1] is not None
