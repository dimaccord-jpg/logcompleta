"""SCRUM-146 lote 1: orçamento técnico da extração do AgenteCompara.

Provedor simulado. Não chama o Gemini pago.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from app.extensions import db
from app.models import Franquia, IaChamadaTentativa, IaConsumoEvento, utcnow_naive
from app.services.agente_compara_extraction_budget import (
    EXTRACAO_TETO_TECNICO_TOKENS,
    MIN_JSON_OUTPUT_TOKENS,
    capacidade_do_modelo,
    consumo_tecnico,
    encaixar_limites_na_operacao,
)
from app.services.cleiton_billable_ai_call import (
    BILLABLE_AI_MAX_OUTPUT_TOKENS,
    BILLABLE_AI_MAX_THINKING_TOKENS,
    cleiton_governed_billable_ai_call,
    resolver_limites_config,
)
from tests.conftest import seed_cleiton_cost_config, seed_conta_franquia_cliente, seed_sistema_interno, seed_usuario

import app.run_agente_compara_temp_table as temp_mod

VALID_JSON = (
    '{"status":"needs_review","freight_tables":[],"freight_routes":[],'
    '"accessorial_fees":[],"reading_alerts":[],"evidence_refs":[]}'
)
SECRET_RESPONSE = "RESPOSTA_SIGILOSA_NAO_LOGAR_orcamento"
DOC_SECRET = "DOCUMENTO_SIGILOSO_NAO_LOGAR_tabela"


class _Models:
    def __init__(self, behavior, count_behavior):
        self.calls = []
        self.count_calls = []
        self._behavior = behavior
        self._count_behavior = count_behavior

    def count_tokens(self, *, model, contents, config=None):
        self.count_calls.append({"model": model, "contents": contents, "config": config})
        return self._count_behavior(model, contents, config)

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._behavior(model, contents, config)


class _Client:
    def __init__(self, behavior, count_behavior):
        self.models = _Models(behavior, count_behavior)
        self.http_options = SimpleNamespace(retry_options=SimpleNamespace(attempts=1))


def _count_fixo(*totais: int):
    fila = list(totais)

    def behavior(model, contents, config):
        total = fila.pop(0) if fila else totais[-1]
        return SimpleNamespace(total_tokens=total, cached_content_token_count=None)

    return behavior


def _usage(*, prompt: int, resposta: int, pensamento: int):
    return SimpleNamespace(
        prompt_token_count=prompt,
        candidates_token_count=resposta,
        thoughts_token_count=pensamento,
        total_token_count=prompt + resposta + pensamento,
    )


def _resposta(texto: str, *, prompt: int, resposta: int, pensamento: int, finish: str | None = None):
    candidates = [SimpleNamespace(finish_reason=finish)] if finish else []
    return SimpleNamespace(
        text=texto,
        usage_metadata=_usage(prompt=prompt, resposta=resposta, pensamento=pensamento),
        candidates=candidates,
    )


def _limites(config):
    if isinstance(config, dict):
        saida = config["max_output_tokens"]
        think = config["thinking_config"]
        thinking = think["thinking_budget"] if isinstance(think, dict) else think
    else:
        saida = config.max_output_tokens
        think = config.thinking_config
        thinking = think["thinking_budget"] if isinstance(think, dict) else think.thinking_budget
    return int(saida), int(thinking)


def _seed(*, limite="100", consumo="0", reserva="0", email="extracao@test.com", taxa=2000.0):
    seed_sistema_interno()
    cfg = seed_cleiton_cost_config()
    cfg.credit_tokens_per_credit = taxa
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = Decimal(limite)
    franquia.consumo_acumulado = Decimal(consumo)
    franquia.reserva_pendente = Decimal(reserva)
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email, categoria="free")
    return usuario, franquia


def _instalar(monkeypatch, *, client, usuario, models):
    monkeypatch.setattr(temp_mod, "_get_client", lambda: client)
    monkeypatch.setattr(temp_mod, "_get_model_candidates", lambda: list(models))
    monkeypatch.setattr(temp_mod, "_get_cached_extraction", lambda *_a, **_k: None)
    monkeypatch.setattr(temp_mod, "_cache_extraction_result", lambda *_a, **_k: None)
    monkeypatch.setattr(temp_mod, "_register_nonbillable_processing_event", lambda *_a, **_k: None)
    monkeypatch.setattr(temp_mod, "get_active_calculation_bases_for_runtime", lambda: [])
    monkeypatch.setattr(temp_mod, "usuario_operacional_da_chamada", lambda _usuario=None: usuario)
    monkeypatch.setattr(
        temp_mod,
        "build_agente_compara_document_context_for_chat",
        lambda *_a, **_k: {
            "has_documents": True,
            "context_block": DOC_SECRET,
            "gemini_file_parts": None,
        },
    )

    def _apply(payload, source_doc_ids=None, **kwargs):
        return {
            "status": (payload or {}).get("status") or "needs_review",
            "reading_alerts": list((payload or {}).get("reading_alerts") or []),
            "temp_table_id": "tt-orcamento",
            "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
        }

    monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)


def _extrair():
    return temp_mod.run_agente_compara_temp_table_extraction(["doc-1"], session_obj={})


def _franquia(fid):
    db.session.expire_all()
    return db.session.get(Franquia, fid)


def test_encaixe_respeita_teto_e_deixa_json_fora_do_thinking():
    modelo = capacidade_do_modelo("gemini-2.5-flash")
    dentro = encaixar_limites_na_operacao(
        1_000,
        modelo.max_output_tokens,
        modelo.max_thinking_tokens,
        EXTRACAO_TETO_TECNICO_TOKENS,
    )
    assert dentro is not None
    saida, thinking = dentro
    assert 1_000 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS
    assert saida - thinking >= MIN_JSON_OUTPUT_TOKENS
    assert thinking < saida
    assert saida > BILLABLE_AI_MAX_OUTPUT_TOKENS

    assert (
        encaixar_limites_na_operacao(
            27_000,
            modelo.max_output_tokens,
            modelo.max_thinking_tokens,
            EXTRACAO_TETO_TECNICO_TOKENS,
        )
        is None
    )
    assert (
        encaixar_limites_na_operacao(0, modelo.max_output_tokens, modelo.max_thinking_tokens, 4_000)
        is None
    )


def test_tetos_globais_dos_demais_agentes_permanecem(app):
    with app.app_context():
        usuario, franquia = _seed(limite="100")
        fim = franquia.fim_ciclo
        client = _Client(
            lambda model, contents, config: _resposta("ok", prompt=80, resposta=20, pensamento=0),
            _count_fixo(40),
        )
        cleiton_governed_billable_ai_call(
            client,
            model="gemini-2.5-flash",
            contents="chamada de outro agente",
            agent="roberto",
            flow_type="roberto_chat",
            api_key_label="test",
            usuario=usuario,
        )
        saida, thinking = _limites(client.models.calls[0]["config"])
        assert saida == BILLABLE_AI_MAX_OUTPUT_TOKENS == 8192
        assert thinking == BILLABLE_AI_MAX_THINKING_TOKENS == 8192
        assert resolver_limites_config(None) == (8192, 1, 8192, None)
        assert _franquia(franquia.id).fim_ciclo == fim


def test_operacao_dentro_do_teto_reserva_no_maximo_30000(app, monkeypatch, caplog):
    with app.app_context():
        usuario, franquia = _seed(limite="100")
        fim = franquia.fim_ciclo
        client = _Client(
            lambda model, contents, config: _resposta(VALID_JSON, prompt=100, resposta=50, pensamento=10),
            _count_fixo(1_000),
        )
        _instalar(monkeypatch, client=client, usuario=usuario, models=["gemini-2.5-flash"])
        with caplog.at_level(logging.INFO):
            resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert len(client.models.calls) == 1
        saida, thinking = _limites(client.models.calls[0]["config"])
        total = 1_000 + saida + thinking
        assert total == EXTRACAO_TETO_TECNICO_TOKENS
        assert saida - thinking >= MIN_JSON_OUTPUT_TOKENS
        assert thinking < saida
        assert IaChamadaTentativa.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).fim_ciclo == fim
        assert DOC_SECRET not in caplog.text
        assert VALID_JSON not in caplog.text
        assert "autorizado=30000" in caplog.text


def test_operacao_acima_de_30000_nao_gera(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(AssertionError("nao gerar")),
            _count_fixo(27_000),
        )
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert IaChamadaTentativa.query.count() == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert resultado["status"] == "failed"


def test_saldo_positivo_abaixo_do_teto_autoriza_sem_reserva(app, monkeypatch, caplog):
    """Saldo positivo abaixo do teto autoriza o orçamento técnico inteiro."""
    with app.app_context():
        usuario, franquia = _seed(limite="14")
        client = _Client(
            lambda model, contents, config: _resposta(VALID_JSON, prompt=40, resposta=40, pensamento=20),
            _count_fixo(500),
        )
        _instalar(monkeypatch, client=client, usuario=usuario, models=["gemini-2.5-flash"])
        with caplog.at_level(logging.INFO):
            resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert len(client.models.calls) == 1
        modelo = capacidade_do_modelo("gemini-2.5-flash")
        esperado = encaixar_limites_na_operacao(
            500,
            modelo.max_output_tokens,
            modelo.max_thinking_tokens,
            EXTRACAO_TETO_TECNICO_TOKENS,
            model="gemini-2.5-flash",
        )
        saida, thinking = _limites(client.models.calls[0]["config"])
        assert esperado == (saida, thinking)
        assert 500 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS
        reduzido = encaixar_limites_na_operacao(
            500,
            modelo.max_output_tokens,
            modelo.max_thinking_tokens,
            28_000,
            model="gemini-2.5-flash",
        )
        assert (saida, thinking) != reduzido
        config = client.models.calls[0]["config"]
        candidatos = config["candidate_count"] if isinstance(config, dict) else config.candidate_count
        assert candidatos == 1
        assert IaChamadaTentativa.query.count() == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert "autorizado=30000" in caplog.text
        assert "autorizado=28000" not in caplog.text
        assert "tokens_saldo=" not in caplog.text


def test_saldo_residual_positivo_inicia_no_teto_tecnico_sem_reserva(app, monkeypatch, caplog):
    with app.app_context():
        usuario, franquia = _seed(limite="15", consumo="13", email="residual@test.com")
        conta_rica, franquia_rica = seed_conta_franquia_cliente(slug="conta-rica")
        franquia_rica.limite_total = Decimal("100")
        franquia_rica.consumo_acumulado = Decimal("0")
        franquia_rica.reserva_pendente = Decimal("0")
        franquia_rica.inicio_ciclo = utcnow_naive()
        franquia_rica.fim_ciclo = utcnow_naive() + timedelta(days=28)
        db.session.commit()
        outro = seed_usuario(franquia_rica.id, conta_rica.id, email="rica@test.com")
        assert outro.id != usuario.id
        client = _Client(
            lambda model, contents, config: _resposta(VALID_JSON, prompt=40, resposta=20, pensamento=0),
            _count_fixo(100),
        )
        _instalar(monkeypatch, client=client, usuario=usuario, models=["gemini-2.5-flash"])
        with caplog.at_level(logging.INFO):
            resultado = _extrair()
        assert len(client.models.calls) == 1
        saida, thinking = _limites(client.models.calls[0]["config"])
        assert 100 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS
        assert IaChamadaTentativa.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == Decimal("13")
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia_rica.id).consumo_acumulado == 0
        assert resultado["status"] == "needs_review"
        assert "autorizado=30000" in caplog.text
        assert "autorizado=4000" not in caplog.text
        assert "tokens_saldo=" not in caplog.text
        assert DOC_SECRET not in caplog.text


def test_reserva_pendente_nao_reduz_o_orcamento_tecnico(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="30", reserva="20")
        client = _Client(
            lambda model, contents, config: _resposta(VALID_JSON, prompt=30, resposta=30, pensamento=10),
            _count_fixo(1_000),
        )
        _instalar(monkeypatch, client=client, usuario=usuario, models=["gemini-2.5-flash"])
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        saida, thinking = _limites(client.models.calls[0]["config"])
        modelo = capacidade_do_modelo("gemini-2.5-flash")
        assert (saida, thinking) == encaixar_limites_na_operacao(
            1_000,
            modelo.max_output_tokens,
            modelo.max_thinking_tokens,
            EXTRACAO_TETO_TECNICO_TOKENS,
            model="gemini-2.5-flash",
        )
        assert 1_000 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS
        assert (saida, thinking) != encaixar_limites_na_operacao(
            1_000,
            modelo.max_output_tokens,
            modelo.max_thinking_tokens,
            20_000,
            model="gemini-2.5-flash",
        )
        assert IaChamadaTentativa.query.count() == 0
        assert _franquia(franquia.id).reserva_pendente == Decimal("20")
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_saldos_positivos_preservam_o_mesmo_orcamento_tecnico(app, monkeypatch):
    """0,10, 2, 14 e 21 créditos usam o mesmo encaixe técnico do saldo amplo."""
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="saldo-igual@test.com")
        referencia = None
        payload_referencia = None
        for limite in ("0.10", "2", "14", "21", "100"):
            atual = _franquia(franquia.id)
            atual.limite_total = Decimal(limite)
            atual.consumo_acumulado = Decimal("0")
            atual.reserva_pendente = Decimal("0")
            db.session.commit()
            recebidos = []

            def _apply(payload, source_doc_ids=None, **kwargs):
                recebidos.append(dict(payload or {}))
                return {
                    "status": (payload or {}).get("status") or "needs_review",
                    "reading_alerts": list((payload or {}).get("reading_alerts") or []),
                    "temp_table_id": "tt-orcamento",
                    "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
                }

            client = _Client(
                lambda model, contents, config: _resposta(
                    VALID_JSON, prompt=40, resposta=40, pensamento=20
                ),
                _count_fixo(500),
            )
            _instalar(monkeypatch, client=client, usuario=usuario, models=["gemini-2.5-flash"])
            monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)
            resultado = _extrair()
            assert resultado["status"] == "needs_review"
            assert len(client.models.calls) == 1
            modelo = capacidade_do_modelo("gemini-2.5-flash")
            limites = _limites(client.models.calls[0]["config"])
            assert limites == encaixar_limites_na_operacao(
                500,
                modelo.max_output_tokens,
                modelo.max_thinking_tokens,
                EXTRACAO_TETO_TECNICO_TOKENS,
                model="gemini-2.5-flash",
            )
            assert 500 + limites[0] + limites[1] == EXTRACAO_TETO_TECNICO_TOKENS
            assert IaChamadaTentativa.query.count() == 0
            assert _franquia(franquia.id).reserva_pendente == 0
            assert _franquia(franquia.id).consumo_acumulado == 0
            assert recebidos
            if referencia is None:
                referencia = limites
                payload_referencia = recebidos[0]
            else:
                assert limites == referencia
                assert recebidos[0] == payload_referencia


def test_fallback_com_saldo_baixo_respeita_teto_tecnico_acumulado(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="14", email="fallback-saldo-baixo@test.com")

        class _Falha(RuntimeError):
            def __init__(self):
                super().__init__("provider down")
                self.usage_metadata = _usage(prompt=700, resposta=200, pensamento=100)

        def gerar(model, contents, config):
            if model == "gemini-2.5-flash":
                raise _Falha()
            return _resposta(VALID_JSON, prompt=80, resposta=40, pensamento=10)

        client = _Client(gerar, _count_fixo(1_000, 1_000))
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert [chamada["model"] for chamada in client.models.calls] == [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ]
        saida_primaria, thinking_primario = _limites(client.models.calls[0]["config"])
        assert 1_000 + saida_primaria + thinking_primario == EXTRACAO_TETO_TECNICO_TOKENS
        modelo = capacidade_do_modelo("gemini-2.5-flash-lite")
        saida, thinking = _limites(client.models.calls[1]["config"])
        assert (saida, thinking) == encaixar_limites_na_operacao(
            1_000,
            modelo.max_output_tokens,
            modelo.max_thinking_tokens,
            EXTRACAO_TETO_TECNICO_TOKENS - 1_000,
            model="gemini-2.5-flash-lite",
        )
        assert 1_000 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS - 1_000
        assert (saida, thinking) != encaixar_limites_na_operacao(
            1_000,
            modelo.max_output_tokens,
            modelo.max_thinking_tokens,
            28_000,
            model="gemini-2.5-flash-lite",
        )
        assert IaChamadaTentativa.query.count() == 0
        assert _franquia(franquia.id).reserva_pendente == 0


def test_primario_com_falha_e_fallback_dentro_do_restante(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100")

        class _Falha(RuntimeError):
            def __init__(self):
                super().__init__("provider down")
                self.usage_metadata = _usage(prompt=700, resposta=200, pensamento=100)

        def gerar(model, contents, config):
            if model == "gemini-2.5-flash":
                raise _Falha()
            return _resposta(VALID_JSON, prompt=80, resposta=40, pensamento=10)

        client = _Client(gerar, _count_fixo(1_000, 1_000))
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert [chamada["model"] for chamada in client.models.calls] == [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ]
        saida, thinking = _limites(client.models.calls[1]["config"])
        assert 1_000 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS - 1_000
        assert _franquia(_franquia_row.id).reserva_pendente == 0


def test_fallback_que_ultrapassaria_o_restante_nao_inicia(app, monkeypatch, caplog):
    with app.app_context():
        usuario, franquia = _seed(limite="100")
        client = _Client(
            lambda model, contents, config: _resposta(
                SECRET_RESPONSE,
                prompt=2_000,
                resposta=24_000,
                pensamento=1_000,
                finish="MAX_TOKENS",
            ),
            _count_fixo(1_000),
        )
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        with caplog.at_level(logging.INFO):
            resultado = _extrair()
        assert [chamada["model"] for chamada in client.models.calls] == ["gemini-2.5-flash"]
        assert resultado["status"] == "failed"
        assert "tentativa nao iniciada" in caplog.text
        assert "model=gemini-2.5-flash-lite" in caplog.text
        assert SECRET_RESPONSE not in caplog.text
        assert DOC_SECRET not in caplog.text
        assert _franquia(franquia.id).reserva_pendente == 0


def test_max_tokens_preserva_espaco_de_json_e_so_faz_fallback_com_saldo_tecnico(app, monkeypatch, caplog):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100")
        chamadas = {"n": 0}

        def gerar(model, contents, config):
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                return _resposta(
                    SECRET_RESPONSE,
                    prompt=400,
                    resposta=1_200,
                    pensamento=400,
                    finish="MAX_TOKENS",
                )
            return _resposta(VALID_JSON, prompt=50, resposta=30, pensamento=10)

        client = _Client(gerar, _count_fixo(1_000, 1_000))
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        with caplog.at_level(logging.WARNING):
            resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert len(client.models.calls) == 2
        saida, thinking = _limites(client.models.calls[0]["config"])
        assert saida - thinking >= MIN_JSON_OUTPUT_TOKENS
        assert thinking < saida
        assert "finish_reason=MAX_TOKENS" in caplog.text
        assert SECRET_RESPONSE not in caplog.text


def test_consumo_incerto_nao_abre_fallback_e_mantem_reserva(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100")

        def gerar(model, contents, config):
            raise RuntimeError("timeout de rede")

        client = _Client(gerar, _count_fixo(1_000, 1_000))
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        resultado = _extrair()
        assert [chamada["model"] for chamada in client.models.calls] == ["gemini-2.5-flash"]
        assert IaChamadaTentativa.query.count() == 0
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        assert resultado["status"] == "failed"


def _thinking_respeita_modelo(model: str, thinking: int) -> None:
    nome = model.strip().lower()
    if nome == "gemini-2.5-flash-lite" or nome.startswith("gemini-2.5-flash-lite-"):
        assert thinking == 0 or 512 <= thinking <= 24_576
    elif nome == "gemini-2.5-pro" or nome.startswith("gemini-2.5-pro-"):
        assert 128 <= thinking <= 32_768
    elif nome == "gemini-2.5-flash" or nome.startswith("gemini-2.5-flash-"):
        assert 0 <= thinking <= 24_576
    else:
        assert thinking >= 0


def _tokens_observados() -> int:
    total = 0
    for evento in IaConsumoEvento.query.all():
        if isinstance(evento.total_tokens, int):
            total += evento.total_tokens
    return total


def test_consumo_tecnico_le_response_e_result_sem_duplicar():
    uso = _usage(prompt=20_000, resposta=1_500, pensamento=500)
    por_response = RuntimeError("provider")
    por_response.response = SimpleNamespace(usage_metadata=uso)
    por_result = RuntimeError("provider")
    por_result.result = SimpleNamespace(usage_metadata=uso)
    duplicado = RuntimeError("provider")
    duplicado.usage_metadata = uso
    duplicado.response = SimpleNamespace(usage_metadata=uso)
    duplicado.result = SimpleNamespace(usage_metadata=uso)
    sem_uso = RuntimeError("provider")
    sem_uso.response = SimpleNamespace(text="sem metrica")
    assert consumo_tecnico(por_response) == 22_000
    assert consumo_tecnico(por_result) == 22_000
    assert consumo_tecnico(duplicado) == 22_000
    assert consumo_tecnico(sem_uso) is None


def test_encaixe_respeita_minimos_de_thinking_sem_ampliar_orcamento():
    flash = capacidade_do_modelo("gemini-2.5-flash")
    lite = capacidade_do_modelo("gemini-2.5-flash-lite")
    pro = capacidade_do_modelo("gemini-2.5-pro")
    antigo = capacidade_do_modelo("gemini-2.0-flash")

    flash_apertado = encaixar_limites_na_operacao(
        25_500,
        flash.max_output_tokens,
        flash.max_thinking_tokens,
        EXTRACAO_TETO_TECNICO_TOKENS,
        model="gemini-2.5-flash",
    )
    assert flash_apertado == (4_298, 202)

    lite_apertado = encaixar_limites_na_operacao(
        25_500,
        lite.max_output_tokens,
        lite.max_thinking_tokens,
        EXTRACAO_TETO_TECNICO_TOKENS,
        model="gemini-2.5-flash-lite",
    )
    assert lite_apertado == (4_500, 0)
    assert 25_500 + 4_500 + 0 == EXTRACAO_TETO_TECNICO_TOKENS

    assert (
        encaixar_limites_na_operacao(
            25_800,
            pro.max_output_tokens,
            pro.max_thinking_tokens,
            EXTRACAO_TETO_TECNICO_TOKENS,
            model="gemini-2.5-pro",
        )
        is None
    )

    pro_folgado = encaixar_limites_na_operacao(
        1_000,
        pro.max_output_tokens,
        pro.max_thinking_tokens,
        EXTRACAO_TETO_TECNICO_TOKENS,
        model="gemini-2.5-pro",
    )
    assert pro_folgado is not None
    saida_pro, thinking_pro = pro_folgado
    assert thinking_pro >= 128
    assert 1_000 + saida_pro + thinking_pro == EXTRACAO_TETO_TECNICO_TOKENS

    legado = encaixar_limites_na_operacao(
        1_000,
        antigo.max_output_tokens,
        antigo.max_thinking_tokens,
        EXTRACAO_TETO_TECNICO_TOKENS,
        model="gemini-2.0-flash",
    )
    assert legado is not None
    saida_legado, thinking_legado = legado
    assert thinking_legado == 0
    assert 1_000 + saida_legado + thinking_legado <= EXTRACAO_TETO_TECNICO_TOKENS


def _fallback_depois_de_22000(monkeypatch, usuario, *, atributo: str):
    class _Falha(RuntimeError):
        def __init__(self):
            super().__init__("provider failure")
            setattr(
                self,
                atributo,
                SimpleNamespace(usage_metadata=_usage(prompt=20_000, resposta=1_500, pensamento=500)),
            )

    def gerar(model, contents, config):
        if model == "gemini-2.5-flash":
            raise _Falha()
        return _resposta(VALID_JSON, prompt=200, resposta=100, pensamento=50)

    client = _Client(gerar, _count_fixo(1_000, 1_000))
    _instalar(
        monkeypatch,
        client=client,
        usuario=usuario,
        models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
    )
    resultado = _extrair()
    return client, resultado


def test_cenario_a_usage_em_exception_response_nao_reabre_30000(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100", email="cenario-a-response@test.com")
        client, resultado = _fallback_depois_de_22000(monkeypatch, usuario, atributo="response")
        assert resultado["status"] == "needs_review"
        assert [chamada["model"] for chamada in client.models.calls] == [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ]
        entrada_fallback = 1_000
        saida, thinking = _limites(client.models.calls[1]["config"])
        autorizado_fallback = entrada_fallback + saida + thinking
        assert autorizado_fallback == EXTRACAO_TETO_TECNICO_TOKENS - 22_000
        assert autorizado_fallback != EXTRACAO_TETO_TECNICO_TOKENS
        _thinking_respeita_modelo("gemini-2.5-flash-lite", thinking)
        assert _tokens_observados() == 22_000 + 350
        assert _tokens_observados() <= EXTRACAO_TETO_TECNICO_TOKENS
        assert 22_000 + autorizado_fallback == EXTRACAO_TETO_TECNICO_TOKENS


def test_cenario_a_usage_em_exception_result_nao_reabre_30000(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100", email="cenario-a-result@test.com")
        client, resultado = _fallback_depois_de_22000(monkeypatch, usuario, atributo="result")
        assert resultado["status"] == "needs_review"
        saida, thinking = _limites(client.models.calls[1]["config"])
        assert 1_000 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS - 22_000
        assert _tokens_observados() == 22_350
        assert _tokens_observados() <= EXTRACAO_TETO_TECNICO_TOKENS


def test_cenario_b_consumo_contado_antes_da_falha_local(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100", email="cenario-b@test.com")
        chamadas = {"n": 0}

        def gerar(model, contents, config):
            chamadas["n"] += 1
            if chamadas["n"] == 1:
                return _resposta(VALID_JSON, prompt=18_000, resposta=3_000, pensamento=1_000)
            return _resposta(VALID_JSON, prompt=80, resposta=40, pensamento=10)

        client = _Client(gerar, _count_fixo(1_000, 1_000))
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        aplicacoes = {"n": 0}

        def _apply(payload, source_doc_ids=None, **kwargs):
            aplicacoes["n"] += 1
            if aplicacoes["n"] == 1:
                raise RuntimeError("falha ao aplicar payload")
            return {
                "status": (payload or {}).get("status") or "needs_review",
                "reading_alerts": list((payload or {}).get("reading_alerts") or []),
                "temp_table_id": "tt-orcamento",
                "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
            }

        monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert len(client.models.calls) == 2
        saida, thinking = _limites(client.models.calls[1]["config"])
        assert 1_000 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS - 22_000
        assert _tokens_observados() == 22_000 + 130
        assert _tokens_observados() <= EXTRACAO_TETO_TECNICO_TOKENS


def test_mesma_tentativa_nao_e_contabilizada_duas_vezes(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100", email="sem-duplicata@test.com")

        def gerar(model, contents, config):
            if model == "gemini-2.5-flash":
                resposta = _resposta(VALID_JSON, prompt=18_000, resposta=3_000, pensamento=1_000)
                exc = RuntimeError("falha ao aplicar payload")
                exc.response = resposta
                gerar.resposta = resposta
                gerar.falha = exc
                return resposta
            return _resposta(VALID_JSON, prompt=40, resposta=20, pensamento=10)

        client = _Client(gerar, _count_fixo(1_000, 1_000))
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )

        def _apply(payload, source_doc_ids=None, **kwargs):
            if getattr(_apply, "pendente", True):
                _apply.pendente = False
                raise gerar.falha
            return {
                "status": "needs_review",
                "reading_alerts": [],
                "temp_table_id": "tt-orcamento",
                "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
            }

        _apply.pendente = True
        monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert len(client.models.calls) == 2
        saida, thinking = _limites(client.models.calls[1]["config"])
        assert 1_000 + saida + thinking == 8_000
        assert _tokens_observados() == 22_000 + 70
        assert _tokens_observados() <= EXTRACAO_TETO_TECNICO_TOKENS


def test_consumo_ausente_nao_continua_e_nao_duplica(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="consumo-ausente@test.com")

        class _SemMetrica(RuntimeError):
            def __init__(self):
                super().__init__("provider sem usage")
                self.response = SimpleNamespace(text=VALID_JSON)

        def gerar(model, contents, config):
            if model == "gemini-2.5-flash":
                raise _SemMetrica()
            raise AssertionError("fallback nao pode iniciar")

        client = _Client(gerar, _count_fixo(1_000, 1_000))
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        resultado = _extrair()
        assert [chamada["model"] for chamada in client.models.calls] == ["gemini-2.5-flash"]
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 1
        assert _tokens_observados() == 0
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
        assert resultado["status"] == "failed"


def test_flash_lite_25500_envia_thinking_zero(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100", email="flash-lite@test.com")
        client = _Client(
            lambda model, contents, config: _resposta(VALID_JSON, prompt=40, resposta=20, pensamento=0),
            _count_fixo(25_500),
        )
        _instalar(monkeypatch, client=client, usuario=usuario, models=["gemini-2.5-flash-lite"])
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert len(client.models.calls) == 1
        saida, thinking = _limites(client.models.calls[0]["config"])
        assert thinking == 0
        assert 25_500 + saida + thinking == EXTRACAO_TETO_TECNICO_TOKENS
        assert saida - thinking >= MIN_JSON_OUTPUT_TOKENS
        _thinking_respeita_modelo("gemini-2.5-flash-lite", thinking)
        assert thinking != 202
        pedido_contagem = client.models.count_calls[0]["config"]
        assert "thinking_config" not in pedido_contagem
        assert "thinking_budget" not in pedido_contagem


def test_pro_25800_bloqueia_sem_enviar_thinking_invalido(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="pro-bloqueado@test.com")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(AssertionError("nao gerar")),
            _count_fixo(25_800),
        )
        _instalar(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-pro", "gemini-2.5-flash-lite"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        pedido_contagem = client.models.count_calls[0]["config"]
        assert "thinking_config" not in pedido_contagem
        assert "thinking_budget" not in pedido_contagem
        assert IaChamadaTentativa.query.count() == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert resultado["status"] == "failed"
        assert _tokens_observados() == 0
