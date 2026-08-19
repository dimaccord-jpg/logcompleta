"""
Camada persistente de communication suppression por finalidade.

Não é newsletter, cookie consent nem encerramento contratual.
Não guarda plaintext de e-mail. HMAC é centralizado aqui.

derive_email_hmac é a única autoridade de normalização + HMAC-SHA256 hex.
Lead.email_hmac e CommunicationSuppression.email_hmac usam o mesmo digest.

O secret COMMUNICATION_SUPPRESSION_HMAC_SECRET precisa permanecer estável.
Rotacioná-lo exige estratégia futura de migração/versionamento. Sem fallback,
sem versionamento neste pacote.

Quando o secret está ausente, a camada fica desabilitada: fluxos atuais de
Lead plaintext continuam no fallback (opt_out_at / activation_opt_out_at).
Minimização de Lead.email e backfill histórico recusam operar sem o secret.

Consulta com secret configurado + falha operacional: resultado explícito
unavailable (fail-closed nos gates de envio). Não confundir com
"não suppressed".
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

from flask import current_app, has_app_context
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import CommunicationSuppression, utcnow_naive

logger = logging.getLogger(__name__)

PURPOSE_PRE_REGISTRATION = "pre_registration"
PURPOSE_ACTIVATION = "activation"

SOURCE_CAMPAIGN_UNSUBSCRIBE = "campaign_unsubscribe"
SOURCE_ACTIVATION_UNSUBSCRIBE = "activation_unsubscribe"
SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT = "historical_campaign_opt_out"
SOURCE_HISTORICAL_ACTIVATION_OPT_OUT = "historical_activation_opt_out"

REASON_DISABLED = "disabled"
REASON_UNAVAILABLE = "unavailable"
REASON_SUPPRESSED = "suppressed"
REASON_NOT_SUPPRESSED = "not_suppressed"
REASON_INVALID = "invalid"

_ALLOWED_PURPOSES = frozenset(
    {
        PURPOSE_PRE_REGISTRATION,
        PURPOSE_ACTIVATION,
    }
)
_ALLOWED_SOURCES = frozenset(
    {
        SOURCE_CAMPAIGN_UNSUBSCRIBE,
        SOURCE_ACTIVATION_UNSUBSCRIBE,
        SOURCE_HISTORICAL_CAMPAIGN_OPT_OUT,
        SOURCE_HISTORICAL_ACTIVATION_OPT_OUT,
    }
)

_HMAC_SECRET_CONFIG_KEY = "COMMUNICATION_SUPPRESSION_HMAC_SECRET"
_HMAC_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
EMAIL_HMAC_HEX_LENGTH = 64
_missing_secret_warned = False


@dataclass(frozen=True)
class SuppressionSnapshot:
    """Leitura pontual para backfill: sem HMAC e sem plaintext de e-mail."""

    suppressed_at: datetime
    source: str


@dataclass(frozen=True)
class SuppressionCheck:
    """Resultado explícito da consulta de suppression.

    available=False + reason=disabled → secret ausente (fallback legado).
    available=False + reason=unavailable → secret configurado e DB falhou.
    """

    suppressed: bool
    available: bool
    reason: str

    @property
    def is_unavailable(self) -> bool:
        return self.reason == REASON_UNAVAILABLE

    @property
    def blocks_send(self) -> bool:
        return self.suppressed or self.reason == REASON_UNAVAILABLE


def normalize_email(email: str) -> str:
    """Trim + lower — mesma semântica de Lead creation/reconciliation."""
    return (email or "").strip().lower()


def normalize_email_hmac(email_hmac) -> str | None:
    """Digest HMAC-SHA256 hex: string, 64 chars, lowercase. None se inválido."""
    if not isinstance(email_hmac, str):
        return None
    digest = email_hmac.strip().lower()
    if not _HMAC_HEX_RE.fullmatch(digest):
        return None
    return digest


def is_valid_email_hmac(email_hmac) -> bool:
    return normalize_email_hmac(email_hmac) is not None


def derive_email_hmac(email: str) -> str:
    """Única autoridade: normaliza e-mail e devolve HMAC-SHA256 hex (64).

    Compatível com CommunicationSuppression.email_hmac e Lead.email_hmac.
    Sem secret: RuntimeError. E-mail vazio após normalizar: ValueError.
    Nunca registra o secret nem o plaintext.
    """
    secret = _get_hmac_secret()
    if not secret:
        _warn_missing_secret_once()
        raise RuntimeError("COMMUNICATION_SUPPRESSION_HMAC_SECRET ausente ou vazio")
    email_normalized = normalize_email(email)
    if not email_normalized:
        raise ValueError("e-mail vazio apos normalizacao")
    return _email_hmac(email_normalized, secret)


def is_suppression_enabled() -> bool:
    return bool(_get_hmac_secret())


def _purpose_and_source_allowed(purpose: str, source: str) -> bool:
    if purpose not in _ALLOWED_PURPOSES:
        logger.error("Communication suppression recusada: purpose nao permitido")
        return False
    if source not in _ALLOWED_SOURCES:
        logger.error("Communication suppression recusada: source nao permitido")
        return False
    return True


def suppress_email(
    email: str,
    purpose: str,
    source: str,
    suppressed_at=None,
    *,
    commit: bool = True,
    nested: bool = True,
) -> bool:
    """
    Upsert idempotente de suppression.

    First-write-wins: preserva suppressed_at e source originais.
    Retorna True se o registro existe após a chamada (incluindo já existente).
    Retorna False se a camada está desabilitada ou os argumentos são inválidos.

    commit=True: este helper conclui a transação (uso avulso).
    commit=False: apenas adiciona/flush na sessão; o caller controla o commit.

    nested=True (default): IntegrityError de corrida (email_hmac, purpose) é
    tratado com savepoint — não faz rollback da transação externa.
    nested=False: insert na transação do caller (backfill em batch). Corrida
    aborta o batch; não muda callers online.
    Outros IntegrityError são relançados (não escondidos).
    """
    if not _purpose_and_source_allowed(purpose, source):
        return False

    secret = _get_hmac_secret()
    if not secret:
        _warn_missing_secret_once()
        return False

    try:
        digest = derive_email_hmac(email)
    except RuntimeError:
        return False
    except ValueError:
        logger.warning("Communication suppression ignorada: e-mail vazio apos normalizacao")
        return False

    return _persist_suppression(
        digest,
        purpose,
        source,
        suppressed_at,
        commit=commit,
        nested=nested,
    )


def suppress_email_hmac(
    email_hmac,
    purpose: str,
    source: str,
    suppressed_at=None,
    *,
    commit: bool = True,
    nested: bool = True,
) -> bool:
    """Upsert idempotente por digest já calculado. Não recebe plaintext.

    Mesma persistência, first-write-wins e tratamento de UNIQUE/race do 4C-A.
    Digest inválido / purpose inválido / secret ausente: False (sem write).
    """
    if not _purpose_and_source_allowed(purpose, source):
        return False

    digest = normalize_email_hmac(email_hmac)
    if digest is None:
        logger.error("Communication suppression recusada: email_hmac invalido")
        return False

    secret = _get_hmac_secret()
    if not secret:
        _warn_missing_secret_once()
        return False

    return _persist_suppression(
        digest,
        purpose,
        source,
        suppressed_at,
        commit=commit,
        nested=nested,
    )


def check_email_suppression(email: str, purpose: str) -> SuppressionCheck:
    """Consulta suppression pela identidade HMAC + finalidade.

    Secret ausente: disabled (não bloqueia envio por esta camada).
    Secret configurado + falha de DB: unavailable (gate de envio fail-closed).
    """
    if purpose not in _ALLOWED_PURPOSES:
        logger.error("Consulta de suppression recusada: purpose nao permitido")
        return SuppressionCheck(
            suppressed=False,
            available=True,
            reason=REASON_INVALID,
        )

    secret = _get_hmac_secret()
    if not secret:
        _warn_missing_secret_once()
        return SuppressionCheck(
            suppressed=False,
            available=False,
            reason=REASON_DISABLED,
        )

    email_normalized = normalize_email(email)
    if not email_normalized:
        return SuppressionCheck(
            suppressed=False,
            available=True,
            reason=REASON_NOT_SUPPRESSED,
        )

    try:
        digest = _email_hmac(email_normalized, secret)
        row = _lookup_suppression_row(digest, purpose)
    except Exception as exc:
        logger.error(
            "Falha ao consultar communication suppression: purpose=%s error_type=%s",
            purpose,
            type(exc).__name__,
        )
        return SuppressionCheck(
            suppressed=False,
            available=False,
            reason=REASON_UNAVAILABLE,
        )

    if row is not None:
        return SuppressionCheck(
            suppressed=True,
            available=True,
            reason=REASON_SUPPRESSED,
        )
    return SuppressionCheck(
        suppressed=False,
        available=True,
        reason=REASON_NOT_SUPPRESSED,
    )


def is_email_suppressed(email: str, purpose: str) -> bool:
    """True somente se existe row de suppression.

    False não distingue desabilitado / não encontrado / falha operacional.
    Gates de envio devem usar check_email_suppression().
    """
    return check_email_suppression(email, purpose).suppressed


def get_suppression_snapshot(email: str, purpose: str) -> SuppressionSnapshot | None:
    """Leitura pontual para backfill: (suppressed_at, source) ou None.

    Não expõe HMAC. Não grava. Sem secret: recusa (sem fallback legado).
    """
    if purpose not in _ALLOWED_PURPOSES:
        raise ValueError("purpose nao permitido para snapshot de suppression")

    secret = _get_hmac_secret()
    if not secret:
        raise RuntimeError(
            "COMMUNICATION_SUPPRESSION_HMAC_SECRET ausente ou vazio"
        )

    try:
        digest = derive_email_hmac(email)
    except ValueError:
        return None
    return _snapshot_for_digest(digest, purpose)


def get_suppression_snapshot_for_hmac(
    email_hmac, purpose: str
) -> SuppressionSnapshot | None:
    """Leitura pontual por digest já persistido. Não recebe plaintext."""
    if purpose not in _ALLOWED_PURPOSES:
        raise ValueError("purpose nao permitido para snapshot de suppression")

    secret = _get_hmac_secret()
    if not secret:
        raise RuntimeError(
            "COMMUNICATION_SUPPRESSION_HMAC_SECRET ausente ou vazio"
        )

    digest = normalize_email_hmac(email_hmac)
    if digest is None:
        raise ValueError("email_hmac invalido")
    return _snapshot_for_digest(digest, purpose)


def has_email_hmac_suppression(email_hmac, purpose: str) -> bool:
    """True se existe row para o digest + purpose. Digest inválido: False."""
    if purpose not in _ALLOWED_PURPOSES:
        return False
    digest = normalize_email_hmac(email_hmac)
    if digest is None:
        return False
    return _lookup_suppression_row(digest, purpose) is not None


def _snapshot_for_digest(digest: str, purpose: str) -> SuppressionSnapshot | None:
    row = _lookup_suppression_row(digest, purpose)
    if row is None:
        return None
    return SuppressionSnapshot(suppressed_at=row.suppressed_at, source=row.source)


def _persist_suppression(
    digest: str,
    purpose: str,
    source: str,
    suppressed_at,
    *,
    commit: bool,
    nested: bool,
) -> bool:
    existing = _lookup_suppression_row(digest, purpose)
    if existing is not None:
        return True

    stamp = suppressed_at if suppressed_at is not None else utcnow_naive()
    row = CommunicationSuppression(
        email_hmac=digest,
        purpose=purpose,
        suppressed_at=stamp,
        source=source,
        created_at=utcnow_naive(),
    )

    try:
        if nested:
            with db.session.begin_nested():
                db.session.add(row)
                db.session.flush()
        else:
            db.session.add(row)
            db.session.flush()
    except IntegrityError:
        raced = _lookup_suppression_row(digest, purpose)
        if raced is not None:
            return True
        raise
    except Exception as exc:
        logger.error(
            "Falha ao persistir communication suppression: purpose=%s source=%s error_type=%s",
            purpose,
            source,
            type(exc).__name__,
        )
        if commit:
            db.session.rollback()
            return False
        raise

    if commit:
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            raced = _lookup_suppression_row(digest, purpose)
            if raced is not None:
                return True
            raise
        except Exception as exc:
            db.session.rollback()
            logger.error(
                "Falha ao persistir communication suppression: purpose=%s source=%s error_type=%s",
                purpose,
                source,
                type(exc).__name__,
            )
            return False

    logger.info(
        "Communication suppression registrada: purpose=%s source=%s",
        purpose,
        source,
    )
    return True


def _lookup_suppression_row(digest: str, purpose: str):
    return (
        CommunicationSuppression.query.filter_by(
            email_hmac=digest,
            purpose=purpose,
        ).first()
    )


def _email_hmac(normalized_email: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        normalized_email.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _get_hmac_secret() -> str | None:
    if has_app_context() and _HMAC_SECRET_CONFIG_KEY in current_app.config:
        configured = (current_app.config.get(_HMAC_SECRET_CONFIG_KEY) or "").strip()
        return configured or None
    try:
        from app.settings import settings

        configured = (settings.communication_suppression_hmac_secret or "").strip()
        if configured:
            return configured
    except Exception:
        pass
    configured = (os.getenv(_HMAC_SECRET_CONFIG_KEY) or "").strip()
    return configured or None


def _warn_missing_secret_once() -> None:
    global _missing_secret_warned
    if _missing_secret_warned:
        return
    _missing_secret_warned = True
    logger.warning(
        "Communication suppression desabilitada: %s ausente. "
        "Opt-out continua pelos campos Lead.opt_out_at / Lead.activation_opt_out_at. "
        "Antes de backfill/autoridade, %s precisa estar configurado.",
        _HMAC_SECRET_CONFIG_KEY,
        _HMAC_SECRET_CONFIG_KEY,
    )
