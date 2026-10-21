"""Synthetic values for the recall ports defined in the task plan."""

from datetime import timedelta

from digital_souls_memory.recall.domain import (
    CandidateRef,
    DocumentStamp,
    RecallCandidate,
    SourceStamp,
    VectorSpace,
)
from digital_souls_memory.recall.inputs import InputLabel

from digital_souls_memory.evidence.domain import Turn
from digital_souls_memory.memory.domain import DerivedMemory
from digital_souls_memory.voice.domain import VerbatimAnchor

SPACE = VectorSpace(
    model="bge-m3",
    revision="9eba04c5d75ba5a1595e45de734d36bef4e5cb98",
    digest="aa473d51f451a22f0fcf39ba3330c14bed38a385712b1113440f69df4047a173",
    dimensions=1024,
)


def source_stamp(source: Turn, *, character_id: str) -> SourceStamp:
    return SourceStamp(
        turn_id=source.turn_id,
        character_id=character_id,
        basis="declared_full",
        receipt_version=0,
        available_end=len(source.text),
        used_end=len(source.text),
        deadline=source.recorded_at + timedelta(minutes=10),
    )


def candidate_for(
    value: DerivedMemory, *, sources: tuple[Turn, ...], semantic: float = 0.9, lexical: float = 0.8
) -> RecallCandidate:
    ref = CandidateRef(owner_id=value.character_id, memory_id=value.id, anchor_id=None)
    return RecallCandidate(
        ref=ref,
        memory=value,
        anchor=None,
        search_text=value.content,
        semantic=semantic,
        lexical=lexical,
        importance=0.5,
        occurred_at=max(source.occurred_at for source in sources),
        document=DocumentStamp(ref=ref, version=1, content_digest="a" * 64, space=SPACE),
        dependencies=tuple(source_stamp(v, character_id=value.character_id) for v in sources),
        labels=tuple(InputLabel(v.sensitivity, v.classifier_version) for v in sources),
    )


def anchor_candidate(value: VerbatimAnchor, *, source: Turn) -> RecallCandidate:
    ref = CandidateRef(owner_id=value.character_id, memory_id=None, anchor_id=value.anchor_id)
    return RecallCandidate(
        ref=ref,
        memory=None,
        anchor=value,
        search_text=source.text,
        semantic=0.9,
        lexical=0.8,
        importance=0.5,
        occurred_at=source.occurred_at,
        document=DocumentStamp(ref=ref, version=1, content_digest="b" * 64, space=SPACE),
        dependencies=(source_stamp(source, character_id=value.character_id),),
        labels=(InputLabel(source.sensitivity, source.classifier_version),),
    )
