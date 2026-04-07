"""Testes de integração: motor de abatimento e reconciliação (SQLite em memória)."""
from decimal import Decimal

from app.extensions import db
from app.models import (
    CleitonCostConfig,
    Franquia,
    IaConsumoEvento,
    ProcessingEvent,
)
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)


def test_evento_cliente_ia_abate(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        _c, f = seed_conta_franquia_cliente()
        u = seed_usuario(f.id, _c.id)
        ev = IaConsumoEvento(
            provider="gemini",
            operation="generate_content",
            model="m",
            agent="a",
            flow_type="f",
            api_key_label="k",
            status="success",
            total_tokens=1000,
            franquia_id=f.id,
            usuario_id=u.id,
            origem_sistema=False,
            tipo_origem="http_usuario",
        )
        db.session.add(ev)
        db.session.commit()
        from app.services.cleiton_franquia_operacional_service import (
            aplicar_motor_apos_ia_consumo_evento,
        )

        r = aplicar_motor_apos_ia_consumo_evento(ev.id)
        assert r.abateu_franquia is True
        fr = db.session.get(Franquia, f.id)
        assert fr.consumo_acumulado == Decimal("1")


def test_evento_interno_nao_abate(app):
    with app.app_context():
        _sc, sf = seed_sistema_interno()
        seed_cleiton_cost_config()
        ev = IaConsumoEvento(
            provider="gemini",
            operation="generate_content",
            model="m",
            agent="a",
            flow_type="f",
            api_key_label="k",
            status="success",
            total_tokens=5000,
            franquia_id=sf.id,
            usuario_id=None,
            origem_sistema=True,
            tipo_origem="http_cron",
        )
        db.session.add(ev)
        db.session.commit()
        from app.services.cleiton_franquia_operacional_service import (
            aplicar_motor_apos_ia_consumo_evento,
        )

        r = aplicar_motor_apos_ia_consumo_evento(ev.id)
        assert r.abateu_franquia is False
        fr = db.session.get(Franquia, sf.id)
        assert fr.consumo_acumulado == Decimal("0")


def test_processamento_linhas_e_ms_creditos(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        _c, f = seed_conta_franquia_cliente()
        u = seed_usuario(f.id, _c.id)
        ev = ProcessingEvent(
            agent="roberto",
            flow_type="upload_bi",
            processing_type="non_llm",
            rows_processed=100,
            processing_time_ms=1000,
            status="success",
            franquia_id=f.id,
            usuario_id=u.id,
            origem_sistema=False,
            tipo_origem="http_usuario",
        )
        db.session.add(ev)
        db.session.commit()
        from app.services.cleiton_franquia_operacional_service import (
            aplicar_motor_apos_processing_event,
        )

        r = aplicar_motor_apos_processing_event(ev.id)
        assert r.abateu_franquia is True
        fr = db.session.get(Franquia, f.id)
        assert fr.consumo_acumulado == Decimal("2")


def test_sem_regua_valida_nao_abate(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        cfg = db.session.get(CleitonCostConfig, 1)
        cfg.credit_tokens_per_credit = None
        db.session.add(cfg)
        db.session.commit()
        _c, f = seed_conta_franquia_cliente()
        u = seed_usuario(f.id, _c.id)
        ev = IaConsumoEvento(
            provider="gemini",
            operation="generate_content",
            model="m",
            agent="a",
            flow_type="f",
            api_key_label="k",
            status="success",
            total_tokens=1000,
            franquia_id=f.id,
            usuario_id=u.id,
            origem_sistema=False,
            tipo_origem="http_usuario",
        )
        db.session.add(ev)
        db.session.commit()
        from app.services.cleiton_franquia_operacional_service import (
            aplicar_motor_apos_ia_consumo_evento,
        )

        r = aplicar_motor_apos_ia_consumo_evento(ev.id)
        assert r.abateu_franquia is False
        assert r.motivo_nao_abateu == "falha_conversao_creditos"


def test_reconciliacao_ok_e_correcao(app):
    with app.app_context():
        seed_sistema_interno()
        seed_cleiton_cost_config()
        _c, f = seed_conta_franquia_cliente()
        u = seed_usuario(f.id, _c.id)
        ev = IaConsumoEvento(
            provider="gemini",
            operation="generate_content",
            model="m",
            agent="a",
            flow_type="f",
            api_key_label="k",
            status="success",
            total_tokens=1000,
            franquia_id=f.id,
            usuario_id=u.id,
            origem_sistema=False,
            tipo_origem="http_usuario",
        )
        db.session.add(ev)
        db.session.commit()
        from app.services.cleiton_franquia_operacional_service import (
            aplicar_motor_apos_ia_consumo_evento,
        )
        from app.services.cleiton_franquia_reconciliacao_service import (
            reconciliar_franquia_cleiton,
        )

        aplicar_motor_apos_ia_consumo_evento(ev.id)
        r0 = reconciliar_franquia_cleiton(f.id)
        assert r0.status == "ok"

        fr = db.session.get(Franquia, f.id)
        fr.consumo_acumulado = Decimal("99")
        db.session.add(fr)
        db.session.commit()

        r1 = reconciliar_franquia_cleiton(f.id, aplicar_correcao=False)
        assert r1.status == "divergente"

        r2 = reconciliar_franquia_cleiton(f.id, aplicar_correcao=True)
        assert r2.correcao_aplicada is True
        assert r2.status == "ok"
        fr2 = db.session.get(Franquia, f.id)
        assert fr2.consumo_acumulado == Decimal("1")
