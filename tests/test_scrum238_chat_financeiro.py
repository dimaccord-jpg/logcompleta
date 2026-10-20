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
from app.agente_compara_chat_query_catalog import CATALOG_VERSION
from app.agente_compara_chat_semantic_selection import SELECTION_VERSION
from app.agente_compara_comparison_state import AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY
from app.run_agente_compara_comparison_chat import (
    ERROR_ADMISSION_BLOCKED,
    ERROR_FRANCHISE_EXHAUSTED,
    ERROR_PLAN_BLOCKED,
    ERROR_PLAN_EXPIRED,
    MSG_ADMISSION_BLOCKED,
    MSG_PLAN_BLOCKED_MANUAL,
    MSG_PLAN_EXPIRED,
    ComparisonChatProviderError,
    _answer_response_config,
    _generate_governed_text,
    _selection_response_config,
    chat_agente_compara_comparison_reply,
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


def _assert_contrato_gemini(chamada, *, contents, saida, mime=None, temperature=None):
    """Mesmo payload da base a8c09ed: sem teto objetivo e sem transporte extra."""

    assert chamada["model"] == "gemini-2.5-flash"
    assert chamada["contents"] == contents
    dump = chamada["config"].model_dump(exclude_none=True)
    assert dump["max_output_tokens"] == saida
    assert dump["candidate_count"] == 1
    assert dump["thinking_config"]["thinking_budget"] == 0
    assert "http_options" not in dump
    if mime is None:
        assert set(dump) == {"candidate_count", "max_output_tokens", "thinking_config"}
    else:
        assert dump["response_mime_type"] == mime
        assert dump["temperature"] == temperature
        assert dump["tools"] == []
        assert dump["automatic_function_calling"]["disable"] is True
    saida_lida, candidatos, thinking, erro = resolver_limites_config(chamada["config"])
    assert erro is None
    assert (saida_lida, candidatos, thinking) == (saida, 1, 0)


def test_504_na_selecao_nao_reserva_nem_debita(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="selecao-504@test.com")
        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(GatewayTimeout()))
        _patch_client(monkeypatch, client)
        prompt = "selecione a consulta de economia"
        with pytest.raises(ComparisonChatProviderError):
            _generate_governed_text(
                prompt=prompt,
                usuario=usuario,
                config=_selection_response_config(),
                billable_stage="semantic_selection",
            )
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 0
        assert client.models.count_calls == []
        assert len(client.models.calls) == 1
        _assert_contrato_gemini(
            client.models.calls[0],
            contents=prompt,
            saida=2048,
            mime="application/json",
            temperature=0,
        )
        evento = IaConsumoEvento.query.one()
        assert evento.status == "failure"
        assert evento.total_tokens is None
        assert evento.franquia_id == franquia.id
        assert evento.usuario_id == usuario.id
        assert evento.flow_type == "agente_compara_comparison_chat"
        assert evento.origem_sistema is False
        assert IaConsumoAbatimento.query.count() == 0
        repetido = aplicar_motor_apos_ia_consumo_evento(evento.id)
        assert repetido.abateu_franquia is False
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0


def test_504_na_resposta_analitica_nao_reserva_nem_debita(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="analitica-504@test.com")
        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(GatewayTimeout()))
        _patch_client(monkeypatch, client)
        prompt = "explique a economia publicada"
        with pytest.raises(ComparisonChatProviderError):
            _generate_governed_text(
                prompt=prompt,
                usuario=usuario,
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        _assert_contrato_gemini(client.models.calls[-1], contents=prompt, saida=8192)


def test_falha_sem_metricas_nao_debita_nem_cria_reserva(app, monkeypatch):
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
        assert IaChamadaTentativa.query.count() == 0
        evento = IaConsumoEvento.query.one()
        assert evento.input_tokens is None
        assert evento.output_tokens is None
        assert evento.total_tokens is None
        resultado = aplicar_motor_apos_ia_consumo_evento(evento.id)
        assert resultado.abateu_franquia is False
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        assert IaConsumoAbatimento.query.count() == 0


def test_sucesso_debita_o_consumo_real_uma_vez(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="sucesso@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        prompt = "quanto foi a economia"
        texto, model = _generate_governed_text(
            prompt=prompt,
            usuario=usuario,
            config=_answer_response_config(),
            billable_stage="answer_generation",
        )
        assert texto == "resposta oficial"
        assert model == "gemini-2.5-flash"
        atual = _franquia(franquia.id)
        assert IaChamadaTentativa.query.count() == 0
        assert atual.consumo_acumulado == Decimal("0.1")
        assert atual.reserva_pendente == 0
        assert IaConsumoAbatimento.query.count() == 1
        evento = IaConsumoEvento.query.one()
        assert evento.franquia_id == franquia.id
        assert evento.usuario_id == usuario.id
        assert evento.status == "success"
        assert evento.total_tokens == 100
        abatimento = IaConsumoAbatimento.query.one()
        assert abatimento.ia_consumo_evento_id == evento.id
        assert abatimento.creditos == Decimal("0.1")
        antes = atual.consumo_acumulado
        repetido = aplicar_motor_apos_ia_consumo_evento(evento.id)
        assert repetido.abateu_franquia is False
        assert repetido.motivo_nao_abateu == "ja_apropriado"
        assert _franquia(franquia.id).consumo_acumulado == antes
        assert IaConsumoAbatimento.query.count() == 1
        assert client.models.count_calls == []
        _assert_contrato_gemini(client.models.calls[-1], contents=prompt, saida=8192)


def test_saldo_parcial_nao_reserva_e_debita_o_consumo_real(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="1", email="sem-saldo@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        texto, _model = _generate_governed_text(
            prompt="pergunta dentro da franquia ativa",
            usuario=usuario,
            config=_answer_response_config(),
            billable_stage="answer_generation",
        )
        assert texto == "resposta oficial"
        assert len(client.models.calls) == 1
        assert client.models.count_calls == []
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == Decimal("0.1")
        assert atual.reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 1


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
        assert IaChamadaTentativa.query.count() == 0
        assert atual.consumo_acumulado == Decimal("0.1")
        assert atual.reserva_pendente == 0
        evento = IaConsumoEvento.query.one()
        assert evento.status == "failure"
        assert evento.total_tokens == 100
        assert IaConsumoAbatimento.query.one().creditos == Decimal("0.1")
        repetido = aplicar_motor_apos_ia_consumo_evento(evento.id)
        assert repetido.abateu_franquia is False
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.1")


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


PERGUNTA_OBJETIVA = (
    "Qual transportadora apresenta o menor frete nesta comparação? "
    "Responda apenas com o nome e o valor."
)
PERGUNTA_COMPLEXA = (
    "Explique os componentes de frete e a oportunidade de negociação por UF."
)
def _selection_oficial(query_type="official_summary", query_id="menor"):
    return json.dumps(
        {
            "selection_version": SELECTION_VERSION,
            "action": "query",
            "clarification": None,
            "catalog_version": CATALOG_VERSION,
            "queries": [{"query_id": query_id, "type": query_type}],
            "referent_criterion": "lowest_comparable_freight",
        },
        ensure_ascii=False,
    )


class _Fluxo:
    def __init__(self, *, inflar_resposta=False, falhar_resposta=False):
        self.calls = []
        self.count_calls = []
        self.inflar_resposta = inflar_resposta
        self.falhar_resposta = falhar_resposta

    def count_tokens(self, *, model, contents, config=None):
        self.count_calls.append({"model": model, "contents": contents, "config": config})
        if self.inflar_resposta and isinstance(contents, str) and "Seleção de consultas" not in contents:
            return SimpleNamespace(total_tokens=100_000)
        tokens, erro = estimar_tokens_entrada(contents, config)
        if erro:
            raise RuntimeError("count_indisponivel")
        return SimpleNamespace(total_tokens=tokens)

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        selecao = isinstance(contents, str) and "Seleção de consultas" in contents
        if not selecao and self.falhar_resposta:
            raise GatewayTimeout()
        if selecao:
            texto = _selection_oficial(
                "official_summary" if PERGUNTA_OBJETIVA in contents else "freight_components",
                "menor" if PERGUNTA_OBJETIVA in contents else "componentes",
            )
        else:
            texto = "Transportadora Alfa: R$ 10,00."
        return SimpleNamespace(
            text=texto,
            usage_metadata=_usage(80),
            candidates=[SimpleNamespace(finish_reason="STOP")],
        )


def _cliente_fluxo(*, inflar_resposta=False, falhar_resposta=False):
    models = _Fluxo(inflar_resposta=inflar_resposta, falhar_resposta=falhar_resposta)
    return SimpleNamespace(
        models=models,
        http_options=SimpleNamespace(retry_options=SimpleNamespace(attempts=1)),
    ), models


def _preparar_comparacao(monkeypatch, usuario, franquia):
    from tests.test_agente_compara_chat_analytical_bridge import _ready_context

    ready = _ready_context(monkeypatch)
    state = ready["session"][AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY]
    state["conta_id"] = usuario.conta_id
    state["usuario_id"] = usuario.id
    state["franquia_id"] = franquia.id
    monkeypatch.setattr(
        "app.run_agente_compara_comparison_chat.get_agente_compara_config",
        lambda: SimpleNamespace(question_max_chars=4000, chat_max_history=10),
    )
    return ready


@pytest.fixture
def storage_env(tmp_path, monkeypatch):
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_store
    import time

    import app.agente_compara_calculation_result_storage as storage_mod

    patch_cleiton_doc_store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "app.agente_compara_calculation_result_storage.get_cleiton_doc_tmp_dir",
        lambda: str(tmp_path),
    )
    monkeypatch.setattr(storage_mod, "_calc_storage_sweep_monotonic", time.monotonic())
    return tmp_path


def _rodar(
    monkeypatch,
    *,
    limite,
    pergunta,
    client,
    email,
    consumo="0",
    bloqueio=False,
    ciclo_vencido=False,
):
    usuario, franquia = _seed(limite=limite, email=email)
    franquia.consumo_acumulado = Decimal(consumo)
    franquia.bloqueio_manual = bloqueio
    if ciclo_vencido:
        franquia.fim_ciclo = utcnow_naive() - timedelta(days=1)
    db.session.commit()
    ready = _preparar_comparacao(monkeypatch, usuario, franquia)
    _patch_client(monkeypatch, client)
    resposta = chat_agente_compara_comparison_reply(
        pergunta,
        [],
        session_obj=ready["session"],
        usuario=usuario,
        comparison_id=ready["comparison_id"],
        request_id=f"scrum238-{email}",
        entrega_ja_avaliada=True,
    )
    return usuario, franquia, resposta


def test_prompt_e_config_gemini_equivalem_a_base():
    from app.agente_compara_chat_prompt import build_comparison_chat_selection_prompt
    from app.agente_compara_chat_semantic_selection import selection_catalog_document

    prompt = build_comparison_chat_selection_prompt(
        user_message=PERGUNTA_OBJETIVA,
        history_slice=[],
        referents={},
        authorized_tables=[{"table_id": "t1", "slot_number": 1, "carrier_name": "Alfa"}],
        catalog=selection_catalog_document(),
    )
    assert "Menor frete, somente nome e valor" not in prompt
    assert "use somente official_summary" not in prompt
    assert "Resumo executivo e prioridade de investigação" in prompt
    resposta = _answer_response_config().model_dump(exclude_none=True)
    assert resposta["max_output_tokens"] == 8192
    assert resposta["candidate_count"] == 1
    assert resposta["thinking_config"]["thinking_budget"] == 0
    assert "http_options" not in resposta
    assert "automatic_function_calling" not in resposta
    selecao = _selection_response_config().model_dump(exclude_none=True)
    assert selecao["max_output_tokens"] == 2048
    assert selecao["temperature"] == 0
    assert "http_options" not in selecao
    assert resolver_limites_config(_answer_response_config()) == (8192, 1, 0, None)
    assert resolver_limites_config(_selection_response_config()) == (2048, 1, 0, None)


def test_chamada_interna_sem_usuario_nao_gera_nem_grava_evento(app, monkeypatch):
    with app.app_context():
        _usuario_row, franquia = _seed(email="interno-sem-usuario@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        with pytest.raises(ComparisonChatProviderError) as exc:
            _generate_governed_text(
                prompt="chamada interna sem operador",
                usuario=None,
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        assert exc.value.error_code == ERROR_ADMISSION_BLOCKED
        assert exc.value.message == MSG_ADMISSION_BLOCKED
        assert client.models.calls == []
        assert client.models.count_calls == []
        assert IaConsumoEvento.query.count() == 0
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        for item in Franquia.query.all():
            assert item.consumo_acumulado == 0
            assert item.reserva_pendente == 0


def test_identidade_incompleta_nao_gera_nem_grava_evento(app, monkeypatch):
    with app.app_context():
        _usuario_row, franquia = _seed(email="identidade-incompleta@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        incompleto = SimpleNamespace(
            id=1,
            conta_id=1,
            franquia_id=None,
            is_authenticated=True,
        )
        with pytest.raises(ComparisonChatProviderError) as exc:
            _generate_governed_text(
                prompt="identidade sem franquia",
                usuario=incompleto,
                config=_selection_response_config(),
                billable_stage="semantic_selection",
            )
        assert exc.value.error_code == ERROR_ADMISSION_BLOCKED
        assert client.models.calls == []
        assert IaConsumoEvento.query.count() == 0
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0


def test_chamada_interna_com_franquia_real_nao_contorna_autorizacao(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="interno-franquia-real@test.com")
        franquia.bloqueio_manual = True
        db.session.commit()
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        interno = SimpleNamespace(
            id=usuario.id,
            conta_id=usuario.conta_id,
            franquia_id=franquia.id,
            is_authenticated=True,
        )
        with pytest.raises(ComparisonChatProviderError) as exc:
            _generate_governed_text(
                prompt="chamada interna com franquia bloqueada",
                usuario=interno,
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        assert exc.value.error_code == ERROR_PLAN_BLOCKED
        assert exc.value.message == MSG_PLAN_BLOCKED_MANUAL
        assert client.models.calls == []
        assert IaConsumoEvento.query.count() == 0
        assert IaChamadaTentativa.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0


def _objeto_autenticado(usuario, franquia_id):
    return SimpleNamespace(
        id=usuario.id,
        conta_id=usuario.conta_id,
        franquia_id=franquia_id,
        is_authenticated=True,
    )


def _assert_chamada_recusada_sem_efeito(client, franquia_id):
    assert client.models.calls == []
    assert client.models.count_calls == []
    assert IaConsumoEvento.query.count() == 0
    assert IaChamadaTentativa.query.count() == 0
    assert IaConsumoAbatimento.query.count() == 0
    atual = _franquia(franquia_id)
    assert atual.consumo_acumulado == 0
    assert atual.reserva_pendente == 0


def test_chamada_interna_com_franquia_inexistente_nao_gera_nem_grava_evento(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="interno-franquia-ausente@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        with pytest.raises(ComparisonChatProviderError) as exc:
            _generate_governed_text(
                prompt="franquia inexistente",
                usuario=_objeto_autenticado(usuario, 999999),
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        assert exc.value.error_code == ERROR_ADMISSION_BLOCKED
        assert exc.value.message == MSG_ADMISSION_BLOCKED
        _assert_chamada_recusada_sem_efeito(client, franquia.id)
        assert db.session.get(Franquia, 999999) is None


def test_falha_na_consulta_ou_autorizacao_nao_gera_nem_grava_evento(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="interno-consulta-falha@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)

        def _consulta_indisponivel(_usuario):
            raise RuntimeError("consulta indisponivel")

        monkeypatch.setattr(
            "app.run_agente_compara_comparison_chat._franquia_persistida",
            _consulta_indisponivel,
        )
        with pytest.raises(ComparisonChatProviderError) as consulta:
            _generate_governed_text(
                prompt="consulta da franquia indisponivel",
                usuario=_objeto_autenticado(usuario, franquia.id),
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        assert consulta.value.error_code == ERROR_ADMISSION_BLOCKED
        assert consulta.value.message == MSG_ADMISSION_BLOCKED
        _assert_chamada_recusada_sem_efeito(client, franquia.id)

        def _autorizacao_indisponivel(*_args, **_kwargs):
            raise RuntimeError("autorizacao indisponivel")

        monkeypatch.setattr(
            "app.run_agente_compara_comparison_chat._franquia_persistida",
            lambda _usuario: True,
        )
        monkeypatch.setattr(
            "app.services.cleiton_operacao_autorizacao_service.avaliar_autorizacao_operacao_por_franquia",
            _autorizacao_indisponivel,
        )
        with pytest.raises(ComparisonChatProviderError) as autorizacao:
            _generate_governed_text(
                prompt="autorizacao indisponivel",
                usuario=usuario,
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        assert autorizacao.value.error_code == ERROR_ADMISSION_BLOCKED
        assert autorizacao.value.message == MSG_ADMISSION_BLOCKED
        _assert_chamada_recusada_sem_efeito(client, franquia.id)


def test_chamada_interna_com_franquia_ativa_gera_e_cobra(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="interno-franquia-ativa@test.com")
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        texto, model = _generate_governed_text(
            prompt="consulta com franquia ativa",
            usuario=_objeto_autenticado(usuario, franquia.id),
            config=_answer_response_config(),
            billable_stage="answer_generation",
        )
        assert texto == "resposta oficial"
        assert model == "gemini-2.5-flash"
        assert len(client.models.calls) == 1
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == Decimal("0.1")
        assert atual.reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 1
        evento = IaConsumoEvento.query.one()
        assert evento.franquia_id == franquia.id
        assert evento.status == "success"


def test_chamada_interna_com_franquia_expirada_nao_gera(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="interno-franquia-expirada@test.com")
        franquia.fim_ciclo = utcnow_naive() - timedelta(days=1)
        db.session.commit()
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        with pytest.raises(ComparisonChatProviderError) as exc:
            _generate_governed_text(
                prompt="franquia expirada",
                usuario=_objeto_autenticado(usuario, franquia.id),
                config=_answer_response_config(),
                billable_stage="answer_generation",
            )
        assert exc.value.error_code == ERROR_PLAN_EXPIRED
        assert exc.value.message == MSG_PLAN_EXPIRED
        _assert_chamada_recusada_sem_efeito(client, franquia.id)


def test_pergunta_complexa_mantem_o_teto_analitico(app, storage_env, monkeypatch):
    del storage_env
    with app.app_context():
        client, models = _cliente_fluxo()
        _usuario_row, franquia, resposta = _rodar(
            monkeypatch,
            limite="100",
            pergunta=PERGUNTA_COMPLEXA,
            client=client,
            email="complexa@test.com",
        )
        assert resposta.get("error") is None
        assert len(models.calls) == 2
        assert models.count_calls == []
        assert resolver_limites_config(models.calls[1]["config"]) == (8192, 1, 0, None)
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 2
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.16")
        assert _franquia(franquia.id).reserva_pendente == 0


def test_saldo_parcial_executa_as_duas_geracoes_sem_reserva(app, storage_env, monkeypatch):
    del storage_env
    with app.app_context():
        client, models = _cliente_fluxo()
        _usuario_row, franquia, resposta = _rodar(
            monkeypatch,
            limite="1",
            pergunta=PERGUNTA_OBJETIVA,
            client=client,
            email="inviavel@test.com",
        )
        assert resposta.get("error") is None
        assert len(models.calls) == 2
        assert models.count_calls == []
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 2
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == Decimal("0.16")
        assert atual.reserva_pendente == 0
        assert atual.consumo_acumulado < franquia.limite_total


def test_contagem_inflada_nao_bloqueia_a_segunda_geracao(app, storage_env, monkeypatch):
    del storage_env
    with app.app_context():
        client, models = _cliente_fluxo(inflar_resposta=True)
        _usuario_row, franquia, resposta = _rodar(
            monkeypatch,
            limite="30",
            pergunta=PERGUNTA_OBJETIVA,
            client=client,
            email="admissao@test.com",
        )
        assert resposta.get("error") is None
        assert len(models.calls) == 2
        assert models.count_calls == []
        assert "Seleção de consultas" in models.calls[0]["contents"]
        assert IaChamadaTentativa.query.count() == 0
        atual = _franquia(franquia.id)
        assert atual.reserva_pendente == 0
        assert atual.consumo_acumulado == Decimal("0.16")
        assert IaConsumoAbatimento.query.count() == 2


def test_franquia_esgotada_informa_o_limite_do_plano(app, storage_env, monkeypatch):
    del storage_env
    with app.app_context():
        client, models = _cliente_fluxo()
        _usuario_row, _franquia_row, resposta = _rodar(
            monkeypatch,
            limite="5",
            consumo="5",
            pergunta=PERGUNTA_OBJETIVA,
            client=client,
            email="esgotada@test.com",
        )
        assert resposta["error_code"] == ERROR_FRANCHISE_EXHAUSTED
        assert "Você atingiu o limite de uso do plano Free" in resposta["message"]
        assert models.calls == []
        assert models.count_calls == []
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0


def test_plano_bloqueado_nao_usa_a_mensagem_de_limite(app, storage_env, monkeypatch):
    del storage_env
    with app.app_context():
        client, models = _cliente_fluxo()
        _usuario_row, _franquia_row, resposta = _rodar(
            monkeypatch,
            limite="100",
            bloqueio=True,
            pergunta=PERGUNTA_OBJETIVA,
            client=client,
            email="bloqueada@test.com",
        )
        assert resposta["error_code"] == ERROR_PLAN_BLOCKED
        assert resposta["message"] == MSG_PLAN_BLOCKED_MANUAL
        assert "atingiu o limite" not in resposta["message"]
        assert models.calls == []
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0


def test_plano_expirado_nao_usa_a_mensagem_de_limite(app, storage_env, monkeypatch):
    del storage_env
    with app.app_context():
        client, models = _cliente_fluxo()
        _usuario_row, _franquia_row, resposta = _rodar(
            monkeypatch,
            limite="100",
            ciclo_vencido=True,
            pergunta=PERGUNTA_OBJETIVA,
            client=client,
            email="expirada@test.com",
        )
        assert resposta["error_code"] == ERROR_PLAN_EXPIRED
        assert resposta["message"] == MSG_PLAN_EXPIRED
        assert "atingiu o limite" not in resposta["message"]
        assert models.calls == []
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0


def test_plano_degradado_autoriza_e_debita_sem_reserva(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="5", email="degradada@test.com", categoria="starter")
        franquia.consumo_acumulado = Decimal("5")
        db.session.commit()
        client = _Client(_ok)
        _patch_client(monkeypatch, client)
        texto, _model = _generate_governed_text(
            prompt="consulta com franquia degradada",
            usuario=usuario,
            config=_answer_response_config(),
            billable_stage="answer_generation",
        )
        assert texto == "resposta oficial"
        assert len(client.models.calls) == 1
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == Decimal("5.1")
        assert atual.reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 1


def test_plano_desobediente_nao_descarta_evidencia_nem_encolhe_a_analise(app, storage_env, monkeypatch):
    del storage_env
    with app.app_context():

        class _Desobediente(_Fluxo):
            def generate_content(self, *, model, contents, config=None):
                self.calls.append({"model": model, "contents": contents, "config": config})
                if isinstance(contents, str) and "Seleção de consultas" in contents:
                    texto = _selection_oficial("freight_components", "partes")
                else:
                    texto = "Os componentes publicados estão na evidência."
                return SimpleNamespace(
                    text=texto,
                    usage_metadata=_usage(80),
                    candidates=[SimpleNamespace(finish_reason="STOP")],
                )

        models = _Desobediente()
        client = SimpleNamespace(
            models=models,
            http_options=SimpleNamespace(retry_options=SimpleNamespace(attempts=1)),
        )
        _usuario_row, _franquia_row, resposta = _rodar(
            monkeypatch,
            limite="100",
            pergunta=PERGUNTA_OBJETIVA,
            client=client,
            email="desobediente@test.com",
        )
        assert resposta.get("error") is None
        assert resolver_limites_config(models.calls[1]["config"]) == (8192, 1, 0, None)
        assert "freight_components" in models.calls[1]["contents"]
