"""Entrega web do texto já produzido para o WhatsApp vinculado do próprio usuário."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    ConsumoInteracaoCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    ExecucaoOperacionalCanal,
    IaConsumoEvento,
    IdentidadeCanalExterna,
    InterpretacaoConversacionalCanal,
    SolicitacaoEntregaCanal,
    User,
    utcnow_naive,
)
from app.services import canal_entrega_web_whatsapp_service as entrega
from app.services import canal_intencao_entrega_whatsapp_service as intencao
from app.services import canal_reconciliacao_status_service as reconciliacao
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services import whatsapp_meta_config as config
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.canal_intencao_entrega_whatsapp_service import (
    NOME_ENVIAR_PARA_MEU_WHATSAPP,
    NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP,
)
from tests.conftest import seed_conta_franquia_cliente

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "f4g5h6i7j8k9_solicitacao_entrega_canal_web.py"

TOKEN = "EAAWHATSAPPWEBTOKENSECRETO"
VERSAO = "v26.0"
PHONE_NUMBER_ID = "106540352242922"
DESTINATARIO = "16505551234"
OUTRO_TELEFONE = "5511999887766"
TEXTO = "Os três principais riscos são atraso, avaria e custo."
MARCADOR = "MARCADOR_CONTEUDO_WEB_NAO_LOGAR"


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_entrega_web", MIGRATION)
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

    def json(self):
        return self._payload

    def close(self):
        return None


def _mock(monkeypatch, *, falhar_na=None, status_erro=500):
    chamadas = []

    def _post(url, **kwargs):
        identificador = f"wamid.WEB{len(chamadas) + 1}"
        chamadas.append((url, kwargs, identificador))
        if falhar_na is not None and len(chamadas) == falhar_na:
            return _Resposta(status_erro, {})
        return _Resposta(200, {"messages": [{"id": identificador}]})

    monkeypatch.setattr(adapter.requests, "post", _post)
    return chamadas


def _usuario(email="entrega.web@example.com"):
    conta, franquia = seed_conta_franquia_cliente("conta-entrega-web")
    user = User(
        email=email,
        full_name="Entrega Web",
        conta_id=conta.id,
        franquia_id=franquia.id,
        sessao_contexto_geracao=0,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _vincular(user, sujeito=DESTINATARIO, contexto=PHONE_NUMBER_ID):
    agora = utcnow_naive()
    row = IdentidadeCanalExterna(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=sujeito,
        contexto_destino=contexto,
        estado=IdentidadeCanalExterna.ESTADO_VINCULADA,
        user_id=int(user.id),
        interacoes_uteis=0,
        criada_em=agora,
        atualizada_em=agora,
        vinculada_em=agora,
        revogada_em=None,
    )
    db.session.add(row)
    db.session.commit()
    return row


def _evento(externo="wamid.ENTRADA.WEB"):
    row = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=externo,
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id="correntradaw",
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(row)
    db.session.commit()
    return row


def _interpretacao(evento):
    row = InterpretacaoConversacionalCanal(
        evento_id=int(evento.id),
        codigo="resposta_registrada",
        acao="registrar_nome",
        texto_resposta="Resposta inbound antiga.",
    )
    db.session.add(row)
    db.session.commit()
    return row


def _solicitacao(user, chave="a" * 32):
    row = SolicitacaoEntregaCanal(
        user_id=int(user.id),
        provider=SolicitacaoEntregaCanal.PROVIDER_META_WHATSAPP,
        superficie_origem=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        correlation_id=uuid4().hex,
        chave_idempotencia=chave,
        content_hash="ab" * 32,
        criada_em=utcnow_naive(),
    )
    db.session.add(row)
    db.session.commit()
    return row


def _saida_inbound(evento, interpretacao):
    return EventoCanalSaida(
        evento_entrada_id=int(evento.id),
        solicitacao_entrega_id=None,
        interpretacao_id=int(interpretacao.id),
        execucao_operacional_id=None,
        conclusao_id=None,
        provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
        chave_idempotencia=EventoCanalSaida.chave_resposta_principal(int(evento.id)),
        status_envio=EventoCanalSaida.STATUS_RESERVADO,
        provider_message_id=None,
        codigo_erro=None,
        criado_em=utcnow_naive(),
        enviado_em=None,
        correlation_id="corrinboundweb1",
    )


def _saida_web(solicitacao, parte=1):
    return EventoCanalSaida(
        evento_entrada_id=None,
        solicitacao_entrega_id=int(solicitacao.id),
        interpretacao_id=None,
        execucao_operacional_id=None,
        conclusao_id=None,
        provider=EventoCanalSaida.PROVIDER_META_WHATSAPP,
        chave_idempotencia=EventoCanalSaida.chave_resposta_web(int(solicitacao.id), parte),
        status_envio=EventoCanalSaida.STATUS_RESERVADO,
        provider_message_id=None,
        codigo_erro=None,
        criado_em=utcnow_naive(),
        enviado_em=None,
        correlation_id=solicitacao.correlation_id,
    )


def _rejeitar(row):
    with pytest.raises(IntegrityError):
        db.session.add(row)
        db.session.commit()
    db.session.rollback()


def _historico(texto=TEXTO):
    return [{"role": "model", "content": texto}, {"role": "user", "content": "obrigado"}]


def _decidir(monkeypatch, valor):
    def _fixo(*_args, **_kwargs):
        if valor is True:
            return intencao.DecisaoEntregaWhatsApp.POSITIVO
        if valor is False:
            return intencao.DecisaoEntregaWhatsApp.NEGATIVO
        return valor

    monkeypatch.setattr(entrega, "decidir_enviar_para_meu_whatsapp", _fixo)


def _cliente_falso(monkeypatch, modulo):
    monkeypatch.setattr(modulo, "_get_client", lambda: object())


def test_migration_casa_com_o_modelo():
    modulo = _migration_module()
    assert modulo.revision == "f4g5h6i7j8k9"
    assert modulo.down_revision == "e3f4g5h6i7j8"
    assert modulo._SQL_ORIGEM_VIGENTE == EventoCanalSaida._SQL_ORIGEM_VIGENTE
    assert modulo._SQL_CHAVE_VIGENTE == EventoCanalSaida._SQL_CHAVE_VIGENTE
    assert modulo._SQL_XOR_ORIGEM == EventoCanalSaida._SQL_XOR_ORIGEM
    assert modulo._SQL_ORIGEM_ANTERIOR == EventoCanalSaida._SQL_ORIGEM
    assert modulo._SQL_CHAVE_ANTERIOR == EventoCanalSaida._SQL_CHAVE


def test_saida_aceita_inbound_e_web_e_rejeita_xor(ctx):
    user = _usuario()
    evento = _evento()
    interpretacao = _interpretacao(evento)
    inbound = _saida_inbound(evento, interpretacao)
    db.session.add(inbound)
    db.session.commit()
    solicitacao = _solicitacao(user, chave="b" * 32)
    web = _saida_web(solicitacao)
    db.session.add(web)
    db.session.commit()
    assert inbound.evento_entrada_id == evento.id
    assert inbound.solicitacao_entrega_id is None
    assert web.evento_entrada_id is None
    assert web.solicitacao_entrega_id == solicitacao.id
    ambos = _saida_web(solicitacao, parte=2)
    ambos.evento_entrada_id = int(evento.id)
    _rejeitar(ambos)
    nenhum = _saida_web(solicitacao, parte=3)
    nenhum.evento_entrada_id = None
    nenhum.solicitacao_entrega_id = None
    nenhum.chave_idempotencia = EventoCanalSaida.chave_resposta_principal(999)
    _rejeitar(nenhum)


def test_um_vinculo_resolve_e_zero_ou_varios_nao_enviam(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario()
    codigo, destino = entrega.resolver_whatsapp_do_usuario(user)
    assert codigo == entrega.CODIGO_SEM_VINCULO
    assert destino is None
    assert (
        entrega.entregar_texto_no_whatsapp_do_usuario(
            usuario=user,
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            texto=TEXTO,
        ).codigo
        == entrega.CODIGO_SEM_VINCULO
    )
    assert chamadas == []
    _vincular(user)
    _vincular(user, sujeito="16505550000", contexto="106540352242923")
    codigo, destino = entrega.resolver_whatsapp_do_usuario(user)
    assert codigo == entrega.CODIGO_AMBIGUO
    assert destino is None
    assert (
        entrega.entregar_texto_no_whatsapp_do_usuario(
            usuario=user,
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            texto=TEXTO,
        ).codigo
        == entrega.CODIGO_AMBIGUO
    )
    assert chamadas == []


def test_destino_vem_do_vinculo_e_ignora_telefone_do_texto(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario()
    _vincular(user)
    texto = f"Pode encaminhar, mas não use {OUTRO_TELEFONE}."
    resultado = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=texto,
        chave_idempotencia=uuid4().hex,
    )
    assert resultado.codigo == entrega.CODIGO_ENVIADO
    corpo = chamadas[0][1]["json"]
    assert corpo["to"] == DESTINATARIO
    assert corpo["to"] != OUTRO_TELEFONE
    assert OUTRO_TELEFONE not in corpo["to"]
    solicitacao = db.session.get(SolicitacaoEntregaCanal, resultado.solicitacao_id)
    saida = EventoCanalSaida.query.filter_by(solicitacao_entrega_id=solicitacao.id).one()
    for row in (solicitacao, saida):
        for coluna in row.__table__.columns:
            valor = getattr(row, coluna.name)
            assert OUTRO_TELEFONE not in str(valor or "")
            assert DESTINATARIO not in str(valor or "")
            assert TOKEN not in str(valor or "")
            assert texto not in str(valor or "")


def test_replay_nao_duplica_e_nova_solicitacao_envia_de_novo(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario()
    _vincular(user)
    chave = uuid4().hex
    primeiro = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=TEXTO,
        chave_idempotencia=chave,
    )
    segundo = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=TEXTO,
        chave_idempotencia=chave,
    )
    assert primeiro.solicitacao_id == segundo.solicitacao_id
    assert len(chamadas) == 1
    assert EventoCanalSaida.query.count() == 1
    terceiro = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=TEXTO,
        chave_idempotencia=uuid4().hex,
    )
    assert terceiro.solicitacao_id != primeiro.solicitacao_id
    assert len(chamadas) == 2


def test_texto_longo_divide_sem_passar_de_4096(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario()
    _vincular(user)
    texto = ("risco de atraso " * 400).strip()
    assert len(texto) > adapter._TEXTO_MAXIMO
    partes = entrega.dividir_texto_whatsapp(texto)
    assert len(partes) >= 2
    assert "".join(partes) == texto
    assert all(0 < len(parte) <= adapter._TEXTO_MAXIMO for parte in partes)
    resultado = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_AUDITORIA,
        texto=texto,
        chave_idempotencia=uuid4().hex,
    )
    assert resultado.codigo == entrega.CODIGO_ENVIADO
    assert resultado.partes == len(partes)
    corpos = [chamada[1]["json"]["text"]["body"] for chamada in chamadas]
    assert corpos == partes
    chaves = [row.chave_idempotencia for row in EventoCanalSaida.query.order_by(EventoCanalSaida.id)]
    assert len(chaves) == len(set(chaves)) == len(partes)
    assert all(":parte:" in chave and chave.endswith(":resposta_principal") for chave in chaves)


def test_falha_meta_nao_confirma_e_sucesso_confirma(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch, falhar_na=1, status_erro=500)
    user = _usuario()
    _vincular(user)
    falha = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=TEXTO,
        chave_idempotencia=uuid4().hex,
    )
    assert falha.codigo == entrega.CODIGO_FALHA_ENVIO
    assert entrega.mensagem_da_entrega(falha) == entrega.MENSAGEM_FALHA
    assert entrega.mensagem_da_entrega(falha) != entrega.MENSAGEM_ENVIADO
    saida = EventoCanalSaida.query.one()
    assert saida.status_envio == EventoCanalSaida.STATUS_ERRO
    _mock(monkeypatch)
    ok = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=TEXTO,
        chave_idempotencia=uuid4().hex,
    )
    assert ok.codigo == entrega.CODIGO_ENVIADO
    assert entrega.mensagem_da_entrega(ok) == entrega.MENSAGEM_ENVIADO


def test_nao_cria_consumo_whatsapp(ctx, monkeypatch):
    _configurar(monkeypatch)
    _mock(monkeypatch)
    user = _usuario()
    _vincular(user)
    antes = (
        ConsumoInteracaoCanal.query.count(),
        ExecucaoOperacionalCanal.query.count(),
        CleitonBillingApropriacao.query.count(),
        IaConsumoEvento.query.count(),
    )
    entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO,
        texto=TEXTO,
        chave_idempotencia=uuid4().hex,
    )
    assert (
        ConsumoInteracaoCanal.query.count(),
        ExecucaoOperacionalCanal.query.count(),
        CleitonBillingApropriacao.query.count(),
        IaConsumoEvento.query.count(),
    ) == antes


def test_logs_nao_expoem_telefone_token_nem_conteudo(ctx, monkeypatch, caplog):
    _configurar(monkeypatch)
    _mock(monkeypatch)
    user = _usuario()
    _vincular(user)
    texto = f"{TEXTO} {MARCADOR}"
    with caplog.at_level(logging.INFO):
        entrega.entregar_texto_no_whatsapp_do_usuario(
            usuario=user,
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            texto=texto,
            chave_idempotencia=uuid4().hex,
        )
    assert DESTINATARIO not in caplog.text
    assert PHONE_NUMBER_ID not in caplog.text
    assert TOKEN not in caplog.text
    assert MARCADOR not in caplog.text
    assert TEXTO not in caplog.text


def test_status_provider_encontra_saida_web(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario()
    _vincular(user)
    resultado = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=TEXTO,
        chave_idempotencia=uuid4().hex,
    )
    assert resultado.codigo == entrega.CODIGO_ENVIADO
    message_id = chamadas[0][2]
    saida = EventoCanalSaida.query.one()
    assert saida.evento_entrada_id is None
    assert saida.provider_message_id == message_id
    for status in ("sent", "delivered", "read"):
        evento = EventoCanalRecebido(
            provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
            evento_externo_id=f"{message_id}:{status}:1710000000",
            tipo_evento=EventoCanalRecebido.TIPO_STATUS_ENTREGA,
            sujeito_externo=DESTINATARIO,
            contexto_destino=PHONE_NUMBER_ID,
            recebido_em=utcnow_naive(),
            status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
            correlation_id="corrstatusweb1",
            diagnostico_seguro=f"status_entrega:{status}",
        )
        db.session.add(evento)
        db.session.commit()
        aplicado = reconciliacao.reconciliar_status_saida(int(evento.id))
        assert aplicado.saida_id == saida.id
        assert aplicado.status_entrega == status
    outro = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=TEXTO + " de novo",
        chave_idempotencia=uuid4().hex,
    )
    assert outro.codigo == entrega.CODIGO_ENVIADO
    failed_id = chamadas[1][2]
    failed = EventoCanalSaida.query.filter_by(provider_message_id=failed_id).one()
    evento_failed = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=f"{failed_id}:failed:1710000001",
        tipo_evento=EventoCanalRecebido.TIPO_STATUS_ENTREGA,
        sujeito_externo=DESTINATARIO,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id="corrstatusweb2",
        diagnostico_seguro="status_entrega:failed",
    )
    db.session.add(evento_failed)
    db.session.commit()
    aplicado = reconciliacao.reconciliar_status_saida(int(evento_failed.id))
    assert aplicado.saida_id == failed.id
    assert aplicado.status_entrega == "failed"


def test_intencao_usa_funcao_sem_parametro_de_destino(monkeypatch):
    capturado = {"chamadas": 0}

    def _capturar(*_args, **kwargs):
        capturado["chamadas"] += 1
        capturado["config"] = kwargs["config"]
        capturado["contents"] = kwargs["contents"]
        return SimpleNamespace(function_calls=[], candidates=[])

    monkeypatch.setattr(intencao, "cleiton_governed_generate_content", _capturar)
    assert (
        intencao.decidir_enviar_para_meu_whatsapp(
            "envia isso para meu whatsapp",
            agent="julia",
            flow_type="julia_chat",
            client=object(),
            model="gemini-2.5-flash",
            api_key_label="teste",
        )
        is intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA
    )
    assert capturado["chamadas"] == 2
    declaracoes = capturado["config"].tools[0].function_declarations
    assert [item.name for item in declaracoes] == [
        NOME_ENVIAR_PARA_MEU_WHATSAPP,
        NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP,
    ]
    assert all(getattr(item, "parameters", None) in (None, {}) for item in declaracoes)
    modo = capturado["config"].tool_config.function_calling_config.mode
    assert getattr(modo, "value", modo) == "ANY"
    assert list(capturado["config"].tool_config.function_calling_config.allowed_function_names) == [
        NOME_ENVIAR_PARA_MEU_WHATSAPP,
        NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP,
    ]
    assert capturado["config"].temperature == 0
    assert capturado["config"].max_output_tokens > 32
    assert capturado["config"].thinking_config.thinking_budget == 0
    assert capturado["config"].thinking_config.include_thoughts is False
    assert capturado["contents"] == "envia isso para meu whatsapp"
    assert "16505551234" not in str(capturado["config"])

    def _chama(*_args, **_kwargs):
        return SimpleNamespace(
            function_calls=[SimpleNamespace(name=NOME_ENVIAR_PARA_MEU_WHATSAPP, args={"telefone": OUTRO_TELEFONE})],
            candidates=[],
        )

    monkeypatch.setattr(intencao, "cleiton_governed_generate_content", _chama)
    assert (
        intencao.decidir_enviar_para_meu_whatsapp(
            "pode me mandar isso por lá?",
            agent="julia",
            flow_type="julia_chat",
            client=object(),
            model="gemini-2.5-flash",
            api_key_label="teste",
        )
        is intencao.DecisaoEntregaWhatsApp.POSITIVO
    )


def test_julia_envia_resposta_anterior_sem_nova_analise(ctx, monkeypatch):
    from app import run_julia_chat as julia

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario()
    _vincular(user)
    _decidir(monkeypatch, True)
    _cliente_falso(monkeypatch, julia)

    def _proibido(*_args, **_kwargs):
        raise AssertionError("geracao analitica")

    monkeypatch.setattr(julia, "cleiton_governed_generate_content", _proibido)
    for frase in (
        "Envie o resumo para meu WhatsApp.",
        "envia isso para meu whatsapp",
        "manda essa resposta no whatsapp",
        "pode me mandar isso por lá?",
        "joga essa análise no meu whatsapp",
    ):
        resposta = julia.chat_julia_reply(frase, _historico(), usuario=user)
        assert resposta["reply"] == entrega.MENSAGEM_ENVIADO
    assert len(chamadas) == 5
    assert all(item[1]["json"]["text"]["body"] == TEXTO for item in chamadas)
    assert all(item[1]["json"]["to"] == DESTINATARIO for item in chamadas)


def test_julia_sem_intencao_nao_envia_mesmo_com_frase(ctx, monkeypatch):
    from app import run_julia_chat as julia

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario()
    _vincular(user)
    _decidir(monkeypatch, False)
    _cliente_falso(monkeypatch, julia)
    geracoes = []

    def _gerar(*_args, **_kwargs):
        geracoes.append(True)
        return SimpleNamespace(text="Análise nova de frete.", usage_metadata=None)

    monkeypatch.setattr(julia, "cleiton_governed_generate_content", _gerar)
    monkeypatch.setattr(julia, "should_search_web_for_question", lambda *_args, **_kwargs: False)
    resposta = julia.chat_julia_reply("envia isso para meu whatsapp", _historico(), usuario=user)
    assert resposta["reply"] == "Análise nova de frete."
    assert geracoes == [True]
    assert chamadas == []


def test_julia_sem_resposta_anterior_e_sem_vinculo(ctx, monkeypatch):
    from app import run_julia_chat as julia

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario()
    _decidir(monkeypatch, True)
    _cliente_falso(monkeypatch, julia)
    sem_anterior = julia.chat_julia_reply("manda essa resposta no whatsapp", [], usuario=user)
    assert sem_anterior["reply"] == entrega.MENSAGEM_SEM_ANTERIOR
    assert chamadas == []
    _vincular(user)
    IdentidadeCanalExterna.query.delete()
    db.session.commit()
    sem_vinculo = julia.chat_julia_reply(
        "envia isso para meu whatsapp",
        _historico(),
        usuario=user,
    )
    assert sem_vinculo["reply"] == entrega.MENSAGEM_SEM_VINCULO
    assert chamadas == []


def test_auditoria_envia_resposta_anterior(ctx, monkeypatch):
    from app import run_cleide_audit_chat as auditoria

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("auditoria.web@example.com")
    _vincular(user)
    _decidir(monkeypatch, True)
    _cliente_falso(monkeypatch, auditoria)

    def _proibido(*_args, **_kwargs):
        raise AssertionError("geracao analitica")

    monkeypatch.setattr(auditoria, "cleiton_governed_generate_content", _proibido)
    resposta = auditoria.chat_cleide_audit_reply(
        "manda essa análise no meu whatsapp",
        [{"role": "assistant", "content": TEXTO}],
        usuario=user,
    )
    assert resposta["answer"] == entrega.MENSAGEM_ENVIADO
    assert chamadas[0][1]["json"]["text"]["body"] == TEXTO
    assert chamadas[0][1]["json"]["to"] == DESTINATARIO
    assert ConsumoInteracaoCanal.query.count() == 0


def test_comparacao_envia_depois_do_gate_sem_nova_geracao(ctx, monkeypatch):
    from app import run_agente_compara_comparison_chat as comparacao

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("comparacao.web@example.com")
    _vincular(user)
    _decidir(monkeypatch, True)
    _cliente_falso(monkeypatch, comparacao)
    monkeypatch.setattr(
        comparacao,
        "evaluate_comparison_chat_availability",
        lambda **_kwargs: {"chat_available": True},
    )
    monkeypatch.setattr(
        comparacao,
        "build_comparison_chat_context",
        lambda **_kwargs: {
            "selected_scope": {"scope": "overview", "capability": "ready"},
            "comparison": {"comparison_id": "cmp-web", "table_count": 2},
            "data_quality": {},
            "comparability": {},
            "tables": [],
            "limitations": [],
        },
    )

    def _proibido(*_args, **_kwargs):
        raise AssertionError("geracao analitica")

    monkeypatch.setattr(comparacao, "cleiton_governed_generate_content", _proibido)
    resposta = comparacao.chat_agente_compara_comparison_reply(
        "me envia essa análise pelo whatsapp",
        [{"role": "model", "content": TEXTO}],
        session_obj={},
        usuario=user,
        request_id="req-comparacao-web-1",
    )
    assert resposta["answer"] == entrega.MENSAGEM_ENVIADO
    assert chamadas[0][1]["json"]["text"]["body"] == TEXTO
    assert chamadas[0][1]["json"]["to"] == DESTINATARIO
    assert ExecucaoOperacionalCanal.query.count() == 0
    assert ConsumoInteracaoCanal.query.count() == 0


def test_falha_meta_no_chat_nao_confirma_sucesso(ctx, monkeypatch):
    from app import run_julia_chat as julia

    _configurar(monkeypatch)
    _mock(monkeypatch, falhar_na=1, status_erro=500)
    user = _usuario("falha.web@example.com")
    _vincular(user)
    _decidir(monkeypatch, True)
    _cliente_falso(monkeypatch, julia)
    resposta = julia.chat_julia_reply("envia isso para meu whatsapp", _historico(), usuario=user)
    assert resposta["reply"] == entrega.MENSAGEM_FALHA
    assert "Enviei" not in resposta["reply"]


def _modelo(*, funcao=False):
    nome = NOME_ENVIAR_PARA_MEU_WHATSAPP if funcao else NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP

    class _Models:
        def __init__(self):
            self.chamadas = 0

        def generate_content(self, **_kwargs):
            self.chamadas += 1
            return SimpleNamespace(
                text="Resposta logistica de frete.",
                function_calls=[SimpleNamespace(name=nome, args={})],
                candidates=[SimpleNamespace(finish_reason="STOP")],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=200,
                    candidates_token_count=800,
                    total_token_count=1000,
                ),
            )

    models = _Models()
    return SimpleNamespace(models=models), models


def _preparar_franquia(email):
    from tests.conftest import seed_cleiton_cost_config, seed_sistema_interno

    sistema = seed_sistema_interno()
    seed_cleiton_cost_config()
    user = _usuario(email)
    return sistema, user


def test_mensagem_normal_abate_franquia_somente_na_geracao(app, ctx, monkeypatch):
    from decimal import Decimal

    from app.consumo_identidade import identidade_de_usuario, set_consumo_identidade
    from app.models import Franquia

    from app import run_julia_chat as julia

    _sistema, user = _preparar_franquia("normal.franquia@example.com")
    _franquia_sistema = _sistema[1]
    cliente, _models = _modelo(funcao=False)
    monkeypatch.setattr(julia, "_get_client", lambda: cliente)
    monkeypatch.setattr(julia, "should_search_web_for_question", lambda *_args, **_kwargs: False)
    with app.test_request_context("/api/chat_julia"):
        set_consumo_identidade(identidade_de_usuario(user, "http_usuario"))
        resposta = julia.chat_julia_reply(
            "qual o prazo medio de um frete rodoviario?",
            [],
            usuario=user,
            identidade_requisicao="req-normal-1",
        )
    assert resposta["reply"] == "Resposta logistica de frete."
    db.session.expire_all()
    franquia = db.session.get(Franquia, user.franquia_id)
    interna = db.session.get(Franquia, _franquia_sistema.id)
    do_cliente = IaConsumoEvento.query.filter_by(usuario_id=user.id).all()
    internos = IaConsumoEvento.query.filter_by(origem_sistema=True).all()
    assert len(do_cliente) == 1
    assert len(internos) == 1
    assert internos[0].usuario_id is None
    assert franquia.consumo_acumulado == Decimal("1")
    assert interna.consumo_acumulado == Decimal("0")


def test_pedido_whatsapp_nao_abate_franquia_nem_consumo_de_canal(app, ctx, monkeypatch):
    from decimal import Decimal

    from app.consumo_identidade import identidade_de_usuario, set_consumo_identidade
    from app.models import Franquia

    from app import run_julia_chat as julia

    _sistema, user = _preparar_franquia("whatsapp.franquia@example.com")
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    _vincular(user)
    cliente, models = _modelo(funcao=True)
    monkeypatch.setattr(julia, "_get_client", lambda: cliente)

    def _proibido(*_args, **_kwargs):
        raise AssertionError("geracao analitica")

    monkeypatch.setattr(julia, "cleiton_governed_generate_content", _proibido)
    with app.test_request_context("/api/chat_julia"):
        set_consumo_identidade(identidade_de_usuario(user, "http_usuario"))
        resposta = julia.chat_julia_reply(
            "envia isso para meu whatsapp",
            _historico(),
            usuario=user,
            identidade_requisicao="req-whatsapp-1",
        )
    assert resposta["reply"] == entrega.MENSAGEM_ENVIADO
    assert models.chamadas == 1
    assert len(chamadas) == 1
    assert ConsumoInteracaoCanal.query.count() == 0
    db.session.expire_all()
    franquia = db.session.get(Franquia, user.franquia_id)
    interna = db.session.get(Franquia, _sistema[1].id)
    assert IaConsumoEvento.query.filter_by(usuario_id=user.id).count() == 0
    assert IaConsumoEvento.query.filter_by(origem_sistema=True).count() == 1
    assert franquia.consumo_acumulado == Decimal("0")
    assert interna.consumo_acumulado == Decimal("0")


def test_replay_da_mesma_requisicao_cria_uma_solicitacao(ctx, monkeypatch):
    from app import run_julia_chat as julia

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("replay.web@example.com")
    _vincular(user)
    _decidir(monkeypatch, True)
    _cliente_falso(monkeypatch, julia)
    primeira = julia.chat_julia_reply(
        "envia isso para meu whatsapp",
        _historico(),
        usuario=user,
        identidade_requisicao="req-replay-1",
    )
    segunda = julia.chat_julia_reply(
        "envia isso para meu whatsapp",
        _historico(),
        usuario=user,
        identidade_requisicao="req-replay-1",
    )
    assert primeira["reply"] == entrega.MENSAGEM_ENVIADO
    assert segunda["reply"] == entrega.MENSAGEM_ENVIADO
    assert SolicitacaoEntregaCanal.query.count() == 1
    assert len(chamadas) == 1
    nova = julia.chat_julia_reply(
        "envia isso para meu whatsapp",
        _historico(),
        usuario=user,
        identidade_requisicao="req-replay-2",
    )
    assert nova["reply"] == entrega.MENSAGEM_ENVIADO
    assert SolicitacaoEntregaCanal.query.count() == 2
    assert len(chamadas) == 2


def test_concorrencia_da_mesma_chave_nao_duplica_envio(tmp_path, monkeypatch):
    import threading

    from flask import Flask

    from app.services.canal_entrega_web_whatsapp_service import chave_da_requisicao_web

    banco = tmp_path / "entrega_web_concorrencia.sqlite"
    flask_app = Flask("entrega-web-concorrencia")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + banco.as_posix()
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 15},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    with flask_app.app_context():
        import app.models  # noqa: F401

        db.create_all()
        user = _usuario("concorrencia.web@example.com")
        _vincular(user)
        user_id = int(user.id)
        chave = chave_da_requisicao_web(
            user_id,
            SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            "req-concorrente-1",
        )
        barreira = threading.Barrier(2)
        resultados = {}

        def _worker(indice):
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    atual = db.session.get(User, user_id)
                    resultados[indice] = entrega.entregar_texto_no_whatsapp_do_usuario(
                        usuario=atual,
                        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
                        texto=TEXTO,
                        chave_idempotencia=chave,
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
        assert all(isinstance(item, entrega.ResultadoEntregaWeb) for item in resultados.values())
        assert all(item.codigo == entrega.CODIGO_ENVIADO for item in resultados.values())
        assert SolicitacaoEntregaCanal.query.count() == 1
        assert len(chamadas) == 1
        db.session.remove()
        db.drop_all()


def _texto_longo():
    return ("B" * 8997) + "FIM"


def _assert_partes_integrais(chamadas, texto):
    partes = [item[1]["json"]["text"]["body"] for item in chamadas]
    assert partes
    assert all(len(parte) <= 4096 for parte in partes)
    assert "".join(partes) == texto
    assert texto.endswith("FIM")
    assert "FIM" in partes[-1]


def test_auditoria_envia_texto_integral_acima_de_oito_mil(ctx, monkeypatch):
    from app import run_cleide_audit_chat as auditoria

    texto = _texto_longo()
    sanitizado = auditoria.sanitize_chat_history([{"role": "assistant", "content": texto}])
    assert len(sanitizado[0]["content"]) == auditoria.MAX_HISTORY_ITEM_CHARS
    assert "FIM" not in sanitizado[0]["content"]
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("integral.auditoria@example.com")
    _vincular(user)
    _decidir(monkeypatch, True)
    _cliente_falso(monkeypatch, auditoria)
    resposta = auditoria.chat_cleide_audit_reply(
        "manda essa análise no meu whatsapp",
        [{"role": "assistant", "content": texto}, {"role": "user", "content": "obrigado"}],
        usuario=user,
        identidade_requisicao="req-integral-auditoria",
    )
    assert resposta["answer"] == entrega.MENSAGEM_ENVIADO
    _assert_partes_integrais(chamadas, texto)


def test_comparacao_envia_texto_integral_acima_de_oito_mil(ctx, monkeypatch):
    from app import run_agente_compara_comparison_chat as comparacao
    from app.run_agente_compara_chat import MAX_HISTORY_ITEM_CHARS, sanitize_chat_history

    texto = _texto_longo()
    sanitizado = sanitize_chat_history([{"role": "model", "content": texto}])
    assert len(sanitizado[0]["content"]) == MAX_HISTORY_ITEM_CHARS
    assert "FIM" not in sanitizado[0]["content"]
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("integral.comparacao@example.com")
    _vincular(user)
    _decidir(monkeypatch, True)
    _cliente_falso(monkeypatch, comparacao)
    monkeypatch.setattr(
        comparacao,
        "evaluate_comparison_chat_availability",
        lambda **_kwargs: {"chat_available": True},
    )
    monkeypatch.setattr(
        comparacao,
        "build_comparison_chat_context",
        lambda **_kwargs: {
            "selected_scope": {"scope": "overview", "capability": "ready"},
            "comparison": {"comparison_id": "cmp-integral", "table_count": 2},
            "data_quality": {},
            "comparability": {},
            "tables": [],
            "limitations": [],
        },
    )
    monkeypatch.setattr(
        comparacao,
        "cleiton_governed_generate_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("geracao analitica")),
    )
    resposta = comparacao.chat_agente_compara_comparison_reply(
        "me envia essa análise pelo whatsapp",
        [{"role": "model", "content": texto}],
        session_obj={},
        usuario=user,
        request_id="req-integral-comparacao",
    )
    assert resposta["answer"] == entrega.MENSAGEM_ENVIADO
    _assert_partes_integrais(chamadas, texto)


def _cfg_auditoria():
    from app.services.cleide_audit_config_service import CleideAuditConfig, DEFAULT_FALLBACK_MESSAGE

    return CleideAuditConfig(
        chat_enabled=True,
        upload_enabled=True,
        chat_max_history=10,
        document_context_max_chars=24000,
        max_documents_considered=3,
        question_max_chars=4000,
        fallback_message=DEFAULT_FALLBACK_MESSAGE,
        no_documents_behavior="allow_guided",
        show_documents_used=True,
        no_hallucination_instruction_enabled=True,
        audited_file_max_bytes=None,
        audited_file_max_rows=2000,
    )


def test_auditoria_rota_envia_texto_integral(app, ctx, monkeypatch):
    from app.cleide_audit_routes import cleide_audit_bp

    app.config["SECRET_KEY"] = "teste-entrega-web"
    if "cleide_audit" not in app.blueprints:
        app.register_blueprint(cleide_audit_bp)
    texto = _texto_longo()
    user = _usuario("rota.integral.auditoria@example.com")
    _vincular(user)
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    cfg = _cfg_auditoria()
    monkeypatch.setattr("app.cleide_audit_routes.current_user", user)
    monkeypatch.setattr("app.cleide_audit_routes.get_cleide_audit_config", lambda: cfg)
    monkeypatch.setattr("app.run_cleide_audit_chat.get_cleide_audit_config", lambda: cfg)
    monkeypatch.setattr(
        "app.cleide_audit_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario_atual: {"permitido": True},
    )
    monkeypatch.setattr(
        "app.cleide_audit_routes.build_cleide_audit_document_context_for_chat",
        lambda _session: {
            "context_block": "",
            "gemini_file_parts": [],
            "has_documents": False,
            "flow_type": "cleide_audit_chat",
            "meta": {"documents": []},
        },
    )
    _decidir(monkeypatch, True)
    monkeypatch.setattr("app.run_cleide_audit_chat._get_client", lambda: object())
    monkeypatch.setattr(
        "app.run_cleide_audit_chat.cleiton_governed_generate_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("geracao analitica")),
    )
    resposta = app.test_client().post(
        "/api/cleide-auditoria/chat",
        json={
            "message": "manda essa análise no meu whatsapp",
            "request_id": "req-rota-integral-auditoria",
            "history": [{"role": "assistant", "content": texto}],
        },
    )
    assert resposta.status_code == 200
    assert resposta.get_json()["answer"] == entrega.MENSAGEM_ENVIADO
    _assert_partes_integrais(chamadas, texto)


def test_comparacao_rota_envia_texto_integral(app, ctx, monkeypatch):
    from types import SimpleNamespace as Configuracao

    from app.agente_compara_api_routes import agente_compara_api_bp

    app.config["SECRET_KEY"] = "teste-entrega-web"
    if "agente_compara_api" not in app.blueprints:
        app.register_blueprint(agente_compara_api_bp)
    texto = _texto_longo()
    user = _usuario("rota.integral.comparacao@example.com")
    _vincular(user)
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    cfg = Configuracao(
        chat_enabled=True,
        question_max_chars=4000,
        chat_max_history=10,
        comparison_chat_question_max_chars=4000,
        comparison_chat_history_max_items=10,
        fallback_message="falhou",
    )
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", user)
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", lambda: cfg)
    monkeypatch.setattr("app.run_agente_compara_comparison_chat.get_agente_compara_config", lambda: cfg)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario_atual: {"permitido": True},
    )
    monkeypatch.setattr(
        "app.agente_compara_api_routes.evaluate_comparison_chat_availability",
        lambda **_kwargs: {"chat_available": True},
    )
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.build_comparison_chat_context",
        lambda **_kwargs: {
            "selected_scope": {"scope": "overview", "capability": "ready"},
            "comparison": {"comparison_id": "cmp-rota-integral", "table_count": 2},
            "data_quality": {},
            "comparability": {},
            "tables": [],
            "limitations": [],
        },
    )
    _decidir(monkeypatch, True)
    monkeypatch.setattr("app.run_agente_compara_comparison_chat._get_client", lambda: object())
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("geracao analitica")),
    )
    resposta = app.test_client().post(
        "/api/agente-compara/comparison-chat",
        json={
            "message": "me envia essa análise pelo whatsapp",
            "request_id": "req-rota-integral-comparacao",
            "history": [{"role": "model", "content": texto}],
        },
    )
    assert resposta.status_code == 200
    assert resposta.get_json()["answer"] == entrega.MENSAGEM_ENVIADO
    _assert_partes_integrais(chamadas, texto)


def test_auditoria_cacheada_nao_classifica_de_novo(app, ctx, monkeypatch):
    from app.cleide_audit_routes import cleide_audit_bp
    from app.services.cleide_audit_config_service import CleideAuditConfig, DEFAULT_FALLBACK_MESSAGE

    app.config["SECRET_KEY"] = "teste-entrega-web"
    if "cleide_audit" not in app.blueprints:
        app.register_blueprint(cleide_audit_bp)
    user = _usuario("cache.auditoria@example.com")
    cfg = CleideAuditConfig(
        chat_enabled=True,
        upload_enabled=True,
        chat_max_history=10,
        document_context_max_chars=24000,
        max_documents_considered=3,
        question_max_chars=4000,
        fallback_message=DEFAULT_FALLBACK_MESSAGE,
        no_documents_behavior="allow_guided",
        show_documents_used=True,
        no_hallucination_instruction_enabled=True,
        audited_file_max_bytes=None,
        audited_file_max_rows=2000,
    )
    monkeypatch.setattr("app.cleide_audit_routes.current_user", user)
    monkeypatch.setattr("app.cleide_audit_routes.get_cleide_audit_config", lambda: cfg)
    monkeypatch.setattr("app.run_cleide_audit_chat.get_cleide_audit_config", lambda: cfg)
    monkeypatch.setattr(
        "app.cleide_audit_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario_atual: {"permitido": True},
    )
    monkeypatch.setattr(
        "app.cleide_audit_routes.build_cleide_audit_document_context_for_chat",
        lambda _session: {
            "context_block": "",
            "gemini_file_parts": [],
            "has_documents": False,
            "flow_type": "cleide_audit_chat",
            "meta": {"documents": []},
        },
    )
    contagem = {"decidir": 0, "gerar": 0}

    def _decidir(*_args, **_kwargs):
        contagem["decidir"] += 1
        return False

    def _gerar(*_args, **_kwargs):
        contagem["gerar"] += 1
        return SimpleNamespace(text="Resposta em cache.", usage_metadata=None)

    monkeypatch.setattr(entrega, "decidir_enviar_para_meu_whatsapp", _decidir)
    monkeypatch.setattr("app.run_cleide_audit_chat._get_client", lambda: object())
    monkeypatch.setattr("app.run_cleide_audit_chat.cleiton_governed_generate_content", _gerar)
    client = app.test_client()
    payload = {"message": "auditar este frete", "request_id": "req-cache-auditoria", "history": []}
    primeira = client.post("/api/cleide-auditoria/chat", json=payload)
    segunda = client.post("/api/cleide-auditoria/chat", json=payload)
    assert primeira.status_code == 200
    assert segunda.status_code == 200
    assert segunda.get_json().get("cached") is True
    assert contagem == {"decidir": 1, "gerar": 1}


def test_comparacao_nao_ready_nao_chama_gemini(app, ctx, monkeypatch):
    from types import SimpleNamespace as Configuracao

    from app.agente_compara_api_routes import agente_compara_api_bp

    if "agente_compara_api" not in app.blueprints:
        app.register_blueprint(agente_compara_api_bp)
    user = _usuario("nao.ready.rota@example.com")
    cfg = Configuracao(
        chat_enabled=True,
        question_max_chars=4000,
        chat_max_history=10,
        comparison_chat_question_max_chars=4000,
        comparison_chat_history_max_items=10,
        fallback_message="falhou",
    )
    monkeypatch.setattr("app.agente_compara_api_routes.current_user", user)
    monkeypatch.setattr("app.agente_compara_api_routes.get_agente_compara_config", lambda: cfg)
    monkeypatch.setattr(
        "app.agente_compara_api_routes.avaliar_autorizacao_operacao_por_franquia",
        lambda _usuario_atual: {"permitido": True},
    )
    contagem = {"decidir": 0, "gerar": 0}

    def _decidir(*_args, **_kwargs):
        contagem["decidir"] += 1
        return True

    def _gerar(*_args, **_kwargs):
        contagem["gerar"] += 1
        raise AssertionError("gemini")

    monkeypatch.setattr(entrega, "decidir_enviar_para_meu_whatsapp", _decidir)
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.cleiton_governed_generate_content",
        _gerar,
    )
    monkeypatch.setattr("app.run_agente_compara_comparison_chat._get_client", lambda: object())
    client = app.test_client()
    resposta = client.post(
        "/api/agente-compara/comparison-chat",
        json={"message": "envia isso para meu whatsapp", "history": [{"role": "model", "content": TEXTO}]},
    )
    assert resposta.status_code == 409
    assert contagem == {"decidir": 0, "gerar": 0}


def test_comparacao_cacheada_nao_classifica_de_novo(ctx, monkeypatch):
    from app import run_agente_compara_comparison_chat as comparacao

    user = _usuario("cache.comparacao@example.com")
    _cliente_falso(monkeypatch, comparacao)
    monkeypatch.setattr(
        comparacao,
        "evaluate_comparison_chat_availability",
        lambda **_kwargs: {"chat_available": True},
    )
    monkeypatch.setattr(
        comparacao,
        "build_comparison_chat_context",
        lambda **_kwargs: {
            "selected_scope": {"scope": "overview", "capability": "ready"},
            "comparison": {"comparison_id": "cmp-cache", "table_count": 2},
            "data_quality": {},
            "comparability": {},
            "tables": [],
            "limitations": [],
        },
    )
    contagem = {"decidir": 0, "gerar": 0}

    def _decidir(*_args, **_kwargs):
        contagem["decidir"] += 1
        return False

    def _gerar(*_args, **_kwargs):
        contagem["gerar"] += 1
        return SimpleNamespace(text="Cobertura da tabela Alpha.", usage_metadata=None)

    monkeypatch.setattr(entrega, "decidir_enviar_para_meu_whatsapp", _decidir)
    monkeypatch.setattr(comparacao, "cleiton_governed_generate_content", _gerar)

    class _Sessao(dict):
        modified = False

    sessao = _Sessao()
    primeira = comparacao.chat_agente_compara_comparison_reply(
        "qual tabela cobre mais?",
        [],
        session_obj=sessao,
        usuario=user,
        request_id="req-cache-comparacao",
    )
    segunda = comparacao.chat_agente_compara_comparison_reply(
        "qual tabela cobre mais?",
        [],
        session_obj=sessao,
        usuario=user,
        request_id="req-cache-comparacao",
    )
    assert primeira.get("answer")
    assert segunda.get("cached") is True
    assert contagem == {"decidir": 1, "gerar": 1}


def _gates_comparacao(monkeypatch, comparacao):
    monkeypatch.setattr(
        comparacao,
        "evaluate_comparison_chat_availability",
        lambda **_kwargs: {"chat_available": True},
    )
    monkeypatch.setattr(
        comparacao,
        "build_comparison_chat_context",
        lambda **_kwargs: {
            "selected_scope": {"scope": "overview", "capability": "ready"},
            "comparison": {"comparison_id": "cmp-intencao", "table_count": 2},
            "data_quality": {},
            "comparability": {},
            "tables": [],
            "limitations": [],
        },
    )


def test_julia_falha_tecnica_nao_gera_resposta_logistica(ctx, monkeypatch):
    from app import run_julia_chat as julia

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("falha.classificacao.julia@example.com")
    _vincular(user)
    _decidir(monkeypatch, intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA)
    _cliente_falso(monkeypatch, julia)

    def _proibido(*_args, **_kwargs):
        raise AssertionError("geracao analitica")

    monkeypatch.setattr(julia, "cleiton_governed_generate_content", _proibido)
    resposta = julia.chat_julia_reply(
        "Envie o resumo para meu WhatsApp.",
        _historico(),
        usuario=user,
    )
    assert resposta["reply"] == entrega.MENSAGEM_FALHA_CLASSIFICACAO
    assert chamadas == []


def test_auditoria_negativo_segue_chat(ctx, monkeypatch):
    from app import run_cleide_audit_chat as auditoria

    user = _usuario("negativo.auditoria@example.com")
    _decidir(monkeypatch, False)
    _cliente_falso(monkeypatch, auditoria)
    geracoes = []

    def _gerar(*_args, **_kwargs):
        geracoes.append(True)
        return SimpleNamespace(text="A cobrança está consistente.", usage_metadata=None)

    monkeypatch.setattr(auditoria, "cleiton_governed_generate_content", _gerar)
    resposta = auditoria.chat_cleide_audit_reply(
        "Meu WhatsApp está conectado?",
        [{"role": "assistant", "content": TEXTO}],
        usuario=user,
    )
    assert resposta["answer"] == "A cobrança está consistente."
    assert geracoes == [True]


def test_auditoria_falha_tecnica_nao_gera_resposta_logistica(ctx, monkeypatch):
    from app import run_cleide_audit_chat as auditoria

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("falha.classificacao.auditoria@example.com")
    _vincular(user)
    _decidir(monkeypatch, intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA)
    _cliente_falso(monkeypatch, auditoria)

    def _proibido(*_args, **_kwargs):
        raise AssertionError("geracao analitica")

    monkeypatch.setattr(auditoria, "cleiton_governed_generate_content", _proibido)
    resposta = auditoria.chat_cleide_audit_reply(
        "Envie o resumo para meu WhatsApp.",
        [{"role": "assistant", "content": TEXTO}],
        usuario=user,
    )
    assert resposta["answer"] == entrega.MENSAGEM_FALHA_CLASSIFICACAO
    assert chamadas == []


def test_comparacao_negativo_segue_chat(ctx, monkeypatch):
    from app import run_agente_compara_comparison_chat as comparacao

    user = _usuario("negativo.comparacao@example.com")
    _decidir(monkeypatch, False)
    _cliente_falso(monkeypatch, comparacao)
    _gates_comparacao(monkeypatch, comparacao)
    geracoes = []

    def _gerar(*_args, **_kwargs):
        geracoes.append(True)
        return SimpleNamespace(text="A tabela Alpha cobre mais faixas.", usage_metadata=None)

    monkeypatch.setattr(comparacao, "cleiton_governed_generate_content", _gerar)
    resposta = comparacao.chat_agente_compara_comparison_reply(
        "Explique como funciona o WhatsApp.",
        [{"role": "model", "content": TEXTO}],
        session_obj={},
        usuario=user,
    )
    assert resposta["answer"] == "A tabela Alpha cobre mais faixas."
    assert geracoes == [True]


def test_comparacao_falha_tecnica_nao_gera_resposta_logistica(ctx, monkeypatch):
    from app import run_agente_compara_comparison_chat as comparacao

    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("falha.classificacao.comparacao@example.com")
    _vincular(user)
    _decidir(monkeypatch, intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA)
    _cliente_falso(monkeypatch, comparacao)
    _gates_comparacao(monkeypatch, comparacao)

    def _proibido(*_args, **_kwargs):
        raise AssertionError("geracao analitica")

    monkeypatch.setattr(comparacao, "cleiton_governed_generate_content", _proibido)
    resposta = comparacao.chat_agente_compara_comparison_reply(
        "Envie o resumo para meu WhatsApp.",
        [{"role": "model", "content": TEXTO}],
        session_obj={},
        usuario=user,
    )
    assert resposta["answer"] == entrega.MENSAGEM_FALHA_CLASSIFICACAO
    assert chamadas == []


def _cliente_que_registra_identidade(*, nome=None, erro=None):
    from flask import g

    vistas = []

    class _Models:
        def __init__(self):
            self.chamadas = 0

        def generate_content(self, **_kwargs):
            self.chamadas += 1
            vistas.append(
                {
                    "tipo_origem": g.identidade.get("tipo_origem"),
                    "origem_sistema": g.identidade.get("origem_sistema"),
                    "usuario_id": g.identidade.get("usuario_id"),
                }
            )
            if erro is not None:
                raise erro
            return SimpleNamespace(
                function_calls=[SimpleNamespace(name=nome, args={})],
                candidates=[SimpleNamespace(finish_reason="STOP")],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=20,
                    candidates_token_count=8,
                    total_token_count=28,
                ),
            )

    models = _Models()
    return SimpleNamespace(models=models), models, vistas


def _assert_classificacao_sem_debito(user, *, eventos_internos):
    from decimal import Decimal

    from app.models import Franquia

    db.session.expire_all()
    franquia = db.session.get(Franquia, user.franquia_id)
    do_cliente = IaConsumoEvento.query.filter_by(usuario_id=user.id).all()
    internos = IaConsumoEvento.query.filter_by(origem_sistema=True).all()
    assert do_cliente == []
    assert len(internos) == eventos_internos
    assert all(item.usuario_id is None for item in internos)
    assert all(item.origem_sistema is True for item in internos)
    assert franquia.consumo_acumulado == Decimal("0")
    assert ConsumoInteracaoCanal.query.count() == 0


def test_classificacao_positiva_nao_debita_e_restaura_identidade(app, ctx, monkeypatch):
    from flask import g

    from app.consumo_identidade import identidade_de_usuario, set_consumo_identidade

    _sistema, user = _preparar_franquia("positivo.franquia.intencao@example.com")
    cliente, _models, vistas = _cliente_que_registra_identidade(nome=NOME_ENVIAR_PARA_MEU_WHATSAPP)
    with app.test_request_context("/api/chat_julia"):
        set_consumo_identidade(identidade_de_usuario(user, "http_usuario"))
        anterior = dict(g.identidade)
        decisao = intencao.decidir_enviar_para_meu_whatsapp(
            "Envie o resumo para meu WhatsApp.",
            agent="julia",
            flow_type="julia_chat",
            client=cliente,
            model="gemini-2.5-flash",
            api_key_label="teste",
        )
        depois = dict(g.identidade)
    assert decisao is intencao.DecisaoEntregaWhatsApp.POSITIVO
    assert vistas[0]["tipo_origem"] == "interno_nao_faturavel"
    assert vistas[0]["origem_sistema"] is True
    assert vistas[0]["usuario_id"] is None
    assert depois == anterior
    _assert_classificacao_sem_debito(user, eventos_internos=1)


def test_classificacao_negativa_nao_debita_e_restaura_identidade(app, ctx, monkeypatch):
    from flask import g

    from app.consumo_identidade import identidade_de_usuario, set_consumo_identidade

    _sistema, user = _preparar_franquia("negativo.franquia.intencao@example.com")
    cliente, _models, vistas = _cliente_que_registra_identidade(nome=NOME_NAO_ENVIAR_PARA_MEU_WHATSAPP)
    with app.test_request_context("/api/chat_julia"):
        set_consumo_identidade(identidade_de_usuario(user, "http_usuario"))
        anterior = dict(g.identidade)
        decisao = intencao.decidir_enviar_para_meu_whatsapp(
            "Não envie isso ao meu WhatsApp.",
            agent="cleide",
            flow_type="cleide_audit_chat",
            client=cliente,
            model="gemini-2.5-flash",
            api_key_label="teste",
        )
        depois = dict(g.identidade)
    assert decisao is intencao.DecisaoEntregaWhatsApp.NEGATIVO
    assert vistas[0]["tipo_origem"] == "interno_nao_faturavel"
    assert depois == anterior
    _assert_classificacao_sem_debito(user, eventos_internos=1)


def test_falha_tecnica_com_retry_nao_debita_e_restaura_identidade(app, ctx, monkeypatch):
    from flask import g

    from app.consumo_identidade import identidade_de_usuario, set_consumo_identidade

    _sistema, user = _preparar_franquia("falha.franquia.intencao@example.com")
    cliente, models, vistas = _cliente_que_registra_identidade(erro=TimeoutError("timeout"))
    with app.test_request_context("/api/chat_julia"):
        set_consumo_identidade(identidade_de_usuario(user, "http_usuario"))
        anterior = dict(g.identidade)
        decisao = intencao.decidir_enviar_para_meu_whatsapp(
            "Mande para 19 99999-9999.",
            agent="agente_compara",
            flow_type="agente_compara_comparison_chat",
            client=cliente,
            model="gemini-2.5-flash",
            api_key_label="teste",
        )
        depois = dict(g.identidade)
    assert decisao is intencao.DecisaoEntregaWhatsApp.FALHA_TECNICA
    assert models.chamadas == 2
    assert len(vistas) == 2
    assert all(item["tipo_origem"] == "interno_nao_faturavel" for item in vistas)
    assert all(item["origem_sistema"] is True for item in vistas)
    assert depois == anterior
    _assert_classificacao_sem_debito(user, eventos_internos=2)
