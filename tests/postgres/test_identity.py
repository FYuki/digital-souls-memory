from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.types.json import Jsonb

from digital_souls_memory.adapters.postgres_identity import PostgresIdentityStore
from digital_souls_memory.api import create_app
from tests.identity_support import (
    ASPECT_KEYS,
    DESCRIPTION,
    INSTRUCTION,
    REGISTERED,
    ROOT,
    SEED_PATH,
    TEMPERAMENT,
    aspects,
    core_payload,
    seed_payload,
)
from tests.it1.identity_fakes import FakeLLMGateway, FakeReviewer

pytestmark = pytest.mark.postgres


def make_client(url: str) -> TestClient:
    return TestClient(
        create_app(
            identity_store=PostgresIdentityStore(url),
            llm_gateway=FakeLLMGateway(),
            reviewer=FakeReviewer(),
            clock=lambda: REGISTERED,
        )
    )


def register_identity(url: str) -> None:
    with make_client(url) as client:
        assert client.post(ROOT + "/core-identities", json=core_payload()).is_success
        assert client.post(ROOT + "/temperaments", json={"description": TEMPERAMENT}).is_success
        assert client.post(SEED_PATH, json=seed_payload()).is_success


def test_all_profile_attributes_survive_new_store_and_current_read(app_database_url: str) -> None:
    register_identity(app_database_url)
    restored = PostgresIdentityStore(app_database_url)
    value = restored.get_profile("char-a", 1)
    assert value is not None
    assert value.character_id == "char-a"
    assert dict(value.aspects) == aspects()
    assert value.description == DESCRIPTION
    assert value.renderer_version == "synthetic-renderer-v1"
    assert value.version == 1
    assert value.temperament_version == 1
    assert value.valid_from == REGISTERED
    assert value.valid_to is None
    assert tuple(value.evidence_belief_ids) == ()
    assert value.change_kind == "author_seed"
    core, temperament, profile = restored.get_current("char-a", REGISTERED)
    assert profile == value
    assert core is not None and tuple(core.values) == ("誠実",)
    assert core.registered_at == REGISTERED
    assert temperament is not None and temperament.description == TEMPERAMENT
    assert temperament.registered_at == REGISTERED
    assert restored.get_current("char-b", REGISTERED) == (None, None, None)


def test_parallel_core_registration_uses_distinct_versions_and_preserves_history(
    app_database_url: str,
) -> None:
    register_identity(app_database_url)
    with make_client(app_database_url) as client:
        assert client.post(ROOT + "/core-identities", json=core_payload()).is_success
        old = client.get(ROOT + "/core-identities/2").json()
    barrier = Barrier(2)

    def register(label: str) -> int:
        with make_client(app_database_url) as client:
            barrier.wait(timeout=10)
            response = client.post(
                ROOT + "/core-identities", json=core_payload() | {"values": [label]}
            )
            assert response.is_success
            return int(response.json()["version"])

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(register, label) for label in ("勇気", "思いやり")]
        versions = [future.result(timeout=20) for future in futures]
    assert len(set(versions)) == 2
    assert all(version > 2 for version in versions)
    with make_client(app_database_url) as client:
        assert client.get(ROOT + "/core-identities/2").json() == old
        assert {
            tuple(client.get(f"{ROOT}/core-identities/{v}").json()["values"]) for v in versions
        } == {
            ("勇気",),
            ("思いやり",),
        }


def test_parallel_author_seed_publishes_only_one_profile(app_database_url: str) -> None:
    with make_client(app_database_url) as client:
        assert client.post(ROOT + "/temperaments", json={"description": TEMPERAMENT}).is_success
    barrier = Barrier(2)

    def seed() -> int:
        with make_client(app_database_url) as client:
            barrier.wait(timeout=10)
            return client.post(SEED_PATH, json=seed_payload()).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(seed) for _ in range(2)]
        statuses = [future.result(timeout=20) for future in futures]
    assert sum(200 <= status < 300 for status in statuses) == 1
    assert statuses.count(409) == 1
    restored = PostgresIdentityStore(app_database_url)
    assert restored.get_profile("char-a", 1) is not None
    assert restored.get_profile("char-a", 2) is None


@pytest.mark.parametrize(
    "table,column",
    [
        ("core_identities", "values"),
        ("temperaments", "description"),
        ("personality_profiles", "description"),
    ],
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_nonowner_cannot_rewrite_or_delete_identity_history(
    app_database_url: str, table: str, column: str, operation: str
) -> None:
    register_identity(app_database_url)
    with make_client(app_database_url) as client:
        assert client.post(
            ROOT + "/core-identities", json=core_payload() | {"values": ["勇気"]}
        ).is_success
        assert client.post(ROOT + "/temperaments", json={"description": "静かに喜ぶ。"}).is_success
        before = [
            client.get(f"{ROOT}/{resource}/1").json()
            for resource in ("core-identities", "temperaments", "personality-profiles")
        ]
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        assert connection.execute(
            "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
        ).fetchone() == (False,)
        assert connection.execute(
            "SELECT tableowner <> current_user FROM pg_tables "
            "WHERE schemaname='public' AND tablename=%s",
            (table,),
        ).fetchone() == (True,)
        statement = (
            sql.SQL("UPDATE {} SET {} = %s WHERE character_id='char-a' AND version=1").format(
                sql.Identifier(table), sql.Identifier(column)
            )
            if operation == "UPDATE"
            else sql.SQL("DELETE FROM {} WHERE character_id='char-a' AND version=1").format(
                sql.Identifier(table)
            )
        )
        params = (["changed"],) if column == "values" else ("changed",)
        with pytest.raises(psycopg.Error):
            connection.execute(statement, params if operation == "UPDATE" else ())
    with make_client(app_database_url) as client:
        assert [
            client.get(f"{ROOT}/{resource}/1").json()
            for resource in ("core-identities", "temperaments", "personality-profiles")
        ] == before


@pytest.mark.parametrize("table", ["core_identities", "temperaments", "personality_profiles"])
def test_duplicate_character_version_is_rejected_by_database(
    app_database_url: str, table: str
) -> None:
    register_identity(app_database_url)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                sql.SQL("INSERT INTO {} SELECT * FROM {}").format(
                    sql.Identifier(table), sql.Identifier(table)
                )
            )
        assert connection.execute(
            sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        ).fetchone() == (1,)


def test_database_rejects_second_seed_even_with_a_new_version(app_database_url: str) -> None:
    register_identity(app_database_url)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        with pytest.raises(psycopg.Error):
            connection.execute(
                "INSERT INTO personality_profiles SELECT "
                "(jsonb_populate_record(NULL::personality_profiles, "
                "to_jsonb(p) || '{\"version\": 2}'::jsonb)).* FROM personality_profiles p"
            )
        assert connection.execute("SELECT count(*) FROM personality_profiles").fetchone() == (1,)


@pytest.mark.parametrize(
    "invalid",
    [
        {key: 0.5 for key in ASPECT_KEYS if key != "Volatility"},
        aspects() | {"Unknown": 0.5},
        aspects() | {"Volatility": -0.01},
        aspects() | {"Withdrawal": 1.01},
        aspects() | {"Openness": "NaN"},
        aspects() | {"Openness": "Infinity"},
        aspects() | {"Intellect": True},
    ],
)
def test_database_rejects_invalid_aspects_independently_of_domain(
    app_database_url: str, invalid: dict[str, object]
) -> None:
    register_identity(app_database_url)
    with psycopg.connect(app_database_url, autocommit=True, connect_timeout=5) as connection:
        connection.execute(
            "INSERT INTO core_identities SELECT (jsonb_populate_record(NULL::core_identities, "
            'to_jsonb(c) || \'{"character_id": "char-b"}\'::jsonb)).* FROM core_identities c'
        )
        connection.execute(
            "INSERT INTO temperaments SELECT (jsonb_populate_record(NULL::temperaments, "
            'to_jsonb(t) || \'{"character_id": "char-b"}\'::jsonb)).* FROM temperaments t'
        )
        with pytest.raises(psycopg.Error):
            connection.execute(
                "INSERT INTO personality_profiles SELECT "
                "(jsonb_populate_record(NULL::personality_profiles, "
                "to_jsonb(p) || jsonb_build_object("
                "'character_id', 'char-b', 'aspects', %s::jsonb))).* "
                "FROM personality_profiles p WHERE character_id='char-a'",
                (Jsonb(invalid),),
            )
        assert connection.execute(
            "SELECT count(*) FROM personality_profiles WHERE character_id='char-b'"
        ).fetchone() == (0,)
        connection.execute(
            "INSERT INTO personality_profiles SELECT "
            "(jsonb_populate_record(NULL::personality_profiles, "
            "to_jsonb(p) || jsonb_build_object('character_id', 'char-b', 'aspects', %s::jsonb))).* "
            "FROM personality_profiles p WHERE character_id='char-a'",
            (Jsonb(aspects()),),
        )
        assert connection.execute(
            "SELECT count(*) FROM personality_profiles WHERE character_id='char-b'"
        ).fetchone() == (1,)


def test_transaction_failure_rolls_back_candidate_and_allows_later_registration(
    app_database_url: str,
) -> None:
    register_identity(app_database_url)
    store = PostgresIdentityStore(app_database_url)
    original = store.get_core("char-a", 1)
    assert original is not None
    with pytest.raises(RuntimeError, match="synthetic abort"):
        with store.transaction("char-a", source="registration") as transaction:
            transaction.add_core(replace(original, version=2, values=("勇気",)))
            raise RuntimeError("synthetic abort")
    assert store.get_core("char-a", 2) is None
    assert store.get_core("char-a", 1) == original
    with make_client(app_database_url) as client:
        current = client.get(ROOT + "/identity/current").json()
        assert current["core_identity"]["version"] == original.version
        assert client.post(
            ROOT + "/core-identities", json=core_payload() | {"values": ["勇気"]}
        ).is_success


@pytest.mark.parametrize("source", ["memory", "reflection", "dream", "fact", "store"])
@pytest.mark.parametrize("entity", ["core", "temperament", "profile"])
def test_store_transaction_does_not_bypass_registration_policy(
    app_database_url: str, source: str, entity: str
) -> None:
    register_identity(app_database_url)
    store = PostgresIdentityStore(app_database_url)
    original = store.get_core("char-a", 1)
    assert original is not None
    with pytest.raises((ValueError, PermissionError)):
        with store.transaction("char-a", source=source) as transaction:
            if entity == "core":
                transaction.add_core(replace(original, version=2))
            elif entity == "temperament":
                temperament = store.get_temperament("char-a", 1)
                assert temperament is not None
                transaction.add_temperament(replace(temperament, version=2))
            else:
                profile = store.get_profile("char-a", 1)
                assert profile is not None
                transaction.add_profile(replace(profile, version=2))
    assert store.get_core("char-a", 2) is None
    assert store.get_temperament("char-a", 2) is None
    assert store.get_profile("char-a", 2) is None


@pytest.mark.parametrize("entity", ["core", "temperament", "profile"])
def test_transaction_cannot_write_outside_its_character_lock(
    app_database_url: str, entity: str
) -> None:
    register_identity(app_database_url)
    store = PostgresIdentityStore(app_database_url)
    with pytest.raises(PermissionError):
        with store.transaction("char-b", source="registration") as tx:
            if entity == "core":
                core = store.get_core("char-a", 1)
                assert core is not None
                tx.add_core(replace(core, version=2))
            elif entity == "temperament":
                temperament = store.get_temperament("char-a", 1)
                assert temperament is not None
                tx.add_temperament(replace(temperament, version=2))
            else:
                profile = store.get_profile("char-a", 1)
                assert profile is not None
                tx.add_profile(replace(profile, version=2))
    assert store.get_core("char-a", 2) is None
    assert store.get_temperament("char-a", 2) is None
    assert store.get_profile("char-a", 2) is None


def test_default_factory_reads_persisted_identity_and_sql_text_is_only_data(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as environment:
        environment.setenv("MEMORY_DATABASE_URL", app_database_url)
        with TestClient(create_app(clock=lambda: REGISTERED)) as client:
            response = client.post(
                ROOT + "/core-identities", json=core_payload() | {"values": [INSTRUCTION]}
            )
            assert response.is_success
        with TestClient(create_app(clock=lambda: REGISTERED)) as client:
            assert client.get(ROOT + "/core-identities/1").json()["values"] == [INSTRUCTION]
            assert client.post(ROOT + "/core-identities", json=core_payload()).is_success


def test_commit_failure_never_publishes_seed_and_later_retry_succeeds(
    database_url: str, app_database_url: str
) -> None:
    with make_client(app_database_url) as client:
        assert client.post(ROOT + "/temperaments", json={"description": TEMPERAMENT}).is_success
        before = client.get(ROOT + "/identity/current").json()
        with psycopg.connect(database_url, autocommit=True, connect_timeout=5) as owner:
            owner.execute(
                "CREATE FUNCTION synthetic_reject_identity_commit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'synthetic commit failure'; END $$"
            )
            try:
                owner.execute(
                    "CREATE CONSTRAINT TRIGGER synthetic_identity_commit "
                    "AFTER INSERT ON personality_profiles DEFERRABLE INITIALLY DEFERRED "
                    "FOR EACH ROW EXECUTE FUNCTION synthetic_reject_identity_commit()"
                )
                try:
                    assert client.post(SEED_PATH, json=seed_payload()).status_code == 503
                    assert client.get(ROOT + "/identity/current").json() == before
                    assert client.get(ROOT + "/personality-profiles/1").status_code == 404
                    assert owner.execute(
                        "SELECT count(*) FROM personality_profiles"
                    ).fetchone() == (0,)
                finally:
                    owner.execute("DROP TRIGGER synthetic_identity_commit ON personality_profiles")
            finally:
                owner.execute("DROP FUNCTION synthetic_reject_identity_commit()")
        assert client.post(SEED_PATH, json=seed_payload()).is_success
