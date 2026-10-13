"""SCRUM-146 hotfix: reconciliação não cobra evento técnico sem abatimento.

Provedor simulado e banco descartável. Não chama o Gemini pago.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from app.extensions import db
from app.models import (
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    ProcessingEvent,
    utcnow_naive,
)
from app.services.agente_compara_extraction_billing import liquidar_ou_dispensar_entrega
from app.services.cleiton_franquia_reconciliacao_service import reconciliar_franquia_cleiton
from tests.test_scrum146_extracao_ciclo import (
    DIGEST,
    _abrir,
    _avancar,
    _pendurar,
    _saldo,
    _seed as _seed_ciclo,
)
from tests.test_scrum146_extracao_cobranca import JSON_UTIL, _abatimentos, _extrair, _preparar
from tests.test_scrum146_extracao_orcamento import _franquia, _resposta, _seed, _usage


def _evento(franquia, usuario, *, agent, tokens, quando):
    row = IaConsumoEvento(
        occurred_at=quando,
        provider="gemini",
        operation="generate_content",
        model="gemini-test",
        agent=agent,
        flow_type="chat",
        api_key_label="test",
        status="success",
        total_tokens=tokens,
        franquia_id=franquia.id,
        usuario_id=usuario.id,
        origem_sistema=False,
        tipo_origem="http_usuario",
    )
    db.session.add(row)
    db.session.flush()
    return row


def test_extracao_sem_json_util_reconciliacao_nao_debita(app, monkeypatch):
    with app.app_context():
        _usuario, franquia = _seed(limite="100", email="sem-json-recon@test.com")
        _preparar(
            monkeypatch,
            usuario=_usuario,
            gerar=lambda model, contents, config: _resposta("sem json", prompt=40, resposta=20, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["status"] == "failed"
        tentativa = IaChamadaTentativa.query.one()
        evento = db.session.get(IaConsumoEvento, tentativa.ia_consumo_evento_id)
        assert tentativa.status == "released"
        assert tentativa.failure_reason == "json_invalido"
        assert evento.total_tokens == 70
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0

        leitura = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=False)
        assert leitura.total_recalculado == 0
        assert leitura.contagem_eventos_ia_abativel == 0

        corrigida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert corrigida.status == "ok"
        assert corrigida.correcao_aplicada is False
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _abatimentos() == 0
        assert db.session.get(IaConsumoEvento, evento.id).total_tokens == 70


def test_primario_descartado_fallback_util_mantem_so_0080(app, monkeypatch):
    with app.app_context():
        _usuario, franquia = _seed(limite="100", email="fallback-recon@test.com")

        class _Falha(RuntimeError):
            def __init__(self):
                super().__init__("provider down")
                self.usage_metadata = _usage(prompt=700, resposta=200, pensamento=100)

        def gerar(model, contents, config):
            if model == "gemini-2.5-flash":
                raise _Falha()
            return _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10)

        _preparar(
            monkeypatch,
            usuario=_usuario,
            gerar=gerar,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
            totais=(1_000, 1_000),
        )
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        marca = IaConsumoAbatimento.query.one()
        marca_id = marca.id
        descartada = IaChamadaTentativa.query.filter_by(status="released").one()
        evento_descartado = db.session.get(IaConsumoEvento, descartada.ia_consumo_evento_id)
        assert descartada.failure_reason == "erro_provedor"
        assert evento_descartado.total_tokens == 1000
        assert IaConsumoAbatimento.query.filter_by(ia_consumo_evento_id=evento_descartado.id).count() == 0

        atual = _franquia(franquia.id)
        atual.consumo_acumulado = Decimal("0")
        db.session.commit()
        corrigida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert corrigida.correcao_aplicada is True
        assert corrigida.total_recalculado == Decimal("0.080000")
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _abatimentos() == 1
        assert db.session.get(IaConsumoAbatimento, marca_id).creditos == Decimal("0.080000")
        assert db.session.get(IaConsumoEvento, evento_descartado.id).total_tokens == 1000

        repetida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert repetida.correcao_aplicada is False
        assert repetida.status == "ok"
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")


def test_replay_apos_renovacao_nao_reincorpora_ciclo_a(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="replay-recon@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        primeiro = _extrair()
        assert primeiro["status"] == "needs_review"
        original = IaChamadaTentativa.query.one()
        inicio_a, fim_a = original.ciclo_inicio, original.ciclo_fim
        marca = IaConsumoAbatimento.query.one()
        marca_id = marca.id
        creditos = marca.creditos
        evento_a = marca.ia_consumo_evento_id
        assert creditos == Decimal("0.080000")
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")

        _avancar(franquia.id, consumo="0")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        _extrair()
        db.session.expire_all()
        replay = IaChamadaTentativa.query.filter_by(failure_reason="entrega_ja_faturada").one()
        assert replay.status == "released"
        assert replay.ciclo_inicio != inicio_a
        assert replay.ia_consumo_evento_id is not None
        assert replay.ia_consumo_evento_id != evento_a
        assert db.session.get(IaConsumoEvento, replay.ia_consumo_evento_id).total_tokens == 160
        assert IaConsumoAbatimento.query.count() == 1
        assert _franquia(franquia.id).consumo_acumulado == 0

        corrigida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert corrigida.total_recalculado == 0
        assert corrigida.status == "ok"
        assert corrigida.correcao_aplicada is False
        assert _franquia(franquia.id).consumo_acumulado == 0
        preservada = db.session.get(IaConsumoAbatimento, marca_id)
        assert preservada.creditos == creditos
        assert preservada.ia_consumo_evento_id == evento_a
        original = IaChamadaTentativa.query.filter_by(status="settled").one()
        assert original.ciclo_inicio == inicio_a
        assert original.ciclo_fim == fim_a

        repetida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert repetida.correcao_aplicada is False
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert db.session.get(IaConsumoAbatimento, marca_id).creditos == creditos


def test_entrega_util_no_mesmo_ciclo_continua_faturavel(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="util-recon@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        marca = IaConsumoAbatimento.query.one()
        marca_id = marca.id
        assert marca.creditos == Decimal("0.080000")
        atual = _franquia(franquia.id)
        atual.consumo_acumulado = Decimal("0")
        db.session.commit()

        corrigida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert corrigida.correcao_aplicada is True
        assert corrigida.total_recalculado == Decimal("0.080000")
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert db.session.get(IaConsumoAbatimento, marca_id).creditos == Decimal("0.080000")

        repetida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert repetida.correcao_aplicada is False
        assert repetida.status == "ok"
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert IaConsumoAbatimento.query.count() == 1


def test_liquidacao_tardia_permanece_no_ciclo_original(app):
    with app.app_context():
        usuario, franquia = _seed_ciclo(email="tardia-recon@test.com")
        resultado = _abrir(usuario)
        tentativa = db.session.get(IaChamadaTentativa, resultado.attempt_id)
        inicio_a, fim_a = tentativa.ciclo_inicio, tentativa.ciclo_fim
        _avancar(franquia.id, consumo="0")
        liquidar_ou_dispensar_entrega(resultado.attempt_id, DIGEST)
        marca = IaConsumoAbatimento.query.one()
        marca_id = marca.id
        assert marca.creditos == Decimal("1")
        assert _franquia(franquia.id).consumo_acumulado == 0

        corrigida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert corrigida.total_recalculado == 0
        assert corrigida.status == "ok"
        assert _franquia(franquia.id).consumo_acumulado == 0
        preservada = db.session.get(IaConsumoAbatimento, marca_id)
        assert preservada.creditos == Decimal("1")
        liquidada = db.session.get(IaChamadaTentativa, resultado.attempt_id)
        assert liquidada.ciclo_inicio == inicio_a
        assert liquidada.ciclo_fim == fim_a
        assert liquidada.status == "settled"


def test_reserva_de_ciclo_anterior_nao_altera_saldo_novo(app):
    with app.app_context():
        _usuario, franquia = _seed_ciclo(email="reserva-recon@test.com", limite="100")
        _pendurar(franquia, "40")
        _avancar(franquia.id, consumo="0")
        assert _saldo(franquia.id) == Decimal("100")
        assert _franquia(franquia.id).reserva_pendente == Decimal("40")

        corrigida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert corrigida.total_recalculado == 0
        assert corrigida.status == "ok"
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == Decimal("40")
        assert _saldo(franquia.id) == Decimal("100")
        assert IaChamadaTentativa.query.filter_by(reserva_ativa=True).count() == 1


def test_evento_legado_e_outro_agente_permanecem_compativeis(app):
    with app.app_context():
        usuario, franquia = _seed_ciclo(email="legado-recon@test.com")
        agora = utcnow_naive()
        legado = _evento(franquia, usuario, agent="cleide", tokens=1000, quando=agora)
        fora = _evento(
            franquia,
            usuario,
            agent="cleide",
            tokens=5000,
            quando=franquia.inicio_ciclo - timedelta(days=1),
        )
        tecnico = _evento(franquia, usuario, agent="roberto", tokens=5000, quando=agora)
        db.session.add(
            IaChamadaTentativa(
                attempt_key="recon-roberto-tecnico",
                franquia_id=franquia.id,
                conta_id=franquia.conta_id,
                usuario_id=usuario.id,
                agent="roberto",
                flow_type="chat",
                operation="generate_content",
                provider="gemini",
                model="gemini-test",
                ciclo_inicio=franquia.inicio_ciclo,
                ciclo_fim=franquia.fim_ciclo,
                reserved_credits=Decimal("5"),
                status="released",
                failure_reason="erro_provedor",
                ia_consumo_evento_id=tecnico.id,
                reserva_ativa=False,
                debita_cliente=True,
                created_at=agora,
            )
        )
        confirmado = _evento(franquia, usuario, agent="cleide", tokens=9000, quando=agora)
        marca = IaConsumoAbatimento(
            ia_consumo_evento_id=confirmado.id,
            franquia_id=franquia.id,
            creditos=Decimal("0.250000"),
            created_at=agora,
        )
        db.session.add(marca)
        db.session.add(
            IaChamadaTentativa(
                attempt_key="recon-cleide-faturada",
                franquia_id=franquia.id,
                conta_id=franquia.conta_id,
                usuario_id=usuario.id,
                agent="cleide",
                flow_type="auditoria",
                operation="generate_content",
                provider="gemini",
                model="gemini-test",
                ciclo_inicio=franquia.inicio_ciclo,
                ciclo_fim=franquia.fim_ciclo,
                reserved_credits=Decimal("1"),
                status="settled",
                ia_consumo_evento_id=confirmado.id,
                reserva_ativa=False,
                debita_cliente=True,
                created_at=agora,
            )
        )
        db.session.add(
            ProcessingEvent(
                occurred_at=agora,
                agent="roberto",
                flow_type="upload_bi",
                processing_type="non_llm",
                rows_processed=100,
                processing_time_ms=0,
                status="success",
                franquia_id=franquia.id,
                usuario_id=usuario.id,
                origem_sistema=False,
                tipo_origem="http_usuario",
            )
        )
        db.session.commit()
        marca_id = marca.id

        corrigida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert corrigida.total_recalculado == Decimal("2.250000")
        assert corrigida.correcao_aplicada is True
        assert _franquia(franquia.id).consumo_acumulado == Decimal("2.250000")
        assert db.session.get(IaConsumoAbatimento, marca_id).creditos == Decimal("0.250000")
        assert db.session.get(IaConsumoEvento, tecnico.id).total_tokens == 5000
        assert db.session.get(IaConsumoEvento, legado.id).total_tokens == 1000
        assert db.session.get(IaConsumoEvento, fora.id).total_tokens == 5000
        assert db.session.get(IaConsumoEvento, confirmado.id).total_tokens == 9000
        assert IaConsumoAbatimento.query.count() == 1

        repetida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert repetida.correcao_aplicada is False
        assert repetida.status == "ok"
        assert _franquia(franquia.id).consumo_acumulado == Decimal("2.250000")
