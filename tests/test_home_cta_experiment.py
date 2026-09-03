"""Experimento A/B/C do CTA da Home (home_chat_cta_v1)."""
from __future__ import annotations

import importlib
import os
import pathlib
import re
from types import SimpleNamespace
from unittest.mock import MagicMock

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.extensions import db
from app.models import HomeCtaExperimentEvent
from app.services.home_cta_experiment_service import (
    EVENT_CONVERSION,
    EVENT_IMPRESSION,
    HOME_CTA_EXPERIMENT,
    HOME_CTA_VARIANTS,
    ORIGIN_SUGGESTION,
    ORIGIN_TYPED,
    SESSION_KEY,
    build_authenticated_assignment,
    record_home_cta_event,
)

ROOT = pathlib.Path(__file__).resolve().parents[1]
MIG_PATH = ROOT / "migrations" / "versions" / "z0a1b2c3d4e5_home_cta_experiment_event.py"


def _load_web_module():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


def _home_client(monkeypatch, *, authenticated=False, user_id=None):
    web = _load_web_module()
    user = SimpleNamespace(is_authenticated=authenticated, id=user_id)
    monkeypatch.setattr(web, "current_user", user)
    monkeypatch.setattr(web, "get_julia_chat_max_history", lambda: 10)
    monkeypatch.setattr(
        web,
        "avaliar_autorizacao_operacao_por_franquia",
        lambda _u: {"permitido": True},
    )
    return web, web.app.test_client(), user


def _typewriter_text(html: str) -> str | None:
    match = re.search(r'data-typewriter-text="([^"]+)"', html)
    return match.group(1) if match else None


def _persist_impression_on_sqlite(app):
    from app.services import home_cta_experiment_service as svc

    def _persist(assignment):
        if not assignment:
            return
        with app.app_context():
            svc.record_home_cta_event(
                experiment=assignment["experiment"],
                assignment_id=assignment["assignment_id"],
                variant=assignment["variant"],
                event_type=EVENT_IMPRESSION,
            )
            db.session.commit()

    return _persist


def _persist_conversion_on_sqlite(app):
    from app.services import home_cta_experiment_service as svc
    from flask import session as request_session

    def _persist(*, cta_id=None, store=None):
        assignment = svc.load_home_cta_assignment_from_session(store or request_session)
        if not assignment:
            return
        origin = ORIGIN_SUGGESTION if (cta_id or "").strip() else ORIGIN_TYPED
        with app.app_context():
            svc.record_home_cta_event(
                experiment=assignment["experiment"],
                assignment_id=assignment["assignment_id"],
                variant=assignment["variant"],
                event_type=EVENT_CONVERSION,
                interaction_origin=origin,
            )
            db.session.commit()

    return _persist


def _mock_julia_chat(monkeypatch):
    monkeypatch.setattr(
        "app.run_julia_chat.chat_julia_reply",
        lambda *a, **k: {"reply": "ok-julia", "suggestions": []},
    )


def _mock_discovery(monkeypatch):
    monkeypatch.setattr(
        "app.run_cleiton_discovery.cleiton_discovery_reply",
        lambda *a, **k: {
            "reply": "ok",
            "discovery": {"next_action": "converse", "confidence": "low"},
            "handoff": None,
        },
    )


class TestHomeCtaAssignment:
    def test_somente_variantes_catalogadas(self, monkeypatch):
        web, client, _user = _home_client(monkeypatch)
        seen = set()
        for _ in range(12):
            other = web.app.test_client()
            other.get("/")
            with other.session_transaction() as sess:
                variant = sess[SESSION_KEY]["variant"]
            assert variant in HOME_CTA_VARIANTS
            seen.add(variant)
        assert seen <= set(HOME_CTA_VARIANTS)

    def test_mesma_sessao_mantem_variante(self, monkeypatch):
        _web, client, _user = _home_client(monkeypatch)
        client.get("/")
        with client.session_transaction() as sess:
            first = dict(sess[SESSION_KEY])
        client.get("/")
        with client.session_transaction() as sess:
            second = dict(sess[SESSION_KEY])
        assert first == second
        assert first["variant"] in HOME_CTA_VARIANTS

    def test_refresh_mantem_variante(self, monkeypatch):
        _web, client, _user = _home_client(monkeypatch)
        html1 = client.get("/").get_data(as_text=True)
        html2 = client.get("/").get_data(as_text=True)
        with client.session_transaction() as sess:
            assignment = sess[SESSION_KEY]
        assert _typewriter_text(html1) == HOME_CTA_VARIANTS[assignment["variant"]]
        assert _typewriter_text(html2) == HOME_CTA_VARIANTS[assignment["variant"]]

    def test_reset_onboarding_mantem_assignment(self, monkeypatch):
        _web, client, _user = _home_client(monkeypatch)
        client.get("/")
        with client.session_transaction() as sess:
            before = dict(sess[SESSION_KEY])
        resp = client.post("/api/onboarding_discovery/reset")
        assert resp.status_code == 200
        with client.session_transaction() as sess:
            after = dict(sess[SESSION_KEY])
            assert "onboarding_discovery_count" not in sess
        assert after == before

    def test_login_preserva_assignment_existente(self, monkeypatch):
        _web, client, user = _home_client(monkeypatch, authenticated=False)
        client.get("/")
        with client.session_transaction() as sess:
            before = dict(sess[SESSION_KEY])
        user.is_authenticated = True
        user.id = 424242
        client.get("/")
        with client.session_transaction() as sess:
            after = dict(sess[SESSION_KEY])
        assert after == before

    def test_autenticado_sem_assignment_recebe_variante_estavel(self, monkeypatch):
        web, _client, user = _home_client(monkeypatch, authenticated=True, user_id=777001)
        a = web.app.test_client()
        b = web.app.test_client()
        a.get("/")
        b.get("/")
        with a.session_transaction() as sess:
            first = dict(sess[SESSION_KEY])
        with b.session_transaction() as sess:
            second = dict(sess[SESSION_KEY])
        expected = build_authenticated_assignment("777001")
        assert first["variant"] == second["variant"] == expected["variant"]
        assert first["assignment_id"] == second["assignment_id"] == expected["assignment_id"]

    def test_assignment_autenticado_nao_contem_user_id_em_claro(self):
        user_id = "987654321"
        assignment = build_authenticated_assignment(user_id)
        assert user_id not in assignment["assignment_id"]
        assert assignment["assignment_id"] != user_id
        assert ":" not in assignment["assignment_id"]
        assert re.fullmatch(r"[0-9a-f]{64}", assignment["assignment_id"])
        assert assignment["variant"] in HOME_CTA_VARIANTS


class TestHomeCtaUi:
    def test_home_discovery_renderiza_texto_atribuido(self, monkeypatch):
        _web, client, _user = _home_client(monkeypatch, authenticated=False)
        html = client.get("/").get_data(as_text=True)
        with client.session_transaction() as sess:
            variant = sess[SESSION_KEY]["variant"]
        text = HOME_CTA_VARIANTS[variant]
        assert f'data-typewriter-text="{text}"' in html
        assert "HOME_CTA_EXPERIMENT" in html
        assert text in html
        assert 'id="copilotWelcomeMessage"' in html

    def test_home_operacional_renderiza_mesmo_texto_atribuido(self, monkeypatch):
        _web, client, _user = _home_client(monkeypatch, authenticated=True, user_id=55)
        html = client.get("/").get_data(as_text=True)
        with client.session_transaction() as sess:
            variant = sess[SESSION_KEY]["variant"]
        text = HOME_CTA_VARIANTS[variant]
        assert f'data-typewriter-text="{text}"' in html
        assert 'id="juliaWelcomeMessage"' in html
        assert "HOME_CTA_EXPERIMENT" in html

    def test_typewriter_recebe_texto_correto(self, monkeypatch):
        _web, client, _user = _home_client(monkeypatch)
        html = client.get("/").get_data(as_text=True)
        with client.session_transaction() as sess:
            text = HOME_CTA_VARIANTS[sess[SESSION_KEY]["variant"]]
        assert 'data-typewriter-enabled="true"' in html
        assert f'data-typewriter-text="{text}"' in html
        assert f"window.HOME_CTA_EXPERIMENT" in html
        assert text in html

    def test_texto_antigo_nao_reaparece_via_js(self):
        source = pathlib.Path("app/static/js/chat_behavior.js").read_text(encoding="utf-8")
        assert "function resolveWelcomeTypewriterText" in source
        assert "window.HOME_CTA_EXPERIMENT" in source
        fn = source.split("function enableJuliaOperationalMode()")[1].split("function startJuliaOperationalHandoff")[0]
        hardcoded_idx = fn.find("Faça uma pergunta sobre logística...")
        resolve_idx = fn.find("resolveWelcomeTypewriterText")
        assert resolve_idx != -1
        assert hardcoded_idx == -1 or resolve_idx < hardcoded_idx
        assert "Como posso ajudar sua operação logística hoje?" not in source
        assert "Descreva seu desafio logístico" not in source
        assert "Tem uma dúvida de logística?" not in source

    def test_chat_julia_operacional_nao_usa_experimento(self, monkeypatch):
        _web, client, _user = _home_client(monkeypatch, authenticated=True, user_id=9)
        html = client.get("/chat_julia?mode=operational").get_data(as_text=True)
        assert 'data-typewriter-text="Faça uma pergunta sobre logística..."' in html
        assert "HOME_CTA_EXPERIMENT" not in html
        for text in HOME_CTA_VARIANTS.values():
            assert f'data-typewriter-text="{text}"' not in html

    def test_js_so_marca_superficie_na_home(self):
        source = pathlib.Path("app/static/js/chat_behavior.js").read_text(encoding="utf-8")
        assert "payload.home_cta_surface = true" in source
        assert "HOME_CTA_EXPERIMENT" in source.split("payload.home_cta_surface = true")[0][-400:]
        payload_block = source.split("var payload = { message: text, history: history }")[1].split("if (options.cta_id)")[0]
        assert "variant" not in payload_block
        assert "assignment_id" not in payload_block


class TestHomeCtaTelemetry:
    def test_primeiro_get_registra_impression(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch)
        monkeypatch.setattr(svc, "try_record_home_cta_impression", _persist_impression_on_sqlite(app))
        resp = client.get("/")
        assert resp.status_code == 200
        with app.app_context():
            rows = HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_IMPRESSION).all()
            assert len(rows) == 1
            assert rows[0].variant in HOME_CTA_VARIANTS
            assert rows[0].experiment == HOME_CTA_EXPERIMENT
            assert rows[0].interaction_origin is None

    def test_refresh_nao_duplica_impression(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch)
        monkeypatch.setattr(svc, "try_record_home_cta_impression", _persist_impression_on_sqlite(app))
        client.get("/")
        client.get("/")
        with app.app_context():
            assert HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_IMPRESSION).count() == 1

    def test_falha_impression_nao_quebra_home(self, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch)
        monkeypatch.setattr(
            svc,
            "record_home_cta_event",
            lambda **_k: (_ for _ in ()).throw(RuntimeError("telemetry down")),
        )
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert _typewriter_text(html) in HOME_CTA_VARIANTS.values()

    def test_primeira_mensagem_typed_cria_conversion(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch)
        _mock_discovery(monkeypatch)
        monkeypatch.setattr(svc, "try_record_home_cta_impression", _persist_impression_on_sqlite(app))
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        client.get("/")
        resp = client.post("/api/onboarding_discovery", json={"message": "quero reduzir custo", "history": []})
        assert resp.status_code == 200
        with app.app_context():
            rows = HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).all()
            assert len(rows) == 1
            assert rows[0].interaction_origin == ORIGIN_TYPED

    def test_suggestion_cria_conversion_com_origin_suggestion(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch)
        _mock_discovery(monkeypatch)
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        client.get("/")
        resp = client.post(
            "/api/onboarding_discovery",
            json={"message": "Quero reduzir meu custo de frete", "history": [], "cta_id": "reduce_cost"},
        )
        assert resp.status_code == 200
        with app.app_context():
            row = HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).one()
            assert row.interaction_origin == ORIGIN_SUGGESTION

    def test_segunda_mensagem_nao_duplica_conversion(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch)
        _mock_discovery(monkeypatch)
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        client.get("/")
        client.post("/api/onboarding_discovery", json={"message": "oi 1", "history": []})
        client.post("/api/onboarding_discovery", json={"message": "oi 2", "history": []})
        with app.app_context():
            assert HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).count() == 1

    def test_conversao_sem_assignment_nao_cria_evento(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch)
        _mock_discovery(monkeypatch)
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        resp = client.post("/api/onboarding_discovery", json={"message": "oi", "history": []})
        assert resp.status_code == 200
        with app.app_context():
            assert HomeCtaExperimentEvent.query.count() == 0

    def test_falha_telemetria_nao_quebra_chat(self, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch)
        _mock_discovery(monkeypatch)
        monkeypatch.setattr(
            svc,
            "record_home_cta_event",
            lambda **_k: (_ for _ in ()).throw(RuntimeError("telemetry down")),
        )
        client.get("/")
        resp = client.post("/api/onboarding_discovery", json={"message": "oi", "history": []})
        assert resp.status_code == 200
        assert resp.get_json()["reply"] == "ok"

    def test_record_event_idempotente_no_sqlite(self, app):
        with app.app_context():
            kwargs = dict(
                experiment=HOME_CTA_EXPERIMENT,
                assignment_id="abc123def456",
                variant="cta_b",
                event_type=EVENT_IMPRESSION,
            )
            first = record_home_cta_event(**kwargs)
            db.session.commit()
            second = record_home_cta_event(**kwargs)
            db.session.commit()
            assert first["created"] is True
            assert second["created"] is False
            assert HomeCtaExperimentEvent.query.count() == 1


class TestHomeCtaOperationalHomeConversion:
    def test_home_autenticada_chat_julia_gera_conversion(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch, authenticated=True, user_id=55)
        _mock_julia_chat(monkeypatch)
        monkeypatch.setattr(svc, "try_record_home_cta_impression", _persist_impression_on_sqlite(app))
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        assert client.get("/").status_code == 200
        with client.session_transaction() as sess:
            assignment = dict(sess[SESSION_KEY])
        resp = client.post(
            "/api/chat_julia",
            json={"message": "quero reduzir custo", "history": [], "home_cta_surface": True},
        )
        assert resp.status_code == 200
        assert resp.get_json()["reply"] == "ok-julia"
        with app.app_context():
            row = HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).one()
            assert row.variant == assignment["variant"]
            assert row.assignment_id == assignment["assignment_id"]
            assert row.interaction_origin == ORIGIN_TYPED

    def test_segunda_mensagem_home_operacional_nao_duplica_conversion(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch, authenticated=True, user_id=55)
        _mock_julia_chat(monkeypatch)
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        client.get("/")
        client.post(
            "/api/chat_julia",
            json={"message": "oi 1", "history": [], "home_cta_surface": True},
        )
        client.post(
            "/api/chat_julia",
            json={"message": "oi 2", "history": [], "home_cta_surface": True},
        )
        with app.app_context():
            assert HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).count() == 1

    def test_chat_julia_sem_marcador_nao_gera_conversion(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch, authenticated=True, user_id=55)
        _mock_julia_chat(monkeypatch)
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        client.get("/")
        resp = client.post("/api/chat_julia", json={"message": "oi", "history": []})
        assert resp.status_code == 200
        with app.app_context():
            assert HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).count() == 0

    def test_superficie_chat_julia_nao_recebe_marcador_nem_converte(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch, authenticated=True, user_id=9)
        _mock_julia_chat(monkeypatch)
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        client.get("/")
        html = client.get("/chat_julia?mode=operational").get_data(as_text=True)
        assert "HOME_CTA_EXPERIMENT" not in html
        resp = client.post("/api/chat_julia", json={"message": "oi", "history": []})
        assert resp.status_code == 200
        with app.app_context():
            assert HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).count() == 0

    def test_payload_falso_nao_altera_assignment_da_session(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch, authenticated=True, user_id=55)
        _mock_julia_chat(monkeypatch)
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        client.get("/")
        with client.session_transaction() as sess:
            real = dict(sess[SESSION_KEY])
        fake_variant = "cta_a" if real["variant"] != "cta_a" else "cta_b"
        resp = client.post(
            "/api/chat_julia",
            json={
                "message": "oi",
                "history": [],
                "home_cta_surface": True,
                "variant": fake_variant,
                "assignment_id": "forjado-assignment",
                "experiment": "home_chat_cta_v1",
            },
        )
        assert resp.status_code == 200
        with app.app_context():
            row = HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).one()
            assert row.variant == real["variant"]
            assert row.assignment_id == real["assignment_id"]
            assert row.assignment_id != "forjado-assignment"
            assert row.variant != fake_variant

    def test_falha_telemetria_nao_quebra_chat_julia(self, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch, authenticated=True, user_id=55)
        _mock_julia_chat(monkeypatch)
        monkeypatch.setattr(
            svc,
            "record_home_cta_event",
            lambda **_k: (_ for _ in ()).throw(RuntimeError("telemetry down")),
        )
        client.get("/")
        resp = client.post(
            "/api/chat_julia",
            json={"message": "oi", "history": [], "home_cta_surface": True},
        )
        assert resp.status_code == 200
        assert resp.get_json()["reply"] == "ok-julia"

    def test_home_anonima_discovery_permanece_intacta(self, app, monkeypatch):
        from app.services import home_cta_experiment_service as svc

        _web, client, _user = _home_client(monkeypatch, authenticated=False)
        _mock_discovery(monkeypatch)
        monkeypatch.setattr(
            svc,
            "try_record_home_cta_conversion_from_session",
            _persist_conversion_on_sqlite(app),
        )
        client.get("/")
        resp = client.post("/api/onboarding_discovery", json={"message": "oi", "history": []})
        assert resp.status_code == 200
        assert resp.get_json()["reply"] == "ok"
        with app.app_context():
            row = HomeCtaExperimentEvent.query.filter_by(event_type=EVENT_CONVERSION).one()
            assert row.interaction_origin == ORIGIN_TYPED


class TestHomeCtaNegativeScope:
    def test_funnel_event_permanece_sem_alteracao(self):
        models = pathlib.Path("app/models.py").read_text(encoding="utf-8")
        funnel_src = pathlib.Path("app/funnel_event_service.py").read_text(encoding="utf-8")
        funnel_block = models.split("class FunnelEvent")[1].split("class ")[0]
        assert "user_id = db.Column(db.Integer, db.ForeignKey(\"user.id\"), nullable=False)" in funnel_block
        assert "home_cta" not in funnel_src
        assert "HomeCtaExperimentEvent" not in funnel_src
        assert "cta_a" not in funnel_src

    def test_nenhum_novo_meta_ou_openai_ads_event(self):
        for path in (
            "app/templates/partials/pixel_events.html",
            "app/templates/partials/openai_ads_pixel_events.html",
            "app/privacy_marketing.py",
        ):
            src = pathlib.Path(path).read_text(encoding="utf-8")
            assert "home_chat_cta" not in src
            assert "HomeCtaExperiment" not in src

    def test_get_home_nao_chama_llm(self, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_discovery_reply",
            lambda *a, **k: called.append("discovery") or {"reply": "x"},
        )
        monkeypatch.setattr(
            "app.run_cleiton_discovery.cleiton_governed_generate_content",
            lambda *a, **k: called.append("gemini") or MagicMock(),
        )
        _web, client, _user = _home_client(monkeypatch)
        assert client.get("/").status_code == 200
        assert called == []

    def test_billing_auth_prompts_intocados(self):
        for path in (
            "app/services/cleiton_monetizacao_service.py",
            "app/services/conta_franquia_service.py",
            "app/auth_services.py",
            "app/prompts.py",
        ):
            src = pathlib.Path(path).read_text(encoding="utf-8")
            assert "home_chat_cta" not in src
            assert "HomeCtaExperimentEvent" not in src


class TestHomeCtaMigration:
    def test_chain_e_conteudo_aditivo(self):
        cfg = Config()
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        script = ScriptDirectory.from_config(cfg)
        rev = script.get_revision("z0a1b2c3d4e5")
        assert rev is not None
        assert rev.down_revision == "y9z0a1b2c3d4"
        assert script.get_heads() == ["z0a1b2c3d4e5"]
        source = MIG_PATH.read_text(encoding="utf-8")
        assert "op.create_table" in source
        assert "home_cta_experiment_event" in source
        assert "uq_home_cta_experiment_event_assignment_type" in source
        assert "ix_home_cta_experiment_event_experiment_occurred_at" in source
        lowered = source.lower()
        assert "alter_table" not in lowered
        assert "op.execute" not in lowered
        assert "update " not in lowered
        assert "delete " not in lowered
        assert "insert " not in lowered
        assert "funnel_event" not in lowered
        assert 'drop_table("user")' not in lowered
        assert "drop_table(\"conta\")" not in lowered

    def test_upgrade_downgrade_sqlite(self, tmp_path):
        import importlib.util

        from alembic.operations import Operations
        from alembic.runtime.migration import MigrationContext
        from sqlalchemy import create_engine, inspect

        db_path = tmp_path / "home_cta_mig.sqlite"
        engine = create_engine(f"sqlite:///{db_path}")
        spec = importlib.util.spec_from_file_location("home_cta_mig_mod", MIG_PATH)
        mig = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mig)

        def _run(fn):
            with engine.connect() as conn:
                context = MigrationContext.configure(conn, opts={"render_as_batch": True})
                ops = Operations(context)
                original_op = mig.op
                try:
                    mig.op = ops
                    with conn.begin():
                        fn()
                finally:
                    mig.op = original_op

        _run(mig.upgrade)
        tables = inspect(engine).get_table_names()
        assert "home_cta_experiment_event" in tables
        cols = {c["name"] for c in inspect(engine).get_columns("home_cta_experiment_event")}
        assert cols == {
            "id",
            "experiment",
            "assignment_id",
            "variant",
            "event_type",
            "interaction_origin",
            "occurred_at",
        }
        assert "user_id" not in cols
        assert "email" not in cols
        _run(mig.downgrade)
        assert "home_cta_experiment_event" not in inspect(engine).get_table_names()

    def test_model_sem_pii(self):
        cols = {c.name for c in HomeCtaExperimentEvent.__table__.columns}
        forbidden = {
            "user_id",
            "email",
            "conta_id",
            "franquia_id",
            "ip",
            "user_agent",
            "message",
            "reply",
            "history",
            "prompt",
        }
        assert cols.isdisjoint(forbidden)
        assert HomeCtaExperimentEvent.__tablename__ == "home_cta_experiment_event"
