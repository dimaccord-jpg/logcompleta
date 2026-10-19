"""SCRUM-238: teto do Gemini 2.5 e falha sem métricas no chat comparativo.

Gemini simulado e banco isolado. Não chama o provider real.
"""
from __future__ import annotations

import json
import threading
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy.pool import NullPool

from app.extensions import db
from app.models import (
    Franquia,
    IaChamadaTentativa,
    IaConsumoAbatimento,
    IaConsumoEvento,
    utcnow_naive,
)
from app.agente_compara_chat_prompt import build_comparison_chat_user_prompt
from app.run_agente_compara_comparison_chat import (
    ComparisonChatProviderError,
    _answer_response_config,
    _generate_governed_text,
    _selection_response_config,
)
from app.services.cleiton_billable_ai_call import (
    BILLABLE_AI_MAX_OUTPUT_TOKENS,
    BILLABLE_AI_MAX_THINKING_TOKENS,
    MOTIVO_FALHA_ASSUMIDA_PLATAFORMA,
    cleiton_governed_generate_content,
    custo_assumido_pela_plataforma,
    estimar_tokens_entrada,
    liquidar_tentativa_ia,
    resolver_limites_config,
    tokens_teto_total,
)
from app.services.cleiton_cost_service import get_or_create_config
from app.services.cleiton_franquia_operacional_service import (
    aplicar_motor_apos_ia_consumo_evento,
    converter_tokens_para_creditos,
)
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)

def _import_assumir():
    from app.services.cleiton_billable_ai_call import _assumir_falha_tecnica_sem_metricas as fn

    return fn


class GatewayTimeout(Exception):
    def __init__(self):
        super().__init__("HTTP 504 Gateway Timeout")


class _Models:
    def __init__(self, behavior):
        self.calls = []
        self.count_calls = []
        self._behavior = behavior

    def count_tokens(self, *, model, contents, config=None):
        self.count_calls.append({"model": model, "contents": contents, "config": config})
        tokens, erro = estimar_tokens_entrada(contents, config)
        if erro:
            raise RuntimeError("count_indisponivel")
        return SimpleNamespace(total_tokens=tokens)

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._behavior(model, contents, config)


class _Client:
    def __init__(self, behavior):
        self.models = _Models(behavior)
        self.http_options = SimpleNamespace(retry_options=SimpleNamespace(attempts=1))


def _usage(total: int):
    return SimpleNamespace(
        prompt_token_count=total,
        candidates_token_count=0,
        total_token_count=total,
    )


def _ok(model, contents, config):
    return SimpleNamespace(text="resposta oficial", usage_metadata=_usage(100))


def _seed(limite="100", email="scrum238@test.com", categoria="free"):
    seed_sistema_interno()
    seed_cleiton_cost_config()
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = Decimal(limite)
    franquia.consumo_acumulado = Decimal("0")
    franquia.reserva_pendente = Decimal("0")
    franquia.bloqueio_manual = False
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email, categoria=categoria)
    return usuario, franquia


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat._get_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.resolve_comparison_chat_model",
        lambda: "gemini-2.5-flash",
    )


def _franquia(fid):
    return db.session.get(Franquia, fid)


def _creditos(tokens: int) -> Decimal:
    valor, erro = converter_tokens_para_creditos(tokens, get_or_create_config())
    assert erro is None and valor is not None
    return valor


def _componentes(client):
    counted = client.models.count_calls[-1]
    entrada, erro = estimar_tokens_entrada(counted["contents"], counted["config"])
    assert erro is None
    saida, candidatos, thinking, erro_limite = resolver_limites_config(client.models.calls[-1]["config"])
    assert erro_limite is None
    return entrada, saida, candidatos, thinking


def _publicacao_analitica():
    return {
        "publication_version": "comparison_chat_publication_v1",
        "contract_version": "comparison_chat_contract_v1",
        "operation_id": "op-238",
        "comparison_id": "cmp-238",
        "expected_result_version": "resultado-1",
        "catalog_version": "catalogo-1",
        "status": "partial",
        "evidence_sufficient": False,
        "publication_complete": False,
        "losses": [{"code": "page_incomplete", "count": 2, "blocks": ["page_ranking"]}],
        "population": {"examined_rows": 10, "query_count": 1},
        "queries": [
            {
                "query_id": "q1",
                "query_type": "by_uf",
                "population": {"universe": "fully_comparable_rows", "excluded_rows": 3},
                "page": {"offset": 0, "limit": 50, "total": 80, "returned": 50},
                "losses": [{"code": "evidence_omitted", "count": 1, "blocks": ["potential_savings"]}],
                "records": [
                    {
                        "evidence_id": "ev-1",
                        "evidence_digest": "digest-1",
                        "publication_version": "comparison_chat_publication_v1",
                        "contract_version": "comparison_chat_contract_v1",
                        "operation_id": "op-238",
                        "comparison_id": "cmp-238",
                        "expected_result_version": "resultado-1",
                        "catalog_version": "catalogo-1",
                        "query_id": "q1",
                        "query_type": "by_uf",
                        "schema_id": "by_uf",
                        "memory_checksum": "memoria-238",
                        "record_role": "detail",
                        "ordinal": 0,
                        "population": {"universe": "fully_comparable_rows", "excluded_rows": 3},
                        "page": {"offset": 0, "limit": 50, "total": 80, "returned": 50},
                        "facts": {
                            "amount_text": "12.50",
                            "universe": "fully_comparable_rows",
                            "excluded_rows": 4,
                        },
                    },
                    {
                        "evidence_id": "ev-2",
                        "evidence_digest": "digest-2",
                        "publication_version": "comparison_chat_publication_v1",
                        "query_id": "q1",
                        "query_type": "by_uf",
                        "schema_id": "by_uf",
                        "record_role": "summary",
                        "population": {"universe": "all_operational_rows", "excluded_rows": 1},
                        "page": {"offset": 0, "limit": 10, "total": 80, "returned": 10},
                        "facts": {"wins": 6, "amount_text": "0.00"},
                    },
                ],
            }
        ],
    }


def test_padrao_global_das_outras_habilidades_permanece():
    assert BILLABLE_AI_MAX_OUTPUT_TOKENS == 8192
    assert BILLABLE_AI_MAX_THINKING_TOKENS == 8192
    assert resolver_limites_config(None) == (8192, 1, 8192, None)
    assert resolver_limites_config(_answer_response_config()) == (8192, 1, 0, None)
    selecao = _selection_response_config()
    assert selecao is not None
    assert resolver_limites_config(selecao) == (2048, 1, 0, None)


def test_504_na_selecao_encerra_reserva_sem_debito(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="selecao-504@test.com")
        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(GatewayTimeout()))
        _patch_client(monkeypatch, client)
        with pytest.raises(ComparisonChatProviderError):
            _generate_governed_text(
                prompt="selecione a consulta de economia",
                usuario=usuario,
                config=_selection_response_config(),
                billable_stage="semantic_selection",
            )
        atual = _franquia(franquia.id)
        tentativa = IaChamadaTentativa.query.one()
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        assert tentativa.reserva_ativa is False
        assert tentativa.status == "released"
        assert custo_assumido_pela_plataforma(tentativa)
        assert tentativa.failure_reason == f"{MOTIVO_FALHA_ASSUMIDA_PLATAFORMA}:provider_sem_usage"
        assert tentativa.actual_credits is None
        assert tentativa.ia_consumo_evento_id is not None
        evento = db.session.get(IaConsumoEvento, tentativa.ia_consumo_evento_id)
        assert evento is not None
        assert evento.status == "failure"
        assert evento.total_tokens is None
        assert IaConsumoAbatimento.query.count() == 0
        entrada, saida, candidatos, thinking = _componentes(client)
        assert (saida, candidatos, thinking) == (2048, 1, 0)
        assert tentativa.reserved_credits == _creditos(tokens_teto_total(entrada, saida, thinking, candidatos))
        repetido = _import_assumir()(tentativa.id, "provider_sem_usage")
        assert repetido == "ja_assumida"
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_504_na_resposta_analitica_encerra_reserva_sem_debito(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="analitica-504@test.com")
        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(GatewayTimeout()))
        _patch_client(monkeypatch, client)
        with pytest.raises(ComparisonChatProviderError):
            _generate_governed_text(
                prompt="explique a economia publicada",
                usuario=usuario,
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        atual = _franquia(franquia.id)
        tentativa = IaChamadaTentativa.query.one()
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        assert tentativa.reserva_ativa is False
        assert custo_assumido_pela_plataforma(tentativa)
        assert IaConsumoAbatimento.query.count() == 0
        entrada, saida, candidatos, thinking = _componentes(client)
        assert (saida, candidatos, thinking) == (8192, 1, 0)
        assert client.models.calls[-1]["model"] == "gemini-2.5-flash"
        teto = tokens_teto_total(entrada, saida, thinking, candidatos)
        dobro = tokens_teto_total(entrada, 8192, 8192, 1)
        assert teto == entrada + 8192
        assert dobro - teto == 8192
        assert tentativa.reserved_credits == _creditos(teto)
        assert tentativa.reserved_credits != _creditos(dobro)


def test_evento_tardio_nao_cobra_depois_do_encerramento(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="tardio@test.com")
        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(GatewayTimeout()))
        _patch_client(monkeypatch, client)
        with pytest.raises(ComparisonChatProviderError):
            _generate_governed_text(
                prompt="resposta analitica",
                usuario=usuario,
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        tentativa = IaChamadaTentativa.query.one()
        evento = db.session.get(IaConsumoEvento, tentativa.ia_consumo_evento_id)
        evento.input_tokens = 80
        evento.output_tokens = 20
        evento.total_tokens = 100
        db.session.commit()
        liquidar_tentativa_ia(tentativa.id)
        resultado = aplicar_motor_apos_ia_consumo_evento(evento.id)
        assert resultado.motivo_nao_abateu == "custo_assumido_plataforma"
        assert resultado.abateu_franquia is False
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        assert IaConsumoAbatimento.query.count() == 0
        preservada = db.session.get(IaChamadaTentativa, tentativa.id)
        assert custo_assumido_pela_plataforma(preservada)
        assert preservada.actual_credits is None
        assert preservada.ia_consumo_evento_id == evento.id


def test_sucesso_debita_o_consumo_real_uma_vez(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="sucesso@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        texto, model = _generate_governed_text(
            prompt="quanto foi a economia",
            usuario=usuario,
            config=_answer_response_config(),
            billable_stage="answer_generation",
        )
        assert texto == "resposta oficial"
        assert model == "gemini-2.5-flash"
        atual = _franquia(franquia.id)
        tentativa = IaChamadaTentativa.query.one()
        assert atual.consumo_acumulado == Decimal("0.1")
        assert atual.reserva_pendente == 0
        assert tentativa.status == "settled"
        assert tentativa.actual_credits == Decimal("0.1")
        assert tentativa.reserva_ativa is False
        assert tentativa.reserved_credits > tentativa.actual_credits
        assert IaConsumoAbatimento.query.count() == 1
        antes = atual.consumo_acumulado
        liquidar_tentativa_ia(tentativa.id)
        aplicar_motor_apos_ia_consumo_evento(tentativa.ia_consumo_evento_id)
        assert _franquia(franquia.id).consumo_acumulado == antes
        assert IaConsumoAbatimento.query.count() == 1
        entrada, saida, candidatos, thinking = _componentes(client)
        assert (saida, candidatos, thinking) == (8192, 1, 0)
        assert tentativa.reserved_credits == _creditos(
            tokens_teto_total(entrada, saida, thinking, candidatos)
        )


def test_saldo_insuficiente_recusa_antes_da_geracao(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="1", email="sem-saldo@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        with pytest.raises(ComparisonChatProviderError) as exc:
            _generate_governed_text(
                prompt="pergunta que nao pode reservar o teto",
                usuario=usuario,
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        assert exc.value.http_status == 403
        assert client.models.calls == []
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "blocked"
        assert tentativa.reserva_ativa is False
        assert tentativa.reserved_credits == 0


def test_falha_com_consumo_confirmado_liquida(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="com-usage@test.com")

        class Boom(RuntimeError):
            def __init__(self):
                super().__init__("provider falhou com metrica")
                self.usage_metadata = _usage(100)

        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(Boom()))
        _patch_client(monkeypatch, client)
        with pytest.raises(ComparisonChatProviderError):
            _generate_governed_text(
                prompt="falha com uso confirmado",
                usuario=usuario,
                config=_selection_response_config(),
                billable_stage="semantic_selection",
            )
        atual = _franquia(franquia.id)
        tentativa = IaChamadaTentativa.query.one()
        assert atual.consumo_acumulado == Decimal("0.1")
        assert atual.reserva_pendente == 0
        assert tentativa.status == "provider_failed"
        assert tentativa.actual_credits == Decimal("0.1")
        assert custo_assumido_pela_plataforma(tentativa) is False
        assert IaConsumoAbatimento.query.count() == 1


def test_outra_habilidade_sem_metricas_continua_incerta(app):
    with app.app_context():
        usuario, franquia = _seed(email="cleiton@test.com", categoria="pro")
        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(GatewayTimeout()))
        with pytest.raises(Exception):
            cleiton_governed_generate_content(
                client,
                model="gemini-2.5-flash",
                contents="habilidade que nao e o chat comparativo",
                agent="cleiton",
                flow_type="cleiton_chat",
                api_key_label="test",
                usuario=usuario,
            )
        atual = _franquia(franquia.id)
        tentativa = IaChamadaTentativa.query.one()
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente > 0
        assert tentativa.status == "uncertain"
        assert tentativa.failure_reason == "provider_sem_usage"
        assert tentativa.reserva_ativa is True
        assert custo_assumido_pela_plataforma(tentativa) is False
        entrada, saida, candidatos, thinking = _componentes(client)
        assert (saida, candidatos, thinking) == (8192, 1, 8192)
        assert tentativa.reserved_credits == _creditos(tokens_teto_total(entrada, saida, thinking, candidatos))


def _abrir_falha_sem_metricas(
    franquia,
    usuario,
    *,
    flow_type,
    status,
    reservado=Decimal("5"),
    pendente=Decimal("10"),
    failure_reason="provider_sem_usage",
    input_tokens=None,
    output_tokens=None,
    total_tokens=None,
):
    evento = IaConsumoEvento(
        provider="gemini",
        operation="generate_content",
        model="gemini-2.5-flash",
        agent="agente_compara",
        flow_type=flow_type,
        api_key_label="test",
        status="failure",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        franquia_id=franquia.id,
        usuario_id=usuario.id,
        conta_id=franquia.conta_id,
        origem_sistema=False,
        tipo_origem="http_usuario",
        regime_abatimento=IaConsumoEvento.REGIME_NOVO,
        error_summary="HTTP 504",
    )
    db.session.add(evento)
    db.session.flush()
    tentativa = IaChamadaTentativa(
        attempt_key=f"{flow_type}-{evento.id}",
        conta_id=franquia.conta_id,
        franquia_id=franquia.id,
        usuario_id=usuario.id,
        origem_sistema=False,
        agent="agente_compara",
        flow_type=flow_type,
        operation="generate_content",
        provider="gemini",
        model="gemini-2.5-flash",
        ciclo_inicio=franquia.inicio_ciclo,
        ciclo_fim=franquia.fim_ciclo,
        reserved_credits=reservado,
        status=status,
        ia_consumo_evento_id=evento.id,
        reserva_ativa=True,
        debita_cliente=True,
        failure_reason=failure_reason,
        created_at=utcnow_naive(),
    )
    franquia.reserva_pendente = pendente
    db.session.add(tentativa)
    db.session.commit()
    return tentativa, evento


def _afirmar_encerrada_pela_plataforma(tentativa_id, franquia_id, *, pendente):
    atual = _franquia(franquia_id)
    tentativa = db.session.get(IaChamadaTentativa, tentativa_id)
    assert atual.consumo_acumulado == 0
    assert atual.reserva_pendente == pendente
    assert tentativa.reserva_ativa is False
    assert tentativa.status == "released"
    assert tentativa.actual_credits is None
    assert custo_assumido_pela_plataforma(tentativa)
    assert tentativa.failure_reason == f"{MOTIVO_FALHA_ASSUMIDA_PLATAFORMA}:provider_sem_usage"
    assert IaConsumoAbatimento.query.count() == 0
    return tentativa


def _afirmar_metricas_tardias_sem_cobranca(tentativa_id, evento_id, franquia_id, *, pendente):
    evento = db.session.get(IaConsumoEvento, evento_id)
    evento.input_tokens = 80
    evento.output_tokens = 20
    evento.total_tokens = 100
    db.session.commit()
    liquidar_tentativa_ia(tentativa_id)
    resultado = aplicar_motor_apos_ia_consumo_evento(evento_id)
    assert resultado.abateu_franquia is False
    assert resultado.motivo_nao_abateu == "custo_assumido_plataforma"
    _afirmar_encerrada_pela_plataforma(tentativa_id, franquia_id, pendente=pendente)
    assert db.session.get(IaConsumoEvento, evento_id).total_tokens == 100


def test_liquidacao_antes_do_encerramento_nao_vira_zero(app):
    with app.app_context():
        usuario, franquia = _seed(email="liquidar-antes@test.com")
        tentativa, evento = _abrir_falha_sem_metricas(
            franquia,
            usuario,
            flow_type="agente_compara_comparison_chat",
            status=IaChamadaTentativa.STATUS_CALLING,
            failure_reason=None,
        )
        liquidada = liquidar_tentativa_ia(tentativa.id)
        assert liquidada is not None
        _afirmar_encerrada_pela_plataforma(tentativa.id, franquia.id, pendente=Decimal("5"))
        assert _import_assumir()(tentativa.id, "provider_sem_usage") == "ja_assumida"
        repetida = liquidar_tentativa_ia(tentativa.id)
        assert repetida.status == "released"
        assert _franquia(franquia.id).reserva_pendente == Decimal("5")
        assert _franquia(franquia.id).consumo_acumulado == 0
        _afirmar_metricas_tardias_sem_cobranca(
            tentativa.id,
            evento.id,
            franquia.id,
            pendente=Decimal("5"),
        )


def test_rollback_entre_flush_e_commit_recupera_sem_zero(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="rollback-commit@test.com")
        tentativa, evento = _abrir_falha_sem_metricas(
            franquia,
            usuario,
            flow_type="agente_compara_comparison_chat",
            status=IaChamadaTentativa.STATUS_UNCERTAIN,
        )
        import app.services.cleiton_billable_ai_call as billable

        original = billable._commit_session
        chamadas = {"n": 0}

        def _commit_interrompido():
            chamadas["n"] += 1
            if chamadas["n"] > 1:
                raise RuntimeError("commit_interrompido")
            return original()

        monkeypatch.setattr(billable, "_commit_session", _commit_interrompido)
        with pytest.raises(RuntimeError, match="commit_interrompido"):
            billable._assumir_falha_tecnica_sem_metricas(tentativa.id, "provider_sem_usage")
        monkeypatch.setattr(billable, "_commit_session", original)
        db.session.rollback()
        preservada = db.session.get(IaChamadaTentativa, tentativa.id)
        assert preservada.status == "uncertain"
        assert preservada.reserva_ativa is True
        assert preservada.failure_reason == "provider_sem_usage"
        assert preservada.actual_credits is None
        assert preservada.settled_at is None
        assert _franquia(franquia.id).reserva_pendente == Decimal("10")
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert custo_assumido_pela_plataforma(preservada) is False
        from app.services.cleiton_billable_ai_call import _marcar_incerta

        _marcar_incerta(tentativa.id, "liquidacao_interrompida")
        apos_marca = db.session.get(IaChamadaTentativa, tentativa.id)
        assert apos_marca.status == "uncertain"
        assert apos_marca.reserva_ativa is True
        assert _franquia(franquia.id).reserva_pendente == Decimal("10")
        liquidada = liquidar_tentativa_ia(tentativa.id)
        assert liquidada is not None
        assert liquidada.actual_credits is None
        _afirmar_encerrada_pela_plataforma(tentativa.id, franquia.id, pendente=Decimal("5"))
        assert liquidar_tentativa_ia(tentativa.id).status == "released"
        assert _franquia(franquia.id).reserva_pendente == Decimal("5")
        _afirmar_metricas_tardias_sem_cobranca(
            tentativa.id,
            evento.id,
            franquia.id,
            pendente=Decimal("5"),
        )


def test_liquidar_nao_libera_operacao_em_andamento(app):
    with app.app_context():
        usuario, franquia = _seed(email="liquidar-andamento@test.com")
        tentativa, _evento = _abrir_falha_sem_metricas(
            franquia,
            usuario,
            flow_type="agente_compara_comparison_chat",
            status=IaChamadaTentativa.STATUS_RESERVED,
        )
        liquidada = liquidar_tentativa_ia(tentativa.id)
        assert liquidada.status == "reserved"
        assert liquidada.reserva_ativa is True
        assert liquidada.actual_credits is None
        assert custo_assumido_pela_plataforma(liquidada) is False
        assert _franquia(franquia.id).reserva_pendente == Decimal("10")
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert IaConsumoAbatimento.query.count() == 0


def test_consumo_confirmado_no_chat_continua_sendo_cobrado(app):
    with app.app_context():
        usuario, franquia = _seed(email="metricas-presentes@test.com")
        tentativa, _evento = _abrir_falha_sem_metricas(
            franquia,
            usuario,
            flow_type="agente_compara_comparison_chat",
            status=IaChamadaTentativa.STATUS_CALLING,
            input_tokens=80,
            output_tokens=20,
            total_tokens=100,
        )
        liquidada = liquidar_tentativa_ia(tentativa.id)
        assert liquidada.status == "settled"
        assert liquidada.actual_credits == Decimal("0.1")
        assert custo_assumido_pela_plataforma(liquidada) is False
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.1")
        assert _franquia(franquia.id).reserva_pendente == Decimal("5")
        assert IaConsumoAbatimento.query.count() == 1
        liquidar_tentativa_ia(tentativa.id)
        aplicar_motor_apos_ia_consumo_evento(tentativa.ia_consumo_evento_id)
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.1")
        assert _franquia(franquia.id).reserva_pendente == Decimal("5")
        assert IaConsumoAbatimento.query.count() == 1


def test_outra_habilidade_liquidada_sem_a_politica_do_chat(app):
    with app.app_context():
        usuario, franquia = _seed(email="outra-liquidacao@test.com", categoria="pro")
        tentativa, _evento = _abrir_falha_sem_metricas(
            franquia,
            usuario,
            flow_type="cleiton_chat",
            status=IaChamadaTentativa.STATUS_CALLING,
        )
        liquidada = liquidar_tentativa_ia(tentativa.id)
        assert liquidada.status == "settled"
        assert liquidada.actual_credits == 0
        assert liquidada.reserva_ativa is False
        assert custo_assumido_pela_plataforma(liquidada) is False
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == Decimal("5")
        assert IaConsumoAbatimento.query.count() == 0


def test_reserva_em_andamento_nao_e_liberada(app):
    with app.app_context():
        _usuario, franquia = _seed(email="andamento@test.com")
        assumir = _import_assumir()
        row = IaChamadaTentativa(
            attempt_key="reserva-em-andamento",
            conta_id=franquia.conta_id,
            franquia_id=franquia.id,
            usuario_id=None,
            origem_sistema=False,
            agent="agente_compara",
            flow_type="agente_compara_comparison_chat",
            operation="generate_content",
            provider="gemini",
            model="gemini-2.5-flash",
            reserved_credits=Decimal("5"),
            status=IaChamadaTentativa.STATUS_RESERVED,
            reserva_ativa=True,
            debita_cliente=True,
            created_at=utcnow_naive(),
        )
        franquia.reserva_pendente = Decimal("5")
        db.session.add(row)
        db.session.commit()
        assert assumir(row.id, "provider_sem_usage") == "em_andamento"
        preservada = db.session.get(IaChamadaTentativa, row.id)
        assert preservada.status == "reserved"
        assert preservada.reserva_ativa is True
        assert _franquia(franquia.id).reserva_pendente == Decimal("5")
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_compactacao_preserva_o_conteudo_analitico():
    publication = _publicacao_analitica()
    before = json.dumps(publication, ensure_ascii=False, sort_keys=True)
    prompt = build_comparison_chat_user_prompt(
        user_message="Qual a economia e o que ficou de fora?",
        history_slice=[],
        context_payload={},
        scope="overview",
        evidence_publication=publication,
        confirmed_referents={"carrier": None, "uf": None, "ranked_rows": []},
    )
    assert json.dumps(publication, ensure_ascii=False, sort_keys=True) == before
    body = prompt.split("Publicação oficial comparison_chat_publication_v1.", 1)[1]
    start = body.index("{")
    published = json.loads(body[start : body.find("\n\nReferentes confirmados", start)])
    registro = published["queries"][0]["records"][0]
    distinto = published["queries"][0]["records"][1]
    assert "publication_version" not in registro
    assert "query_id" not in registro
    assert "schema_id" not in registro
    assert "page" not in registro
    assert "population" not in registro
    assert registro["evidence_id"] == "ev-1"
    assert registro["evidence_digest"] == "digest-1"
    assert registro["memory_checksum"] == "memoria-238"
    assert registro["facts"]["amount_text"] == "12.50"
    assert registro["facts"]["excluded_rows"] == 4
    assert published["queries"][0]["population"]["universe"] == "fully_comparable_rows"
    assert published["queries"][0]["population"]["excluded_rows"] == 3
    assert published["queries"][0]["losses"][0]["code"] == "evidence_omitted"
    assert published["losses"][0]["code"] == "page_incomplete"
    assert distinto["population"]["universe"] == "all_operational_rows"
    assert distinto["page"]["returned"] == 10
    assert distinto["evidence_id"] == "ev-2"
    assert distinto["evidence_digest"] == "digest-2"
    assert distinto["facts"]["wins"] == 6
    assert publication["queries"][0]["records"][0]["publication_version"] == "comparison_chat_publication_v1"


def _race_app(path):
    race = Flask("scrum238-race")
    uri = "sqlite:///" + str(path).replace("\\", "/")
    race.config["SQLALCHEMY_DATABASE_URI"] = uri
    race.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    race.config["TESTING"] = True
    race.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 30},
        "poolclass": NullPool,
    }
    db.init_app(race)
    return race


def test_concorrencia_entre_falha_liquidacao_e_evento_tardio(tmp_path):
    race = _race_app(tmp_path / "scrum238-race.db")
    with race.app_context():
        import app.models  # noqa: F401

        db.create_all()
        usuario, franquia = _seed(email="corrida@test.com")
        evento = IaConsumoEvento(
            provider="gemini",
            operation="generate_content",
            model="gemini-2.5-flash",
            agent="agente_compara",
            flow_type="agente_compara_comparison_chat",
            api_key_label="test",
            status="failure",
            franquia_id=franquia.id,
            usuario_id=usuario.id,
            conta_id=franquia.conta_id,
            origem_sistema=False,
            tipo_origem="http_usuario",
            regime_abatimento=IaConsumoEvento.REGIME_NOVO,
            error_summary="HTTP 504",
        )
        db.session.add(evento)
        db.session.flush()
        reserva = Decimal("5.000000")
        tentativa = IaChamadaTentativa(
            attempt_key="corrida-238",
            conta_id=franquia.conta_id,
            franquia_id=franquia.id,
            usuario_id=usuario.id,
            origem_sistema=False,
            agent="agente_compara",
            flow_type="agente_compara_comparison_chat",
            operation="generate_content",
            provider="gemini",
            model="gemini-2.5-flash",
            ciclo_inicio=franquia.inicio_ciclo,
            ciclo_fim=franquia.fim_ciclo,
            reserved_credits=reserva,
            status=IaChamadaTentativa.STATUS_CALLING,
            ia_consumo_evento_id=evento.id,
            reserva_ativa=True,
            debita_cliente=True,
            created_at=utcnow_naive(),
        )
        franquia.reserva_pendente = reserva
        db.session.add(tentativa)
        db.session.commit()
        attempt_id = tentativa.id
        evento_id = evento.id
        franquia_id = franquia.id
    assumir = _import_assumir()
    barrier = threading.Barrier(3)
    resultados = []

    def _assumir_thread():
        with race.app_context():
            barrier.wait(timeout=10)
            try:
                resultados.append(("assumir", assumir(attempt_id, "provider_sem_usage")))
            except Exception as exc:
                resultados.append(("assumir", exc.__class__.__name__))
            finally:
                db.session.remove()

    def _liquidar_thread():
        with race.app_context():
            barrier.wait(timeout=10)
            try:
                linha = db.session.get(IaConsumoEvento, evento_id)
                linha.total_tokens = 100
                linha.input_tokens = 80
                linha.output_tokens = 20
                db.session.commit()
                liquidar_tentativa_ia(attempt_id)
                resultados.append(("liquidar", "ok"))
            except Exception as exc:
                resultados.append(("liquidar", exc.__class__.__name__))
            finally:
                db.session.remove()

    def _motor_thread():
        with race.app_context():
            barrier.wait(timeout=10)
            try:
                linha = db.session.get(IaConsumoEvento, evento_id)
                linha.total_tokens = 100
                linha.input_tokens = 80
                linha.output_tokens = 20
                db.session.commit()
                resultado = aplicar_motor_apos_ia_consumo_evento(evento_id)
                resultados.append(("motor", resultado.motivo_nao_abateu or "abateu"))
            except Exception as exc:
                resultados.append(("motor", exc.__class__.__name__))
            finally:
                db.session.remove()

    threads = [
        threading.Thread(target=_assumir_thread),
        threading.Thread(target=_liquidar_thread),
        threading.Thread(target=_motor_thread),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    with race.app_context():
        atual = db.session.get(Franquia, franquia_id)
        tentativa = db.session.get(IaChamadaTentativa, attempt_id)
        abatimentos = IaConsumoAbatimento.query.count()
        assert atual.reserva_pendente == 0
        assert atual.reserva_pendente >= 0
        assert abatimentos <= 1
        if custo_assumido_pela_plataforma(tentativa):
            assert atual.consumo_acumulado == 0
            assert abatimentos == 0
            assert tentativa.actual_credits is None
            assert tentativa.reserva_ativa is False
        else:
            assert tentativa.status in {"settled", "provider_failed"}
            assert abatimentos == 1
            assert atual.consumo_acumulado == Decimal("0.1")
            assert tentativa.reserva_ativa is False
            assert IaConsumoAbatimento.query.one().creditos == Decimal("0.1")
        segunda = assumir(attempt_id, "provider_sem_usage")
        assert segunda in {"ja_assumida", "ja_liquidada", "com_metricas"}
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == atual.consumo_acumulado
        assert db.session.get(Franquia, franquia_id).reserva_pendente == 0
        assert IaConsumoAbatimento.query.count() == abatimentos
    assert len(resultados) == 3


def test_concorrencia_sem_metricas_encerra_uma_vez(tmp_path):
    race = _race_app(tmp_path / "scrum238-sem-metricas.db")
    with race.app_context():
        import app.models  # noqa: F401

        db.create_all()
        usuario, franquia = _seed(email="corrida-sem-metricas@test.com")
        tentativa, evento = _abrir_falha_sem_metricas(
            franquia,
            usuario,
            flow_type="agente_compara_comparison_chat",
            status=IaChamadaTentativa.STATUS_CALLING,
            failure_reason=None,
        )
        attempt_id = tentativa.id
        evento_id = evento.id
        franquia_id = franquia.id
    assumir = _import_assumir()
    barrier = threading.Barrier(2)
    resultados = []

    def _assumir_thread():
        with race.app_context():
            barrier.wait(timeout=10)
            try:
                resultados.append(("assumir", assumir(attempt_id, "provider_sem_usage")))
            except Exception as exc:
                resultados.append(("assumir", exc.__class__.__name__))
            finally:
                db.session.remove()

    def _liquidar_thread():
        with race.app_context():
            barrier.wait(timeout=10)
            try:
                liquidar_tentativa_ia(attempt_id)
                resultados.append(("liquidar", "ok"))
            except Exception as exc:
                resultados.append(("liquidar", exc.__class__.__name__))
            finally:
                db.session.remove()

    threads = [
        threading.Thread(target=_assumir_thread),
        threading.Thread(target=_liquidar_thread),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    with race.app_context():
        atual = db.session.get(Franquia, franquia_id)
        tentativa = db.session.get(IaChamadaTentativa, attempt_id)
        assert custo_assumido_pela_plataforma(tentativa)
        assert tentativa.status == "released"
        assert tentativa.reserva_ativa is False
        assert tentativa.actual_credits is None
        assert atual.reserva_pendente == Decimal("5")
        assert atual.consumo_acumulado == 0
        assert IaConsumoAbatimento.query.count() == 0
        evento = db.session.get(IaConsumoEvento, evento_id)
        evento.input_tokens = 80
        evento.output_tokens = 20
        evento.total_tokens = 100
        db.session.commit()
        liquidar_tentativa_ia(attempt_id)
        resultado = aplicar_motor_apos_ia_consumo_evento(evento_id)
        assert resultado.abateu_franquia is False
        assert resultado.motivo_nao_abateu == "custo_assumido_plataforma"
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == 0
        assert db.session.get(Franquia, franquia_id).reserva_pendente == Decimal("5")
        assert IaConsumoAbatimento.query.count() == 0
        assert assumir(attempt_id, "provider_sem_usage") == "ja_assumida"
    assert len(resultados) == 2
