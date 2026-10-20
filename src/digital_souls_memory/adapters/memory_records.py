"""Explicit persistence representation shared by projections and extraction contributions."""

from dataclasses import asdict
from datetime import datetime
from typing import cast

from digital_souls_memory.memory.domain import (
    ConfidenceFeatures,
    Derivation,
    DerivedMemory,
    Epistemics,
    IdentityLinks,
)


def memory_record(value: DerivedMemory) -> dict[str, object]:
    return {
        "character_id": value.character_id,
        "id": value.id,
        "kind": value.kind,
        "content": value.content,
        "model": value.derivation.model,
        "prompt_version": value.derivation.prompt_version,
        "created_at": value.derivation.created_at,
        "valid_from": value.valid_from,
        "valid_to": value.valid_to,
        "recorded_at": value.recorded_at,
        "superseded_at": value.superseded_at,
        "last_reinforced_at": value.last_reinforced_at,
        "epistemics": asdict(value.epistemics),
        "identity_links": asdict(value.identity_links),
        "subject": value.subject,
        "predicate": value.predicate,
        "object": value.object,
        "shared_with": list(value.shared_with),
        "counterpart": value.counterpart,
        "affection": value.affection,
        "trust": value.trust,
        "status": value.status,
        "deadline": value.deadline,
        "verbatim_turn_id": value.verbatim_turn_id,
    }


def restore_memory(row: dict[str, object], sources: tuple[str, ...]) -> DerivedMemory:
    epistemics = cast(dict[str, object], row["epistemics"])
    features = cast(dict[str, float], epistemics["confidence_features"])
    links = cast(dict[str, object], row["identity_links"])

    def timestamp(key: str) -> datetime:
        value = row[key]
        return datetime.fromisoformat(value) if isinstance(value, str) else cast(datetime, value)

    def optional_time(key: str) -> datetime | None:
        return timestamp(key) if row[key] is not None else None

    return DerivedMemory(
        id=cast(str, row["id"]),
        character_id=cast(str, row["character_id"]),
        kind=cast(str, row["kind"]),
        content=cast(str, row["content"]),
        source_turn_ids=sources,
        derivation=Derivation(
            model=cast(str, row["model"]),
            prompt_version=cast(str, row["prompt_version"]),
            created_at=timestamp("created_at"),
        ),
        valid_from=timestamp("valid_from"),
        valid_to=optional_time("valid_to"),
        recorded_at=timestamp("recorded_at"),
        superseded_at=optional_time("superseded_at"),
        last_reinforced_at=optional_time("last_reinforced_at"),
        epistemics=Epistemics(
            source_type=cast(str, epistemics["source_type"]),
            trust_tier=cast(str, epistemics["trust_tier"]),
            confidence_features=ConfidenceFeatures(**features),
            independent_evidence_count=cast(int, epistemics["independent_evidence_count"]),
            contradiction_ids=tuple(cast(list[str], epistemics["contradiction_ids"])),
        ),
        identity_links=IdentityLinks(
            self_relevance=cast(float, links["self_relevance"]),
            value_links=tuple(cast(list[str], links["value_links"])),
            relationship_links=tuple(cast(list[str], links["relationship_links"])),
        ),
        subject=cast(str | None, row["subject"]),
        predicate=cast(str | None, row["predicate"]),
        object=cast(str | None, row["object"]),
        shared_with=tuple(cast(list[str], row["shared_with"])),
        counterpart=cast(str | None, row["counterpart"]),
        affection=cast(float | None, row["affection"]),
        trust=cast(float | None, row["trust"]),
        status=cast(str | None, row["status"]),
        deadline=optional_time("deadline"),
        verbatim_turn_id=cast(str | None, row["verbatim_turn_id"]),
    )


def candidate_record(value: DerivedMemory) -> dict[str, object]:
    record = memory_record(value)
    for key in (
        "created_at",
        "valid_from",
        "valid_to",
        "recorded_at",
        "superseded_at",
        "last_reinforced_at",
        "deadline",
    ):
        at = record[key]
        if isinstance(at, datetime):
            record[key] = at.isoformat()
    record["source_turn_ids"] = list(value.source_turn_ids)
    return record
