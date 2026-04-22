from datetime import timezone

from app.extensions import db
from app.models import Franquia, User
from app.services import plano_service
from app.services.cleiton_monetizacao_service import processar_evento_stripe
from tests.conftest import seed_conta_franquia_cliente, seed_usuario
from tests.test_cleiton_monetizacao_service import _configurar_plano_starter_para_teste, _ts


def _meta_starter(conta, franquia, user):
    return {
        "conta_id": str(conta.id),
        "franquia_id": str(franquia.id),
        "usuario_id": str(user.id),
        "plano_interno": "starter",
    }


def test_invoice_paid_correlaciona_metadata_apenas_em_lines_data(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="nested-lines-only")
        franquia.limite_total = 5
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="nested-lines-only@test.com",
            categoria="free",
        )
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        meta = _meta_starter(conta, franquia, user)
        inicio = _ts(2026, 4, 1)
        fim = _ts(2026, 5, 1)

        evento = {
            "id": "evt_nested_lines_only",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_nested_lines_only",
                    "customer": "cus_nested_lines_only",
                    "subscription": "sub_nested_lines_only",
                    "status": "paid",
                    "metadata": {},
                    "lines": {
                        "data": [
                            {
                                "metadata": dict(meta),
                                "period": {"start": inicio, "end": fim},
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }

        out = processar_evento_stripe(evento)
        limite_ref = plano_service.obter_limite_referencia_plano_admin(
            "starter", exigir_configurado=True
        )

        user_refresh = db.session.get(User, user.id)
        franquia_refresh = db.session.get(Franquia, franquia.id)
        assert out["status_tecnico"] == "efeito_operacional_aplicado"
        assert out.get("pendente_correlacao") is not True
        assert out["efeito_operacional_aplicado"] is True
        assert user_refresh is not None and user_refresh.categoria == "starter"
        assert franquia_refresh is not None and franquia_refresh.limite_total == limite_ref


def test_invoice_paid_correlaciona_metadata_apenas_em_parent_subscription_details(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="nested-parent-only")
        franquia.limite_total = 5
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="nested-parent-only@test.com",
            categoria="free",
        )
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        meta = _meta_starter(conta, franquia, user)
        inicio = _ts(2026, 4, 1)
        fim = _ts(2026, 5, 1)

        evento = {
            "id": "evt_nested_parent_only",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_nested_parent_only",
                    "customer": "cus_nested_parent_only",
                    "subscription": "sub_nested_parent_only",
                    "status": "paid",
                    "metadata": {},
                    "parent": {
                        "subscription_details": {
                            "metadata": dict(meta),
                        }
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
        limite_ref = plano_service.obter_limite_referencia_plano_admin(
            "starter", exigir_configurado=True
        )

        user_refresh = db.session.get(User, user.id)
        franquia_refresh = db.session.get(Franquia, franquia.id)
        assert out["status_tecnico"] == "efeito_operacional_aplicado"
        assert out.get("pendente_correlacao") is not True
        assert out["efeito_operacional_aplicado"] is True
        assert user_refresh is not None and user_refresh.categoria == "starter"
        assert franquia_refresh is not None and franquia_refresh.limite_total == limite_ref


def test_invoice_paid_precedencia_object_e_parent_mesmos_identificadores(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="nested-precedence")
        franquia.limite_total = 5
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="nested-precedence@test.com",
            categoria="free",
        )
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        meta_full = _meta_starter(conta, franquia, user)
        meta_partial = {
            "conta_id": str(conta.id),
            "franquia_id": str(franquia.id),
        }
        inicio = _ts(2026, 4, 1)
        fim = _ts(2026, 5, 1)

        evento = {
            "id": "evt_nested_precedence",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_nested_precedence",
                    "customer": "cus_nested_precedence",
                    "subscription": "sub_nested_precedence",
                    "status": "paid",
                    "metadata": dict(meta_partial),
                    "parent": {
                        "subscription_details": {
                            "metadata": dict(meta_full),
                        }
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
        limite_ref = plano_service.obter_limite_referencia_plano_admin(
            "starter", exigir_configurado=True
        )

        user_refresh = db.session.get(User, user.id)
        franquia_refresh = db.session.get(Franquia, franquia.id)
        assert out["status_tecnico"] == "efeito_operacional_aplicado"
        assert out.get("pendente_correlacao") is not True
        assert user_refresh is not None and user_refresh.categoria == "starter"
        assert franquia_refresh is not None and franquia_refresh.limite_total == limite_ref


def test_invoice_paid_linhas_metadata_conflito_nao_correlaciona_nem_aplica_upgrade(app):
    with app.app_context():
        conta_a, fr_a = seed_conta_franquia_cliente(slug="nested-conflict-a")
        conta_b, fr_b = seed_conta_franquia_cliente(slug="nested-conflict-b")
        fr_a.limite_total = 3
        fr_b.limite_total = 4
        db.session.add_all([fr_a, fr_b])
        db.session.commit()
        user_a = seed_usuario(fr_a.id, conta_a.id, email="nested-conf-a@test.com", categoria="free")
        user_b = seed_usuario(fr_b.id, conta_b.id, email="nested-conf-b@test.com", categoria="free")
        _configurar_plano_starter_para_teste(franquia_ref_id=fr_a.id)

        meta_a = _meta_starter(conta_a, fr_a, user_a)
        meta_b = _meta_starter(conta_b, fr_b, user_b)
        inicio = _ts(2026, 4, 1)
        fim = _ts(2026, 5, 1)

        evento = {
            "id": "evt_nested_lines_conflict",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_nested_lines_conflict",
                    "customer": "cus_nested_conflict",
                    "subscription": "sub_nested_conflict",
                    "status": "paid",
                    "metadata": {},
                    "lines": {
                        "data": [
                            {
                                "metadata": dict(meta_a),
                                "period": {"start": inicio, "end": fim},
                                "price": {"id": "price_starter_test"},
                            },
                            {
                                "metadata": dict(meta_b),
                                "period": {"start": inicio, "end": fim},
                                "price": {"id": "price_starter_test"},
                            },
                        ]
                    },
                }
            },
        }

        out = processar_evento_stripe(evento)
        assert out.get("pendente_correlacao") is True
        assert out["efeito_operacional_aplicado"] is False

        ua = db.session.get(User, user_a.id)
        ub = db.session.get(User, user_b.id)
        assert ua is not None and ua.categoria == "free"
        assert ub is not None and ub.categoria == "free"


def test_invoice_paid_nested_upgrade_operacional_ciclo_franquia(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="nested-operacional-ciclo")
        franquia.limite_total = 5
        db.session.add(franquia)
        db.session.commit()
        user = seed_usuario(
            franquia.id,
            conta.id,
            email="nested-ciclo@test.com",
            categoria="free",
        )
        _configurar_plano_starter_para_teste(franquia_ref_id=franquia.id)
        meta = _meta_starter(conta, franquia, user)
        inicio = _ts(2026, 4, 1)
        fim = _ts(2026, 5, 1)

        evento = {
            "id": "evt_nested_operacional_ciclo",
            "type": "invoice.paid",
            "created": _ts(2026, 4, 17),
            "data": {
                "object": {
                    "id": "in_nested_operacional_ciclo",
                    "customer": "cus_nested_operacional_ciclo",
                    "subscription": "sub_nested_operacional_ciclo",
                    "status": "paid",
                    "metadata": {},
                    "lines": {
                        "data": [
                            {
                                "metadata": dict(meta),
                                "period": {"start": inicio, "end": fim},
                                "price": {"id": "price_starter_test"},
                            }
                        ]
                    },
                }
            },
        }

        out = processar_evento_stripe(evento)
        fr = db.session.get(Franquia, franquia.id)
        limite_ref = plano_service.obter_limite_referencia_plano_admin(
            "starter", exigir_configurado=True
        )

        assert out["efeito_operacional_aplicado"] is True
        assert fr is not None
        assert fr.inicio_ciclo is not None
        assert fr.fim_ciclo is not None
        assert int(fr.inicio_ciclo.replace(tzinfo=timezone.utc).timestamp()) == inicio
        assert int(fr.fim_ciclo.replace(tzinfo=timezone.utc).timestamp()) == fim
        assert fr.limite_total == limite_ref
