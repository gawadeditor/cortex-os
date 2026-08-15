"""
hypogeum/execution_envelope.py — Generation Execution Envelope (Factory Core)

Implements Commit 1 of docs/architecture/AUTHORIZED_GENERATION_ENVELOPE_
IMPLEMENTATION_BLUEPRINT.md: the versioned, content-addressed, channel-
agnostic data model specified by docs/architecture/
AUTHORIZED_GENERATION_ENVELOPE.md §Phase 3.

Pure data model only. No persistence, no network access, no provider
credentials, no submission gate, no episode-file I/O. Nothing in this
module imports hypogeum.muapi, hypogeum.config, or any provider client —
it is deliberately Factory-Core-generic: every field is a plain identity
or checksum slot whose *values* happen to look like Hypogeum's
"b_047__shot3" naming convention today and would look entirely different
for a different Channel Package tomorrow. See the architecture doc's
§3.2 for the exact boundary this module must not cross.

An envelope is immutable once constructed, except for exactly one field:
`authorization_status`, which is not part of the envelope's own
content-addressed identity (see `envelope_id` / `_identity_payload()`).
Everything else about "what happened to this authorization over time"
belongs to hypogeum.authorization_lifecycle, not this module — this
module answers "what would be submitted," never "what state is it in."
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

__all__ = [
    "SUPPORTED_SCHEMA_VERSIONS",
    "ContentProvenance",
    "DependencyChecksums",
    "ExpirationPolicy",
    "GenerationExecutionEnvelope",
    "EnvelopeSchemaError",
    "EnvelopeIntegrityError",
]

SUPPORTED_SCHEMA_VERSIONS = frozenset({"1.0"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_PROVIDER_OPERATION_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


class EnvelopeSchemaError(Exception):
    """Raised when an envelope cannot be constructed at all: unsupported
    schema version, a missing required field, an empty prompt, a
    malformed checksum, an invalid cost, an unsupported currency
    representation, inconsistent provider/model/operation data, or
    incomplete dependency identity. Always raised before any
    GenerationExecutionEnvelope instance exists — construction fails
    closed, it never returns a partially-valid object."""

    def __init__(self, field_name: str, reason: str):
        self.field_name = field_name
        self.reason = reason
        super().__init__(f"[{field_name}] {reason}")


class EnvelopeIntegrityError(Exception):
    """Raised when an already-constructed envelope's own claims about
    itself do not hold: a stored envelope_id that does not match the
    recomputed content-addressed identity, or a stored
    operation_payload_sha256 that does not match the actual operation
    payload bytes. Distinct from EnvelopeSchemaError because this is a check
    performed *after* the shape is otherwise valid — the difference
    between "this isn't even a well-formed envelope" and "this claims to
    be an envelope but its own identity doesn't check out"."""

    def __init__(self, check: str, reason: str):
        self.check = check
        self.reason = reason
        super().__init__(f"[{check}] {reason}")


def _require_sha256(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.match(value):
        raise EnvelopeSchemaError(field_name, f"must be a 64-character lowercase hex sha256 digest, got {value!r}")
    return value


def _require_nonempty_str(field_name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnvelopeSchemaError(field_name, f"must be a non-empty string, got {value!r}")
    return value


def _to_jsonable(value: Any) -> Any:
    """Recursively converts a value into something json.dumps can encode
    deterministically. Decimal -> its exact string representation
    (never a binary float). Frozen dataclasses -> plain dicts. Dict
    insertion order never matters here: json.dumps(..., sort_keys=True)
    is what actually canonicalizes key order, at every nesting level."""
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        _to_jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


@dataclass(frozen=True)
class ContentProvenance:
    compiler: str
    compiler_version: str
    source_work_item_id: str
    rendered_at: str  # timestamp -- excluded from envelope identity, see _identity_payload()

    def __post_init__(self):
        _require_nonempty_str("content_provenance.compiler", self.compiler)
        _require_nonempty_str("content_provenance.compiler_version", self.compiler_version)
        _require_nonempty_str("content_provenance.source_work_item_id", self.source_work_item_id)
        _require_nonempty_str("content_provenance.rendered_at", self.rendered_at)


@dataclass(frozen=True)
class DependencyChecksums:
    channel_package: str
    plan: str
    structured_spec: str
    compiler: str

    def __post_init__(self):
        _require_sha256("dependency_checksums.channel_package", self.channel_package)
        _require_sha256("dependency_checksums.plan", self.plan)
        _require_sha256("dependency_checksums.structured_spec", self.structured_spec)
        _require_sha256("dependency_checksums.compiler", self.compiler)


@dataclass(frozen=True)
class ExpirationPolicy:
    on_expiry: str
    expires_at: Optional[str] = None

    def __post_init__(self):
        if self.on_expiry != "invalidated_source_changed":
            raise EnvelopeSchemaError(
                "expiration_policy.on_expiry",
                f"must be 'invalidated_source_changed' per the approved architecture, got {self.on_expiry!r}",
            )


@dataclass(frozen=True)
class GenerationExecutionEnvelope:
    """The immutable, versioned, content-addressed unit of authorized
    paid Provider work. See docs/architecture/AUTHORIZED_GENERATION_
    ENVELOPE.md §Phase 3 for the full field-by-field rationale.

    Construct only via GenerationExecutionEnvelope.create() -- never the
    bare dataclass constructor directly -- so every validation and the
    envelope_id computation always run together. (The dataclass
    constructor itself is still what create()/from_dict() ultimately
    call; nothing prevents direct construction at the language level,
    but every other function in this module treats create()/from_dict()
    as the only supported entry points.)
    """

    # Identity / schema
    envelope_schema_version: str
    envelope_id: str
    envelope_version: int

    # Channel / execution context
    channel_id: str
    channel_package_version: str
    channel_package_checksum: str
    execution_id: str
    workflow_version: str
    plan_version: str
    plan_checksum: str
    planned_shot_id: str
    structured_spec_checksum: str

    # Compiled content -- operation-neutral: a canonical serialization of
    # a domain-typed payload (video: prompt text; voice/music: voice id +
    # duration + format; text/research: messages/query; upscale: source
    # identity + scale -- never assumed to be a video/image prompt by
    # this module). operation_payload_schema_version names which typed
    # shape operation_payload_content was serialized from (e.g.
    # "video.text_to_video/1.0"), so a consumer can decode it correctly
    # without this module ever needing to understand any domain's shape.
    compiler_identity: str
    compiler_version: str
    operation_payload_content: str
    operation_payload_sha256: str
    operation_payload_schema_version: str
    content_provenance: ContentProvenance

    # Provider binding
    provider: str
    model: str
    provider_operation: str
    deterministic_output_path: str
    expected_output_identity: str

    # Authorization identity
    authorization_id: str
    authorization_version: int
    max_cost_per_call: Decimal
    run_level_budget_ref: str
    episode_level_budget_ref: str
    currency: str
    retry_authorized: bool
    fallback_authorized: bool
    idempotency_key: str
    created_at: str
    expiration_policy: ExpirationPolicy
    dependency_checksums: DependencyChecksums
    audit_metadata: Mapping[str, Any]

    # The one deliberately mutable field -- never part of envelope_id.
    authorization_status: str = "draft"

    # Optional fields (all immutable when present)
    organization_id: Optional[str] = None
    scene_id: Optional[str] = None
    beat_id: Optional[str] = None
    duration_seconds: Optional[Decimal] = None
    aspect_ratio: Optional[str] = None
    seed: Optional[int] = None
    retry_budget_ref: Optional[str] = None
    fallback_model: Optional[str] = None
    fallback_provider: Optional[str] = None
    fallback_budget_ref: Optional[str] = None
    superseded_envelope_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def create(cls, **kwargs: Any) -> "GenerationExecutionEnvelope":
        """The one supported construction path. Validates every field,
        computes envelope_id from the canonical immutable field set, and
        returns a fully-formed, self-consistent envelope. Raises
        EnvelopeSchemaError (never a bare TypeError/KeyError) on any
        missing/invalid input."""
        _validate_schema_shape(kwargs)

        # Build nested structures if callers passed plain dicts. A
        # TypeError here means the caller's dict is missing/misnamed a
        # required sub-field (e.g. an incomplete dependency_checksums) --
        # translated to the module's own typed exception so no caller of
        # create() ever has to catch a bare TypeError to detect a schema
        # problem.
        try:
            cp = kwargs.get("content_provenance")
            if isinstance(cp, Mapping):
                kwargs["content_provenance"] = ContentProvenance(**cp)
            dc = kwargs.get("dependency_checksums")
            if isinstance(dc, Mapping):
                kwargs["dependency_checksums"] = DependencyChecksums(**dc)
            ep = kwargs.get("expiration_policy")
            if isinstance(ep, Mapping):
                kwargs["expiration_policy"] = ExpirationPolicy(**ep)
        except TypeError as exc:
            raise EnvelopeSchemaError("dependency_identity", f"incomplete or malformed nested structure: {exc}") from exc

        kwargs.setdefault("audit_metadata", {})
        kwargs["audit_metadata"] = dict(kwargs["audit_metadata"])

        for money_field in ("max_cost_per_call", "duration_seconds"):
            if money_field in kwargs and kwargs[money_field] is not None:
                kwargs[money_field] = _coerce_decimal(money_field, kwargs[money_field])

        kwargs.setdefault("envelope_id", None)
        supplied_envelope_id = kwargs.pop("envelope_id")

        instance = cls(envelope_id="", **kwargs)
        computed_id = instance.compute_envelope_id()

        if supplied_envelope_id is not None and supplied_envelope_id != computed_id:
            raise EnvelopeIntegrityError(
                "envelope_id",
                f"supplied envelope_id {supplied_envelope_id!r} does not match "
                f"recomputed identity {computed_id!r}",
            )

        object.__setattr__(instance, "envelope_id", computed_id)
        instance.validate_integrity()
        return instance

    def __post_init__(self):
        _validate_field_contents(self)

    # ------------------------------------------------------------------
    # Canonical serialization / identity
    # ------------------------------------------------------------------

    def _identity_payload(self) -> dict:
        """Exactly the field set the architecture doc's envelope_id
        definition specifies: every field except envelope_id itself and
        every audit/timestamp field (created_at, audit_metadata, and
        content_provenance.rendered_at), and excluding the one
        deliberately mutable field, authorization_status."""
        cp = self.content_provenance
        payload = {
            "envelope_schema_version": self.envelope_schema_version,
            "envelope_version": self.envelope_version,
            "organization_id": self.organization_id,
            "channel_id": self.channel_id,
            "channel_package_version": self.channel_package_version,
            "channel_package_checksum": self.channel_package_checksum,
            "execution_id": self.execution_id,
            "workflow_version": self.workflow_version,
            "plan_version": self.plan_version,
            "plan_checksum": self.plan_checksum,
            "scene_id": self.scene_id,
            "beat_id": self.beat_id,
            "planned_shot_id": self.planned_shot_id,
            "structured_spec_checksum": self.structured_spec_checksum,
            "compiler_identity": self.compiler_identity,
            "compiler_version": self.compiler_version,
            "operation_payload_content": self.operation_payload_content,
            "operation_payload_sha256": self.operation_payload_sha256,
            "operation_payload_schema_version": self.operation_payload_schema_version,
            "content_provenance": {
                "compiler": cp.compiler,
                "compiler_version": cp.compiler_version,
                "source_work_item_id": cp.source_work_item_id,
            },
            "provider": self.provider,
            "model": self.model,
            "provider_operation": self.provider_operation,
            "duration_seconds": self.duration_seconds,
            "aspect_ratio": self.aspect_ratio,
            "seed": self.seed,
            "deterministic_output_path": self.deterministic_output_path,
            "expected_output_identity": self.expected_output_identity,
            "authorization_id": self.authorization_id,
            "authorization_version": self.authorization_version,
            "max_cost_per_call": self.max_cost_per_call,
            "run_level_budget_ref": self.run_level_budget_ref,
            "episode_level_budget_ref": self.episode_level_budget_ref,
            "currency": self.currency,
            "retry_authorized": self.retry_authorized,
            "retry_budget_ref": self.retry_budget_ref,
            "fallback_authorized": self.fallback_authorized,
            "fallback_model": self.fallback_model,
            "fallback_provider": self.fallback_provider,
            "fallback_budget_ref": self.fallback_budget_ref,
            "idempotency_key": self.idempotency_key,
            "expiration_policy": {
                "on_expiry": self.expiration_policy.on_expiry,
                "expires_at": self.expiration_policy.expires_at,
            },
            "dependency_checksums": {
                "channel_package": self.dependency_checksums.channel_package,
                "plan": self.dependency_checksums.plan,
                "structured_spec": self.dependency_checksums.structured_spec,
                "compiler": self.dependency_checksums.compiler,
            },
            "superseded_envelope_id": self.superseded_envelope_id,
        }
        return payload

    def canonical_json_bytes(self) -> bytes:
        """The exact deterministic bytes envelope_id is a sha256 of.
        Identical across processes and regardless of any dict/kwarg
        insertion order used to construct this instance -- json.dumps's
        sort_keys=True is what canonicalizes key order, not construction
        order."""
        return _canonical_bytes(self._identity_payload())

    def compute_envelope_id(self) -> str:
        return hashlib.sha256(self.canonical_json_bytes()).hexdigest()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_integrity(self) -> None:
        """Re-verifies this envelope's own claims about itself. Never
        contacts a provider, a database, or the filesystem -- purely a
        function of the fields already held in memory."""
        recomputed_id = self.compute_envelope_id()
        if recomputed_id != self.envelope_id:
            raise EnvelopeIntegrityError(
                "envelope_id",
                f"stored envelope_id {self.envelope_id!r} != recomputed {recomputed_id!r}",
            )
        recomputed_payload_checksum = hashlib.sha256(
            self.operation_payload_content.encode("utf-8")
        ).hexdigest()
        if recomputed_payload_checksum != self.operation_payload_sha256:
            raise EnvelopeIntegrityError(
                "operation_payload_sha256",
                f"stored checksum {self.operation_payload_sha256!r} != "
                f"recomputed {recomputed_payload_checksum!r} of the exact operation_payload_content bytes",
            )

    # ------------------------------------------------------------------
    # Conversion
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Full field dict, including the mutable authorization_status
        and the audit/timestamp fields excluded from envelope_id -- this
        is for storage/inspection, never for identity computation."""
        payload = self._identity_payload()
        payload["envelope_id"] = self.envelope_id
        payload["authorization_status"] = self.authorization_status
        payload["created_at"] = self.created_at
        payload["content_provenance"]["rendered_at"] = self.content_provenance.rendered_at
        payload["audit_metadata"] = dict(self.audit_metadata)
        return _to_jsonable(payload)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GenerationExecutionEnvelope":
        """Reconstructs and fully re-validates an envelope from a plain
        dict (e.g. loaded from JSON). Runs the exact same validation
        create() does, including verifying the stored envelope_id
        against the recomputed one."""
        payload = dict(data)
        for decimal_field in ("max_cost_per_call", "duration_seconds"):
            if payload.get(decimal_field) is not None:
                payload[decimal_field] = _coerce_decimal(decimal_field, payload[decimal_field])
        return cls.create(**payload)


# ---------------------------------------------------------------------------
# Field-level validation helpers
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS = (
    "envelope_schema_version", "envelope_version", "channel_id",
    "channel_package_version", "channel_package_checksum", "execution_id",
    "workflow_version", "plan_version", "plan_checksum", "planned_shot_id",
    "structured_spec_checksum", "compiler_identity", "compiler_version",
    "operation_payload_content", "operation_payload_sha256", "operation_payload_schema_version",
    "content_provenance", "provider", "model", "provider_operation",
    "deterministic_output_path", "expected_output_identity",
    "authorization_id", "authorization_version", "max_cost_per_call",
    "run_level_budget_ref", "episode_level_budget_ref", "currency",
    "retry_authorized", "fallback_authorized", "idempotency_key",
    "created_at", "expiration_policy", "dependency_checksums",
)


def _validate_schema_shape(kwargs: Mapping[str, Any]) -> None:
    schema_version = kwargs.get("envelope_schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise EnvelopeSchemaError(
            "envelope_schema_version",
            f"unsupported schema version {schema_version!r}; supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}",
        )
    missing = [f for f in _REQUIRED_FIELDS if f not in kwargs or kwargs[f] is None]
    if missing:
        raise EnvelopeSchemaError("required_fields", f"missing required field(s): {missing}")


def _coerce_decimal(field_name: str, value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise EnvelopeSchemaError(
            field_name, "monetary/duration amounts must be Decimal or str, never a binary float",
        )
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise EnvelopeSchemaError(field_name, f"cannot be represented as an exact decimal: {value!r}") from exc


def _validate_field_contents(env: "GenerationExecutionEnvelope") -> None:
    _require_nonempty_str("channel_id", env.channel_id)
    _require_nonempty_str("execution_id", env.execution_id)
    _require_nonempty_str("planned_shot_id", env.planned_shot_id)
    _require_nonempty_str("compiler_identity", env.compiler_identity)
    _require_nonempty_str("compiler_version", env.compiler_version)
    _require_nonempty_str("deterministic_output_path", env.deterministic_output_path)
    _require_nonempty_str("expected_output_identity", env.expected_output_identity)
    _require_nonempty_str("authorization_id", env.authorization_id)
    _require_nonempty_str("run_level_budget_ref", env.run_level_budget_ref)
    _require_nonempty_str("episode_level_budget_ref", env.episode_level_budget_ref)
    _require_nonempty_str("idempotency_key", env.idempotency_key)
    _require_nonempty_str("created_at", env.created_at)

    if not env.operation_payload_content:
        raise EnvelopeSchemaError("operation_payload_content", "must not be empty")
    _require_sha256("operation_payload_sha256", env.operation_payload_sha256)
    _require_nonempty_str("operation_payload_schema_version", env.operation_payload_schema_version)
    _require_sha256("plan_checksum", env.plan_checksum)
    _require_sha256("structured_spec_checksum", env.structured_spec_checksum)
    _require_sha256("channel_package_checksum", env.channel_package_checksum)

    if not isinstance(env.envelope_version, int) or env.envelope_version < 1:
        raise EnvelopeSchemaError("envelope_version", f"must be a positive integer, got {env.envelope_version!r}")
    if not isinstance(env.authorization_version, int) or env.authorization_version < 1:
        raise EnvelopeSchemaError("authorization_version", f"must be a positive integer, got {env.authorization_version!r}")

    if not isinstance(env.max_cost_per_call, Decimal) or env.max_cost_per_call < 0:
        raise EnvelopeSchemaError("max_cost_per_call", f"must be a non-negative Decimal, got {env.max_cost_per_call!r}")
    if env.duration_seconds is not None and (
        not isinstance(env.duration_seconds, Decimal) or env.duration_seconds < 0
    ):
        raise EnvelopeSchemaError("duration_seconds", f"must be a non-negative Decimal or None, got {env.duration_seconds!r}")

    if not isinstance(env.currency, str) or not _CURRENCY_RE.match(env.currency):
        raise EnvelopeSchemaError("currency", f"must be a 3-letter uppercase currency code, got {env.currency!r}")

    if not isinstance(env.provider, str) or not env.provider.strip():
        raise EnvelopeSchemaError("provider", f"must be a non-empty string, got {env.provider!r}")
    if not isinstance(env.model, str) or not env.model.strip():
        raise EnvelopeSchemaError("model", f"must be a non-empty string, got {env.model!r}")
    if not isinstance(env.provider_operation, str) or not _PROVIDER_OPERATION_RE.match(env.provider_operation):
        raise EnvelopeSchemaError(
            "provider_operation",
            f"must be '<domain>.<operation>' (lowercase, e.g. 'video.text_to_video'), got {env.provider_operation!r}",
        )

    if not isinstance(env.dependency_checksums, DependencyChecksums):
        raise EnvelopeSchemaError("dependency_checksums", "must be a complete DependencyChecksums (channel_package, plan, structured_spec, compiler)")
    if not isinstance(env.content_provenance, ContentProvenance):
        raise EnvelopeSchemaError("content_provenance", "must be a complete ContentProvenance (compiler, compiler_version, source_work_item_id, rendered_at)")
    if not isinstance(env.expiration_policy, ExpirationPolicy):
        raise EnvelopeSchemaError("expiration_policy", "must be a complete ExpirationPolicy (on_expiry, expires_at)")

    if env.retry_authorized and not env.retry_budget_ref:
        raise EnvelopeSchemaError("retry_budget_ref", "required when retry_authorized is true")
    if env.fallback_authorized and not (env.fallback_model and env.fallback_provider and env.fallback_budget_ref):
        raise EnvelopeSchemaError(
            "fallback_authorized",
            "fallback_model, fallback_provider, and fallback_budget_ref are all required when fallback_authorized is true",
        )

    if env.authorization_status not in _VALID_AUTHORIZATION_STATUS_VALUES:
        raise EnvelopeSchemaError(
            "authorization_status",
            f"{env.authorization_status!r} is not a recognized authorization state "
            f"(see hypogeum.authorization_lifecycle.AuthorizationState)",
        )


# Kept as a plain string set here (not imported from authorization_lifecycle)
# to avoid a circular import between the two Commit-1 modules -- this module
# only needs to know these are *valid strings*, never their transition rules.
_VALID_AUTHORIZATION_STATUS_VALUES = frozenset({
    "draft", "compiled", "preflight_failed", "awaiting_authorization",
    "authorized", "submitting", "submitted", "downloaded",
    "technical_validation_failed", "pending_editorial_review", "qualified",
    "rejected", "allocated", "consumed", "invalidated_source_changed",
    "superseded", "cancelled",
})
