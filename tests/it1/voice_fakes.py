"""Voice persistence double; validation and material resolution stay in production."""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from threading import RLock

from digital_souls_memory.voice.domain import VerbatimAnchor, VoiceExemplar
from digital_souls_memory.voice.ports import VoiceConflict, VoiceUnavailable

from digital_souls_memory.identity.domain import PersonalityProfile, Temperament
from digital_souls_memory.identity.ports import IdentityUnavailable, ReviewResult
from tests.it1.identity_fakes import FakeLLMGateway, FakeReviewer


class FakeVoiceTransaction:
    def __init__(self, store: "FakeVoiceStore", character_id: str) -> None:
        self.store = store
        self.character_id = character_id
        self.exemplars = store.exemplars.copy()
        self.anchors = store.anchors.copy()
        self.checks = store.checks.copy()

    def get_initial_check(self) -> ReviewResult | None:
        return self.checks.get(self.character_id)

    def save_initial_check(self, result: ReviewResult) -> None:
        self.checks[self.character_id] = result

    def add_exemplar(self, value: VoiceExemplar) -> None:
        if value.character_id != self.character_id:
            raise PermissionError("Wrong transaction character")
        key = (value.character_id, value.exemplar_id)
        if key in self.exemplars:
            raise VoiceConflict()
        self.exemplars[key] = value
        if self.store.fail_at == "insert":
            raise VoiceUnavailable()

    def add_anchor(self, value: VerbatimAnchor) -> None:
        if value.character_id != self.character_id:
            raise PermissionError("Wrong transaction character")
        key = (value.character_id, value.anchor_id)
        if key in self.anchors:
            raise VoiceConflict()
        self.anchors[key] = value


class FakeVoiceStore:
    def __init__(self) -> None:
        self.exemplars: dict[tuple[str, str], VoiceExemplar] = {}
        self.anchors: dict[tuple[str, str], VerbatimAnchor] = {}
        self.checks: dict[str, ReviewResult] = {}
        self.fail_at: str | None = None
        self.lock = RLock()

    @contextmanager
    def transaction(self, character_id: str) -> Iterator[FakeVoiceTransaction]:
        with self.lock:
            tx = FakeVoiceTransaction(self, character_id)
            yield tx
            if self.fail_at == "commit":
                raise VoiceUnavailable()
            self.exemplars, self.anchors, self.checks = tx.exemplars, tx.anchors, tx.checks

    def get_exemplar(self, character_id: str, exemplar_id: str) -> VoiceExemplar | None:
        return self.exemplars.get((character_id, exemplar_id))

    def get_anchor(self, character_id: str, anchor_id: str) -> VerbatimAnchor | None:
        return self.anchors.get((character_id, anchor_id))

    @contextmanager
    def list_exemplars(self, character_id: str) -> Iterator[Iterator[VoiceExemplar]]:
        yield (value for (owner, _), value in self.exemplars.items() if owner == character_id)

    @contextmanager
    def list_anchors(self, character_id: str) -> Iterator[Iterator[VerbatimAnchor]]:
        yield (value for (owner, _), value in self.anchors.items() if owner == character_id)


class VoiceGateway(FakeLLMGateway):
    def __init__(self) -> None:
        super().__init__()
        self.voice_calls: list[tuple[Temperament, PersonalityProfile, tuple[str, ...]]] = []
        self.responses = ("お褒めいただき、ありがとうございます。",)
        self.fail_voice = False

    def generate_typical_responses(
        self, *, temperament: Temperament, profile: PersonalityProfile, exemplars: Sequence[str]
    ) -> tuple[str, ...]:
        self.voice_calls.append((temperament, profile, tuple(exemplars)))
        if self.fail_voice:
            raise IdentityUnavailable()
        return self.responses


class VoiceReviewer(FakeReviewer):
    def __init__(self) -> None:
        super().__init__()
        self.voice_calls: list[
            tuple[Temperament, PersonalityProfile, tuple[str, ...], tuple[str, ...]]
        ] = []
        self.voice_result = ReviewResult(consistent=False, reason="合成応答の口調を作者が確認する")
        self.fail_voice = False

    def review_voice(
        self,
        *,
        temperament: Temperament,
        profile: PersonalityProfile,
        exemplars: Sequence[str],
        responses: Sequence[str],
    ) -> ReviewResult:
        self.voice_calls.append((temperament, profile, tuple(exemplars), tuple(responses)))
        if self.fail_voice:
            raise IdentityUnavailable()
        return self.voice_result
