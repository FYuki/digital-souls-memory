from dataclasses import replace

import pytest

from digital_souls_memory.memory.extraction import CloudInput, LocalOnly, make_gateway_input
from tests.evidence_support import TEXT, turn

pytestmark = pytest.mark.ut


@pytest.mark.parametrize(
    "labels,cloud",
    [
        (("public",), True),
        (("sensitive",), False),
        (("unknown",), False),
        (("public", "sensitive"), False),
    ],
)
def test_shared_gateway_input_preserves_materials_and_routes_the_whole_batch(
    labels: tuple[str, ...],
    cloud: bool,
) -> None:
    sources = tuple(
        replace(turn(str(i)), sensitivity=label, language="ja") for i, label in enumerate(labels)
    )
    result = make_gateway_input(
        character_id="char-b",
        materials=tuple((t, TEXT[:3]) for t in sources),
        non_sensitive_labels=frozenset({"public"}),
    )
    assert isinstance(result, CloudInput if cloud else LocalOnly)
    assert [(t.turn_id, t.text, t.language, t.actor, t.reply_to_turn_id) for t in result.turns] == [
        (t.turn_id, TEXT[:3], t.language, t.actor, t.reply_to_turn_id) for t in sources
    ]
    assert all(t.text == TEXT for t in sources)
