"""SCRUM-146: competência do ciclo da franquia.

Provedor simulado e banco descartável. Não chama o Gemini pago.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from flask import Flask
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from app.extensions import db
from app.models import (
    CleitonCostConfig,
    Conta,
    ContaMonetizacaoVinculo,
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    ProcessingEvent,
    User,
    utcnow_naive,
)
from app.services.agente_compara_extraction_budget import (
    EXTRACAO_TETO_TECNICO_TOKENS,
    montar_orcamento_tentativa,
)
from app.services.cleiton_billable_ai_call import (
    cleiton_governed_billable_ai_call,
    liquidar_tentativa_ia,
)
from app.services.cleiton_franquia_operacional_service import saldo_disponivel_chamada_ia
from app.services.cleiton_franquia_reconciliacao_service import reconciliar_franquia_cleiton
from app.services.cleiton_monetizacao_service import aplicar_fato_contratual_em_franquia
from app.services.conta_multiuser_ciclo_service import aplicar_fanout_ciclo_confirmado
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)

DIGEST = "c3" * 16
PREFIXO = f"ac-tt:{DIGEST}:"


class _Models:
    def __init__(self, total):
        self.calls = []

        def count_tokens(*, model, contents, config=None):
            return SimpleNamespace(total_tokens=20, cached_content_token_count=None)

        self.count_tokens = count_tokens
        self._total = total

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(model)
        return SimpleNamespace(
            text="ok",
            usage_metadata=SimpleNamespace(
                prompt_token_count=self._total,
                candidates_token_count=0,
                thoughts_token_count=0,
                total_token_count=self._total,
            ),
            candidates=[],
        )


class _Client:
    def __init__(self, total=1000):
        self.models = _Models(total)
        self.http_options = SimpleNamespace(retry_options=SimpleNamespace(attempts=1))


def _seed(*, limite="100", email="ciclo@test.com", categoria="free", consumo="0"):
    if db.session.query(Conta).filter_by(slug=Conta.SLUG_SISTEMA).first() is None:
        seed_sistema_interno()
    if db.session.get(CleitonCostConfig, 1) is None:
        seed_cleiton_cost_config()
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    agora = utcnow_naive()
    franquia.limite_total = Decimal(limite)
    franquia.consumo_acumulado = Decimal(consumo)
    franquia.reserva_pendente = Decimal("0")
    franquia.inicio_ciclo = agora - timedelta(days=20)
    franquia.fim_ciclo = agora + timedelta(days=10)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email, categoria=categoria)
    return usuario, franquia


def _franquia(fid):
    db.session.expire_all()
    return db.session.get(Franquia, fid)


def _abrir(usuario, *, model="gemini-test", adiar=True, sufixo="1"):
    return cleiton_governed_billable_ai_call(
        _Client(),
        model=model,
        contents="tabela de frete",
        agent="agente_compara",
        flow_type="agente_compara_temp_table",
        api_key_label="test",
        usuario=usuario,
        attempt_key=f"{PREFIXO}{model}:{sufixo}",
        adiar_liquidacao=adiar,
    )


def _avancar(franquia_id, *, consumo="0"):
    franquia = db.session.get(Franquia, franquia_id)
    anterior = (franquia.inicio_ciclo, franquia.fim_ciclo)
    agora = utcnow_naive()
    franquia.inicio_ciclo = agora
    franquia.fim_ciclo = agora + timedelta(days=30)
    franquia.consumo_acumulado = Decimal(consumo)
    db.session.commit()
    return anterior


def _saldo(fid):
    return saldo_disponivel_chamada_ia(_franquia(fid))


def _pendurar(franquia, creditos="40"):
    reserva = Decimal(creditos)
    row = IaChamadaTentativa(
        attempt_key=f"reserva-ciclo-a-{franquia.id}",
        conta_id=franquia.conta_id,
        franquia_id=franquia.id,
        usuario_id=None,
        origem_sistema=False,
        agent="agente_compara",
        flow_type="agente_compara_temp_table",
        operation="generate_content",
        provider="gemini",
        model="gemini-test",
        ciclo_inicio=franquia.inicio_ciclo,
        ciclo_fim=franquia.fim_ciclo,
        reserved_credits=reserva,
        status=IaChamadaTentativa.STATUS_CALLING,
        reserva_ativa=True,
        debita_cliente=True,
        created_at=utcnow_naive(),
    )
    franquia.reserva_pendente = reserva
    franquia.consumo_acumulado = Decimal("9")
    db.session.add(row)
    db.session.commit()
    return row


def test_renovacao_com_reserva_pendente_nao_bloqueia_credito_novo(app):
    with app.app_context():
        _usuario, franquia = _seed(email="pendente@test.com", limite="100")
        _pendurar(franquia, "40")
        assert _saldo(franquia.id) == Decimal("51")
        _avancar(franquia.id, consumo="0")
        atual = _franquia(franquia.id)
        assert atual.reserva_pendente == Decimal("40")
        assert atual.consumo_acumulado == 0
        assert _saldo(franquia.id) == Decimal("100")
        orcamento = montar_orcamento_tentativa(model="gemini-2.5-flash", consumido=0)
        assert orcamento.autorizado == EXTRACAO_TETO_TECNICO_TOKENS


def test_reconciliacao_nao_reintroduz_consumo_antigo(app):
    with app.app_context():
        usuario, franquia = _seed(email="recon@test.com")
        inicio_a, fim_a = franquia.inicio_ciclo, franquia.fim_ciclo
        _avancar(franquia.id, consumo="99")
        atual = _franquia(franquia.id)
        inicio_b, fim_b = atual.inicio_ciclo, atual.fim_ciclo

        def evento(*, tokens, quando, usuario_id):
            row = IaConsumoEvento(
                occurred_at=quando,
                provider="gemini",
                operation="generate_content",
                model="gemini-test",
                agent="agente_compara",
                flow_type="agente_compara_temp_table",
                api_key_label="test",
                status="success",
                total_tokens=tokens,
                franquia_id=franquia.id,
                usuario_id=usuario_id,
                origem_sistema=False,
                tipo_origem="http_usuario",
            )
            db.session.add(row)
            db.session.flush()
            return row

        antigo = evento(tokens=5000, quando=inicio_b + timedelta(hours=1), usuario_id=usuario.id)
        db.session.add(
            IaChamadaTentativa(
                attempt_key="recon-ciclo-a",
                franquia_id=franquia.id,
                conta_id=franquia.conta_id,
                agent="agente_compara",
                flow_type="agente_compara_temp_table",
                operation="generate_content",
                provider="gemini",
                model="gemini-test",
                ciclo_inicio=inicio_a,
                ciclo_fim=fim_a,
                reserved_credits=Decimal("5"),
                status="settled",
                ia_consumo_evento_id=antigo.id,
                reserva_ativa=False,
                debita_cliente=True,
                created_at=inicio_a,
            )
        )
        marca = IaConsumoAbatimento(
            ia_consumo_evento_id=antigo.id,
            franquia_id=franquia.id,
            creditos=Decimal("5"),
            created_at=inicio_a,
        )
        db.session.add(marca)
        vigente = evento(tokens=1000, quando=inicio_b + timedelta(hours=2), usuario_id=usuario.id)
        db.session.add(
            IaChamadaTentativa(
                attempt_key="recon-ciclo-b",
                franquia_id=franquia.id,
                conta_id=franquia.conta_id,
                agent="agente_compara",
                flow_type="agente_compara_temp_table",
                operation="generate_content",
                provider="gemini",
                model="gemini-test",
                ciclo_inicio=inicio_b,
                ciclo_fim=fim_b,
                reserved_credits=Decimal("1"),
                status="settled",
                ia_consumo_evento_id=vigente.id,
                reserva_ativa=False,
                debita_cliente=True,
                created_at=inicio_b,
            )
        )
        marca_vigente = IaConsumoAbatimento(
            ia_consumo_evento_id=vigente.id,
            franquia_id=franquia.id,
            creditos=Decimal("1"),
            created_at=inicio_b,
        )
        db.session.add(marca_vigente)
        outro_agente = evento(tokens=1000, quando=inicio_b + timedelta(hours=3), usuario_id=usuario.id)
        anterior = evento(tokens=5000, quando=inicio_a, usuario_id=usuario.id)
        db.session.add(
            ProcessingEvent(
                occurred_at=inicio_a,
                agent="roberto",
                flow_type="upload_bi",
                processing_type="non_llm",
                rows_processed=200,
                processing_time_ms=0,
                status="success",
                franquia_id=franquia.id,
                usuario_id=usuario.id,
                origem_sistema=False,
                tipo_origem="http_usuario",
            )
        )
        db.session.add(
            ProcessingEvent(
                occurred_at=inicio_b + timedelta(hours=4),
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
        marca_vigente_id = marca_vigente.id
        tokens_antigos = antigo.total_tokens

        leitura = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=False)
        assert leitura.status == "divergente"
        assert leitura.total_recalculado == Decimal("3")
        assert _franquia(franquia.id).consumo_acumulado == Decimal("99")

        corrigida = reconciliar_franquia_cleiton(franquia.id, aplicar_correcao=True)
        assert corrigida.correcao_aplicada is True
        assert corrigida.status == "ok"
        assert _franquia(franquia.id).consumo_acumulado == Decimal("3")
        assert db.session.get(IaConsumoEvento, antigo.id).total_tokens == tokens_antigos
        assert db.session.get(IaConsumoAbatimento, marca_id).creditos == Decimal("5")
        assert db.session.get(IaConsumoAbatimento, marca_vigente_id).creditos == Decimal("1")
        assert db.session.get(IaConsumoEvento, outro_agente.id).total_tokens == 1000
        assert db.session.get(IaConsumoEvento, anterior.id).total_tokens == 5000
        assert db.session.get(IaConsumoEvento, vigente.id).total_tokens == 1000


def _vinculo(conta_id, plano):
    row = ContaMonetizacaoVinculo(
        conta_id=int(conta_id),
        provider="stripe",
        customer_id=f"cus-{plano}-{conta_id}",
        subscription_id=f"sub-{plano}-{conta_id}",
        price_id="price_teste",
        plano_interno=plano,
        status_contratual_externo="active",
        ativo=True,
        snapshot_normalizado_json="{}",
        payload_bruto_sanitizado_json="{}",
    )
    db.session.add(row)
    db.session.commit()
    return row


def test_renovacao_free_starter_pro_e_multiuser_preserva_limite(app):
    with app.app_context():
        agora = utcnow_naive()
        inicio_b = agora - timedelta(days=1)
        fim_b = agora + timedelta(days=27)

        _usuario, free = _seed(email="plano-free@test.com", categoria="pro", limite="17.5")
        pendente_free = _pendurar(free, "6")
        ciclo_free = (pendente_free.ciclo_inicio, pendente_free.ciclo_fim)
        _vinculo(free.conta_id, "pro")
        aplicar_fato_contratual_em_franquia(
            franquia_id=free.id,
            plano_codigo="free",
            event_type="customer.subscription.deleted",
            status_contratual_externo="canceled",
            ciclo={
                "inicio_ciclo": agora - timedelta(days=31),
                "fim_ciclo": inicio_b,
                "fonte_ciclo": "stripe_periodo_assinatura_confirmado",
                "pendencias": [],
            },
        )
        free_atual = _franquia(free.id)
        assert db.session.get(User, _usuario.id).categoria == "free"
        assert free_atual.limite_total == Decimal("17.5")
        assert free_atual.consumo_acumulado == 0
        assert free_atual.fim_ciclo is None
        assert free_atual.inicio_ciclo == inicio_b
        assert free_atual.reserva_pendente == Decimal("6")
        assert _saldo(free.id) == Decimal("17.5")
        assert db.session.get(IaChamadaTentativa, pendente_free.id).ciclo_inicio == ciclo_free[0]
        assert db.session.get(IaChamadaTentativa, pendente_free.id).ciclo_fim == ciclo_free[1]

        for plano, limite in (("starter", "23.5"), ("pro", "31.5")):
            usuario, franquia = _seed(
                email=f"plano-{plano}@test.com",
                categoria=plano,
                limite=limite,
            )
            pendente = _pendurar(franquia, "8")
            ciclo = (pendente.ciclo_inicio, pendente.ciclo_fim)
            _vinculo(franquia.conta_id, plano)
            resultado = aplicar_fato_contratual_em_franquia(
                franquia_id=franquia.id,
                plano_codigo=plano,
                event_type="invoice.paid",
                status_contratual_externo="active",
                ciclo={
                    "inicio_ciclo": inicio_b,
                    "fim_ciclo": fim_b,
                    "fonte_ciclo": "stripe_periodo_assinatura_confirmado",
                    "pendencias": [],
                },
            )
            atual = _franquia(franquia.id)
            assert resultado["consumo_reiniciado_renovacao"] is True
            assert db.session.get(User, usuario.id).categoria == plano
            assert atual.limite_total == Decimal(limite)
            assert atual.consumo_acumulado == 0
            assert atual.inicio_ciclo == inicio_b
            assert atual.fim_ciclo == fim_b
            assert atual.reserva_pendente == Decimal("8")
            assert _saldo(franquia.id) == Decimal(limite)
            assert db.session.get(IaChamadaTentativa, pendente.id).ciclo_inicio == ciclo[0]

        usuario_m, multi = _seed(
            email="plano-multiuser@test.com",
            categoria="multiuser",
            limite="47.5",
        )
        pendente_m = _pendurar(multi, "11")
        ciclo_m = (pendente_m.ciclo_inicio, pendente_m.ciclo_fim)
        aplicar_fanout_ciclo_confirmado(
            multi.conta_id,
            inicio_b,
            fim_b,
            resetar_consumo=True,
        )
        db.session.commit()
        atual_m = _franquia(multi.id)
        assert db.session.get(User, usuario_m.id).categoria == "multiuser"
        assert atual_m.limite_total == Decimal("47.5")
        assert atual_m.consumo_acumulado == 0
        assert atual_m.inicio_ciclo == inicio_b
        assert atual_m.fim_ciclo == fim_b
        assert atual_m.reserva_pendente == Decimal("11")
        assert _saldo(multi.id) == Decimal("47.5")
        assert db.session.get(IaChamadaTentativa, pendente_m.id).ciclo_fim == ciclo_m[1]


def _race_app(path):
    race = Flask("scrum146-ciclo")
    race.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + str(path).replace("\\", "/")
    race.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    race.config["TESTING"] = True
    race.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 30},
        "poolclass": NullPool,
    }
    db.init_app(race)
    return race


def test_concorrencia_durante_a_renovacao(tmp_path):
    race = _race_app(tmp_path / "ciclo.db")
    with race.app_context():
        import app.models  # noqa: F401

        db.create_all()
        usuario, franquia = _seed(email="corrida@test.com", categoria="multiuser", consumo="5")
        _outro, vizinha = _seed(email="corrida-vizinha@test.com", consumo="4")
        vizinha.reserva_pendente = Decimal("1")
        db.session.commit()
        resultado = _abrir(usuario)
        tentativa = db.session.get(IaChamadaTentativa, resultado.attempt_id)
        inicio_a = tentativa.ciclo_inicio
        fim_a = tentativa.ciclo_fim
        limite = franquia.limite_total
        inicio_b = fim_a
        fim_b = fim_a + timedelta(days=30)
        ids = {
            "conta": franquia.conta_id,
            "franquia": franquia.id,
            "tentativa": tentativa.id,
            "vizinha": vizinha.id,
        }
    erros = []
    barreira = threading.Barrier(2)

    def liquidar():
        with race.app_context():
            try:
                barreira.wait(timeout=10)
                liquidar_tentativa_ia(ids["tentativa"])
            except Exception as exc:
                erros.append(exc)
            finally:
                db.session.remove()

    def renovar():
        with race.app_context():
            try:
                barreira.wait(timeout=10)
                db.session.commit()
                db.session.execute(text("BEGIN IMMEDIATE"))
                aplicar_fanout_ciclo_confirmado(
                    ids["conta"],
                    inicio_b,
                    fim_b,
                    resetar_consumo=True,
                )
                db.session.commit()
            except Exception as exc:
                erros.append(exc)
                db.session.rollback()
            finally:
                db.session.remove()

    threads = [threading.Thread(target=liquidar), threading.Thread(target=renovar)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert erros == []
    with race.app_context():
        atual = _franquia(ids["franquia"])
        tentativa = db.session.get(IaChamadaTentativa, ids["tentativa"])
        vizinha = _franquia(ids["vizinha"])
        assert atual.inicio_ciclo == inicio_b
        assert atual.fim_ciclo == fim_b
        assert atual.consumo_acumulado == 0
        assert atual.limite_total == limite
        assert tentativa.ciclo_inicio == inicio_a
        assert tentativa.status == "settled"
        assert IaConsumoAbatimento.query.filter_by(franquia_id=ids["franquia"]).count() == 1
        assert vizinha.consumo_acumulado == Decimal("4")
        assert vizinha.reserva_pendente == Decimal("1")
        db.session.remove()
