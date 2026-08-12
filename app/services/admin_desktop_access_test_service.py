"""
Homologação E2E Replay da jornada Landing Desktop.

Experiência VISÍVEL idêntica à real (mesmo e-mail, CTA, cadastro, unsubscribe).
Estado canônico em DesktopAccessE2ETestRun — isolado de Lead/FunnelEvent/Meta.
Disponível apenas em APP_ENV=dev|homolog, somente para o e-mail do admin autenticado.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from flask import has_request_context, session
from flask_login import current_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, func, or_

from app.auth_services import send_email
from app.extensions import db
from app.models import DesktopAccessE2ETestRun, User, utcnow_naive
from app.services.lead_campaign_email_service import (
    FOLLOWUP_DELAY_HOURS,
    MAX_FOLLOWUPS,
    build_followup_email,
    build_initial_cta_email,
)

logger = logging.getLogger(__name__)

PURPOSE_E2E_CTA = "desktop_access_e2e_cta"
PURPOSE_E2E_UNSUBSCRIBE = "desktop_access_e2e_unsubscribe"
PURPOSE_E2E_ACTIVATION_UNSUBSCRIBE = "desktop_access_e2e_activation_unsubscribe"

# Aliases de compatibilidade com imports/testes que ainda usam nomes legados.
PURPOSE_TEST_CTA = PURPOSE_E2E_CTA
PURPOSE_TEST_FOLLOWUP = PURPOSE_E2E_CTA

_E2E_CTA_SALT = "desktop-access-e2e-cta-salt"
_E2E_UNSUBSCRIBE_SALT = "desktop-access-e2e-unsubscribe-salt"
_E2E_ACTIVATION_UNSUBSCRIBE_SALT = "desktop-access-e2e-activation-unsubscribe-salt"

TEST_TOKEN_MAX_AGE_SECONDS = 3600
TEST_MODE_TTL_SECONDS = 3600
SEND_COOLDOWN_SECONDS = 10

ALLOWED_TEST_ENVS = frozenset({"dev", "homolog"})

SESSION_TEST_MODE_KEY = "desktop_access_e2e"
SESSION_CSRF_KEY = "desktop_access_e2e_csrf"
SESSION_START_LAST_SENT_KEY = "desktop_access_e2e_start_last_sent_at"
SESSION_START_IN_FLIGHT_KEY = "desktop_access_e2e_start_in_flight"
SESSION_CHECK_FOLLOWUP_LAST_KEY = "desktop_access_e2e_check_followup_last_at"
SESSION_CHECK_FOLLOWUP_IN_FLIGHT_KEY = "desktop_access_e2e_check_followup_in_flight"
SESSION_ACTIVATION_INSPECT_LAST_KEY = "desktop_access_e2e_activation_inspect_last_at"
SESSION_ACTIVATION_INSPECT_IN_FLIGHT_KEY = "desktop_access_e2e_activation_inspect_in_flight"
SESSION_ACTIVATION_TIMED_LAST_KEY = "desktop_access_e2e_activation_timed_last_at"
SESSION_ACTIVATION_TIMED_IN_FLIGHT_KEY = "desktop_access_e2e_activation_timed_in_flight"

ACTION_START_E2E = "start_desktop_access_e2e"
ACTION_CHECK_FOLLOWUP = "check_desktop_access_e2e_followup"
ACTION_INSPECT_ACTIVATION_EMAIL_1 = "inspect_desktop_access_activation_email_1"
ACTION_INSPECT_ACTIVATION_EMAIL_2 = "inspect_desktop_access_activation_email_2"
ACTION_START_ACTIVATION_TIMED = "start_desktop_access_activation_timed"
ACTION_CHECK_ACTIVATION = "check_desktop_access_e2e_activation"

# Legado (UI antiga removida; mantido só se algum teste ainda referenciar).
ACTION_SEND_CTA = ACTION_START_E2E
ACTION_SEND_FOLLOWUP = ACTION_CHECK_FOLLOWUP

_STATUS_SENT = "sent"
_STATUS_FAILED = "failed"
_STATUS_REJECTED = "rejected"
_STATUS_COOLDOWN = "cooldown"
_STATUS_SKIPPED = "skipped"
_STATUS_NOT_ELIGIBLE = "not_eligible"


def resolve_app_env() -> str:
    """Resolução canônica de APP_ENV (settings após boot; fallback ao env)."""
    try:
        from app.settings import settings

        return str(settings.app_env).strip().lower()
    except Exception:
        import os

        return (os.getenv("APP_ENV") or "").strip().lower()


def is_admin_test_env_allowed(app_env: str | None = None) -> bool:
    env = (app_env if app_env is not None else resolve_app_env()).strip().lower()
    return env in ALLOWED_TEST_ENVS


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _serializer(secret_key: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=salt)


def new_test_run_id() -> str:
    return uuid.uuid4().hex


def generate_e2e_cta_token(*, run_id: str, user_id: int, secret_key: str) -> str:
    payload = {
        "purpose": PURPOSE_E2E_CTA,
        "run_id": str(run_id),
        "user_id": int(user_id),
    }
    return _serializer(secret_key, _E2E_CTA_SALT).dumps(payload)


def generate_e2e_unsubscribe_token(*, run_id: str, user_id: int, secret_key: str) -> str:
    payload = {
        "purpose": PURPOSE_E2E_UNSUBSCRIBE,
        "run_id": str(run_id),
        "user_id": int(user_id),
    }
    return _serializer(secret_key, _E2E_UNSUBSCRIBE_SALT).dumps(payload)


def generate_e2e_activation_unsubscribe_token(
    *, run_id: str, user_id: int, secret_key: str
) -> str:
    payload = {
        "purpose": PURPOSE_E2E_ACTIVATION_UNSUBSCRIBE,
        "run_id": str(run_id),
        "user_id": int(user_id),
    }
    return _serializer(secret_key, _E2E_ACTIVATION_UNSUBSCRIBE_SALT).dumps(payload)


# Aliases legados
def generate_test_cta_token(*, user_id: int, run_id: str, secret_key: str) -> str:
    return generate_e2e_cta_token(run_id=run_id, user_id=user_id, secret_key=secret_key)


def generate_test_followup_token(*, user_id: int, run_id: str, secret_key: str) -> str:
    return generate_e2e_cta_token(run_id=run_id, user_id=user_id, secret_key=secret_key)


def _loads_e2e_payload(
    token: str,
    *,
    secret_key: str,
    salt: str,
    expected_purpose: str,
    max_age: int = TEST_TOKEN_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    try:
        data = _serializer(secret_key, salt).loads(token, max_age=max_age)
    except SignatureExpired:
        return None
    except BadSignature:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("purpose") != expected_purpose:
        return None
    if "email" in data:
        return None
    user_id = data.get("user_id")
    run_id = data.get("run_id")
    if not isinstance(user_id, int):
        return None
    if not isinstance(run_id, str) or not run_id.strip():
        return None
    return {
        "purpose": expected_purpose,
        "user_id": user_id,
        "run_id": run_id.strip(),
    }


def loads_e2e_cta_payload(
    token: str,
    *,
    secret_key: str,
    max_age: int = TEST_TOKEN_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    return _loads_e2e_payload(
        token,
        secret_key=secret_key,
        salt=_E2E_CTA_SALT,
        expected_purpose=PURPOSE_E2E_CTA,
        max_age=max_age,
    )


def loads_e2e_unsubscribe_payload(
    token: str,
    *,
    secret_key: str,
    max_age: int = TEST_TOKEN_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    return _loads_e2e_payload(
        token,
        secret_key=secret_key,
        salt=_E2E_UNSUBSCRIBE_SALT,
        expected_purpose=PURPOSE_E2E_UNSUBSCRIBE,
        max_age=max_age,
    )


def loads_e2e_activation_unsubscribe_payload(
    token: str,
    *,
    secret_key: str,
    max_age: int = TEST_TOKEN_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    return _loads_e2e_payload(
        token,
        secret_key=secret_key,
        salt=_E2E_ACTIVATION_UNSUBSCRIBE_SALT,
        expected_purpose=PURPOSE_E2E_ACTIVATION_UNSUBSCRIBE,
        max_age=max_age,
    )


def loads_test_cta_payload(
    token: str,
    *,
    secret_key: str,
    max_age: int = TEST_TOKEN_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    return loads_e2e_cta_payload(token, secret_key=secret_key, max_age=max_age)


def loads_test_followup_payload(
    token: str,
    *,
    secret_key: str,
    max_age: int = TEST_TOKEN_MAX_AGE_SECONDS,
) -> dict[str, Any] | None:
    return loads_e2e_cta_payload(token, secret_key=secret_key, max_age=max_age)


def resolve_admin_test_token(
    token: str,
    *,
    secret_key: str,
    max_age: int = TEST_TOKEN_MAX_AGE_SECONDS,
    app_env: str | None = None,
) -> dict[str, Any] | None:
    """
    Resolve token E2E de CTA.

    Guarda canônica de ambiente: em prod (ou env não permitido) retorna None
    mesmo com token criptograficamente válido. Nunca interpreta token real de Lead.
    """
    if not is_admin_test_env_allowed(app_env):
        return None
    return loads_e2e_cta_payload(token, secret_key=secret_key, max_age=max_age)


def resolve_e2e_unsubscribe_token(
    token: str,
    *,
    secret_key: str,
    max_age: int = TEST_TOKEN_MAX_AGE_SECONDS,
    app_env: str | None = None,
) -> dict[str, Any] | None:
    if not is_admin_test_env_allowed(app_env):
        return None
    payload = loads_e2e_unsubscribe_payload(token, secret_key=secret_key, max_age=max_age)
    if payload is not None:
        return payload
    return loads_e2e_activation_unsubscribe_payload(
        token, secret_key=secret_key, max_age=max_age
    )


def find_user_by_id(user_id: int) -> User | None:
    return db.session.get(User, int(user_id))


def find_user_by_email_normalized(email_normalized: str) -> User | None:
    return (
        User.query.filter(func.lower(User.email) == email_normalized)
        .order_by(User.id.asc())
        .first()
    )


def target_user_still_admin(user: User | None) -> bool:
    if user is None:
        return False
    from app.infra import user_is_admin

    return user_is_admin(user) is True


def get_authorized_admin_test_user(user_id: int) -> User | None:
    user = find_user_by_id(user_id)
    if not target_user_still_admin(user):
        return None
    return user


def find_test_run_by_run_id(run_id: str) -> DesktopAccessE2ETestRun | None:
    rid = (run_id or "").strip()
    if not rid:
        return None
    return DesktopAccessE2ETestRun.query.filter_by(run_id=rid).first()


def get_latest_test_run_for_user(user_id: int) -> DesktopAccessE2ETestRun | None:
    return (
        DesktopAccessE2ETestRun.query.filter_by(user_id=int(user_id))
        .order_by(DesktopAccessE2ETestRun.created_at.desc(), DesktopAccessE2ETestRun.id.desc())
        .first()
    )


def followup_eligible_at(test_run: DesktopAccessE2ETestRun) -> datetime | None:
    if test_run.initial_email_sent_at is None:
        return None
    return test_run.initial_email_sent_at + timedelta(hours=FOLLOWUP_DELAY_HOURS)


def format_admin_ts(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%d/%m/%Y %H:%M")


def build_latest_run_status_payload(user_id: int) -> dict[str, Any] | None:
    from app.services.desktop_access_activation_email_service import (
        ACTIVATION_EMAIL_1_DELAY_HOURS,
        ACTIVATION_EMAIL_2_DELAY_HOURS,
    )

    run = get_latest_test_run_for_user(user_id)
    if run is None:
        return None
    eligible = followup_eligible_at(run)
    act_start = run.activation_sequence_started_at or run.registration_completed_at
    email1_eligible = (
        act_start + timedelta(hours=ACTIVATION_EMAIL_1_DELAY_HOURS)
        if act_start is not None
        else None
    )
    email2_eligible = (
        run.activation_email_1_sent_at + timedelta(hours=ACTIVATION_EMAIL_2_DELAY_HOURS)
        if run.activation_email_1_sent_at is not None
        else None
    )
    return {
        "run_id": run.run_id,
        "run_id_short": run.run_id[:8],
        "created_at": format_admin_ts(run.created_at),
        "initial_email_sent_at": format_admin_ts(run.initial_email_sent_at),
        "cta_clicked_at": format_admin_ts(run.cta_clicked_at),
        "registration_completed_at": format_admin_ts(run.registration_completed_at),
        "first_use_seen_at": format_admin_ts(run.first_use_seen_at),
        "first_audit_seen_at": format_admin_ts(run.first_audit_seen_at),
        "followup_eligible_at": format_admin_ts(eligible),
        "followup_sent_at": format_admin_ts(run.followup_sent_at),
        "opt_out_at": format_admin_ts(run.opt_out_at),
        "completed_at": format_admin_ts(run.completed_at),
        "activation_sequence_started_at": format_admin_ts(act_start),
        "activation_email_1_eligible_at": format_admin_ts(email1_eligible),
        "activation_email_1_sent_at": format_admin_ts(run.activation_email_1_sent_at),
        "activation_email_2_eligible_at": format_admin_ts(email2_eligible),
        "activation_email_2_sent_at": format_admin_ts(run.activation_email_2_sent_at),
        "activation_opt_out_at": format_admin_ts(run.activation_opt_out_at),
    }


def validate_admin_test_email(*, admin_user: Any, email: str) -> tuple[bool, str, User | None]:
    admin_email = normalize_email(getattr(admin_user, "email", None) or "")
    requested = normalize_email(email)
    if not admin_email or not requested:
        return False, "E-mail de teste inválido.", None
    if requested != admin_email:
        return False, "O e-mail de teste deve ser o seu próprio e-mail de administrador.", None
    user = find_user_by_email_normalized(requested)
    if user is None:
        return False, "Usuário correspondente não encontrado.", None
    admin_id = getattr(admin_user, "id", None)
    if admin_id is not None and int(user.id) != int(admin_id):
        return False, "O e-mail de teste deve corresponder à conta autenticada.", None
    if not target_user_still_admin(user):
        return False, "Autorização administrativa indisponível.", None
    return True, "", user


def issue_csrf_token(session_obj=None) -> str:
    sess = session_obj if session_obj is not None else session
    token = secrets.token_urlsafe(32)
    sess[SESSION_CSRF_KEY] = token
    if hasattr(sess, "modified"):
        sess.modified = True
    return token


def validate_and_rotate_csrf(*, submitted: str | None, session_obj=None) -> bool:
    sess = session_obj if session_obj is not None else session
    expected = sess.get(SESSION_CSRF_KEY)
    submitted_value = (submitted or "").strip()
    if not expected or not submitted_value:
        return False
    ok = secrets.compare_digest(str(expected), submitted_value)
    sess.pop(SESSION_CSRF_KEY, None)
    if hasattr(sess, "modified"):
        sess.modified = True
    return ok


def _in_flight_key_for_action(action: str) -> str | None:
    if action == ACTION_START_E2E:
        return SESSION_START_IN_FLIGHT_KEY
    if action == ACTION_CHECK_FOLLOWUP:
        return SESSION_CHECK_FOLLOWUP_IN_FLIGHT_KEY
    if action in (
        ACTION_INSPECT_ACTIVATION_EMAIL_1,
        ACTION_INSPECT_ACTIVATION_EMAIL_2,
    ):
        return SESSION_ACTIVATION_INSPECT_IN_FLIGHT_KEY
    if action in (ACTION_START_ACTIVATION_TIMED, ACTION_CHECK_ACTIVATION):
        return SESSION_ACTIVATION_TIMED_IN_FLIGHT_KEY
    return None


def _cooldown_key_for_action(action: str) -> str | None:
    if action == ACTION_START_E2E:
        return SESSION_START_LAST_SENT_KEY
    if action == ACTION_CHECK_FOLLOWUP:
        return SESSION_CHECK_FOLLOWUP_LAST_KEY
    if action in (
        ACTION_INSPECT_ACTIVATION_EMAIL_1,
        ACTION_INSPECT_ACTIVATION_EMAIL_2,
    ):
        return SESSION_ACTIVATION_INSPECT_LAST_KEY
    if action in (ACTION_START_ACTIVATION_TIMED, ACTION_CHECK_ACTIVATION):
        return SESSION_ACTIVATION_TIMED_LAST_KEY
    return None


def claim_send_slot(
    *,
    action: str,
    session_obj=None,
    now: datetime | None = None,
) -> str | None:
    sess = session_obj if session_obj is not None else session
    cooldown_key = _cooldown_key_for_action(action)
    in_flight_key = _in_flight_key_for_action(action)
    if cooldown_key is None or in_flight_key is None:
        return "rejected"
    if _cooldown_active(sess, cooldown_key, now=now):
        return _STATUS_COOLDOWN
    if sess.get(in_flight_key):
        return "in_flight"
    sess[in_flight_key] = True
    if hasattr(sess, "modified"):
        sess.modified = True
    return None


def release_send_slot(*, action: str, session_obj=None) -> None:
    sess = session_obj if session_obj is not None else session
    in_flight_key = _in_flight_key_for_action(action)
    if in_flight_key and in_flight_key in sess:
        sess.pop(in_flight_key, None)
        if hasattr(sess, "modified"):
            sess.modified = True


def _parse_session_ts(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.replace(tzinfo=None) if raw.tzinfo else raw
    if isinstance(raw, (int, float)):
        try:
            return datetime.utcfromtimestamp(float(raw))
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None


def _cooldown_active(session_obj, key: str, *, now: datetime | None = None) -> bool:
    ref = now if now is not None else utcnow_naive()
    last = _parse_session_ts(session_obj.get(key))
    if last is None:
        return False
    return (ref - last) < timedelta(seconds=SEND_COOLDOWN_SECONDS)


def _mark_send_timestamp(session_obj, key: str, *, now: datetime | None = None) -> None:
    ref = now if now is not None else utcnow_naive()
    session_obj[key] = ref.isoformat(timespec="seconds")
    if hasattr(session_obj, "modified"):
        session_obj.modified = True


def _first_write(field_name: str, test_run: DesktopAccessE2ETestRun, *, now: datetime | None = None) -> bool:
    if getattr(test_run, field_name) is not None:
        return False
    setattr(test_run, field_name, now if now is not None else utcnow_naive())
    return True


def mark_cta_clicked(test_run: DesktopAccessE2ETestRun, *, now: datetime | None = None) -> None:
    if _first_write("cta_clicked_at", test_run, now=now):
        db.session.commit()


def mark_registration_completed(test_run: DesktopAccessE2ETestRun, *, now: datetime | None = None) -> None:
    if _first_write("registration_completed_at", test_run, now=now):
        db.session.commit()


def mark_opt_out(test_run: DesktopAccessE2ETestRun, *, now: datetime | None = None) -> None:
    if _first_write("opt_out_at", test_run, now=now):
        db.session.commit()


def mark_activation_opt_out(
    test_run: DesktopAccessE2ETestRun, *, now: datetime | None = None
) -> None:
    if _first_write("activation_opt_out_at", test_run, now=now):
        db.session.commit()


def mark_first_use_seen(test_run: DesktopAccessE2ETestRun, *, now: datetime | None = None) -> None:
    if _first_write("first_use_seen_at", test_run, now=now):
        db.session.commit()


def mark_first_audit_seen(test_run: DesktopAccessE2ETestRun, *, now: datetime | None = None) -> None:
    changed = _first_write("first_audit_seen_at", test_run, now=now)
    completed = _first_write("completed_at", test_run, now=now)
    if changed or completed:
        db.session.commit()


def mark_first_use_for_current_session(*, now: datetime | None = None) -> bool:
    ctx = get_test_mode_context(now=now)
    if ctx is None:
        return False
    run = find_test_run_by_run_id(ctx["run_id"])
    if run is None:
        return False
    mark_first_use_seen(run, now=now)
    return True


def mark_first_audit_for_current_session(*, now: datetime | None = None) -> bool:
    ctx = get_test_mode_context(now=now)
    if ctx is None:
        return False
    run = find_test_run_by_run_id(ctx["run_id"])
    if run is None:
        return False
    mark_first_audit_seen(run, now=now)
    return True


def should_send_e2e_followup(test_run: DesktopAccessE2ETestRun, *, now=None) -> bool:
    if test_run.initial_email_sent_at is None:
        return False
    if test_run.followup_sent_at is not None:
        return False
    if test_run.opt_out_at is not None:
        return False
    # MAX_FOLLOWUPS do fluxo real: no máximo 1 follow-up por run.
    if MAX_FOLLOWUPS < 1:
        return False
    eligible = followup_eligible_at(test_run)
    if eligible is None:
        return False
    ref = now if now is not None else utcnow_naive()
    return ref >= eligible


def start_e2e_test_run(
    *,
    admin_user: Any,
    email: str,
    secret_key: str,
    build_cta_url: Callable[[str], str],
    build_unsubscribe_url: Callable[[str], str],
    session_obj=None,
    app_env: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Cria novo DesktopAccessE2ETestRun e envia o MESMO e-mail CTA inicial real.
    Não cria Lead. Não altera Lead existente.
    """
    sess = session_obj if session_obj is not None else session
    env = app_env if app_env is not None else resolve_app_env()
    if not is_admin_test_env_allowed(env):
        logger.warning(
            "desktop_access_e2e_start_blocked env=%s user_id=%s",
            env,
            getattr(admin_user, "id", None),
        )
        return {"status": _STATUS_REJECTED, "reason": "env_not_allowed"}

    ok, message, user = validate_admin_test_email(admin_user=admin_user, email=email)
    if not ok or user is None:
        return {"status": _STATUS_REJECTED, "reason": "email_mismatch", "message": message}

    slot = claim_send_slot(action=ACTION_START_E2E, session_obj=sess, now=now)
    if slot == _STATUS_COOLDOWN:
        return {"status": _STATUS_COOLDOWN, "reason": "cooldown"}
    if slot is not None:
        return {"status": _STATUS_REJECTED, "reason": slot}

    run_id = new_test_run_id()
    sent_at = now if now is not None else utcnow_naive()
    test_run = DesktopAccessE2ETestRun(
        run_id=run_id,
        user_id=int(user.id),
        created_at=sent_at,
    )
    db.session.add(test_run)
    try:
        db.session.flush()
        cta_token = generate_e2e_cta_token(
            run_id=run_id,
            user_id=int(user.id),
            secret_key=secret_key,
        )
        unsub_token = generate_e2e_unsubscribe_token(
            run_id=run_id,
            user_id=int(user.id),
            secret_key=secret_key,
        )
        cta_url = build_cta_url(cta_token)
        unsubscribe_url = build_unsubscribe_url(unsub_token)
        built = build_initial_cta_email(cta_url=cta_url, unsubscribe_url=unsubscribe_url)
        send_email(
            to_email=user.email,
            subject=built["subject"],
            html=built["html"],
            text=built["text"],
        )
        test_run.initial_email_sent_at = sent_at
        db.session.commit()
    except Exception:
        db.session.rollback()
        release_send_slot(action=ACTION_START_E2E, session_obj=sess)
        logger.exception(
            "desktop_access_e2e_start_failed user_id=%s run_id=%s env=%s",
            getattr(admin_user, "id", None),
            run_id,
            env,
        )
        return {"status": _STATUS_FAILED, "reason": "send_failed", "run_id": run_id}

    _mark_send_timestamp(sess, SESSION_START_LAST_SENT_KEY, now=sent_at)
    release_send_slot(action=ACTION_START_E2E, session_obj=sess)
    logger.info(
        "desktop_access_e2e_started user_id=%s run_id=%s env=%s action=%s",
        int(user.id),
        run_id,
        env,
        ACTION_START_E2E,
    )
    return {"status": _STATUS_SENT, "run_id": run_id, "user_id": int(user.id)}


def maybe_send_e2e_followup_email(
    test_run: DesktopAccessE2ETestRun,
    *,
    secret_key: str,
    build_cta_url: Callable[[str], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
    app_env: str | None = None,
) -> str:
    """
    Envia follow-up E2E com o MESMO builder real, respeitando FOLLOWUP_DELAY_HOURS.
    """
    env = app_env if app_env is not None else resolve_app_env()
    if not is_admin_test_env_allowed(env):
        return _STATUS_REJECTED

    if not should_send_e2e_followup(test_run, now=now):
        if test_run.opt_out_at is not None:
            return "skipped_opt_out"
        if test_run.followup_sent_at is not None:
            return _STATUS_SKIPPED
        return _STATUS_NOT_ELIGIBLE

    user = find_user_by_id(int(test_run.user_id))
    if user is None or not target_user_still_admin(user):
        return _STATUS_REJECTED

    try:
        cta_token = generate_e2e_cta_token(
            run_id=test_run.run_id,
            user_id=int(user.id),
            secret_key=secret_key,
        )
        unsub_token = generate_e2e_unsubscribe_token(
            run_id=test_run.run_id,
            user_id=int(user.id),
            secret_key=secret_key,
        )
        built = build_followup_email(
            cta_url=build_cta_url(cta_token),
            unsubscribe_url=build_unsubscribe_url(unsub_token),
        )
        send_email(
            to_email=user.email,
            subject=built["subject"],
            html=built["html"],
            text=built["text"],
        )
    except Exception:
        logger.exception(
            "desktop_access_e2e_followup_failed user_id=%s run_id=%s env=%s",
            test_run.user_id,
            test_run.run_id,
            env,
        )
        return _STATUS_FAILED

    sent_at = now if now is not None else utcnow_naive()
    test_run.followup_sent_at = sent_at
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "desktop_access_e2e_followup_sent_but_persist_failed run_id=%s",
            test_run.run_id,
        )
        return _STATUS_FAILED

    logger.info(
        "desktop_access_e2e_followup_sent user_id=%s run_id=%s env=%s",
        test_run.user_id,
        test_run.run_id,
        env,
    )
    return _STATUS_SENT


def check_and_maybe_send_latest_followup(
    *,
    admin_user: Any,
    secret_key: str,
    build_cta_url: Callable[[str], str],
    build_unsubscribe_url: Callable[[str], str],
    session_obj=None,
    app_env: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Ação admin 'Verificar follow-up agora': usa a mesma elegibilidade temporal.
    NÃO força envio antecipado.
    """
    sess = session_obj if session_obj is not None else session
    env = app_env if app_env is not None else resolve_app_env()
    if not is_admin_test_env_allowed(env):
        return {"status": _STATUS_REJECTED, "reason": "env_not_allowed"}

    user_id = getattr(admin_user, "id", None)
    if user_id is None or not target_user_still_admin(admin_user):
        return {"status": _STATUS_REJECTED, "reason": "not_admin"}

    slot = claim_send_slot(action=ACTION_CHECK_FOLLOWUP, session_obj=sess, now=now)
    if slot == _STATUS_COOLDOWN:
        return {"status": _STATUS_COOLDOWN, "reason": "cooldown"}
    if slot is not None:
        return {"status": _STATUS_REJECTED, "reason": slot}

    try:
        run = get_latest_test_run_for_user(int(user_id))
        if run is None:
            return {"status": _STATUS_REJECTED, "reason": "no_run", "message": "Nenhum teste E2E encontrado."}

        eligible_at = followup_eligible_at(run)
        if not should_send_e2e_followup(run, now=now):
            return {
                "status": _STATUS_NOT_ELIGIBLE,
                "reason": "not_eligible",
                "run_id": run.run_id,
                "followup_eligible_at": eligible_at,
                "message": (
                    f"Follow-up ainda não elegível. Previsto para "
                    f"{format_admin_ts(eligible_at)}."
                    if eligible_at is not None
                    else "Follow-up ainda não elegível."
                ),
            }

        status = maybe_send_e2e_followup_email(
            run,
            secret_key=secret_key,
            build_cta_url=build_cta_url,
            build_unsubscribe_url=build_unsubscribe_url,
            now=now,
            app_env=env,
        )
        if status == _STATUS_SENT:
            _mark_send_timestamp(sess, SESSION_CHECK_FOLLOWUP_LAST_KEY, now=now)
            return {"status": _STATUS_SENT, "run_id": run.run_id}
        if status == "skipped_opt_out":
            return {
                "status": _STATUS_SKIPPED,
                "reason": "opt_out",
                "run_id": run.run_id,
                "message": "Follow-up não enviado: unsubscribe deste run.",
            }
        return {
            "status": status,
            "run_id": run.run_id,
            "followup_eligible_at": eligible_at,
            "message": f"Follow-up não enviado ({status}).",
        }
    finally:
        release_send_slot(action=ACTION_CHECK_FOLLOWUP, session_obj=sess)


def list_e2e_followup_candidates(*, now=None) -> list[DesktopAccessE2ETestRun]:
    ref = now if now is not None else utcnow_naive()
    threshold = ref - timedelta(hours=FOLLOWUP_DELAY_HOURS)
    return (
        DesktopAccessE2ETestRun.query.filter(
            DesktopAccessE2ETestRun.initial_email_sent_at.isnot(None),
            DesktopAccessE2ETestRun.followup_sent_at.is_(None),
            DesktopAccessE2ETestRun.opt_out_at.is_(None),
            DesktopAccessE2ETestRun.initial_email_sent_at <= threshold,
        )
        .order_by(DesktopAccessE2ETestRun.id.asc())
        .all()
    )


def process_eligible_e2e_followups(
    *,
    secret_key: str,
    build_cta_url: Callable[[str], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
    app_env: str | None = None,
) -> dict[str, Any]:
    env = app_env if app_env is not None else resolve_app_env()
    stats = {
        "candidates": 0,
        "sent": 0,
        "skipped": 0,
        "skipped_opt_out": 0,
        "failed": 0,
        "rejected": 0,
    }
    if not is_admin_test_env_allowed(env):
        return stats

    candidates = list_e2e_followup_candidates(now=now)
    stats["candidates"] = len(candidates)
    for test_run in candidates:
        try:
            status = maybe_send_e2e_followup_email(
                test_run,
                secret_key=secret_key,
                build_cta_url=build_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
                now=now,
                app_env=env,
            )
        except Exception:
            db.session.rollback()
            logger.exception(
                "desktop_access_e2e_followup_batch_error run_id=%s",
                getattr(test_run, "run_id", None),
            )
            stats["failed"] += 1
            continue

        if status == _STATUS_SENT:
            stats["sent"] += 1
        elif status == "skipped_opt_out":
            stats["skipped_opt_out"] += 1
        elif status == _STATUS_FAILED:
            stats["failed"] += 1
        elif status == _STATUS_REJECTED:
            stats["rejected"] += 1
        else:
            stats["skipped"] += 1

    logger.info(
        "desktop_access_e2e_followup_batch env=%s candidates=%s sent=%s skipped=%s "
        "skipped_opt_out=%s failed=%s rejected=%s",
        env,
        stats["candidates"],
        stats["sent"],
        stats["skipped"],
        stats["skipped_opt_out"],
        stats["failed"],
        stats["rejected"],
    )
    return stats


def activate_test_mode(
    *,
    user_id: int,
    run_id: str,
    session_obj=None,
    now: datetime | None = None,
    ttl_seconds: int = TEST_MODE_TTL_SECONDS,
    app_env: str | None = None,
) -> dict[str, Any] | None:
    if not is_admin_test_env_allowed(app_env):
        logger.warning(
            "desktop_access_e2e_activate_blocked env=%s user_id=%s",
            app_env if app_env is not None else resolve_app_env(),
            user_id,
        )
        return None
    run = find_test_run_by_run_id(run_id)
    if run is None or int(run.user_id) != int(user_id):
        return None
    sess = session_obj if session_obj is not None else session
    started = now if now is not None else utcnow_naive()
    expires = started + timedelta(seconds=int(ttl_seconds))
    ctx = {
        "run_id": str(run_id),
        "user_id": int(user_id),
        # Compat: leitores antigos usavam test_user_id
        "test_user_id": int(user_id),
        "started_at": started.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
    }
    sess[SESSION_TEST_MODE_KEY] = ctx
    if hasattr(sess, "modified"):
        sess.modified = True
    return ctx


def clear_test_mode(session_obj=None) -> None:
    sess = session_obj if session_obj is not None else session
    if SESSION_TEST_MODE_KEY in sess:
        sess.pop(SESSION_TEST_MODE_KEY, None)
        if hasattr(sess, "modified"):
            sess.modified = True


def get_test_mode_context(session_obj=None, *, now: datetime | None = None) -> dict[str, Any] | None:
    sess = session_obj if session_obj is not None else session
    raw = sess.get(SESSION_TEST_MODE_KEY)
    if not isinstance(raw, dict):
        return None
    user_id = raw.get("user_id", raw.get("test_user_id"))
    run_id = raw.get("run_id")
    expires_at = _parse_session_ts(raw.get("expires_at"))
    if not isinstance(user_id, int) or not isinstance(run_id, str) or expires_at is None:
        clear_test_mode(sess)
        return None
    ref = now if now is not None else utcnow_naive()
    if ref >= expires_at:
        clear_test_mode(sess)
        return None
    return {
        "run_id": run_id,
        "user_id": int(user_id),
        "test_user_id": int(user_id),
        "started_at": raw.get("started_at"),
        "expires_at": raw.get("expires_at"),
    }


def is_desktop_access_admin_test_mode_for_user(
    user_id: int | None,
    session_obj=None,
    *,
    now: datetime | None = None,
) -> bool:
    if user_id is None:
        return False
    ctx = get_test_mode_context(session_obj, now=now)
    if ctx is None:
        return False
    return int(ctx["user_id"]) == int(user_id)


def is_desktop_access_admin_test_mode_for_current_user(
    session_obj=None,
    *,
    now: datetime | None = None,
) -> bool:
    if not has_request_context():
        return False
    if not getattr(current_user, "is_authenticated", False):
        return False
    user_id = getattr(current_user, "id", None)
    return is_desktop_access_admin_test_mode_for_user(user_id, session_obj, now=now)


def complete_test_mode_after_successful_audit(session_obj=None) -> bool:
    """Marca first_audit no run e limpa apenas o contexto de sessão."""
    if not is_desktop_access_admin_test_mode_for_current_user(session_obj):
        return False
    mark_first_audit_for_current_session()
    clear_test_mode(session_obj)
    return True


def try_registration_replay(
    *,
    full_name: str,
    email: str,
    password: str,
    accept_terms: bool,
    session_obj=None,
) -> dict[str, Any] | None:
    """
    Branch invisível de Registration replay para E2E.

    Retorna None se não houver contexto E2E ativo (fluxo normal deve seguir).
    Caso contrário retorna dict com status/mensagem.
    """
    if not is_admin_test_env_allowed():
        return None
    ctx = get_test_mode_context(session_obj)
    if ctx is None:
        return None

    run = find_test_run_by_run_id(ctx["run_id"])
    if run is None:
        return {
            "ok": False,
            "message": "Contexto de teste E2E inválido.",
        }

    user = get_authorized_admin_test_user(int(run.user_id))
    if user is None or int(user.id) != int(ctx["user_id"]):
        return {
            "ok": False,
            "message": "Contexto de teste E2E inválido.",
        }

    name = (full_name or "").strip()
    requested_email = normalize_email(email)
    pwd = (password or "").strip()
    if not accept_terms:
        return {
            "ok": False,
            "message": "É obrigatório aceitar os Termos de Uso para criar sua conta.",
        }
    if not name or not requested_email or not pwd:
        return {
            "ok": False,
            "message": "Por favor, preencha nome, e-mail e senha.",
        }

    if requested_email != normalize_email(user.email):
        return {
            "ok": False,
            "message": "Durante o teste E2E use o mesmo e-mail da execução.",
            "email_mismatch": True,
        }

    # Senha apenas valida experiência do formulário (não compara / não altera).
    # Termos validados acima; não altera accepted_terms_at histórico.
    try:
        mark_registration_completed(run)
    except Exception:
        db.session.rollback()
        logger.exception(
            "desktop_access_e2e_registration_persist_failed run_id=%s user_id=%s",
            run.run_id,
            user.id,
        )
        return {"ok": False, "message": "Erro ao registrar etapa do teste E2E."}

    logger.info(
        "desktop_access_e2e_registration_replay user_id=%s run_id=%s",
        user.id,
        run.run_id,
    )
    return {
        "ok": True,
        "user": user,
        "run_id": run.run_id,
        "message": "Conta criada com sucesso! Faça login.",
        "suppress_complete_registration": True,
    }


def _e2e_activation_anchor(test_run: DesktopAccessE2ETestRun) -> datetime | None:
    return test_run.activation_sequence_started_at or test_run.registration_completed_at


def e2e_activation_email_1_eligible_at(
    test_run: DesktopAccessE2ETestRun,
) -> datetime | None:
    from app.services.desktop_access_activation_email_service import (
        ACTIVATION_EMAIL_1_DELAY_HOURS,
    )

    anchor = _e2e_activation_anchor(test_run)
    if anchor is None:
        return None
    return anchor + timedelta(hours=ACTIVATION_EMAIL_1_DELAY_HOURS)


def e2e_activation_email_2_eligible_at(
    test_run: DesktopAccessE2ETestRun,
) -> datetime | None:
    from app.services.desktop_access_activation_email_service import (
        ACTIVATION_EMAIL_2_DELAY_HOURS,
    )

    if test_run.activation_email_1_sent_at is None:
        return None
    return test_run.activation_email_1_sent_at + timedelta(
        hours=ACTIVATION_EMAIL_2_DELAY_HOURS
    )


def should_send_e2e_activation_email_1(
    test_run: DesktopAccessE2ETestRun, *, now=None
) -> bool:
    if _e2e_activation_anchor(test_run) is None:
        return False
    if test_run.activation_email_1_sent_at is not None:
        return False
    if test_run.activation_opt_out_at is not None:
        return False
    if test_run.first_use_seen_at is not None:
        return False
    eligible = e2e_activation_email_1_eligible_at(test_run)
    if eligible is None:
        return False
    ref = now if now is not None else utcnow_naive()
    return ref >= eligible


def should_send_e2e_activation_email_2(
    test_run: DesktopAccessE2ETestRun, *, now=None
) -> bool:
    if test_run.activation_email_1_sent_at is None:
        return False
    if test_run.activation_email_2_sent_at is not None:
        return False
    if test_run.activation_opt_out_at is not None:
        return False
    if test_run.first_use_seen_at is not None:
        return False
    eligible = e2e_activation_email_2_eligible_at(test_run)
    if eligible is None:
        return False
    ref = now if now is not None else utcnow_naive()
    return ref >= eligible


def _send_e2e_activation_built(
    *,
    test_run: DesktopAccessE2ETestRun,
    user: User,
    secret_key: str,
    which: str,
    build_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
) -> None:
    from app.services.desktop_access_activation_email_service import (
        build_activation_email_1,
        build_activation_email_2,
    )

    unsub_token = generate_e2e_activation_unsubscribe_token(
        run_id=test_run.run_id,
        user_id=int(user.id),
        secret_key=secret_key,
    )
    unsubscribe_url = build_unsubscribe_url(unsub_token)
    cta_url = build_cta_url()
    if which == "email1":
        built = build_activation_email_1(
            cta_url=cta_url, unsubscribe_url=unsubscribe_url
        )
    else:
        built = build_activation_email_2(
            cta_url=cta_url, unsubscribe_url=unsubscribe_url
        )
    send_email(
        to_email=user.email,
        subject=built["subject"],
        html=built["html"],
        text=built["text"],
        attachments=built.get("attachments") or None,
        headers={
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )


def inspect_send_activation_email(
    *,
    admin_user: Any,
    which: str,
    secret_key: str,
    build_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
    session_obj=None,
    app_env: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Envio para inspeção de conteúdo (E-mail 1 ou 2).
    NÃO valida cadência 24h/48h. Peça REAL idêntica à produção.
    """
    sess = session_obj if session_obj is not None else session
    env = app_env if app_env is not None else resolve_app_env()
    if not is_admin_test_env_allowed(env):
        return {"status": _STATUS_REJECTED, "reason": "env_not_allowed"}

    action = (
        ACTION_INSPECT_ACTIVATION_EMAIL_1
        if which == "email1"
        else ACTION_INSPECT_ACTIVATION_EMAIL_2
    )
    user_id = getattr(admin_user, "id", None)
    if user_id is None or not target_user_still_admin(admin_user):
        return {"status": _STATUS_REJECTED, "reason": "not_admin"}

    slot = claim_send_slot(action=action, session_obj=sess, now=now)
    if slot == _STATUS_COOLDOWN:
        return {"status": _STATUS_COOLDOWN, "reason": "cooldown"}
    if slot is not None:
        return {"status": _STATUS_REJECTED, "reason": slot}

    try:
        run = get_latest_test_run_for_user(int(user_id))
        if run is None:
            return {
                "status": _STATUS_REJECTED,
                "reason": "no_run",
                "message": "Nenhum teste E2E encontrado. Inicie um teste primeiro.",
            }
        user = find_user_by_id(int(run.user_id))
        if user is None:
            return {"status": _STATUS_REJECTED, "reason": "user_missing"}
        _send_e2e_activation_built(
            test_run=run,
            user=user,
            secret_key=secret_key,
            which=which,
            build_cta_url=build_cta_url,
            build_unsubscribe_url=build_unsubscribe_url,
        )
        _mark_send_timestamp(sess, SESSION_ACTIVATION_INSPECT_LAST_KEY, now=now)
        logger.info(
            "desktop_access_e2e_activation_inspect which=%s user_id=%s run_id=%s",
            which,
            user.id,
            run.run_id,
        )
        return {"status": _STATUS_SENT, "run_id": run.run_id, "which": which}
    except Exception:
        logger.exception(
            "desktop_access_e2e_activation_inspect_failed which=%s user_id=%s",
            which,
            user_id,
        )
        return {"status": _STATUS_FAILED, "reason": "send_failed"}
    finally:
        release_send_slot(action=action, session_obj=sess)


def start_activation_timed_sequence(
    *,
    admin_user: Any,
    secret_key: str,
    session_obj=None,
    app_env: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """
    Inicia sequência E2E temporizada (cadência real 24h + 48h).
    Usa registration_completed_at / activation_sequence_started_at como âncora.
    """
    sess = session_obj if session_obj is not None else session
    env = app_env if app_env is not None else resolve_app_env()
    if not is_admin_test_env_allowed(env):
        return {"status": _STATUS_REJECTED, "reason": "env_not_allowed"}

    user_id = getattr(admin_user, "id", None)
    if user_id is None or not target_user_still_admin(admin_user):
        return {"status": _STATUS_REJECTED, "reason": "not_admin"}

    slot = claim_send_slot(
        action=ACTION_START_ACTIVATION_TIMED, session_obj=sess, now=now
    )
    if slot == _STATUS_COOLDOWN:
        return {"status": _STATUS_COOLDOWN, "reason": "cooldown"}
    if slot is not None:
        return {"status": _STATUS_REJECTED, "reason": slot}

    try:
        run = get_latest_test_run_for_user(int(user_id))
        if run is None:
            return {
                "status": _STATUS_REJECTED,
                "reason": "no_run",
                "message": "Nenhum teste E2E encontrado.",
            }
        started = now if now is not None else utcnow_naive()
        if run.registration_completed_at is None:
            run.registration_completed_at = started
        if run.activation_sequence_started_at is None:
            run.activation_sequence_started_at = started
        # Reinicia timestamps da sequência para novo ciclo temporizado.
        run.activation_email_1_sent_at = None
        run.activation_email_2_sent_at = None
        run.activation_opt_out_at = None
        run.first_use_seen_at = None
        db.session.commit()
        _mark_send_timestamp(sess, SESSION_ACTIVATION_TIMED_LAST_KEY, now=started)
        logger.info(
            "desktop_access_e2e_activation_timed_started user_id=%s run_id=%s",
            user_id,
            run.run_id,
        )
        return {
            "status": _STATUS_SENT,
            "run_id": run.run_id,
            "activation_sequence_started_at": run.activation_sequence_started_at,
            "activation_email_1_eligible_at": e2e_activation_email_1_eligible_at(run),
        }
    except Exception:
        db.session.rollback()
        logger.exception(
            "desktop_access_e2e_activation_timed_failed user_id=%s", user_id
        )
        return {"status": _STATUS_FAILED, "reason": "persist_failed"}
    finally:
        release_send_slot(action=ACTION_START_ACTIVATION_TIMED, session_obj=sess)


def maybe_send_e2e_activation_email_1(
    test_run: DesktopAccessE2ETestRun,
    *,
    secret_key: str,
    build_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
    app_env: str | None = None,
) -> str:
    env = app_env if app_env is not None else resolve_app_env()
    if not is_admin_test_env_allowed(env):
        return _STATUS_REJECTED
    if not should_send_e2e_activation_email_1(test_run, now=now):
        if test_run.activation_opt_out_at is not None:
            return "skipped_opt_out"
        if test_run.first_use_seen_at is not None:
            return "skipped_upload"
        return _STATUS_NOT_ELIGIBLE

    user = find_user_by_id(int(test_run.user_id))
    if user is None or not target_user_still_admin(user):
        return _STATUS_REJECTED

    # Recheck imediato
    db.session.refresh(test_run)
    if not should_send_e2e_activation_email_1(test_run, now=now):
        return _STATUS_SKIPPED

    try:
        _send_e2e_activation_built(
            test_run=test_run,
            user=user,
            secret_key=secret_key,
            which="email1",
            build_cta_url=build_cta_url,
            build_unsubscribe_url=build_unsubscribe_url,
        )
    except Exception:
        logger.exception(
            "desktop_access_e2e_activation_email1_failed run_id=%s", test_run.run_id
        )
        return _STATUS_FAILED

    sent_at = now if now is not None else utcnow_naive()
    test_run.activation_email_1_sent_at = sent_at
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "desktop_access_e2e_activation_email1_persist_failed run_id=%s",
            test_run.run_id,
        )
        return _STATUS_FAILED
    return _STATUS_SENT


def maybe_send_e2e_activation_email_2(
    test_run: DesktopAccessE2ETestRun,
    *,
    secret_key: str,
    build_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
    app_env: str | None = None,
) -> str:
    env = app_env if app_env is not None else resolve_app_env()
    if not is_admin_test_env_allowed(env):
        return _STATUS_REJECTED
    if not should_send_e2e_activation_email_2(test_run, now=now):
        if test_run.activation_opt_out_at is not None:
            return "skipped_opt_out"
        if test_run.first_use_seen_at is not None:
            return "skipped_upload"
        return _STATUS_NOT_ELIGIBLE

    user = find_user_by_id(int(test_run.user_id))
    if user is None or not target_user_still_admin(user):
        return _STATUS_REJECTED

    db.session.refresh(test_run)
    if not should_send_e2e_activation_email_2(test_run, now=now):
        return _STATUS_SKIPPED

    try:
        _send_e2e_activation_built(
            test_run=test_run,
            user=user,
            secret_key=secret_key,
            which="email2",
            build_cta_url=build_cta_url,
            build_unsubscribe_url=build_unsubscribe_url,
        )
    except Exception:
        logger.exception(
            "desktop_access_e2e_activation_email2_failed run_id=%s", test_run.run_id
        )
        return _STATUS_FAILED

    sent_at = now if now is not None else utcnow_naive()
    test_run.activation_email_2_sent_at = sent_at
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(
            "desktop_access_e2e_activation_email2_persist_failed run_id=%s",
            test_run.run_id,
        )
        return _STATUS_FAILED
    return _STATUS_SENT


def check_and_maybe_send_latest_activation(
    *,
    admin_user: Any,
    secret_key: str,
    build_email_1_cta_url: Callable[[], str],
    build_email_2_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
    session_obj=None,
    app_env: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verifica e envia E1/E2 elegíveis pela cadência real (sem acelerar)."""
    sess = session_obj if session_obj is not None else session
    env = app_env if app_env is not None else resolve_app_env()
    if not is_admin_test_env_allowed(env):
        return {"status": _STATUS_REJECTED, "reason": "env_not_allowed"}

    user_id = getattr(admin_user, "id", None)
    if user_id is None or not target_user_still_admin(admin_user):
        return {"status": _STATUS_REJECTED, "reason": "not_admin"}

    slot = claim_send_slot(action=ACTION_CHECK_ACTIVATION, session_obj=sess, now=now)
    if slot == _STATUS_COOLDOWN:
        return {"status": _STATUS_COOLDOWN, "reason": "cooldown"}
    if slot is not None:
        return {"status": _STATUS_REJECTED, "reason": slot}

    try:
        run = get_latest_test_run_for_user(int(user_id))
        if run is None:
            return {
                "status": _STATUS_REJECTED,
                "reason": "no_run",
                "message": "Nenhum teste E2E encontrado.",
            }

        sent_which = None
        if should_send_e2e_activation_email_1(run, now=now):
            status = maybe_send_e2e_activation_email_1(
                run,
                secret_key=secret_key,
                build_cta_url=build_email_1_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
                now=now,
                app_env=env,
            )
            if status == _STATUS_SENT:
                sent_which = "email1"
                _mark_send_timestamp(sess, SESSION_ACTIVATION_TIMED_LAST_KEY, now=now)
                return {"status": _STATUS_SENT, "run_id": run.run_id, "which": sent_which}

        if should_send_e2e_activation_email_2(run, now=now):
            status = maybe_send_e2e_activation_email_2(
                run,
                secret_key=secret_key,
                build_cta_url=build_email_2_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
                now=now,
                app_env=env,
            )
            if status == _STATUS_SENT:
                sent_which = "email2"
                _mark_send_timestamp(sess, SESSION_ACTIVATION_TIMED_LAST_KEY, now=now)
                return {"status": _STATUS_SENT, "run_id": run.run_id, "which": sent_which}

        e1 = e2e_activation_email_1_eligible_at(run)
        e2 = e2e_activation_email_2_eligible_at(run)
        return {
            "status": _STATUS_NOT_ELIGIBLE,
            "reason": "not_eligible",
            "run_id": run.run_id,
            "activation_email_1_eligible_at": e1,
            "activation_email_2_eligible_at": e2,
            "message": (
                "Ativação ainda não elegível. "
                f"E1: {format_admin_ts(e1)} · E2: {format_admin_ts(e2)}"
            ),
        }
    finally:
        release_send_slot(action=ACTION_CHECK_ACTIVATION, session_obj=sess)


def process_eligible_e2e_activation_emails(
    *,
    secret_key: str,
    build_email_1_cta_url: Callable[[], str],
    build_email_2_cta_url: Callable[[], str],
    build_unsubscribe_url: Callable[[str], str],
    now=None,
    app_env: str | None = None,
) -> dict[str, Any]:
    from app.services.desktop_access_activation_email_service import (
        ACTIVATION_EMAIL_1_DELAY_HOURS,
        ACTIVATION_EMAIL_2_DELAY_HOURS,
    )

    env = app_env if app_env is not None else resolve_app_env()
    stats = {
        "examined": 0,
        "email1_candidates": 0,
        "email1_sent": 0,
        "email2_candidates": 0,
        "email2_sent": 0,
        "suppressed_upload": 0,
        "suppressed_opt_out": 0,
        "failures": 0,
    }
    if not is_admin_test_env_allowed(env):
        return stats

    ref = now if now is not None else utcnow_naive()
    e1_threshold = ref - timedelta(hours=ACTIVATION_EMAIL_1_DELAY_HOURS)
    e2_threshold = ref - timedelta(hours=ACTIVATION_EMAIL_2_DELAY_HOURS)

    email1_runs = (
        DesktopAccessE2ETestRun.query.filter(
            DesktopAccessE2ETestRun.activation_email_1_sent_at.is_(None),
            DesktopAccessE2ETestRun.activation_opt_out_at.is_(None),
            DesktopAccessE2ETestRun.first_use_seen_at.is_(None),
            or_(
                and_(
                    DesktopAccessE2ETestRun.activation_sequence_started_at.isnot(None),
                    DesktopAccessE2ETestRun.activation_sequence_started_at <= e1_threshold,
                ),
                and_(
                    DesktopAccessE2ETestRun.activation_sequence_started_at.is_(None),
                    DesktopAccessE2ETestRun.registration_completed_at.isnot(None),
                    DesktopAccessE2ETestRun.registration_completed_at <= e1_threshold,
                ),
            ),
        )
        .order_by(DesktopAccessE2ETestRun.id.asc())
        .all()
    )
    stats["email1_candidates"] = len(email1_runs)
    stats["examined"] += len(email1_runs)
    for run in email1_runs:
        try:
            status = maybe_send_e2e_activation_email_1(
                run,
                secret_key=secret_key,
                build_cta_url=build_email_1_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
                now=now,
                app_env=env,
            )
        except Exception:
            db.session.rollback()
            stats["failures"] += 1
            continue
        if status == _STATUS_SENT:
            stats["email1_sent"] += 1
        elif status == "skipped_upload":
            stats["suppressed_upload"] += 1
        elif status == "skipped_opt_out":
            stats["suppressed_opt_out"] += 1
        elif status == _STATUS_FAILED:
            stats["failures"] += 1

    email2_runs = (
        DesktopAccessE2ETestRun.query.filter(
            DesktopAccessE2ETestRun.activation_email_1_sent_at.isnot(None),
            DesktopAccessE2ETestRun.activation_email_2_sent_at.is_(None),
            DesktopAccessE2ETestRun.activation_opt_out_at.is_(None),
            DesktopAccessE2ETestRun.first_use_seen_at.is_(None),
            DesktopAccessE2ETestRun.activation_email_1_sent_at <= e2_threshold,
        )
        .order_by(DesktopAccessE2ETestRun.id.asc())
        .all()
    )
    stats["email2_candidates"] = len(email2_runs)
    stats["examined"] += len(email2_runs)
    for run in email2_runs:
        try:
            status = maybe_send_e2e_activation_email_2(
                run,
                secret_key=secret_key,
                build_cta_url=build_email_2_cta_url,
                build_unsubscribe_url=build_unsubscribe_url,
                now=now,
                app_env=env,
            )
        except Exception:
            db.session.rollback()
            stats["failures"] += 1
            continue
        if status == _STATUS_SENT:
            stats["email2_sent"] += 1
        elif status == "skipped_upload":
            stats["suppressed_upload"] += 1
        elif status == "skipped_opt_out":
            stats["suppressed_opt_out"] += 1
        elif status == _STATUS_FAILED:
            stats["failures"] += 1

    logger.info(
        "desktop_access_e2e_activation_batch env=%s examined=%s email1_sent=%s "
        "email2_sent=%s failures=%s",
        env,
        stats["examined"],
        stats["email1_sent"],
        stats["email2_sent"],
        stats["failures"],
    )
    return stats
