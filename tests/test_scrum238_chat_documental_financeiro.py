"""SCRUM-238: chat documental consome como a Home, sem reserva.

Provedor simulado e banco isolado. Não chama o Gemini real.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from app.extensions import db
from app.models import Franquia, IaChamadaTentativa, IaConsumoAbatimento, IaConsumoEvento, utcnow_naive
from app.services.cleiton_billable_ai_call import (
    BILLABLE_AI_MAX_OUTPUT_TOKENS,
    BILLABLE_AI_MAX_THINKING_TOKENS,
)
from app.services.cleiton_cost_service import get_or_create_config
from app.services.cleiton_franquia_operacional_service import (
    aplicar_motor_apos_ia_consumo_evento,
    converter_tokens_para_creditos,
)
from app.services.cleiton_operacao_autorizacao_service import (
    avaliar_autorizacao_operacao_por_franquia,
)
from tests.conftest import (
    seed_cleiton_cost_config,
    seed_conta_franquia_cliente,
    seed_sistema_interno,
    seed_usuario,
)


def _usage(total: int):
    return SimpleNamespace(
        prompt_token_count=total,
        candidates_token_count=0,
        total_token_count=total,
    )


class _Models:
    def __init__(self, behavior):
        self.calls = []
        self._behavior = behavior

    def generate_content(self, *, model, contents, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        return self._behavior(model, contents, config)


class _Client:
    def __init__(self, behavior, attempts=1):
        self.models = _Models(behavior)
        self.http_options = SimpleNamespace(retry_options=SimpleNamespace(attempts=attempts))


def _ok(model, contents, config):
    return SimpleNamespace(text="resposta oficial", usage_metadata=_usage(100))


def _seed(limite="100", email="doc-chat@test.com", categoria="free", *, interno=False):
    seed_cleiton_cost_config()
    if interno:
        conta, franquia = seed_sistema_interno()
    else:
        seed_sistema_interno()
        conta, franquia = seed_conta_franquia_cliente(slug=f"conta-{email}")
    franquia.limite_total = None if limite is None else Decimal(limite)
    franquia.consumo_acumulado = Decimal("0")
    franquia.reserva_pendente = Decimal("0")
    franquia.bloqueio_manual = False
    franquia.inicio_ciclo = utcnow_naive()
    franquia.fim_ciclo = utcnow_naive() + timedelta(days=28)
    db.session.commit()
    usuario = seed_usuario(franquia.id, conta.id, email=email, categoria=categoria)
    return usuario, franquia


def _ligar(monkeypatch, client, usuario, models=("gemini-2.5-flash",)):
    from app.models import User

    usuario_id = usuario.id
    monkeypatch.setattr("app.run_agente_compara_chat._get_client", lambda: client)
    monkeypatch.setattr(
        "app.run_agente_compara_chat._get_model_candidates",
        lambda: list(models),
    )
    monkeypatch.setattr(
        "app.run_agente_compara_chat.usuario_operacional_da_chamada",
        lambda usuario=None: db.session.get(User, usuario_id),
    )


def _responder(monkeypatch, usuario, behavior=_ok, models=("gemini-2.5-flash",), attempts=1):
    client = _Client(behavior, attempts=attempts)
    _ligar(monkeypatch, client, usuario, models)
    from app.run_agente_compara_chat import chat_agente_compara_reply

    return client, chat_agente_compara_reply("ola", [])


def _franquia(fid):
    db.session.expire_all()
    return db.session.get(Franquia, fid)


def _creditos(tokens: int) -> Decimal:
    valor, erro = converter_tokens_para_creditos(tokens, get_or_create_config())
    assert erro is None and valor is not None
    return valor


def _sem_reserva():
    assert IaChamadaTentativa.query.count() == 0
    atualizadas = Franquia.query.all()
    assert all((row.reserva_pendente or 0) == 0 for row in atualizadas)


def test_free_com_credito_registra_consumo_e_debita(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed()
        client, out = _responder(monkeypatch, usuario)
        assert out.get("answer") == "resposta oficial"
        assert out.get("error") is None
        assert len(client.models.calls) == 1
        evento = IaConsumoEvento.query.one()
        assert evento.flow_type == "agente_compara_chat"
        assert evento.franquia_id == franquia.id
        assert evento.usuario_id == usuario.id
        assert evento.conta_id == usuario.conta_id
        assert evento.origem_sistema is False
        assert evento.status == "success"
        assert IaConsumoAbatimento.query.count() == 1
        assert _franquia(franquia.id).consumo_acumulado == _creditos(100)
        _sem_reserva()


def test_free_sem_credito_bloqueia_e_preserva_cta(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="1")
        franquia.consumo_acumulado = Decimal("1")
        db.session.commit()
        authz = avaliar_autorizacao_operacao_por_franquia(usuario)
        assert authz["permitido"] is False
        assert authz["sugerir_upgrade"] is True
        cta = authz["upgrade_cta"]
        assert cta["upgrade_url"]
        assert cta["upgrade_label"]
        client, out = _responder(monkeypatch, usuario)
        assert client.models.calls == []
        assert out.get("answer") == ""
        assert out.get("error") == "processing_failed"
        assert out.get("message") == authz["mensagem_usuario"]
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == Decimal("1")
        _sem_reserva()


def test_franquia_ilimitada_registra_consumo_e_aplica_o_motor(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite=None, email="ilimitada-chat@test.com")
        _client, out = _responder(monkeypatch, usuario)
        assert out.get("answer") == "resposta oficial"
        assert IaConsumoEvento.query.one().franquia_id == franquia.id
        assert IaConsumoAbatimento.query.count() == 1
        assert _franquia(franquia.id).consumo_acumulado == _creditos(100)
        _sem_reserva()


def test_franquia_interna_segue_a_politica_central(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="0", email="interno-chat@test.com", interno=True)
        _client, out = _responder(monkeypatch, usuario)
        assert out.get("answer") == "resposta oficial"
        evento = IaConsumoEvento.query.one()
        assert evento.franquia_id == franquia.id
        assert evento.origem_sistema is False
        assert IaConsumoAbatimento.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        _sem_reserva()


def test_falha_de_persistencia_nao_conclui_nem_faz_fallback(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="persistencia-chat@test.com")

        def sem_evento(*_args, **_kwargs):
            return None, "persist_failed"

        monkeypatch.setattr("app.run_cleiton_gemini_governance._persist_event", sem_evento)
        client, out = _responder(
            monkeypatch,
            usuario,
            models=("modelo-a", "modelo-b"),
        )
        assert out.get("answer") == ""
        assert out.get("error") == "processing_failed"
        assert [item["model"] for item in client.models.calls] == ["modelo-a"]
        assert IaConsumoEvento.query.count() == 0
        assert IaConsumoAbatimento.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        _sem_reserva()


def test_falha_de_abatimento_nao_inicia_fallback(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="abatimento-chat@test.com")

        def boom(*_args, **_kwargs):
            raise RuntimeError("abatimento")

        monkeypatch.setattr(
            "app.services.cleiton_franquia_operacional_service.aplicar_motor_apos_ia_consumo_evento",
            boom,
        )
        client, out = _responder(
            monkeypatch,
            usuario,
            models=("modelo-a", "modelo-b"),
        )
        assert out.get("answer") == ""
        assert [item["model"] for item in client.models.calls] == ["modelo-a"]
        assert IaConsumoEvento.query.count() == 1
        assert IaConsumoAbatimento.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        _sem_reserva()


def test_resposta_vazia_com_saldo_reautoriza_antes_da_segunda_geracao(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(email="vazia-saldo@test.com")
        etapas = []

        def behavior(model, contents, config):
            etapas.append(model)
            if model == "modelo-a":
                return SimpleNamespace(text="  ", usage_metadata=_usage(100))
            return SimpleNamespace(text="segunda", usage_metadata=_usage(100))

        client, out = _responder(
            monkeypatch,
            usuario,
            behavior=behavior,
            models=("modelo-a", "modelo-b"),
        )
        assert etapas == ["modelo-a", "modelo-b"]
        assert out.get("answer") == "segunda"
        assert len(client.models.calls) == 2
        assert IaConsumoEvento.query.count() == 2
        assert IaConsumoAbatimento.query.count() == 2
        _sem_reserva()


def test_resposta_vazia_sem_metrica_nao_autoriza_segunda_geracao(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="vazia-incerta@test.com")

        def behavior(model, contents, config):
            return SimpleNamespace(text="   ")

        client, out = _responder(
            monkeypatch,
            usuario,
            behavior=behavior,
            models=("modelo-a", "modelo-b"),
        )
        assert out.get("answer") == ""
        assert [item["model"] for item in client.models.calls] == ["modelo-a"]
        assert IaConsumoAbatimento.query.count() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        _sem_reserva()


def test_mesmo_evento_nao_debita_duas_vezes(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(email="repetido-chat@test.com")
        _client, primeiro = _responder(monkeypatch, usuario)
        assert primeiro.get("answer") == "resposta oficial"
        evento_id = IaConsumoEvento.query.one().id
        antes = _franquia(franquia.id).consumo_acumulado
        repetido = aplicar_motor_apos_ia_consumo_evento(evento_id)
        assert repetido.motivo_nao_abateu == "ja_apropriado"
        assert repetido.abateu_franquia is False
        assert _franquia(franquia.id).consumo_acumulado == antes
        assert IaConsumoAbatimento.query.count() == 1
        _client_2, segundo = _responder(monkeypatch, usuario)
        assert segundo.get("answer") == "resposta oficial"
        assert IaConsumoEvento.query.count() == 2
        assert IaConsumoAbatimento.query.count() == 2
        assert _franquia(franquia.id).consumo_acumulado == antes + _creditos(100)
        _sem_reserva()


def test_retry_automatico_do_sdk_nao_chama_o_provedor(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(email="sdk-chat@test.com")
        client, out = _responder(monkeypatch, usuario, attempts=5)
        assert client.models.calls == []
        assert out.get("answer") == ""
        assert IaConsumoEvento.query.count() == 0
        _sem_reserva()


def test_configuracao_efetiva_de_saida_e_thinking_permanece(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(email="config-chat@test.com")
        client, out = _responder(monkeypatch, usuario)
        assert out.get("answer") == "resposta oficial"
        config = client.models.calls[0]["config"]
        assert config.max_output_tokens == BILLABLE_AI_MAX_OUTPUT_TOKENS
        assert config.candidate_count == 1
        assert config.thinking_config.thinking_budget == BILLABLE_AI_MAX_THINKING_TOKENS
        retry = config.http_options.retry_options
        assert retry.attempts == 1


def test_chat_documental_nao_referencia_reserva(app):
    source = Path("app/run_agente_compara_chat.py").read_text(encoding="utf-8")
    assert "gemini_http_options" in source
    assert "cleiton_governed_generate_content_observed" in source
    assert "aplicar_motor_apos_ia_consumo_evento" in source
    assert "avaliar_autorizacao_operacao_por_franquia" in source
    for proibido in (
        "IaChamadaTentativa",
        "attempt_key",
        "cleiton_governed_billable_ai_call",
        "liquidar_tentativa_ia",
        "_reservar",
    ):
        assert proibido not in source


def test_identidade_ausente_nao_chama_provedor_nem_vira_sistema(app, monkeypatch):
    with app.app_context():
        _seed(email="sem-identidade@test.com")
        client = _Client(_ok)
        monkeypatch.setattr("app.run_agente_compara_chat._get_client", lambda: client)
        monkeypatch.setattr(
            "app.run_agente_compara_chat._get_model_candidates",
            lambda: ["modelo-a", "modelo-b"],
        )
        from app.run_agente_compara_chat import chat_agente_compara_reply

        out = chat_agente_compara_reply("ola", [])
        assert client.models.calls == []
        assert out.get("answer") == ""
        assert IaConsumoEvento.query.filter_by(origem_sistema=True).count() == 0
        _sem_reserva()
