"""Lote WhatsApp 3B: normalização, identidade externa e roteamento por estado."""
from __future__ import annotations

import importlib.util
import logging
import threading
from pathlib import Path

import pytest
from flask import Flask
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

from app.db_operational_safety import run_test_schema_operation
from app.extensions import db
from app.models import (
    ConteudoTextualCanal,
    EventoCanalRecebido,
    IdentidadeCanalExterna,
    OnboardingCanal,
    utcnow_naive,
)
from app.services import canal_entrada_processamento_service as entrada
from app.services.canal_aquisicao_service import (
    CanalAquisicaoError,
    MaterialSensivelRecusadoError,
    obter_ou_criar_identidade_externa,
)
from tests.conftest import (
    PYTEST_DISPOSABLE_SQLALCHEMY_URI,
    seed_conta_franquia_cliente,
    seed_usuario,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "p7q8r9s0t1u2_evento_canal_roteamento.py"
TEXTO = "TEXTO_OPERACIONAL_NAO_LOGAR"
SUJEITO = "subjNaoLogar1"
DESTINO = "destOpaco01"


def _migration_module():
    spec = importlib.util.spec_from_file_location("mig_evento_canal_roteamento", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _evento(
    tipo: str = EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
    *,
    sujeito: str | None = SUJEITO,
    contexto: str | None = DESTINO,
    externo: str = "wamid.LOTE3B",
    diagnostico: str | None = None,
    correlation_id: str = "corr3b0001",
) -> EventoCanalRecebido:
    if diagnostico is None:
        diagnostico = {
            EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL: "mensagem_textual",
            EventoCanalRecebido.TIPO_MIDIA: "midia:image",
            EventoCanalRecebido.TIPO_STATUS_ENTREGA: "status_entrega:delivered",
            EventoCanalRecebido.TIPO_DESCONHECIDO: "desconhecido",
        }[tipo]
    row = EventoCanalRecebido(
        provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
        evento_externo_id=externo,
        tipo_evento=tipo,
        sujeito_externo=sujeito,
        contexto_destino=contexto,
        recebido_em=utcnow_naive(),
        status_processamento=EventoCanalRecebido.STATUS_RECEBIDO,
        correlation_id=correlation_id,
        diagnostico_seguro=diagnostico,
    )
    db.session.add(row)
    db.session.flush()
    return row


def _textual(externo: str = "wamid.LOTE3B", **kwargs) -> EventoCanalRecebido:
    evento = _evento(externo=externo, **kwargs)
    entrada.registrar_conteudo_textual_canal(evento.id, TEXTO)
    return evento


def _status(evento_id: int) -> str:
    row = db.session.get(EventoCanalRecebido, evento_id)
    return row.status_processamento


def _marcar(ident: IdentidadeCanalExterna, estado: str, user=None) -> IdentidadeCanalExterna:
    user_id = None if user is None else user.id
    agora = utcnow_naive()
    ident.estado = estado
    ident.atualizada_em = agora
    ident.user_id = user_id if estado == IdentidadeCanalExterna.ESTADO_VINCULADA else None
    ident.vinculada_em = agora if estado == IdentidadeCanalExterna.ESTADO_VINCULADA else None
    ident.revogada_em = agora if estado == IdentidadeCanalExterna.ESTADO_REVOGADA else None
    db.session.commit()
    return db.session.get(IdentidadeCanalExterna, ident.id)


def _fonte_processador() -> str:
    return Path(entrada.__file__).read_text(encoding="utf-8")


def test_migration_amplia_status_e_cria_texto_minimo():
    modulo = _migration_module()
    assert modulo.revision == "p7q8r9s0t1u2"
    assert modulo.down_revision == "o6p7q8r9s0t1"
    assert modulo._SQL_STATUS == EventoCanalRecebido._SQL_STATUS
    assert modulo._SQL_TEXTO == ConteudoTextualCanal._SQL_TEXTO
    assert modulo._SQL_STATUS_LOTE_3A == "status_processamento IN ('recebido')"
    texto = MIGRATION.read_text(encoding="utf-8")
    assert 'sa.Column("payload"' not in texto
    assert "payload_json" not in texto
    assert "assinatura" not in texto
    assert "op.create_table" in texto
    assert "ck_evento_canal_recebido_status" in texto


def test_mensagem_textual_cria_identidade_guest(ctx, caplog):
    evento = _textual()
    with caplog.at_level(logging.INFO):
        resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_ROTEADO
    assert resultado.rota == entrada.ROTA_GUEST
    assert resultado.estado_identidade == IdentidadeCanalExterna.ESTADO_GUEST
    assert resultado.correlation_id == "corr3b0001"
    ident = IdentidadeCanalExterna.query.one()
    assert resultado.identidade_id == ident.id
    assert ident.provedor == entrada.PROVEDOR_IDENTIDADE_WHATSAPP
    assert ident.sujeito_externo == SUJEITO
    assert ident.contexto_destino == DESTINO
    assert ident.user_id is None
    assert ident.interacoes_uteis == 0
    assert _status(evento.id) == EventoCanalRecebido.STATUS_ROTEADO
    assert OnboardingCanal.query.count() == 0
    mensagens = "\n".join(registro.getMessage() for registro in caplog.records)
    assert TEXTO not in mensagens
    assert SUJEITO not in mensagens
    assert "rota=guest" in mensagens
    assert f"evento_id={evento.id}" in mensagens


def test_reprocessar_nao_duplica_identidade_nem_acao(ctx, monkeypatch):
    evento = _textual()
    chamadas = {"n": 0}
    original = entrada.obter_ou_criar_identidade_externa

    def _spy(**kwargs):
        chamadas["n"] += 1
        return original(**kwargs)

    monkeypatch.setattr(entrada, "obter_ou_criar_identidade_externa", _spy)
    primeiro = entrada.processar_evento_canal_recebido(evento.id)
    segundo = entrada.processar_evento_canal_recebido(evento.id)
    assert primeiro.rota == entrada.ROTA_GUEST
    assert segundo.codigo == entrada.CODIGO_JA_PROCESSADO
    assert segundo.identidade_id is None
    assert segundo.rota is None
    assert chamadas["n"] == 1
    assert IdentidadeCanalExterna.query.count() == 1
    assert _status(evento.id) == EventoCanalRecebido.STATUS_ROTEADO


def test_duas_mensagens_do_mesmo_remetente_resolvem_a_mesma_identidade(ctx):
    primeira = _textual(externo="wamid.MESMA.1", contexto="destPrimeiro")
    segunda = _textual(externo="wamid.MESMA.2", contexto="destSegundo")
    um = entrada.processar_evento_canal_recebido(primeira.id)
    dois = entrada.processar_evento_canal_recebido(segunda.id)
    assert um.identidade_id == dois.identidade_id
    assert um.rota == dois.rota == entrada.ROTA_GUEST
    ident = IdentidadeCanalExterna.query.one()
    assert ident.contexto_destino == "destPrimeiro"
    assert ident.sujeito_externo == SUJEITO


def test_cadastro_em_andamento_roteia_para_onboarding(ctx):
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=SUJEITO,
        contexto_destino=DESTINO,
    )
    _marcar(ident, IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO)
    evento = _textual()
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.rota == entrada.ROTA_ONBOARDING
    assert resultado.estado_identidade == IdentidadeCanalExterna.ESTADO_CADASTRO_EM_ANDAMENTO
    assert resultado.identidade_id == ident.id
    assert IdentidadeCanalExterna.query.count() == 1
    assert OnboardingCanal.query.count() == 0


def test_vinculada_roteia_para_usuario_vinculado(ctx):
    conta, franquia = seed_conta_franquia_cliente("conta-canal-3b")
    user = seed_usuario(franquia.id, conta.id, email="vinculo.canal3b@example.com")
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=SUJEITO,
    )
    _marcar(ident, IdentidadeCanalExterna.ESTADO_VINCULADA, user=user)
    evento = _textual()
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.rota == entrada.ROTA_USUARIO_VINCULADO
    assert resultado.estado_identidade == IdentidadeCanalExterna.ESTADO_VINCULADA
    assert resultado.identidade_id == ident.id
    gravada = db.session.get(IdentidadeCanalExterna, ident.id)
    assert gravada.user_id == user.id
    assert gravada.estado == IdentidadeCanalExterna.ESTADO_VINCULADA
    assert OnboardingCanal.query.count() == 0


def test_vinculo_incoerente_falha_fechado(ctx, monkeypatch):
    conta, franquia = seed_conta_franquia_cliente("conta-incoerente-3b")
    user = seed_usuario(franquia.id, conta.id, email="incoerente.canal3b@example.com")
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=SUJEITO,
    )
    _marcar(ident, IdentidadeCanalExterna.ESTADO_VINCULADA, user=user)
    monkeypatch.setattr(entrada, "_vinculo_coerente", lambda _identidade: False)
    evento = _textual()
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_IDENTIDADE_INCOERENTE
    assert resultado.rota is None
    gravada = db.session.get(IdentidadeCanalExterna, ident.id)
    assert gravada.estado == IdentidadeCanalExterna.ESTADO_VINCULADA
    assert gravada.user_id == user.id
    assert _status(evento.id) == EventoCanalRecebido.STATUS_ERRO_SEGURO
    assert OnboardingCanal.query.count() == 0


def test_bloqueada_roteia_para_bloqueada_sem_mudar_estado(ctx):
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=SUJEITO,
    )
    ident = _marcar(ident, IdentidadeCanalExterna.ESTADO_BLOQUEADA)
    atualizada = ident.atualizada_em
    evento = _textual()
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.rota == entrada.ROTA_BLOQUEADA
    assert resultado.estado_identidade == IdentidadeCanalExterna.ESTADO_BLOQUEADA
    assert resultado.identidade_id == ident.id
    gravada = db.session.get(IdentidadeCanalExterna, ident.id)
    assert gravada.estado == IdentidadeCanalExterna.ESTADO_BLOQUEADA
    assert gravada.user_id is None
    assert gravada.atualizada_em == atualizada
    assert OnboardingCanal.query.count() == 0
    assert _status(evento.id) == EventoCanalRecebido.STATUS_ROTEADO


def test_revogada_nao_e_reativada_e_o_servico_abre_guest_novo(ctx):
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=SUJEITO,
        contexto_destino="destRevogada",
    )
    revogada_id = ident.id
    ident = _marcar(ident, IdentidadeCanalExterna.ESTADO_REVOGADA)
    revogada_em = ident.revogada_em
    evento = _textual(contexto="destNova")
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    antiga = db.session.get(IdentidadeCanalExterna, revogada_id)
    assert antiga.estado == IdentidadeCanalExterna.ESTADO_REVOGADA
    assert antiga.revogada_em == revogada_em
    assert resultado.rota == entrada.ROTA_GUEST
    assert resultado.identidade_id != revogada_id
    assert resultado.estado_identidade == IdentidadeCanalExterna.ESTADO_GUEST
    assert IdentidadeCanalExterna.query.count() == 2
    nova = db.session.get(IdentidadeCanalExterna, resultado.identidade_id)
    assert nova.estado == IdentidadeCanalExterna.ESTADO_GUEST
    assert nova.contexto_destino == "destNova"


def test_identidade_revogada_devolvida_nao_opera_como_ativa(ctx, monkeypatch):
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=SUJEITO,
    )
    ident = _marcar(ident, IdentidadeCanalExterna.ESTADO_REVOGADA)
    monkeypatch.setattr(entrada, "obter_ou_criar_identidade_externa", lambda **_kwargs: ident)
    evento = _textual()
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.rota == entrada.ROTA_REVOGADA
    assert resultado.codigo == entrada.CODIGO_IDENTIDADE_REVOGADA
    assert resultado.estado_identidade == IdentidadeCanalExterna.ESTADO_REVOGADA
    gravada = db.session.get(IdentidadeCanalExterna, ident.id)
    assert gravada.estado == IdentidadeCanalExterna.ESTADO_REVOGADA
    assert IdentidadeCanalExterna.query.count() == 1
    assert OnboardingCanal.query.count() == 0


def test_midia_nao_cria_identidade_nem_le_conteudo(ctx, monkeypatch):
    evento = _evento(EventoCanalRecebido.TIPO_MIDIA, externo="wamid.MIDIA")
    mensagem = entrada.montar_mensagem_canal_entrada(evento)
    assert mensagem.texto is None
    assert mensagem.tipo == EventoCanalRecebido.TIPO_MIDIA
    monkeypatch.setattr(
        entrada,
        "obter_ou_criar_identidade_externa",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("identidade")),
    )
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_AGUARDANDO_MIDIA
    assert resultado.identidade_id is None
    assert resultado.rota is None
    assert IdentidadeCanalExterna.query.count() == 0
    assert ConteudoTextualCanal.query.count() == 0
    assert _status(evento.id) == EventoCanalRecebido.STATUS_AGUARDANDO_SUPORTE_MIDIA


def test_status_de_entrega_nao_cria_identidade(ctx, monkeypatch):
    evento = _evento(EventoCanalRecebido.TIPO_STATUS_ENTREGA, externo="wamid.STATUS")
    monkeypatch.setattr(
        entrada,
        "obter_ou_criar_identidade_externa",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("identidade")),
    )
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_AGUARDANDO_SAIDA
    assert resultado.identidade_id is None
    assert IdentidadeCanalExterna.query.count() == 0
    assert _status(evento.id) == EventoCanalRecebido.STATUS_ROTEADO


def test_desconhecido_e_ignorado_sem_erro(ctx):
    evento = _evento(EventoCanalRecebido.TIPO_DESCONHECIDO, externo="wamid.DESC")
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_IGNORADO
    assert resultado.rota is None
    assert IdentidadeCanalExterna.query.count() == 0
    assert _status(evento.id) == EventoCanalRecebido.STATUS_IGNORADO
    assert db.session.is_active


def test_evento_sem_sujeito_nao_cria_identidade(ctx):
    evento = _evento(sujeito=None, externo="wamid.SEMSUJ")
    entrada.registrar_conteudo_textual_canal(evento.id, TEXTO)
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_SUJEITO_AUSENTE
    assert resultado.identidade_id is None
    assert IdentidadeCanalExterna.query.count() == 0
    assert _status(evento.id) == EventoCanalRecebido.STATUS_ERRO_SEGURO
    replay = entrada.processar_evento_canal_recebido(evento.id)
    assert replay.codigo == entrada.CODIGO_JA_PROCESSADO
    assert IdentidadeCanalExterna.query.count() == 0
    assert db.session.is_active
    seguinte = _textual(externo="wamid.DEPOIS")
    assert entrada.processar_evento_canal_recebido(seguinte.id).rota == entrada.ROTA_GUEST


def test_texto_vazio_e_rejeitado(ctx):
    evento = _evento(externo="wamid.VAZIO")
    with pytest.raises(entrada.RecusaConteudo) as exc:
        entrada.registrar_conteudo_textual_canal(evento.id, "   ")
    assert exc.value.codigo == entrada.CODIGO_TEXTO_VAZIO
    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.add(ConteudoTextualCanal(evento_id=evento.id, texto=""))
            db.session.flush()
    assert ConteudoTextualCanal.query.count() == 0
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_TEXTO_AUSENTE
    assert IdentidadeCanalExterna.query.count() == 0
    assert _status(evento.id) == EventoCanalRecebido.STATUS_ERRO_SEGURO


def test_texto_acima_do_limite_e_rejeitado(ctx):
    evento = _evento(externo="wamid.LIMITE")
    with pytest.raises(entrada.RecusaConteudo) as exc:
        entrada.registrar_conteudo_textual_canal(
            evento.id,
            "a" * (ConteudoTextualCanal.TEXTO_MAXIMO + 1),
        )
    assert exc.value.codigo == entrada.CODIGO_TEXTO_LIMITE
    assert ConteudoTextualCanal.query.count() == 0
    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.add(
                ConteudoTextualCanal(
                    evento_id=evento.id,
                    texto="b" * (ConteudoTextualCanal.TEXTO_MAXIMO + 1),
                )
            )
            db.session.flush()
    assert ConteudoTextualCanal.query.count() == 0
    no_limite = _evento(externo="wamid.NO_LIMITE")
    row = entrada.registrar_conteudo_textual_canal(
        no_limite.id,
        "c" * ConteudoTextualCanal.TEXTO_MAXIMO,
    )
    assert len(row.texto) == ConteudoTextualCanal.TEXTO_MAXIMO
    assert IdentidadeCanalExterna.query.count() == 0


def test_cas_de_processando_nao_executa_de_novo(ctx, monkeypatch):
    evento = _textual(externo="wamid.CAS")
    evento.status_processamento = EventoCanalRecebido.STATUS_PROCESSANDO
    db.session.commit()
    monkeypatch.setattr(
        entrada,
        "obter_ou_criar_identidade_externa",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("identidade")),
    )
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_EM_PROCESSAMENTO
    assert resultado.identidade_id is None
    assert IdentidadeCanalExterna.query.count() == 0
    assert _status(evento.id) == EventoCanalRecebido.STATUS_PROCESSANDO


def test_corrida_nao_duplica_processamento():
    flask_app = Flask("corrida-canal-entrada")
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
        evento = _textual(externo="wamid.CORRIDA")
        evento_id = evento.id
        db.session.commit()
        db.session.remove()
        barreira = threading.Barrier(2)
        resultados: dict[int, str] = {}

        def _worker(indice: int) -> None:
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    resultados[indice] = entrada.processar_evento_canal_recebido(evento_id).codigo
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = f"erro:{type(exc).__name__}:{exc}"

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert set(resultados) == {1, 2}
        assert entrada.CODIGO_ROTEADO in resultados.values()
        assert resultados[1] != resultados[2]
        assert set(resultados.values()) <= {
            entrada.CODIGO_ROTEADO,
            entrada.CODIGO_JA_PROCESSADO,
            entrada.CODIGO_EM_PROCESSAMENTO,
        }
        assert IdentidadeCanalExterna.query.count() == 1
        assert _status(evento_id) == EventoCanalRecebido.STATUS_ROTEADO
        db.session.remove()
        run_test_schema_operation(
            db,
            PYTEST_DISPOSABLE_SQLALCHEMY_URI,
            testing=True,
            operation="drop_all",
        )


def test_processador_nao_depende_do_payload_meta(ctx):
    evento = _textual(externo="wamid.SEM_META")
    mensagem = entrada.montar_mensagem_canal_entrada(evento)
    assert mensagem.texto == TEXTO
    assert mensagem.provider == EventoCanalRecebido.PROVIDER_META_WHATSAPP
    assert mensagem.tipo == EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL
    assert entrada.processar_evento_canal_recebido(evento.id).rota == entrada.ROTA_GUEST
    fonte = _fonte_processador()
    for trecho in (
        "messaging_product",
        "X-Hub-Signature",
        "whatsapp_business_account",
        "entry",
        "sha256=",
        "graph.facebook",
        "chat_julia",
        "gemini",
        "cleiton_monetizacao",
        "stripe",
        "requests",
        "httpx",
        "iniciar_onboarding_canal",
        "registrar_interacao_guest",
    ):
        assert trecho not in fonte
    webhook = (
        ROOT / "app" / "services" / "whatsapp_meta_webhook_service.py"
    ).read_text(encoding="utf-8")
    assert "processar_evento_canal_recebido" not in webhook
    assert "registrar_conteudo_textual_canal" in webhook


WA_META = "16505551234"
PHONE_META = "106540352242922"


def test_from_numerico_meta_e_aceito_em_whatsapp_meta(ctx):
    with pytest.raises(CanalAquisicaoError) as formatado:
        obter_ou_criar_identidade_externa(
            provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
            sujeito_externo=f"+{WA_META}",
        )
    assert formatado.value.codigo == "sujeito_externo_invalido"
    with pytest.raises(MaterialSensivelRecusadoError):
        obter_ou_criar_identidade_externa(
            provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
            sujeito_externo=WA_META,
            contexto_destino='{"token":"abc"}',
        )
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo=f"  {WA_META}  ",
        contexto_destino=PHONE_META,
    )
    assert ident.provedor == entrada.PROVEDOR_IDENTIDADE_WHATSAPP
    assert ident.sujeito_externo == WA_META
    assert ident.contexto_destino == PHONE_META
    assert ident.user_id is None
    assert "telefone" not in IdentidadeCanalExterna.__table__.columns.keys()
    zeros = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo="0016505551234",
    )
    assert zeros.id != ident.id
    assert zeros.sujeito_externo == "0016505551234"


def test_phone_number_id_numerico_e_aceito_como_contexto(ctx):
    ident = obter_ou_criar_identidade_externa(
        provedor=entrada.PROVEDOR_IDENTIDADE_WHATSAPP,
        sujeito_externo="subjOpacoMeta",
        contexto_destino=f"  {PHONE_META}  ",
    )
    assert ident.contexto_destino == PHONE_META
    assert ident.sujeito_externo == "subjOpacoMeta"
    assert "+" not in ident.contexto_destino


def test_outro_provedor_continua_recusando_digitos_de_telefone(ctx):
    with pytest.raises(CanalAquisicaoError) as sujeito:
        obter_ou_criar_identidade_externa(
            provedor="canal_sintetico",
            sujeito_externo="5511999999999",
        )
    assert sujeito.value.codigo == "sujeito_externo_invalido"
    with pytest.raises(CanalAquisicaoError) as remetente:
        obter_ou_criar_identidade_externa(
            provedor="canal_sintetico",
            sujeito_externo=WA_META,
        )
    assert remetente.value.codigo == "sujeito_externo_invalido"
    with pytest.raises(CanalAquisicaoError) as destino:
        obter_ou_criar_identidade_externa(
            provedor="canal_sintetico",
            sujeito_externo="subj-opaco",
            contexto_destino=PHONE_META,
        )
    assert destino.value.codigo == "contexto_destino_invalido"
    assert IdentidadeCanalExterna.query.count() == 0
    opaca = obter_ou_criar_identidade_externa(
        provedor="canal_sintetico",
        sujeito_externo="subj-opaco",
        contexto_destino="dest-opaco",
    )
    assert opaca.sujeito_externo == "subj-opaco"
    assert opaca.contexto_destino == "dest-opaco"


def test_remetente_numerico_e_resolvido_no_roteamento(ctx):
    evento = _textual(sujeito=WA_META, contexto=PHONE_META, externo="wamid.NUM")
    resultado = entrada.processar_evento_canal_recebido(evento.id)
    assert resultado.codigo == entrada.CODIGO_ROTEADO
    assert resultado.rota == entrada.ROTA_GUEST
    ident = IdentidadeCanalExterna.query.one()
    assert ident.provedor == entrada.PROVEDOR_IDENTIDADE_WHATSAPP
    assert ident.sujeito_externo == WA_META
    assert ident.contexto_destino == PHONE_META
    assert ident.user_id is None
    assert _status(evento.id) == EventoCanalRecebido.STATUS_ROTEADO
