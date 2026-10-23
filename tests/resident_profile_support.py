"""Profile storage and tokenizer doubles for the planned HTTP boundary."""

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi.testclient import TestClient

from digital_souls_memory.api import create_app
from digital_souls_memory.recall.domain import CandidateRef, RecallCandidate
from digital_souls_memory.recall.inputs import ModelInput
from tests.recall_support import RecallHarness, StepClock

if TYPE_CHECKING:
    from digital_souls_memory.resident.domain import CharacterProfileConfig


class FakeProfileStore:
    def __init__(self) -> None:
        self.values: dict[str, CharacterProfileConfig] = {}

    def get(self, character_id: str) -> "CharacterProfileConfig | None":
        return self.values.get(character_id)

    def replace(
        self, config: "CharacterProfileConfig", *, snapshot: object
    ) -> "CharacterProfileConfig":
        self.values[config.character_id] = config
        return config


class StoredCandidates:
    def __init__(self) -> None:
        self.values: dict[CandidateRef, RecallCandidate] = {}
        self.calls: list[tuple[CandidateRef, ...]] = []

    def get_candidates(
        self, *, refs: tuple[CandidateRef, ...], now: datetime
    ) -> tuple[RecallCandidate, ...]:
        self.calls.append(refs)
        return tuple(self.values[ref] for ref in refs if ref in self.values)


class ControlledCounter:
    def __init__(self) -> None:
        self.tokens = 7
        self.fail = False
        self.inputs: list[ModelInput] = []

    def count(self, value: ModelInput) -> int:
        self.inputs.append(value)
        if self.fail:
            raise RuntimeError("Synthetic tokenizer failure")
        return self.tokens


def profile_client(
    h: RecallHarness,
    profiles: FakeProfileStore,
    candidates: StoredCandidates,
    counter: ControlledCounter,
) -> TestClient:
    h.profiles = profiles
    h.resident_candidates = candidates
    return TestClient(
        create_app(
            store=h.evidence,
            identity_store=h.identity,
            voice_store=h.voice,
            profile_store=profiles,
            resident_candidates=candidates,
            clock=lambda: h.now,
            retriever=h.retriever,
            embedding=h.embedding,
            reranker=h.reranker,
            token_counter=counter,
            provision_store=h.provisions,
            session_identity_store=h.selections,
            provision_id_factory=h.new_provision_id,
            monotonic=StepClock(),
        ),
        raise_server_exceptions=False,
    )
