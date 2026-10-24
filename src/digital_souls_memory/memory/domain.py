"""Immutable derived values and their evidence contracts."""

from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
from math import isfinite


class MemoryValidationError(ValueError):
    """A derived value violates its contract."""


def nonblank(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MemoryValidationError("Expected nonblank text")


def aware(value: datetime) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise MemoryValidationError("Expected aware timestamp")


def members(values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)):
        raise MemoryValidationError("Expected identifier collection")
    for value in values:
        nonblank(value)
    if len(set(values)) != len(values):
        raise MemoryValidationError("Duplicate identifiers")
    return tuple(values)


def unit(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise MemoryValidationError("Expected number")
    if not isfinite(value) or not 0 <= value <= 1:
        raise MemoryValidationError("Expected number in unit interval")


@dataclass(frozen=True, kw_only=True)
class Derivation:
    model: str
    prompt_version: str
    created_at: datetime

    def __post_init__(self) -> None:
        nonblank(self.model)
        nonblank(self.prompt_version)
        aware(self.created_at)


@dataclass(frozen=True, kw_only=True)
class ConfidenceFeatures:
    retrieval: float
    source: float
    contradiction: float
    temporal: float
    corroboration: float
    self_assessment: float

    def __post_init__(self) -> None:
        for field in fields(self):
            unit(getattr(self, field.name))

    def score(self) -> float:
        return (
            self.retrieval
            + self.source
            + self.temporal
            + self.corroboration
            + 1
            - self.contradiction
            + self.self_assessment
        ) / 6


@dataclass(frozen=True, kw_only=True)
class Epistemics:
    source_type: str
    trust_tier: str
    confidence_features: ConfidenceFeatures
    independent_evidence_count: int
    contradiction_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.source_type not in ("direct_user", "observed", "inferred", "reflection"):
            raise MemoryValidationError("Invalid source type")
        if self.trust_tier not in tuple("ABCDEF"):
            raise MemoryValidationError("Invalid trust tier")
        if not isinstance(self.confidence_features, ConfidenceFeatures):
            raise MemoryValidationError("Missing confidence features")
        if type(self.independent_evidence_count) is not int or self.independent_evidence_count < 1:
            raise MemoryValidationError("Expected positive evidence count")
        object.__setattr__(self, "contradiction_ids", members(self.contradiction_ids))


@dataclass(frozen=True, kw_only=True)
class IdentityLinks:
    self_relevance: float
    value_links: tuple[str, ...]
    relationship_links: tuple[str, ...]

    def __post_init__(self) -> None:
        unit(self.self_relevance)
        object.__setattr__(self, "value_links", members(self.value_links))
        object.__setattr__(self, "relationship_links", members(self.relationship_links))


@dataclass(frozen=True, kw_only=True)
class DerivedMemory:
    id: str
    character_id: str
    kind: str
    content: str
    source_turn_ids: tuple[str, ...]
    derivation: Derivation
    valid_from: datetime
    valid_to: datetime | None
    recorded_at: datetime
    superseded_at: datetime | None
    last_reinforced_at: datetime | None
    epistemics: Epistemics
    identity_links: IdentityLinks
    subject: str | None = None
    predicate: str | None = None
    object: str | None = None
    shared_with: tuple[str, ...] = ()
    counterpart: str | None = None
    affection: float | None = None
    trust: float | None = None
    status: str | None = None
    deadline: datetime | None = None
    verbatim_turn_id: str | None = None

    def __post_init__(self) -> None:
        for text in (self.id, self.character_id, self.content):
            nonblank(text)
        if self.kind not in ("fact", "preference", "relationship", "commitment", "summary", "key"):
            raise MemoryValidationError("Invalid memory kind")
        object.__setattr__(self, "source_turn_ids", members(self.source_turn_ids))
        object.__setattr__(self, "shared_with", members(self.shared_with))
        if not self.source_turn_ids or not isinstance(self.derivation, Derivation):
            raise MemoryValidationError("Evidence and derivation required")
        if not isinstance(self.epistemics, Epistemics) or not isinstance(
            self.identity_links, IdentityLinks
        ):
            raise MemoryValidationError("Missing memory attributes")
        for at in (self.valid_from, self.recorded_at):
            aware(at)
        for optional_at in (
            self.valid_to,
            self.superseded_at,
            self.last_reinforced_at,
            self.deadline,
        ):
            if optional_at is not None:
                aware(optional_at)
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise MemoryValidationError("Invalid time interval")
        if self.kind in ("fact", "preference"):
            for value in (self.subject, self.predicate, self.object):
                if value is None:
                    raise MemoryValidationError("Missing fact attributes")
                nonblank(value)
        if self.kind == "relationship":
            if (
                self.counterpart is None
                or self.status is None
                or self.affection is None
                or self.trust is None
            ):
                raise MemoryValidationError("Missing relationship attributes")
            nonblank(self.counterpart)
            nonblank(self.status)
            unit(self.affection)
            unit(self.trust)
        if self.kind == "commitment":
            if self.verbatim_turn_id is None:
                raise MemoryValidationError("Original commitment reference required")
            nonblank(self.verbatim_turn_id)
            if self.verbatim_turn_id not in self.source_turn_ids:
                raise MemoryValidationError("Original must belong to evidence")
            if self.last_reinforced_at is not None:
                raise MemoryValidationError("Commitments cannot be reinforced")


@dataclass(frozen=True, kw_only=True)
class MemoryRelation:
    from_id: str
    to_id: str
    type: str

    def __post_init__(self) -> None:
        nonblank(self.from_id)
        nonblank(self.to_id)
        if self.from_id == self.to_id or self.type not in (
            "updates",
            "extends",
            "derives",
            "contradicts",
        ):
            raise MemoryValidationError("Invalid relation")


@dataclass(frozen=True, kw_only=True)
class ExtractionProposal:
    memories: tuple[DerivedMemory, ...]
    relations: tuple[MemoryRelation, ...] = ()


class DeliveryBasis(StrEnum):
    DECLARED_FULL = "declared_full"
    PENDING = "pending"
    RECEIPT_CONFIRMED = "receipt_confirmed"
    ASSUMED_FULL = "assumed_full"
    ORIGINAL = "original"


@dataclass(frozen=True, kw_only=True)
class ExtractionState:
    character_id: str
    turn_id: str
    delivery_basis: DeliveryBasis
    signature: str
    status: str
    token: str | None
    lease_until: datetime | None
    deadline: datetime
    error: str | None
    text_hash: str | None
    used_end: int | None
    receipt_version: int


@dataclass(frozen=True, kw_only=True)
class InputDependency:
    turn_id: str
    basis: str
    receipt_version: int
    available_end: int
    used_end: int
    deadline: datetime


def memory_valid_at(value: DerivedMemory, now: datetime) -> bool:
    return (
        value.valid_from <= now
        and (value.valid_to is None or now < value.valid_to)
        and (value.superseded_at is None or now < value.superseded_at)
    )
