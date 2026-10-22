"""SCRUM-238: a franquia interna conclui a extração sem débito e sem consumo incerto.

Provedor simulado. Não altera a regra de quem deve ser debitado.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.agente_compara_doc_service import (
    ERROR_EXTRACAO_CONSUMO_INCERTO,
    ERROR_EXTRACAO_CREDITOS_INSUFICIENTES,
)
from app.extensions import db
from app.models import Franquia, IaConsumoAbatimento, IaConsumoEvento, utcnow_naive
from app.services.agente_compara_extraction_billing import (
    FLOW_EXTRACAO_TABELA,
    ExtracaoConsumoIncerto,
    abater_entrega_confirmada,
    entrega_faturavel,
)
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)
from tests.test_scrum146_extracao_cobranca import (
    JSON_UTIL,
    _abatimentos,
    _extrair,
    _franquia,
    _preparar,
    _resposta,
)
from tests.test_scrum146_extracao_mensagens import (
    UPGRADE_LABEL_DEFAULT,
    UPGRADE_PATH_DEFAULT,
    _publico,
)
from tests.test_scrum146_extracao_orcamento import _seed

import app.run_agente_compara_temp_table as temp_mod


def _gerar_util(model, contents, config):
    return _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10)


def _sem_pendencia(resultado: dict) -> None:
    assert resultado["status"] == "needs_review"
    assert entrega_faturavel(resultado) is True
    assert resultado.get("error_code") != ERROR_EXTRACAO_CONSUMO_INCERTO
    assert resultado.get("credit_disposition") != "unconfirmed"
    assert "pendente" not in (resultado.get("safe_message") or "")


def test_franquia_interna_conclui_sem_debito_e_sem_consumo_incerto(app, monkeypatch):
    with app.app_context():
        _conta, franquia = seed_sistema_interno()
        cfg = seed_cleiton_cost_config()
        cfg.credit_tokens_per_credit = 2000.0
        franquia.consumo_acumulado = Decimal("0")
        franquia.reserva_pendente = Decimal("0")
        db.session.commit()
        usuario = seed_usuario(franquia.id, _conta.id, email="interno-extracao@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=_gerar_util,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        _sem_pendencia(resultado)
        evento = IaConsumoEvento.query.one()
        assert evento.status == "success"
        assert evento.total_tokens == 160
        assert evento.franquia_id == franquia.id
        assert evento.usuario_id == usuario.id
        assert franquia.slug == Franquia.SLUG_SISTEMA_OPERACIONAL
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0

        digest = temp_mod.digest_da_entrega(
            ["doc-1"],
            comparison_id="cmp-entrega",
            table_id="tbl-entrega",
        )
        assert abater_entrega_confirmada(evento.id, digest) == "dispensada"
        db.session.expire_all()
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert IaConsumoEvento.query.one().total_tokens == 160


def test_reexecucao_da_franquia_interna_nao_cobra(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_sistema_interno()
        cfg = seed_cleiton_cost_config()
        cfg.credit_tokens_per_credit = 2000.0
        franquia.consumo_acumulado = Decimal("0")
        franquia.reserva_pendente = Decimal("0")
        db.session.commit()
        usuario = seed_usuario(conta_id=conta.id, franquia_id=franquia.id, email="interno-replay@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=_gerar_util,
            models=["gemini-2.5-flash"],
        )
        _sem_pendencia(_extrair())
        _sem_pendencia(_extrair())
        assert IaConsumoEvento.query.count() == 2
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0


def test_cache_indisponivel_preserva_entrega_da_franquia_interna(app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_sistema_interno()
        cfg = seed_cleiton_cost_config()
        cfg.credit_tokens_per_credit = 2000.0
        franquia.consumo_acumulado = Decimal("0")
        franquia.reserva_pendente = Decimal("0")
        db.session.commit()
        usuario = seed_usuario(franquia.id, conta.id, email="interno-cache@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=_gerar_util,
            models=["gemini-2.5-flash"],
        )

        def _cache_indisponivel(*_args, **_kwargs):
            raise RuntimeError("cache indisponivel")

        monkeypatch.setattr(temp_mod, "_cache_extraction_result", _cache_indisponivel)
        resultado = _extrair()
        _sem_pendencia(resultado)
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_franquia_comum_ilimitada_debita_o_consumo(app, monkeypatch):
    with app.app_context():
        seed_sistema_interno()
        cfg = seed_cleiton_cost_config()
        cfg.credit_tokens_per_credit = 2000.0
        conta, franquia = seed_conta_franquia_cliente(slug="conta-ilimitada")
        franquia.limite_total = None
        franquia.consumo_acumulado = Decimal("0")
        franquia.reserva_pendente = Decimal("0")
        franquia.inicio_ciclo = utcnow_naive()
        franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
        db.session.commit()
        usuario = seed_usuario(franquia.id, conta.id, email="ilimitada@test.com", categoria="free")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=_gerar_util,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        _sem_pendencia(resultado)
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaConsumoEvento.query.one().total_tokens == 160


def test_free_com_creditos_debita_o_consumo(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="free-com-credito@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=_gerar_util,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        _sem_pendencia(resultado)
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0


def test_free_sem_creditos_mantem_bloqueio_e_cta(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="15", consumo="15", email="free-sem-credito@test.com")

        def _nao_gerar(model, contents, config):
            raise AssertionError("nao gerar")

        client = _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=_nao_gerar,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert resultado["error_code"] == ERROR_EXTRACAO_CREDITOS_INSUFICIENTES
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        cta = publico["upgrade_cta"]
        assert cta["upgrade_url"] == UPGRADE_PATH_DEFAULT
        assert cta["upgrade_label"] == UPGRADE_LABEL_DEFAULT
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == Decimal("15")
        assert _franquia(franquia.id).reserva_pendente == 0


def test_ausencia_de_debito_que_nao_e_dispensa_continua_incerta(app):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="sem-usuario@test.com")
        del usuario
        evento = IaConsumoEvento(
            occurred_at=utcnow_naive(),
            provider="gemini",
            operation="generate_content",
            model="gemini-2.5-flash",
            agent="agente_compara",
            flow_type=FLOW_EXTRACAO_TABELA,
            api_key_label="test",
            status="success",
            input_tokens=100,
            output_tokens=50,
            total_tokens=160,
            franquia_id=franquia.id,
            usuario_id=None,
            tipo_origem="http_usuario",
            origem_sistema=False,
            regime_abatimento=IaConsumoEvento.REGIME_NOVO,
        )
        db.session.add(evento)
        db.session.commit()
        evento_id = evento.id
        with pytest.raises(ExtracaoConsumoIncerto) as exc:
            abater_entrega_confirmada(evento_id, "digest-sem-usuario")
        assert str(exc.value) == "sem_usuario_id"
        db.session.expire_all()
        assert IaConsumoAbatimento.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert db.session.get(IaConsumoEvento, evento_id).total_tokens == 160
