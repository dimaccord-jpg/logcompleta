import json

from app.extensions import db
from app.models import MonetizacaoFato
from app.services.cleiton_franquia_validacao_admin_service import (
    obter_pacote_validacao_franquia_cleiton,
    reprocessar_pendencias_monetizacao_franquia_admin,
)
from app.services.cleiton_monetizacao_service import (
    processar_evento_stripe,
    registrar_fato_monetizacao,
    registrar_vinculo_comercial_externo,
)
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)


def test_pacote_validacao_expoe_contexto_monetario_estruturado(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-validacao-audit")
        user = seed_usuario(franquia.id, conta.id, email="audit@test.com", categoria="starter")

        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_audit",
            subscription_id="sub_audit",
            price_id="price_audit",
            plano_interno="starter",
            status_contratual_externo="active",
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origin": "teste"},
        )
        registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_paid",
            status_tecnico="efeito_operacional_aplicado",
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            provider="stripe",
            external_event_id="evt_pkg_audit",
            identificadores_externos={"event_id": "evt_pkg_audit"},
            snapshot_normalizado={"ok": True},
            payload_bruto_sanitizado={"raw": "payload"},
        )
        db.session.commit()

        pacote = obter_pacote_validacao_franquia_cleiton(franquia.id)
        assert pacote["ok"] is True
        contexto = pacote["contexto_monetario"]
        ativo = contexto["vinculo_comercial_externo_ativo"]
        assert "snapshot_normalizado_json" in ativo
        assert "snapshot_normalizado" in ativo
        assert ativo["snapshot_normalizado"]["franquia_id"] == franquia.id
        assert ativo["payload_bruto_sanitizado"]["origin"] == "teste"
        assert contexto["vinculos_comerciais_historico"][0]["snapshot_normalizado"]["conta_id"] == conta.id
        fato = contexto["fatos_monetizacao_recentes"][0]
        assert fato["snapshot_normalizado"]["ok"] is True
        assert fato["payload_bruto_sanitizado"]["raw"] == "payload"
        assert fato["identificadores_externos"]["event_id"] == "evt_pkg_audit"


def test_pacote_validacao_sinaliza_pendencias_stripe_admin(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-validacao-pend")
        seed_usuario(franquia.id, conta.id, email="pend@test.com", categoria="starter")

        pacote = obter_pacote_validacao_franquia_cleiton(franquia.id)
        assert pacote["ok"] is True
        pendencias = pacote["pendencias_configuracao_stripe_planos"]
        assert len(pendencias) > 0
        assert any(p["plano_codigo"] == "starter" for p in pendencias)
        assert "stripe_configuracao_pendente_admin" in pacote["pendencias_governanca"]


def test_pacote_oficial_validacao_retorna_contexto_expandido(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-endpoint-valid")
        seed_usuario(franquia.id, conta.id, email="endpoint@test.com", categoria="starter")
        registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_payment_failed",
            status_tecnico="pendente_correlacao",
            conta_id=conta.id,
            franquia_id=franquia.id,
            provider="stripe",
            external_event_id="evt_endpoint",
            snapshot_normalizado={"pendente": True},
            payload_bruto_sanitizado={"raw": "endpoint"},
        )
        db.session.commit()

        payload = obter_pacote_validacao_franquia_cleiton(franquia.id)
        assert payload["ok"] is True
        assert payload["franquia_id"] == franquia.id
        fatos = payload["contexto_monetario"]["fatos_monetizacao_recentes"]
        assert len(fatos) >= 1
        assert "snapshot_normalizado" in fatos[0]
        assert "payload_bruto_sanitizado" in fatos[0]


def test_pacote_validacao_sem_efeito_colateral_na_leitura(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-leitura-estavel")
        seed_usuario(franquia.id, conta.id, email="leitura@test.com", categoria="starter")
        registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_payment_failed",
            status_tecnico="pendente_correlacao",
            conta_id=conta.id,
            franquia_id=franquia.id,
            provider="stripe",
            external_event_id="evt_leitura_estavel",
            snapshot_normalizado={"pendente": True},
            payload_bruto_sanitizado={"raw": "endpoint"},
        )
        db.session.commit()
        before = MonetizacaoFato.query.count()
        _ = obter_pacote_validacao_franquia_cleiton(franquia.id)
        after = MonetizacaoFato.query.count()
        assert before == after


def test_pacote_validacao_explicita_divergencia_fato_vs_vinculo(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-divergencia")
        user = seed_usuario(franquia.id, conta.id, email="divergencia@test.com", categoria="starter")
        registrar_vinculo_comercial_externo(
            conta_id=conta.id,
            provider="stripe",
            customer_id="cus_div",
            subscription_id="sub_div",
            price_id="price_div",
            plano_interno="starter",
            status_contratual_externo="canceled",
            snapshot_normalizado={"conta_id": conta.id, "franquia_id": franquia.id},
            payload_bruto_sanitizado={"origem": "teste"},
        )
        registrar_fato_monetizacao(
            tipo_fato="stripe_invoice_paid",
            status_tecnico="efeito_operacional_aplicado",
            conta_id=conta.id,
            franquia_id=franquia.id,
            usuario_id=user.id,
            provider="stripe",
            external_event_id="evt_div_1",
            snapshot_normalizado={
                "plano_resolvido": "pro",
                "status_contratual_externo": "paid",
            },
            payload_bruto_sanitizado={"raw": "payload"},
        )
        db.session.commit()
        pacote = obter_pacote_validacao_franquia_cleiton(franquia.id)
        divergencias = pacote["auditoria_monetizacao"]["divergencias_relevantes"]
        assert len(divergencias) >= 1
        assert any(d["tipo"] == "status_contratual_divergente" for d in divergencias)


def test_reprocessamento_admin_por_franquia_resolve_quando_vinculo_existe(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        conta, franquia = seed_conta_franquia_cliente(slug="conta-reprocess-admin")
        seed_usuario(franquia.id, conta.id, email="reprocess-admin@test.com", categoria="starter")

        evento = {
            "id": "evt_pkg_reprocess",
            "type": "invoice.payment_failed",
            "created": 1713350401,
            "data": {
                "object": {
                    "id": "in_pkg_reprocess",
                    "customer": "cus_pkg_reprocess",
                    "subscription": "sub_pkg_reprocess",
                    "status": "open",
                }
            },
        }
        processar_evento_stripe(evento)
        fato = MonetizacaoFato.query.filter_by(external_event_id="evt_pkg_reprocess").first()
        assert fato is not None
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

        out = reprocessar_pendencias_monetizacao_franquia_admin(
            franquia_id=franquia.id,
            admin_user_id=123,
            limite=10,
        )
        assert out["ok"] is True
        assert out["reprocessamento"]["total_analisado"] >= 1
