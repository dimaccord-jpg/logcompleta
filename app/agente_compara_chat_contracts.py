"""Contratos do chat analítico do Agente Compara (SCRUM-238, lote 1).

Define identidade, validação, digest e máquina de estados de uma operação
`comparison_chat`. A e B são caminhos alternativos. A liquidação consolidada
exige cada etapa faturável do caminho escolhido. Não executa o chat, não chama
modelo, não consome franquia e não persiste em banco.

A identidade autenticada e o snapshot oficial entram somente por objetos
montados pelo backend. O plano semântico não pode defini-los.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol
from uuid import UUID

CONTRACT_VERSION = "comparison_chat_contract_v1"
OPERATION_RECORD_SCHEMA_VERSION = 1
OPERATION_TYPE_COMPARISON_CHAT = "comparison_chat"

# Faixas de `agente_compara_config_service._bounded`. Não são lidas do ambiente
# aqui: o chamador entrega o limite já escolhido e este módulo só o confere.
QUESTION_MAX_CHARS_MIN = 500
QUESTION_MAX_CHARS_MAX = 12_000
QUESTION_MAX_CHARS_DEFAULT = 4_000
HISTORY_MAX_ITEMS_MIN = 1
HISTORY_MAX_ITEMS_MAX = 100
HISTORY_MAX_ITEMS_DEFAULT = 10
# Teto por item já usado por `MAX_HISTORY_ITEM_CHARS` no chat documental.
# Este contrato rejeita o excesso; não trunca.
HISTORY_ITEM_MAX_CHARS = 8_000

UI_CONTEXT_MAX_DEPTH = 6
UI_CONTEXT_MAX_KEYS = 32
UI_CONTEXT_MAX_ITEMS = 32
UI_CONTEXT_MAX_STRING = 2_000
UI_CONTEXT_MAX_BYTES = 16_384
JSON_INT_ABS_MAX = 10**12
RESPONSE_MAX_DEPTH = 8
RESPONSE_MAX_BYTES = 256_000
RESPONSE_MAX_STRING = 20_000

CLIENT_REQUEST_FIELDS = frozenset(
    {
        "message",
        "question",
        "history",
        "request_id",
        "comparison_id",
        "ui_context",
        "visual_focus",
    }
)
HISTORY_ITEM_FIELDS = frozenset({"role", "content"})
ALLOWED_HISTORY_ROLES = frozenset({"user", "assistant", "model"})
FORBIDDEN_TENANT_FIELDS = frozenset(
    {
        "conta_id",
        "usuario_id",
        "franquia_id",
        "franquia_operacional_id",
        "user_id",
        "account_id",
        "tenant_id",
    }
)
SAFE_ERROR_FIELDS = frozenset(
    {
        "message",
        "question",
        "history",
        "request_id",
        "comparison_id",
        "ui_context",
        "visual_focus",
        "role",
        "content",
        "conta_id",
        "usuario_id",
        "franquia_id",
        "conversation_id",
        "conversation_revision",
        "result_fingerprint",
        "result_checksum",
        "result_schema_version",
        "expected_result_version",
        "memory_checksum",
        "memory_schema_version",
        "semantic_plan",
        "question_max_chars",
        "history_max_items",
        "amount",
        "currency",
        "attempt_id",
        "operation_type",
        "contract_version",
        "completed_response",
        "plan_reference",
        "evidence_reference",
        "stage",
    }
)

# Superfícies deste lote que permanecem sem implementação operacional.
UNIMPLEMENTED_CONTRACT_SURFACES = (
    "durable_chat_operation_store",
    "digest_secret_provider",
    "database_migration",
    "gemini_planning",
    "official_result_query",
    "franchise_consumption",
    "route_integration",
)

_HEX_DIGITS = frozenset("0123456789abcdef")
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")
_REVISION_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-:")
_ERROR_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_CURRENCY_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_KEY_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")

# Versão do algoritmo HMAC deste contrato. Não é segredo e viaja com o digest.
HMAC_DIGEST_VERSION = 1
HMAC_KEY_ROTATION_POLICY = (
    "O digest armazena a versao do algoritmo e o key_id da chave HMAC, nunca o segredo. "
    "Um digest so e compativel com a mesma versao e o mesmo key_id. "
    "A rotacao cria um key_id novo para operacoes novas e nao reescreve digests ja gravados. "
    "Chaves distintas nao sao intercambiaveis."
)


class ChatContractError(Exception):
    """Falha contratual. A mensagem pública é só o código, sem payload."""

    def __init__(self, code: str, *, field: str | None = None):
        safe_code = code if _is_error_code(code) else "invalid_contract"
        safe_field = field if field in SAFE_ERROR_FIELDS else None
        super().__init__(safe_code)
        self.code = safe_code
        self.field = safe_field

    def __str__(self) -> str:
        return self.code

    def __repr__(self) -> str:
        if self.field is None:
            return f"ChatContractError({self.code})"
        return f"ChatContractError({self.code}, field={self.field})"


class OperationState(Enum):
    """Estado funcional da operação. Não liquida franquia por si só."""

    RECEIVED = "RECEIVED"
    VALIDATED = "VALIDATED"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    QUERYING = "QUERYING"
    EVIDENCE_READY = "EVIDENCE_READY"
    GENERATING = "GENERATING"
    COMPLETED = "COMPLETED"
    QUOTA_BLOCKED = "QUOTA_BLOCKED"
    RECOVERABLE_FAILURE = "RECOVERABLE_FAILURE"
    FINAL_FAILURE = "FINAL_FAILURE"
    UNCERTAIN_CONSUMPTION = "UNCERTAIN_CONSUMPTION"


class FinancialState(Enum):
    """Estado financeiro, independente do estado funcional."""

    UNSETTLED = "unsettled"
    CONFIRMED = "confirmed"
    BLOCKED = "blocked"
    UNCERTAIN = "uncertain"


class AnswerPath(Enum):
    """Caminhos alternativos. Este lote não executa nenhum dos dois.

    A: evidências já conhecidas e suficientes seguem para a geração governada.
    B: planejamento governado, plano validado, consulta somente leitura,
    evidências e só então a geração governada.

    A não passa por planejamento. B não trata o plano como evidência pronta.
    """

    A = "A"
    B = "B"


# Sucessores de cada caminho. A e B não são etapas empilhadas na mesma operação.
_PATH_A_NEXT: dict[OperationState, OperationState] = {
    OperationState.VALIDATED: OperationState.EVIDENCE_READY,
    OperationState.EVIDENCE_READY: OperationState.GENERATING,
    OperationState.GENERATING: OperationState.COMPLETED,
}
_PATH_B_NEXT: dict[OperationState, OperationState] = {
    OperationState.VALIDATED: OperationState.PLANNING,
    OperationState.PLANNING: OperationState.PLAN_READY,
    OperationState.PLAN_READY: OperationState.QUERYING,
    OperationState.QUERYING: OperationState.EVIDENCE_READY,
    OperationState.EVIDENCE_READY: OperationState.GENERATING,
    OperationState.GENERATING: OperationState.COMPLETED,
}
_INTERRUPTIBLE = frozenset(
    {
        OperationState.RECEIVED,
        OperationState.VALIDATED,
        OperationState.PLANNING,
        OperationState.PLAN_READY,
        OperationState.QUERYING,
        OperationState.EVIDENCE_READY,
        OperationState.GENERATING,
    }
)
_UNCERTAIN_SOURCES = frozenset(
    {
        OperationState.PLANNING,
        OperationState.QUERYING,
        OperationState.GENERATING,
    }
)
_FAILURE_TARGETS = frozenset(
    {
        OperationState.RECOVERABLE_FAILURE,
        OperationState.FINAL_FAILURE,
        OperationState.QUOTA_BLOCKED,
        OperationState.UNCERTAIN_CONSUMPTION,
    }
)
# UNCERTAIN_CONSUMPTION sai só pela recuperação explícita. Não é sucesso nem novo débito.
_TERMINAL = frozenset(
    {
        OperationState.COMPLETED,
        OperationState.QUOTA_BLOCKED,
        OperationState.FINAL_FAILURE,
    }
)
# Chamada externa faturável ou consulta. A evidência fica na tentativa, não no estado.
_EXTERNAL_STAGES = _UNCERTAIN_SOURCES
_BILLABLE_STAGES = frozenset({OperationState.PLANNING, OperationState.GENERATING})
_PLANNING_RESUME = frozenset({OperationState.VALIDATED, OperationState.PLANNING})
_GENERATION_RESUME = frozenset({OperationState.EVIDENCE_READY, OperationState.GENERATING})
# Retomada só em checkpoint já concluído, nunca no meio de uma etapa externa.
_SAFE_RESUME: dict[OperationState, frozenset[OperationState]] = {
    OperationState.RECEIVED: frozenset({OperationState.RECEIVED}),
    OperationState.VALIDATED: frozenset({OperationState.RECEIVED, OperationState.VALIDATED}),
    OperationState.PLANNING: frozenset({OperationState.VALIDATED}),
    OperationState.PLAN_READY: frozenset({OperationState.VALIDATED, OperationState.PLAN_READY}),
    OperationState.QUERYING: frozenset({OperationState.PLAN_READY}),
    OperationState.EVIDENCE_READY: frozenset(
        {
            OperationState.VALIDATED,
            OperationState.PLAN_READY,
            OperationState.EVIDENCE_READY,
        }
    ),
    OperationState.GENERATING: frozenset({OperationState.EVIDENCE_READY}),
}


class DigestSecretProvider(Protocol):
    """Fornece o segredo HMAC e o identificador público da chave.

    O segredo não é persistido. O key_id é a única referência armazenável.
    """

    def reveal_request_digest_secret(self) -> bytes:
        """Devolve o segredo. O chamador não deve registrá-lo."""

    def request_digest_key_id(self) -> str:
        """Identificador público da chave. Não é o segredo."""


class UnimplementedDigestSecretProvider:
    """Provedor real ainda não ligado a um cofre. Não contém segredo."""

    PRODUCTION_READY = False

    def reveal_request_digest_secret(self) -> bytes:
        raise ChatContractError("digest_secret_provider_not_implemented")

    def request_digest_key_id(self) -> str:
        raise ChatContractError("digest_secret_provider_not_implemented")


def has_valid_financial_settlement(financial_state: FinancialState) -> bool:
    """COMPLETED não implica liquidação. Só `confirmed` liquida."""

    return financial_state is FinancialState.CONFIRMED


def canonical_json_bytes(value: Any) -> bytes:
    """JSON canônico: chaves ordenadas, sem espaços e sem NaN."""

    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ChatContractError("invalid_type") from None
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise ChatContractError("invalid_unicode") from None


@dataclass(frozen=True)
class VersionedRequestDigest:
    """Digest verificável sem carregar o segredo."""

    version: int
    key_id: str
    signature: str

    def serialize(self) -> str:
        return f"v{self.version}:{self.key_id}:{self.signature}"


def compute_request_digest(material: Mapping[str, Any], secret: DigestSecretProvider) -> str:
    """HMAC-SHA256 versionado. O resultado cita key_id e não contém o segredo."""

    key = _reveal_secret(secret)
    key_id = _reveal_key_id(secret)
    body = canonical_json_bytes(
        {
            "hmac_version": HMAC_DIGEST_VERSION,
            "hmac_key_id": key_id,
            "material": dict(material),
        }
    )
    signature = hmac.new(key, body, hashlib.sha256).hexdigest()
    return VersionedRequestDigest(
        version=HMAC_DIGEST_VERSION,
        key_id=key_id,
        signature=signature,
    ).serialize()


def parse_request_digest(digest: str) -> VersionedRequestDigest:
    """Lê versão e key_id. Recusa formato estranho sem devolver o conteúdo assinado."""

    if not isinstance(digest, str):
        raise ChatContractError("invalid_type")
    parts = digest.split(":")
    if len(parts) != 3 or not parts[0].startswith("v"):
        raise ChatContractError("invalid_type")
    version_text = parts[0][1:]
    if not version_text.isdigit():
        raise ChatContractError("invalid_type")
    version = int(version_text)
    if version != HMAC_DIGEST_VERSION:
        raise ChatContractError("digest_key_invalid")
    key_id = _canonical_key_id(parts[1])
    signature = parts[2].strip().lower()
    if len(signature) != 64 or any(char not in _HEX_DIGITS for char in signature):
        raise ChatContractError("invalid_type")
    return VersionedRequestDigest(version=version, key_id=key_id, signature=signature)


def verify_request_digest(
    material: Mapping[str, Any],
    secret: DigestSecretProvider,
    digest: str,
) -> bool:
    """Aceita só a mesma versão e o mesmo key_id. Chave rotacionada não valida digest antigo."""

    try:
        parsed = parse_request_digest(digest)
        key_id = _reveal_key_id(secret)
    except ChatContractError:
        return False
    if parsed.version != HMAC_DIGEST_VERSION or parsed.key_id != key_id:
        return False
    expected = compute_request_digest(material, secret)
    return hmac.compare_digest(expected, digest)


@dataclass(frozen=True)
class ChatRequestLimits:
    """Limites recebidos do chamador, dentro da faixa administrativa atual."""

    question_max_chars: int
    history_max_items: int

    def __post_init__(self) -> None:
        if (
            type(self.question_max_chars) is not int
            or not QUESTION_MAX_CHARS_MIN <= self.question_max_chars <= QUESTION_MAX_CHARS_MAX
        ):
            raise ChatContractError("invalid_limit", field="question_max_chars")
        if (
            type(self.history_max_items) is not int
            or not HISTORY_MAX_ITEMS_MIN <= self.history_max_items <= HISTORY_MAX_ITEMS_MAX
        ):
            raise ChatContractError("invalid_limit", field="history_max_items")


@dataclass(frozen=True)
class HistoryTurn:
    role: str
    content: str


@dataclass(frozen=True)
class AuthorizedIdentity:
    """Identidade autenticada. Origem exclusiva: backend autorizado."""

    conta_id: int
    usuario_id: int
    franquia_id: int

    def __post_init__(self) -> None:
        _positive_int(self.conta_id, field="conta_id")
        _positive_int(self.usuario_id, field="usuario_id")
        _positive_int(self.franquia_id, field="franquia_id")


@dataclass(frozen=True)
class ConversationIdentity:
    conversation_id: str
    revision: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "conversation_id",
            _bounded_token(self.conversation_id, field="conversation_id", alphabet=_ID_CHARS, max_len=128),
        )
        object.__setattr__(
            self,
            "revision",
            _bounded_token(
                self.revision,
                field="conversation_revision",
                alphabet=_REVISION_CHARS,
                max_len=64,
            ),
        )


@dataclass(frozen=True)
class AuthorizedComparisonScope:
    """Snapshot oficial da comparação. Não vem do plano do modelo."""

    comparison_id: str
    result_fingerprint: str
    result_checksum: str
    result_schema_version: int
    memory_checksum: str | None = None
    memory_schema_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "comparison_id", canonical_comparison_id(self.comparison_id))
        object.__setattr__(
            self,
            "result_fingerprint",
            _sha256_hex(self.result_fingerprint, field="result_fingerprint"),
        )
        object.__setattr__(
            self,
            "result_checksum",
            _sha256_hex(self.result_checksum, field="result_checksum"),
        )
        if (
            type(self.result_schema_version) is not int
            or not 1 <= self.result_schema_version <= JSON_INT_ABS_MAX
        ):
            raise ChatContractError("invalid_snapshot_reference", field="result_schema_version")
        memory_checksum = self.memory_checksum
        memory_schema = self.memory_schema_version
        if memory_checksum is None and memory_schema is None:
            return
        if memory_checksum is None or memory_schema is None:
            raise ChatContractError("invalid_snapshot_reference", field="memory_checksum")
        object.__setattr__(
            self,
            "memory_checksum",
            _sha256_hex(memory_checksum, field="memory_checksum"),
        )
        if type(memory_schema) is not int or not 1 <= memory_schema <= JSON_INT_ABS_MAX:
            raise ChatContractError("invalid_snapshot_reference", field="memory_schema_version")

    @property
    def expected_result_version(self) -> str:
        """Versão esperada do resultado oficial: schema, fingerprint e checksum.

        Checksum de memória não entra aqui. Ele permanece na operação e a
        troca dele é conflito de snapshot, não uma nova pergunta.
        """

        return (
            f"{self.result_schema_version}:{self.result_fingerprint}:{self.result_checksum}"
        )


@dataclass(frozen=True)
class OperationIdentity:
    """Chave única `(conta_id, usuario_id, operation_type, request_id)`."""

    conta_id: int
    usuario_id: int
    operation_type: str
    request_id: str

    def __post_init__(self) -> None:
        _positive_int(self.conta_id, field="conta_id")
        _positive_int(self.usuario_id, field="usuario_id")
        if self.operation_type != OPERATION_TYPE_COMPARISON_CHAT:
            raise ChatContractError("invalid_operation_type", field="operation_type")
        object.__setattr__(self, "request_id", canonical_request_id(self.request_id))

    @property
    def key(self) -> tuple[int, int, str, str]:
        return (self.conta_id, self.usuario_id, self.operation_type, self.request_id)


@dataclass(frozen=True)
class AcceptedChatRequest:
    question: str
    history: tuple[HistoryTurn, ...]
    request_id: str
    comparison_id: str
    conversation: ConversationIdentity
    expected_result_version: str
    ui_context_provided: bool
    ui_context: dict[str, Any] | None
    visual_focus_provided: bool
    visual_focus_null: bool
    visual_focus: dict[str, Any] | None
    contract_version: str
    _sealed_material: bytes = field(default=b"", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "ui_context",
            None if self.ui_context is None else _seal_json_document(self.ui_context),
        )
        object.__setattr__(
            self,
            "visual_focus",
            None if self.visual_focus is None else _seal_json_document(self.visual_focus),
        )
        object.__setattr__(self, "history", tuple(self.history))
        object.__setattr__(self, "_sealed_material", canonical_json_bytes(self._material_dict()))

    def digest_material(self) -> dict[str, Any]:
        """Material selado na aceitação. Mutação posterior não altera o digest."""

        return json.loads(self._sealed_material.decode("utf-8"))

    def _material_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "question": self.question,
            "history": [{"role": turn.role, "content": turn.content} for turn in self.history],
            "conversation": {
                "id": self.conversation.conversation_id,
                "revision": self.conversation.revision,
            },
            "comparison_id": self.comparison_id,
            "expected_result_version": self.expected_result_version,
            "ui_context": {
                "provided": self.ui_context_provided,
                "value": _copy_json_document(self.ui_context),
            },
            "visual_focus": {
                "provided": self.visual_focus_provided,
                "null": self.visual_focus_null,
                "value": _copy_json_document(self.visual_focus),
            },
        }


@dataclass(frozen=True)
class FinancialAttempt:
    """Registro financeiro. Invariantes valem na construção, não só no helper."""

    attempt_id: str
    consumption: FinancialState
    amount: str | None
    currency: str | None
    stage: OperationState
    operation_id: str | None = None
    reconciled: bool = False
    reconciles_attempt_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attempt_id",
            _canonical_uuid(self.attempt_id, field="attempt_id", invalid_code="invalid_identifier"),
        )
        if type(self.consumption) is not FinancialState or self.consumption not in {
            FinancialState.CONFIRMED,
            FinancialState.BLOCKED,
            FinancialState.UNCERTAIN,
        }:
            raise ChatContractError("invalid_type")
        if type(self.stage) is not OperationState:
            raise ChatContractError("invalid_type", field="stage")
        if type(self.reconciled) is not bool:
            raise ChatContractError("invalid_type")
        amount = self.amount
        if amount is not None:
            if type(amount) is not str or canonicalize_monetary_amount(amount) != amount:
                raise ChatContractError("invalid_monetary_amount", field="amount")
        object.__setattr__(
            self,
            "currency",
            _canonicalize_currency(self.currency, amount_present=amount is not None),
        )
        if self.operation_id is not None:
            object.__setattr__(
                self,
                "operation_id",
                _canonical_uuid(
                    self.operation_id,
                    field="attempt_id",
                    invalid_code="invalid_identifier",
                ),
            )
        if self.reconciles_attempt_id is not None:
            object.__setattr__(
                self,
                "reconciles_attempt_id",
                _canonical_uuid(
                    self.reconciles_attempt_id,
                    field="attempt_id",
                    invalid_code="invalid_identifier",
                ),
            )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt_id": self.attempt_id,
            "consumption": self.consumption.value,
            "stage": self.stage.value,
            "reconciled": self.reconciled,
        }
        if self.operation_id is not None:
            payload["operation_id"] = self.operation_id
        if self.amount is not None:
            payload["amount"] = self.amount
        if self.currency is not None:
            payload["currency"] = self.currency
        if self.reconciles_attempt_id is not None:
            payload["reconciles_attempt_id"] = self.reconciles_attempt_id
        return payload


@dataclass(frozen=True)
class GovernedAttempt:
    attempt_id: str
    operation_id: str
    from_state: OperationState
    to_state: OperationState
    error_code: str | None
    failed_stage: OperationState | None
    safe_resume_stage: OperationState | None
    external_stage: OperationState | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "attempt_id",
            _canonical_uuid(self.attempt_id, field="attempt_id", invalid_code="invalid_identifier"),
        )
        object.__setattr__(
            self,
            "operation_id",
            _canonical_uuid(self.operation_id, field="attempt_id", invalid_code="invalid_identifier"),
        )
        if type(self.from_state) is not OperationState or type(self.to_state) is not OperationState:
            raise ChatContractError("invalid_transition")
        expected_external = self.to_state if self.to_state in _EXTERNAL_STAGES else None
        if self.external_stage is None:
            object.__setattr__(self, "external_stage", expected_external)
        elif self.external_stage is not expected_external:
            raise ChatContractError("invalid_transition")
        if self.failed_stage is not None and type(self.failed_stage) is not OperationState:
            raise ChatContractError("invalid_transition")
        if self.safe_resume_stage is not None and type(self.safe_resume_stage) is not OperationState:
            raise ChatContractError("invalid_transition")
        if self.error_code is not None and not _is_error_code(self.error_code):
            raise ChatContractError("invalid_type")


@dataclass(frozen=True)
class PlanReference:
    """Referência verificável de plano. Não contém o plano nem dispara planejamento.

    O vínculo com operação, snapshot e versão do contrato é obrigatório.
    A existência física do artefato fica para o armazenamento durável futuro.
    """

    plan_id: str
    plan_digest: str
    operation_id: str
    comparison_id: str
    expected_result_version: str
    contract_version: str
    memory_checksum: str | None = None
    memory_schema_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "plan_id",
            _canonical_uuid(self.plan_id, field="plan_reference", invalid_code="invalid_identifier"),
        )
        object.__setattr__(self, "plan_digest", _sha256_hex(self.plan_digest, field="plan_reference"))
        _bind_artifact_fields(self, field="plan_reference")


@dataclass(frozen=True)
class EvidenceReference:
    """Referência verificável de evidência. Não consulta resultado oficial.

    O vínculo com operação, snapshot e versão do contrato é obrigatório.
    A existência física do artefato fica para o armazenamento durável futuro.
    """

    evidence_id: str
    evidence_digest: str
    operation_id: str
    comparison_id: str
    expected_result_version: str
    contract_version: str
    memory_checksum: str | None = None
    memory_schema_version: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_id",
            _canonical_uuid(
                self.evidence_id,
                field="evidence_reference",
                invalid_code="invalid_identifier",
            ),
        )
        object.__setattr__(
            self,
            "evidence_digest",
            _sha256_hex(self.evidence_digest, field="evidence_reference"),
        )
        _bind_artifact_fields(self, field="evidence_reference")


def canonical_request_id(value: Any) -> str:
    return _canonical_uuid(value, field="request_id", invalid_code="invalid_request_id")


def canonical_comparison_id(value: Any) -> str:
    parsed = _parse_uuid(value, field="comparison_id", invalid_code="invalid_comparison_reference")
    return parsed.hex


def build_operation_identity(auth: AuthorizedIdentity, request_id: str) -> OperationIdentity:
    return OperationIdentity(
        conta_id=auth.conta_id,
        usuario_id=auth.usuario_id,
        operation_type=OPERATION_TYPE_COMPARISON_CHAT,
        request_id=request_id,
    )


def assert_plan_has_no_tenant_identifiers(plan: Mapping[str, Any]) -> None:
    """Rejeita identificadores de tenant vindos do plano semântico."""

    if not isinstance(plan, Mapping):
        raise ChatContractError("invalid_type", field="semantic_plan")
    if _mapping_contains_forbidden_key(plan, depth=0):
        raise ChatContractError("tenant_identifier_forbidden")


def validate_client_chat_request(
    payload: Mapping[str, Any],
    *,
    limits: ChatRequestLimits,
    scope: AuthorizedComparisonScope,
    conversation: ConversationIdentity,
) -> AcceptedChatRequest:
    """Valida o corpo do cliente. Não lê sessão, banco nem plano do modelo."""

    if not isinstance(payload, Mapping) or isinstance(payload, (str, bytes)):
        raise ChatContractError("invalid_type")
    _reject_unknown_or_tenant_fields(payload)
    request_id = _required_present_string(payload, "request_id", invalid_code="invalid_request_id")
    request_id = canonical_request_id(request_id)
    question = _accept_question(payload, limits)
    history = _accept_history(payload, limits)
    comparison_id = _accept_comparison_id(payload, scope)
    ui_provided, ui_value = _accept_ui_context(payload)
    focus_provided, focus_null, focus_value = _accept_visual_focus(payload)
    return AcceptedChatRequest(
        question=question,
        history=history,
        request_id=request_id,
        comparison_id=comparison_id,
        conversation=conversation,
        expected_result_version=scope.expected_result_version,
        ui_context_provided=ui_provided,
        ui_context=ui_value,
        visual_focus_provided=focus_provided,
        visual_focus_null=focus_null,
        visual_focus=focus_value,
        contract_version=CONTRACT_VERSION,
    )


def required_billable_stages(path: AnswerPath) -> tuple[OperationState, ...]:
    """Etapas faturáveis do caminho. Confirmação de outra etapa não as substitui.

    A exige só a geração final. B exige planejamento e geração, cada um à parte.
    A consulta somente leitura não é etapa faturável.
    """

    if path is AnswerPath.A:
        return (OperationState.GENERATING,)
    if path is AnswerPath.B:
        return (OperationState.PLANNING, OperationState.GENERATING)
    raise ChatContractError("invalid_transition")


def assert_artifact_binding(
    reference: PlanReference | EvidenceReference,
    *,
    operation_id: str,
    scope: AuthorizedComparisonScope,
    contract_version: str,
) -> None:
    """Recusa referência de outra operação ou de outro snapshot.

    Não consulta o artefato físico. Só confere o vínculo declarado.
    """

    if type(reference) not in {PlanReference, EvidenceReference}:
        raise ChatContractError("invalid_type")
    if type(scope) is not AuthorizedComparisonScope:
        raise ChatContractError("invalid_snapshot_reference")
    canonical_operation = _canonical_uuid(
        operation_id,
        field="plan_reference" if type(reference) is PlanReference else "evidence_reference",
        invalid_code="invalid_identifier",
    )
    field = "plan_reference" if type(reference) is PlanReference else "evidence_reference"
    if reference.operation_id != canonical_operation:
        raise ChatContractError("artifact_binding_mismatch", field=field)
    if reference.comparison_id != scope.comparison_id:
        raise ChatContractError("artifact_binding_mismatch", field="comparison_id")
    if reference.contract_version != contract_version:
        raise ChatContractError("artifact_binding_mismatch", field="contract_version")
    if reference.expected_result_version != scope.expected_result_version:
        raise ChatContractError("snapshot_conflict", field="result_fingerprint")
    if (
        reference.memory_checksum != scope.memory_checksum
        or reference.memory_schema_version != scope.memory_schema_version
    ):
        raise ChatContractError("snapshot_conflict", field="memory_checksum")


def assert_transition(
    current: OperationState,
    target: OperationState,
    *,
    failed_stage: OperationState | None = None,
    safe_resume_stage: OperationState | None = None,
    recorded_safe_resume_stage: OperationState | None = None,
    attempts: Sequence[GovernedAttempt] | None = None,
    financial_attempts: Sequence[FinancialAttempt] | None = None,
    path: AnswerPath | None = None,
    interrupted_stage: OperationState | None = None,
    plan_available: bool = False,
    response_available: bool = False,
    evidence_available: bool = False,
) -> None:
    """Aceita só transições declaradas. Retomada exige evidência da tentativa."""

    if not isinstance(current, OperationState) or not isinstance(target, OperationState):
        raise ChatContractError("invalid_transition")
    if path is not None and type(path) is not AnswerPath:
        raise ChatContractError("path_conflict")
    if current is target or current in _TERMINAL:
        raise ChatContractError("invalid_transition")
    if current is OperationState.UNCERTAIN_CONSUMPTION:
        _assert_recovery_from_uncertain(
            target=target,
            path=path,
            interrupted_stage=interrupted_stage,
            attempts=() if attempts is None else attempts,
            financial_attempts=() if financial_attempts is None else financial_attempts,
            plan_available=plan_available,
            response_available=response_available,
            evidence_available=evidence_available,
            failed_stage=failed_stage,
            safe_resume_stage=safe_resume_stage,
        )
        return
    if current is OperationState.RECOVERABLE_FAILURE:
        if recorded_safe_resume_stage is None or target is not recorded_safe_resume_stage:
            raise ChatContractError("invalid_transition")
        _require_resume_evidence(
            current=current,
            target=target,
            safe_resume_stage=safe_resume_stage,
            attempts=attempts,
            financial_attempts=financial_attempts,
            path=path,
        )
        return
    if target in _happy_targets(current, path):
        if failed_stage is not None or safe_resume_stage is not None:
            raise ChatContractError("invalid_transition")
        known_attempts = () if attempts is None else attempts
        known_financial = () if financial_attempts is None else financial_attempts
        if target in {OperationState.PLANNING, OperationState.GENERATING} and attempts is None:
            raise ChatContractError("resume_forbidden")
        if _has_unreconciled_uncertain(known_financial) and target in {
            OperationState.PLANNING,
            OperationState.GENERATING,
            OperationState.COMPLETED,
        }:
            raise ChatContractError("reconciliation_required")
        _reject_repeated_external_stage(target, known_attempts, known_financial)
        return
    if target not in _FAILURE_TARGETS or current not in _INTERRUPTIBLE:
        raise ChatContractError("invalid_transition")
    if failed_stage is not current:
        raise ChatContractError("invalid_transition")
    if target is OperationState.UNCERTAIN_CONSUMPTION and current not in _UNCERTAIN_SOURCES:
        raise ChatContractError("invalid_transition")
    if target is OperationState.RECOVERABLE_FAILURE:
        allowed = _SAFE_RESUME.get(current, frozenset())
        if safe_resume_stage not in allowed:
            raise ChatContractError("invalid_transition")
        _require_resume_evidence(
            current=current,
            target=target,
            safe_resume_stage=safe_resume_stage,
            attempts=attempts,
            financial_attempts=financial_attempts,
            path=path,
        )
        return
    if safe_resume_stage is not None:
        raise ChatContractError("invalid_transition")


def assert_resume_authorized(
    *,
    current: OperationState,
    target: OperationState,
    safe_resume_stage: OperationState | None,
    attempts: Sequence[GovernedAttempt],
    financial_attempts: Sequence[FinancialAttempt],
    path: AnswerPath | None = None,
) -> None:
    """Recusa repetir geração já enviada. Não bloqueia etapa posterior ainda não gerada."""

    _require_attempt_evidence(attempts, financial_attempts)
    authorized = _authorized_resume_stage(current, target, safe_resume_stage)
    if authorized is None:
        _reject_repeated_external_stage(target, attempts, financial_attempts)
        return
    if _financial_blocks_stage(financial_attempts, authorized):
        raise ChatContractError("resume_forbidden")
    if _authorizes_planning(authorized, path) and _blocks_planning(attempts, financial_attempts):
        raise ChatContractError("resume_forbidden")
    if _authorizes_generation(authorized, path) and _blocks_generation(attempts, financial_attempts):
        raise ChatContractError("resume_forbidden")
    _reject_repeated_external_stage(target, attempts, financial_attempts)


def assert_completion_allowed(
    target: OperationState,
    financial_attempts: Sequence[FinancialAttempt],
    financial_state: FinancialState,
    *,
    path: AnswerPath | None = None,
) -> None:
    """Consumo incerto não pode ser encerrado como conclusão publicável.

    Conclusão funcional sem as liquidações do caminho permanece não publicável.
    """

    if target is not OperationState.COMPLETED:
        return
    if financial_state is FinancialState.UNCERTAIN or _has_unreconciled_uncertain(financial_attempts):
        raise ChatContractError("reconciliation_required")
    if path is None:
        return
    for stage in required_billable_stages(path):
        if _stage_financial_status(stage, financial_attempts) is FinancialState.UNCERTAIN:
            raise ChatContractError("reconciliation_required")


def derive_financial_status(
    functional_state: OperationState,
    financial_attempts: Sequence[FinancialAttempt],
    *,
    path: AnswerPath | None = None,
) -> tuple[FinancialState, bool]:
    """Consolida pelas etapas faturáveis do caminho, não por qualquer confirmação.

    Uma etapa BLOCKED não vira geração liquidada e não apaga débito anterior.
    Planejamento confirmado não liquida a geração.
    """

    if _has_unreconciled_uncertain(financial_attempts):
        return FinancialState.UNCERTAIN, True
    if functional_state is OperationState.QUOTA_BLOCKED:
        return FinancialState.BLOCKED, False
    required = () if path is None else required_billable_stages(path)
    if not required:
        if _any_blocked(financial_attempts):
            return FinancialState.BLOCKED, False
        return FinancialState.UNSETTLED, False
    statuses = [_stage_financial_status(stage, financial_attempts) for stage in required]
    if any(status is FinancialState.UNCERTAIN for status in statuses):
        return FinancialState.UNCERTAIN, True
    if any(status is FinancialState.BLOCKED for status in statuses) or _any_blocked(financial_attempts):
        return FinancialState.BLOCKED, False
    if any(status is FinancialState.UNSETTLED for status in statuses):
        return FinancialState.UNSETTLED, False
    return FinancialState.CONFIRMED, False


def response_publication_authorized(
    functional_state: OperationState,
    financial_state: FinancialState,
    financial_attempts: Sequence[FinancialAttempt],
    *,
    path: AnswerPath | None = None,
) -> bool:
    """Publicação exige COMPLETED e liquidação de todas as etapas faturáveis do caminho."""

    if functional_state is not OperationState.COMPLETED or path is None:
        return False
    derived, pending = derive_financial_status(
        functional_state,
        financial_attempts,
        path=path,
    )
    if pending or derived is not FinancialState.CONFIRMED:
        return False
    return financial_state is FinancialState.CONFIRMED


def financial_state_after_transition(
    target: OperationState,
    current_financial: FinancialState,
) -> FinancialState:
    """COMPLETED conserva o estado financeiro anterior.

    A consolidação publicável é `derive_financial_status`, por etapa do caminho.
    """

    if target is OperationState.QUOTA_BLOCKED:
        return FinancialState.BLOCKED
    if target is OperationState.UNCERTAIN_CONSUMPTION:
        return FinancialState.UNCERTAIN
    return current_financial


def canonicalize_monetary_amount(value: Any) -> str:
    """Preserva zero. Recusa float e ausência disfarçada de valor."""

    from decimal import Decimal, InvalidOperation

    if value is None or type(value) is bool or type(value) is float:
        raise ChatContractError("invalid_monetary_amount", field="amount")
    try:
        if type(value) is int:
            amount = Decimal(value)
        elif type(value) is str or type(value) is Decimal:
            amount = Decimal(value)
        else:
            raise ChatContractError("invalid_monetary_amount", field="amount")
    except (InvalidOperation, ValueError):
        raise ChatContractError("invalid_monetary_amount", field="amount") from None
    if not amount.is_finite() or amount < 0:
        raise ChatContractError("invalid_monetary_amount", field="amount")
    text = format(amount, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in {"", "-0"}:
        text = "0"
    return text


def build_financial_attempt(
    *,
    attempt_id: Any,
    consumption: FinancialState,
    stage: OperationState,
    amount: Any = None,
    currency: str | None = None,
    operation_id: Any = None,
) -> FinancialAttempt:
    if consumption not in {FinancialState.CONFIRMED, FinancialState.BLOCKED, FinancialState.UNCERTAIN}:
        raise ChatContractError("invalid_type")
    if type(stage) is not OperationState:
        raise ChatContractError("invalid_type", field="stage")
    canonical_amount = None if amount is None else canonicalize_monetary_amount(amount)
    canonical_currency = _canonicalize_currency(currency, amount_present=canonical_amount is not None)
    canonical_operation = None
    if operation_id is not None:
        canonical_operation = _canonical_uuid(
            operation_id,
            field="attempt_id",
            invalid_code="invalid_identifier",
        )
    return FinancialAttempt(
        attempt_id=_canonical_uuid(
            attempt_id,
            field="attempt_id",
            invalid_code="invalid_identifier",
        ),
        consumption=consumption,
        amount=canonical_amount,
        currency=canonical_currency,
        stage=stage,
        operation_id=canonical_operation,
        reconciled=False,
    )


def validate_financial_attempt(attempt: FinancialAttempt) -> FinancialAttempt:
    """Reconstrói o registro. Objeto forjado ou tipo inválido não entra no armazenamento."""

    if type(attempt) is not FinancialAttempt:
        raise ChatContractError("invalid_type")
    return FinancialAttempt(
        attempt_id=attempt.attempt_id,
        consumption=attempt.consumption,
        amount=attempt.amount,
        currency=attempt.currency,
        stage=attempt.stage,
        operation_id=attempt.operation_id,
        reconciled=attempt.reconciled,
        reconciles_attempt_id=attempt.reconciles_attempt_id,
    )


def freeze_json_document(value: Any, *, depth: int = 0) -> Any:
    """Cópia JSON estável. Zero numérico permanece zero."""

    from decimal import Decimal

    if depth > RESPONSE_MAX_DEPTH:
        raise ChatContractError("invalid_type")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > JSON_INT_ABS_MAX:
            raise ChatContractError("invalid_type")
        return value
    if type(value) is str:
        _require_unicode(value, field="completed_response")
        if len(value) > RESPONSE_MAX_STRING:
            raise ChatContractError("response_too_large")
        return value
    if type(value) is Decimal:
        return canonicalize_monetary_amount(value)
    if type(value) is float:
        raise ChatContractError("invalid_monetary_amount", field="amount")
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ChatContractError("invalid_type")
            frozen[key] = freeze_json_document(item, depth=depth + 1)
        return frozen
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [freeze_json_document(item, depth=depth + 1) for item in value]
    raise ChatContractError("invalid_type")


def _reveal_secret(secret: DigestSecretProvider) -> bytes:
    if not hasattr(secret, "reveal_request_digest_secret"):
        raise ChatContractError("digest_secret_invalid")
    try:
        raw = secret.reveal_request_digest_secret()
    except ChatContractError:
        raise
    except Exception:
        raise ChatContractError("digest_secret_invalid") from None
    if type(raw) is not bytes or len(raw) < 16:
        raise ChatContractError("digest_secret_invalid")
    return raw


def _reveal_key_id(secret: DigestSecretProvider) -> str:
    if not hasattr(secret, "request_digest_key_id"):
        raise ChatContractError("digest_key_invalid")
    try:
        raw = secret.request_digest_key_id()
    except ChatContractError:
        raise
    except Exception:
        raise ChatContractError("digest_key_invalid") from None
    return _canonical_key_id(raw)


def _canonical_key_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ChatContractError("digest_key_invalid")
    text = value.strip()
    if not 1 <= len(text) <= 64 or any(char not in _KEY_ID_CHARS for char in text):
        raise ChatContractError("digest_key_invalid")
    return text


def _require_unicode(value: str, *, field: str | None) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ChatContractError("invalid_unicode", field=field) from None
    return value


def _seal_json_document(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, MappingProxyType):
        return MappingProxyType({key: _seal_json_document(item) for key, item in value.items()})
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _seal_json_document(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_seal_json_document(item) for item in value)
    raise ChatContractError("invalid_type")


def _copy_json_document(value: Any) -> Any:
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, Mapping):
        return {key: _copy_json_document(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_copy_json_document(item) for item in value]
    raise ChatContractError("invalid_type")


def _require_resume_evidence(
    *,
    current: OperationState,
    target: OperationState,
    safe_resume_stage: OperationState | None,
    attempts: Sequence[GovernedAttempt] | None,
    financial_attempts: Sequence[FinancialAttempt] | None,
    path: AnswerPath | None,
) -> None:
    if attempts is None or financial_attempts is None:
        raise ChatContractError("resume_forbidden")
    assert_resume_authorized(
        current=current,
        target=target,
        safe_resume_stage=safe_resume_stage,
        attempts=attempts,
        financial_attempts=financial_attempts,
        path=path,
    )


def _require_attempt_evidence(
    attempts: Sequence[GovernedAttempt],
    financial_attempts: Sequence[FinancialAttempt],
) -> None:
    if isinstance(attempts, (str, bytes)) or isinstance(financial_attempts, (str, bytes)):
        raise ChatContractError("invalid_type")
    for item in attempts:
        if type(item) is not GovernedAttempt:
            raise ChatContractError("invalid_type")
    for item in financial_attempts:
        if type(item) is not FinancialAttempt:
            raise ChatContractError("invalid_type")


def _authorized_resume_stage(
    current: OperationState,
    target: OperationState,
    safe_resume_stage: OperationState | None,
) -> OperationState | None:
    if target is OperationState.RECOVERABLE_FAILURE:
        return safe_resume_stage
    if current is OperationState.RECOVERABLE_FAILURE:
        return target
    return None


def _authorizes_planning(stage: OperationState, path: AnswerPath | None) -> bool:
    """O caminho A não autoriza planejamento. Confirmação de plano não viaja para outra etapa."""

    if path is AnswerPath.A:
        return False
    return stage in _PLANNING_RESUME


def _authorizes_generation(stage: OperationState, path: AnswerPath | None) -> bool:
    if path is not None and type(path) is not AnswerPath:
        return False
    return stage in _GENERATION_RESUME


def _financial_blocks_stage(
    financial_attempts: Sequence[FinancialAttempt],
    stage: OperationState,
) -> bool:
    """Bloqueia repetição só da etapa que já foi confirmada ou segue incerta."""

    for item in financial_attempts:
        if item.stage is not stage:
            continue
        if item.reconciles_attempt_id is not None:
            if item.consumption is FinancialState.CONFIRMED:
                return True
            if item.consumption is FinancialState.UNCERTAIN and item.reconciled is not True:
                return True
            continue
        if item.consumption is FinancialState.UNCERTAIN and item.reconciled is True:
            continue
        if item.consumption is FinancialState.CONFIRMED:
            return True
        if item.consumption is FinancialState.UNCERTAIN and item.reconciled is not True:
            return True
    return False


def _blocks_planning(
    attempts: Sequence[GovernedAttempt],
    financial_attempts: Sequence[FinancialAttempt],
) -> bool:
    if any(item.external_stage is OperationState.PLANNING for item in attempts):
        return True
    return _financial_blocks_stage(financial_attempts, OperationState.PLANNING)


def _blocks_generation(
    attempts: Sequence[GovernedAttempt],
    financial_attempts: Sequence[FinancialAttempt],
) -> bool:
    if any(item.external_stage is OperationState.GENERATING for item in attempts):
        return True
    return _financial_blocks_stage(financial_attempts, OperationState.GENERATING)


def _classify_follow_up(
    target: OperationState,
    attempts: Sequence[GovernedAttempt],
    financial_attempts: Sequence[FinancialAttempt],
) -> str:
    """Separa repetição, continuação, reconsulta e primeira tentativa externa.

    Reconciliação não autoriza repetir chamada já enviada.
    A primeira entrada explícita na etapa é a autorização da chamada.
    Consulta determinística pode ser reexecutada. Geração enviada, não.
    """

    if target is OperationState.QUERYING:
        return "rerun_readonly_query"
    if target is OperationState.COMPLETED:
        return "continue_completed_generation"
    if target is OperationState.PLANNING:
        if any(item.external_stage is OperationState.PLANNING for item in attempts):
            return "repeat_sent_generation"
        if _financial_blocks_stage(financial_attempts, OperationState.PLANNING):
            return "repeat_sent_generation"
        return "authorized_external_attempt"
    if target is OperationState.GENERATING:
        if any(item.external_stage is OperationState.GENERATING for item in attempts):
            return "repeat_sent_generation"
        if _financial_blocks_stage(financial_attempts, OperationState.GENERATING):
            return "repeat_sent_generation"
        return "authorized_external_attempt"
    return "continue_without_external_repeat"


def _reject_repeated_external_stage(
    target: OperationState,
    attempts: Sequence[GovernedAttempt],
    financial_attempts: Sequence[FinancialAttempt] = (),
) -> None:
    if _classify_follow_up(target, attempts, financial_attempts) == "repeat_sent_generation":
        raise ChatContractError("resume_forbidden")


def _happy_targets(current: OperationState, path: AnswerPath | None) -> frozenset[OperationState]:
    if current is OperationState.RECEIVED:
        if path is not None:
            return frozenset()
        return frozenset({OperationState.VALIDATED})
    if path is AnswerPath.A:
        return frozenset({_PATH_A_NEXT[current]}) if current in _PATH_A_NEXT else frozenset()
    if path is AnswerPath.B:
        return frozenset({_PATH_B_NEXT[current]}) if current in _PATH_B_NEXT else frozenset()
    return frozenset()


def _assert_recovery_from_uncertain(
    *,
    target: OperationState,
    path: AnswerPath | None,
    interrupted_stage: OperationState | None,
    attempts: Sequence[GovernedAttempt],
    financial_attempts: Sequence[FinancialAttempt],
    plan_available: bool,
    response_available: bool,
    evidence_available: bool,
    failed_stage: OperationState | None,
    safe_resume_stage: OperationState | None,
) -> None:
    """Sai do consumo incerto só quando a pendência foi resolvida e a continuação não repete chamada.

    Sem plano, resposta ou evidência suficiente, a operação permanece bloqueada.
    """

    if safe_resume_stage is not None:
        raise ChatContractError("invalid_transition")
    if _has_unreconciled_uncertain(financial_attempts):
        raise ChatContractError("reconciliation_required")
    if interrupted_stage not in _UNCERTAIN_SOURCES:
        raise ChatContractError("recovery_evidence_insufficient")
    if target is OperationState.FINAL_FAILURE:
        if failed_stage is not interrupted_stage:
            raise ChatContractError("invalid_transition")
        return
    if failed_stage is not None:
        raise ChatContractError("invalid_transition")
    if _classify_follow_up(target, attempts, financial_attempts) == "repeat_sent_generation":
        raise ChatContractError("resume_forbidden")

    if interrupted_stage is OperationState.PLANNING:
        if path is not AnswerPath.B or target is not OperationState.PLAN_READY:
            raise ChatContractError("resume_forbidden")
        if _stage_financial_status(OperationState.PLANNING, financial_attempts) is FinancialState.BLOCKED:
            raise ChatContractError("consumption_blocked")
        if _stage_financial_status(OperationState.PLANNING, financial_attempts) is not FinancialState.CONFIRMED:
            raise ChatContractError("recovery_evidence_insufficient")
        if not plan_available:
            raise ChatContractError("recovery_evidence_insufficient")
        return

    if interrupted_stage is OperationState.QUERYING:
        if path is not AnswerPath.B:
            raise ChatContractError("path_conflict")
        if not plan_available:
            raise ChatContractError("recovery_evidence_insufficient")
        if target is OperationState.QUERYING:
            return
        if target is OperationState.EVIDENCE_READY and evidence_available:
            return
        raise ChatContractError("recovery_evidence_insufficient")

    if interrupted_stage is OperationState.GENERATING:
        if target is not OperationState.COMPLETED:
            raise ChatContractError("resume_forbidden")
        if _stage_financial_status(OperationState.GENERATING, financial_attempts) is FinancialState.BLOCKED:
            raise ChatContractError("consumption_blocked")
        if _stage_financial_status(OperationState.GENERATING, financial_attempts) is not FinancialState.CONFIRMED:
            raise ChatContractError("recovery_evidence_insufficient")
        if not response_available:
            raise ChatContractError("recovery_evidence_insufficient")
        return

    raise ChatContractError("recovery_evidence_insufficient")


def _stage_financial_status(
    stage: OperationState,
    financial_attempts: Sequence[FinancialAttempt],
) -> FinancialState:
    """Estado da etapa. Resolução só vale para a tentativa e o estágio originais."""

    primaries = [
        item
        for item in financial_attempts
        if item.stage is stage and item.reconciles_attempt_id is None
    ]
    if not primaries:
        return FinancialState.UNSETTLED
    outcomes: list[FinancialState] = []
    for item in primaries:
        if item.consumption is FinancialState.UNCERTAIN:
            if item.reconciled is not True:
                return FinancialState.UNCERTAIN
            resolution = _resolution_for(item.attempt_id, financial_attempts)
            if resolution is None or resolution.stage is not stage:
                return FinancialState.UNCERTAIN
            outcomes.append(resolution.consumption)
            continue
        outcomes.append(item.consumption)
    if any(outcome is FinancialState.UNCERTAIN for outcome in outcomes):
        return FinancialState.UNCERTAIN
    if any(outcome is FinancialState.BLOCKED for outcome in outcomes):
        return FinancialState.BLOCKED
    if outcomes and all(outcome is FinancialState.CONFIRMED for outcome in outcomes):
        return FinancialState.CONFIRMED
    return FinancialState.UNSETTLED


def _resolution_for(
    attempt_id: str,
    financial_attempts: Sequence[FinancialAttempt],
) -> FinancialAttempt | None:
    for item in financial_attempts:
        if item.reconciles_attempt_id == attempt_id:
            return item
    return None


def _any_blocked(financial_attempts: Sequence[FinancialAttempt]) -> bool:
    return any(item.consumption is FinancialState.BLOCKED for item in financial_attempts)


def _has_unreconciled_uncertain(financial_attempts: Sequence[FinancialAttempt]) -> bool:
    return any(
        item.consumption is FinancialState.UNCERTAIN and item.reconciled is not True
        for item in financial_attempts
    )


def _bind_artifact_fields(reference: PlanReference | EvidenceReference, *, field: str) -> None:
    operation_id, comparison_id, expected_result_version, contract_version, memory_checksum, memory_schema = (
        _artifact_identity(
            reference.operation_id,
            reference.comparison_id,
            reference.expected_result_version,
            reference.contract_version,
            reference.memory_checksum,
            reference.memory_schema_version,
            field=field,
        )
    )
    object.__setattr__(reference, "operation_id", operation_id)
    object.__setattr__(reference, "comparison_id", comparison_id)
    object.__setattr__(reference, "expected_result_version", expected_result_version)
    object.__setattr__(reference, "contract_version", contract_version)
    object.__setattr__(reference, "memory_checksum", memory_checksum)
    object.__setattr__(reference, "memory_schema_version", memory_schema)


def _artifact_identity(
    operation_id: Any,
    comparison_id: Any,
    expected_result_version: Any,
    contract_version: Any,
    memory_checksum: Any,
    memory_schema_version: Any,
    *,
    field: str,
) -> tuple[str, str, str, str, str | None, int | None]:
    canonical_operation = _canonical_uuid(
        operation_id,
        field=field,
        invalid_code="invalid_identifier",
    )
    canonical_comparison = canonical_comparison_id(comparison_id)
    if not isinstance(expected_result_version, str):
        raise ChatContractError("invalid_snapshot_reference", field=field)
    version = expected_result_version.strip()
    if not version or len(version) > 200 or any(char.isspace() for char in version):
        raise ChatContractError("invalid_snapshot_reference", field=field)
    if contract_version != CONTRACT_VERSION:
        raise ChatContractError("invalid_identifier", field="contract_version")
    if memory_checksum is None and memory_schema_version is None:
        return canonical_operation, canonical_comparison, version, contract_version, None, None
    if memory_checksum is None or memory_schema_version is None:
        raise ChatContractError("invalid_snapshot_reference", field="memory_checksum")
    checksum = _sha256_hex(memory_checksum, field="memory_checksum")
    if type(memory_schema_version) is not int or not 1 <= memory_schema_version <= JSON_INT_ABS_MAX:
        raise ChatContractError("invalid_snapshot_reference", field="memory_schema_version")
    return (
        canonical_operation,
        canonical_comparison,
        version,
        contract_version,
        checksum,
        memory_schema_version,
    )


def _is_error_code(value: str) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        return False
    if value[0] not in _ERROR_CODE_CHARS or value[0].isdigit():
        return False
    return all(char in _ERROR_CODE_CHARS for char in value)


def _positive_int(value: Any, *, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ChatContractError("invalid_identifier", field=field)
    return value


def _bounded_token(value: Any, *, field: str, alphabet: frozenset[str], max_len: int) -> str:
    if not isinstance(value, str):
        raise ChatContractError("invalid_identifier", field=field)
    text = value.strip()
    if not text or len(text) > max_len or any(char not in alphabet for char in text):
        raise ChatContractError("invalid_identifier", field=field)
    return text


def _sha256_hex(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ChatContractError("invalid_snapshot_reference", field=field)
    text = value.strip().lower()
    if len(text) != 64 or any(char not in _HEX_DIGITS for char in text):
        raise ChatContractError("invalid_snapshot_reference", field=field)
    return text


def _parse_uuid(value: Any, *, field: str, invalid_code: str) -> UUID:
    if not isinstance(value, str):
        raise ChatContractError(invalid_code, field=field)
    candidate = value.strip()
    try:
        parsed = UUID(candidate)
    except (ValueError, AttributeError, TypeError):
        raise ChatContractError(invalid_code, field=field) from None
    if candidate.lower() not in {parsed.hex, str(parsed)}:
        raise ChatContractError(invalid_code, field=field)
    return parsed


def _canonical_uuid(value: Any, *, field: str, invalid_code: str) -> str:
    return str(_parse_uuid(value, field=field, invalid_code=invalid_code))


def _mapping_contains_forbidden_key(value: Any, *, depth: int) -> bool:
    if depth > 32:
        raise ChatContractError("invalid_type", field="semantic_plan")
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key in FORBIDDEN_TENANT_FIELDS:
                return True
            if _mapping_contains_forbidden_key(item, depth=depth + 1):
                return True
        return False
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_mapping_contains_forbidden_key(item, depth=depth + 1) for item in value)
    return False


def _reject_unknown_or_tenant_fields(payload: Mapping[str, Any]) -> None:
    for key in payload.keys():
        if not isinstance(key, str):
            raise ChatContractError("unknown_field")
        if key in FORBIDDEN_TENANT_FIELDS:
            raise ChatContractError("tenant_identifier_forbidden", field=key)
        if key not in CLIENT_REQUEST_FIELDS:
            raise ChatContractError("unknown_field")


def _field_state(payload: Mapping[str, Any], name: str) -> str:
    if name not in payload:
        return "absent"
    if payload[name] is None:
        return "null"
    return "present"


def _required_present_string(payload: Mapping[str, Any], name: str, *, invalid_code: str) -> str:
    state = _field_state(payload, name)
    if state == "absent":
        raise ChatContractError("missing_field", field=name)
    if state == "null":
        raise ChatContractError("null_not_allowed", field=name)
    value = payload[name]
    if not isinstance(value, str):
        raise ChatContractError(invalid_code, field=name)
    return value


def _accept_question(payload: Mapping[str, Any], limits: ChatRequestLimits) -> str:
    message_state = _field_state(payload, "message")
    question_state = _field_state(payload, "question")
    if message_state == "absent" and question_state == "absent":
        raise ChatContractError("missing_field", field="question")
    if message_state == "null" or question_state == "null":
        raise ChatContractError(
            "null_not_allowed",
            field="message" if message_state == "null" else "question",
        )
    message = payload.get("message") if message_state == "present" else None
    question = payload.get("question") if question_state == "present" else None
    if message_state == "present" and not isinstance(message, str):
        raise ChatContractError("invalid_type", field="message")
    if question_state == "present" and not isinstance(question, str):
        raise ChatContractError("invalid_type", field="question")
    accepted_message = message.strip() if isinstance(message, str) else None
    accepted_question = question.strip() if isinstance(question, str) else None
    if accepted_message is not None and accepted_question is not None:
        if accepted_message != accepted_question:
            raise ChatContractError("message_question_mismatch")
        chosen = accepted_message
    else:
        chosen = accepted_message if accepted_message is not None else accepted_question
    if not chosen:
        raise ChatContractError("invalid_type", field="question")
    _require_unicode(chosen, field="question")
    if len(chosen) > limits.question_max_chars:
        raise ChatContractError("question_too_long", field="question")
    return chosen


def _accept_history(
    payload: Mapping[str, Any],
    limits: ChatRequestLimits,
) -> tuple[HistoryTurn, ...]:
    state = _field_state(payload, "history")
    if state == "absent":
        return ()
    if state == "null":
        raise ChatContractError("null_not_allowed", field="history")
    history = payload["history"]
    if not isinstance(history, list):
        raise ChatContractError("invalid_type", field="history")
    if len(history) > limits.history_max_items:
        raise ChatContractError("history_limit_exceeded", field="history")
    turns: list[HistoryTurn] = []
    for item in history:
        turns.append(_accept_history_item(item))
    return tuple(turns)


def _accept_history_item(item: Any) -> HistoryTurn:
    if not isinstance(item, Mapping):
        raise ChatContractError("invalid_type", field="history")
    for key in item.keys():
        if not isinstance(key, str) or key not in HISTORY_ITEM_FIELDS:
            raise ChatContractError("unknown_field", field="history")
    if "role" not in item:
        raise ChatContractError("missing_field", field="role")
    if item.get("role") is None:
        raise ChatContractError("null_not_allowed", field="role")
    if "content" not in item:
        raise ChatContractError("missing_field", field="content")
    if item.get("content") is None:
        raise ChatContractError("null_not_allowed", field="content")
    role_raw = item.get("role")
    content_raw = item.get("content")
    if not isinstance(role_raw, str) or not isinstance(content_raw, str):
        raise ChatContractError("invalid_type", field="history")
    role = role_raw.strip().lower()
    if role not in ALLOWED_HISTORY_ROLES:
        raise ChatContractError("invalid_role", field="role")
    content = content_raw.strip()
    if not content:
        raise ChatContractError("invalid_type", field="content")
    _require_unicode(content, field="content")
    if len(content) > HISTORY_ITEM_MAX_CHARS:
        raise ChatContractError("history_item_too_long", field="content")
    return HistoryTurn(role=role, content=content)


def _accept_comparison_id(payload: Mapping[str, Any], scope: AuthorizedComparisonScope) -> str:
    state = _field_state(payload, "comparison_id")
    if state == "absent":
        return scope.comparison_id
    if state == "null":
        raise ChatContractError("null_not_allowed", field="comparison_id")
    client_id = canonical_comparison_id(payload.get("comparison_id"))
    if client_id != scope.comparison_id:
        raise ChatContractError("invalid_comparison_reference", field="comparison_id")
    return client_id


def _accept_ui_context(payload: Mapping[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    state = _field_state(payload, "ui_context")
    if state == "absent":
        return False, None
    if state == "null":
        raise ChatContractError("null_not_allowed", field="ui_context")
    value = payload["ui_context"]
    if not isinstance(value, Mapping):
        raise ChatContractError("invalid_type", field="ui_context")
    normalized = _normalize_tree(value, depth=0)
    if not isinstance(normalized, dict):
        raise ChatContractError("invalid_type", field="ui_context")
    if len(canonical_json_bytes(normalized)) > UI_CONTEXT_MAX_BYTES:
        raise ChatContractError("ui_context_too_large", field="ui_context")
    return True, normalized


def _accept_visual_focus(
    payload: Mapping[str, Any],
) -> tuple[bool, bool, dict[str, Any] | None]:
    state = _field_state(payload, "visual_focus")
    if state == "absent":
        return False, False, None
    if state == "null":
        return True, True, None
    value = payload["visual_focus"]
    if not isinstance(value, Mapping):
        raise ChatContractError("invalid_type", field="visual_focus")
    normalized = _normalize_tree(value, depth=0)
    if not isinstance(normalized, dict):
        raise ChatContractError("invalid_type", field="visual_focus")
    if len(canonical_json_bytes(normalized)) > UI_CONTEXT_MAX_BYTES:
        raise ChatContractError("ui_context_too_large", field="visual_focus")
    return True, False, normalized


def _normalize_tree(value: Any, *, depth: int) -> Any:
    if depth > UI_CONTEXT_MAX_DEPTH:
        raise ChatContractError("ui_context_too_large")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if abs(value) > JSON_INT_ABS_MAX:
            raise ChatContractError("invalid_type")
        return value
    if type(value) is str:
        _require_unicode(value, field="ui_context")
        if len(value) > UI_CONTEXT_MAX_STRING:
            raise ChatContractError("ui_context_too_large")
        return value
    if type(value) is float:
        raise ChatContractError("invalid_type")
    if isinstance(value, Mapping):
        if len(value) > UI_CONTEXT_MAX_KEYS:
            raise ChatContractError("ui_context_too_large")
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ChatContractError("invalid_type")
            _require_unicode(key, field="ui_context")
            if key in FORBIDDEN_TENANT_FIELDS:
                raise ChatContractError("tenant_identifier_forbidden", field=key)
            normalized[key] = _normalize_tree(item, depth=depth + 1)
        return normalized
    if isinstance(value, list):
        if len(value) > UI_CONTEXT_MAX_ITEMS:
            raise ChatContractError("ui_context_too_large")
        return [_normalize_tree(item, depth=depth + 1) for item in value]
    raise ChatContractError("invalid_type")


def _canonicalize_currency(currency: str | None, *, amount_present: bool) -> str | None:
    if not amount_present:
        if currency is not None:
            raise ChatContractError("invalid_monetary_amount", field="currency")
        return None
    if not isinstance(currency, str) or len(currency) != 3:
        raise ChatContractError("invalid_monetary_amount", field="currency")
    if any(char not in _CURRENCY_CHARS for char in currency):
        raise ChatContractError("invalid_monetary_amount", field="currency")
    return currency
