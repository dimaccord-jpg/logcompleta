"""Lote WhatsApp 3G2: interação útil entra na régua de créditos do Gerenciador.

A taxa vem do Bloco D. Sem taxa não há débito. Com taxa, a conta é a
mesma de linhas e tokens: quantidade / taxa, quantizada em 6 casas.
"""
from __future__ import annotations

import logging
import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from flask import Flask

from app.extensions import db
from app.models import (
    CleitonBillingApropriacao,
    CleitonCostConfig,
    ConsumoInteracaoCanal,
    EventoCanalRecebido,
    EventoCanalSaida,
    ExecucaoOperacionalCanal,
    Franquia,
    IaConsumoEvento,
    MonetizacaoFato,
    Plugin,
    utcnow_naive,
)
from app.services import canal_apropriacao_whatsapp_service as apropriacao
from app.services import canal_consumo_whatsapp_service as consumo
from app.services import canal_operacao_whatsapp_service as operacao
from app.services import canal_orquestracao_whatsapp_service as orquestracao
from app.services.cleiton_cost_service import parse_regua_opcional, save_config
from app.services.cleiton_franquia_operacional_service import (
    classificar_estado_operacional_franquia,
    converter_interacoes_whatsapp_para_creditos,
    converter_linhas_para_creditos,
)
from app.services.cleiton_plano_resolver import resolver_plano_operacional_para_franquia
from tests.conftest import seed_cleiton_cost_config
from tests.test_whatsapp_canal_lote3g1 import (
    EMAIL,
    PERGUNTA,
    PHONE,
    RESPOSTA,
    TOKEN,
    _cliente,
    _configurar,
    _mock_julia,
    _mock_meta,
    _proibir_julia,
    _status,
    _texto,
    _vincular,
)

TAXA = 10


def _dec(valor) -> Decimal:
    return Decimal(str(valor))


def _definir_taxa(valor):
    cfg = db.session.get(CleitonCostConfig, 1)
    if cfg is None:
        cfg = seed_cleiton_cost_config()
    cfg.interacoes_whatsapp_por_credito = valor
    db.session.commit()
    db.session.refresh(cfg)
    return cfg


def _unitario(cfg) -> Decimal:
    db.session.refresh(cfg)
    valor, erro = converter_interacoes_whatsapp_para_creditos(1, cfg)
    assert erro is None and valor is not None and valor > 0
    return valor


def _preparar(app, monkeypatch, mensagem_id, taxa=TAXA, consumo_a="1"):
    _configurar(monkeypatch)
    meta = _mock_meta(monkeypatch)
    modelos = _mock_julia(monkeypatch)
    seed_cleiton_cost_config()
    cfg = _definir_taxa(taxa)
    client = _cliente(app)
    user, admin, franquia, outra, ident = _vincular(consumo_a=consumo_a)
    antes = _dec(franquia.consumo_acumulado)
    assert _texto(client, mensagem_id, PERGUNTA).status_code == 200
    db.session.expire_all()
    return client, meta, modelos, user, admin, franquia, outra, ident, cfg, antes


def _admin_contexto():
    import importlib
    import os

    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    web = importlib.import_module("app.web")
    from app.painel_admin import admin_routes

    return web, admin_routes


def _auth(monkeypatch, admin_routes):
    monkeypatch.setattr(
        admin_routes,
        "current_user",
        SimpleNamespace(is_authenticated=True, is_admin=True, email="admin@example.com"),
    )
    monkeypatch.setattr(admin_routes, "verificar_acesso_admin", lambda: True)


def test_conversao_whatsapp_usa_a_mesma_conta_das_linhas(ctx):
    cfg = seed_cleiton_cost_config()
    cfg.credit_lines_per_credit = 3
    cfg.interacoes_whatsapp_por_credito = 3
    db.session.commit()
    linhas, erro_linhas = converter_linhas_para_creditos(1, cfg)
    interacoes, erro_interacoes = converter_interacoes_whatsapp_para_creditos(1, cfg)
    assert erro_linhas is None and erro_interacoes is None
    assert linhas == interacoes
    assert interacoes != Decimal("1")
    cfg.interacoes_whatsapp_por_credito = None
    vazio, erro_vazio = converter_interacoes_whatsapp_para_creditos(1, cfg)
    assert vazio is None and erro_vazio
    cfg.interacoes_whatsapp_por_credito = 0
    zero, erro_zero = converter_interacoes_whatsapp_para_creditos(1, cfg)
    assert zero is None and erro_zero
    cfg.interacoes_whatsapp_por_credito = -4
    negativo, erro_negativo = converter_interacoes_whatsapp_para_creditos(1, cfg)
    assert negativo is None and erro_negativo


def test_parse_da_regua_aceita_vazio_e_decimal_e_rejeita_zero(ctx):
    assert parse_regua_opcional(None) is None
    assert parse_regua_opcional("") is None
    assert parse_regua_opcional("  ") is None
    assert parse_regua_opcional("10,5") == 10.5
    assert parse_regua_opcional("10.5") == 10.5
    for bruto in ("0", "0,0", "0.0", "-1", "-0,5", "abc"):
        with pytest.raises(ValueError):
            parse_regua_opcional(bruto)
    save_config(
        runtime_monthly_cost=None,
        month_seconds=2592000,
        allocation_percent=1.0,
        overhead_factor=1.0,
        cost_per_million_tokens=None,
        credit_tokens_per_credit=1000.0,
        credit_lines_per_credit=100.0,
        credit_ms_per_credit=1000.0,
        interacoes_whatsapp_por_credito=12.5,
    )
    db.session.expire_all()
    cfg = db.session.get(CleitonCostConfig, 1)
    assert cfg.interacoes_whatsapp_por_credito == 12.5
    assert cfg.credit_tokens_per_credit == 1000.0
    save_config(
        runtime_monthly_cost=None,
        month_seconds=2592000,
        allocation_percent=1.0,
        overhead_factor=1.0,
        cost_per_million_tokens=None,
        credit_tokens_per_credit=1000.0,
        credit_lines_per_credit=100.0,
        credit_ms_per_credit=1000.0,
        interacoes_whatsapp_por_credito=None,
    )
    db.session.expire_all()
    assert db.session.get(CleitonCostConfig, 1).interacoes_whatsapp_por_credito is None
    with pytest.raises(ValueError):
        save_config(
            runtime_monthly_cost=None,
            month_seconds=2592000,
            allocation_percent=1.0,
            overhead_factor=1.0,
            cost_per_million_tokens=None,
            interacoes_whatsapp_por_credito=0,
        )


def test_bloco_d_mostra_campo_salva_decimal_e_rejeita_invalido(monkeypatch):
    web, admin_routes = _admin_contexto()
    _auth(monkeypatch, admin_routes)
    monkeypatch.setattr(
        "app.services.cleiton_cost_service.get_or_create_config",
        lambda: SimpleNamespace(
            runtime_monthly_cost=450.0,
            month_seconds=2592000,
            allocation_percent=1.0,
            overhead_factor=1.0,
            cost_per_million_tokens=None,
            credit_tokens_per_credit=1000.0,
            credit_lines_per_credit=500.0,
            credit_ms_per_credit=60000.0,
            interacoes_whatsapp_por_credito=12.5,
            updated_at=None,
        ),
    )
    monkeypatch.setattr("app.services.cleiton_cost_service.compute_cost_per_second", lambda cfg: 0.001)
    monkeypatch.setattr(
        "app.services.cleiton_doc_config_service.get_cleiton_doc_config",
        lambda: SimpleNamespace(
            upload_enabled=True,
            max_files_per_session=5,
            session_max_bytes=1,
            upload_ttl_hours=48,
            cleanup_enabled=True,
            prompt_context_max_chars=24000,
            prompt_max_files_considered=3,
            pdf_enabled=True,
            pdf_max_bytes=1,
            pdf_max_pages=1,
            pdf_max_chars=1,
            excel_enabled=True,
            excel_max_bytes=1,
            excel_max_rows=1,
            excel_max_columns=1,
            excel_max_chars=1,
            docx_enabled=True,
            docx_max_bytes=1,
            docx_max_paragraphs=1,
            docx_max_chars=1,
            txt_enabled=True,
            txt_max_bytes=1,
            txt_max_chars=1,
            xml_enabled=True,
            xml_max_bytes=1,
            xml_max_nodes=1,
            xml_max_depth=1,
            xml_max_chars=1,
            csv_enabled=True,
            csv_max_bytes=1,
            csv_max_rows=1,
            csv_max_columns=1,
            csv_max_chars=1,
        ),
    )
    with web.app.test_request_context("/admin/agentes/cleiton"):
        html = admin_routes.agentes_cleiton.__wrapped__()
    bloco_d = html.index("Bloco D - Régua de conversão de créditos")
    campo = html.index("Interações WhatsApp por 1 crédito")
    bloco_e = html.index("Bloco E - Upload documental governado")
    assert bloco_d < campo < bloco_e
    assert 'name="interacoes_whatsapp_por_credito"' in html
    assert 'value="12.5"' in html
    assert "deixe em branco se ainda não definido" in html

    chamadas = []

    def _save(*, cost_kwargs, doc_campos):
        chamadas.append(cost_kwargs)

    monkeypatch.setattr(
        "app.services.cleiton_doc_config_service.salvar_agentes_cleiton_config",
        _save,
    )
    with web.app.test_request_context(
        "/admin/agentes/cleiton",
        method="POST",
        data={
            "month_seconds": "2592000",
            "credit_tokens_per_credit": "1000",
            "credit_lines_per_credit": "500",
            "credit_ms_per_credit": "60000",
            "interacoes_whatsapp_por_credito": "10,5",
        },
    ):
        admin_routes.agentes_cleiton.__wrapped__()
    assert chamadas[-1]["interacoes_whatsapp_por_credito"] == 10.5
    assert chamadas[-1]["credit_tokens_per_credit"] == 1000.0

    with web.app.test_request_context(
        "/admin/agentes/cleiton",
        method="POST",
        data={"month_seconds": "2592000", "interacoes_whatsapp_por_credito": ""},
    ):
        admin_routes.agentes_cleiton.__wrapped__()
    assert chamadas[-1]["interacoes_whatsapp_por_credito"] is None

    from flask import get_flashed_messages

    for bruto in ("0", "-2", "abc"):
        antes = len(chamadas)
        with web.app.test_request_context(
            "/admin/agentes/cleiton",
            method="POST",
            data={
                "cleiton_doc_form": "1",
                "month_seconds": "2592000",
                "interacoes_whatsapp_por_credito": bruto,
            },
        ):
            admin_routes.agentes_cleiton.__wrapped__()
            mensagens = get_flashed_messages(with_categories=True)
        assert len(chamadas) == antes
        assert any(categoria == "danger" for categoria, _texto in mensagens)


def test_taxa_vazia_registra_sem_debito(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    _mock_meta(monkeypatch)
    _mock_julia(monkeypatch)
    seed_cleiton_cost_config()
    assert consumo.obter_regra_conversao_whatsapp() is None
    client = _cliente(app)
    user, admin, franquia, outra, _ident = _vincular()
    with caplog.at_level(logging.INFO):
        assert _texto(client, "wamid.G2VAZIA", PERGUNTA).status_code == 200
    row = ConsumoInteracaoCanal.query.one()
    assert row.estado == ConsumoInteracaoCanal.ESTADO_PRONTA
    assert row.motivo == ConsumoInteracaoCanal.MOTIVO_TAXA_PENDENTE
    assert row.quantidade == 1
    assert row.unidade == ConsumoInteracaoCanal.UNIDADE_INTERACAO
    assert row.creditos_apropriados is None
    assert CleitonBillingApropriacao.query.count() == 0
    db.session.refresh(franquia)
    db.session.refresh(outra)
    assert _dec(franquia.consumo_acumulado) == Decimal("1")
    assert _dec(outra.consumo_acumulado) == Decimal("7")
    assert user.creditos == 10
    assert admin.creditos == 10
    assert PERGUNTA not in caplog.text
    assert RESPOSTA not in caplog.text
    assert PHONE not in caplog.text
    assert EMAIL not in caplog.text
    assert TOKEN not in caplog.text


def test_taxa_configurada_aplica_fracao_da_regua(ctx, app, monkeypatch, caplog):
    with caplog.at_level(logging.INFO):
        _client, _meta, modelos, user, admin, franquia, outra, _ident, cfg, antes = _preparar(
            app, monkeypatch, "wamid.G2TAXA"
        )
    valor = _unitario(cfg)
    assert valor != Decimal("1")
    row = ConsumoInteracaoCanal.query.one()
    assert row.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA
    assert row.motivo == ConsumoInteracaoCanal.MOTIVO_APROPRIADA
    assert row.quantidade == 1
    assert row.unidade == ConsumoInteracaoCanal.UNIDADE_INTERACAO
    assert row.user_id == user.id
    assert row.franquia_id == franquia.id
    assert _dec(row.creditos_apropriados) == valor
    marker = CleitonBillingApropriacao.query.one()
    assert marker.idempotency_key == row.chave_idempotente
    assert marker.agent == "canal_whatsapp"
    assert marker.rows_processed == 0
    assert marker.processing_time_ms == 0
    assert marker.processing_event_id is None
    assert marker.error_summary is None
    assert _dec(marker.creditos_apropriados) == valor
    db.session.refresh(franquia)
    db.session.refresh(outra)
    assert _dec(franquia.consumo_acumulado) == antes + valor
    assert _dec(outra.consumo_acumulado) == Decimal("7")
    assert user.creditos == 10
    assert admin.creditos == 10
    assert Plugin.query.count() == 0
    assert MonetizacaoFato.query.count() == 0
    assert len(modelos.chamadas) == 1
    for evento in IaConsumoEvento.query.all():
        assert evento.usuario_id is None
        from app.services.cleiton_franquia_operacional_service import (
            aplicar_motor_apos_ia_consumo_evento,
        )

        aplicar_motor_apos_ia_consumo_evento(int(evento.id))
    db.session.refresh(franquia)
    assert _dec(franquia.consumo_acumulado) == antes + valor
    assert CleitonBillingApropriacao.query.count() == 1
    assert PERGUNTA not in caplog.text
    assert RESPOSTA not in caplog.text
    assert PHONE not in caplog.text
    assert EMAIL not in caplog.text
    assert TOKEN not in caplog.text


def test_dez_interacoes_fecham_a_regua(ctx, app, monkeypatch):
    client, _meta, _modelos, _user, _admin, franquia, outra, _ident, cfg, antes = _preparar(
        app, monkeypatch, "wamid.G2N0", taxa=TAXA, consumo_a="0"
    )
    for indice in range(1, 10):
        assert _texto(client, f"wamid.G2N{indice}", PERGUNTA).status_code == 200
    valor = _unitario(cfg)
    total_formula, erro = converter_interacoes_whatsapp_para_creditos(10, cfg)
    assert erro is None
    rows = ConsumoInteracaoCanal.query.order_by(ConsumoInteracaoCanal.id).all()
    assert len(rows) == 10
    assert all(row.quantidade == 1 for row in rows)
    assert all(_dec(row.creditos_apropriados) == valor for row in rows)
    soma = sum((_dec(row.creditos_apropriados) for row in rows), Decimal("0"))
    assert soma == valor * 10
    assert soma == total_formula
    assert soma == Decimal("1")
    assert valor != Decimal("1")
    db.session.refresh(franquia)
    db.session.refresh(outra)
    assert _dec(franquia.consumo_acumulado) == antes + soma
    assert _dec(outra.consumo_acumulado) == Decimal("7")
    assert CleitonBillingApropriacao.query.count() == 10


def test_replay_nao_duplica_debito(ctx, app, monkeypatch):
    client, _meta, modelos, _user, _admin, franquia, _outra, _ident, cfg, antes = _preparar(
        app, monkeypatch, "wamid.G2REPLAY"
    )
    valor = _unitario(cfg)
    consumo_id = ConsumoInteracaoCanal.query.one().id
    evento_id = ExecucaoOperacionalCanal.query.one().evento_id
    execucao_id = ExecucaoOperacionalCanal.query.one().id
    assert _texto(client, "wamid.G2REPLAY", PERGUNTA).get_json()["replays"] == 1
    assert orquestracao.processar_evento_whatsapp(evento_id).codigo == EventoCanalSaida.STATUS_ACEITO
    assert operacao.executar_operacao_canal(evento_id).codigo == EventoCanalSaida.STATUS_ACEITO
    repetido = consumo.registrar_consumo_operacional_canal(execucao_id)
    assert repetido.duplicado is True
    assert repetido.consumo_id == consumo_id
    de_novo = apropriacao.sincronizar_interacao(consumo_id, imediata=True)
    assert de_novo.duplicado is True
    assert de_novo.creditos == valor
    assert apropriacao.apropriar_consumos_pendentes() == []
    db.session.refresh(franquia)
    assert _dec(franquia.consumo_acumulado) == antes + valor
    assert ConsumoInteracaoCanal.query.count() == 1
    assert CleitonBillingApropriacao.query.count() == 1
    assert len(modelos.chamadas) == 1


def test_status_de_entrega_nao_debita_de_novo(ctx, app, monkeypatch):
    client, meta, modelos, _user, _admin, franquia, _outra, _ident, cfg, antes = _preparar(
        app, monkeypatch, "wamid.G2STATUS"
    )
    valor = _unitario(cfg)
    mensagem = EventoCanalSaida.query.one().provider_message_id
    for status in ("sent", "delivered", "read"):
        assert _status(client, mensagem, status).status_code == 200
    db.session.refresh(franquia)
    assert _dec(franquia.consumo_acumulado) == antes + valor
    assert ConsumoInteracaoCanal.query.count() == 1
    assert len(modelos.chamadas) == 1
    assert len(meta) == 1


def test_guest_com_taxa_nao_debita(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _mock_meta(monkeypatch)
    _proibir_julia(monkeypatch)
    seed_cleiton_cost_config()
    _definir_taxa(TAXA)
    client = _cliente(app)
    assert _texto(client, "wamid.G2GUEST", PERGUNTA).status_code == 200
    assert ConsumoInteracaoCanal.query.count() == 0
    assert CleitonBillingApropriacao.query.count() == 0
    assert ExecucaoOperacionalCanal.query.count() == 0


def test_troca_de_taxa_nao_recalcula_o_que_ja_foi_lancado(ctx, app, monkeypatch):
    client, _meta, _modelos, _user, _admin, franquia, outra, _ident, cfg, antes = _preparar(
        app, monkeypatch, "wamid.G2A", taxa=TAXA
    )
    primeiro = _unitario(cfg)
    id_a = ConsumoInteracaoCanal.query.one().id
    cfg = _definir_taxa(20)
    assert _texto(client, "wamid.G2C", PERGUNTA).status_code == 200
    db.session.expire_all()
    segundo = _unitario(cfg)
    assert primeiro != segundo
    assert ConsumoInteracaoCanal.query.count() == 2
    recarregada_a = ConsumoInteracaoCanal.query.filter_by(id=id_a).one()
    assert _dec(recarregada_a.creditos_apropriados) == primeiro
    outra_linha = ConsumoInteracaoCanal.query.filter(
        ConsumoInteracaoCanal.id != id_a
    ).one()
    assert _dec(outra_linha.creditos_apropriados) == segundo
    db.session.refresh(franquia)
    db.session.refresh(outra)
    assert _dec(franquia.consumo_acumulado) == antes + primeiro + segundo
    assert _dec(outra.consumo_acumulado) == Decimal("7")


def test_pendente_so_apropria_na_chamada_explicita(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    _mock_meta(monkeypatch)
    _mock_julia(monkeypatch)
    seed_cleiton_cost_config()
    client = _cliente(app)
    _user, _admin, franquia, outra, _ident = _vincular()
    assert _texto(client, "wamid.G2PEND", PERGUNTA).status_code == 200
    row = ConsumoInteracaoCanal.query.one()
    assert row.motivo == ConsumoInteracaoCanal.MOTIVO_TAXA_PENDENTE
    cfg = _definir_taxa(TAXA)
    valor = _unitario(cfg)
    evento_id = int(row.evento_canal_recebido_id)
    execucao_id = int(row.execucao_operacional_canal_id)
    assert _texto(client, "wamid.G2PEND", PERGUNTA).status_code == 200
    assert orquestracao.processar_evento_whatsapp(evento_id).codigo == EventoCanalSaida.STATUS_ACEITO
    repetido = consumo.registrar_consumo_operacional_canal(execucao_id)
    assert repetido.duplicado is True
    db.session.refresh(franquia)
    assert _dec(franquia.consumo_acumulado) == Decimal("1")
    assert ConsumoInteracaoCanal.query.one().estado == ConsumoInteracaoCanal.ESTADO_PRONTA
    primeiro = apropriacao.apropriar_consumos_pendentes()
    assert len(primeiro) == 1
    assert primeiro[0].codigo == apropriacao.CODIGO_APROPRIADA
    assert primeiro[0].creditos == valor
    segundo = apropriacao.apropriar_consumos_pendentes()
    assert segundo == []
    de_novo = apropriacao.sincronizar_interacao(int(row.id), imediata=True)
    assert de_novo.duplicado is True
    assert de_novo.creditos == valor
    db.session.expire_all()
    atual = ConsumoInteracaoCanal.query.one()
    assert atual.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA
    assert _dec(atual.creditos_apropriados) == valor
    db.session.refresh(franquia)
    db.session.refresh(outra)
    assert _dec(franquia.consumo_acumulado) == Decimal("1") + valor
    assert _dec(outra.consumo_acumulado) == Decimal("7")
    assert CleitonBillingApropriacao.query.count() == 1


def test_janela_de_falha_conclui_local_sem_novo_debito(ctx, app, monkeypatch):
    _client, _meta, _modelos, _user, _admin, franquia, _outra, _ident, cfg, antes = _preparar(
        app, monkeypatch, "wamid.G2JANELA"
    )
    valor = _unitario(cfg)
    row = ConsumoInteracaoCanal.query.one()
    db.session.refresh(franquia)
    saldo = _dec(franquia.consumo_acumulado)
    assert saldo == antes + valor
    row.estado = ConsumoInteracaoCanal.ESTADO_PRONTA
    row.motivo = ConsumoInteracaoCanal.MOTIVO_TAXA_PENDENTE
    row.creditos_apropriados = None
    row.atualizada_em = utcnow_naive()
    db.session.commit()
    cfg = _definir_taxa(20)
    novo = _unitario(cfg)
    assert novo != valor
    repetido = consumo.registrar_consumo_operacional_canal(int(row.execucao_operacional_canal_id))
    assert repetido.duplicado is True
    db.session.expire_all()
    atual = ConsumoInteracaoCanal.query.one()
    assert atual.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA
    assert _dec(atual.creditos_apropriados) == valor
    assert _dec(atual.creditos_apropriados) != novo
    db.session.refresh(franquia)
    assert _dec(franquia.consumo_acumulado) == saldo
    assert CleitonBillingApropriacao.query.count() == 1


def test_estouro_atualiza_status_pela_regra_existente(ctx, app, monkeypatch):
    _client, _meta, _modelos, _user, _admin, franquia, outra, _ident, cfg, _antes = _preparar(
        app, monkeypatch, "wamid.G2LIMITE", consumo_a="49.95"
    )
    valor = _unitario(cfg)
    db.session.refresh(franquia)
    assert _dec(franquia.consumo_acumulado) == Decimal("49.95") + valor
    assert _dec(franquia.consumo_acumulado) > _dec(franquia.limite_total)
    plano = resolver_plano_operacional_para_franquia(franquia.id)
    status, _motivo = classificar_estado_operacional_franquia(franquia, plano)
    assert franquia.status == status
    assert status == Franquia.STATUS_BLOCKED
    db.session.refresh(outra)
    assert outra.status == Franquia.STATUS_ACTIVE
    assert _dec(outra.consumo_acumulado) == Decimal("7")


def test_duas_sessoes_apropriam_uma_vez(tmp_path):
    banco = tmp_path / "corrida_apropriacao_3g2.sqlite"
    flask_app = Flask("corrida-apropriacao-3g2")
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + banco.as_posix()
    flask_app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 5},
    }
    flask_app.config["TESTING"] = True
    db.init_app(flask_app)
    with flask_app.app_context():
        import app.models  # noqa: F401

        db.create_all()
        user, admin, franquia, outra, ident = _vincular(email="corrida.3g2@example.com")
        user_id = int(user.id)
        franquia_id = int(franquia.id)
        outra_id = int(outra.id)
        admin_id = int(admin.id)
        seed_cleiton_cost_config()
        agora = utcnow_naive()
        evento = EventoCanalRecebido(
            provider=EventoCanalRecebido.PROVIDER_META_WHATSAPP,
            evento_externo_id="wamid.G2CORRIDA",
            tipo_evento=EventoCanalRecebido.TIPO_MENSAGEM_TEXTUAL,
            sujeito_externo=PHONE,
            contexto_destino="106540352242922",
            recebido_em=agora,
            status_processamento=EventoCanalRecebido.STATUS_ROTEADO,
            correlation_id="corrida-3g2",
            diagnostico_seguro="mensagem_textual",
        )
        db.session.add(evento)
        db.session.flush()
        execucao = ExecucaoOperacionalCanal(
            evento_id=int(evento.id),
            identidade_id=int(ident.id),
            user_id=user_id,
            codigo=ExecucaoOperacionalCanal.CODIGO_RESPOSTA,
            estado=ExecucaoOperacionalCanal.ESTADO_CONCLUIDA,
            texto_resposta=RESPOSTA,
            execution_id=str(uuid4()),
            conclusao_util=1,
            correlation_id="corrida-3g2",
            criada_em=agora,
            atualizada_em=agora,
        )
        db.session.add(execucao)
        db.session.commit()
        execucao_id = int(execucao.id)
        registro = consumo.registrar_consumo_operacional_canal(execucao_id)
        assert registro.estado == ConsumoInteracaoCanal.ESTADO_PRONTA
        consumo_id = int(registro.consumo_id)
        cfg = _definir_taxa(TAXA)
        valor = _unitario(cfg)
        db.session.remove()

        barreira = threading.Barrier(2)
        resultados: dict[int, object] = {}

        def _worker(indice: int) -> None:
            try:
                with flask_app.app_context():
                    barreira.wait(timeout=5)
                    resultados[indice] = apropriacao.sincronizar_interacao(
                        consumo_id, imediata=True
                    )
                    db.session.remove()
            except Exception as exc:
                resultados[indice] = exc

        threads = [threading.Thread(target=_worker, args=(indice,)) for indice in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)

        assert set(resultados) == {1, 2}
        falhas = {
            indice: item
            for indice, item in resultados.items()
            if not isinstance(item, apropriacao.ResultadoApropriacaoCanal)
        }
        assert falhas == {}
        assert ConsumoInteracaoCanal.query.count() == 1
        row = ConsumoInteracaoCanal.query.one()
        assert row.estado == ConsumoInteracaoCanal.ESTADO_APROPRIADA
        assert row.user_id == user_id
        assert row.franquia_id == franquia_id
        assert _dec(row.creditos_apropriados) == valor
        assert CleitonBillingApropriacao.query.count() == 1
        franquia_atual = db.session.get(Franquia, franquia_id)
        outra_atual = db.session.get(Franquia, outra_id)
        assert _dec(franquia_atual.consumo_acumulado) == Decimal("1") + valor
        assert _dec(outra_atual.consumo_acumulado) == Decimal("7")
        assert db.session.get(type(user), user_id).creditos == 10
        assert db.session.get(type(admin), admin_id).creditos == 10
        db.session.remove()
        db.drop_all()


def test_servico_reusa_o_gerenciador_sem_carteira_propria(ctx):
    fonte = Path(apropriacao.__file__).read_text(encoding="utf-8")
    assert "lancar_creditos_convertidos" in fonte
    assert "converter_interacoes_whatsapp_para_creditos" in fonte
    assert "consumo_acumulado" not in fonte
    assert "Plugin(" not in fonte
    seed_cleiton_cost_config()
    assert consumo.obter_regra_conversao_whatsapp() is None
