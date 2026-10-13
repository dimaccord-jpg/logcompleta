"""SCRUM-146 lote 2: débito do AgenteCompara só depois da tabela útil persistida.

Provedor simulado. Não chama o Gemini pago.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

from app.models import IaChamadaTentativa, IaConsumoAbatimento, IaConsumoEvento, utcnow_naive
from app.agente_compara_doc_service import ERROR_EXTRACAO_CONSUMO_INCERTO, ERROR_EXTRACAO_RESPOSTA_TRUNCADA
from app.services.agente_compara_extraction_billing import entrega_faturavel
from app.extensions import db
from app.services.cleiton_billable_ai_call import cleiton_governed_billable_ai_call
from tests.test_scrum146_extracao_orcamento import (
    _Client,
    _count_fixo,
    _franquia,
    _instalar,
    _resposta,
    _seed,
    _usage,
)

import app.run_agente_compara_temp_table as temp_mod

TABELA_UTIL = {
    "columns": ["Destino", "Ate 100kg"],
    "rows": [{"Destino": "Curitiba", "Ate 100kg": "80"}],
}
JSON_UTIL = json.dumps(
    {
        "status": "needs_review",
        "freight_tables": [TABELA_UTIL],
        "freight_routes": [],
        "accessorial_fees": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
)
JSON_INUTIL = json.dumps(
    {
        "status": "needs_review",
        "freight_tables": [{"table_title": "Sem faixa", "columns": ["Destino"]}],
        "freight_routes": [],
        "accessorial_fees": [{"name": "GRIS"}],
        "reading_alerts": [],
        "evidence_refs": [],
    }
)


def _apply_fiel(payload, source_doc_ids=None, **kwargs):
    payload = payload or {}
    return {
        "status": payload.get("status") or "needs_review",
        "freight_tables": list(payload.get("freight_tables") or []),
        "freight_routes": list(payload.get("freight_routes") or []),
        "freight_values": list(payload.get("freight_values") or []),
        "weight_ranges": list(payload.get("weight_ranges") or []),
        "accessorial_fees": list(payload.get("accessorial_fees") or []),
        "reading_alerts": list(payload.get("reading_alerts") or []),
        "error_code": payload.get("error_code"),
        "temp_table_id": "tt-entrega",
        "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
    }


def _extrair():
    return temp_mod.run_agente_compara_temp_table_extraction(
        ["doc-1"],
        session_obj={},
        comparison_id="cmp-entrega",
        table_id="tbl-entrega",
    )


def _preparar(monkeypatch, *, usuario, gerar, models, totais=(1_000,)):
    client = _Client(gerar, _count_fixo(*totais))
    _instalar(monkeypatch, client=client, usuario=usuario, models=list(models))
    monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply_fiel)
    return client


def _abatimentos():
    return IaConsumoAbatimento.query.count()


def test_tabela_util_debita_uma_vez(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="util@test.com")
        fim = franquia.fim_ciclo
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert entrega_faturavel(resultado) is True
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "settled"
        assert tentativa.actual_credits == Decimal("0.080000")
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).fim_ciclo == fim
        assert IaConsumoEvento.query.one().total_tokens == 160


def test_extracao_sem_entrega_util_debita_zero(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="sem-entrega@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta("sem json", prompt=40, resposta=20, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        resultado = _extrair()
        assert resultado["status"] == "failed"
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "released"
        assert tentativa.failure_reason == "json_invalido"
        assert IaConsumoEvento.query.one().total_tokens == 70
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0


def test_needs_review_util_cobra_e_inutilizavel_nao_cobra(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="revisao@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_INUTIL, prompt=30, resposta=20, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        inutil = _extrair()
        assert inutil["status"] == "needs_review"
        assert entrega_faturavel(inutil) is False
        assert IaChamadaTentativa.query.one().failure_reason == "tabela_inutilizavel"
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        util = _extrair()
        assert entrega_faturavel(util) is True
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0


def test_primario_falha_e_so_o_fallback_e_debitado(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="fallback@test.com")

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
            usuario=usuario,
            gerar=gerar,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
            totais=(1_000, 1_000),
        )
        resultado = _extrair()
        assert entrega_faturavel(resultado) is True
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        liquidada = IaChamadaTentativa.query.filter_by(status="settled").one()
        evento = db.session.get(IaConsumoEvento, liquidada.ia_consumo_evento_id)
        assert evento.total_tokens == 160
        assert IaConsumoEvento.query.count() == 2
        falha = IaChamadaTentativa.query.filter_by(status="released").one()
        assert falha.failure_reason == "erro_provedor"
        assert falha.actual_credits is None


def test_ambos_falham_debita_zero(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="ambos@test.com")

        class _Falha(RuntimeError):
            def __init__(self):
                super().__init__("provider down")
                self.usage_metadata = _usage(prompt=10, resposta=10, pensamento=0)

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: (_ for _ in ()).throw(_Falha()),
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
            totais=(1_000, 1_000),
        )
        resultado = _extrair()
        assert resultado["status"] == "failed"
        assert IaConsumoEvento.query.count() == 2
        assert sum(evento.total_tokens or 0 for evento in IaConsumoEvento.query.all()) == 40
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert {row.failure_reason for row in IaChamadaTentativa.query.all()} == {"erro_provedor"}


def test_max_tokens_nao_cobra_e_o_fallback_util_cobra_so_a_si(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="max-tokens@test.com")

        def gerar(model, contents, config):
            if model == "gemini-2.5-flash":
                return _resposta(
                    JSON_UTIL,
                    prompt=400,
                    resposta=1_200,
                    pensamento=400,
                    finish="MAX_TOKENS",
                )
            return _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10)

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=gerar,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
            totais=(1_000, 1_000),
        )
        resultado = _extrair()
        assert entrega_faturavel(resultado) is True
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert IaChamadaTentativa.query.filter_by(failure_reason="max_tokens").count() == 1
        assert IaConsumoEvento.query.count() == 2


def test_falha_ao_persistir_debita_zero(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="persistencia@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=80, resposta=40, pensamento=10),
            models=["gemini-2.5-flash"],
        )

        def _apply(payload, source_doc_ids=None, **kwargs):
            if isinstance(payload, dict) and payload.get("freight_tables"):
                raise RuntimeError("falha ao gravar tabela")
            return {
                "status": "failed",
                "reading_alerts": list((payload or {}).get("reading_alerts") or []),
                "temp_table_id": "tt-falha",
                "version_marker": temp_mod.TEMP_TABLE_VERSION_MARKER,
            }

        monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)
        resultado = _extrair()
        assert resultado["status"] == "failed"
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "released"
        assert tentativa.failure_reason == "persistencia_falhou"
        assert IaConsumoEvento.query.one().total_tokens == 130
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0


def test_reexecucao_da_mesma_entrega_nao_duplica_debito(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="reexec@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        primeiro = _extrair()
        segundo = _extrair()
        assert entrega_faturavel(primeiro) is True
        assert entrega_faturavel(segundo) is True
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaConsumoEvento.query.count() == 2
        assert IaChamadaTentativa.query.filter_by(status="settled").count() == 1
        assert IaChamadaTentativa.query.filter_by(failure_reason="entrega_ja_faturada").count() == 1


def test_consumo_incerto_nao_cobra_e_mantem_reserva(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="incerto@test.com")

        def gerar(model, contents, config):
            raise RuntimeError("timeout de rede")

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=gerar,
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
            totais=(1_000, 1_000),
        )
        resultado = _extrair()
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "uncertain"
        assert tentativa.reserva_ativa is True
        assert _abatimentos() == 0
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == tentativa.reserved_credits
        assert atual.reserva_pendente > 0
        assert resultado["status"] == "failed"


def test_outro_agente_continua_debitando_na_chamada(app):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="roberto@test.com")
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
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "settled"
        assert tentativa.actual_credits == Decimal("0.050000")
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.050000")
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).fim_ciclo == fim


def _payload(corpo: dict) -> str:
    base = {
        "status": "needs_review",
        "freight_tables": [],
        "freight_routes": [],
        "freight_values": [],
        "weight_ranges": [],
        "accessorial_fees": [],
        "reading_alerts": [],
        "evidence_refs": [],
    }
    base.update(corpo)
    return json.dumps(base)


PAYLOADS_SEM_TARIFA = (
    _payload(
        {
            "freight_tables": [
                {"columns": ["Destino"], "rows": [{"Destino": "Curitiba"}]},
            ],
        }
    ),
    _payload(
        {
            "freight_routes": [
                {"origin": "São Paulo", "destination": "Curitiba"},
            ],
        }
    ),
    _payload(
        {
            "freight_tables": [
                {
                    "table_title": "Frete",
                    "columns": ["Ate 100kg"],
                    "rows": [{"Ate 100kg": "80"}],
                },
            ],
            "freight_values": [{"label": "Frete", "value": 80}],
        }
    ),
    _payload(
        {
            "weight_ranges": [
                {"label": "Até 100 kg", "min_weight": 0, "max_weight": 100},
            ],
        }
    ),
    _payload(
        {
            "freight_routes": [{"pedagio": "12,50"}],
            "accessorial_fees": [{"name": "Pedágio", "value": "12,50"}],
        }
    ),
)


def test_cinco_payloads_sem_estrutura_tarifaria_nao_debitam(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="sem-tarifa@test.com")
        for indice, texto in enumerate(PAYLOADS_SEM_TARIFA):
            _preparar(
                monkeypatch,
                usuario=usuario,
                gerar=lambda model, contents, config, texto=texto: _resposta(
                    texto, prompt=30, resposta=20, pensamento=10
                ),
                models=["gemini-2.5-flash"],
            )
            resultado = temp_mod.run_agente_compara_temp_table_extraction(
                ["doc-1"],
                session_obj={},
                comparison_id=f"cmp-sem-{indice}",
                table_id=f"tbl-sem-{indice}",
            )
            assert resultado["status"] == "needs_review"
            assert entrega_faturavel(resultado) is False
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 5
        assert {row.failure_reason for row in IaChamadaTentativa.query.all()} == {"tabela_inutilizavel"}


def test_ciclo_mensal_nao_muda_com_a_cobranca_da_extracao(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="ciclo@test.com")
        inicio = utcnow_naive() - timedelta(days=3)
        fim = utcnow_naive() + timedelta(days=20)
        franquia.inicio_ciclo = inicio
        franquia.fim_ciclo = fim
        db.session.commit()
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        _extrair()
        atual = _franquia(franquia.id)
        assert atual.inicio_ciclo == inicio
        assert atual.fim_ciclo == fim


def test_execucoes_intercaladas_da_mesma_entrega_debitam_uma_vez(tmp_path, monkeypatch):
    import threading

    from flask import Flask
    from sqlalchemy.pool import NullPool

    from app.models import User

    race = Flask("scrum146-entrega-intercalada")
    uri = "sqlite:///" + str(tmp_path / "entrega.db").replace("\\", "/")
    race.config["SQLALCHEMY_DATABASE_URI"] = uri
    race.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    race.config["TESTING"] = True
    race.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "connect_args": {"check_same_thread": False, "timeout": 30},
        "poolclass": NullPool,
    }
    db.init_app(race)
    with race.app_context():
        import app.models  # noqa: F401

        db.create_all()
        usuario, franquia = _seed(limite="100", email="corrida@test.com")
        usuario_id = usuario.id
        franquia_id = franquia.id
        barreira = threading.Barrier(2)

        def gerar(model, contents, config):
            barreira.wait(timeout=10)
            return _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10)

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=gerar,
            models=["gemini-2.5-flash"],
        )
        monkeypatch.setattr(
            temp_mod,
            "usuario_operacional_da_chamada",
            lambda _usuario=None: db.session.get(User, usuario_id),
        )

    erros = []

    def worker():
        with race.app_context():
            try:
                _extrair()
            except Exception as exc:
                erros.append(exc)
            finally:
                db.session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert erros == []
    with race.app_context():
        assert _abatimentos() == 1
        assert _franquia(franquia_id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia_id).reserva_pendente == 0
        assert IaChamadaTentativa.query.filter_by(status="settled").count() == 1
        assert IaChamadaTentativa.query.filter_by(failure_reason="entrega_ja_faturada").count() == 1
        assert IaConsumoEvento.query.count() == 2


def test_json_util_truncado_nao_cobra_nem_entra_em_revisao(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="json-truncado@test.com")
        client = _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(
                JSON_UTIL,
                prompt=100,
                resposta=50,
                pensamento=10,
                finish="MAX_TOKENS",
            ),
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
            totais=(1_000, 1_000),
        )
        resultado = _extrair()
        assert resultado["status"] == "failed"
        assert resultado.get("error_code") == ERROR_EXTRACAO_RESPOSTA_TRUNCADA
        assert entrega_faturavel(resultado) is False
        assert [chamada["model"] for chamada in client.models.calls] == [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ]
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaChamadaTentativa.query.filter_by(failure_reason="max_tokens").count() == 2


def test_checkpoint_apos_tabela_util_avisa_consumo_incerto(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="checkpoint-aviso@test.com")
        client = _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )

        def _quebra_depois_do_debito():
            db.session.commit()
            raise RuntimeError("falha no checkpoint da liquidacao")

        monkeypatch.setattr(
            "app.services.cleiton_billable_ai_call._checkpoint_pos_debito",
            _quebra_depois_do_debito,
        )
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert resultado["freight_tables"] == [TABELA_UTIL]
        assert resultado["error_code"] == ERROR_EXTRACAO_CONSUMO_INCERTO
        assert "pendente" in resultado["safe_message"]
        assert resultado.get("upgrade_cta") is None
        texto = resultado["safe_message"].lower()
        for promessa in ("estorno", "cobrança", "cobranca", "liberad", "nenhum crédito", "não será cobrad"):
            assert promessa not in texto
        assert entrega_faturavel(resultado) is True
        assert [chamada["model"] for chamada in client.models.calls] == ["gemini-2.5-flash"]
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "uncertain"
        assert tentativa.reserva_ativa is True
        assert tentativa.status != "released"
        atual = _franquia(franquia.id)
        assert atual.reserva_pendente == tentativa.reserved_credits
        assert atual.reserva_pendente > 0
        assert _abatimentos() == 1


def test_excecao_apos_debito_e_replay_encerra_a_reserva_original(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="replay@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )

        def _quebra_depois_do_debito():
            db.session.commit()
            raise RuntimeError("falha apos debito")

        monkeypatch.setattr(
            "app.services.cleiton_billable_ai_call._checkpoint_pos_debito",
            _quebra_depois_do_debito,
        )
        primeiro = _extrair()
        assert primeiro["status"] == "needs_review"
        original = IaChamadaTentativa.query.one()
        original_id = original.id
        assert original.status == "uncertain"
        assert original.reserva_ativa is True
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == original.reserved_credits

        monkeypatch.setattr(
            "app.services.cleiton_billable_ai_call._checkpoint_pos_debito",
            lambda: None,
        )
        _extrair()
        db.session.expire_all()
        original = db.session.get(IaChamadaTentativa, original_id)
        assert original.status == "settled"
        assert original.reserva_ativa is False
        assert original.failure_reason is None
        assert original.settled_at is not None
        nova = IaChamadaTentativa.query.filter(IaChamadaTentativa.id != original_id).one()
        assert nova.status == "released"
        assert nova.failure_reason == "entrega_ja_faturada"
        assert nova.reserva_ativa is False
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0


def test_replay_preserva_reserva_incerta_sem_comprovacao(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="incerta-replay@test.com")

        def gerar_timeout(model, contents, config):
            raise RuntimeError("timeout de rede")

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=gerar_timeout,
            models=["gemini-2.5-flash"],
        )
        falha = _extrair()
        assert falha["status"] == "failed"
        incerta = IaChamadaTentativa.query.one()
        incerta_id = incerta.id
        reservado = incerta.reserved_credits
        assert incerta.status == "uncertain"
        assert incerta.reserva_ativa is True
        assert _abatimentos() == 0

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        util = _extrair()
        assert entrega_faturavel(util) is True
        db.session.expire_all()
        incerta = db.session.get(IaChamadaTentativa, incerta_id)
        assert incerta.status == "uncertain"
        assert incerta.reserva_ativa is True
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == reservado


def test_excecao_no_cache_apos_entrega_preserva_a_tabela(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="cache@test.com")
        aplicacoes = []

        def _apply(payload, source_doc_ids=None, **kwargs):
            aplicacoes.append(dict(payload or {}))
            return _apply_fiel(payload, source_doc_ids=source_doc_ids, **kwargs)

        def _cache_indisponivel(*_args, **_kwargs):
            raise RuntimeError("cache indisponivel")

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
            totais=(1_000, 1_000),
        )
        monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)
        monkeypatch.setattr(temp_mod, "_cache_extraction_result", _cache_indisponivel)
        resultado = _extrair()
        assert resultado["status"] == "needs_review"
        assert resultado["temp_table_id"] == "tt-entrega"
        assert resultado["freight_tables"] == [TABELA_UTIL]
        assert entrega_faturavel(resultado) is True
        assert len(aplicacoes) == 1
        assert aplicacoes[0].get("status") != "failed"
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        tentativa = IaChamadaTentativa.query.one()
        assert tentativa.status == "settled"
