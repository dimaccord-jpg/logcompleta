"""SCRUM-146 lote 2: débito do AgenteCompara só depois da tabela útil persistida.

Provedor simulado. Não chama o Gemini pago.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.exc import SQLAlchemyError

from app.models import IaChamadaTentativa, IaConsumoAbatimento, IaConsumoEvento, utcnow_naive
from app.agente_compara_doc_service import (
    ERROR_EXTRACAO_CONSUMO_INCERTO,
    ERROR_EXTRACAO_RESPOSTA_TRUNCADA,
    TEMP_TABLE_CREDIT_UNCONFIRMED,
)
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
        assert IaChamadaTentativa.query.count() == 0
        assert _abatimentos() == 1
        evento = IaConsumoEvento.query.one()
        assert evento.total_tokens == 160
        assert evento.franquia_id == franquia.id
        assert evento.usuario_id == usuario.id
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        assert _franquia(franquia.id).fim_ciclo == fim


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
        assert IaChamadaTentativa.query.count() == 0
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
        assert IaChamadaTentativa.query.count() == 0
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
        assert IaChamadaTentativa.query.count() == 0
        eventos = IaConsumoEvento.query.order_by(IaConsumoEvento.id.asc()).all()
        assert len(eventos) == 2
        assert eventos[0].status == "failure"
        assert eventos[0].total_tokens == 1000
        assert eventos[1].status == "success"
        assert eventos[1].total_tokens == 160
        assert IaConsumoAbatimento.query.one().ia_consumo_evento_id == eventos[1].id


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
        assert IaChamadaTentativa.query.count() == 0


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
        assert IaChamadaTentativa.query.count() == 0
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
        assert IaChamadaTentativa.query.count() == 0
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
        assert IaChamadaTentativa.query.count() == 0


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
        assert IaChamadaTentativa.query.count() == 0
        assert _abatimentos() == 0
        atual = _franquia(franquia.id)
        assert atual.consumo_acumulado == 0
        assert atual.reserva_pendente == 0
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
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 5


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
        assert IaChamadaTentativa.query.count() == 0
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
        assert IaChamadaTentativa.query.count() == 0
        assert IaConsumoEvento.query.count() == 2


def test_falha_de_debito_preserva_tabela_sem_reserva(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="checkpoint-aviso@test.com")
        client = _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )

        def _debito_falha(*_args, **_kwargs):
            raise temp_mod.ExtracaoConsumoIncerto("debito_nao_confirmado")

        monkeypatch.setattr(temp_mod, "abater_entrega_confirmada", _debito_falha)
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
        assert IaChamadaTentativa.query.count() == 0
        atual = _franquia(franquia.id)
        assert atual.reserva_pendente == 0
        assert atual.consumo_acumulado == 0
        assert _abatimentos() == 0


def test_replay_depois_do_debito_nao_cobra_de_novo(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="replay@test.com")
        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        primeiro = _extrair()
        assert primeiro["status"] == "needs_review"
        assert IaChamadaTentativa.query.count() == 0
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0

        _extrair()
        db.session.expire_all()
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 0


def test_timeout_sem_metricas_nao_deixa_reserva_e_a_entrega_util_cobra_uma_vez(app, monkeypatch):
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
        assert IaChamadaTentativa.query.count() == 0
        assert _abatimentos() == 0
        assert _franquia(franquia.id).reserva_pendente == 0

        _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash"],
        )
        util = _extrair()
        assert entrega_faturavel(util) is True
        db.session.expire_all()
        assert IaChamadaTentativa.query.count() == 0
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0


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
        assert IaChamadaTentativa.query.count() == 0


_SEGREDO_COMMIT_ABATIMENTO = "SEGREDO_SQL_NAO_LOGAR_commit_abatimento"


def _commit_confirma_o_abatimento() -> bool:
    """O commit final grava o marcador no evento depois do débito já enviado à transação."""
    import inspect

    if not any(frame.function == "abater_entrega_confirmada" for frame in inspect.stack()):
        return False
    return any(isinstance(obj, IaConsumoEvento) for obj in db.session.dirty)


def test_commit_financeiro_falho_preserva_tabela_sem_segunda_chamada(app, monkeypatch, caplog):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="commit-abatimento@test.com")
        client = _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
        )
        falhar = {"ativo": True}
        real_commit = db.session.commit

        def commit_do_abatimento():
            if falhar["ativo"] and _commit_confirma_o_abatimento():
                raise SQLAlchemyError(_SEGREDO_COMMIT_ABATIMENTO)
            return real_commit()

        monkeypatch.setattr(db.session, "commit", commit_do_abatimento)
        caplog.set_level(logging.INFO)
        resultado = _extrair()

        assert resultado["status"] == "needs_review"
        assert resultado["freight_tables"] == [TABELA_UTIL]
        assert resultado["temp_table_id"] == "tt-entrega"
        assert resultado["error_code"] == ERROR_EXTRACAO_CONSUMO_INCERTO
        assert resultado["failure_code"] == ERROR_EXTRACAO_CONSUMO_INCERTO
        assert resultado["credit_disposition"] == TEMP_TABLE_CREDIT_UNCONFIRMED
        assert resultado["retryable"] is False
        assert "pendente" in resultado["safe_message"]
        assert entrega_faturavel(resultado) is True
        assert [chamada["model"] for chamada in client.models.calls] == ["gemini-2.5-flash"]
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 0
        avisos = [
            rec.getMessage()
            for rec in caplog.records
            if rec.name == "app.services.agente_compara_extraction_billing"
        ]
        assert any(
            "pendencia financeira no abatimento" in mensagem and "SQLAlchemyError" in mensagem
            for mensagem in avisos
        )
        assert _SEGREDO_COMMIT_ABATIMENTO not in caplog.text

        falhar["ativo"] = False
        antes = len(client.models.calls)
        segundo = _extrair()
        assert len(client.models.calls) == antes + 1
        assert segundo["status"] == "needs_review"
        assert entrega_faturavel(segundo) is True
        assert segundo.get("error_code") != ERROR_EXTRACAO_CONSUMO_INCERTO
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")

        _extrair()
        db.session.expire_all()
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _franquia(franquia.id).reserva_pendente == 0
        primeiro = IaConsumoEvento.query.order_by(IaConsumoEvento.id.asc()).first()
        digest = temp_mod.digest_da_entrega(
            ["doc-1"],
            comparison_id="cmp-entrega",
            table_id="tbl-entrega",
        )
        assert temp_mod.abater_entrega_confirmada(primeiro.id, digest) == "ja_faturada"
        db.session.expire_all()
        assert _abatimentos() == 1
        assert _franquia(franquia.id).consumo_acumulado == Decimal("0.080000")
        assert _SEGREDO_COMMIT_ABATIMENTO not in caplog.text


def test_persistencia_da_tabela_nao_vira_pendencia_financeira(app, monkeypatch):
    with app.app_context():
        usuario, franquia = _seed(limite="100", email="persistencia-tabela@test.com")

        def _apply(payload, source_doc_ids=None, **kwargs):
            payload = payload or {}
            if payload.get("status") == "failed":
                return _apply_fiel(payload, source_doc_ids=source_doc_ids, **kwargs)
            raise SQLAlchemyError("gravacao da tabela")

        client = _preparar(
            monkeypatch,
            usuario=usuario,
            gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10),
            models=["gemini-2.5-flash", "gemini-2.5-flash-lite"],
            totais=(1_000, 1_000),
        )
        monkeypatch.setattr(temp_mod, "apply_temp_table_extraction_from_model_payload", _apply)
        resultado = _extrair()
        assert resultado["status"] == "failed"
        assert resultado["freight_tables"] == []
        assert resultado.get("error_code") != ERROR_EXTRACAO_CONSUMO_INCERTO
        assert resultado.get("credit_disposition") != TEMP_TABLE_CREDIT_UNCONFIRMED
        assert entrega_faturavel(resultado) is False
        assert [chamada["model"] for chamada in client.models.calls] == [
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ]
        assert _abatimentos() == 0
        assert _franquia(franquia.id).consumo_acumulado == 0
        assert _franquia(franquia.id).reserva_pendente == 0
        assert IaChamadaTentativa.query.count() == 0
