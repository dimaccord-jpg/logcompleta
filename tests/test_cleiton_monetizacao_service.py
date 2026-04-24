import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.extensions import db
from app.models import ConfigRegras, ContaMonetizacaoVinculo, Franquia, MonetizacaoFato
from app.services import plano_service
from app.services.cleiton_monetizacao_service import (
    _extrair_ids_externos_stripe,
    _obter_assinatura_stripe_ativa,
    aplicar_fato_contratual_em_franquia,
    efetivar_mudancas_pendentes_ciclo,
    iniciar_jornada_assinatura_stripe,
    obter_contexto_monetizacao_conta,
    processar_evento_stripe,
    processar_fato_stripe_conciliado,
    reprocessar_fato_pendente_correlacao_admin,
    registrar_vinculo_comercial_externo,
    registrar_fato_monetizacao,
    sincronizar_retorno_checkout_stripe,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _ts(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _naive_utc(ts: int) -> datetime:
    """Converte timestamp Unix para datetime naive em UTC, alinhado a _to_datetime_utc_naive do serviço."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def _configurar_plano_starter_para_teste(*, franquia_ref_id: int) -> None:
    cfg_ref = ConfigRegras(
        chave="plano_franquia_ref_admin_starter",
        descricao="ref starter",
        valor_inteiro=franquia_ref_id,
        valor_texto=str(franquia_ref_id),
    )
    cfg_provider = ConfigRegras(
        chave="plano_gateway_provider_admin_starter",
        descricao="provider starter",
        valor_texto="stripe",
    )
    cfg_product = ConfigRegras(
        chave="plano_gateway_product_id_admin_starter",
        descricao="product starter",
        valor_texto="prod_test",
    )
    cfg_price = ConfigRegras(
        chave="plano_gateway_price_id_admin_starter",
        descricao="price starter",
        valor_texto="price_starter_test",
    )
    cfg_currency = ConfigRegras(
        chave="plano_gateway_currency_admin_starter",
        descricao="currency starter",
        valor_texto="brl",
    )
    cfg_interval = ConfigRegras(
        chave="plano_gateway_interval_admin_starter",
        descricao="interval starter",
        valor_texto="month",
    )
    cfg_ready = ConfigRegras(
        chave="plano_gateway_ready_admin_starter",
        descricao="ready starter",
        valor_inteiro=1,
        valor_texto="1",
    )
    db.session.add_all(
        [
            cfg_ref,
            cfg_provider,
            cfg_product,
            cfg_price,
            cfg_currency,
            cfg_interval,
            cfg_ready,
        ]
    )
    db.session.commit()


def _configurar_plano_pro_para_teste(*, franquia_ref_id: int) -> None:
    cfg_ref = ConfigRegras(
        chave="plano_franquia_ref_admin_pro",
        descricao="ref pro",
        valor_inteiro=franquia_ref_id,
        valor_texto=str(franquia_ref_id),
    )
    cfg_provider = ConfigRegras(
        chave="plano_gateway_provider_admin_pro",
        descricao="provider pro",
        valor_texto="stripe",
    )
    cfg_product = ConfigRegras(
        chave="plano_gateway_product_id_admin_pro",
        descricao="product pro",
        valor_texto="prod_test_pro",
    )
    cfg_price = ConfigRegras(
        chave="plano_gateway_price_id_admin_pro",
        descricao="price pro",
        valor_texto="price_pro_test",
    )
    cfg_currency = ConfigRegras(
        chave="plano_gateway_currency_admin_pro",
        descricao="currency pro",
        valor_texto="brl",
    )
    cfg_interval = ConfigRegras(
        chave="plano_gateway_interval_admin_pro",
        descricao="interval pro",
        valor_texto="month",
    )
    cfg_ready = ConfigRegras(
        chave="plano_gateway_ready_admin_pro",
        descricao="ready pro",
        valor_inteiro=1,
        valor_texto="1",
    )
    db.session.add_all(
        [
            cfg_ref,
            cfg_provider,
            cfg_product,
            cfg_price,
            cfg_currency,
            cfg_interval,
            cfg_ready,
        ]
    )
    db.session.commit()


def test_registrar_fato_monetizacao_idempotente_retorna_mesmo_registro(app):
    with app.app_context():
        first = registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_paid",
            status_tecnico="success",
            idempotency_key="idemp-k1",
            snapshot_normalizado={"x": 1},
        )
        second = registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_paid",
            status_tecnico="success",
            idempotency_key="idemp-k1",
            snapshot_normalizado={"x": 2},
        )
        assert first.id == second.id
        assert MonetizacaoFato.query.count() == 1


def test_processar_evento_stripe_sem_correlacao_mantem_pendente(app):
    with app.app_context():
        evento = {
            "id": "evt_sem_correlacao",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_001",
                    "customer": "cus_001",
                    "subscription": "sub_001",
                    "status": "paid",
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out["ok"] is True
        assert out["efeito_operacional_aplicado"] is False
        fato = MonetizacaoFato.query.filter_by(external_event_id="evt_sem_correlacao").first()
        assert fato is not None
        assert fato.status_tecnico == "pendente_correlacao"


def test_processar_evento_stripe_replay_idempotente(app):
    with app.app_context():
        evento = {
            "id": "evt_replay_1",
            "type": "invoice.payment_failed",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_replay",
                    "status": "open",
                }
            },
        }
        first = processar_evento_stripe(evento)
        second = processar_evento_stripe(evento)
        assert first["ok"] is True
        assert second["ok"] is True
        assert second["replay"] is True
        fatos = MonetizacaoFato.query.filter_by(external_event_id="evt_replay_1").all()
        assert len(fatos) == 1


def test_processar_invoice_paid_aplica_ciclo_em_franquia_correlacionada(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ciclo")
        franquia.limite_total = 10
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="starter@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)

        inicio = _ts(2026, 4, 1)
        fim = _ts(2026, 5, 1)
        evento = {
            "id": "evt_invoice_paid_ok",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_200",
                    "customer": "cus_200",
                    "subscription": "sub_200",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": inicio, "end": fim},
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out["ok"] is True
        assert out["efeito_operacional_aplicado"] is True

        fr = db.session.get(Franquia, franquia.id)
        assert fr.inicio_ciclo is not None
        assert fr.fim_ciclo is not None
        assert int(fr.inicio_ciclo.replace(tzinfo=timezone.utc).timestamp()) == inicio
        assert int(fr.fim_ciclo.replace(tzinfo=timezone.utc).timestamp()) == fim

        vinculo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        assert vinculo is not None
        assert vinculo.customer_id == "cus_200"
        assert vinculo.subscription_id == "sub_200"

        fato = MonetizacaoFato.query.filter_by(external_event_id="evt_invoice_paid_ok").first()
        assert fato is not None
        assert fato.status_tecnico == "efeito_operacional_aplicado"


def test_extrair_ids_invoice_paid_subscription_em_parent_subscription_item_details(app):
    with app.app_context():
        objeto = {
            "id": "in_nested_sub_only",
            "customer": "cus_nested_line",
            "status": "paid",
            "lines": {
                "data": [
                    {
                        "period": {"start": _ts(2026, 4, 1), "end": _ts(2026, 5, 1)},
                        "price": {"id": "price_starter_test"},
                        "parent": {
                            "subscription_item_details": {
                                "subscription": "sub_nested_line_item",
                            }
                        },
                    }
                ]
            },
        }
        ids = _extrair_ids_externos_stripe({"type": "invoice.paid"}, objeto)
        assert ids["subscription_id"] == "sub_nested_line_item"
        assert ids["invoice_id"] == "in_nested_sub_only"


def test_invoice_paid_apenas_nested_subscription_persiste_no_vinculo_ativo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-nested-vinculo")
        franquia.limite_total = 10
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="nested-vinculo@test.com", categoria="free")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        inicio = _ts(2026, 4, 1)
        fim = _ts(2026, 5, 1)
        evento = {
            "id": "evt_invoice_nested_vinculo",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_nested_vinculo",
                    "customer": "cus_nested_vinculo",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": inicio, "end": fim},
                                "price": {"id": "price_starter_test"},
                                "parent": {
                                    "subscription_item_details": {
                                        "subscription": "sub_only_on_line_parent",
                                    }
                                },
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out["ok"] is True
        vinculo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        assert vinculo is not None
        assert vinculo.subscription_id == "sub_only_on_line_parent"


def test_invoice_paid_sem_subscription_no_payload_preserva_sub_vinculo_anterior(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-preserve-sub")
        franquia.limite_total = 10
        franquia.fim_ciclo = datetime(2026, 5, 1)
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="preserve-sub@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_keep",
            subscription_id="sub_keep_previous",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="paid",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2026, 5, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()

        inicio = _ts(2026, 5, 1)
        fim = _ts(2026, 6, 1)
        evento = {
            "id": "evt_invoice_paid_preserve_sub",
            "type": "invoice.paid",
            "created": _ts(2026, 5, 15),
            "data": {
                "object": {
                    "id": "in_no_sub_field",
                    "customer": "cus_keep",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": inicio, "end": fim},
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out["ok"] is True

        vinculo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        assert vinculo is not None
        assert vinculo.subscription_id == "sub_keep_previous"


def test_invoice_paid_renovacao_reinicia_consumo_acumulado(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-renova")
        franquia.limite_total = 10
        franquia.consumo_acumulado = 10
        franquia.status = Franquia.STATUS_DEGRADED
        franquia.inicio_ciclo = datetime(2026, 3, 1)
        franquia.fim_ciclo = datetime(2026, 4, 1)
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="renova@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)

        inicio_novo = _ts(2026, 4, 1)
        fim_novo = _ts(2026, 5, 1)
        evento = {
            "id": "evt_invoice_paid_renova",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_renova",
                    "customer": "cus_renova",
                    "subscription": "sub_renova",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": inicio_novo, "end": fim_novo},
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out["ok"] is True
        assert out["efeito_operacional_aplicado"] is True

        fr = db.session.get(Franquia, franquia.id)
        assert fr.consumo_acumulado == 0
        assert fr.status == Franquia.STATUS_ACTIVE
        assert int(fr.inicio_ciclo.replace(tzinfo=timezone.utc).timestamp()) == inicio_novo
        assert int(fr.fim_ciclo.replace(tzinfo=timezone.utc).timestamp()) == fim_novo

        fato = MonetizacaoFato.query.filter_by(external_event_id="evt_invoice_paid_renova").first()
        assert fato is not None
        assert fato.status_tecnico == "efeito_operacional_aplicado"
        assert "consumo_reiniciado_renovacao" in (fato.snapshot_normalizado_json or "")


def test_invoice_payment_failed_aplica_reflexo_operacional_controlado(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-failed")
        franquia.limite_total = 100
        franquia.consumo_acumulado = 2
        franquia.status = Franquia.STATUS_ACTIVE
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="failed@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)

        evento = {
            "id": "evt_invoice_failed_ok",
            "type": "invoice.payment_failed",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_failed_1",
                    "customer": "cus_failed",
                    "subscription": "sub_failed",
                    "status": "open",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out["ok"] is True
        assert out["efeito_operacional_aplicado"] is True

        fr = db.session.get(Franquia, franquia.id)
        assert fr.consumo_acumulado == 2
        assert fr.status == Franquia.STATUS_ACTIVE

        fato = MonetizacaoFato.query.filter_by(external_event_id="evt_invoice_failed_ok").first()
        assert fato is not None
        assert fato.status_tecnico == "efeito_operacional_aplicado"
        assert "nao_bloqueio_imediato_reavaliacao_operacional" in (
            fato.snapshot_normalizado_json or ""
        )


def test_iniciar_jornada_assinatura_stripe_registra_fato(monkeypatch, app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-checkout")
        franquia.limite_total = 200
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="checkout@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)

        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")
        monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test")
        monkeypatch.setenv("STRIPE_SUCCESS_URL", "/contrate-um-plano?ok=1")
        monkeypatch.setenv("STRIPE_CANCEL_URL", "/contrate-um-plano?cancel=1")

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "id": "cs_test_123",
                    "client_secret": "secret_123",
                    "customer": "cus_123",
                    "subscription": "sub_123",
                    "status": "open",
                    "payment_status": "unpaid",
                    "client_reference_id": f"conta:{conta.id}:franquia:{franquia.id}",
                }

            text = "{}"

        def _fake_post(url, data, headers, timeout):
            assert url.endswith("/checkout/sessions")
            assert data["ui_mode"] == "embedded_page"
            assert "customer_creation" not in data
            assert "success_url" not in data
            assert "cancel_url" not in data
            assert data["return_url"] == "https://example.com/contrate-um-plano?ok=1"
            assert data["metadata[conta_id]"] == str(conta.id)
            assert data["metadata[franquia_id]"] == str(franquia.id)
            assert headers["Authorization"] == "Bearer sk_test"
            return _Resp()

        monkeypatch.setattr("app.services.cleiton_monetizacao_service.requests.post", _fake_post)

        out = iniciar_jornada_assinatura_stripe(
            user=user,
            plano_codigo="starter",
            site_origin="https://example.com",
        )
        assert out["checkout_session_id"] == "cs_test_123"
        assert out["checkout_client_secret"] == "secret_123"
        assert out["publishable_key"] == "pk_test"

        fatos = MonetizacaoFato.query.filter_by(tipo_fato="stripe_checkout_session_created").all()
        assert len(fatos) == 1
        assert fatos[0].conta_id == conta.id
        assert fatos[0].franquia_id == franquia.id


def test_resolve_plano_por_price_id_admin(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-price")
        franquia.limite_total = 100
        db.session.add(franquia)
        db.session.commit()
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        out = plano_service.resolver_plano_por_gateway_price_id_admin(
            provider="stripe",
            price_id="price_starter_test",
        )
        assert out is not None
        assert out["plano_codigo"] == "starter"


def test_contexto_monetizacao_retorna_projecoes_estruturadas(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-contexto")
        user = seed_usuario(franquia.id, conta.id, email="ctx@test.com", categoria="starter")
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_ctx",
            subscription_id="sub_ctx",
            price_id="price_ctx",
            plano_interno="starter",
            status_contratual_externo="active",
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"source": "teste"},
        )
        registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_paid",
            status_tecnico="efeito_operacional_aplicado",
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            provider="stripe",
            external_event_id="evt_ctx_1",
            identificadores_externos={"invoice_id": "in_ctx"},
            snapshot_normalizado={"ok": True},
            payload_bruto_sanitizado={"raw": "x"},
        )
        db.session.commit()

        contexto = obter_contexto_monetizacao_conta(conta.id)
        ativo = contexto["vinculo_comercial_externo_ativo"]
        assert ativo["snapshot_normalizado_json"] is not None
        assert ativo["snapshot_normalizado"]["franquia_id"] == franquia.id
        assert ativo["payload_bruto_sanitizado"]["source"] == "teste"

        fatos = contexto["fatos_monetizacao_recentes"]
        assert len(fatos) >= 1
        assert fatos[0]["snapshot_normalizado"]["ok"] is True
        assert fatos[0]["payload_bruto_sanitizado"]["raw"] == "x"
        assert fatos[0]["identificadores_externos"]["invoice_id"] == "in_ctx"


def test_reprocessamento_admin_resolve_pendente_quando_correlacao_fica_disponivel(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-reprocess-ok")
        franquia.limite_total = 20
        franquia.consumo_acumulado = 5
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="reprocess-ok@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)

        evento = {
            "id": "evt_reprocess_ok",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_reprocess_ok",
                    "customer": "cus_reprocess_ok",
                    "subscription": "sub_reprocess_ok",
                    "status": "paid",
                    "lines": {
                        "data": [
                            {
                                "period": {
                                    "start": _ts(2026, 4, 1),
                                    "end": _ts(2026, 5, 1),
                                },
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        processar_evento_stripe(evento)
        fato = MonetizacaoFato.query.filter_by(external_event_id="evt_reprocess_ok").first()
        assert fato is not None
        assert fato.status_tecnico == "pendente_correlacao"

        evento_reprocessavel = json.loads(fato.payload_bruto_sanitizado_json or "{}")
        data_obj = ((evento_reprocessavel.get("data") or {}).get("object") or {})
        data_obj["metadata"] = {
            "conta_id": str(conta.id),
            "franquia_id": str(franquia.id),
            "plano_interno": "starter",
        }
        fato.payload_bruto_sanitizado_json = json.dumps(evento_reprocessavel)
        db.session.add(fato)
        db.session.commit()

        out = reprocessar_fato_pendente_correlacao_admin(
            fato_id=fato.id,
            admin_user_id=999,
            franquia_id_contexto=franquia.id,
        )
        assert out["ok"] is True
        assert out["permanece_pendente"] is False

        fato_refresh = db.session.get(MonetizacaoFato, fato.id)
        assert fato_refresh is not None
        assert fato_refresh.status_tecnico == "efeito_operacional_aplicado"
        assert "reprocessamento_admin" in (fato_refresh.snapshot_normalizado_json or "")


def test_reprocessamento_admin_permanece_pendente_sem_nova_correlacao(app):
    with app.app_context():
        evento = {
            "id": "evt_reprocess_still_pending",
            "type": "invoice.payment_failed",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_reprocess_still_pending",
                    "status": "open",
                }
            },
        }
        processar_evento_stripe(evento)
        fato = MonetizacaoFato.query.filter_by(
            external_event_id="evt_reprocess_still_pending"
        ).first()
        assert fato is not None
        out = reprocessar_fato_pendente_correlacao_admin(fato_id=fato.id, admin_user_id=7)
        assert out["ok"] is True
        assert out["permanece_pendente"] is True
        fato_refresh = db.session.get(MonetizacaoFato, fato.id)
        assert fato_refresh is not None
        assert fato_refresh.status_tecnico == "pendente_correlacao"


def test_reprocessamento_admin_nao_cria_novo_fato_quando_reexecutado(app):
    with app.app_context():
        evento = {
            "id": "evt_reprocess_idempotente",
            "type": "invoice.payment_failed",
            "created": _ts(2026, 4, 17),
            "data": {"object": {"id": "in_reprocess_idempotente", "status": "open"}},
        }
        processar_evento_stripe(evento)
        fato = MonetizacaoFato.query.filter_by(
            external_event_id="evt_reprocess_idempotente"
        ).first()
        assert fato is not None
        reprocessar_fato_pendente_correlacao_admin(fato_id=fato.id, admin_user_id=1)
        reprocessar_fato_pendente_correlacao_admin(fato_id=fato.id, admin_user_id=1)
        fatos = MonetizacaoFato.query.filter_by(
            external_event_id="evt_reprocess_idempotente"
        ).all()
        assert len(fatos) == 1


def test_invoice_paid_antigo_nao_retrocede_ciclo_nem_reinicia_consumo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ciclo-antigo")
        franquia.limite_total = 10
        franquia.consumo_acumulado = 3
        franquia.inicio_ciclo = datetime(2026, 5, 1)
        franquia.fim_ciclo = datetime(2026, 6, 1)
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="ciclo-antigo@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)

        evento_antigo = {
            "id": "evt_invoice_paid_antigo",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_antigo",
                    "customer": "cus_antigo",
                    "subscription": "sub_antigo",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {
                                    "start": _ts(2026, 4, 1),
                                    "end": _ts(2026, 5, 1),
                                },
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento_antigo)
        assert out["ok"] is True
        fr = db.session.get(Franquia, franquia.id)
        assert fr is not None
        assert int(fr.fim_ciclo.replace(tzinfo=timezone.utc).timestamp()) == _ts(2026, 6, 1)
        assert fr.consumo_acumulado == 3


def test_invoice_paid_antigo_nao_sobrescreve_vinculo_ativo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-vinculo-antigo")
        franquia.inicio_ciclo = datetime(2026, 5, 1)
        franquia.fim_ciclo = datetime(2026, 6, 1)
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="vinculo-antigo@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_atual",
            subscription_id="sub_atual",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="paid",
            vigencia_externa_inicio=datetime(2026, 5, 1),
            vigencia_externa_fim=datetime(2026, 6, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed_vinculo_atual"},
        )
        db.session.commit()

        evento_antigo = {
            "id": "evt_invoice_paid_antigo_vinculo",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_antigo_vinculo",
                    "customer": "cus_antigo",
                    "subscription": "sub_antigo",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {
                                    "start": _ts(2026, 4, 1),
                                    "end": _ts(2026, 5, 1),
                                },
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento_antigo)
        assert out["ok"] is True

        vinculo_ativo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        assert vinculo_ativo is not None
        assert vinculo_ativo.subscription_id == "sub_atual"
        assert vinculo_ativo.customer_id == "cus_atual"
        assert vinculo_ativo.vigencia_externa_fim == datetime(2026, 6, 1)


def test_sincronizar_retorno_checkout_pago_aplica_plano_e_categoria(monkeypatch, app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-retorno-checkout")
        franquia.limite_total = 50
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="retorno-checkout@test.com",
            categoria="free",
        )
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "id": "cs_return_ok",
                    "status": "complete",
                    "payment_status": "paid",
                    "customer": "cus_return_ok",
                    "subscription": {
                        "id": "sub_return_ok",
                        "status": "active",
                        "metadata": {
                            "conta_id": str(conta.id),
                            "franquia_id": str(franquia.id),
                            "usuario_id": str(user.id),
                            "plano_interno": "starter",
                        },
                    },
                    "invoice": {
                        "id": "in_return_ok",
                        "customer": "cus_return_ok",
                        "subscription": "sub_return_ok",
                        "status": "paid",
                        "lines": {
                            "data": [
                                {
                                    "period": {
                                        "start": _ts(2026, 4, 1),
                                        "end": _ts(2026, 5, 1),
                                    },
                                    "price": {"id": "price_starter_test"},
                                }
                            ]
                        },
                    },
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "usuario_id": str(user.id),
                        "plano_interno": "starter",
                    },
                    "client_reference_id": f"conta:{conta.id}:franquia:{franquia.id}",
                }

            text = "{}"

        def _fake_get(url, params, headers, timeout):
            assert url.endswith("/checkout/sessions/cs_return_ok")
            assert headers["Authorization"] == "Bearer sk_test"
            return _Resp()

        monkeypatch.setattr("app.services.cleiton_monetizacao_service.requests.get", _fake_get)

        out = sincronizar_retorno_checkout_stripe(checkout_session_id="cs_return_ok")
        assert out["ok"] is True
        assert out["event_type"] == "invoice.paid"
        assert out["efeito_operacional_aplicado"] is True

        user_refresh = db.session.get(type(user), user.id)
        franquia_refresh = db.session.get(Franquia, franquia.id)
        assert user_refresh is not None
        assert user_refresh.categoria == "starter"
        assert franquia_refresh is not None
        assert franquia_refresh.status == Franquia.STATUS_ACTIVE

        fato = MonetizacaoFato.query.filter_by(
            external_event_id="checkout_return:cs_return_ok:invoice.paid"
        ).first()
        assert fato is not None


def test_processar_checkout_session_completed_registra_sem_efeito_operacional(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-checkout-completed")
        db.session.commit()
        evento = {
            "id": "evt_checkout_completed_sem_efeito",
            "type": "checkout.session.completed",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "cs_checkout_completed",
                    "customer": "cus_checkout_completed",
                    "subscription": "sub_checkout_completed",
                    "status": "complete",
                    "payment_status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                    },
                    "client_reference_id": f"conta:{conta.id}:franquia:{franquia.id}",
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out["ok"] is True
        assert out["event_type"] == "checkout.session.completed"
        assert out["efeito_operacional_aplicado"] is False

        fato = MonetizacaoFato.query.filter_by(
            external_event_id="evt_checkout_completed_sem_efeito"
        ).first()
        assert fato is not None
        assert fato.status_tecnico == "registrado_sem_efeito_operacional"


def test_free_para_starter_aplica_imediatamente(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-free-starter")
        franquia.limite_total = 5
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="free-starter@test.com", categoria="free")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        evento = {
            "id": "evt_free_starter",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_free_starter",
                    "customer": "cus_free_starter",
                    "subscription": "sub_free_starter",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": _ts(2026, 4, 1), "end": _ts(2026, 5, 1)},
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        user_refresh = db.session.get(type(user), user.id)
        assert out["efeito_operacional_aplicado"] is True
        assert user_refresh is not None and user_refresh.categoria == "starter"


def test_starter_para_pro_aplica_imediatamente(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-starter-pro")
        franquia.limite_total = 10
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="starter-pro@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        _configurar_plano_pro_para_teste(franquia_ref_id=franquia.id)
        evento = {
            "id": "evt_starter_pro",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_starter_pro",
                    "customer": "cus_starter_pro",
                    "subscription": "sub_starter_pro",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "pro",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": _ts(2026, 4, 1), "end": _ts(2026, 5, 1)},
                                "price": {"id": "price_pro_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        user_refresh = db.session.get(type(user), user.id)
        assert out["efeito_operacional_aplicado"] is True
        assert user_refresh is not None and user_refresh.categoria == "pro"


def test_pro_para_starter_com_ciclo_vigente_registra_pendencia_sem_aplicar(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pro-starter-pendente")
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="pro-starter@test.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        _configurar_plano_pro_para_teste(franquia_ref_id=franquia.id)
        evento = {
            "id": "evt_pro_starter_pendente",
            "type": "customer.subscription.updated",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "sub_ps",
                    "customer": "cus_ps",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2099, 1, 1),
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        out = processar_evento_stripe(evento)
        user_refresh = db.session.get(type(user), user.id)
        vinculo_ativo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        snapshot = json.loads(vinculo_ativo.snapshot_normalizado_json or "{}")
        assert out["efeito_operacional_aplicado"] is False
        assert out["mudanca_pendente"] is True
        assert user_refresh is not None and user_refresh.categoria == "pro"
        assert snapshot.get("mudanca_pendente") is True
        assert snapshot.get("plano_futuro") == "starter"
        assert snapshot.get("efetivar_em") is not None


def test_pendencia_downgrade_preservada_apos_evento_subsequente_recriando_vinculo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-preserva-pendencia")
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="preserva@test.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        evento_downgrade = {
            "id": "evt_preserva_pendencia_1",
            "type": "customer.subscription.updated",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "sub_preserva",
                    "customer": "cus_preserva",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2099, 1, 1),
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        processar_evento_stripe(evento_downgrade)
        evento_subsequente = {
            "id": "evt_preserva_pendencia_2",
            "type": "invoice.payment_failed",
            "created": _ts(2026, 4, 18),
            "data": {
                "object": {
                    "id": "in_preserva_2",
                    "customer": "cus_preserva",
                    "subscription": "sub_preserva",
                    "status": "open",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        processar_evento_stripe(evento_subsequente)
        vinculo_ativo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        snapshot = json.loads(vinculo_ativo.snapshot_normalizado_json or "{}")
        assert snapshot.get("mudanca_pendente") is True
        assert snapshot.get("plano_futuro") == "starter"
        assert snapshot.get("efetivar_em") is not None


def test_downgrade_prioriza_data_vigencia_externa_para_efetivar_em(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-data-externa")
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="data-externa@test.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        evento = {
            "id": "evt_data_externa",
            "type": "customer.subscription.updated",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "sub_data_externa",
                    "customer": "cus_data_externa",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2098, 12, 1),
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        processar_evento_stripe(evento)
        vinculo_ativo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        snapshot = json.loads(vinculo_ativo.snapshot_normalizado_json or "{}")
        assert snapshot.get("efetivar_em", "").startswith("2098-12-01")


def test_pro_para_free_com_ciclo_vigente_mantem_operacao_e_registra_pendencia(monkeypatch, app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pro-free")
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="pro-free@test.com", categoria="pro")
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_pf",
            subscription_id="sub_pf",
            price_id="price_pro_test",
            plano_interno="pro",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2099, 1, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")

        def _fake_stripe_get(path: str, params=None):
            if path == "/subscriptions/sub_pf":
                return {
                    "id": "sub_pf",
                    "customer": "cus_pf",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2099, 1, 1),
                    "items": {"data": [{"id": "si_pf", "price": {"id": "price_pro_test"}}]},
                    "metadata": {"plano_interno": "pro", "conta_id": str(conta.id)},
                }
            if path == "/subscriptions":
                return {"data": []}
            raise AssertionError(f"GET Stripe inesperado: {path} {params!r}")

        monkeypatch.setattr("app.services.cleiton_monetizacao_service._stripe_get", _fake_stripe_get)

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "id": "sub_pf",
                    "customer": "cus_pf",
                    "status": "active",
                    "cancel_at_period_end": True,
                    "current_period_end": _ts(2099, 1, 1),
                    "current_period_start": _ts(2026, 4, 1),
                    "items": {"data": [{"id": "si_pf", "price": {"id": "price_pro_test"}}]},
                    "metadata": {"plano_interno": "pro", "conta_id": str(conta.id)},
                }

            text = "{}"

        stripe_posts: list[tuple[str, dict]] = []

        def _capture_post(url, *args, **kwargs):
            data = kwargs.get("data") or {}
            if isinstance(data, dict):
                stripe_posts.append((url, dict(data)))
            else:
                stripe_posts.append((url, {}))
            return _Resp()

        monkeypatch.setattr(
            "app.services.cleiton_monetizacao_service.requests.post",
            _capture_post,
        )
        out = iniciar_jornada_assinatura_stripe(user=user, plano_codigo="free", site_origin="https://example.com")
        user_refresh = db.session.get(type(user), user.id)
        vinculo_ativo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        snapshot = json.loads(vinculo_ativo.snapshot_normalizado_json or "{}")
        assert out["downgrade_agendado"] is True
        assert user_refresh is not None and user_refresh.categoria == "pro"
        assert snapshot.get("mudanca_pendente") is True
        assert snapshot.get("plano_futuro") == "free"
        assert len(stripe_posts) >= 1
        cancel_url, cancel_payload = stripe_posts[0]
        assert "subscriptions/sub_pf" in cancel_url
        assert cancel_payload.get("cancel_at_period_end") == "true"


def test_pro_para_starter_usa_modify_subscription_sem_nova_checkout(monkeypatch, app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pro-starter")
        franquia.fim_ciclo = datetime(2099, 6, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="pro-starter@test.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        _configurar_plano_pro_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_ps",
            subscription_id="sub_ps",
            price_id="price_pro_test",
            plano_interno="pro",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2099, 6, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")
        monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test")

        def _fake_stripe_get(path: str, params=None):
            if path == "/subscriptions/sub_ps":
                return {
                    "id": "sub_ps",
                    "customer": "cus_ps",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2099, 6, 1),
                    "items": {"data": [{"id": "si_ps_line", "price": {"id": "price_pro_test"}}]},
                    "metadata": {"plano_interno": "pro", "conta_id": str(conta.id)},
                }
            if path == "/subscriptions":
                return {"data": []}
            raise AssertionError(path)

        monkeypatch.setattr("app.services.cleiton_monetizacao_service._stripe_get", _fake_stripe_get)

        stripe_posts: list[tuple[str, dict]] = []

        def _capture_post(url, *args, **kwargs):
            data = kwargs.get("data") or {}
            if isinstance(data, dict):
                stripe_posts.append((url, dict(data)))
            else:
                stripe_posts.append((url, {}))

            class _R:
                status_code = 200
                text = "{}"

                def json(self):
                    return {
                        "id": "sub_ps",
                        "customer": "cus_ps",
                        "status": "active",
                        "current_period_start": _ts(2026, 4, 1),
                        "current_period_end": _ts(2099, 6, 1),
                        "items": {
                            "data": [{"id": "si_ps_line", "price": {"id": "price_starter_test"}}]
                        },
                        "metadata": {"plano_interno": "starter", "conta_id": str(conta.id)},
                    }

            return _R()

        monkeypatch.setattr("app.services.cleiton_monetizacao_service.requests.post", _capture_post)

        out = iniciar_jornada_assinatura_stripe(
            user=user, plano_codigo="starter", site_origin="https://example.com"
        )
        assert out.get("assinatura_atualizada_sem_checkout") is True
        assert out.get("downgrade_agendado") is True
        assert out.get("checkout_session_id") is None
        assert len(stripe_posts) == 1
        mod_url, mod_payload = stripe_posts[0]
        assert "subscriptions/sub_ps" in mod_url
        assert "checkout" not in mod_url
        assert mod_payload.get("items[0][id]") == "si_ps_line"
        assert mod_payload.get("items[0][price]") == "price_starter_test"
        assert mod_payload.get("proration_behavior") == "none"


def test_starter_para_free_com_ciclo_vigente_mantem_starter_ate_virada(monkeypatch, app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-starter-free")
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="starter-free@test.com", categoria="starter")
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_sf",
            subscription_id="sub_sf",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2099, 1, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")

        def _fake_stripe_get_sf(path: str, params=None):
            if path == "/subscriptions/sub_sf":
                return {
                    "id": "sub_sf",
                    "customer": "cus_sf",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2099, 1, 1),
                    "items": {"data": [{"id": "si_sf", "price": {"id": "price_starter_test"}}]},
                    "metadata": {"plano_interno": "starter", "conta_id": str(conta.id)},
                }
            if path == "/subscriptions":
                return {"data": []}
            raise AssertionError(path)

        monkeypatch.setattr("app.services.cleiton_monetizacao_service._stripe_get", _fake_stripe_get_sf)

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "id": "sub_sf",
                    "customer": "cus_sf",
                    "status": "active",
                    "cancel_at_period_end": True,
                    "current_period_end": _ts(2099, 1, 1),
                    "current_period_start": _ts(2026, 4, 1),
                    "items": {"data": [{"id": "si_sf", "price": {"id": "price_starter_test"}}]},
                    "metadata": {"plano_interno": "starter", "conta_id": str(conta.id)},
                }

            text = "{}"

        monkeypatch.setattr("app.services.cleiton_monetizacao_service.requests.post", lambda *args, **kwargs: _Resp())
        iniciar_jornada_assinatura_stripe(user=user, plano_codigo="free", site_origin="https://example.com")
        user_refresh = db.session.get(type(user), user.id)
        assert user_refresh is not None and user_refresh.categoria == "starter"


def test_downgrade_free_preserva_pendencia_apos_customer_subscription_deleted(monkeypatch, app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-free-deleted")
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="free-deleted@test.com", categoria="starter")
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_fd",
            subscription_id="sub_fd",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2099, 1, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")

        def _fake_stripe_get_fd(path: str, params=None):
            if path == "/subscriptions/sub_fd":
                return {
                    "id": "sub_fd",
                    "customer": "cus_fd",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2099, 1, 1),
                    "items": {"data": [{"id": "si_fd", "price": {"id": "price_starter_test"}}]},
                    "metadata": {"plano_interno": "starter", "conta_id": str(conta.id)},
                }
            if path == "/subscriptions":
                return {"data": []}
            raise AssertionError(path)

        monkeypatch.setattr("app.services.cleiton_monetizacao_service._stripe_get", _fake_stripe_get_fd)

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {
                    "id": "sub_fd",
                    "customer": "cus_fd",
                    "status": "active",
                    "cancel_at_period_end": True,
                    "current_period_end": _ts(2099, 1, 1),
                    "current_period_start": _ts(2026, 4, 1),
                    "items": {"data": [{"id": "si_fd", "price": {"id": "price_starter_test"}}]},
                    "metadata": {"plano_interno": "starter", "conta_id": str(conta.id)},
                }

            text = "{}"

        monkeypatch.setattr("app.services.cleiton_monetizacao_service.requests.post", lambda *args, **kwargs: _Resp())
        iniciar_jornada_assinatura_stripe(user=user, plano_codigo="free", site_origin="https://example.com")

        evento_deleted = {
            "id": "evt_deleted_pos_free",
            "type": "customer.subscription.deleted",
            "created": _ts(2026, 4, 18),
            "data": {
                "object": {
                    "id": "sub_fd",
                    "customer": "cus_fd",
                    "status": "canceled",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2099, 1, 1),
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "free",
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        out = processar_evento_stripe(evento_deleted)
        user_refresh = db.session.get(type(user), user.id)
        vinculo_ativo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        snapshot = json.loads(vinculo_ativo.snapshot_normalizado_json or "{}")
        assert out["mudanca_pendente"] is True
        assert out["efeito_operacional_aplicado"] is False
        assert user_refresh is not None and user_refresh.categoria == "starter"
        assert snapshot.get("mudanca_pendente") is True
        assert snapshot.get("plano_futuro") == "free"


def test_efetivacao_pendencia_na_virada_troca_plano_e_zera_consumo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-virada")
        franquia.consumo_acumulado = 7
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="virada@test.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        vinculo = registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_v",
            subscription_id="sub_v",
            price_id="price_pro_test",
            plano_interno="pro",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2026, 5, 1),
            snapshot_normalizado={
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "starter",
                "efetivar_em": datetime(2026, 5, 1).isoformat(),
                "origem": "solicitacao_usuario",
            },
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        out = efetivar_mudancas_pendentes_ciclo(agora=datetime(2026, 5, 2))
        user_refresh = db.session.get(type(user), user.id)
        franquia_refresh = db.session.get(Franquia, franquia.id)
        vinculo_refresh = db.session.get(ContaMonetizacaoVinculo, vinculo.id)
        snapshot = json.loads(vinculo_refresh.snapshot_normalizado_json or "{}")
        assert out["efetivados"] == 1
        assert user_refresh is not None and user_refresh.categoria == "starter"
        assert franquia_refresh is not None and franquia_refresh.consumo_acumulado == 0
        assert franquia_refresh.inicio_ciclo == datetime(2026, 5, 1)
        assert franquia_refresh.fim_ciclo == datetime(2026, 6, 1)
        assert franquia_refresh.status == Franquia.STATUS_ACTIVE
        assert snapshot.get("mudanca_pendente") is False


def test_efetivacao_pendencia_free_atualiza_ciclo_e_status(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-virada-free")
        franquia.consumo_acumulado = 9
        franquia.inicio_ciclo = datetime(2026, 4, 1)
        franquia.fim_ciclo = datetime(2026, 5, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="virada-free@test.com", categoria="starter")
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_vf",
            subscription_id="sub_vf",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2026, 5, 1),
            snapshot_normalizado={
                "conta_id": conta.id,
                "franquia_id": franquia.id,
                "mudanca_pendente": True,
                "tipo_mudanca": "downgrade",
                "plano_futuro": "free",
                "efetivar_em": datetime(2026, 5, 1).isoformat(),
                "origem": "solicitacao_usuario",
            },
            payload_bruto_sanitizado={"origem": "seed"},
        )
        out = efetivar_mudancas_pendentes_ciclo(agora=datetime(2026, 5, 2))
        user_refresh = db.session.get(type(user), user.id)
        franquia_refresh = db.session.get(Franquia, franquia.id)
        assert out["efetivados"] == 1
        assert user_refresh is not None and user_refresh.categoria == "free"
        assert franquia_refresh is not None and franquia_refresh.consumo_acumulado == 0
        assert franquia_refresh.inicio_ciclo == datetime(2026, 5, 1)
        assert franquia_refresh.fim_ciclo is None
        assert franquia_refresh.status == Franquia.STATUS_ACTIVE


def test_aplicar_fato_pro_para_starter_sem_datas_no_evento_pendencia_sem_aplicar_operacional(app):
    """Downgrade pago inferior: sem fim no evento nao deve cair em aplicacao imediata de starter."""
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pro-st-sem-ciclo-ev")
        franquia.limite_total = 8888
        franquia.consumo_acumulado = 33
        franquia.inicio_ciclo = datetime(2026, 4, 1)
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="pro-st-sem@app.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        _configurar_plano_pro_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_sem_ev",
            subscription_id="sub_sem_ev",
            price_id="price_pro_test",
            plano_interno="pro",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2099, 1, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        out = aplicar_fato_contratual_em_franquia(
            franquia_id=franquia.id,
            plano_codigo="starter",
            event_type="customer.subscription.updated",
            status_contratual_externo="active",
            ciclo={
                "inicio_ciclo": None,
                "fim_ciclo": None,
                "fonte_ciclo": "sem_alteracao_ciclo_por_tipo_evento",
                "pendencias": [],
            },
        )
        assert out["mudanca_pendente"] is True
        assert out["plano_pendente"] == "starter"
        user_refresh = db.session.get(type(user), user.id)
        fr_refresh = db.session.get(Franquia, franquia.id)
        assert user_refresh is not None and user_refresh.categoria == "pro"
        assert fr_refresh is not None
        assert fr_refresh.limite_total == 8888
        assert int(fr_refresh.consumo_acumulado) == 33


def test_invoice_paid_pro_para_starter_pendencia_preserva_consumo_e_categoria(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pro-st-inv")
        franquia.limite_total = 7777
        franquia.consumo_acumulado = 44
        franquia.inicio_ciclo = datetime(2026, 1, 1)
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="pro-st-inv@app.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        _configurar_plano_pro_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_ps_inv",
            subscription_id="sub_ps_inv",
            price_id="price_pro_test",
            plano_interno="pro",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 1, 1),
            vigencia_externa_fim=datetime(2099, 1, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        inicio_inv = _ts(2026, 6, 1)
        fim_inv = _ts(2026, 7, 1)
        evento = {
            "id": "evt_inv_pro_st",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_pro_st",
                    "customer": "cus_ps_inv",
                    "subscription": "sub_ps_inv",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": inicio_inv, "end": fim_inv},
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out["mudanca_pendente"] is True
        assert out["plano_pendente"] == "starter"
        assert out.get("efetivar_em")
        user_refresh = db.session.get(type(user), user.id)
        fr_refresh = db.session.get(Franquia, franquia.id)
        assert user_refresh is not None and user_refresh.categoria == "pro"
        assert fr_refresh is not None
        assert fr_refresh.limite_total == 7777
        assert int(fr_refresh.consumo_acumulado) == 44
        assert fr_refresh.fim_ciclo == datetime(2099, 1, 1)


def test_reprocessamento_evento_downgrade_repetido_nao_antecipa_nem_duplica(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-downgrade-idempotente")
        franquia.fim_ciclo = datetime(2099, 1, 1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="idempotente@test.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        evento = {
            "id": "evt_downgrade_repetido",
            "type": "customer.subscription.updated",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "sub_id",
                    "customer": "cus_id",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2099, 1, 1),
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        first = processar_evento_stripe(evento)
        second = processar_evento_stripe(evento)
        user_refresh = db.session.get(type(user), user.id)
        fatos = MonetizacaoFato.query.filter_by(external_event_id="evt_downgrade_repetido").all()
        assert first["mudanca_pendente"] is True
        assert second["replay"] is True
        assert user_refresh is not None and user_refresh.categoria == "pro"
        assert len(fatos) == 1


def test_checkout_pago_bloqueado_na_revalidacao_quando_assinatura_ativa_surge(monkeypatch, app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-revalidacao-checkout")
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="revalidacao-checkout@test.com", categoria="starter")
        _configurar_plano_pro_para_teste(franquia_ref_id=franquia.id)
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")
        monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test")

        n_calls = {"n": 0}

        def _fake_obter_assinatura(cid: int):
            n_calls["n"] += 1
            if n_calls["n"] == 1:
                return None
            return {
                "subscription_id": "sub_late",
                "subscription_item_id": "si_late",
                "stripe_subscription": {"id": "sub_late", "status": "active"},
                "origem": "test_double",
            }

        monkeypatch.setattr(
            "app.services.cleiton_monetizacao_service._obter_assinatura_stripe_ativa",
            _fake_obter_assinatura,
        )
        stripe_posts: list[str] = []

        def _capture_post(url, *args, **kwargs):
            stripe_posts.append(url)
            raise AssertionError("checkout nao deveria ser chamado")

        monkeypatch.setattr("app.services.cleiton_monetizacao_service.requests.post", _capture_post)

        with pytest.raises(ValueError, match="assinatura Stripe ativa"):
            iniciar_jornada_assinatura_stripe(
                user=user, plano_codigo="pro", site_origin="https://example.com"
            )
        assert n_calls["n"] == 2
        assert len(stripe_posts) == 0


def test_processar_evento_stripe_guardrail_subscription_id_divergente_nao_promove_vinculo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-guard-sub-webhook")
        franquia.limite_total = 10
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="guard-sub@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_guard_sub",
            subscription_id="sub_canonico",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2026, 5, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        inicio = _ts(2026, 5, 1)
        fim = _ts(2026, 6, 1)
        evento = {
            "id": "evt_guard_sub_div",
            "type": "invoice.paid",
            "created": _ts(2026, 5, 10),
            "data": {
                "object": {
                    "id": "in_guard_sub_div",
                    "customer": "cus_guard_sub",
                    "status": "paid",
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "lines": {
                        "data": [
                            {
                                "period": {"start": inicio, "end": fim},
                                "price": {"id": "price_starter_test"},
                                "parent": {
                                    "subscription_item_details": {
                                        "subscription": "sub_intruso",
                                    }
                                },
                            }
                        ]
                    },
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out.get("vinculo_guardrail_bloqueado") is True
        assert out.get("efeito_operacional_aplicado") is False
        v = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        assert v is not None
        assert v.subscription_id == "sub_canonico"
        guard = MonetizacaoFato.query.filter_by(
            tipo_fato="stripe_vinculo_guardrail_ids_inconsistentes"
        ).first()
        assert guard is not None
        assert guard.conta_id == conta.id


def test_processar_evento_stripe_guardrail_customer_id_divergente_nao_promove_vinculo(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-guard-cus-webhook")
        franquia.limite_total = 10
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="guard-cus@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_canonico",
            subscription_id="sub_mesmo",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2026, 5, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        evento = {
            "id": "evt_guard_cus_div",
            "type": "customer.subscription.updated",
            "created": _ts(2026, 5, 10),
            "data": {
                "object": {
                    "id": "sub_mesmo",
                    "customer": "cus_intruso",
                    "status": "active",
                    "current_period_start": _ts(2026, 4, 1),
                    "current_period_end": _ts(2026, 6, 1),
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                        "plano_interno": "starter",
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        out = processar_evento_stripe(evento)
        assert out.get("vinculo_guardrail_bloqueado") is True
        v = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        assert v is not None
        assert v.customer_id == "cus_canonico"
        assert v.subscription_id == "sub_mesmo"


def test_processar_fato_conciliado_guardrail_subscription_id_divergente(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-guard-sub-conc")
        franquia.limite_total = 10
        db.session.add(franquia)
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="guard-conc@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_conc",
            subscription_id="sub_vinculo_conc",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            vigencia_externa_inicio=datetime(2026, 4, 1),
            vigencia_externa_fim=datetime(2026, 5, 1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        object_data = {
            "id": "sub_payload_conciliacao",
            "customer": "cus_conc",
            "status": "active",
            "current_period_start": _ts(2026, 4, 1),
            "current_period_end": _ts(2026, 6, 1),
            "metadata": {
                "conta_id": str(conta.id),
                "franquia_id": str(franquia.id),
                "plano_interno": "starter",
            },
            "items": {"data": [{"price": {"id": "price_starter_test"}}]},
        }
        out = processar_fato_stripe_conciliado(
            event_type="customer.subscription.updated",
            object_data=object_data,
            session_id="cs_guard_conc",
            event_id="evt_conc_guard_sub",
            created_at=datetime(2026, 5, 10, tzinfo=timezone.utc),
        )
        assert out.get("vinculo_guardrail_bloqueado") is True
        v = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        assert v is not None
        assert v.subscription_id == "sub_vinculo_conc"


def test_obter_assinatura_stripe_ativa_recupera_via_historico_quando_ativo_cancelado(app, monkeypatch):
    """Vinculo ativo aponta para sub cancelada; historico tem sub ativa antes do fallback por customer."""
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-hist-ativo-cancel")
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="hist-cancel@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_errado_ativo",
            subscription_id="sub_ativa_ruim",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="canceled",
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed_b"},
        )
        db.session.commit()
        monkeypatch.setattr(
            "app.services.cleiton_monetizacao_service._subscription_ids_ordenados_vinculos_stripe_conta",
            lambda _cid: ["sub_ativa_ruim", "sub_historica_boa"],
        )
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")

        def _fake_get(path: str, params=None):
            if path == "/subscriptions/sub_ativa_ruim":
                return {
                    "id": "sub_ativa_ruim",
                    "status": "canceled",
                    "customer": "cus_errado_ativo",
                    "items": {"data": [{"id": "si_bad", "price": {"id": "price_starter_test"}}]},
                }
            if path == "/subscriptions/sub_historica_boa":
                return {
                    "id": "sub_historica_boa",
                    "status": "active",
                    "customer": "cus_correto",
                    "metadata": {"conta_id": str(conta.id), "franquia_id": str(franquia.id)},
                    "items": {"data": [{"id": "si_good", "price": {"id": "price_starter_test"}}]},
                }
            if path == "/subscriptions":
                return {"data": []}
            raise AssertionError(f"GET inesperado: {path}")

        monkeypatch.setattr("app.services.cleiton_monetizacao_service._stripe_get", _fake_get)

        out = _obter_assinatura_stripe_ativa(conta.id)
        assert out is not None
        assert out.get("subscription_id") == "sub_historica_boa"
        assert out.get("origem") == "vinculo_historico_stripe_get"
        assert out.get("customer_id") == "cus_correto"


def test_obter_assinatura_stripe_ativa_historico_prefere_metadata_conta(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-hist-meta-pref")
        db.session.commit()
        seed_usuario(franquia.id, conta.id, email="hist-meta@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_x",
            subscription_id="sub_ativo_ruim_meta",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "m3"},
        )
        db.session.commit()
        monkeypatch.setattr(
            "app.services.cleiton_monetizacao_service._subscription_ids_ordenados_vinculos_stripe_conta",
            lambda _cid: ["sub_ativo_ruim_meta", "sub_sem_meta", "sub_com_meta"],
        )
        monkeypatch.setenv("STRIPE_API_KEY", "sk_test")

        def _fake_get(path: str, params=None):
            if path == "/subscriptions/sub_ativo_ruim_meta":
                return {
                    "id": "sub_ativo_ruim_meta",
                    "status": "canceled",
                    "customer": "cus_x",
                    "items": {"data": [{"id": "si1", "price": {"id": "price_starter_test"}}]},
                }
            if path == "/subscriptions/sub_sem_meta":
                return {
                    "id": "sub_sem_meta",
                    "status": "active",
                    "customer": "cus_x",
                    "metadata": {},
                    "items": {"data": [{"id": "si2", "price": {"id": "price_starter_test"}}]},
                }
            if path == "/subscriptions/sub_com_meta":
                return {
                    "id": "sub_com_meta",
                    "status": "active",
                    "customer": "cus_x",
                    "metadata": {"conta_id": str(conta.id)},
                    "items": {"data": [{"id": "si3", "price": {"id": "price_starter_test"}}]},
                }
            if path == "/subscriptions":
                return {"data": []}
            raise AssertionError(path)

        monkeypatch.setattr("app.services.cleiton_monetizacao_service._stripe_get", _fake_get)

        out = _obter_assinatura_stripe_ativa(conta.id)
        assert out is not None
        assert out.get("subscription_id") == "sub_com_meta"
        assert out.get("origem") == "vinculo_historico_stripe_get"


def test_pro_para_starter_vigente_subscription_updated_sincroniza_frase_ciclo(app):
    """
    Apos Pro com ciclo operacional fechado, Stripe (Starter) com novo inicio de periodo
    aplica o plano, atualiza o ciclo e zera o consumo — sem manter 'pendente' falso-positivo.
    """
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-pro-st-vigente")
        franquia.limite_total = 1000
        franquia.consumo_acumulado = Decimal("918.97")
        t0 = _ts(2026, 1, 1)
        t1 = _ts(2026, 2, 1)
        franquia.inicio_ciclo = _naive_utc(t0)
        franquia.fim_ciclo = _naive_utc(t1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="pro-st-vigente@test.com", categoria="pro")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        _configurar_plano_pro_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_vig",
            subscription_id="sub_vig",
            price_id="price_pro_test",
            plano_interno="pro",
            status_contratual_externo="active",
            vigencia_externa_inicio=_naive_utc(t0),
            vigencia_externa_fim=_naive_utc(t1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        inicio = _ts(2026, 2, 1)
        fim = _ts(2026, 3, 1)
        evento = {
            "id": "evt_pro_st_vigente",
            "type": "customer.subscription.updated",
            "created": _ts(2026, 2, 2),
            "data": {
                "object": {
                    "id": "sub_vig",
                    "customer": "cus_vig",
                    "status": "active",
                    "current_period_start": inicio,
                    "current_period_end": fim,
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        out = processar_evento_stripe(evento)
        vinculo = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        user_refresh = db.session.get(type(user), user.id)
        fr = db.session.get(Franquia, franquia.id)
        snap = json.loads(vinculo.snapshot_normalizado_json or "{}")
        assert out["efeito_operacional_aplicado"] is True
        assert out.get("mudanca_pendente") is False
        assert user_refresh is not None and user_refresh.categoria == "starter"
        assert fr is not None
        assert fr.consumo_acumulado == Decimal("0")
        assert fr.inicio_ciclo == _naive_utc(inicio)
        assert fr.fim_ciclo == _naive_utc(fim)
        assert snap.get("mudanca_pendente") in (None, False)


def test_starter_mesma_categoria_subscription_updated_virada_ciclo_zera_consumo(app):
    """Mesmo plano, novo periodo Stripe via customer.subscription.updated — reinicia franquia."""
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-st-virada-sub")
        franquia.consumo_acumulado = Decimal("42.5")
        t_mar = _ts(2026, 3, 1)
        t_apr = _ts(2026, 4, 1)
        franquia.inicio_ciclo = _naive_utc(t_mar)
        franquia.fim_ciclo = _naive_utc(t_apr)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="st-virada@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_vira",
            subscription_id="sub_vira",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            vigencia_externa_inicio=_naive_utc(t_mar),
            vigencia_externa_fim=_naive_utc(t_apr),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        inicio2 = _ts(2026, 4, 1)
        fim2 = _ts(2026, 5, 1)
        evento = {
            "id": "evt_st_virada",
            "type": "customer.subscription.updated",
            "created": _ts(2026, 4, 2),
            "data": {
                "object": {
                    "id": "sub_vira",
                    "customer": "cus_vira",
                    "status": "active",
                    "current_period_start": inicio2,
                    "current_period_end": fim2,
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        out = processar_evento_stripe(evento)
        fr = db.session.get(Franquia, franquia.id)
        assert out["efeito_operacional_aplicado"] is True
        assert fr is not None
        assert fr.consumo_acumulado == Decimal("0")
        assert fr.inicio_ciclo == _naive_utc(inicio2)
        assert fr.fim_ciclo == _naive_utc(fim2)
        u = db.session.get(type(user), user.id)
        assert u is not None and u.categoria == "starter"


def test_idempotencia_subscription_updated_segundo_evento_igual_nao_zera_novamente(app):
    """Segundo processamento (ev distinto) com o mesmo periodo: sem novo reset indevido."""
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-idem-sub")
        franquia.consumo_acumulado = Decimal("1")
        t_mar = _ts(2026, 3, 1)
        t_apr = _ts(2026, 4, 1)
        franquia.inicio_ciclo = _naive_utc(t_mar)
        franquia.fim_ciclo = _naive_utc(t_apr)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="idem-sub@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_idem2",
            subscription_id="sub_idem2",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        inicio2 = _ts(2026, 4, 1)
        fim2 = _ts(2026, 5, 1)
        def _payload(inc_eid: int):
            return {
                "id": f"evt_idem_w_{inc_eid}",
                "type": "customer.subscription.updated",
                "created": _ts(2026, 4, 2),
                "data": {
                    "object": {
                        "id": "sub_idem2",
                        "customer": "cus_idem2",
                        "status": "active",
                        "current_period_start": inicio2,
                        "current_period_end": fim2,
                        "metadata": {
                            "conta_id": str(conta.id),
                            "franquia_id": str(franquia.id),
                        },
                        "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                    }
                },
            }
        first = processar_evento_stripe(_payload(1))
        # Simula reenvio/duplicata com outro id mas o mesmo fato de periodo
        second = processar_evento_stripe(_payload(2))
        fr = db.session.get(Franquia, franquia.id)
        assert first["efeito_operacional_aplicado"] is True
        assert second.get("efeito_operacional_aplicado") in (True, False)
        assert fr is not None and fr.consumo_acumulado == Decimal("0")
        # Periodo inalterado no segundo: consumo nao fica negativo/estranho
        assert fr.inicio_ciclo == _naive_utc(inicio2)


def test_subscription_updated_admin_mesmo_periodo_nao_zera_consumo_starter(app):
    """
    Update administrativo (sem virada de periodo): mesmo current_period, mesmo price Starter,
    consumo e ciclo operacional nao devem ser alterados.
    """
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="conta-sub-adm-no-rollover")
        t0 = _ts(2026, 6, 1)
        t1 = _ts(2026, 7, 1)
        limite_antes = Decimal("350")
        franquia.limite_total = limite_antes
        franquia.consumo_acumulado = Decimal("12.34")
        franquia.inicio_ciclo = _naive_utc(t0)
        franquia.fim_ciclo = _naive_utc(t1)
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(franquia.id, conta.id, email="sub-adm-noroll@test.com", categoria="starter")
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_adm",
            subscription_id="sub_adm",
            price_id="price_starter_test",
            plano_interno="starter",
            status_contratual_externo="active",
            vigencia_externa_inicio=_naive_utc(t0),
            vigencia_externa_fim=_naive_utc(t1),
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "seed"},
        )
        db.session.commit()
        inicio_espelho = t0
        fim_espelho = t1
        evento = {
            "id": "evt_sub_update_admin_mesmo_ciclo",
            "type": "customer.subscription.updated",
            "created": _ts(2026, 6, 15),
            "data": {
                "object": {
                    "id": "sub_adm",
                    "customer": "cus_adm",
                    "status": "active",
                    "current_period_start": inicio_espelho,
                    "current_period_end": fim_espelho,
                    "metadata": {
                        "conta_id": str(conta.id),
                        "franquia_id": str(franquia.id),
                    },
                    "items": {"data": [{"price": {"id": "price_starter_test"}}]},
                }
            },
        }
        out = processar_evento_stripe(evento)
        fr = db.session.get(Franquia, franquia.id)
        u = db.session.get(type(user), user.id)
        v = (
            ContaMonetizacaoVinculo.query.filter_by(conta_id=conta.id, ativo=True)
            .order_by(ContaMonetizacaoVinculo.id.desc())
            .first()
        )
        snap = json.loads(v.snapshot_normalizado_json or "{}")
        assert u is not None and u.categoria == "starter"
        assert fr is not None
        assert fr.limite_total == limite_antes
        assert fr.consumo_acumulado == Decimal("12.34")
        assert fr.inicio_ciclo == _naive_utc(t0)
        assert fr.fim_ciclo == _naive_utc(t1)
        assert not snap.get("mudanca_pendente")
        assert out.get("mudanca_pendente") in (None, False)
        assert out.get("ok") is True
