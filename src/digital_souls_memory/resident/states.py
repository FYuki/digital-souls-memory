"""Read saved artifacts against current evidence availability."""

from dataclasses import dataclass

from digital_souls_memory.episodes.domain import Episode
from digital_souls_memory.evidence.materials import EvidenceMaterials
from digital_souls_memory.evidence.ports import EvidenceNotFound, EvidenceStore
from digital_souls_memory.evidence.use_cases import Clock
from digital_souls_memory.recall.domain import SourceStamp
from digital_souls_memory.resident.domain import CharacterState, ContextKey
from digital_souls_memory.resident.ports import ContextStore


@dataclass(frozen=True, kw_only=True)
class EpisodeSource:
    turn_id: str
    start: int
    end: int
    text: str
    actor: str
    language: str


@dataclass(frozen=True, kw_only=True)
class EpisodeReadResult:
    episode: Episode
    source_spans: tuple[EpisodeSource, ...]


class ContextReader:
    def __init__(
        self,
        store: ContextStore,
        evidence: EvidenceStore,
        materials: EvidenceMaterials,
        clock: Clock,
    ) -> None:
        self.store, self.evidence, self.materials, self.clock = store, evidence, materials, clock

    def available(self, dependencies: tuple[SourceStamp, ...]) -> bool:
        for stamp in dependencies:
            decision = self.materials.get_decision(stamp.character_id, stamp.turn_id)
            if decision is None or decision.text is None or len(decision.text) < stamp.used_end:
                return False
        return True

    def state(self, key: ContextKey) -> CharacterState | None:
        session = self.evidence.get_session(key.session_id)
        if session is None:
            raise EvidenceNotFound()
        value = self.store.get_state(key)
        if (
            value is None
            or session.ended_at is not None
            or value.expires_at <= self.clock()
            or not self.available(value.dependencies)
        ):
            return None
        return value

    def episodes(self, key: ContextKey) -> tuple[EpisodeReadResult, ...]:
        if self.evidence.get_session(key.session_id) is None:
            raise EvidenceNotFound()
        results = []
        for value in self.store.list_episodes(key):
            if not self.available(value.dependencies):
                continue
            spans = []
            for span in value.turn_span:
                turn = self.evidence.get_turn(span.turn_id)
                assert turn is not None
                spans.append(
                    EpisodeSource(
                        turn_id=span.turn_id,
                        start=span.start,
                        end=span.end,
                        text=turn.text[span.start : span.end],
                        actor=turn.actor,
                        language=turn.language,
                    )
                )
            results.append(EpisodeReadResult(episode=value, source_spans=tuple(spans)))
        return tuple(results)
