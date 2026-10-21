"""Count independent supporting opportunities and speakers (ADR 0023)."""

from collections.abc import Iterable

from digital_souls_memory.evidence.domain import Turn


def independent_evidence_count(source_turns: Iterable[Turn]) -> int:
    turns = {turn.turn_id: turn for turn in source_turns}
    parents = {identifier: identifier for identifier in turns}

    def root(identifier: str) -> str:
        while parents[identifier] != identifier:
            parents[identifier] = parents[parents[identifier]]
            identifier = parents[identifier]
        return identifier

    for turn in turns.values():
        if turn.reply_to_turn_id in turns:
            parents[root(turn.turn_id)] = root(turn.reply_to_turn_id)
    speakers: dict[str, set[str]] = {}
    for turn in turns.values():
        speakers.setdefault(root(turn.turn_id), set()).add(turn.actor)
    matched: dict[str, str] = {}

    def augment(opportunity: str, visited: set[str]) -> bool:
        for speaker in speakers[opportunity]:
            if speaker in visited:
                continue
            visited.add(speaker)
            if speaker not in matched or augment(matched[speaker], visited):
                matched[speaker] = opportunity
                return True
        return False

    for opportunity in speakers:
        augment(opportunity, set())
    return max(1, len(matched))
