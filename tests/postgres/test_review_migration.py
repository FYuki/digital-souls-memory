"""Revision 0011 upgrades synthetic 0010 data without runtime compatibility paths."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.memory_records import memory_record
from digital_souls_memory.adapters.postgres_memory import copy_payload
from digital_souls_memory.adapters.postgres_provision import PostgresProvisionStore
from digital_souls_memory.adapters.recall_records import provision_payload
from digital_souls_memory.api import create_app
from tests.evidence_support import OCCURRED, RECORDED
from tests.identity_support import aspects
from tests.memory_support import memory
from tests.postgres.test_deletion import HEADERS
from tests.postgres.test_deletion import deletion_url as deletion_url

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures("deletion_url")]


def seed_0010(conn: psycopg.Connection[tuple[object, ...]], *, shared_source: bool = False) -> None:
    conn.execute(
        'INSERT INTO core_identities(character_id,"values",prohibitions,version,registered_at) '
        "VALUES ('char-b',ARRAY['合成価値'],ARRAY[]::text[],1,%s),"
        "('char-c',ARRAY['合成価値'],ARRAY[]::text[],1,%s)",
        (RECORDED, RECORDED),
    )
    conn.execute(
        "INSERT INTO sessions(session_id,started_at,ended_at,surface,reports_delivery) "
        "VALUES ('session-1',%s,NULL,'synthetic-chat',false)",
        (OCCURRED,),
    )
    for identifier, owner in (("turn-1", "char-b"), ("turn-2", "char-c")):
        conn.execute(
            "INSERT INTO turns(turn_id,session_id,surface,actor,kind,text,perceived_by,"
            "addressees,address_mode,language,register,sensitivity,classifier_version,"
            "occurred_at,recorded_at) VALUES (%s,'session-1','synthetic-chat','user-old',"
            "'utterance','合成原文',%s,ARRAY[]::text[],'broadcast','ja','{}','synthetic',"
            "'synthetic-v1',%s,%s)",
            (
                identifier,
                ["char-b", "char-c"] if shared_source and owner == "char-b" else [owner],
                OCCURRED,
                RECORDED,
            ),
        )
        conn.execute(
            "INSERT INTO verbatim_anchors(character_id,anchor_id,turn_id,kind,reason,"
            "approved_by,registered_at) VALUES (%s,'anchor-1',%s,'commitment','合成理由',"
            "'synthetic-author',%s)",
            (owner, identifier, RECORDED),
        )
    payload = {
        "provision_id": "old-provision",
        "kind": "turn",
        "character_id": "char-c",
        "session_id": "session-1",
        "surface": "synthetic-chat",
        "query": "合成検索",
        "turn_id": None,
        "memory_ids": [],
        "source_spans": [],
        "verbatim_anchor_ids": ["anchor-1"],
        "voice_exemplar_ids": [],
        "motivation_ids": [],
        "temperament_version": None,
        "personality_version": None,
        "scores": [
            {
                "ref": {"owner_id": "char-c", "memory_id": None, "anchor_id": "anchor-1"},
                "semantic": 1.0,
                "lexical": 1.0,
                "recency": 1.0,
                "importance": 1.0,
                "self_relevance": 1.0,
                "trust": 1.0,
                "composite": 1.0,
                "relevance": 1.0,
            }
        ],
        "duration_ms": 0.0,
        "provided_at": RECORDED.isoformat(),
    }
    conn.execute(
        "INSERT INTO provision_records(provision_id,payload) VALUES ('old-provision',%s)",
        (Jsonb(payload),),
    )
    conn.execute(
        "INSERT INTO recall_embeddings(content_digest,model,revision,digest,dimensions,embedding) "
        "VALUES ('synthetic-orphan','synthetic','v1','synthetic',1024,%s::vector)",
        (str([1.0] + [0.0] * 1023),),
    )


@pytest.mark.parametrize("existing", [False, True], ids=["empty", "0010-with-data"])
def test_review_migration_preserves_sources_and_upgrades_actor_and_provision_semantics(
    database_url: str, existing: bool
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0010" if existing else "base")
    with psycopg.connect(database_url, autocommit=True) as conn:
        before = {}
        if existing:
            seed_0010(conn)
            before = {
                table: conn.execute(
                    sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))
                ).fetchall()
                for table in ("sessions", "turns", "core_identities", "verbatim_anchors")
            }
        try:
            command.upgrade(config, "0011")
        finally:
            # The shared DB fixture tears down from 0005 even when the RED upgrade fails.
            if conn.execute("SELECT version_num FROM alembic_version").fetchone() is None:
                command.upgrade(config, "0010")
        command.upgrade(config, "0011")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0011",)
        for table, rows in before.items():
            assert (
                conn.execute(
                    sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))
                ).fetchall()
                == rows
            )
        assert (
            conn.execute(
                "SELECT content_digest FROM recall_embeddings "
                "WHERE content_digest='synthetic-orphan'"
            ).fetchall()
            == []
        )
        command.downgrade(config, "0010")
        command.upgrade(config, "0011")
        for table, rows in before.items():
            assert (
                conn.execute(
                    sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))
                ).fetchall()
                == rows
            )
    if existing:
        provisions = PostgresProvisionStore(database_url)
        saved = provisions.get("old-provision")
        assert saved is not None and saved.verbatim_anchor_ids == ("anchor-1",)
        with TestClient(create_app(clock=lambda: RECORDED)) as client:
            for resource, payload in (
                ("core-identities", {"values": ["合成価値"], "prohibitions": []}),
                ("temperaments", {"description": "合成気質"}),
            ):
                assert client.post(f"/characters/user-old/{resource}", json=payload).is_client_error
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": "delete-after-migration", "target_turn_ids": ["turn-1"]},
            )
            assert response.status_code == 200
            assert (
                "old-provision"
                not in response.json()["cascade_result"]["invalidated_provision_ids"]
            )
            assert provisions.get("old-provision") == saved


def seed_0010_resident(conn: psycopg.Connection[tuple[object, ...]], kind: str) -> None:
    seed_0010(conn, shared_source=kind == "memory")
    if kind == "memory":
        for owner, source in (("char-b", "turn-1"), ("char-c", "turn-2")):
            value = replace(
                memory(),
                character_id=owner,
                source_turn_ids=(source,),
                shared_with=("char-c",) if owner == "char-b" else (),
            )
            record = memory_record(value)
            record["epistemics"] = Jsonb(record["epistemics"])
            record["identity_links"] = Jsonb(record["identity_links"])
            # 0010 has no temporal_origin column; do not use the current store writer.
            with conn.transaction():
                conn.execute(
                    sql.SQL("INSERT INTO derived_memories ({}) VALUES ({})").format(
                        sql.SQL(",").join(map(sql.Identifier, record)),
                        sql.SQL(",").join(sql.Placeholder() for _ in record),
                    ),
                    tuple(record.values()),
                )
                conn.execute(
                    "INSERT INTO memory_sources(character_id,memory_id,turn_id,position) "
                    "VALUES (%s,%s,%s,0)",
                    (owner, value.id, source),
                )
                conn.execute(
                    "INSERT INTO memory_baselines(character_id,memory_id,payload) "
                    "VALUES (%s,%s,%s)",
                    (owner, value.id, Jsonb(copy_payload(value))),
                )
                conn.execute(
                    "INSERT INTO memory_baseline_sources(character_id,memory_id,turn_id,position) "
                    "VALUES (%s,%s,%s,0)",
                    (owner, value.id, source),
                )
        conn.execute(
            "INSERT INTO character_profiles(character_id,version,token_limit) "
            "VALUES ('char-c',1,100)"
        )
        conn.execute(
            "INSERT INTO profile_entries(character_id,entry_id,position,kind,owner_id,memory_id) "
            "VALUES ('char-c','shared-memory',0,'memory','char-b',%s)",
            (memory().id,),
        )
    else:
        for owner, source in (("char-b", "turn-1"), ("char-c", "turn-2")):
            conn.execute(
                "INSERT INTO temperaments(character_id,description,version,registered_at) "
                "VALUES (%s,'合成気質',1,%s)",
                (owner, RECORDED),
            )
            conn.execute(
                "INSERT INTO personality_profiles(character_id,aspects,description,"
                "renderer_version,temperament_version,version,valid_from,"
                "evidence_belief_ids,change_kind) "
                "VALUES (%s,%s,'合成人格','synthetic-v1',1,1,%s,ARRAY[]::text[],'author_seed')",
                (owner, Jsonb(aspects()), RECORDED),
            )
            conn.execute(
                "INSERT INTO voice_exemplars(character_id,exemplar_id,text,source,turn_id,"
                "situation_tags,emotion_tags,register,trait_version,approved_by,registered_at) "
                "VALUES (%s,'voice-1','合成原文','turn_id',%s,ARRAY[]::text[],ARRAY[]::text[],"
                "'{}',1,'synthetic-author',%s)",
                (owner, source, RECORDED),
            )
    row = conn.execute(
        "SELECT payload FROM provision_records WHERE provision_id='old-provision'"
    ).fetchone()
    assert row is not None
    payload = cast(dict[str, object], row[0]) | {
        "provision_id": "old-resident",
        "kind": "resident",
        "memory_ids": [memory().id] if kind == "memory" else [],
        "verbatim_anchor_ids": [],
        "voice_exemplar_ids": ["voice-1"] if kind == "voice" else [],
        "profile_entry_ids": ["shared-memory"] if kind == "memory" else [],
        "core_identity_version": 1,
        "temperament_version": 1 if kind == "voice" else None,
        "personality_version": 1 if kind == "voice" else None,
        "scores": [
            {
                "ref": {
                    "kind": "profile" if kind == "memory" else "voice",
                    "identifier": "shared-memory" if kind == "memory" else "voice-1",
                },
                "lexical": None,
                "semantic": None,
                "recency": None,
                "importance": None,
                "self_relevance": None,
                "trust": None,
                "composite": None,
                "relevance": None,
            }
        ],
    }
    conn.execute(
        "INSERT INTO provision_records(provision_id,payload) VALUES ('old-resident',%s)",
        (Jsonb(payload),),
    )


@pytest.mark.parametrize("kind", ["memory", "voice"])
def test_review_migration_restores_resident_owners_and_deletes_only_matching_owner(
    database_url: str, kind: str
) -> None:
    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0010")
    provisions = PostgresProvisionStore(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        seed_0010_resident(conn, kind)
        tables = (
            "sessions",
            "turns",
            "core_identities",
            "temperaments",
            "personality_profiles",
            "verbatim_anchors",
            "voice_exemplars",
            "derived_memories",
            "memory_sources",
            "memory_baseline_sources",
            "character_profiles",
            "profile_entries",
        )
        before = {
            table: conn.execute(
                sql.SQL("SELECT * FROM {} ORDER BY 1,2").format(sql.Identifier(table))
            ).fetchall()
            for table in tables
        }
        old_payloads = conn.execute(
            "SELECT provision_id,payload FROM provision_records ORDER BY provision_id"
        ).fetchall()
        original_row = conn.execute(
            "SELECT payload FROM provision_records WHERE provision_id='old-resident'"
        ).fetchone()
        assert original_row is not None
        original = cast(dict[str, object], original_row[0])
        baselines = conn.execute(
            "SELECT character_id,memory_id,payload FROM memory_baselines ORDER BY 1,2"
        ).fetchall()
        expected_ref = (
            kind,
            "char-b" if kind == "memory" else "char-c",
            memory().id if kind == "memory" else "voice-1",
        )
        restored = None
        for _ in range(2):
            command.upgrade(config, "0011")
            saved = provisions.get("old-resident")
            assert saved is not None and saved.kind == "resident"
            assert [(v.kind, v.owner_id, v.identifier) for v in saved.provided_refs] == [
                expected_ref
            ]
            assert provision_payload(saved) | {"provided_refs": []} == original | {
                "provided_refs": []
            }
            if restored is not None:
                assert saved == restored
            restored = saved
            for table, rows in before.items():
                assert (
                    conn.execute(
                        sql.SQL("SELECT * FROM {} ORDER BY 1,2").format(sql.Identifier(table))
                    ).fetchall()
                    == rows
                )
            assert (
                conn.execute(
                    "SELECT character_id,memory_id,payload FROM memory_baselines ORDER BY 1,2"
                ).fetchall()
                == baselines
            )
            command.downgrade(config, "0010")
            assert (
                conn.execute(
                    "SELECT provision_id,payload FROM provision_records ORDER BY provision_id"
                ).fetchall()
                == old_payloads
            )
        command.upgrade(config, "0011")
        assert provisions.get("old-resident") == restored
    # No direct turn_id or source_spans can hide errors in the owned-reference match.
    assert restored is not None and restored.turn_id is None and restored.source_spans == ()
    other_source, actual_source = ("turn-2", "turn-1") if kind == "memory" else ("turn-1", "turn-2")
    with TestClient(create_app(clock=lambda: RECORDED)) as client:
        for source, expected in ((other_source, False), (actual_source, True)):
            response = client.post(
                "/deletion-requests",
                headers=HEADERS,
                json={"request_id": f"delete-{source}", "target_turn_ids": [source]},
            )
            assert response.status_code == 200, response.json()
            assert response.json()["status"] == "done"
            invalidated = response.json()["cascade_result"]["invalidated_provision_ids"]
            assert (restored.provision_id in invalidated) == expected
            assert provisions.get(restored.provision_id) == (None if expected else restored)


def test_ambiguous_legacy_provision_owner_rolls_back_entire_upgrade(database_url: str) -> None:
    from sqlalchemy.exc import DBAPIError

    config = Config(str(Path(__file__).parents[2] / "alembic.ini"))
    command.downgrade(config, "0010")
    with psycopg.connect(database_url, autocommit=True) as conn:
        seed_0010(conn)
        original = conn.execute(
            "SELECT payload FROM provision_records WHERE provision_id='old-provision'"
        ).fetchone()
        assert original is not None
        payload = original[0]
        other = payload["scores"][0] | {
            "ref": {"owner_id": "char-b", "memory_id": None, "anchor_id": "anchor-1"}
        }
        ambiguous = payload | {"scores": [*payload["scores"], other]}
        conn.execute("UPDATE provision_records SET payload=%s", (Jsonb(ambiguous),))
        with pytest.raises(DBAPIError):
            command.upgrade(config, "0011")
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == ("0010",)
        assert conn.execute("SELECT to_regclass('actor_kinds')").fetchone() == (None,)
        assert conn.execute(
            "SELECT payload FROM provision_records WHERE provision_id='old-provision'"
        ).fetchone() == (ambiguous,)
        assert conn.execute(
            "SELECT count(*) FROM recall_embeddings WHERE content_digest='synthetic-orphan'"
        ).fetchone() == (1,)
        conn.execute("UPDATE provision_records SET payload=%s", (Jsonb(payload),))
        command.upgrade(config, "0011")
