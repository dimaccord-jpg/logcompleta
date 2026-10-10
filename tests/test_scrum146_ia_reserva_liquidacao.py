"""SCRUM-146 lote 2: reserva atômica e liquidação idempotente de IA."""
from __future__ import annotations

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
    ProcessingEvent,
    utcnow_naive,
)
from app.services.cleiton_ai_data_governance import CleitonAiGovernanceBlockedError
from app.services.cleiton_billable_ai_call import (
    BILLABLE_AI_MAX_OUTPUT_TOKENS,
    BillableAiAdmissionBlocked,
    BillableAiCallError,
    BillableAiCommercialBlocked,
    BillableAiGovernanceBlocked,
    BillableAiUncertainError,
    calcular_teto_chamada,
    cleiton_governed_billable_ai_call,
    estimar_tokens_entrada,
    liquidar_tentativa_ia,
    tentativas_retry_sdk,
)
from app.services.cleiton_franquia_operacional_service import (
    aplicar_motor_apos_ia_consumo_evento,
    decidir_admissao_chamada_ia,
)
from app.services.cleiton_plano_resolver import resolver_plano_operacional_para_franquia
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)

CONTENTS = "chamada de teste"


class _Models:
    def __init__(self, behavior, count_behavior=None):
        self.calls = []
        self.count_calls = []
        self._behavior = behavior
        self._count_behavior = count_behavior

    def count_tokens(self, *, model, contents, config=None):
        self.count_calls.append({"model": model, "contents": contents, "config": config})
        if self._count_behavior is not None:
            return self._count_behavior(model, contents, config)
        from app.services.cleiton_billable_ai_call import estimar_tokens_entrada

        tokens, erro = estimar_tokens_entrada(contents, config)
        if erro:
            raise RuntimeError("count_indisponivel")
        return SimpleNamespace(total_tokens=tokens, cached_content_token_count=None)

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._behavior(model, contents, config)


class _Client:
    def __init__(self, behavior=None, attempts=1, count_behavior=None):
        self.models = _Models(behavior or _ok, count_behavior)
        if attempts is not None:
            self.http_options = SimpleNamespace(retry_options=SimpleNamespace(attempts=attempts))


def _usage(total: int):
    return SimpleNamespace(
        prompt_token_count=total,
        candidates_token_count=0,
        total_token_count=total,
    )


def _ok(model, contents, config):
    return SimpleNamespace(text="ok", usage_metadata=_usage(100))


def _seed(categoria="free", limite="100", consumo="0", bloqueio=False, email="u@test.com"):
    seed_sistema_interno()
    seed_cleiton_cost_config()
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = None if limite is None else Decimal(limite)
    franquia.consumo_acumulado = Decimal(consumo)
    franquia.bloqueio_manual = bloqueio
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email, categoria=categoria)
    return usuario, franquia


def _call(usuario, client, **kwargs):
    return cleiton_governed_billable_ai_call(
        client,
        model=kwargs.pop("model", "gemini-test"),
        contents=kwargs.pop("contents", CONTENTS),
        agent="agente_compara",
        flow_type="agente_compara_chat",
        api_key_label="test",
        usuario=usuario,
        **kwargs,
    )


def _franquia(fid):
    db.session.expire_all()
    return db.session.get(Franquia, fid)


def test_franquia_permitida_reserva_chama_liquida_uma_vez(app):
    with app.app_context():
        usuario, franquia = _seed()
        client = _Client()
        result = _call(usuario, client)
        assert result.complete is True
        assert len(client.models.calls) == 1
        assert IaConsumoEvento.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 1
        assert IaChamadaTentativa.query.filter_by(status="settled").count() == 1
        assert ProcessingEvent.query.count() == 0
        atual = _franquia(franquia.id)
        assert atual.reserva_pendente == 0
        assert atual.consumo_acumulado == Decimal("0.1")


def test_franquia_bloqueada_nao_chama_provider(app):
    with app.app_context():
        usuario, franquia = _seed(bloqueio=True)
        client = _Client()
        with pytest.raises(BillableAiAdmissionBlocked):
            _call(usuario, client)
        assert client.models.calls == []
        assert IaConsumoEvento.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0


def test_saldo_menor_que_reserva_nao_chama_provider(app):
    with app.app_context():
        usuario, franquia = _seed(limite="0.000001")
        client = _Client()
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client)
        assert exc.value.motivo == "saldo_insuficiente"
        assert client.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0


def test_degraded_sem_saldo_bloqueia_no_gerenciador(app):
    with app.app_context():
        usuario, franquia = _seed(categoria="starter", limite="5", consumo="5")
        plano = resolver_plano_operacional_para_franquia(franquia.id)
        decisao = decidir_admissao_chamada_ia(
            _franquia(franquia.id),
            plano,
            creditos_necessarios=Decimal("1"),
            origem_sistema=False,
        )
        assert decisao.classe == "degraded"
        assert decisao.admitida is False
        assert decisao.motivo == "degraded_saldo_insuficiente"
        client = _Client()
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client)
        assert exc.value.motivo == "degraded_saldo_insuficiente"
        assert client.models.calls == []


def test_politica_distingue_estados(app):
    with app.app_context():
        _usuario, franquia = _seed(limite=None)
        plano = resolver_plano_operacional_para_franquia(franquia.id)
        ilimitada = decidir_admissao_chamada_ia(
            _franquia(franquia.id),
            plano,
            creditos_necessarios=Decimal("9"),
            origem_sistema=False,
        )
        assert ilimitada.classe == "unlimited"
        assert ilimitada.admitida is True
        interna = decidir_admissao_chamada_ia(
            _franquia(franquia.id),
            plano,
            creditos_necessarios=Decimal("9"),
            origem_sistema=True,
        )
        assert interna.classe == "internal"
        assert interna.debita_cliente is False
        franquia.fim_ciclo = utcnow_naive() - timedelta(days=1)
        db.session.commit()
        expirada = decidir_admissao_chamada_ia(
            _franquia(franquia.id),
            plano,
            creditos_necessarios=Decimal("1"),
            origem_sistema=False,
        )
        assert expirada.classe == "expired"
        assert expirada.admitida is False
        franquia.fim_ciclo = utcnow_naive() + timedelta(days=10)
        franquia.bloqueio_manual = True
        db.session.commit()
        bloqueada = decidir_admissao_chamada_ia(
            _franquia(franquia.id),
            plano,
            creditos_necessarios=Decimal("1"),
            origem_sistema=False,
        )
        assert bloqueada.classe == "blocked"


def test_system_origin_nao_debita_cliente(app):
    with app.app_context():
        _usuario, franquia = _seed(limite="0.000001")
        client = _Client()
        result = cleiton_governed_billable_ai_call(
            client,
            model="gemini-test",
            contents=CONTENTS,
            agent="agente_compara",
            flow_type="agente_compara_chat",
            api_key_label="test",
            origem_sistema=True,
        )
        assert result.complete is True
        assert len(client.models.calls) == 1
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert IaConsumoAbatimento.query.count() == 0
        evento = IaConsumoEvento.query.one()
        assert evento.origem_sistema is True


def test_identidade_ausente_nao_cai_em_sistema(app):
    with app.app_context():
        _seed()
        client = _Client()
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            cleiton_governed_billable_ai_call(
                client,
                model="gemini-test",
                contents=CONTENTS,
                agent="agente_compara",
                flow_type="agente_compara_chat",
                api_key_label="test",
            )
        assert exc.value.motivo == "identidade_cliente_ausente"
        assert client.models.calls == []
        assert IaConsumoEvento.query.filter_by(origem_sistema=True).count() == 0
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.franquia_id is None
        assert tentativa.origem_sistema is False


def test_admin_nao_vira_system_origin(app):
    with app.app_context():
        usuario, franquia = _seed()
        usuario.is_admin = True
        db.session.commit()
        client = _Client()
        _call(usuario, client)
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.1")
        assert IaConsumoEvento.query.filter_by(origem_sistema=True).count() == 0


def test_governance_block_libera_reserva_sem_debito(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed()

        def _bloqueia(*_args, **_kwargs):
            raise CleitonAiGovernanceBlockedError("bloqueado", reason_codes=["governance_blocked"])

        monkeypatch.setattr("app.run_cleiton_gemini_governance.govern_or_raise", _bloqueia)
        client = _Client()
        with pytest.raises(BillableAiGovernanceBlocked):
            _call(usuario, client)
        assert client.models.calls == []
        assert IaConsumoAbatimento.query.count() == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        tentativa = IaChamadaTentativa.query.filter_by(flow_type="agente_compara_chat").one()
        assert tentativa.status == "governance_blocked"


def test_fallback_reavalia_depois_que_a_primeira_consome_saldo(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100")
        calls = []
        usuario_id = usuario.id
        from app.models import User

        class ComUsage(RuntimeError):
            def __init__(self):
                super().__init__("provider")
                self.usage_metadata = _usage(100)

        def behavior_usage(model, contents, config):
            calls.append(model)
            atual = _franquia(franquia.id)
            atual.consumo_acumulado = atual.limite_total
            db.session.commit()
            raise ComUsage()

        client = _Client(behavior_usage)
        monkeypatch.setattr("app.run_agente_compara_chat._get_client", lambda: client)
        monkeypatch.setattr(
            "app.run_agente_compara_chat._get_model_candidates",
            lambda: ["modelo-a", "modelo-b"],
        )
        monkeypatch.setattr(
            "app.run_agente_compara_chat.usuario_operacional_da_chamada",
            lambda usuario=None: db.session.get(User, usuario_id),
        )
        from app.run_agente_compara_chat import chat_agente_compara_reply

        out = chat_agente_compara_reply("ola", [])
        assert out.get("answer") == ""
        assert calls == ["modelo-a"]
        assert IaChamadaTentativa.query.filter_by(status="blocked").count() == 1


def test_resposta_vazia_nao_autoriza_segundo_modelo(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100")
        calls = []

        def behavior(model, contents, config):
            calls.append(model)
            atual = _franquia(franquia.id)
            atual.consumo_acumulado = atual.limite_total
            db.session.commit()
            return SimpleNamespace(text="  ", usage_metadata=_usage(50))

        client = _Client(behavior)
        usuario_id = usuario.id
        from app.models import User

        monkeypatch.setattr("app.run_agente_compara_chat._get_client", lambda: client)
        monkeypatch.setattr(
            "app.run_agente_compara_chat._get_model_candidates",
            lambda: ["modelo-a", "modelo-b"],
        )
        monkeypatch.setattr(
            "app.run_agente_compara_chat.usuario_operacional_da_chamada",
            lambda usuario=None: db.session.get(User, usuario_id),
        )
        from app.run_agente_compara_chat import chat_agente_compara_reply

        chat_agente_compara_reply("ola", [])
        assert calls == ["modelo-a"]
        assert IaChamadaTentativa.query.filter_by(model="modelo-b", status="blocked").count() == 1


def _race_app(path):
    race = Flask("scrum146-race")
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


def _disputar(race, franquia_id, user_ids):
    from app.models import User

    calls = []
    results = []
    barrier = threading.Barrier(len(user_ids))

    def behavior(model, contents, config):
        calls.append(model)
        return SimpleNamespace(text="ok", usage_metadata=_usage(10))

    def worker(index, user_id):
        with race.app_context():
            usuario = db.session.get(User, user_id)
            barrier.wait(timeout=10)
            try:
                _call(usuario, _Client(behavior), attempt_key=f"race-{index}-{user_id}")
                results.append("ok")
            except BillableAiAdmissionBlocked:
                results.append("blocked")
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker, args=(index, uid)) for index, uid in enumerate(user_ids)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    return calls, results


def test_concorrencia_admite_uma_so(tmp_path):
    race = _race_app(tmp_path / "uma.db")
    with race.app_context():
        import app.models  # noqa: F401

        db.create_all()
        usuario, franquia = _seed(limite="0")
        _entrada, _saida, teto, erro = calcular_teto_chamada(CONTENTS)
        assert erro is None and teto is not None
        franquia.limite_total = teto
        db.session.commit()
        franquia_id = franquia.id
        user_id = usuario.id
    calls, results = _disputar(race, franquia_id, [user_id, user_id])
    assert sorted(results) == ["blocked", "ok"]
    assert len(calls) == 1


def test_dois_usuarios_disputam_o_mesmo_orcamento(tmp_path):
    race = _race_app(tmp_path / "dois.db")
    with race.app_context():
        import app.models  # noqa: F401

        db.create_all()
        primeiro, franquia = _seed(limite="0", email="a@test.com")
        segundo = seed_usuario(franquia.id, primeiro.conta_id, email="b@test.com", categoria="free")
        _entrada, _saida, teto, erro = calcular_teto_chamada(CONTENTS)
        assert erro is None and teto is not None
        franquia.limite_total = teto
        db.session.commit()
        ids = [primeiro.id, segundo.id]
        franquia_id = franquia.id
    calls, results = _disputar(race, franquia_id, ids)
    assert sorted(results) == ["blocked", "ok"]
    assert len(calls) == 1


def test_uso_menor_que_reserva_libera_excedente(app):
    with app.app_context():
        usuario, franquia = _seed(limite="100")
        _entrada, saida, teto, _erro = calcular_teto_chamada(CONTENTS)
        assert teto > Decimal("0.1")
        client = _Client()
        _call(usuario, client)
        atual = _franquia(franquia.id)
        assert atual.reserva_pendente == 0
        assert atual.consumo_acumulado == Decimal("0.1")
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.reserved_credits == teto
        assert tentativa.actual_credits == Decimal("0.1")
        assert saida == BILLABLE_AI_MAX_OUTPUT_TOKENS


def test_liquidacao_repetida_nao_gera_segundo_debito(app):
    with app.app_context():
        usuario, franquia = _seed()
        result = _call(usuario, _Client())
        antes = _franquia(franquia.id).consumo_acumulado
        liquidar_tentativa_ia(result.attempt_id)
        aplicar_motor_apos_ia_consumo_evento(result.evento_id)
        assert _franquia(franquia.id).consumo_acumulado == antes
        assert IaConsumoAbatimento.query.count() == 1
        segunda = aplicar_motor_apos_ia_consumo_evento(result.evento_id)
        assert segunda.motivo_nao_abateu == "ja_apropriado"
        assert _franquia(franquia.id).consumo_acumulado == antes


def test_provider_failure_com_usage_liquida(app):
    with app.app_context():
        usuario, franquia = _seed()

        class Boom(RuntimeError):
            def __init__(self):
                super().__init__("falhou")
                self.usage_metadata = _usage(1000)

        def behavior(model, contents, config):
            raise Boom()

        with pytest.raises(Boom):
            _call(usuario, _Client(behavior))
        assert _franquia(franquia.id).consumo_acumulado == Decimal("1")
        assert _franquia(franquia.id).reserva_pendente == 0
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "provider_failed"
        assert tentativa.actual_credits == Decimal("1")
        assert IaConsumoEvento.query.one().total_tokens == 1000


def test_provider_failure_sem_usage_fica_incerta(app):
    with app.app_context():
        usuario, franquia = _seed()

        def behavior(model, contents, config):
            raise RuntimeError("timeout de rede")

        with pytest.raises(BillableAiUncertainError):
            _call(usuario, _Client(behavior))
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente > 0
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "uncertain"
        assert tentativa.failure_reason == "provider_sem_usage"
        assert tentativa.reserva_ativa is True


def test_success_no_metrics_nao_vira_zero_confirmado(app):
    with app.app_context():
        usuario, franquia = _seed()

        def behavior(model, contents, config):
            return SimpleNamespace(text="ok")

        with pytest.raises(BillableAiUncertainError) as exc:
            _call(usuario, _Client(behavior))
        assert exc.value.motivo == "success_no_metrics"
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente > 0
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.actual_credits is None
        assert IaConsumoAbatimento.query.count() == 0


def test_falha_de_persistencia_antes_do_provider(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed()
        client = _Client()

        def boom():
            raise RuntimeError("disco")

        monkeypatch.setattr("app.services.cleiton_billable_ai_call._commit_session", boom)
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client)
        assert exc.value.motivo == "persistencia_reserva_falhou"
        assert client.models.calls == []


def test_falha_de_persistencia_depois_do_provider(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed()
        client = _Client()

        def sem_evento(**_kwargs):
            return None, "persist_failed"

        monkeypatch.setattr("app.run_cleiton_gemini_governance._persist_event", sem_evento)
        with pytest.raises(BillableAiUncertainError) as exc:
            _call(usuario, client)
        assert exc.value.motivo == "persistencia_evento_falhou"
        assert len(client.models.calls) == 1
        assert _franquia(franquia.id).reserva_pendente > 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert IaChamadaTentativa.query.one().status == "uncertain"


def test_cache_local_nao_reserva_nem_cria_evento(app, monkeypatch):
    with app.app_context():
        _seed()

        def proibido(*_args, **_kwargs):
            raise AssertionError("cache nao pode chamar IA")

        monkeypatch.setattr(
            "app.run_agente_compara_insights_chat.load_audit_insights_bundle",
            lambda *_args, **_kwargs: {"ok": True, "bundle": {}},
        )
        monkeypatch.setattr(
            "app.run_agente_compara_insights_chat.insights_batch_scope",
            lambda _bundle: "lote",
        )
        monkeypatch.setattr(
            "app.run_agente_compara_insights_chat.get_cached_insights_chat_response",
            lambda *_args, **_kwargs: {"answer": "local", "flow_type": "x", "deterministic": True},
        )
        monkeypatch.setattr(
            "app.run_agente_compara_insights_chat.cleiton_governed_generate_content",
            proibido,
        )
        from app.run_agente_compara_insights_chat import chat_agente_compara_insights_reply

        out = chat_agente_compara_insights_reply("quanto", [], session_obj={})
        assert out["cached"] is True
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 0
        assert ProcessingEvent.query.count() == 0


def test_sdk_retry_acima_de_um_nao_escapa_da_reserva(app):
    with app.app_context():
        usuario, franquia = _seed()
        client = _Client(attempts=5)
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client)
        assert exc.value.motivo == "sdk_retry_nao_contido"
        assert client.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0
        permitido = _Client(attempts=1)
        _call(usuario, permitido, attempt_key="sdk-uma")
        assert len(permitido.models.calls) == 1


def test_builder_desliga_retry_automatico_do_sdk():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    facade = (root / "app" / "services" / "cleiton_billable_ai_call.py").read_text(encoding="utf-8")
    assert "HttpRetryOptions(attempts=SDK_RETRY_ATTEMPTS)" in facade
    assert "SDK_RETRY_ATTEMPTS = 1" in facade
    for relative in (
        "app/run_agente_compara_chat.py",
        "app/run_agente_compara_temp_table.py",
        "app/run_agente_compara_comparison_chat.py",
        "app/run_agente_compara_insights_chat.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "gemini_http_options" in source
        assert "HttpOptions(timeout=" not in source


def test_estimativa_nao_guarda_conteudo():
    tokens, erro = estimar_tokens_entrada("abc")
    assert erro is None
    assert tokens >= len("abc".encode("utf-8"))
    assert estimar_tokens_entrada("abc")[0] == tokens


def _outra_conta(email, limite="100"):
    conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = None if limite is None else Decimal(limite)
    franquia.consumo_acumulado = Decimal("0")
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email, categoria="free")
    return usuario, franquia


def _config_genai(**kwargs):
    from google.genai import types

    return types.GenerateContentConfig(**kwargs)


def test_system_instruction_grande_entra_no_teto(app):
    with app.app_context():
        _seed()
        _, _, curto, erro_curto = calcular_teto_chamada(
            "oi",
            config=_config_genai(system_instruction="a"),
        )
        _, _, longo, erro_longo = calcular_teto_chamada(
            "oi",
            config=_config_genai(system_instruction="a" + ("x" * 10000)),
        )
        assert erro_curto is None and erro_longo is None
        assert longo - curto >= Decimal("10")


def test_content_parts_entram_no_teto(app):
    from google.genai import types

    with app.app_context():
        _seed()
        parte = types.Content(role="user", parts=[types.Part(text="y" * 8000)])
        _, _, grande, erro_grande = calcular_teto_chamada(parte)
        _, _, curto, erro_curto = calcular_teto_chamada("y")
        assert erro_grande is None and erro_curto is None
        assert grande - curto >= Decimal("7")


def test_tools_entram_no_teto(app):
    from google.genai import types

    with app.app_context():
        _seed()
        ferramenta = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="frete", description="z" * 6000),
            ]
        )
        _, _, com_tool, erro_tool = calcular_teto_chamada(
            "oi",
            config=_config_genai(tools=[ferramenta]),
        )
        _, _, sem_tool, erro_sem = calcular_teto_chamada("oi")
        assert erro_tool is None and erro_sem is None
        assert com_tool - sem_tool >= Decimal("6")


def test_candidate_count_multiplica_teto_de_saida(app):
    from google.genai import types

    with app.app_context():
        _seed()
        pensamento = types.ThinkingConfig(thinking_budget=0)
        _, _, um, erro_um = calcular_teto_chamada(
            "oi",
            config=_config_genai(candidate_count=1, max_output_tokens=100, thinking_config=pensamento),
        )
        _, _, quatro, erro_quatro = calcular_teto_chamada(
            "oi",
            config=_config_genai(candidate_count=4, max_output_tokens=100, thinking_config=pensamento),
        )
        assert erro_um is None and erro_quatro is None
        assert quatro - um == Decimal("0.3")


def test_teto_de_texto_nao_usa_chars_sobre_dois():
    texto = "á" * 100
    tokens, erro = estimar_tokens_entrada(texto)
    assert erro is None
    assert tokens >= len(texto.encode("utf-8"))
    assert tokens >= 200


def test_teto_menor_que_input_real_bloqueia_antes_do_provider(app):
    with app.app_context():
        usuario, franquia = _seed(limite="1")
        client = _Client()
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, contents="x" * 20000)
        assert exc.value.motivo == "saldo_insuficiente"
        assert client.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0


def test_saldo_exatamente_suficiente_e_insuficiente(app):
    with app.app_context():
        usuario, franquia = _seed(limite="0", email="exato@test.com")
        _entrada, _saida, teto, erro = calcular_teto_chamada(CONTENTS)
        assert erro is None and teto is not None
        franquia.limite_total = teto
        db.session.commit()
        result = _call(usuario, _Client(), attempt_key="saldo-exato")
        assert result.complete is True

        outro, outra = _outra_conta("insuficiente@test.com", limite="0")
        _entrada, _saida, teto_outro, erro_outro = calcular_teto_chamada(CONTENTS)
        assert erro_outro is None and teto_outro is not None
        outra.limite_total = teto_outro - Decimal("0.000001")
        db.session.commit()
        client = _Client()
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(outro, client, attempt_key="saldo-insuficiente")
        assert exc.value.motivo == "saldo_insuficiente"
        assert client.models.calls == []
        assert _franquia(outra.id).reserva_pendente == 0


def test_cliente_real_com_cinco_retries_e_rejeitado_antes_da_reserva(app):
    from google import genai
    from google.genai import types

    with app.app_context():
        usuario, franquia = _seed()
        client = genai.Client(
            api_key="teste",
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=5)),
        )
        assert tentativas_retry_sdk(client) == 5
        client.http_options = SimpleNamespace(retry_options=SimpleNamespace(attempts=1))
        assert tentativas_retry_sdk(client) == 5
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client)
        assert exc.value.motivo == "sdk_retry_nao_contido"
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaConsumoEvento.query.count() == 0
        padrao = genai.Client(api_key="teste")
        assert tentativas_retry_sdk(padrao) == 1


def test_config_que_reativa_retry_e_rejeitada(app):
    from google.genai import types

    with app.app_context():
        usuario, franquia = _seed()
        client = _Client(attempts=1)
        config = types.GenerateContentConfig(
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=5)),
        )
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, config=config)
        assert exc.value.motivo == "sdk_retry_nao_contido"
        assert client.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0
        vazio = types.GenerateContentConfig(
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions()),
        )
        with pytest.raises(BillableAiAdmissionBlocked):
            _call(usuario, _Client(attempts=1), config=vazio, attempt_key="retry-vazio")


def test_tool_calling_nao_gera_segunda_chamada(app):
    from google.genai import types

    with app.app_context():
        usuario, _franquia_row = _seed()
        vistos = []

        def behavior(model, contents, config):
            afc = getattr(config, "automatic_function_calling", None)
            if isinstance(config, dict):
                afc = config.get("automatic_function_calling")
            disable = afc.get("disable") if isinstance(afc, dict) else getattr(afc, "disable", None)
            vistos.append(disable)
            if disable is not True:
                vistos.append("segunda")
            return _ok(model, contents, config)

        ferramenta = types.Tool(
            function_declarations=[types.FunctionDeclaration(name="frete", description="consulta")],
        )
        config = types.GenerateContentConfig(
            tools=[ferramenta],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=False,
                maximum_remote_calls=10,
            ),
        )
        _call(usuario, _Client(behavior), config=config)
        assert vistos == [True]


def _evento_ia(franquia_id, usuario_id, *, tokens=100, regime="novo"):
    evento = IaConsumoEvento(
        provider="gemini",
        operation="generate_content",
        model="m",
        agent="a",
        flow_type="f",
        api_key_label="k",
        status="success",
        total_tokens=tokens,
        franquia_id=franquia_id,
        usuario_id=usuario_id,
        origem_sistema=False,
        tipo_origem="http_usuario",
        regime_abatimento=regime,
    )
    db.session.add(evento)
    db.session.commit()
    return evento


def _disputar_motor(race, evento_ids):
    results = []
    barrier = threading.Barrier(len(evento_ids))

    def worker(evento_id):
        with race.app_context():
            barrier.wait(timeout=10)
            try:
                resultado = aplicar_motor_apos_ia_consumo_evento(evento_id)
                results.append(resultado.motivo_nao_abateu or "abateu")
            except Exception as exc:
                results.append(repr(exc))
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker, args=(evento_id,)) for evento_id in evento_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    return results


def test_dois_eventos_concorrentes_somam_consumo(tmp_path):
    race = _race_app(tmp_path / "soma.db")
    with race.app_context():
        import app.models  # noqa: F401

        db.create_all()
        usuario, franquia = _seed()
        primeiro = _evento_ia(franquia.id, usuario.id)
        segundo = _evento_ia(franquia.id, usuario.id)
        franquia_id = franquia.id
        evento_ids = [primeiro.id, segundo.id]
    resultados = _disputar_motor(race, evento_ids)
    assert sorted(resultados) == ["abateu", "abateu"]
    with race.app_context():
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == Decimal("0.2")
        assert IaConsumoAbatimento.query.count() == 2


def test_mesmo_evento_concorrente_debita_uma_vez(tmp_path):
    race = _race_app(tmp_path / "mesmo.db")
    with race.app_context():
        import app.models  # noqa: F401

        db.create_all()
        usuario, franquia = _seed()
        evento = _evento_ia(franquia.id, usuario.id)
        franquia_id = franquia.id
        evento_id = evento.id
    resultados = _disputar_motor(race, [evento_id, evento_id])
    assert sorted(resultados) == ["abateu", "ja_apropriado"]
    with race.app_context():
        assert db.session.get(Franquia, franquia_id).consumo_acumulado == Decimal("0.1")
        assert IaConsumoAbatimento.query.count() == 1


def test_falha_pos_debito_fica_incerta_e_retry_nao_duplica(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed()

        def boom():
            raise RuntimeError("depois do debito")

        monkeypatch.setattr("app.services.cleiton_billable_ai_call._checkpoint_pos_debito", boom)
        with pytest.raises(BillableAiUncertainError) as exc:
            _call(usuario, _Client())
        assert exc.value.motivo == "liquidacao_interrompida"
        assert isinstance(exc.value, BillableAiCallError)
        assert exc.value.terminal is True
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "uncertain"
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.1")
        assert _franquia(franquia.id).reserva_pendente > 0
        assert IaConsumoAbatimento.query.count() == 1
        monkeypatch.setattr("app.services.cleiton_billable_ai_call._checkpoint_pos_debito", lambda: None)
        liquidar_tentativa_ia(tentativa.id)
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.1")
        assert IaConsumoAbatimento.query.count() == 1
        atualizada = db.session.get(IaChamadaTentativa, tentativa.id)
        assert atualizada.status == "settled"
        assert atualizada.reserva_ativa is False
        assert _franquia(franquia.id).reserva_pendente == 0


def test_falha_pos_debito_nao_avanca_fallback(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed()
        calls = []

        def behavior(model, contents, config):
            calls.append(model)
            return SimpleNamespace(text="ok", usage_metadata=_usage(100))

        client = _Client(behavior)
        usuario_id = usuario.id
        from app.models import User

        def boom():
            raise RuntimeError("depois do debito")

        monkeypatch.setattr("app.services.cleiton_billable_ai_call._checkpoint_pos_debito", boom)
        monkeypatch.setattr("app.run_agente_compara_chat._get_client", lambda: client)
        monkeypatch.setattr(
            "app.run_agente_compara_chat._get_model_candidates",
            lambda: ["modelo-a", "modelo-b"],
        )
        monkeypatch.setattr(
            "app.run_agente_compara_chat.usuario_operacional_da_chamada",
            lambda usuario=None: db.session.get(User, usuario_id),
        )
        from app.run_agente_compara_chat import chat_agente_compara_reply

        out = chat_agente_compara_reply("ola", [])
        assert out.get("answer") == ""
        assert calls == ["modelo-a"]
        assert IaChamadaTentativa.query.filter_by(model="modelo-b").count() == 0
        assert IaChamadaTentativa.query.one().status == "uncertain"


def test_falha_de_vinculo_evento_fica_incerta_sem_sucesso(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed()

        def boom(*_args, **_kwargs):
            raise RuntimeError("vinculo")

        monkeypatch.setattr("app.services.cleiton_billable_ai_call._persistir_vinculo_evento", boom)
        with pytest.raises(BillableAiUncertainError) as exc:
            _call(usuario, _Client())
        assert exc.value.motivo == "vinculo_evento_falhou"
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "uncertain"
        assert tentativa.ia_consumo_evento_id is None
        assert tentativa.reserva_ativa is True
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente > 0
        assert IaConsumoAbatimento.query.count() == 0


def test_ids_explicitos_inexistentes_bloqueiam_antes_da_reserva(app):
    with app.app_context():
        _seed()
        client = _Client()
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            cleiton_governed_billable_ai_call(
                client,
                model="gemini-test",
                contents=CONTENTS,
                agent="agente_compara",
                flow_type="agente_compara_chat",
                api_key_label="test",
                conta_id=999999,
                franquia_id=999999,
                usuario_id=999999,
            )
        assert exc.value.motivo == "identidade_incoerente"
        assert isinstance(exc.value, BillableAiCommercialBlocked)
        assert client.models.calls == []
        assert IaConsumoEvento.query.count() == 0
        assert IaChamadaTentativa.query.filter(IaChamadaTentativa.reserva_ativa.is_(True)).count() == 0


def test_identidade_cruzada_bloqueia_e_valida_passa(app):
    from app.consumo_identidade import set_consumo_identidade

    with app.app_context():
        primeiro, franquia_a = _seed(email="a@test.com")
        segundo, franquia_b = _outra_conta("b@test.com")
        client = _Client()
        with pytest.raises(BillableAiAdmissionBlocked) as outra_franquia:
            _call(primeiro, client, franquia_id=franquia_b.id, attempt_key="outra-franquia")
        assert outra_franquia.value.motivo == "identidade_incoerente"
        with pytest.raises(BillableAiAdmissionBlocked) as outra_conta:
            _call(primeiro, _Client(), conta_id=segundo.conta_id, attempt_key="outra-conta")
        assert outra_conta.value.motivo == "identidade_incoerente"
        with pytest.raises(BillableAiAdmissionBlocked) as cruzada:
            cleiton_governed_billable_ai_call(
                _Client(),
                model="gemini-test",
                contents=CONTENTS,
                agent="agente_compara",
                flow_type="agente_compara_chat",
                api_key_label="test",
                conta_id=primeiro.conta_id,
                franquia_id=franquia_b.id,
                usuario_id=primeiro.id,
                attempt_key="cruzada",
            )
        assert cruzada.value.motivo == "identidade_incoerente"
        assert IaConsumoEvento.query.count() == 0
        set_consumo_identidade(
            {
                "conta_id": segundo.conta_id,
                "franquia_id": franquia_b.id,
                "usuario_id": segundo.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
        )
        with pytest.raises(BillableAiAdmissionBlocked) as contradicao:
            _call(primeiro, _Client(), attempt_key="contradicao")
        assert contradicao.value.motivo == "identidade_incoerente"
        set_consumo_identidade(
            {
                "conta_id": primeiro.conta_id,
                "franquia_id": franquia_a.id,
                "usuario_id": primeiro.id,
                "tipo_origem": "http_usuario",
                "origem_sistema": False,
            }
        )
        result = cleiton_governed_billable_ai_call(
            _Client(),
            model="gemini-test",
            contents=CONTENTS,
            agent="agente_compara",
            flow_type="agente_compara_chat",
            api_key_label="test",
            conta_id=primeiro.conta_id,
            franquia_id=franquia_a.id,
            usuario_id=primeiro.id,
            attempt_key="identidade-valida",
        )
        assert result.complete is True


def test_system_origin_explicito_continua_separado(app):
    with app.app_context():
        _usuario, franquia = _seed(limite="0.000001")
        client = _Client()
        result = cleiton_governed_billable_ai_call(
            client,
            model="gemini-test",
            contents=CONTENTS,
            agent="agente_compara",
            flow_type="agente_compara_chat",
            api_key_label="test",
            origem_sistema=True,
            conta_id=999999,
            franquia_id=999999,
            usuario_id=999999,
            attempt_key="system-ids-invalidos",
        )
        assert result.complete is True
        assert len(client.models.calls) == 1
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_evento_historico_nao_debita_de_novo(app):
    with app.app_context():
        usuario, franquia = _seed(consumo="0.1")
        evento = _evento_ia(franquia.id, usuario.id, regime="legado")
        resultado = aplicar_motor_apos_ia_consumo_evento(evento.id)
        assert resultado.abateu_franquia is False
        assert resultado.motivo_nao_abateu == "evento_historico_ja_integrado"
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.1")
        assert IaConsumoAbatimento.query.count() == 0
        repetido = aplicar_motor_apos_ia_consumo_evento(evento.id)
        assert repetido.motivo_nao_abateu == "evento_historico_ja_integrado"
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.1")


def test_integrity_error_alheio_nao_vira_ja_apropriado(app, monkeypatch):
    from sqlalchemy.exc import IntegrityError

    with app.app_context():
        usuario, franquia = _seed()
        evento = _evento_ia(franquia.id, usuario.id)
        real = db.session.commit
        chamadas = {"n": 0}

        def commit():
            chamadas["n"] += 1
            if chamadas["n"] >= 2:
                raise IntegrityError("UPDATE franquia", {}, Exception("FOREIGN KEY constraint failed"))
            return real()

        monkeypatch.setattr(db.session, "commit", commit)
        with pytest.raises(IntegrityError):
            aplicar_motor_apos_ia_consumo_evento(evento.id)
        monkeypatch.undo()
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert IaConsumoAbatimento.query.count() == 0


def test_sql_de_lock_e_incremento_no_postgresql():
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.dialects import sqlite

    from app.services.cleiton_franquia_operacional_service import (
        stmt_incremento_consumo,
        stmt_lock_franquia,
    )

    lock_sql = str(stmt_lock_franquia().compile(dialect=postgresql.dialect()))
    incremento_sql = str(stmt_incremento_consumo().compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in lock_sql.upper()
    assert "consumo_acumulado" in incremento_sql
    assert "+" in incremento_sql
    sqlite_sql = str(stmt_lock_franquia().compile(dialect=sqlite.dialect()))
    assert "FOR UPDATE" not in sqlite_sql.upper()


def test_migration_postgresql_compila_upgrade_e_downgrade():
    from io import StringIO
    from pathlib import Path

    from alembic.config import Config
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from migrations.versions import h6i7j8k9l0m1_ia_chamada_tentativa_reserva as migracao

    raiz = Path(__file__).resolve().parents[1]
    cfg = Config(str(raiz / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(raiz / "migrations"))
    assert ScriptDirectory.from_config(cfg).get_heads() == ["i7j8k9l0m1n2"]

    def _sql(fn) -> str:
        buf = StringIO()
        ctx = MigrationContext.configure(
            dialect_name="postgresql",
            opts={"as_sql": True, "output_buffer": buf, "literal_binds": True},
        )
        with Operations.context(ctx):
            fn()
        return buf.getvalue()

    upgrade_sql = _sql(migracao.upgrade).lower()
    downgrade_sql = _sql(migracao.downgrade).lower()
    assert "boolean default 0" not in upgrade_sql
    assert "boolean default false" in upgrade_sql
    assert "ck_franquia_reserva_pendente_nao_negativa" in upgrade_sql
    assert "ck_ia_chamada_tentativa_status" in upgrade_sql
    assert "uq_ia_consumo_abatimento_evento" in upgrade_sql
    assert "regime_abatimento = 'legado'" in upgrade_sql
    assert "drop table ia_chamada_tentativa" in downgrade_sql
    assert "drop table ia_consumo_abatimento" in downgrade_sql
    assert "drop column regime_abatimento" in downgrade_sql
    assert "drop column reserva_pendente" in downgrade_sql


def _creditos_oficiais(entrada, config=None):
    from app.services.cleiton_billable_ai_call import resolver_limites_config, tokens_teto_total
    from app.services.cleiton_cost_service import get_or_create_config
    from app.services.cleiton_franquia_operacional_service import converter_tokens_para_creditos

    saida, candidatos, thinking, erro = resolver_limites_config(config)
    assert erro is None, erro
    total = tokens_teto_total(entrada, saida, thinking, candidatos)
    creditos, erro_conv = converter_tokens_para_creditos(total, get_or_create_config())
    assert erro_conv is None and creditos is not None
    return creditos


def _count_fixo(total, cache=None):
    def behavior(model, contents, config):
        return SimpleNamespace(total_tokens=total, cached_content_token_count=cache)

    return behavior


def _cfg_saida(**extra):
    config = {
        "max_output_tokens": 10,
        "candidate_count": 1,
        "thinking_config": {"thinking_budget": 0},
    }
    config.update(extra)
    return config


def _tentativa_sem_reserva_definitiva():
    assert IaConsumoEvento.query.count() == 0
    assert ProcessingEvent.query.count() == 0
    ativas = IaChamadaTentativa.query.filter(IaChamadaTentativa.reserva_ativa.is_(True)).count()
    assert ativas == 0


def test_cached_content_usa_count_oficial_e_bloqueia_saldo_curto(app):
    from app.services.cleiton_cost_service import get_or_create_config
    from app.services.cleiton_franquia_operacional_service import converter_tokens_para_creditos

    with app.app_context():
        _seed(email="cache-regua@test.com")
        config = _cfg_saida(cached_content="abc123")
        teto = _creditos_oficiais(55000, config)
        identificador, _erro = converter_tokens_para_creditos(len("abc123") + 10, get_or_create_config())
        assert identificador < Decimal("1") < teto

        usuario, _franquia_row = _outra_conta("cache-exato@test.com", limite=str(teto))
        client = _Client(count_behavior=_count_fixo(55000, cache=55000))
        result = _call(usuario, client, config=config, attempt_key="cache-55000")
        assert result.complete is True
        assert result.reserved_credits == teto
        assert client.models.count_calls[0]["model"] == "gemini-test"
        assert client.models.count_calls[0]["config"]["cached_content"] == "abc123"
        assert client.models.count_calls[0]["config"]["http_options"].retry_options.attempts == 1
        assert len(client.models.calls) == 1
        assert client.models.calls[0]["model"] == "gemini-test"
        assert IaConsumoEvento.query.count() == 1
        assert IaConsumoEvento.query.one().operation == "generate_content"
        assert ProcessingEvent.query.count() == 0

        outro, outra = _outra_conta("cache-curto@test.com", limite="1")
        bloqueado = _Client(count_behavior=_count_fixo(55000, cache=55000))
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(outro, bloqueado, config=config, attempt_key="cache-insuficiente")
        assert exc.value.motivo == "saldo_insuficiente"
        assert bloqueado.models.calls == []
        assert len(bloqueado.models.count_calls) == 1
        assert _franquia(outra.id).reserva_pendente == 0
        assert IaConsumoEvento.query.count() == 1


def test_cached_content_nao_contabilizavel_nao_gera(app):
    with app.app_context():
        usuario, franquia = _seed(email="cache-falha@test.com")
        config = _cfg_saida(cached_content="abc123")

        def falha(model, contents, config):
            raise RuntimeError("cached_content nao suportado")

        client = _Client(count_behavior=falha)
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, config=config, attempt_key="cache-nao-suporta")
        assert exc.value.motivo == "teto_entrada_nao_verificavel"
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert _franquia(franquia.id).reserva_pendente == 0
        _tentativa_sem_reserva_definitiva()

        def sem_metrica(model, contents, config):
            return SimpleNamespace(total_tokens=12)

        client_sem = _Client(count_behavior=sem_metrica)
        with pytest.raises(BillableAiAdmissionBlocked) as sem:
            _call(usuario, client_sem, config=config, attempt_key="cache-sem-metrica")
        assert sem.value.motivo == "teto_entrada_nao_verificavel"
        assert client_sem.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0


def test_property_name_longo_muda_estimativa_e_admissao_segue_count_oficial(app):
    curto = {"properties": {"a": {"type": "string", "title": "t", "description": "d", "enum": ["x"]}}}
    nome = "p" * 55000
    longo = {"properties": {nome: {"type": "string", "title": "t", "description": "d", "enum": ["y"]}}}
    tokens_curtos, erro_curto = estimar_tokens_entrada(curto)
    tokens_longos, erro_longo = estimar_tokens_entrada(longo)
    assert erro_curto is None and erro_longo is None
    assert tokens_longos > tokens_curtos
    assert tokens_longos - tokens_curtos >= 54999

    with app.app_context():
        _seed(email="schema-regua@test.com")
        config = _cfg_saida(
            tools=[
                {
                    "function_declarations": [
                        {"name": "fn", "description": "consulta", "parameters": longo},
                    ]
                }
            ]
        )
        oficial = 30
        teto = _creditos_oficiais(oficial, config)
        local, erro_local = estimar_tokens_entrada("oi", config)
        assert erro_local is None and local > 55000
        from app.services.cleiton_franquia_operacional_service import converter_tokens_para_creditos
        from app.services.cleiton_cost_service import get_or_create_config

        creditos_locais, _erro = converter_tokens_para_creditos(local, get_or_create_config())
        assert creditos_locais > teto

        usuario, _franquia_row = _outra_conta("schema@test.com", limite=str(teto))

        def resposta_dentro_da_reserva(model, contents, config):
            return SimpleNamespace(text="ok", usage_metadata=_usage(10))

        client = _Client(resposta_dentro_da_reserva, count_behavior=_count_fixo(oficial))
        result = _call(usuario, client, contents="oi", config=config, attempt_key="schema-oficial")
        assert result.complete is True
        assert result.reserved_credits == teto
        pedido = client.models.count_calls[0]["config"]
        declaracao = pedido["tools"][0]["function_declarations"][0]["parameters"]["properties"]
        assert nome in declaracao
        assert len(client.models.calls) == 1


def test_system_instruction_grande_reserva_segue_count_oficial(app, caplog):
    import logging

    instrucao = "s" * 55000
    with app.app_context():
        _seed(email="instrucao-regua@test.com")
        config = _cfg_saida(system_instruction=instrucao)
        oficial = 111
        teto = _creditos_oficiais(oficial, config)
        usuario, _franquia_row = _outra_conta("instrucao@test.com", limite=str(teto))
        client = _Client(count_behavior=_count_fixo(oficial))
        with caplog.at_level(logging.INFO):
            result = _call(usuario, client, contents="oi", config=config, attempt_key="instrucao-oficial")
        assert result.complete is True
        assert result.reserved_credits == teto
        assert client.models.count_calls[0]["config"]["system_instruction"] == instrucao
        assert instrucao not in caplog.text
        assert len(client.models.calls) == 1
        assert IaConsumoEvento.query.count() == 1


def test_tools_entram_no_count_oficial_da_reserva(app):
    from google.genai import types

    with app.app_context():
        ferramenta = types.Tool(
            function_declarations=[
                types.FunctionDeclaration(name="frete", description="consulta de tabela"),
            ]
        )
        config = _config_genai(
            tools=[ferramenta],
            max_output_tokens=10,
            candidate_count=1,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        _seed(email="tools-regua@test.com")
        oficial = 777
        teto = _creditos_oficiais(oficial, config)
        usuario, _franquia_row = _outra_conta("tools@test.com", limite=str(teto))
        client = _Client(count_behavior=_count_fixo(oficial))
        result = _call(usuario, client, config=config, attempt_key="tools-oficial")
        assert result.complete is True
        assert result.reserved_credits == teto
        tools = client.models.count_calls[0]["config"]["tools"]
        declaracao = tools[0].function_declarations[0]
        assert declaracao.name == "frete"
        assert len(client.models.calls) == 1


@pytest.mark.parametrize("valor", [False, True, "1", "2", 0, -4, 1.5, None])
def test_candidate_count_invalido_bloqueia_antes_da_contagem(app, valor):
    with app.app_context():
        usuario, franquia = _seed(email=f"cand-{valor}@test.com")
        client = _Client(count_behavior=_count_fixo(10))
        config = _cfg_saida(candidate_count=valor)
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, config=config, attempt_key=f"cand-{valor}")
        assert exc.value.motivo == "teto_saida_nao_garantido"
        assert client.models.count_calls == []
        assert client.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0
        _tentativa_sem_reserva_definitiva()


def test_candidate_count_valido_multiplica_saida_e_thinking(app):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="cand-valido@test.com")
        um = _cfg_saida(candidate_count=1, max_output_tokens=100)
        quatro = _cfg_saida(candidate_count=4, max_output_tokens=100)
        teto_um = _creditos_oficiais(20, um)
        teto_quatro = _creditos_oficiais(20, quatro)
        assert teto_quatro - teto_um == Decimal("0.300")
        primeiro = _call(
            usuario,
            _Client(count_behavior=_count_fixo(20)),
            config=um,
            attempt_key="cand-1",
        )
        assert primeiro.complete is True
        assert primeiro.reserved_credits == teto_um
        db.session.refresh(_franquia(franquia.id))
        segundo = _call(
            usuario,
            _Client(count_behavior=_count_fixo(20)),
            config=quatro,
            attempt_key="cand-4",
        )
        assert segundo.complete is True
        assert segundo.reserved_credits == teto_quatro


def test_count_tokens_falha_nao_reserva_e_nao_gera(app):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="count-falha@test.com")

        def falha(model, contents, config):
            raise TimeoutError("metering")

        client = _Client(count_behavior=falha)
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, contents="oi", attempt_key="count-falha")
        assert exc.value.motivo == "teto_entrada_nao_verificavel"
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert _franquia(franquia.id).reserva_pendente == 0
        _tentativa_sem_reserva_definitiva()


def test_saldo_exato_do_count_oficial_admite(app):
    with app.app_context():
        _seed(email="oficial-regua@test.com")
        config = _cfg_saida()
        teto = _creditos_oficiais(250, config)
        usuario, _franquia_row = _outra_conta("oficial-exato@test.com", limite=str(teto))
        result = _call(
            usuario,
            _Client(count_behavior=_count_fixo(250)),
            config=config,
            attempt_key="oficial-exato",
        )
        assert result.complete is True
        assert result.reserved_credits == teto
        assert IaConsumoEvento.query.filter_by(operation="generate_content").count() == 1


def test_saldo_um_microcredito_abaixo_bloqueia_antes_da_geracao(app):
    with app.app_context():
        _seed(email="oficial-micro-regua@test.com")
        config = _cfg_saida()
        teto = _creditos_oficiais(250, config)
        usuario, franquia = _outra_conta("oficial-micro@test.com", limite="0")
        franquia.limite_total = teto - Decimal("0.000001")
        db.session.commit()
        client = _Client(count_behavior=_count_fixo(250))
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, config=config, attempt_key="oficial-micro")
        assert exc.value.motivo == "saldo_insuficiente"
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaConsumoEvento.query.count() == 0


def test_bytes_e_file_data_falham_fechado_antes_da_contagem(app):
    from google.genai import types

    with app.app_context():
        usuario, franquia = _seed(email="midia@test.com")
        for indice, contents in enumerate(
            (
                b"\x00\x01raw",
                {"file_data": {"file_uri": "gs://bucket/doc", "mime_type": "application/pdf"}},
                {"inline_data": {"mime_type": "image/png", "data": "abcd"}},
                types.Part(
                    file_data=types.FileData(file_uri="https://example.invalid/a", mime_type="text/plain")
                ),
            )
        ):
            client = _Client(count_behavior=_count_fixo(10))
            with pytest.raises(BillableAiAdmissionBlocked) as exc:
                _call(usuario, client, contents=contents, attempt_key=f"midia-{indice}")
            assert exc.value.motivo == "teto_entrada_nao_verificavel"
            assert client.models.count_calls == []
            assert client.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaConsumoEvento.query.count() == 0


def _extra_bloqueador():
    return {
        "systemInstruction": {"parts": [{"text": "S" * 55000}]},
        "generationConfig": {"maxOutputTokens": 65536},
    }


def _config_teto_minimo(**http_options):
    config = {
        "max_output_tokens": 10,
        "thinking_config": {"thinking_budget": 0},
    }
    if http_options:
        config["http_options"] = http_options
    return config


def _ok_dentro_do_teto(model, contents, config):
    return SimpleNamespace(text="ok", usage_metadata=_usage(8))


def _assert_bloqueio_antes_de_metering(client, franquia_id):
    assert client.models.count_calls == []
    assert client.models.calls == []
    assert _franquia(franquia_id).reserva_pendente == 0
    assert IaConsumoEvento.query.count() == 0
    assert ProcessingEvent.query.count() == 0
    assert IaChamadaTentativa.query.filter_by(reserva_ativa=True).count() == 0


def test_extra_body_do_bloqueador_nao_conta_nem_reserva(app):
    with app.app_context():
        usuario, franquia = _seed(email="extra-body@test.com")
        client = _Client(count_behavior=_count_fixo(8))
        config = _config_teto_minimo(extra_body=_extra_bloqueador())
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, config=config, attempt_key="extra-body-bloqueador")
        assert exc.value.motivo == "teto_request_mutavel"
        _assert_bloqueio_antes_de_metering(client, franquia.id)


@pytest.mark.parametrize(
    "corpo",
    [
        {"systemInstruction": {"parts": [{"text": "A"}]}},
        {"contents": [{"parts": [{"text": "B"}]}]},
        {"tools": [{"functionDeclarations": [{"name": "fn"}]}]},
        {"generationConfig": {"maxOutputTokens": 65536}},
        {"generationConfig": {"candidateCount": 4}},
        {"generationConfig": {"thinkingConfig": {"thinkingBudget": 100}}},
        {
            "systemInstruction": {"parts": [{"text": "S"}]},
            "contents": [{"parts": [{"text": "C"}]}],
            "tools": [{"functionDeclarations": [{"name": "fn"}]}],
            "generationConfig": {
                "maxOutputTokens": 65536,
                "candidateCount": 4,
                "thinkingConfig": {"thinkingBudget": 100},
            },
        },
    ],
)
def test_extra_body_nao_vazio_bloqueia_antes_da_contagem(app, corpo):
    with app.app_context():
        usuario, franquia = _seed(email=f"variante-{abs(hash(str(corpo)))}@test.com")
        client = _Client(count_behavior=_count_fixo(8))
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(
                usuario,
                client,
                config=_config_teto_minimo(extra_body=corpo),
                attempt_key=f"variante-{abs(hash(str(corpo)))}",
            )
        assert exc.value.motivo == "teto_request_mutavel"
        _assert_bloqueio_antes_de_metering(client, franquia.id)


@pytest.mark.parametrize(
    "http_options",
    [
        None,
        {"extra_body": None},
        {"extra_body": {}},
        {"timeout": 1500},
        {"timeout": 1500, "extra_body": {}},
    ],
)
def test_extra_body_ausente_ou_vazio_permanece_admissivel(app, http_options):
    with app.app_context():
        usuario, _franquia_row = _seed(email=f"vazio-{abs(hash(str(http_options)))}@test.com")
        client = _Client(_ok_dentro_do_teto, count_behavior=_count_fixo(8))
        if http_options is None:
            config = _config_teto_minimo()
        else:
            config = _config_teto_minimo(**http_options)
        result = _call(
            usuario,
            client,
            config=config,
            attempt_key=f"vazio-{abs(hash(str(http_options)))}",
        )
        assert result.complete is True
        assert len(client.models.count_calls) == 1
        assert len(client.models.calls) == 1


def test_mutacao_do_chamador_depois_da_contagem_nao_entra_na_geracao(app):
    with app.app_context():
        usuario, _franquia_row = _seed(email="mutacao@test.com")
        instrucao = {"parts": [{"text": "estavel"}]}
        config = {
            "max_output_tokens": 10,
            "thinking_config": {"thinking_budget": 0},
            "system_instruction": instrucao,
        }

        def contar(model, contents, config_contada):
            config_contada["system_instruction"]["parts"][0]["text"] = "MUTADO-NA-CONTAGEM"
            config_contada["max_output_tokens"] = 65536
            instrucao["parts"][0]["text"] = "MUTADO-NO-CHAMADOR"
            config["http_options"] = {"extra_body": _extra_bloqueador()}
            return SimpleNamespace(total_tokens=8, cached_content_token_count=None)

        client = _Client(_ok_dentro_do_teto, count_behavior=contar)
        result = _call(usuario, client, contents="frete contado", config=config, attempt_key="mutacao")
        assert result.complete is True
        gerada = client.models.calls[0]["config"]
        assert gerada["system_instruction"]["parts"][0]["text"] == "estavel"
        assert gerada["max_output_tokens"] == 10
        assert gerada["thinking_config"]["thinking_budget"] == 0
        assert gerada["candidate_count"] == 1
        http_opts = gerada["http_options"]
        extra = http_opts.extra_body if not isinstance(http_opts, dict) else http_opts.get("extra_body")
        assert extra in (None, {})
        assert client.models.calls[0]["contents"] == "frete contado"


def test_gancho_http_que_altera_corpo_bloqueia_antes_da_contagem(app):
    with app.app_context():
        usuario, franquia = _seed(email="gancho@test.com")
        cliente_proprio = _Client(count_behavior=_count_fixo(8))
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(
                usuario,
                cliente_proprio,
                config=_config_teto_minimo(httpx_client=object()),
                attempt_key="gancho-cliente",
            )
        assert exc.value.motivo == "teto_request_mutavel"
        _assert_bloqueio_antes_de_metering(cliente_proprio, franquia.id)

        outro, outra = _outra_conta("gancho-hook@test.com")
        com_hook = _Client(count_behavior=_count_fixo(8))
        with pytest.raises(BillableAiAdmissionBlocked) as hook:
            _call(
                outro,
                com_hook,
                config=_config_teto_minimo(client_args={"event_hooks": {"request": [object()]}}),
                attempt_key="gancho-hook",
            )
        assert hook.value.motivo == "teto_request_mutavel"
        assert com_hook.models.count_calls == []
        assert _franquia(outra.id).reserva_pendente == 0


def test_governanca_nao_repassa_extra_body():
    from app.services.cleiton_ai_data_governance import (
        CleitonAiGovernanceBlockedError,
        govern_provider_config,
    )

    with pytest.raises(CleitonAiGovernanceBlockedError) as exc:
        govern_provider_config(
            _config_teto_minimo(extra_body=_extra_bloqueador()),
            purpose="comparacao_fretes",
            agent="agente_compara",
            provider="gemini",
        )
    assert "teto_request_mutavel" in exc.value.reason_codes

    permitido = govern_provider_config(
        _config_teto_minimo(timeout=1500, extra_body={}),
        purpose="comparacao_fretes",
        agent="agente_compara",
        provider="gemini",
    )
    assert permitido["http_options"]["timeout"] == 1500
    assert permitido["http_options"]["extra_body"] == {}


def _cliente_sdk(capturados):
    import httpx
    from google import genai

    def handler(request: httpx.Request) -> httpx.Response:
        capturados.append(
            {
                "url": str(request.url),
                "body": request.content.decode("utf-8") if request.content else "",
            }
        )
        url = str(request.url)
        if "countTokens" in url:
            return httpx.Response(200, json={"totalTokens": 8})
        if "generateContent" in url:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "ok"}]},
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 8,
                        "candidatesTokenCount": 1,
                        "totalTokenCount": 9,
                    },
                },
            )
        return httpx.Response(500, json={"error": {"code": 500, "message": "inesperado", "status": "INTERNAL"}})

    client = genai.Client(api_key="teste")
    client._api_client._httpx_client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _nos(body, chave):
    achados = []
    if isinstance(body, dict):
        for nome, valor in body.items():
            if nome == chave:
                achados.append(valor)
            achados.extend(_nos(valor, chave))
    elif isinstance(body, list):
        for item in body:
            achados.extend(_nos(item, chave))
    return achados


def test_sdk_extra_body_nao_gera_http_nem_reserva(app):
    from google.genai import types

    with app.app_context():
        usuario, franquia = _seed(email="sdk-extra@test.com")
        capturados = []
        client = _cliente_sdk(capturados)
        config = types.GenerateContentConfig(
            max_output_tokens=10,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            http_options=types.HttpOptions(extra_body=_extra_bloqueador()),
        )
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, contents="frete contado", config=config, attempt_key="sdk-extra")
        assert exc.value.motivo == "teto_request_mutavel"
        assert capturados == []
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaConsumoEvento.query.count() == 0
        assert ProcessingEvent.query.count() == 0


def test_sdk_cliente_com_extra_body_bloqueia_antes_do_http(app):
    from google import genai
    from google.genai import types

    with app.app_context():
        usuario, franquia = _seed(email="sdk-cliente-extra@test.com")
        capturados = []
        transporte = _cliente_sdk(capturados)
        client = genai.Client(
            api_key="teste",
            http_options=types.HttpOptions(extra_body={"generationConfig": {"maxOutputTokens": 65536}}),
        )
        client._api_client._httpx_client = transporte._api_client._httpx_client
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, contents="frete contado", config=_config_teto_minimo(), attempt_key="sdk-cliente")
        assert exc.value.motivo == "teto_request_mutavel"
        assert capturados == []
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaConsumoEvento.query.count() == 0


def test_sdk_count_e_generate_enviam_o_mesmo_conteudo_faturavel(app):
    import json

    from google.genai import types

    with app.app_context():
        _seed(email="sdk-regua@test.com")
        config = types.GenerateContentConfig(
            max_output_tokens=10,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        teto = _creditos_oficiais(8, config)
        usuario, _franquia_row = _outra_conta("sdk-eq@test.com", limite=str(teto))
        capturados = []
        result = _call(
            usuario,
            _cliente_sdk(capturados),
            contents="frete contado",
            config=config,
            attempt_key="sdk-eq",
        )
        assert result.complete is True
        assert result.reserved_credits == teto
        counts = [item for item in capturados if "countTokens" in item["url"]]
        generates = [item for item in capturados if "generateContent" in item["url"]]
        assert len(counts) == 1
        assert len(generates) == 1
        corpo_count = json.loads(counts[0]["body"])
        corpo_generate = json.loads(generates[0]["body"])
        assert _nos(corpo_count, "text") == ["frete contado"]
        assert _nos(corpo_generate, "text") == ["frete contado"]
        assert "S" * 100 not in generates[0]["body"]
        assert _nos(corpo_generate, "maxOutputTokens") == [10]
        assert _nos(corpo_generate, "candidateCount") == [1]
        assert _nos(corpo_generate, "thinkingBudget") + _nos(corpo_generate, "thinking_budget") == [0]
        assert _nos(corpo_generate, "systemInstruction") == []
        assert IaConsumoEvento.query.count() == 1
        assert ProcessingEvent.query.count() == 0


_BLOQUEIOS_TRANSPORTE = [
    ("auth", {"client_args": {"auth": object()}}),
    ("async-auth", {"async_client_args": {"auth": object()}}),
    ("hooks", {"client_args": {"event_hooks": {"request": [object()]}}}),
    ("transport", {"client_args": {"transport": object()}}),
    ("mounts", {"client_args": {"mounts": {"https://": object()}}}),
    ("chave", {"client_args": {"desconhecida": 1}}),
    ("async-chave", {"async_client_args": {"desconhecida": 1}}),
    ("proxy", {"client_args": {"proxy": "http://127.0.0.1:9"}}),
    ("verify", {"client_args": {"verify": False}}),
    ("httpx", {"httpx_client": object()}),
    ("httpx-async", {"httpx_async_client": object()}),
    ("aiohttp", {"aiohttp_client": object()}),
    ("desconhecido", {"extensao_desconhecida": {"hook": True}}),
    ("escopo", {"base_url_resource_scope": "COLLECTION"}),
    ("header", {"headers": {"X-Hook": object()}}),
]

_PERMITIDOS_TRANSPORTE = [
    ("vazio", {"client_args": {}}),
    ("none", {"client_args": None}),
    ("ausente", {}),
    ("async-vazio", {"async_client_args": {}}),
    ("async-none", {"async_client_args": None}),
    (
        "estaticos",
        {
            "timeout": 1500,
            "base_url": "https://generativelanguage.googleapis.com/",
            "api_version": "v1beta",
            "headers": {"Content-Type": "application/json"},
            "extra_body": {},
            "client_args": {},
            "async_client_args": {},
        },
    ),
]


def _cliente_com_transporte(campos, behavior=_ok_dentro_do_teto):
    client = _Client(behavior, count_behavior=_count_fixo(8))
    dados = {"retry_options": SimpleNamespace(attempts=1)}
    dados.update(campos)
    client.http_options = SimpleNamespace(**dados)
    return client


@pytest.mark.parametrize("lugar", ["cliente", "config"])
@pytest.mark.parametrize("rotulo, campos", _BLOQUEIOS_TRANSPORTE)
def test_transporte_fora_da_allowlist_bloqueia_antes_da_contagem(app, lugar, rotulo, campos):
    with app.app_context():
        usuario, franquia = _seed(email=f"tr-{lugar}-{rotulo}@test.com")
        if lugar == "cliente":
            client = _cliente_com_transporte(campos)
            config = _config_teto_minimo()
        else:
            client = _Client(count_behavior=_count_fixo(8))
            config = _config_teto_minimo(**campos)
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(usuario, client, config=config, attempt_key=f"tr-{lugar}-{rotulo}")
        assert exc.value.motivo == "teto_request_mutavel"
        _assert_bloqueio_antes_de_metering(client, franquia.id)


@pytest.mark.parametrize("lugar", ["cliente", "config"])
@pytest.mark.parametrize("rotulo, campos", _PERMITIDOS_TRANSPORTE)
def test_transporte_allowlist_permanece_admissivel(app, lugar, rotulo, campos):
    with app.app_context():
        usuario, _franquia_row = _seed(email=f"ok-{lugar}-{rotulo}@test.com")
        if lugar == "cliente":
            client = _cliente_com_transporte(campos)
            config = _config_teto_minimo()
        else:
            client = _Client(_ok_dentro_do_teto, count_behavior=_count_fixo(8))
            config = _config_teto_minimo(**campos)
        result = _call(usuario, client, config=config, attempt_key=f"ok-{lugar}-{rotulo}")
        assert result.complete is True
        assert len(client.models.count_calls) == 1
        assert len(client.models.calls) == 1
        gerada = client.models.calls[0]["config"]
        http_opts = gerada["http_options"]
        args = http_opts.client_args if not isinstance(http_opts, dict) else http_opts.get("client_args")
        async_args = (
            http_opts.async_client_args if not isinstance(http_opts, dict) else http_opts.get("async_client_args")
        )
        assert args in (None, {})
        assert async_args in (None, {})
        if lugar == "config" and rotulo == "estaticos":
            timeout = http_opts.timeout if not isinstance(http_opts, dict) else http_opts.get("timeout")
            assert timeout == 1500


def _auth_mutante():
    import httpx

    class MutatingAuth(httpx.Auth):
        requires_request_body = True

        def auth_flow(self, request):
            import json

            if "generateContent" not in str(request.url):
                yield request
                return
            corpo = json.loads(request.content.decode("utf-8") or "{}")
            corpo["systemInstruction"] = {"parts": [{"text": "S" * 55000}]}
            geracao = corpo.setdefault("generationConfig", {})
            geracao["maxOutputTokens"] = 65536
            geracao["candidateCount"] = 4
            geracao["thinkingConfig"] = {"thinkingBudget": 100000}
            alterado = httpx.Request(
                request.method,
                request.url,
                headers=request.headers,
                content=json.dumps(corpo).encode("utf-8"),
            )
            yield alterado
            yield alterado

    return MutatingAuth()


def _instrumentar_http(client, capturados):
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        capturados.append(
            {
                "url": str(request.url),
                "body": request.content.decode("utf-8") if request.content else "",
            }
        )
        url = str(request.url)
        if "countTokens" in url:
            return httpx.Response(200, json={"totalTokens": 8})
        if "generateContent" in url:
            return httpx.Response(
                200,
                json={
                    "candidates": [
                        {
                            "content": {"role": "model", "parts": [{"text": "ok"}]},
                            "finishReason": "STOP",
                            "index": 0,
                        }
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 8,
                        "candidatesTokenCount": 1,
                        "totalTokenCount": 9,
                    },
                },
            )
        return httpx.Response(500, json={"error": {"code": 500, "message": "inesperado", "status": "INTERNAL"}})

    auth = getattr(client._api_client._httpx_client, "auth", None)
    client._api_client._httpx_client = httpx.Client(auth=auth, transport=httpx.MockTransport(handler), timeout=1.0)
    return client


def _assert_sdk_sem_efeito(capturados, franquia_id):
    assert capturados == []
    assert _franquia(franquia_id).reserva_pendente == 0
    assert IaConsumoEvento.query.count() == 0
    assert ProcessingEvent.query.count() == 0
    assert IaChamadaTentativa.query.filter_by(reserva_ativa=True).count() == 0


def test_sdk_client_args_auth_nao_gera_http_nem_reserva(app):
    from google import genai
    from google.genai import types

    with app.app_context():
        usuario, franquia = _seed(email="sdk-auth@test.com")
        auth = _auth_mutante()
        capturados = []
        client = genai.Client(
            api_key="teste",
            http_options=types.HttpOptions(client_args={"auth": auth}),
        )
        assert client._api_client._http_options.client_args["auth"] is auth
        _instrumentar_http(client, capturados)
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(
                usuario,
                client,
                contents="frete contado",
                config=_config_teto_minimo(),
                attempt_key="sdk-auth",
            )
        assert exc.value.motivo == "teto_request_mutavel"
        _assert_sdk_sem_efeito(capturados, franquia.id)
        assert not any("countTokens" in item["url"] for item in capturados)
        assert not any("generateContent" in item["url"] for item in capturados)


def test_sdk_async_client_args_auth_nao_gera_http_nem_reserva(app):
    from google import genai
    from google.genai import types

    with app.app_context():
        usuario, franquia = _seed(email="sdk-async-auth@test.com")
        auth = _auth_mutante()
        capturados = []
        client = genai.Client(
            api_key="teste",
            http_options=types.HttpOptions(async_client_args={"auth": auth}),
        )
        assert client._api_client._http_options.async_client_args["auth"] is auth
        _instrumentar_http(client, capturados)
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(
                usuario,
                client,
                contents="frete contado",
                config=_config_teto_minimo(),
                attempt_key="sdk-async-auth",
            )
        assert exc.value.motivo == "teto_request_mutavel"
        _assert_sdk_sem_efeito(capturados, franquia.id)


def test_sdk_client_args_vazio_mantem_um_count_e_um_generate(app):
    import json

    from google import genai
    from google.genai import types

    with app.app_context():
        _seed(email="sdk-regua-args@test.com")
        config = types.GenerateContentConfig(
            max_output_tokens=10,
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        )
        teto = _creditos_oficiais(8, config)
        usuario, _franquia_row = _outra_conta("sdk-args-vazio@test.com", limite=str(teto))
        capturados = []
        client = genai.Client(
            api_key="teste",
            http_options=types.HttpOptions(client_args={}, async_client_args={}),
        )
        _instrumentar_http(client, capturados)
        result = _call(
            usuario,
            client,
            contents="frete contado",
            config=config,
            attempt_key="sdk-args-vazio",
        )
        assert result.complete is True
        assert result.reserved_credits == teto
        counts = [item for item in capturados if "countTokens" in item["url"]]
        generates = [item for item in capturados if "generateContent" in item["url"]]
        assert len(counts) == 1
        assert len(generates) == 1
        corpo = json.loads(generates[0]["body"])
        assert _nos(corpo, "text") == ["frete contado"]
        assert _nos(corpo, "maxOutputTokens") == [10]
        assert _nos(corpo, "candidateCount") == [1]
        assert IaConsumoEvento.query.count() == 1
        assert ProcessingEvent.query.count() == 0


def test_max_reserved_credits_ausente_mantem_o_fluxo(app):
    with app.app_context():
        usuario, _franquia_row = _seed(email="teto-ausente@test.com")
        client = _Client(_ok_dentro_do_teto, count_behavior=_count_fixo(8))
        result = _call(usuario, client, config=_config_teto_minimo(), attempt_key="teto-ausente")
        assert result.complete is True
        assert len(client.models.calls) == 1
        assert IaChamadaTentativa.query.filter_by(status="settled").count() == 1


def test_max_reserved_credits_no_teto_ou_acima_segue(app):
    with app.app_context():
        usuario, _franquia_row = _seed(email="teto-igual@test.com")
        config = _config_teto_minimo()
        teto = _creditos_oficiais(8, config)
        result = _call(
            usuario,
            _Client(_ok_dentro_do_teto, count_behavior=_count_fixo(8)),
            config=config,
            attempt_key="teto-igual",
            max_reserved_credits=teto,
        )
        assert result.complete is True
        assert result.reserved_credits == teto


def test_max_reserved_credits_abaixo_do_teto_nao_reserva_franquia(app):
    with app.app_context():
        usuario, franquia = _seed(email="teto-baixo@test.com")
        client = _Client(count_behavior=_count_fixo(8))
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(
                usuario,
                client,
                config=_config_teto_minimo(),
                attempt_key="teto-baixo",
                max_reserved_credits=Decimal("0.000001"),
            )
        assert exc.value.motivo == "teto_excede_reserva_job"
        assert client.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert IaConsumoEvento.query.count() == 0


def _definir_regua(rate):
    from app.services.cleiton_cost_service import get_or_create_config

    cfg = get_or_create_config()
    cfg.credit_tokens_per_credit = rate
    db.session.commit()
    return cfg


def _assert_sem_geracao(capturados, franquia_id, chave):
    from app.services.conta_franquia_service import get_sistema_interno_ids

    counts = [item for item in capturados if "countTokens" in item["url"]]
    generates = [item for item in capturados if "generateContent" in item["url"]]
    assert len(counts) == 1
    assert len(generates) == 0
    assert _franquia(franquia_id).reserva_pendente == 0
    assert _franquia(franquia_id).consumo_acumulado == 0
    _cid, sid = get_sistema_interno_ids()
    if sid is not None:
        assert _franquia(sid).reserva_pendente == 0
    assert IaConsumoEvento.query.count() == 0
    assert ProcessingEvent.query.count() == 0
    assert IaChamadaTentativa.query.filter_by(status="calling").count() == 0
    assert IaChamadaTentativa.query.filter_by(status="uncertain").count() == 0
    tentativa = IaChamadaTentativa.query.filter_by(attempt_key=chave).one()
    assert tentativa.status == "blocked"
    assert tentativa.reserva_ativa is False
    assert tentativa.actual_credits is None


@pytest.mark.parametrize("origem", [False, True])
def test_regua_invalida_com_hard_cap_nao_gera(app, origem):
    with app.app_context():
        usuario, franquia = _seed(email=f"regua-invalida-{origem}@test.com")
        _definir_regua(0)
        capturados = []
        chave = f"regua-invalida-{origem}"
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(
                usuario,
                _cliente_sdk(capturados),
                model="gemini-2.5-flash",
                contents="frete contado",
                max_output_tokens=512,
                origem_sistema=origem,
                max_reserved_credits=Decimal("0.000001"),
                attempt_key=chave,
            )
        assert not isinstance(exc.value, BillableAiUncertainError)
        assert exc.value.motivo == "teto_creditos_nao_verificavel"
        _assert_sem_geracao(capturados, franquia.id, chave)


def test_origem_sistema_regua_valida_acima_do_hard_cap_nao_gera(app):
    with app.app_context():
        usuario, franquia = _seed(email="origem-cap-insuficiente@test.com")
        capturados = []
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(
                usuario,
                _cliente_sdk(capturados),
                model="gemini-2.5-flash",
                contents="frete contado",
                max_output_tokens=512,
                origem_sistema=True,
                max_reserved_credits=Decimal("0.000001"),
                attempt_key="origem-cap-insuficiente",
            )
        assert exc.value.motivo == "teto_excede_reserva_job"
        _assert_sem_geracao(capturados, franquia.id, "origem-cap-insuficiente")


def test_origem_sistema_regua_valida_dentro_do_hard_cap_segue(app):
    with app.app_context():
        _seed(email="origem-cap-ok-regua@test.com")
        config = _config_teto_minimo()
        teto = _creditos_oficiais(8, config)
        usuario, franquia = _outra_conta("origem-cap-ok@test.com", limite=str(teto))
        client = _Client(_ok_dentro_do_teto, count_behavior=_count_fixo(8))
        result = _call(
            usuario,
            client,
            config=config,
            origem_sistema=True,
            max_reserved_credits=teto,
            attempt_key="origem-cap-ok",
        )
        assert result.complete is True
        assert result.status == "settled"
        assert result.actual_credits is not None
        assert len(client.models.count_calls) == 1
        assert len(client.models.calls) == 1
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert IaChamadaTentativa.query.filter_by(status="uncertain").count() == 0


@pytest.mark.parametrize("origem", [False, True])
def test_teto_zero_calculado_passa_o_hard_cap(app, origem):
    with app.app_context():
        usuario, _franquia = _seed(email=f"zero-real-{origem}@test.com")
        _definir_regua(10**15)
        config = _config_teto_minimo()
        teto = _creditos_oficiais(8, config)
        assert teto == Decimal("0")
        client = _Client(_ok_dentro_do_teto, count_behavior=_count_fixo(8))
        result = _call(
            usuario,
            client,
            config=config,
            origem_sistema=origem,
            max_reserved_credits=Decimal("0"),
            attempt_key=f"zero-real-{origem}",
        )
        assert result.complete is True
        assert result.status == "settled"
        assert len(client.models.count_calls) == 1
        assert len(client.models.calls) == 1
        assert IaChamadaTentativa.query.filter_by(status="blocked").count() == 0
        assert IaChamadaTentativa.query.filter_by(status="uncertain").count() == 0


def test_cliente_sem_hard_cap_e_sem_regua_bloqueia_antes_do_provider(app):
    with app.app_context():
        usuario, franquia = _seed(email="sem-cap-sem-regua@test.com")
        _definir_regua(0)
        client = _Client(count_behavior=_count_fixo(8))
        with pytest.raises(BillableAiAdmissionBlocked) as exc:
            _call(
                usuario,
                client,
                config=_config_teto_minimo(),
                origem_sistema=False,
                max_output_tokens=512,
                attempt_key="sem-cap-sem-regua",
            )
        assert exc.value.motivo == "regua_indisponivel"
        assert len(client.models.count_calls) == 1
        assert client.models.calls == []
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaConsumoEvento.query.count() == 0
        assert IaChamadaTentativa.query.filter_by(status="calling").count() == 0


def test_origem_sistema_sem_hard_cap_preserva_fluxo_com_regua_invalida(app):
    with app.app_context():
        usuario, franquia = _seed(email="origem-sem-cap@test.com")
        _definir_regua(0)
        client = _Client(_ok_dentro_do_teto, count_behavior=_count_fixo(8))
        with pytest.raises(BillableAiUncertainError) as exc:
            _call(
                usuario,
                client,
                config=_config_teto_minimo(),
                origem_sistema=True,
                max_output_tokens=512,
                attempt_key="origem-sem-cap",
            )
        assert exc.value.motivo == "falha_conversao_creditos"
        assert len(client.models.count_calls) == 1
        assert len(client.models.calls) == 1
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert IaChamadaTentativa.query.filter_by(status="calling").count() == 0
