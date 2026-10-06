"""Executor, estado e segurança da orquestração contextual de WhatsApp.

A compreensão semântica fica no modelo. Estes testes não tratam um booleano
mockado como prova de que uma frase foi entendida.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    ConsumoInteracaoCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    IdentidadeCanalExterna,
    RascunhoEntregaWhatsApp,
    SolicitacaoEntregaCanal,
    User,
    utcnow_naive,
)
from app.services import canal_entrega_web_whatsapp_service as entrega
from app.services import canal_orquestracao_whatsapp_contextual_service as orquestracao
from app.services import whatsapp_meta_cloud_api_adapter as adapter
from app.services import whatsapp_meta_config as config
from app.services.canal_aquisicao_service import PROVEDOR_WHATSAPP_META
from app.services.canal_elegibilidade_whatsapp_service import (
    MENSAGEM_TERCEIRO_BLOQUEADO,
    elegibilidade_terceiro,
)
from app.services.canal_execucao_whatsapp_contextual_service import (
    executar_acao_whatsapp,
)
from app.services.canal_orquestracao_whatsapp_contextual_service import (
    NOME_ATUALIZAR_TERCEIRO,
    NOME_CANCELAR_TERCEIRO,
    NOME_CONFIRMAR_TERCEIRO,
    NOME_CONTINUAR,
    NOME_ENVIAR_PARA_MEU_WHATSAPP,
    NOME_ESCLARECER,
    NOME_PREPARAR_TERCEIRO,
    TIPO_FALHA_TECNICA,
    AcaoWhatsAppContextual,
    CatalogoDecisaoWhatsApp,
)
from app.services.canal_rascunho_entrega_whatsapp_service import (
    MENSAGEM_CANCELADO,
    MENSAGEM_DDI,
    MENSAGEM_RECUSADO,
    atualizar_rascunho,
    cancelar_rascunho,
    confirmar_rascunho,
    preparar_rascunho,
    rascunho_aberto,
)
from app.services.canal_resposta_compartilhavel_service import (
    montar_contexto,
    registrar_resposta_compartilhavel,
    resolver_resposta_compartilhavel,
)
from app.services.canal_telefone_declarado_service import (
    alias_telefones_do_turno,
    normalizar_telefone_e164,
    ocultar_telefones,
)
from tests.conftest import seed_conta_franquia_cliente

PHONE_NUMBER_ID = "106540352242922"
PROPRIO = "16505551234"
TERCEIRO = "+5519999999999"
TEXTO_SERVIDOR = "Análise autorizada pelo backend."
TEXTO_NAVEGADOR = "Texto vindo do navegador, que não pode ser enviado."


def _usuario(email="orquestracao@example.com"):
    conta, franquia = seed_conta_franquia_cliente(email.split("@")[0])
    user = User(
        email=email,
        full_name="Orquestracao",
        conta_id=conta.id,
        franquia_id=franquia.id,
        sessao_contexto_geracao=0,
    )
    db.session.add(user)
    db.session.commit()
    return user


def _vincular(user, sujeito=PROPRIO, contexto=PHONE_NUMBER_ID, estado=None, revogada=False):
    agora = utcnow_naive()
    row = IdentidadeCanalExterna(
        provedor=PROVEDOR_WHATSAPP_META,
        sujeito_externo=sujeito,
        contexto_destino=contexto,
        estado=estado or IdentidadeCanalExterna.ESTADO_VINCULADA,
        user_id=None if estado == IdentidadeCanalExterna.ESTADO_GUEST else int(user.id),
        interacoes_uteis=0,
        criada_em=agora,
        atualizada_em=agora,
        vinculada_em=None if estado == IdentidadeCanalExterna.ESTADO_GUEST else agora,
        revogada_em=agora if revogada else None,
    )
    if revogada:
        row.estado = IdentidadeCanalExterna.ESTADO_REVOGADA
    db.session.add(row)
    db.session.commit()
    return row


def _entrada(sujeito, horas=1):
    evento = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=f"in-{sujeito}-{horas}",
        tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=sujeito,
        contexto_destino=PHONE_NUMBER_ID,
        recebido_em=utcnow_naive() - timedelta(hours=horas),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id="correntradaorq1",
        diagnostico_seguro="mensagem_textual",
    )
    db.session.add(evento)
    db.session.commit()
    return evento


def _registrar(user, texto=TEXTO_SERVIDOR, superficie=None, comparison_id=None, escopo=None):
    superficie = superficie or SolicitacaoEntregaCanal.SUPERFICIE_JULIA
    contexto = montar_contexto(
        superficie=superficie,
        user_id=int(user.id),
        comparison_id=comparison_id,
        escopo_auditoria=escopo,
    )
    referencia = registrar_resposta_compartilhavel(
        usuario=user,
        superficie=superficie,
        contexto_conversa=contexto,
        texto=texto,
        comparison_id=comparison_id,
        escopo_auditoria=escopo,
    )
    return contexto, referencia


def _catalogo(user, contexto, *, referencia, telefone=None, proprio=True, rascunho=None):
    return CatalogoDecisaoWhatsApp(
        mensagem="pedido atual",
        historico=[{"role": "model", "content": TEXTO_SERVIDOR, "conteudo_alias": "[CONTEUDO_1]"}],
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        whatsapp_proprio_valido=proprio,
        conteudos={"[CONTEUDO_1]": referencia},
        telefones={"[TEL_1]": telefone} if telefone else {},
        rascunho_alias="[RASCUNHO_1]" if rascunho is not None else None,
        rascunho_referencia=rascunho.referencia if rascunho is not None else None,
        rascunho_estado=rascunho.estado if rascunho is not None else None,
        rascunho_versao=int(rascunho.versao) if rascunho is not None else None,
        rascunho_nome=rascunho.nome_destinatario if rascunho is not None else None,
        rascunho_tem_telefone=bool(rascunho is not None and rascunho.telefone_e164),
        confirmacao_alias="[CONFIRMACAO_1]" if rascunho is not None and rascunho.confirmacao_referencia else None,
        confirmacao_referencia=rascunho.confirmacao_referencia if rascunho is not None else None,
        capabilities=(
            NOME_CONTINUAR,
            NOME_ESCLARECER,
            NOME_ENVIAR_PARA_MEU_WHATSAPP,
            NOME_PREPARAR_TERCEIRO,
            NOME_ATUALIZAR_TERCEIRO,
            NOME_CANCELAR_TERCEIRO,
            NOME_CONFIRMAR_TERCEIRO,
        ),
    )


def _configurar(monkeypatch):
    monkeypatch.setenv(config.ENV_ACCESS_TOKEN, "EAAWHATSAPPWEBTOKENSECRETO")
    monkeypatch.setenv(config.ENV_GRAPH_API_VERSION, "v26.0")
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


def _mock(monkeypatch, *, erro=None):
    chamadas = []

    def _post(url, **kwargs):
        chamadas.append((url, kwargs))
        if erro is not None:
            raise erro
        return _Resposta(200, {"messages": [{"id": f"wamid.ORQ{len(chamadas)}"}]})

    monkeypatch.setattr(adapter.requests, "post", _post)
    return chamadas


def _resposta_tool(nome, argumentos, finish_reason="STOP"):
    return SimpleNamespace(
        function_calls=[SimpleNamespace(name=nome, args=argumentos)],
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
    )


def test_telefone_sem_ddi_nao_assume_pais():
    assert normalizar_telefone_e164("19 99999-9999").codigo == "sem_ddi"
    assert normalizar_telefone_e164("5519999999999").codigo == "sem_ddi"
    normalizado = normalizar_telefone_e164("+55 19 99999-9999")
    assert normalizado.codigo == "ok"
    assert normalizado.e164 == "+5519999999999"
    assert normalizar_telefone_e164("abc").codigo == "invalido"
    assert normalizar_telefone_e164("+0123").codigo == "invalido"


def test_alias_do_turno_nao_trata_telefone_de_resposta_como_declarado():
    mensagem, declarados = alias_telefones_do_turno("manda para +55 19 99999-9999")
    assert declarados[0].alias == "[TEL_1]"
    assert "+55" not in mensagem
    oculto = ocultar_telefones("O contato da transportadora é +1 202 555 0143.")
    assert "+1" not in oculto
    assert "[TEL_0]" in oculto
    assert "[TEL_0]" not in {item.alias for item in declarados}


def test_conteudo_de_outra_comparacao_nao_resolve(ctx):
    user = _usuario()
    _registrar(user, superficie=SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO, comparison_id="cmp-a")
    contexto_b = montar_contexto(
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO,
        user_id=int(user.id),
        comparison_id="cmp-b",
    )
    assert (
        resolver_resposta_compartilhavel(
            user_id=int(user.id),
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_COMPARACAO,
            contexto_conversa=contexto_b,
            referencia="inexistente",
            comparison_id="cmp-b",
        )
        is None
    )


def test_envio_proprio_usa_texto_do_backend_e_vinculo_do_user(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("proprio.orq@example.com")
    _vincular(user)
    contexto, referencia = _registrar(user)
    catalogo = _catalogo(user, contexto, referencia=referencia)
    efeito = executar_acao_whatsapp(
        AcaoWhatsAppContextual(NOME_ENVIAR_PARA_MEU_WHATSAPP, {"conteudo_ref": "[CONTEUDO_1]"}),
        catalogo=catalogo,
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        identidade_requisicao="req-proprio-1",
    )
    resultado = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=efeito.texto_proprio,
        chave_idempotencia="a" * 40,
    )
    assert resultado.codigo == entrega.CODIGO_ENVIADO
    assert chamadas[0][1]["json"]["text"]["body"] == TEXTO_SERVIDOR
    assert chamadas[0][1]["json"]["to"] == PROPRIO
    assert TEXTO_NAVEGADOR not in chamadas[0][1]["json"]["text"]["body"]
    assert ConsumoInteracaoCanal.query.count() == 0


def test_telefone_da_analise_nao_vira_destinatario(ctx):
    user = _usuario("analise.orq@example.com")
    contexto, referencia = _registrar(user, texto="Frete da rota com contato +55 11 98888-7777.")
    catalogo = _catalogo(user, contexto, referencia=referencia)
    efeito = executar_acao_whatsapp(
        AcaoWhatsAppContextual(
            NOME_PREPARAR_TERCEIRO,
            {"conteudo_ref": "[CONTEUDO_1]", "nome_destinatario": "João"},
        ),
        catalogo=catalogo,
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        identidade_requisicao="req-analise-1",
    )
    row = RascunhoEntregaWhatsApp.query.one()
    assert efeito.codigo == "aguardando_telefone"
    assert row.telefone_e164 is None
    assert row.estado == RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_TELEFONE
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_alias_inventado_e_de_outro_turno_falham():
    catalogo = CatalogoDecisaoWhatsApp(
        mensagem="segue o numero",
        historico=[],
        superficie="julia",
        whatsapp_proprio_valido=True,
        conteudos={"[CONTEUDO_1]": "rc" + "a" * 32},
        telefones={"[TEL_1]": "+5519999999999"},
        capabilities=(NOME_PREPARAR_TERCEIRO, NOME_CONTINUAR),
    )
    inventado = orquestracao.interpretar_resposta_orquestracao(
        _resposta_tool(NOME_PREPARAR_TERCEIRO, {"conteudo_ref": "[CONTEUDO_1]", "telefone_declarado_ref": "[TEL_9]"}),
        catalogo=catalogo,
    )
    oculto = orquestracao.interpretar_resposta_orquestracao(
        _resposta_tool(NOME_PREPARAR_TERCEIRO, {"conteudo_ref": "[CONTEUDO_1]", "telefone_declarado_ref": "[TEL_0]"}),
        catalogo=catalogo,
    )
    assert inventado.tipo == TIPO_FALHA_TECNICA
    assert oculto.tipo == TIPO_FALHA_TECNICA


def test_argumento_extra_e_funcao_desconhecida_sao_falha_tecnica():
    catalogo = CatalogoDecisaoWhatsApp(
        mensagem="manda",
        historico=[],
        superficie="julia",
        whatsapp_proprio_valido=True,
        conteudos={"[CONTEUDO_1]": "rc" + "b" * 32},
        telefones={},
        capabilities=(NOME_ENVIAR_PARA_MEU_WHATSAPP, NOME_CONTINUAR),
    )
    extra = orquestracao.interpretar_resposta_orquestracao(
        _resposta_tool(NOME_ENVIAR_PARA_MEU_WHATSAPP, {"conteudo_ref": "[CONTEUDO_1]", "telefone": PROPRIO}),
        catalogo=catalogo,
    )
    desconhecida = orquestracao.interpretar_resposta_orquestracao(
        _resposta_tool("enviar_para_qualquer_um", {}),
        catalogo=catalogo,
    )
    vazia = orquestracao.interpretar_resposta_orquestracao(
        SimpleNamespace(function_calls=[], candidates=[SimpleNamespace(finish_reason="STOP")]),
        catalogo=catalogo,
    )
    assert extra.tipo == TIPO_FALHA_TECNICA
    assert desconhecida.tipo == TIPO_FALHA_TECNICA
    assert vazia.tipo == TIPO_FALHA_TECNICA


def test_prompt_leva_historico_e_nao_leva_telefone_nem_segredo():
    catalogo = CatalogoDecisaoWhatsApp(
        mensagem="então manda lá",
        historico=[
            {"role": "model", "content": "Análise dos três riscos.", "conteudo_alias": "[CONTEUDO_1]"},
            {"role": "user", "content": "nem por WhatsApp?"},
        ],
        superficie="julia",
        whatsapp_proprio_valido=True,
        conteudos={"[CONTEUDO_1]": "rc" + "c" * 32},
        telefones={"[TEL_1]": "+5519999999999"},
        rascunho_alias="[RASCUNHO_1]",
        rascunho_estado="aguardando_telefone",
        rascunho_versao=1,
        capabilities=(NOME_CONTINUAR,),
    )
    conteudo = orquestracao.montar_conteudo_decisao(catalogo)
    assert "Análise dos três riscos." in conteudo
    assert "nem por WhatsApp?" in conteudo
    assert "então manda lá" in conteudo
    assert "[CONTEUDO_1]" in conteudo
    assert "[TEL_1]" in conteudo
    assert "5519999999999" not in conteudo
    assert "access_token" not in conteudo
    assert PHONE_NUMBER_ID not in conteudo


def test_preparar_pede_telefone_e_replay_nao_duplica(ctx):
    user = _usuario("prepara.orq@example.com")
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    primeiro = preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status=None,
        telefone_e164=None,
        telefone_exibicao=None,
        turno_telefone_ref=None,
        chave_preparo="1" * 64,
    )
    segundo = preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status=None,
        telefone_e164=None,
        telefone_exibicao=None,
        turno_telefone_ref=None,
        chave_preparo="1" * 64,
    )
    assert primeiro.codigo == "aguardando_telefone"
    assert "João" in primeiro.mensagem
    assert segundo.rascunho_id == primeiro.rascunho_id
    assert RascunhoEntregaWhatsApp.query.count() == 1
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_telefone_seguinte_pede_confirmacao_e_ddi_ausente_nao_avanca(ctx):
    user = _usuario("telefone.orq@example.com")
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    base = dict(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_exibicao=None,
        turno_telefone_ref="[TEL_1]",
        chave_preparo=None,
    )
    sem_ddi = preparar_rascunho(**base, telefone_status="sem_ddi", telefone_e164=None)
    assert sem_ddi.mensagem == MENSAGEM_DDI
    assert RascunhoEntregaWhatsApp.query.one().estado == RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_TELEFONE
    completo = preparar_rascunho(
        **{
            **base,
            "telefone_exibicao": "+55 19 99999-9999",
            "chave_preparo": "2" * 64,
        },
        telefone_status="ok",
        telefone_e164=TERCEIRO,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    assert completo.codigo == "confirmacao"
    assert "Confirmar?" in completo.mensagem
    assert row.estado == RascunhoEntregaWhatsApp.ESTADO_AGUARDANDO_CONFIRMACAO
    assert row.telefone_e164 == TERCEIRO
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_correcao_de_telefone_incrementa_versao_e_invalida_confirmacao(ctx):
    user = _usuario("correcao.orq@example.com")
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status="ok",
        telefone_e164=TERCEIRO,
        telefone_exibicao="+55 19 99999-9999",
        turno_telefone_ref="[TEL_1]",
        chave_preparo="3" * 64,
    )
    anterior = RascunhoEntregaWhatsApp.query.one()
    token_antigo = anterior.confirmacao_referencia
    versao_antiga = int(anterior.versao)
    from app.services.canal_rascunho_entrega_whatsapp_service import atualizar_rascunho

    atualizar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=anterior.referencia,
        versao=versao_antiga,
        conteudo_referencia=None,
        content_hash=None,
        nome_destinatario=None,
        nome_informado=False,
        telefone_status="ok",
        telefone_e164="+14155552671",
        telefone_exibicao="+1 415 555 2671",
        turno_telefone_ref="[TEL_1]",
    )
    row = db.session.get(RascunhoEntregaWhatsApp, int(anterior.id))
    assert int(row.versao) == versao_antiga + 1
    assert row.confirmacao_referencia != token_antigo
    assert row.confirmacao_versao == int(row.versao)
    recusa = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=versao_antiga,
        confirmacao_referencia=token_antigo,
    )
    assert recusa.codigo == "recusado"
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_cancelar_nao_cria_entrega(ctx):
    user = _usuario("cancela.orq@example.com")
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status=None,
        telefone_e164=None,
        telefone_exibicao=None,
        turno_telefone_ref=None,
        chave_preparo="4" * 64,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    efeito = cancelar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=int(row.versao),
    )
    assert efeito.mensagem == MENSAGEM_CANCELADO
    assert db.session.get(RascunhoEntregaWhatsApp, row.id).estado == RascunhoEntregaWhatsApp.ESTADO_CANCELADO
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_rascunho_de_outro_user_ou_conversa_falha(ctx):
    dono = _usuario("dono.orq@example.com")
    outro = _usuario("outro.orq@example.com")
    contexto, referencia = _registrar(dono)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(dono.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(dono.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status="ok",
        telefone_e164=TERCEIRO,
        telefone_exibicao=TERCEIRO,
        turno_telefone_ref="[TEL_1]",
        chave_preparo="5" * 64,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    alheio = confirmar_rascunho(
        user_id=int(outro.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    outra_conversa = confirmar_rascunho(
        user_id=int(dono.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa="julia:outra",
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    assert alheio.mensagem == MENSAGEM_RECUSADO
    assert outra_conversa.mensagem == MENSAGEM_RECUSADO
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_confirmacao_expirada_nao_envia_e_pede_outra(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("expira.orq@example.com")
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status="ok",
        telefone_e164=TERCEIRO,
        telefone_exibicao=TERCEIRO,
        turno_telefone_ref="[TEL_1]",
        chave_preparo="6" * 64,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    row.expira_em = utcnow_naive() - timedelta(minutes=1)
    db.session.commit()
    efeito = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    assert efeito.codigo == "confirmacao"
    assert "Enviei" not in efeito.mensagem
    assert chamadas == []
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_terceiro_sem_janela_nao_diz_que_enviou(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("janela.orq@example.com")
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status="ok",
        telefone_e164=TERCEIRO,
        telefone_exibicao="+55 19 99999-9999",
        turno_telefone_ref="[TEL_1]",
        chave_preparo="7" * 64,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    efeito = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    assert efeito.codigo == "bloqueado_elegibilidade"
    assert efeito.mensagem == MENSAGEM_TERCEIRO_BLOQUEADO
    assert "Enviei" not in efeito.mensagem
    assert chamadas == []
    assert db.session.get(RascunhoEntregaWhatsApp, row.id).estado == (
        RascunhoEntregaWhatsApp.ESTADO_BLOQUEADO_ELEGIBILIDADE
    )


def test_consentimento_revogado_e_janela_fechada(ctx):
    user = _usuario("revogada.orq@example.com")
    _vincular(
        user,
        sujeito="5519999999999",
        estado=IdentidadeCanalExterna.ESTADO_VINCULADA,
        revogada=True,
    )
    revogada = elegibilidade_terceiro(TERCEIRO)
    assert revogada.elegivel is False
    assert revogada.codigo == "consentimento_revogado"
    ativa = _usuario("ativa.orq@example.com")
    _vincular(ativa, sujeito="14155552671")
    _entrada("14155552671", horas=30)
    fechada = elegibilidade_terceiro("+14155552671")
    assert fechada.elegivel is False
    assert fechada.codigo == "sem_janela"
    _entrada("14155552671", horas=1)
    aberta = elegibilidade_terceiro("+14155552671")
    assert aberta.elegivel is True
    assert aberta.codigo == "janela_aberta"


def test_terceiro_elegivel_envia_uma_vez_e_replay_nao_repete(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch)
    user = _usuario("elegivel.orq@example.com")
    _vincular(user, sujeito="5519999999999")
    _entrada("5519999999999", horas=1)
    contexto, referencia = _registrar(user)
    catalogo = _catalogo(user, contexto, referencia=referencia, telefone=TERCEIRO)
    preparado = executar_acao_whatsapp(
        AcaoWhatsAppContextual(
            NOME_PREPARAR_TERCEIRO,
            {
                "conteudo_ref": "[CONTEUDO_1]",
                "nome_destinatario": "João",
                "telefone_declarado_ref": "[TEL_1]",
            },
        ),
        catalogo=catalogo,
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        identidade_requisicao="req-elegivel-1",
    )
    assert preparado.codigo == "confirmacao"
    assert SolicitacaoEntregaCanal.query.count() == 0
    row = RascunhoEntregaWhatsApp.query.one()
    catalogo_confirmacao = _catalogo(user, contexto, referencia=referencia, telefone=TERCEIRO, rascunho=row)
    confirmado = executar_acao_whatsapp(
        AcaoWhatsAppContextual(
            NOME_CONFIRMAR_TERCEIRO,
            {"rascunho_ref": "[RASCUNHO_1]", "versao": int(row.versao), "confirmacao_ref": "[CONFIRMACAO_1]"},
        ),
        catalogo=catalogo_confirmacao,
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        identidade_requisicao="req-elegivel-2",
    )
    assert confirmado.terceiro.enviar is True
    enviado = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=confirmado.terceiro.texto,
        chave_idempotencia=confirmado.terceiro.chave_execucao,
        destino_autorizado=entrega.DestinoWhatsAppVinculado(
            identidade_id=0,
            destinatario=confirmado.terceiro.destinatario,
            phone_number_id=confirmado.terceiro.phone_number_id,
        ),
    )
    repetido = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=confirmado.terceiro.texto,
        chave_idempotencia=confirmado.terceiro.chave_execucao,
        destino_autorizado=entrega.DestinoWhatsAppVinculado(
            identidade_id=0,
            destinatario=confirmado.terceiro.destinatario,
            phone_number_id=confirmado.terceiro.phone_number_id,
        ),
    )
    assert enviado.codigo == entrega.CODIGO_ENVIADO
    assert repetido.codigo == entrega.CODIGO_ENVIADO
    assert len(chamadas) == 1
    assert chamadas[0][1]["json"]["to"] == "5519999999999"
    assert chamadas[0][1]["json"]["text"]["body"] == TEXTO_SERVIDOR
    assert SolicitacaoEntregaCanal.query.count() == 1
    assert ConsumoInteracaoCanal.query.count() == 0


def test_resultado_incerto_nao_repete_http(ctx, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, erro=adapter.requests.Timeout("timeout"))
    user = _usuario("incerto.orq@example.com")
    _vincular(user, sujeito="5519999999999")
    _entrada("5519999999999", horas=1)
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status="ok",
        telefone_e164=TERCEIRO,
        telefone_exibicao=TERCEIRO,
        turno_telefone_ref="[TEL_1]",
        chave_preparo="8" * 64,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    efeito = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    primeiro = entrega.entregar_texto_no_whatsapp_do_usuario(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        texto=efeito.texto,
        chave_idempotencia=efeito.chave_execucao,
        destino_autorizado=entrega.DestinoWhatsAppVinculado(
            identidade_id=0,
            destinatario=efeito.destinatario,
            phone_number_id=efeito.phone_number_id,
        ),
    )
    from app.services.canal_rascunho_entrega_whatsapp_service import finalizar_transporte

    fechado = finalizar_transporte(
        int(efeito.rascunho_id),
        codigo_transporte=primeiro.codigo,
        solicitacao_id=primeiro.solicitacao_id,
        incerto=True,
    )
    segundo = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    assert fechado.codigo == "resultado_incerto"
    assert "Enviei" not in fechado.mensagem
    assert segundo.enviar is False
    assert len(chamadas) == 1
    assert EventoCanalSaida.query.filter_by(codigo_erro="timeout").count() == 1


def test_esclarecer_nao_cria_rascunho(ctx):
    user = _usuario("esclarece.orq@example.com")
    contexto, referencia = _registrar(user)
    _registrar(user, texto="Outra análise.")
    catalogo = _catalogo(user, contexto, referencia=referencia)
    efeito = executar_acao_whatsapp(
        AcaoWhatsAppContextual(NOME_ESCLARECER, {"motivo": "conteudo_ambiguo"}),
        catalogo=catalogo,
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        identidade_requisicao="req-esclarece",
    )
    assert efeito.codigo == "esclarecimento"
    assert RascunhoEntregaWhatsApp.query.count() == 0
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_continuar_conversa_nao_e_falha_e_falha_nao_e_continuar():
    catalogo = CatalogoDecisaoWhatsApp(
        mensagem="como funciona",
        historico=[],
        superficie="auditoria_frete",
        whatsapp_proprio_valido=False,
        conteudos={},
        telefones={},
        capabilities=(NOME_CONTINUAR, NOME_ESCLARECER),
    )
    seguir = orquestracao.interpretar_resposta_orquestracao(
        _resposta_tool(NOME_CONTINUAR, {}),
        catalogo=catalogo,
    )
    truncada = orquestracao.interpretar_resposta_orquestracao(
        _resposta_tool(NOME_CONTINUAR, {}, finish_reason="MAX_TOKENS"),
        catalogo=catalogo,
    )
    assert seguir.tipo == NOME_CONTINUAR
    assert truncada.tipo == TIPO_FALHA_TECNICA
    assert truncada.tipo != NOME_CONTINUAR


def test_ferramentas_nao_aceitam_telefone_livre_e_tem_orcamento(monkeypatch):
    capturado = {}

    def _capturar(*_args, **kwargs):
        capturado["config"] = kwargs["config"]
        capturado["contents"] = kwargs["contents"]
        return _resposta_tool(NOME_CONTINUAR, {})

    monkeypatch.setattr(orquestracao, "cleiton_governed_generate_content", _capturar)
    catalogo = CatalogoDecisaoWhatsApp(
        mensagem="manda isso pra mim",
        historico=[{"role": "model", "content": "análise", "conteudo_alias": "[CONTEUDO_1]"}],
        superficie="julia",
        whatsapp_proprio_valido=True,
        conteudos={"[CONTEUDO_1]": "rc" + "d" * 32},
        telefones={},
        capabilities=(
            NOME_ENVIAR_PARA_MEU_WHATSAPP,
            NOME_PREPARAR_TERCEIRO,
            NOME_ATUALIZAR_TERCEIRO,
            NOME_CONFIRMAR_TERCEIRO,
            NOME_CANCELAR_TERCEIRO,
            NOME_ESCLARECER,
            NOME_CONTINUAR,
        ),
    )
    acao = orquestracao.decidir_acao_whatsapp_contextual(
        catalogo=catalogo,
        agent="julia",
        flow_type="julia_chat",
        client=object(),
        model="gemini-2.5-flash",
        api_key_label="teste",
    )
    nomes = [item.name for item in capturado["config"].tools[0].function_declarations]
    assert NOME_ENVIAR_PARA_MEU_WHATSAPP in nomes
    assert NOME_CONTINUAR in nomes
    enviar = next(item for item in capturado["config"].tools[0].function_declarations if item.name == NOME_ENVIAR_PARA_MEU_WHATSAPP)
    assert "telefone" not in (enviar.parameters.properties or {})
    assert capturado["config"].temperature == 0
    assert capturado["config"].max_output_tokens > 32
    assert capturado["config"].thinking_config.thinking_budget == 0
    assert "análise" in capturado["contents"]
    assert acao.tipo == NOME_CONTINUAR


def test_decisao_contextual_nao_debita_franquia(app, ctx, monkeypatch):
    from decimal import Decimal

    from flask import g

    from app.consumo_identidade import identidade_de_usuario, set_consumo_identidade
    from app.models import Franquia, IaConsumoEvento
    from tests.conftest import seed_cleiton_cost_config, seed_sistema_interno

    seed_sistema_interno()
    seed_cleiton_cost_config()
    user = _usuario("billing.orq@example.com")

    class _Models:
        def generate_content(self, **_kwargs):
            return SimpleNamespace(
                function_calls=[SimpleNamespace(name=NOME_CONTINUAR, args={})],
                candidates=[SimpleNamespace(finish_reason="STOP")],
                usage_metadata=SimpleNamespace(
                    prompt_token_count=10,
                    candidates_token_count=4,
                    total_token_count=14,
                ),
            )

    cliente = SimpleNamespace(models=_Models())
    catalogo = CatalogoDecisaoWhatsApp(
        mensagem="como funciona o whatsapp",
        historico=[],
        superficie="julia",
        whatsapp_proprio_valido=False,
        conteudos={},
        telefones={},
        capabilities=(NOME_CONTINUAR, NOME_ESCLARECER),
    )
    with app.test_request_context("/api/chat_julia"):
        set_consumo_identidade(identidade_de_usuario(user, "http_usuario"))
        anterior = dict(g.identidade)
        acao = orquestracao.decidir_acao_whatsapp_contextual(
            catalogo=catalogo,
            agent="julia",
            flow_type="julia_chat",
            client=cliente,
            model="gemini-2.5-flash",
            api_key_label="teste",
        )
        depois = dict(g.identidade)
    assert acao.tipo == NOME_CONTINUAR
    assert depois == anterior
    franquia = db.session.get(Franquia, user.franquia_id)
    assert IaConsumoEvento.query.filter_by(usuario_id=user.id).count() == 0
    assert IaConsumoEvento.query.filter_by(origem_sistema=True).count() == 1
    assert franquia.consumo_acumulado == Decimal("0")
    assert ConsumoInteracaoCanal.query.count() == 0


def test_duas_confirmacoes_simultaneas_geram_uma_entrega(tmp_path, monkeypatch):
    from flask import Flask

    banco = tmp_path / "orquestracao_concorrencia.sqlite"
    flask_app = Flask("orquestracao-concorrencia")
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
        user = _usuario("concorrencia.orq@example.com")
        _vincular(user, sujeito="5519999999999")
        _entrada("5519999999999", horas=1)
        contexto, referencia = _registrar(user)
        resposta = resolver_resposta_compartilhavel(
            user_id=int(user.id),
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            contexto_conversa=contexto,
            referencia=referencia,
        )
        preparar_rascunho(
            user_id=int(user.id),
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            contexto_conversa=contexto,
            conteudo_referencia=referencia,
            content_hash=resposta.content_hash,
            nome_destinatario="João",
            telefone_status="ok",
            telefone_e164=TERCEIRO,
            telefone_exibicao=TERCEIRO,
            turno_telefone_ref="[TEL_1]",
            chave_preparo="9" * 64,
        )
        row = RascunhoEntregaWhatsApp.query.one()
        referencia_rascunho = row.referencia
        versao = int(row.versao)
        token = row.confirmacao_referencia
        user_id = int(user.id)
        barreira = threading.Barrier(2)
        resultados = {}

        def _worker(indice):
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    efeito = confirmar_rascunho(
                        user_id=user_id,
                        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
                        contexto_conversa=contexto,
                        referencia=referencia_rascunho,
                        versao=versao,
                        confirmacao_referencia=token,
                    )
                    if efeito.enviar:
                        entrega.entregar_texto_no_whatsapp_do_usuario(
                            usuario=db.session.get(User, user_id),
                            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
                            texto=efeito.texto,
                            chave_idempotencia=efeito.chave_execucao,
                            destino_autorizado=entrega.DestinoWhatsAppVinculado(
                                identidade_id=0,
                                destinatario=efeito.destinatario,
                                phone_number_id=efeito.phone_number_id,
                            ),
                        )
                    resultados[indice] = efeito.codigo
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = exc

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        codigos = set(resultados.values())
        assert codigos <= {"enviar", "em_execucao", "enviado"}
        assert "enviar" in resultados.values() or "enviado" in resultados.values()
        assert all(not isinstance(item, Exception) for item in resultados.values())
        assert SolicitacaoEntregaCanal.query.count() == 1
        assert len(chamadas) == 1
        db.session.remove()
        db.drop_all()


def test_migration_cria_as_duas_tabelas_sem_reusar_dominio_antigo():
    import importlib.util
    from pathlib import Path

    caminho = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "g5h6i7j8k9l0_orquestracao_whatsapp_contextual.py"
    spec = importlib.util.spec_from_file_location("mig_orquestracao", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    texto = caminho.read_text(encoding="utf-8")
    assert modulo.revision == "g5h6i7j8k9l0"
    assert modulo.down_revision == "f4g5h6i7j8k9"
    assert 'op.create_table(\n        "resposta_compartilhavel_canal"' in texto
    assert 'op.create_table(\n        "rascunho_entrega_whatsapp"' in texto
    assert "onboarding_canal" not in texto
    assert "interpretacao_conversacional_canal" not in texto


def test_cancelamento_vencedor_impede_envio(ctx):
    user = _usuario("cancela.vence.orq@example.com")
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status="ok",
        telefone_e164=TERCEIRO,
        telefone_exibicao=TERCEIRO,
        turno_telefone_ref="[TEL_1]",
        chave_preparo="a" * 64,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    cancelado = cancelar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=int(row.versao),
    )
    confirmado = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    assert cancelado.codigo == "cancelado"
    assert confirmado.enviar is False
    assert confirmado.codigo == "recusado"
    assert db.session.get(RascunhoEntregaWhatsApp, row.id).estado == RascunhoEntregaWhatsApp.ESTADO_CANCELADO
    assert SolicitacaoEntregaCanal.query.count() == 0


def test_confirmacao_vencedora_rejeita_cancelamento_e_alteracao(ctx):
    user = _usuario("confirma.vence.orq@example.com")
    _vincular(user, sujeito="5519999999999")
    _entrada("5519999999999", horas=1)
    contexto, referencia = _registrar(user)
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status="ok",
        telefone_e164=TERCEIRO,
        telefone_exibicao=TERCEIRO,
        turno_telefone_ref="[TEL_1]",
        chave_preparo="b" * 64,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    versao = int(row.versao)
    confirmado = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=versao,
        confirmacao_referencia=row.confirmacao_referencia,
    )
    cancelado = cancelar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=versao,
    )
    alterado = atualizar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto,
        referencia=row.referencia,
        versao=versao,
        conteudo_referencia=None,
        content_hash=None,
        nome_destinatario="Maria",
        nome_informado=True,
        telefone_status=None,
        telefone_e164=None,
        telefone_exibicao=None,
        turno_telefone_ref=None,
    )
    atual = db.session.get(RascunhoEntregaWhatsApp, row.id)
    assert confirmado.enviar is True
    assert cancelado.codigo == "recusado"
    assert alterado.codigo == "recusado"
    assert atual.estado == RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO
    assert int(atual.versao) == versao
    assert atual.nome_destinatario == "João"


def _corrida_rascunho(tmp_path, monkeypatch, nome, segundo):
    from flask import Flask

    banco = tmp_path / f"{nome}.sqlite"
    flask_app = Flask(nome)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + banco.as_posix()
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 15},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    _configurar(monkeypatch)
    with flask_app.app_context():
        import app.models  # noqa: F401

        db.create_all()
        user = _usuario(f"{nome}@example.com")
        _vincular(user, sujeito="5519999999999")
        _entrada("5519999999999", horas=1)
        contexto, referencia = _registrar(user)
        resposta = resolver_resposta_compartilhavel(
            user_id=int(user.id),
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            contexto_conversa=contexto,
            referencia=referencia,
        )
        preparar_rascunho(
            user_id=int(user.id),
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            contexto_conversa=contexto,
            conteudo_referencia=referencia,
            content_hash=resposta.content_hash,
            nome_destinatario="João",
            telefone_status="ok",
            telefone_e164=TERCEIRO,
            telefone_exibicao=TERCEIRO,
            turno_telefone_ref="[TEL_1]",
            chave_preparo="c" * 64,
        )
        row = RascunhoEntregaWhatsApp.query.one()
        pacote = {
            "user_id": int(user.id),
            "contexto": contexto,
            "referencia": row.referencia,
            "versao": int(row.versao),
            "token": row.confirmacao_referencia,
        }
        barreira = threading.Barrier(2)
        resultados = {}

        def _confirmar():
            with flask_app.app_context():
                barreira.wait(timeout=5)
                efeito = confirmar_rascunho(
                    user_id=pacote["user_id"],
                    superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
                    contexto_conversa=pacote["contexto"],
                    referencia=pacote["referencia"],
                    versao=pacote["versao"],
                    confirmacao_referencia=pacote["token"],
                )
                resultados["confirmar"] = efeito
                db.session.remove()

        def _outro():
            with flask_app.app_context():
                barreira.wait(timeout=5)
                resultados["outro"] = segundo(flask_app, pacote)
                db.session.remove()

        def _confirmar_seguro():
            try:
                _confirmar()
            except Exception as exc:
                resultados["confirmar"] = exc

        threads = [threading.Thread(target=_confirmar_seguro), threading.Thread(target=_outro)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert set(resultados) == {"confirmar", "outro"}
        assert all(not isinstance(item, Exception) for item in resultados.values())
        db.session.remove()
        atual = RascunhoEntregaWhatsApp.query.filter_by(referencia=pacote["referencia"]).one()
        foto = SimpleNamespace(
            estado=atual.estado,
            versao=int(atual.versao),
            nome=atual.nome_destinatario,
            token=atual.confirmacao_referencia,
            solicitacoes=SolicitacaoEntregaCanal.query.count(),
        )
        db.session.remove()
        db.drop_all()
        return resultados, foto, pacote


def test_confirmar_e_cancelar_concorrentes(tmp_path, monkeypatch):
    def _cancelar(_app, pacote):
        try:
            return cancelar_rascunho(
                user_id=pacote["user_id"],
                superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
                contexto_conversa=pacote["contexto"],
                referencia=pacote["referencia"],
                versao=pacote["versao"],
            )
        except Exception as exc:
            return exc

    resultados, atual, _pacote = _corrida_rascunho(tmp_path, monkeypatch, "corrida-cancelar", _cancelar)
    confirmado = resultados["confirmar"]
    cancelado = resultados["outro"]
    if atual.estado == RascunhoEntregaWhatsApp.ESTADO_CANCELADO:
        assert confirmado.enviar is False
        assert cancelado.codigo == "cancelado"
        assert atual.solicitacoes == 0
    else:
        assert confirmado.enviar is True
        assert cancelado.codigo == "recusado"
        assert atual.estado == RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO


def test_confirmar_e_atualizar_concorrentes(tmp_path, monkeypatch):
    def _atualizar(_app, pacote):
        try:
            return atualizar_rascunho(
                user_id=pacote["user_id"],
                superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
                contexto_conversa=pacote["contexto"],
                referencia=pacote["referencia"],
                versao=pacote["versao"],
                conteudo_referencia=None,
                content_hash=None,
                nome_destinatario="Maria",
                nome_informado=True,
                telefone_status=None,
                telefone_e164=None,
                telefone_exibicao=None,
                turno_telefone_ref=None,
            )
        except Exception as exc:
            return exc

    resultados, atual, pacote = _corrida_rascunho(tmp_path, monkeypatch, "corrida-atualizar", _atualizar)
    confirmado = resultados["confirmar"]
    alterado = resultados["outro"]
    if confirmado.enviar:
        assert alterado.codigo == "recusado"
        assert atual.estado == RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO
        assert int(atual.versao) == pacote["versao"]
        assert atual.nome == "João"
    else:
        assert confirmado.codigo == "recusado"
        assert alterado.codigo == "confirmacao"
        assert int(atual.versao) == pacote["versao"] + 1
        assert atual.nome == "Maria"
        assert atual.token != pacote["token"]


def test_janela_so_abre_no_mesmo_numero_empresarial(ctx):
    user = _usuario("janela.numero.orq@example.com")
    numero_a = "111111111111111"
    numero_b = "222222222222222"
    sujeito = "5519999999999"
    _vincular(user, sujeito=sujeito, contexto=numero_a)
    _entrada_no_numero(sujeito, numero_b, horas=1)
    outro_numero = elegibilidade_terceiro(TERCEIRO)
    assert outro_numero.elegivel is False
    assert outro_numero.codigo == "sem_janela"
    _entrada_no_numero(sujeito, numero_a, horas=30, evento_id="in-antigo-a")
    antigo = elegibilidade_terceiro(TERCEIRO)
    assert antigo.elegivel is False
    assert antigo.codigo == "sem_janela"
    _entrada_no_numero(
        sujeito,
        numero_a,
        horas=1,
        evento_id="in-status-a",
        tipo=EventoCanalRecebido.TIPO_STATUS_ENTREGA,
        diagnostico="status_entrega:delivered",
    )
    status = elegibilidade_terceiro(TERCEIRO)
    assert status.elegivel is False
    assert status.codigo == "sem_janela"
    _entrada_no_numero(sujeito, numero_a, horas=1, evento_id="in-texto-a")
    mesma_janela = elegibilidade_terceiro(TERCEIRO)
    assert mesma_janela.elegivel is True
    assert mesma_janela.codigo == "janela_aberta"
    assert mesma_janela.phone_number_id == numero_a


def test_identidade_ambigua_nao_abre_janela(ctx):
    from sqlalchemy import text

    user = _usuario("ambigua.orq@example.com")
    sujeito = "14155552671"
    _vincular(user, sujeito=sujeito, contexto="111111111111111")
    db.session.execute(text("DROP INDEX IF EXISTS uq_identidade_canal_provider_subject_ativa"))
    db.session.commit()
    _vincular(user, sujeito=sujeito, contexto="222222222222222")
    _entrada_no_numero(sujeito, "111111111111111", horas=1, evento_id="in-ambigua")
    resultado = elegibilidade_terceiro("+14155552671")
    assert resultado.elegivel is False
    assert resultado.codigo == "evidencia_insuficiente"
    assert resultado.phone_number_id is None


def _entrada_no_numero(sujeito, numero, horas=1, evento_id=None, tipo=None, diagnostico="mensagem_textual"):
    evento = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=evento_id or f"in-{sujeito}-{numero}-{horas}",
        tipo_evento=tipo or EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
        sujeito_externo=sujeito,
        contexto_destino=numero,
        recebido_em=utcnow_naive() - timedelta(hours=horas),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id="corrjanelaorq01",
        diagnostico_seguro=diagnostico,
    )
    db.session.add(evento)
    db.session.commit()
    return evento


class _SessaoConversa(dict):
    modified = False


def test_julia_isola_catalogo_e_rascunho_por_conversa(ctx):
    from app.services.canal_execucao_whatsapp_contextual_service import construir_catalogo

    user = _usuario("conversa.julia.orq@example.com")
    _vincular(user, sujeito="5519999999999")
    _entrada("5519999999999", horas=1)
    sessao = _SessaoConversa()
    contexto_a = montar_contexto(
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        user_id=int(user.id),
        historico=[],
        sessao=sessao,
    )
    referencia = registrar_resposta_compartilhavel(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto_a,
        texto=TEXTO_SERVIDOR,
    )
    resposta = resolver_resposta_compartilhavel(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto_a,
        referencia=referencia,
    )
    preparar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto_a,
        conteudo_referencia=referencia,
        content_hash=resposta.content_hash,
        nome_destinatario="João",
        telefone_status="ok",
        telefone_e164=TERCEIRO,
        telefone_exibicao=TERCEIRO,
        turno_telefone_ref="[TEL_1]",
        chave_preparo="d" * 64,
    )
    row = RascunhoEntregaWhatsApp.query.one()
    continuacao = montar_contexto(
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        user_id=int(user.id),
        historico=[{"role": "model", "content": TEXTO_SERVIDOR}],
        sessao=sessao,
    )
    assert continuacao == contexto_a
    assert contexto_a != f"julia:{int(user.id)}"
    catalogo_a = construir_catalogo(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto_a,
        mensagem="envia para o João",
        historico=[{"role": "model", "content": TEXTO_SERVIDOR}],
        whatsapp_proprio_valido=False,
    )
    assert referencia in catalogo_a.conteudos.values()
    assert catalogo_a.rascunho_referencia == row.referencia
    contexto_b = montar_contexto(
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        user_id=int(user.id),
        historico=[],
        sessao=sessao,
    )
    assert contexto_b != contexto_a
    catalogo_b = construir_catalogo(
        usuario=user,
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto_b,
        mensagem="nova conversa",
        historico=[],
        whatsapp_proprio_valido=False,
    )
    assert referencia not in catalogo_b.conteudos.values()
    assert catalogo_b.rascunho_referencia is None
    assert rascunho_aberto(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto_b,
    ) is None
    recusa = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto_b,
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    assert recusa.codigo == "recusado"
    assert recusa.enviar is False
    confirmado = confirmar_rascunho(
        user_id=int(user.id),
        superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
        contexto_conversa=contexto_a,
        referencia=row.referencia,
        versao=int(row.versao),
        confirmacao_referencia=row.confirmacao_referencia,
    )
    assert confirmado.enviar is True
    assert db.session.get(RascunhoEntregaWhatsApp, row.id).estado == (
        RascunhoEntregaWhatsApp.ESTADO_EM_EXECUCAO
    )


def test_julia_amarra_conversa_na_sessao_do_servidor(app, ctx):
    from flask import g, session

    user = _usuario("sessao.julia.orq@example.com")
    app.secret_key = "teste-conversa-julia"
    with app.test_request_context("/api/chat_julia"):
        session["julia_entrega_conversa_id"] = "valor-vindo-do-cliente"
        session["julia_entrega_conversa_user_id"] = int(user.id)
        com_historico = montar_contexto(
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            user_id=int(user.id),
            historico=[{"role": "model", "content": TEXTO_SERVIDOR}],
        )
        assert "valor-vindo-do-cliente" not in com_historico
        assert com_historico != f"julia:{int(user.id)}"
    g._julia_entrega_conversa_id = None
    g._julia_entrega_conversa_user = None
    with app.test_request_context("/api/chat_julia"):
        primeiro = montar_contexto(
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            user_id=int(user.id),
            historico=[],
        )
        seguinte = montar_contexto(
            superficie=SolicitacaoEntregaCanal.SUPERFICIE_JULIA,
            user_id=int(user.id),
            historico=[{"role": "model", "content": TEXTO_SERVIDOR}],
        )
        assert primeiro == seguinte
        assert primeiro != f"julia:{int(user.id)}"
        assert session["julia_entrega_conversa_id"] in primeiro
        assert session["julia_entrega_conversa_user_id"] == int(user.id)


def test_modelo_rejeita_rascunho_sem_referencia_opaca(ctx):
    user = _usuario("check.orq@example.com")
    with pytest.raises(IntegrityError):
        db.session.add(
            RascunhoEntregaWhatsApp(
                referencia="curta",
                user_id=int(user.id),
                superficie="julia",
                contexto_conversa="julia:1",
                conteudo_referencia="rc" + "e" * 32,
                content_hash="a" * 64,
                estado="aguardando_telefone",
                versao=1,
                criada_em=utcnow_naive(),
                atualizada_em=utcnow_naive(),
                expira_em=utcnow_naive(),
            )
        )
        db.session.commit()
    db.session.rollback()
