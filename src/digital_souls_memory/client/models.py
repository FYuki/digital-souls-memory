"""Core wire DTOs; no persistence or server-side domain decisions."""

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

Nonblank = Annotated[StrictStr, Field(pattern=r"\S")]
Identifier = Annotated[StrictStr, Field(pattern=r"^[A-Za-z0-9_-]+$")]


class SessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: Identifier
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    surface: Nonblank
    reports_delivery: StrictBool


class SessionResponse(SessionRequest):
    model_config = ConfigDict(from_attributes=True)


class _TurnFields(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn_id: Identifier
    session_id: Identifier
    surface: Nonblank
    actor: Nonblank
    kind: Literal["utterance", "observation", "thought"]
    text: StrictStr
    perceived_by: list[Identifier]
    addressees: list[Identifier]
    reply_to_turn_id: Identifier | None = None
    language: Nonblank
    speech_register: dict[StrictStr, StrictStr] = Field(alias="register")
    sensitivity: Nonblank
    classifier_version: Nonblank
    occurred_at: AwareDatetime


class TurnResponse(_TurnFields):
    model_config = ConfigDict(from_attributes=True)
    address_mode: Literal["direct", "broadcast", "self"]
    recorded_at: AwareDatetime


class ReceiptRequest(BaseModel):
    status: Literal["full", "partial", "none"]
    spoken_until: Annotated[StrictInt, Field(ge=0)]
    reported_by: Nonblank
    reported_at: AwareDatetime


class ReceiptResponse(ReceiptRequest):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str


class CoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: tuple[Nonblank, ...]
    prohibitions: tuple[Nonblank, ...]


class TemperamentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: Nonblank


class SeedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aspects: dict[str, object]
    renderer_version: Nonblank


class CoreOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    values: tuple[str, ...]
    prohibitions: tuple[str, ...]
    version: int
    registered_at: datetime


class TemperamentOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    description: str
    version: int
    registered_at: datetime


class ProfileOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    description: str
    renderer_version: str
    temperament_version: int
    version: int
    valid_from: datetime
    valid_to: datetime | None


class CurrentOutput(BaseModel):
    core_identity: CoreOutput | None
    temperament: TemperamentOutput | None
    personality_profile: ProfileOutput | None


class ExemplarInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exemplar_id: Identifier
    text: Nonblank
    source: StrictStr
    turn_id: Identifier | None
    situation_tags: tuple[Nonblank, ...]
    emotion_tags: tuple[Nonblank, ...]
    speech_register: dict[StrictStr, StrictStr] = Field(alias="register")
    trait_version: Annotated[StrictInt, Field(gt=0)]
    approved_by: Nonblank


class ExemplarSetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exemplars: Annotated[list[ExemplarInput], Field(min_length=1)]


class AnchorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_id: Identifier
    turn_id: Identifier
    kind: StrictStr
    reason: Nonblank
    approved_by: Nonblank


class ExemplarOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    exemplar_id: str
    text: str
    source: str
    turn_id: str | None
    situation_tags: tuple[str, ...]
    emotion_tags: tuple[str, ...]
    speech_register: dict[str, str] = Field(alias="register")
    trait_version: int
    approved_by: str
    registered_at: datetime


class AnchorOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    anchor_id: str
    turn_id: str
    kind: str
    reason: str
    approved_by: str
    registered_at: datetime
    text: str


class CheckOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    consistent: bool
    reason: str


class RegistrationOutput(BaseModel):
    consistency_check: CheckOutput


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    session_id: str = Field(min_length=1)
    format: Literal["materials", "assembled"]
    query: str | None = Field(default=None, min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    sensitivity: str | None = Field(default=None, min_length=1)
    classifier_version: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_query(self) -> Self:
        if (self.query is None) == (self.turn_id is None):
            raise ValueError("Exactly one query or turn_id is required")
        if self.query is not None and (self.sensitivity is None or self.classifier_version is None):
            raise ValueError("Query classification is required")
        return self


class ResidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: Identifier
    query: Nonblank | None = None


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_limit: Annotated[StrictInt, Field(ge=0)]
    entries: tuple[dict[str, object], ...]


class ProfileSourceOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str
    start: int
    end: int
    text: str
    actor: str
    language: str
    truncated: bool
    original_start: int
    original_end: int


class ProfileEntryOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    entry_id: str
    kind: str
    text: str
    source_spans: tuple[ProfileSourceOutput, ...]
    owner_id: str | None
    memory_id: str | None


class CharacterProfileOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    character_id: str
    token_limit: int
    entries: tuple[ProfileEntryOutput, ...]
    version: int


class PersonalityDescriptionOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    description: str
    version: int
    renderer_version: str


class ResidentOutput(BaseModel):
    provision_id: str
    core_identity: CoreOutput | None
    temperament: TemperamentOutput | None
    personality_profile: PersonalityDescriptionOutput | None
    character_profile: CharacterProfileOutput
    motivations: tuple[()]
    voice_exemplars: tuple[ExemplarOutput, ...]
    temperament_version: int | None
    personality_version: int | None


class DependencyOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    turn_id: str
    character_id: str
    basis: str
    receipt_version: int
    available_end: int
    used_end: int
    deadline: datetime


class CharacterStateOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    mood: str | None
    relationship_temperature: str | None
    current_topic: str | None
    character_id: str
    session_id: str
    surface: str
    updated_at: datetime
    expires_at: datetime
    input_signature: str
    dependencies: tuple[DependencyOutput, ...]


class StateOutput(BaseModel):
    state: CharacterStateOutput | None


class TurnRequest(_TurnFields):
    address_mode: Literal["direct", "broadcast", "self"]


class ResponseMemory(BaseModel):
    id: str
    kind: str
    content: str
    source_turn_ids: tuple[str, ...]


class ResponseAnchor(BaseModel):
    anchor_id: str
    turn_id: str
    text: str


class RecallResponse(BaseModel):
    provision_id: Nonblank
    temperament_version: int | None
    personality_version: int | None
    memories: tuple[ResponseMemory, ...]
    source_spans: tuple[ProfileSourceOutput, ...]
    verbatim_anchors: tuple[ResponseAnchor, ...]
    context: str | None


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: Nonblank
    target_turn_ids: tuple[Nonblank, ...] = ()
    user_id: Nonblank | None = None


class MemoryRef(BaseModel):
    character_id: str
    memory_id: str


class StoredReference(BaseModel):
    table: str
    key: tuple[str, ...]


class CascadeResult(BaseModel):
    deleted_turn_ids: tuple[str, ...]
    recalculated_memories: tuple[MemoryRef, ...]
    invalidated_memories: tuple[MemoryRef, ...]
    invalidated_provision_ids: tuple[str, ...]
    removed_references: tuple[StoredReference, ...]


class DeletionResponse(BaseModel):
    command: DeletionRequest
    requested_by: str
    target_turn_ids: tuple[str, ...]
    status: str
    cascade_result: CascadeResult | None
    error: str | None
    requested_at: AwareDatetime
    completed_at: AwareDatetime | None


class Derivation(BaseModel):
    model: str
    prompt_version: str
    created_at: datetime


class ConfidenceFeatures(BaseModel):
    retrieval: float
    source: float
    contradiction: float
    temporal: float
    corroboration: float
    self_assessment: float


class Epistemics(BaseModel):
    source_type: str
    trust_tier: str
    confidence_features: ConfidenceFeatures
    independent_evidence_count: int
    contradiction_ids: tuple[str, ...]


class IdentityLinks(BaseModel):
    self_relevance: float
    value_links: tuple[str, ...]
    relationship_links: tuple[str, ...]


class MemoryResponse(BaseModel):
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


class ExtractionResponse(BaseModel):
    character_id: str
    turn_id: str
    delivery_basis: str
    status: str
    deadline: AwareDatetime
    error: str | None
    receipt_version: int
