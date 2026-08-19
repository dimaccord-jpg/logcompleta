"""
Exercício controlado de direitos de privacidade/LGPD de um User.

Não é encerramento contratual, delete account, opt-out nem suppression.
Não é self-service: execução administrativa, dry-run por default.

Distinções explícitas:
- processar exercício de privacidade != encerrar vínculo operacional
- desidentificar User != apagar User / Conta / Franquia / billing
- encerrar jornada de ativação != opt-out / CommunicationSuppression
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.extensions import db
from app.models import User
from app.services.user_lifecycle_service import (
    NOME_OPERACIONAL_APOS_ENCERRAMENTO,
    _desidentificar_usuario,
    _encerrar_jornadas_ativacao_associadas,
    _jornada_ativacao_ja_encerrada_para_usuario,
    _listar_leads_jornada_ativacao,
    _normalize_email,
    email_operacional_apos_encerramento,
)

logger = logging.getLogger(__name__)

MODE_DRY_RUN = "DRY_RUN"
MODE_APPLY = "APPLY"

STATUS_OK = "OK"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_ERROR = "ERROR"

SESSION_ACCESS_REVOCATION_ENFORCED = "enforced_on_next_request"
GLOBAL_SESSION_STORAGE_PURGE_UNSUPPORTED = "unsupported"

USER_FIELDS_DEIDENTIFIED = (
    "email",
    "full_name",
    "password_hash",
    "oauth_provider",
    "oauth_sub",
    "subscribes_to_newsletter",
    "job_role",
    "usage_purpose",
)

USER_FIELDS_PRESERVED = (
    "id",
    "is_admin",
    "categoria",
    "creditos",
    "created_at",
    "last_login_at",
    "accepted_terms_at",
    "first_audit_completed_at",
    "trial_start_date",
    "conta_id",
    "franquia_id",
)

PRESERVED_CATEGORIES = (
    "conta",
    "franquia",
    "billing",
    "funnel_event",
    "processing_event",
    "ia_consumo_evento",
    "lead",
    "communication_suppression",
    "agente_compara_ttl",
)

UNSUPPORTED_CLEANUP_CATEGORIES = (
    "current_session_cleanup",
    "global_user_temp_cleanup",
    "global_session_storage_purge",
)

DEIDENTIFIED_CATEGORIES = ("user_profile",)


class PrivacyRightsAborted(RuntimeError):
    """Erro operacional após rollback. Não representa User ausente."""

    def __init__(self, error_type: str) -> None:
        self.error_type = error_type
        super().__init__(error_type)


@dataclass(frozen=True)
class PrivacyRightsResult:
    """Relatório técnico sem PII do exercício de privacidade."""

    mode: str
    status: str
    user_id: int | None
    user_deidentified: bool
    activation_journeys_ended: int
    user_fields_altered: tuple[str, ...]
    user_fields_preserved: tuple[str, ...]
    preserved_categories: tuple[str, ...]
    unsupported_cleanup_categories: tuple[str, ...]
    current_session_cleanup_supported: bool = False
    global_user_temp_cleanup_supported: bool = False
    session_access_revocation: str = SESSION_ACCESS_REVOCATION_ENFORCED
    global_session_storage_purge: str = GLOBAL_SESSION_STORAGE_PURGE_UNSUPPORTED
    error_type: str | None = None


def processar_exercicio_privacidade_usuario(
    user: User | None,
    *,
    apply: bool = False,
) -> PrivacyRightsResult:
    """
    Processa pedido individual de privacidade/LGPD.

    apply=False (default): calcula ações, não modifica User/Lead, não commita.
    apply=True: desidentifica o User e encerra jornadas de ativação na mesma
    transação, com um único commit.

    Não apaga User, Conta, Franquia, billing, eventos, Lead nem suppression.
    """
    mode = MODE_APPLY if apply else MODE_DRY_RUN
    if not isinstance(user, User) or getattr(user, "id", None) is None:
        logger.error(
            "privacy_rights operation=process status=%s error_type=user_invalid",
            STATUS_NOT_FOUND,
        )
        return _empty_result(
            mode=mode,
            status=STATUS_NOT_FOUND,
            user_id=None,
            error_type="user_invalid",
        )
    return _processar_usuario_carregado(user, apply=apply)


def processar_exercicio_privacidade_por_user_id(
    user_id: int,
    *,
    apply: bool = False,
) -> PrivacyRightsResult:
    """Entrypoint administrativo: resolve somente por user_id. Não aceita e-mail."""
    mode = MODE_APPLY if apply else MODE_DRY_RUN
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        logger.error(
            "privacy_rights operation=process status=%s error_type=user_id_invalid",
            STATUS_NOT_FOUND,
        )
        return _empty_result(
            mode=mode,
            status=STATUS_NOT_FOUND,
            user_id=None,
            error_type="user_id_invalid",
        )

    user = db.session.get(User, uid)
    if user is None:
        logger.error(
            "privacy_rights operation=process status=%s user_id=%s error_type=user_not_found",
            STATUS_NOT_FOUND,
            uid,
        )
        return _empty_result(
            mode=mode,
            status=STATUS_NOT_FOUND,
            user_id=uid,
            error_type="user_not_found",
        )
    return _processar_usuario_carregado(user, apply=apply)


def format_privacy_rights_cli(result: PrivacyRightsResult) -> str:
    """Saída operacional sem PII: mode, status, categorias e contagens."""
    lines = [
        f"MODE={result.mode}",
        f"STATUS={result.status}",
    ]
    if result.user_id is not None:
        lines.append(f"user_id={result.user_id}")
    if result.status == STATUS_OK:
        lines.extend(
            [
                f"deidentified={','.join(DEIDENTIFIED_CATEGORIES)}",
                f"preserved={','.join(result.preserved_categories)}",
                f"unsupported={','.join(result.unsupported_cleanup_categories)}",
                f"session_access_revocation={result.session_access_revocation}",
                f"global_session_storage_purge={result.global_session_storage_purge}",
                f"counts.user_fields_to_alter={len(result.user_fields_altered)}",
                f"counts.activation_journeys_ended={result.activation_journeys_ended}",
            ]
        )
    if result.error_type:
        lines.append(f"error_type={result.error_type}")
    return "\n".join(lines)


def emit_privacy_rights_cli(*, user_id: int, apply: bool, echo) -> int:
    """Entrypoint CLI: MODE no início implícito no relatório. 0=ok, 1=erro."""
    try:
        result = processar_exercicio_privacidade_por_user_id(user_id, apply=apply)
    except PrivacyRightsAborted as exc:
        echo("MODE=APPLY" if apply else "MODE=DRY_RUN")
        echo("STATUS=ERROR")
        echo(f"user_id={user_id}")
        echo(f"error_type={exc.error_type}")
        return 1
    echo(format_privacy_rights_cli(result))
    return 0 if result.status == STATUS_OK else 1


def register_privacy_rights_user_command(app) -> None:
    """Registra o comando Flask CLI. Default: dry-run."""
    import click

    @app.cli.command("privacy-rights-user")
    @click.option(
        "--user-id",
        "user_id",
        type=int,
        required=True,
        help="Identificador numérico do User. Não aceita e-mail.",
    )
    @click.option(
        "--apply",
        is_flag=True,
        default=False,
        help="Aplica desidentificação. Default: dry-run (nao grava).",
    )
    def _privacy_rights_user(user_id: int, apply: bool) -> None:
        """
        Exercicio administrativo de privacidade/LGPD de um User.

        Default: DRY RUN. Nao apaga User, Conta, Franquia, billing nem Lead.
        Nao e encerramento contratual nem self-service em /perfil.

        DRY RUN:
          flask --app app.web privacy-rights-user --user-id 123

        APPLY:
          flask --app app.web privacy-rights-user --user-id 123 --apply
        """
        exit_code = emit_privacy_rights_cli(
            user_id=user_id,
            apply=apply,
            echo=click.echo,
        )
        if exit_code:
            raise SystemExit(exit_code)


def _processar_usuario_carregado(user: User, *, apply: bool) -> PrivacyRightsResult:
    uid = int(user.id)
    mode = MODE_APPLY if apply else MODE_DRY_RUN
    logger.info(
        "privacy_rights operation=process mode=%s user_id=%s",
        mode,
        uid,
    )

    if not apply:
        try:
            result = _resultado_inspecao(user, mode=mode, executed=False)
            logger.info(
                "privacy_rights operation=process mode=%s status=%s user_id=%s",
                mode,
                result.status,
                uid,
            )
            return result
        finally:
            db.session.rollback()

    try:
        from app.services.newsletter_subscription_service import (
            unsubscribe_for_user_before_deidentify,
        )

        fields_to_alter, _preserved = _inspecionar_campos_user(user)
        unsubscribe_for_user_before_deidentify(user, commit=False)
        ended = _encerrar_jornadas_ativacao_associadas(user)
        _desidentificar_usuario(user)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        db.session.expunge_all()
        error_type = type(exc).__name__
        logger.error(
            "privacy_rights operation=process status=%s user_id=%s error_type=%s",
            STATUS_ERROR,
            uid,
            error_type,
        )
        raise PrivacyRightsAborted(error_type) from exc

    result = PrivacyRightsResult(
        mode=mode,
        status=STATUS_OK,
        user_id=uid,
        user_deidentified=True,
        activation_journeys_ended=ended,
        user_fields_altered=fields_to_alter,
        user_fields_preserved=USER_FIELDS_PRESERVED,
        preserved_categories=PRESERVED_CATEGORIES,
        unsupported_cleanup_categories=UNSUPPORTED_CLEANUP_CATEGORIES,
        current_session_cleanup_supported=False,
        global_user_temp_cleanup_supported=False,
        session_access_revocation=SESSION_ACCESS_REVOCATION_ENFORCED,
        global_session_storage_purge=GLOBAL_SESSION_STORAGE_PURGE_UNSUPPORTED,
    )
    logger.info(
        "privacy_rights operation=process mode=%s status=%s user_id=%s",
        mode,
        result.status,
        uid,
    )
    return result


def _resultado_inspecao(user: User, *, mode: str, executed: bool) -> PrivacyRightsResult:
    fields_to_alter, _preserved = _inspecionar_campos_user(user)
    pending, _already = _contar_jornadas_ativacao(user)
    return PrivacyRightsResult(
        mode=mode,
        status=STATUS_OK,
        user_id=int(user.id),
        user_deidentified=executed,
        activation_journeys_ended=pending,
        user_fields_altered=fields_to_alter,
        user_fields_preserved=USER_FIELDS_PRESERVED,
        preserved_categories=PRESERVED_CATEGORIES,
        unsupported_cleanup_categories=UNSUPPORTED_CLEANUP_CATEGORIES,
        current_session_cleanup_supported=False,
        global_user_temp_cleanup_supported=False,
        session_access_revocation=SESSION_ACCESS_REVOCATION_ENFORCED,
        global_session_storage_purge=GLOBAL_SESSION_STORAGE_PURGE_UNSUPPORTED,
    )


def _inspecionar_campos_user(user: User) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Compara o estado atual com o alvo desidentificado. Sem mutação."""
    expected = _alvo_desidentificacao(user)
    altered: list[str] = []
    for field in USER_FIELDS_DEIDENTIFIED:
        current = getattr(user, field)
        target = expected[field]
        if field == "email":
            if _normalize_email(current) != _normalize_email(target):
                altered.append(field)
            continue
        if field == "subscribes_to_newsletter":
            if bool(current) is not False:
                altered.append(field)
            continue
        if current != target:
            altered.append(field)
    return tuple(altered), USER_FIELDS_PRESERVED


def _alvo_desidentificacao(user: User) -> dict[str, object]:
    return {
        "email": email_operacional_apos_encerramento(user.id),
        "full_name": NOME_OPERACIONAL_APOS_ENCERRAMENTO,
        "password_hash": None,
        "oauth_provider": None,
        "oauth_sub": None,
        "subscribes_to_newsletter": False,
        "job_role": None,
        "usage_purpose": None,
    }


def _contar_jornadas_ativacao(user: User) -> tuple[int, int]:
    uid = int(user.id)
    pending = 0
    already = 0
    for lead in _listar_leads_jornada_ativacao(user):
        if _jornada_ativacao_ja_encerrada_para_usuario(lead, uid):
            already += 1
        else:
            pending += 1
    return pending, already


def _empty_result(
    *,
    mode: str,
    status: str,
    user_id: int | None,
    error_type: str,
) -> PrivacyRightsResult:
    return PrivacyRightsResult(
        mode=mode,
        status=status,
        user_id=user_id,
        user_deidentified=False,
        activation_journeys_ended=0,
        user_fields_altered=(),
        user_fields_preserved=USER_FIELDS_PRESERVED,
        preserved_categories=PRESERVED_CATEGORIES,
        unsupported_cleanup_categories=UNSUPPORTED_CLEANUP_CATEGORIES,
        current_session_cleanup_supported=False,
        global_user_temp_cleanup_supported=False,
        session_access_revocation=SESSION_ACCESS_REVOCATION_ENFORCED,
        global_session_storage_purge=GLOBAL_SESSION_STORAGE_PURGE_UNSUPPORTED,
        error_type=error_type,
    )


