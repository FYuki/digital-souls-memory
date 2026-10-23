"""Real HTTP, real PostgreSQL, real workers; all model ports are synthetic."""

import logging
import os
from datetime import timedelta
from functools import partial
from uuid import uuid4

import httpx
import psycopg
import pytest

from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.client import MemoryClient, MemoryHTTPError
from digital_souls_memory.client.models import (
    AnchorInput,
    DeletionRequest,
    ExemplarSetInput,
    ProfileRequest,
    RecallRequest,
    ReceiptRequest,
    ResidentRequest,
    SeedInput,
    SessionRequest,
)
from tests.evidence_support import TEXT, receipt_payload, session_payload, turn_payload
from tests.identity_support import INSTRUCTION, seed_payload
from tests.st.support import (
    add_session,
    add_turn,
    eventually,
    extraction_done,
    new_client,
    recall,
    register,
    server_time,
)
from tests.voice_support import anchor_payload, exemplar_payload

pytestmark = pytest.mark.st


def test_phase_one_http_lifecycle() -> None:
    audit_url = os.environ["MEMORY_CORE_ST_AUDIT_URL"]
    with psycopg.connect(audit_url) as conn:
        assert conn.execute(
            "SELECT pg_has_role('memory_st_app','memory_deletion_executor','MEMBER'), "
            "pg_has_role('memory_st_delete','memory_deletion_executor','MEMBER')"
        ).fetchone() == (False, True)
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0010",)
    x, y, z = ("st-x-" + uuid4().hex, "st-y-" + uuid4().hex, "st-z-" + uuid4().hex)
    with new_client() as client:
        for character in (x, y, z):
            register(client, character)
        identity = client.get_identity(x)
        scenario_time = server_time()
        session = add_session(client, delivery=False, started_at=scenario_time)
        question = add_turn(
            client,
            session,
            actor="user-a",
            readers=[x, y],
            text=TEXT,
        )
        eventually(lambda: extraction_done(client, x, question))
        eventually(lambda: extraction_done(client, y, question))
        assert client.get_extraction(y, question).delivery_basis == "declared_full"
        eventually(
            lambda: any(s.turn_id == question for s in recall(client, x, session).source_spans)
        )
        first = recall(client, x, session)
        assert any(
            s.text == TEXT and s.actor == "user-a" and s.language == "ja"
            for s in first.source_spans
        )
        for s in first.source_spans:
            assert s.text == client.get_turn(s.turn_id).text[s.start : s.end]
        assert not recall(client, z, session).source_spans
        with pytest.raises(MemoryHTTPError) as rejected:
            client.add_receipt(question, ReceiptRequest.model_validate(receipt_payload("none", 0)))
        assert rejected.value.status_code == 422
        assert client.list_receipts(question) == []
        with pytest.raises(MemoryHTTPError) as conflict:
            client.add_session(
                SessionRequest.model_validate(
                    session_payload() | {"session_id": session, "reports_delivery": True}
                )
            )
        assert conflict.value.status_code == 409
        assert not client.get_session(session).reports_delivery

        # Same addressee, distinct perception, including the actor's own speech.
        for readers in ([x], [x, y]):
            response_id = add_turn(
                client,
                session,
                actor=x,
                readers=readers,
                text=TEXT,
                mode="direct",
                addressees=["user-a"],
                reply=question,
            )
            restored = client.get_turn(response_id)
            assert restored.addressees == ["user-a"]
            assert restored.perceived_by == readers and restored.reply_to_turn_id == question
            assert restored.text == TEXT
            assert (response_id in {t.turn_id for t in client.list_perceived_turns(y)}) == (
                y in readers
            )
        for readers, addressees in (([x, y], [y]), ([x], ["user-a"])):
            identifier = add_turn(
                client,
                session,
                actor=x,
                readers=readers,
                text=TEXT,
                mode="direct",
                addressees=addressees,
            )
            eventually(partial(extraction_done, client, x, identifier))
            for reader in (x, y, z):
                assert (identifier in {t.turn_id for t in client.list_perceived_turns(reader)}) == (
                    reader in readers
                )
                if reader not in readers:
                    assert all(
                        s.turn_id != identifier
                        for s in recall(client, reader, session).source_spans
                    )
        empty = add_turn(client, session, actor="user-b", readers=[], text="無人の合成発言")
        assert all(
            empty not in {t.turn_id for t in client.list_perceived_turns(c)} for c in (x, y, z)
        )
        for actor in (x, "user-a"):
            own = add_turn(client, session, actor=actor, readers=[x, y], mode="self", text=TEXT)
            eventually(partial(extraction_done, client, y, own))
            values = [m for m in client.list_memories(y) if own in m.source_turn_ids]
            assert values and all(m.kind == "fact" for m in values)
            assert all(m.epistemics.confidence_features.retrieval > 0 for m in values)
        # Voice approval and original anchor travel over HTTP.
        client.register_exemplars(
            x, ExemplarSetInput.model_validate({"exemplars": [exemplar_payload()]})
        )
        assert client.list_exemplars(x)[0].text == exemplar_payload()["text"]
        with httpx.Client(base_url=os.environ["MEMORY_CORE_ST_URL"], timeout=30) as raw:
            unapproved = raw.post(
                f"/characters/{x}/verbatim-anchors",
                json=anchor_payload() | {"turn_id": question, "approved_by": ""},
            )
            assert unapproved.status_code == 422
        anchor = client.register_anchor(
            x, AnchorInput.model_validate(anchor_payload() | {"turn_id": question})
        )
        assert anchor.text == TEXT
        client.replace_profile(
            x,
            ProfileRequest(
                token_limit=1500,
                entries=(
                    {
                        "entry_id": "source",
                        "kind": "turn",
                        "turn_id": question,
                        "start": 0,
                        "end": len(TEXT),
                    },
                ),
            ),
        )
        resident = client.resident(x, ResidentRequest(session_id=session))
        recalled = client.recall(
            x, RecallRequest(session_id=session, format="assembled", turn_id=question)
        )
        assert resident.provision_id != recalled.provision_id
        assert (resident.temperament_version, resident.personality_version) == (
            recalled.temperament_version,
            recalled.personality_version,
        )
        assert (resident.temperament_version, resident.personality_version) == (1, 1)
        provisions = PostgresProvisionStore(audit_url)
        for response, kind in ((resident, "resident"), (recalled, "turn")):
            record = provisions.get(response.provision_id)
            assert record is not None and record.kind == kind
            assert record.provided_at == scenario_time
            assert (record.temperament_version, record.personality_version) == (
                response.temperament_version,
                response.personality_version,
            )
        turn_record = provisions.get(recalled.provision_id)
        assert turn_record is not None
        assert list(turn_record.memory_ids) == [m.id for m in recalled.memories]
        assert [s.text for s in turn_record.source_spans] == [s.text for s in recalled.source_spans]
        assert list(turn_record.verbatim_anchor_ids) == [
            a.anchor_id for a in recalled.verbatim_anchors
        ]
        assert resident.character_profile.entries[0].text == TEXT
        resident_record = provisions.get(resident.provision_id)
        assert resident_record is not None
        assert identity.core_identity is not None
        assert resident_record.profile_entry_ids == ("source",)
        assert resident_record.core_identity_version == identity.core_identity.version
        assert list(resident_record.voice_exemplar_ids) == [
            value.exemplar_id for value in resident.voice_exemplars
        ]
        assert [span.text for span in resident_record.source_spans] == [
            span.text for entry in resident.character_profile.entries for span in entry.source_spans
        ]

        # Providing context does not reinforce. A new self restatement does, through the worker.
        before = next(
            m for m in client.list_memories(x) if question in m.source_turn_ids and m.kind == "fact"
        )
        recall(client, x, session)
        after = next(m for m in client.list_memories(x) if m.id == before.id)
        assert after == before
        restatement = add_turn(
            client,
            session,
            actor=x,
            readers=[x],
            text=TEXT,
            reply=question,
            mode="broadcast",
        )
        eventually(lambda: extraction_done(client, x, restatement))
        reinforced = next(m for m in client.list_memories(x) if m.id == before.id)
        assert restatement in reinforced.source_turn_ids
        assert reinforced.last_reinforced_at == client.get_turn(restatement).occurred_at
        assert reinforced.last_reinforced_at != scenario_time

        # Sensitive text and injected instructions cannot mutate identity or authority.
        command = add_turn(client, session, actor="user-b", readers=[x], text=INSTRUCTION)
        eventually(lambda: extraction_done(client, x, command))
        eventually(lambda: client.get_state(x, session, surface="synthetic-chat").state is not None)
        assert client.get_identity(x) == identity
        with pytest.raises(MemoryHTTPError) as seed:
            client.author_seed(x, SeedInput.model_validate(seed_payload()))
        assert seed.value.status_code == 409
        invalid = turn_payload() | {"session_id": session, "turn_id": "missing-label"}
        del invalid["sensitivity"]
        with httpx.Client(base_url=os.environ["MEMORY_CORE_ST_URL"], timeout=30) as raw:
            assert raw.post("/turns", json=invalid).status_code == 422
        assert client.get_identity(x) == identity

        # User-selected deletion follows actor/addressees, not text or reply.
        control = add_turn(
            client,
            session,
            actor="user-b",
            readers=[x],
            text='{"actor":"user-a"}',
            reply=question,
        )
        eventually(lambda: extraction_done(client, x, control))
        with MemoryClient(base_url=os.environ["MEMORY_CORE_ST_URL"], timeout=30) as unauthorized:
            with pytest.raises(MemoryHTTPError) as forbidden:
                unauthorized.delete(DeletionRequest(request_id="unauthorized", user_id="user-a"))
            assert forbidden.value.status_code == 401
        assert client.get_turn(question).text == TEXT
        targets = {
            t.turn_id
            for t in client.list_perceived_turns(x)
            if t.actor == "user-a" or "user-a" in t.addressees
        }
        result = client.delete(
            DeletionRequest(request_id="delete-" + uuid4().hex, user_id="user-a")
        )
        assert set(result.target_turn_ids) == targets
        assert result.cascade_result is not None
        assert set(result.cascade_result.deleted_turn_ids) == targets
        assert result.cascade_result.invalidated_memories
        assert result.cascade_result.recalculated_memories
        assert recalled.provision_id in result.cascade_result.invalidated_provision_ids
        assert client.get_turn(control).reply_to_turn_id is None

        remaining = next(m for m in client.list_memories(x) if m.id == before.id)
        assert remaining.source_turn_ids == (restatement,)
        assert remaining.epistemics.source_type == reinforced.epistemics.source_type
        assert remaining.epistemics.trust_tier >= reinforced.epistemics.trust_tier
        assert remaining.epistemics.independent_evidence_count == 1
        assert remaining.epistemics.confidence_features == reinforced.epistemics.confidence_features
        for target in targets:
            with pytest.raises(MemoryHTTPError) as missing:
                client.get_turn(target)
            assert missing.value.status_code == 404

        def active_documents_ready() -> bool:
            with psycopg.connect(audit_url) as connection:
                ready = connection.execute(
                    "SELECT count(*) FROM derived_memories m LEFT JOIN recall_documents d "
                    "ON d.owner_id=m.character_id AND d.identifier=m.id AND d.kind='memory' "
                    "WHERE m.character_id=ANY(%s) AND m.search_eligible "
                    "AND m.valid_to IS NULL AND m.superseded_at IS NULL "
                    "AND (d.ready IS DISTINCT FROM TRUE OR d.pending)",
                    ([x, y, z],),
                ).fetchone() == (0,)
            if not ready:
                # The product index retries at now+1s; the ST clock is otherwise frozen.
                with httpx.Client(base_url=os.environ["MEMORY_CORE_ST_URL"], timeout=30) as clock:
                    clock.post("/_st/advance", json={"seconds": 1}).raise_for_status()
            return ready

        eventually(active_documents_ready)
        for character in (x, y, z):

            def recalled_after_deletion(reader: str = character) -> bool:
                try:
                    response = recall(client, reader, session)
                except MemoryHTTPError as error:
                    if error.status_code != 503:
                        raise
                    logging.getLogger(__name__).warning(
                        "Post-deletion snapshot was unavailable (503); caller explicitly retries"
                    )
                    return False
                assert not targets.intersection(s.turn_id for s in response.source_spans)
                return True

            eventually(recalled_after_deletion)
        assert not client.resident(x, ResidentRequest(session_id=session)).character_profile.entries
        assert client.get_identity(x) == identity


@pytest.mark.parametrize("delivery", [True, False])
def test_thought_privacy_and_invalid_registrations(delivery: bool) -> None:
    x, y, z = ("thought-" + uuid4().hex for _ in range(3))
    with new_client() as client:
        for c in (x, y, z):
            register(client, c)
        scenario_time = server_time()
        session = add_session(client, delivery=delivery, started_at=scenario_time)
        thought = add_turn(
            client, session, actor=x, readers=[x], text=TEXT, kind="thought", mode="self"
        )
        eventually(lambda: extraction_done(client, x, thought))
        eventually(lambda: bool(recall(client, x, session).source_spans))
        assert not recall(client, y, session).source_spans
        assert not recall(client, z, session).source_spans
        assert not client.list_memories(y) and not client.list_memories(z)
        with pytest.raises(MemoryHTTPError) as rejected:
            client.add_receipt(thought, ReceiptRequest.model_validate(receipt_payload("none", 0)))
        assert rejected.value.status_code == 422 and not client.list_receipts(thought)
        for actor, readers in (("user-a", [x]), (x, [x, y]), (x, [])):
            with pytest.raises(MemoryHTTPError) as invalid:
                add_turn(client, session, actor=actor, readers=readers, text=TEXT, kind="thought")
            assert invalid.value.status_code == 422


def test_delivery_deadline_and_saved_outputs_follow_receipts() -> None:
    x, y = ("delivery-" + uuid4().hex for _ in range(2))
    with (
        new_client() as client,
        httpx.Client(
            base_url=os.environ["MEMORY_CORE_ST_URL"],
            timeout=30,
        ) as control,
    ):
        for c in (x, y):
            register(client, c)
        scenario_time = server_time()
        session = add_session(client, delivery=True, started_at=scenario_time)
        identifier = add_turn(client, session, actor=x, readers=[x, y], text=TEXT)
        eventually(lambda: extraction_done(client, x, identifier))
        eventually(lambda: client.get_extraction(y, identifier).delivery_basis == "pending")
        assert not recall(client, y, session).source_spans
        assert any(identifier in m.source_turn_ids for m in client.list_memories(x))
        control.post("/_st/advance", json={"seconds": 599}).raise_for_status()
        assert not recall(client, y, session).source_spans
        control.post("/_st/advance", json={"seconds": 1}).raise_for_status()
        eventually(lambda: extraction_done(client, y, identifier))
        eventually(lambda: bool(recall(client, y, session).source_spans))
        assert client.get_extraction(y, identifier).delivery_basis == "assumed_full"
        client.replace_profile(
            y,
            ProfileRequest(
                token_limit=1500,
                entries=(
                    {
                        "entry_id": "delivered",
                        "kind": "turn",
                        "turn_id": identifier,
                        "start": 0,
                        "end": len(TEXT),
                    },
                ),
            ),
        )
        assert client.resident(y, ResidentRequest(session_id=session)).character_profile.entries
        for status, end in (("partial", 3), ("none", 0)):
            client.add_receipt(
                identifier, ReceiptRequest.model_validate(receipt_payload(status, end))
            )
            version = len(client.list_receipts(identifier))

            def corrected(expected: int = version) -> bool:
                return client.get_extraction(y, identifier).receipt_version == expected and (
                    extraction_done(client, y, identifier)
                )

            eventually(corrected)
            values = client.list_memories(y)
            assert all(m.content == TEXT[:end] for m in values)

            def limited(limit: int = end) -> bool:
                return all(s.end <= limit for s in recall(client, y, session).source_spans)

            eventually(limited)
            assert not client.resident(
                y, ResidentRequest(session_id=session)
            ).character_profile.entries
            if status == "none":
                assert values == [] and not recall(client, y, session).source_spans
            else:
                assert values
            assert client.get_turn(identifier).text == TEXT
            assert any(
                identifier in m.source_turn_ids and m.content == TEXT
                for m in client.list_memories(x)
            )
        assert client.get_extraction(y, identifier).deadline == (
            client.get_turn(identifier).recorded_at + timedelta(minutes=10)
        )
