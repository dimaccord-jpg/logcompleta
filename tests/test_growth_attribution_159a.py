"""Testes direcionados do Lote 159A SCRUM-159: atribuicao first-party por UTM."""
from __future__ import annotations

import importlib
import os
from types import SimpleNamespace

import pytest

from app.funnel_event_service import (
    FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
    FUNNEL_EVENT_SIGNUP_COMPLETED,
    FUNNEL_SOURCE_GROWTH,
    META_PIXEL_ALLOWED_EVENTS,
    SIGNUP_METHOD_GOOGLE,
    SIGNUP_METHOD_PASSWORD,
    is_meta_pixel_allowed,
    record_funnel_event,
    try_record_session_origin_observed,
    try_record_signup_completed,
)
from app.growth_routes import growth_bp
from app.models import FunnelEvent
from app.services.growth_attribution_service import (
    DEFERRED_CLICK_ID_PARAMS,
    SESSION_KEY,
    apply_growth_attribution_before_request,
    get_session_growth_attribution,
    sanitize_growth_attribution_snapshot,
    snapshot_growth_attribution_for_persist,
    validate_attribution_field,
)
from tests.conftest import seed_conta_franquia_cliente, seed_usuario


def _load_web():
    os.environ.setdefault("APP_ENV", "dev")
    os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost:5432/testdb")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    return importlib.import_module("app.web")


@pytest.fixture
def attr_app(app):
    """App minimo com sessao + before_request de atribuicao."""
    app.config["SECRET_KEY"] = "test-secret-159a"
    app.config["TESTING"] = True

    @app.route("/")
    def index():
        return "ok"

    @app.route("/login")
    def login():
        return "login"

    @app.route("/fretes")
    def fretes():
        return "fretes"

    @app.route("/api/thing")
    def api_thing():
        return "api"

    @app.route("/health/liveness")
    def health_liveness():
        return "ok"

    @app.route("/logout")
    def logout():
        return "bye"

    @app.before_request
    def _attr():
        apply_growth_attribution_before_request()

    return app


@pytest.fixture
def attr_client(attr_app):
    return attr_app.test_client()


# --- CAPTURA ---


def test_valid_utm_creates_growth_attribution(attr_client):
    resp = attr_client.get("/?utm_source=google&utm_campaign=campA&utm_medium=cpc")
    assert resp.status_code == 200
    with attr_client.session_transaction() as sess:
        ctx = sess.get(SESSION_KEY)
        assert ctx is not None
        assert ctx["version"] == 1
        assert ctx["first_touch"]["source"] == "google"
        assert ctx["first_touch"]["campaign"] == "campA"
        assert ctx["first_touch"]["medium"] == "cpc"
        assert ctx["first_touch"]["landing_page"] == "index"
        assert ctx["current_session_origin"] == ctx["first_touch"]


def test_without_utm_does_not_create_context(attr_client):
    resp = attr_client.get("/")
    assert resp.status_code == 200
    with attr_client.session_transaction() as sess:
        assert SESSION_KEY not in sess


def test_first_touch_immutable_on_second_campaign(attr_client):
    attr_client.get("/?utm_source=google&utm_campaign=A")
    attr_client.get("/?utm_source=linkedin&utm_campaign=B")
    with attr_client.session_transaction() as sess:
        ctx = sess[SESSION_KEY]
        assert ctx["first_touch"]["source"] == "google"
        assert ctx["first_touch"]["campaign"] == "A"
        assert ctx["current_session_origin"]["source"] == "linkedin"
        assert ctx["current_session_origin"]["campaign"] == "B"


def test_current_session_origin_replaced_as_unit(attr_client):
    attr_client.get("/?utm_source=google&utm_medium=cpc&utm_campaign=A")
    attr_client.get("/?utm_source=linkedin&utm_campaign=B")
    with attr_client.session_transaction() as sess:
        current = sess[SESSION_KEY]["current_session_origin"]
        assert current["source"] == "linkedin"
        assert current["campaign"] == "B"
        assert "medium" not in current


def test_later_absence_does_not_clear_origin(attr_client):
    attr_client.get("/?utm_source=google&utm_campaign=A")
    attr_client.get("/")
    with attr_client.session_transaction() as sess:
        ctx = sess[SESSION_KEY]
        assert ctx["first_touch"]["source"] == "google"
        assert ctx["current_session_origin"]["source"] == "google"


def test_never_creates_direct(attr_client):
    attr_client.get("/")
    attr_client.get("/login")
    with attr_client.session_transaction() as sess:
        assert SESSION_KEY not in sess
        assert "direct" not in str(sess.get(SESSION_KEY) or {}).lower()


def test_over_limit_field_discarded_not_truncated():
    assert validate_attribution_field("source", "x" * 51) is None
    assert validate_attribution_field("source", "ok-source") == "ok-source"
    assert validate_attribution_field("campaign", "c" * 81) is None
    assert validate_attribution_field("campaign_id", "i" * 121) is None
    assert validate_attribution_field("content", "t" * 121) is None


def test_control_url_email_discarded(attr_client):
    assert validate_attribution_field("source", "https://evil.example/path") is None
    assert validate_attribution_field("campaign", "user@example.com") is None
    assert validate_attribution_field("content", "has\x00null") is None
    assert validate_attribution_field("medium", "good") == "good"

    attr_client.get(
        "/?utm_source=https://evil.example/path"
        "&utm_medium=good"
        "&utm_campaign=user@example.com"
        "&utm_id=valid-id"
    )
    with attr_client.session_transaction() as sess:
        ctx = sess.get(SESSION_KEY)
        assert ctx is not None
        origin = ctx["current_session_origin"]
        assert "source" not in origin
        assert origin.get("medium") == "good"
        assert "campaign" not in origin
        assert origin.get("campaign_id") == "valid-id"


def test_at_sign_anywhere_discards_utm_field(attr_client):
    """Qualquer '@' no valor UTM descarta o campo inteiro (captura + snapshot)."""
    assert validate_attribution_field("campaign", "ana@example.com") is None
    assert validate_attribution_field("campaign", "Contato ana@example.com") is None
    assert validate_attribution_field("campaign", "campanha-ana@example.com-x") is None
    assert (
        validate_attribution_field("campaign", "campanha_setembro_2026")
        == "campanha_setembro_2026"
    )

    # Campo com '@' descartado; outro UTM valido permanece → contexto criado.
    attr_client.get(
        "/?utm_source=google&utm_campaign=Contato%20ana@example.com"
    )
    with attr_client.session_transaction() as sess:
        ctx = sess.get(SESSION_KEY)
        assert ctx is not None
        origin = ctx["current_session_origin"]
        assert origin.get("source") == "google"
        assert "campaign" not in origin
        assert "@" not in str(origin)

    # Todos os campos de aquisicao com '@' → nao cria/altera contexto.
    client2 = attr_client
    with client2.session_transaction() as sess:
        sess.pop(SESSION_KEY, None)
    client2.get(
        "/?utm_source=ana@example.com"
        "&utm_medium=Contato%20ana@example.com"
        "&utm_campaign=campanha-ana@example.com-x"
    )
    with client2.session_transaction() as sess:
        assert SESSION_KEY not in sess

    # Snapshot manual com '@' rejeitado antes da persistencia.
    assert (
        sanitize_growth_attribution_snapshot(
            {"current_session_origin": {"source": "Contato ana@example.com"}}
        )
        is None
    )
    assert (
        sanitize_growth_attribution_snapshot(
            {"first_touch": {"campaign": "campanha-ana@example.com-x"}}
        )
        is None
    )
    assert (
        sanitize_growth_attribution_snapshot(
            {
                "current_session_origin": {
                    "source": "google",
                    "campaign": "campanha_setembro_2026",
                }
            }
        )
        is not None
    )


def test_arbitrary_query_not_captured(attr_client):
    attr_client.get("/?utm_source=google&foo=bar&email=x@y.com&adset_id=99&utm_term=kw")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert set(origin.keys()) <= {
            "source",
            "medium",
            "campaign",
            "campaign_id",
            "content",
            "landing_page",
        }
        assert origin["source"] == "google"
        assert "foo" not in origin
        assert "email" not in origin
        assert "adset_id" not in origin
        assert "term" not in origin


def test_click_ids_ignored(attr_client):
    assert DEFERRED_CLICK_ID_PARAMS == {"gclid", "gbraid", "wbraid", "fbclid"}
    attr_client.get(
        "/?utm_source=google"
        "&gclid=CLICK1&gbraid=CLICK2&wbraid=CLICK3&fbclid=CLICK4"
    )
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        blob = str(origin)
        assert "CLICK1" not in blob
        assert "CLICK2" not in blob
        assert "CLICK3" not in blob
        assert "CLICK4" not in blob
        assert "gclid" not in origin
        assert "fbclid" not in origin


# --- LANDING ---


def test_landing_page_uses_endpoint(attr_client):
    attr_client.get("/login?utm_source=newsletter")
    with attr_client.session_transaction() as sess:
        assert sess[SESSION_KEY]["current_session_origin"]["landing_page"] == "login"


def test_landing_does_not_persist_raw_path_or_query(attr_client):
    attr_client.get("/fretes?utm_source=google&utm_campaign=x&extra=1")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin["landing_page"] == "fretes"
        assert all("/" not in str(v) for v in origin.values())
        assert "extra=1" not in str(origin)


def test_landing_alone_does_not_create_origin(attr_client):
    attr_client.get("/login")
    with attr_client.session_transaction() as sess:
        assert SESSION_KEY not in sess


def test_api_and_health_excluded(attr_client):
    attr_client.get("/api/thing?utm_source=google")
    attr_client.get("/health/liveness?utm_source=google")
    attr_client.get("/logout?utm_source=google")
    with attr_client.session_transaction() as sess:
        assert SESSION_KEY not in sess


# --- SIGNUP ---


def test_password_signup_completed_receives_snapshot(attr_app, app):
    with attr_app.test_request_context("/?utm_source=google&utm_campaign=launch"):
        apply_growth_attribution_before_request()
        assert get_session_growth_attribution() is not None
        conta, franquia = seed_conta_franquia_cliente(slug="159a-signup-pw")
        user = seed_usuario(franquia.id, conta.id, email="159a-signup-pw@test.com")
        try_record_signup_completed(user, signup_method=SIGNUP_METHOD_PASSWORD)
        event = FunnelEvent.query.filter_by(
            event_name=FUNNEL_EVENT_SIGNUP_COMPLETED,
            user_id=user.id,
        ).one()
        assert event.metadata_json["signup_method"] == SIGNUP_METHOD_PASSWORD
        ga = event.metadata_json["growth_attribution"]
        assert ga["first_touch"]["source"] == "google"
        assert ga["first_touch"]["campaign"] == "launch"
        assert ga["current_session_origin"]["source"] == "google"
        assert "visitor_id" not in str(event.metadata_json)
        assert "onboarding_discovery_anon_id" not in str(event.metadata_json)


def test_oauth_new_user_receives_snapshot(monkeypatch):
    web = _load_web()
    completed = []

    def _capture(user, **kwargs):
        completed.append(
            {
                "user_id": user.id,
                "signup_method": kwargs.get("signup_method"),
                "snap": snapshot_growth_attribution_for_persist(
                    include_first_touch=True,
                    include_current=True,
                ),
            }
        )

    monkeypatch.setattr(web, "try_record_signup_completed", _capture)
    monkeypatch.setattr(web, "try_record_session_origin_observed", lambda *_a, **_k: None)
    new_user = SimpleNamespace(
        id=501,
        conta_id=3,
        franquia_id=4,
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: "501",
        job_role="",
        usage_purpose="",
    )
    monkeypatch.setattr(
        web,
        "handle_google_oauth_callback",
        lambda *a, **k: (new_user, None, False, True),
    )
    monkeypatch.setattr(web, "login_user", lambda *_a, **_k: None)
    monkeypatch.setattr(web, "_post_login_redirect", lambda *_a, **_k: web.redirect("/"))

    client = web.app.test_client()
    client.get("/?utm_source=oauth-src&utm_campaign=oauth-camp")
    with client.session_transaction() as sess:
        sess["oauth_state"] = "state-new"
        sess["oauth_states"] = ["state-new"]
        assert SESSION_KEY in sess

    resp = client.get("/login/google/callback?code=abc&state=state-new", follow_redirects=False)
    assert resp.status_code in (302, 200)
    assert len(completed) == 1
    assert completed[0]["signup_method"] == SIGNUP_METHOD_GOOGLE
    assert completed[0]["snap"]["first_touch"]["source"] == "oauth-src"


def test_oauth_existing_user_no_signup_completed(monkeypatch):
    web = _load_web()
    completed = []
    observed = []
    monkeypatch.setattr(
        web,
        "try_record_signup_completed",
        lambda *a, **k: completed.append(True),
    )
    monkeypatch.setattr(
        web,
        "try_record_session_origin_observed",
        lambda user, **k: observed.append(getattr(user, "id", None)),
    )
    existing = SimpleNamespace(
        id=7,
        conta_id=1,
        franquia_id=1,
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: "7",
        job_role="x",
        usage_purpose="y",
    )
    monkeypatch.setattr(
        web,
        "handle_google_oauth_callback",
        lambda *a, **k: (existing, None, False, False),
    )
    monkeypatch.setattr(web, "login_user", lambda *_a, **_k: None)
    monkeypatch.setattr(web, "_post_login_redirect", lambda *_a, **_k: web.redirect("/"))

    client = web.app.test_client()
    client.get("/?utm_source=keep")
    with client.session_transaction() as sess:
        sess["oauth_state"] = "state-ex"
        sess["oauth_states"] = ["state-ex"]
    resp = client.get("/login/google/callback?code=abc&state=state-ex", follow_redirects=False)
    assert resp.status_code in (302, 200)
    assert completed == []
    assert observed == [7]


# --- LOGIN / AUTHENTICATED ---


def test_login_existing_with_origin_emits_session_origin_observed(monkeypatch):
    web = _load_web()
    observed = []
    monkeypatch.setattr(
        web,
        "try_record_session_origin_observed",
        lambda user, **k: observed.append(user.id),
    )
    fake_user = SimpleNamespace(
        id=88,
        conta_id=1,
        franquia_id=1,
        is_authenticated=True,
        is_active=True,
        is_anonymous=False,
        get_id=lambda: "88",
    )
    monkeypatch.setattr(web, "authenticate_user", lambda *a, **k: (fake_user, None))
    monkeypatch.setattr(web, "login_user", lambda *_a, **_k: None)
    monkeypatch.setattr(web, "_post_login_redirect", lambda *_a, **_k: web.redirect("/"))
    monkeypatch.setattr(web, "get_active_term", lambda: None)

    client = web.app.test_client()
    client.get("/?utm_source=login-src&utm_campaign=L1")
    resp = client.post(
        "/login",
        data={"email": "a@b.com", "password": "x"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 200)
    assert observed == [88]


def test_login_without_origin_does_not_emit(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="159a-login-no-origin")
        user = seed_usuario(franquia.id, conta.id, email="159a-login-no@test.com")
        with app.test_request_context("/"):
            try_record_session_origin_observed(user)
            assert (
                FunnelEvent.query.filter_by(
                    event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
                    user_id=user.id,
                ).count()
                == 0
            )


def test_authenticated_new_utm_emits_observation(attr_app, app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="159a-auth-utm")
        user = seed_usuario(franquia.id, conta.id, email="159a-auth-utm@test.com")
        uid = user.id
        auth_user = SimpleNamespace(
            id=uid,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            is_authenticated=True,
        )

    monkeypatch.setattr(
        "app.services.growth_attribution_service.current_user",
        auth_user,
    )
    client = attr_app.test_client()

    client.get("/fretes?utm_source=first&utm_campaign=A")
    with app.app_context():
        assert (
            FunnelEvent.query.filter_by(
                event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
                user_id=uid,
            ).count()
            == 1
        )

    client.get("/fretes?utm_source=first&utm_campaign=A")
    with app.app_context():
        assert (
            FunnelEvent.query.filter_by(
                event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
                user_id=uid,
            ).count()
            == 1
        )

    client.get("/fretes?utm_source=second&utm_campaign=B")
    with app.app_context():
        rows = (
            FunnelEvent.query.filter_by(
                event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
                user_id=uid,
            )
            .order_by(FunnelEvent.id.asc())
            .all()
        )
        assert len(rows) == 2
        last = rows[-1].metadata_json["growth_attribution"]
        assert set(last.keys()) == {"current_session_origin"}
        assert last["current_session_origin"]["source"] == "second"
        assert "first_touch" not in last


def test_same_origin_repeated_no_duplicate_observation(attr_app, app, monkeypatch):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="159a-same-origin")
        user = seed_usuario(franquia.id, conta.id, email="159a-same@test.com")
        uid = user.id
        auth_user = SimpleNamespace(
            id=uid,
            conta_id=user.conta_id,
            franquia_id=user.franquia_id,
            is_authenticated=True,
        )
    monkeypatch.setattr(
        "app.services.growth_attribution_service.current_user",
        auth_user,
    )
    client = attr_app.test_client()
    for _ in range(3):
        client.get("/?utm_source=stable&utm_medium=cpc")
    with app.app_context():
        assert (
            FunnelEvent.query.filter_by(
                event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
                user_id=uid,
            ).count()
            == 1
        )


# --- PRIVACIDADE ---


def test_snapshot_rejects_extra_keys():
    assert (
        sanitize_growth_attribution_snapshot(
            {"first_touch": {"source": "x"}, "evil": 1}
        )
        is None
    )
    assert (
        sanitize_growth_attribution_snapshot(
            {"current_session_origin": {"source": "x", "email": "a@b.com"}}
        )
        is None
    )
    # Click IDs sao chaves estruturais permitidas (gate de consentimento e no snapshot).
    with_click = sanitize_growth_attribution_snapshot(
        {"current_session_origin": {"source": "x", "gclid": "AbC123"}}
    )
    assert with_click is not None
    assert with_click["current_session_origin"]["gclid"] == "AbC123"
    ok = sanitize_growth_attribution_snapshot(
        {
            "first_touch": {"source": "google", "landing_page": "index"},
            "current_session_origin": {"source": "linkedin"},
        }
    )
    assert ok is not None


def test_browser_cannot_forge_attribution_via_page_view(app, ctx):
    app.config["SECRET_KEY"] = "test"
    if "growth" not in app.blueprints:
        app.register_blueprint(growth_bp)
    client = app.test_client()
    resp = client.post(
        "/api/growth/page-view",
        json={
            "event_id": "pv-forge-attr-1",
            "page": "index",
            "growth_attribution": {
                "first_touch": {"source": "forged"},
                "current_session_origin": {"source": "forged"},
            },
            "utm_source": "forged",
        },
    )
    assert resp.status_code == 200
    event = FunnelEvent.query.filter_by(idempotency_key="pv-forge-attr-1").one()
    assert event.metadata_json == {"page": "index"}
    assert "growth_attribution" not in (event.metadata_json or {})


def test_record_rejects_forged_extra_attribution_keys(app):
    with app.app_context():
        conta, franquia = seed_conta_franquia_cliente(slug="159a-forge-meta")
        user = seed_usuario(franquia.id, conta.id, email="159a-forge-meta@test.com")
        with pytest.raises(ValueError, match="growth_attribution"):
            record_funnel_event(
                event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
                source=FUNNEL_SOURCE_GROWTH,
                user_id=user.id,
                idempotency_key="growth:session_origin_observed:forge:1",
                metadata_json={
                    "growth_attribution": {
                        "current_session_origin": {
                            "source": "x",
                            "email": "a@b.com",
                        }
                    }
                },
            )


def test_no_visitor_id_created(attr_client):
    attr_client.get("/?utm_source=google")
    with attr_client.session_transaction() as sess:
        keys = list(sess.keys())
        assert SESSION_KEY in keys
        assert not any("visitor" in str(k).lower() for k in keys)
        ctx = sess[SESSION_KEY]
        assert "visitor_id" not in ctx
        assert "sid" not in ctx
        assert "session_id" not in ctx


def test_onboarding_discovery_anon_id_not_reused(attr_client):
    with attr_client.session_transaction() as sess:
        sess["onboarding_discovery_anon_id"] = "anon-should-not-be-growth"
    attr_client.get("/?utm_source=google")
    with attr_client.session_transaction() as sess:
        ctx = sess[SESSION_KEY]
        assert "onboarding_discovery_anon_id" not in str(ctx)
        assert ctx["first_touch"]["source"] == "google"
        assert sess.get("onboarding_discovery_anon_id") == "anon-should-not-be-growth"


# --- LOGOUT ---


def test_logout_session_clear_removes_growth_attribution(monkeypatch):
    web = _load_web()
    monkeypatch.setattr(web, "logout_user", lambda: None)
    client = web.app.test_client()
    client.get("/?utm_source=google&utm_campaign=bye")
    with client.session_transaction() as sess:
        assert SESSION_KEY in sess
    resp = client.get("/logout", follow_redirects=False)
    assert resp.status_code in (302, 200)
    with client.session_transaction() as sess:
        assert SESSION_KEY not in sess


# --- EXTERNO ---


def test_session_origin_observed_outside_meta_allowlist():
    assert FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED not in META_PIXEL_ALLOWED_EVENTS
    assert is_meta_pixel_allowed(FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED) is False
    assert META_PIXEL_ALLOWED_EVENTS == set() or META_PIXEL_ALLOWED_EVENTS == frozenset()


def test_session_origin_observed_rejects_anonymous(app):
    with app.app_context():
        with pytest.raises(ValueError, match="user_id"):
            record_funnel_event(
                event_name=FUNNEL_EVENT_SESSION_ORIGIN_OBSERVED,
                source=FUNNEL_SOURCE_GROWTH,
                user_id=None,
                idempotency_key="growth:session_origin_observed:anon:1",
                metadata_json={
                    "growth_attribution": {
                        "current_session_origin": {"source": "google"}
                    }
                },
            )


def test_capitalization_preserved(attr_client):
    attr_client.get("/?utm_source=GoogleAds&utm_campaign=Brand_Campaign")
    with attr_client.session_transaction() as sess:
        origin = sess[SESSION_KEY]["current_session_origin"]
        assert origin["source"] == "GoogleAds"
        assert origin["campaign"] == "Brand_Campaign"


def test_non_string_utm_discarded():
    assert validate_attribution_field("source", 12345) is None
    assert validate_attribution_field("source", None) is None
    assert validate_attribution_field("source", ["google"]) is None
