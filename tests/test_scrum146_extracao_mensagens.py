"""SCRUM-146 lote 4: mensagens e CTA da extração do AgenteCompara.

Provedor simulado. Não altera teto, preço, reserva nem débito por entrega útil.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.agente_compara_doc_service import (
    ERROR_EXTRACAO_CONSUMO_INCERTO,
    ERROR_EXTRACAO_CREDITOS_INSUFICIENTES,
    ERROR_EXTRACAO_JSON_INVALIDO,
    ERROR_EXTRACAO_PROVEDOR_INDISPONIVEL,
    ERROR_EXTRACAO_RESPOSTA_TRUNCADA,
    ERROR_EXTRACAO_TETO_TECNICO,
    _classify_temp_table_failure,
    _public_temp_table,
)
from app.models import IaChamadaTentativa
from app.services.agente_compara_extraction_budget import EXTRACAO_TETO_TECNICO_TOKENS
from app.services.cleiton_mensageria_operacao_service import (
    UPGRADE_LABEL_DEFAULT,
    UPGRADE_PATH_DEFAULT,
)
from tests.test_scrum146_extracao_orcamento import (
    DOC_SECRET,
    VALID_JSON,
    _Client,
    _count_fixo,
    _extrair,
    _franquia,
    _resposta,
    _seed,
    _usage,
)

import app.run_agente_compara_temp_table as temp_mod

_JS = Path("app/static/js/agente_compara.js")
_HTML = Path("app/templates/agente_compara.html")
_PROMESSAS_INDEVIDAS = ("estorno", "nenhum crédito", "não será cobrad", "nao sera cobrad")


def _instalar_capturando(monkeypatch, *, client, usuario, models):
    capturados = []

    def _apply(payload, source_doc_ids=None, **kwargs):
        capturados.append(payload)
        return {
            "status": (payload or {}).get("status") or "needs_review",
            "reading_alerts": list((payload or {}).get("reading_alerts") or []),
            "error_code": (payload or {}).get("error_code"),
            "temp_table_id": "tt-mensagens",
            "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
        }

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
    monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)
    return capturados


def _publico(code: str, alerts: list[str] | None = None) -> dict:
    classified = _classify_temp_table_failure(
        reading_alerts=alerts or ["alerta interno"],
        error_code=code,
    )
    record = {
        "status": "failed",
        "reading_alerts": alerts or ["alerta interno"],
        "error_code": code,
        "source_documents": [],
        "origins": [],
        "destinations": [],
        "routes": [],
        "freight_tables": [],
        "freight_routes": [],
        "weight_ranges": [],
        "freight_values": [],
        "accessorial_fees": [],
        "extracted_items": [],
        "uncertain_fields": [],
        "evidence_refs": [],
        **classified,
    }
    return _public_temp_table(record)


def test_teto_de_30000_permanece():
    assert EXTRACAO_TETO_TECNICO_TOKENS == 30_000


def test_documento_acima_do_teto_nao_oferece_cta_financeiro(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="teto-msg@test.com")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(AssertionError("nao gerar")),
            _count_fixo(27_000),
        )
        capturados = _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert resultado["error_code"] == ERROR_EXTRACAO_TETO_TECNICO
        assert capturados[-1]["error_code"] == ERROR_EXTRACAO_TETO_TECNICO
        publico = _publico(ERROR_EXTRACAO_TETO_TECNICO, capturados[-1]["reading_alerts"])
        assert "capacidade máxima de processamento" in publico["safe_message"]
        assert publico["upgrade_cta"] is None
        assert publico["error_code"] == ERROR_EXTRACAO_TETO_TECNICO
        assert "crédito" not in publico["safe_message"].lower()
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "blocked"
        assert tentativa.failure_reason == "orcamento_operacao_insuficiente"
        assert tentativa.reserved_credits == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert DOC_SECRET not in publico["safe_message"]


def test_documento_grande_com_saldo_baixo_continua_sendo_teto(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="4", email="teto-e-saldo@test.com")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(AssertionError("nao gerar")),
            _count_fixo(27_000),
        )
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert resultado["error_code"] == ERROR_EXTRACAO_TETO_TECNICO
        assert _publico(resultado["error_code"])["upgrade_cta"] is None


def test_saldo_insuficiente_nao_inicia_geracao_e_oferece_cta(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="15", consumo="13", email="saldo-msg@test.com")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(AssertionError("nao gerar")),
            _count_fixo(100),
        )
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert IaChamadaTentativa.query.count() == 0
        assert resultado["error_code"] == ERROR_EXTRACAO_CREDITOS_INSUFICIENTES
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        cta = publico["upgrade_cta"]
        assert cta["upgrade_url"] == UPGRADE_PATH_DEFAULT == "/contrate-um-plano"
        assert cta["upgrade_label"] == UPGRADE_LABEL_DEFAULT == "Faça o upgrade"
        assert cta["error_code"] == ERROR_EXTRACAO_CREDITOS_INSUFICIENTES
        assert "créditos" in publico["safe_message"]
        assert "capacidade máxima de processamento permitida" not in publico["safe_message"]
        assert _franquia(franquia.id).consumo_acumulado == Decimal("13")
        assert _franquia(franquia.id).reserva_pendente == 0


def test_saldo_baixo_mas_documento_viavel_no_teto_oferece_cta(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="4", email="saldo-viavel@test.com")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(AssertionError("nao gerar")),
            _count_fixo(5_000),
        )
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert resultado["error_code"] == ERROR_EXTRACAO_CREDITOS_INSUFICIENTES
        assert _publico(resultado["error_code"])["upgrade_cta"]["upgrade_url"] == "/contrate-um-plano"


def test_erro_de_servico_do_gemini_nao_e_limite_financeiro(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="provedor-msg@test.com")

        class _Servico(RuntimeError):
            def __init__(self):
                super().__init__("503 UNAVAILABLE")
                self.code = 503
                self.usage_metadata = _usage(prompt=40, resposta=10, pensamento=0)

        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(_Servico()), _count_fixo(100))
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["status"] == "failed"
        assert resultado["error_code"] == ERROR_EXTRACAO_PROVEDOR_INDISPONIVEL
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        assert "temporariamente indisponível" in publico["safe_message"]
        assert publico["upgrade_cta"] is None
        assert publico["failure_code"] != ERROR_EXTRACAO_CREDITOS_INSUFICIENTES
        assert "crédito" not in publico["safe_message"].lower()
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "released"
        assert tentativa.failure_reason == "erro_provedor"
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_json_invalido_nao_vira_saldo_insuficiente(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100", email="json-msg@test.com")
        client = _Client(
            lambda model, contents, config: _resposta("sem json aproveitavel", prompt=20, resposta=10, pensamento=0),
            _count_fixo(100),
        )
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["error_code"] == ERROR_EXTRACAO_JSON_INVALIDO
        assert resultado["reading_alerts"] == [temp_mod.READING_ALERT_PARSER_NO_JSON]
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        assert "formato inválido" in publico["safe_message"]
        assert publico["upgrade_cta"] is None
        assert publico["failure_code"] != ERROR_EXTRACAO_CREDITOS_INSUFICIENTES


def test_max_tokens_nao_e_classificado_como_saldo(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100", email="max-tokens-msg@test.com")
        client = _Client(
            lambda model, contents, config: _resposta(
                "resposta interrompida",
                prompt=30,
                resposta=20,
                pensamento=0,
                finish="MAX_TOKENS",
            ),
            _count_fixo(100),
        )
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["error_code"] == ERROR_EXTRACAO_RESPOSTA_TRUNCADA
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        assert "não é falta de créditos" in publico["safe_message"]
        assert publico["failure_code"] == ERROR_EXTRACAO_RESPOSTA_TRUNCADA
        assert publico["upgrade_cta"] is None
        assert "capacidade máxima de processamento permitida" not in publico["safe_message"]


def test_consumo_incerto_nao_promete_estorno_e_mantem_reserva(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="incerto-msg@test.com")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(RuntimeError("timeout de rede")),
            _count_fixo(1_000),
        )
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        resultado = _extrair()
        assert [chamada["model"] for chamada in client.models.calls] == ["gemini-2.5-flash"]
        assert resultado["error_code"] == ERROR_EXTRACAO_CONSUMO_INCERTO
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        texto = publico["safe_message"].lower()
        assert "não foi concluída" in publico["safe_message"]
        assert publico["credit_disposition"] == "unconfirmed"
        assert publico["retryable"] is False
        assert publico["upgrade_cta"] is None
        for promessa in _PROMESSAS_INDEVIDAS:
            assert promessa not in texto
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "uncertain"
        assert tentativa.reserva_ativa is True
        assert _franquia(franquia.id).reserva_pendente == tentativa.reserved_credits
        assert _franquia(franquia.id).reserva_pendente > 0
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_extracao_bem_sucedida_segue_para_revisao(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="sucesso-msg@test.com")
        client = _Client(
            lambda model, contents, config: _resposta(VALID_JSON, prompt=40, resposta=20, pensamento=0),
            _count_fixo(100),
        )
        capturados = _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert resultado["error_code"] is None
        assert "error_code" not in capturados[-1]
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_timeout_confirmado_preserva_alerta_anterior(app, monkeypatch):
    with app.app_context():
        usuario, _franquia_row = _seed(limite="100", email="timeout-msg@test.com")

        class _Timeout(TimeoutError):
            def __init__(self):
                super().__init__("DEADLINE_EXCEEDED")
                self.usage_metadata = _usage(prompt=20, resposta=5, pensamento=0)

        client = _Client(lambda model, contents, config: (_ for _ in ()).throw(_Timeout()), _count_fixo(100))
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["error_code"] is None
        assert temp_mod.READING_ALERT_PROVIDER_TIMEOUT in resultado["reading_alerts"]
        classified = _classify_temp_table_failure(
            reading_alerts=resultado["reading_alerts"],
            error_code=None,
        )
        assert classified["failure_code"] == "platform_temporary_failure"
        assert classified["retryable"] is True


def test_entrada_de_31000_com_saldo_de_2000_e_teto_sem_cta(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="1", email="teto-31000@test.com")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(AssertionError("nao gerar")),
            _count_fixo(31_000),
        )
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert IaChamadaTentativa.query.count() == 0
        assert resultado["status"] == "failed"
        assert resultado["error_code"] == ERROR_EXTRACAO_TETO_TECNICO
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        assert publico["upgrade_cta"] is None
        assert publico["error_code"] == ERROR_EXTRACAO_TETO_TECNICO
        assert "capacidade máxima de processamento" in publico["safe_message"]
        assert "crédito" not in publico["safe_message"].lower()
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert DOC_SECRET not in publico["safe_message"]


def test_entrada_viavel_com_saldo_de_2000_oferece_cta(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="1", email="saldo-2000@test.com")
        client = _Client(
            lambda model, contents, config: (_ for _ in ()).throw(AssertionError("nao gerar")),
            _count_fixo(1_000),
        )
        _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert client.models.calls == []
        assert len(client.models.count_calls) == 1
        assert IaChamadaTentativa.query.count() == 0
        assert resultado["error_code"] == ERROR_EXTRACAO_CREDITOS_INSUFICIENTES
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        cta = publico["upgrade_cta"]
        assert cta["upgrade_url"] == UPGRADE_PATH_DEFAULT == "/contrate-um-plano"
        assert cta["upgrade_label"] == UPGRADE_LABEL_DEFAULT
        assert "créditos" in publico["safe_message"]
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_json_valido_com_max_tokens_permanece_truncado(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="json-max-tokens@test.com")
        client = _Client(
            lambda model, contents, config: _resposta(
                VALID_JSON,
                prompt=40,
                resposta=20,
                pensamento=0,
                finish="MAX_TOKENS",
            ),
            _count_fixo(100),
        )
        capturados = _instalar_capturando(
            monkeypatch,
            client=client,
            usuario=usuario,
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["status"] == "failed"
        assert resultado["error_code"] == ERROR_EXTRACAO_RESPOSTA_TRUNCADA
        assert capturados[-1]["status"] == "failed"
        assert capturados[-1].get("freight_tables") is None
        publico = _publico(resultado["error_code"], resultado["reading_alerts"])
        assert "não é falta de créditos" in publico["safe_message"]
        assert publico["upgrade_cta"] is None
        assert publico["failure_code"] == ERROR_EXTRACAO_RESPOSTA_TRUNCADA
        assert publico["failure_code"] != ERROR_EXTRACAO_CREDITOS_INSUFICIENTES
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "released"
        assert tentativa.failure_reason == "max_tokens"
        assert tentativa.reserva_ativa is False
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).consumo_acumulado == 0


def test_consumo_incerto_permanece_no_payload_publico_da_revisao(app, monkeypatch, tmp_path):
    from flask import session

    from app.agente_compara_doc_service import (
        apply_temp_table_extraction_from_model_payload,
        load_temp_table_record,
    )
    from tests.cleiton_doc_fixtures import patch_cleiton_doc_store

    patch_cleiton_doc_store(tmp_path, monkeypatch)
    app.config["SECRET_KEY"] = "test-secret"
    payload = {
        "status": "needs_review",
        "freight_tables": [
            {
                "columns": ["Destino", "Ate 100kg"],
                "rows": [{"Destino": "Curitiba", "Ate 100kg": "80"}],
            }
        ],
        "freight_routes": [],
        "accessorial_fees": [],
        "reading_alerts": [],
        "evidence_refs": [],
        "error_code": ERROR_EXTRACAO_CONSUMO_INCERTO,
    }
    with app.test_request_context("/agente-compara"):
        session["agente_compara_doc_ids"] = ["doc-incerto"]
        saved = apply_temp_table_extraction_from_model_payload(
            payload,
            source_doc_ids=["doc-incerto"],
        )
        assert saved["status"] == "needs_review"
        assert saved["error_code"] == ERROR_EXTRACAO_CONSUMO_INCERTO
        assert "pendente" in saved["safe_message"]
        assert saved.get("upgrade_cta") is None
        assert saved["credit_disposition"] == "unconfirmed"
        assert saved["retryable"] is False
        publico = _public_temp_table(saved)
        assert publico["status"] == "needs_review"
        assert publico["error_code"] == ERROR_EXTRACAO_CONSUMO_INCERTO
        assert publico["safe_message"] == saved["safe_message"]
        assert publico["upgrade_cta"] is None
        assert publico["credit_disposition"] == "unconfirmed"
        texto = publico["safe_message"].lower()
        for promessa in _PROMESSAS_INDEVIDAS:
            assert promessa not in texto
        reloaded = load_temp_table_record(saved["temp_table_id"], ttl_hours=24)
        assert reloaded["status"] == "needs_review"
        assert reloaded["error_code"] == ERROR_EXTRACAO_CONSUMO_INCERTO
        assert reloaded["safe_message"] == saved["safe_message"]


def test_frontend_reusa_cta_e_mantem_codigos_no_desktop_e_no_mobile():
    js = _JS.read_text(encoding="utf-8")
    html = _HTML.read_text(encoding="utf-8")
    failed = js[js.index("function renderFailedFlowView") : js.index("function renderReviewFlowView")]
    for code in (
        ERROR_EXTRACAO_TETO_TECNICO,
        ERROR_EXTRACAO_CREDITOS_INSUFICIENTES,
        ERROR_EXTRACAO_PROVEDOR_INDISPONIVEL,
        ERROR_EXTRACAO_JSON_INVALIDO,
        ERROR_EXTRACAO_RESPOSTA_TRUNCADA,
        ERROR_EXTRACAO_CONSUMO_INCERTO,
    ):
        assert code in js
    assert "fillLimitMessageElement(text, source)" in failed
    assert "data-error-code" in failed
    assert "safe_message" in failed
    review = js[js.index("function renderTempTableModalContent") : js.index("function isTempTableModalOpen")]
    assert "appendConsumoIncertoNotice(body, tempTable)" in review
    assert "extracao_consumo_incerto" in js
    assert "agente-compara-extracao-consumo-incerto" in html
    assert "PLAN_LIMIT_UPGRADE_PATH = '/contrate-um-plano'" in js
    assert "agente-compara-flow-failed-text a" in html
    mobile = html[html.index("@media (max-width: 576px)") :]
    assert ".agente-compara-flow-failed-panel" in mobile
    assert ".agente-compara-extracao-consumo-incerto" in mobile
    assert ".agente-compara-flow-retry-btn" in mobile
