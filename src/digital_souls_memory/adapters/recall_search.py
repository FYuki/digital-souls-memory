"""Index candidate retrieval and evidence reads owned by one search snapshot."""

from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

import psycopg
from psycopg.rows import DictRow

from digital_souls_memory.adapters.recall_records import read_material
from digital_souls_memory.evidence.materials import material_decision
from digital_souls_memory.recall.domain import CandidateMaterial, VectorSpace
from digital_souls_memory.recall.lexical import lexical_score, lexical_terms


def search_rows(
    connection: psycopg.Connection[DictRow],
    *,
    character_id: str,
    text: str,
    vector: str,
    space: VectorSpace,
    now: datetime,
    timeout: timedelta,
    limit: int,
) -> tuple[DictRow, ...]:
    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    materials: OrderedDict[str, CandidateMaterial | None] = OrderedDict()
    batch_size = limit
    cache_capacity = 2 * limit
    found: dict[tuple[str, str, str], DictRow] = {}
    terms = lexical_terms(text)
    for lexical in (False, True):
        if lexical and not terms:
            continue
        width = limit
        while True:
            accepted: dict[tuple[str, str, str], DictRow] = {}
            scanned = 0
            with _candidates(
                connection, terms, vector, space, width, lexical, character_id, now
            ) as cursor:
                while len(accepted) < limit:
                    rows = cursor.fetchmany(batch_size)
                    if not rows:
                        break
                    scanned += len(rows)
                    for row in rows:
                        key = (row["owner_id"], row["kind"], row["identifier"])
                        eligible = found.get(key)
                        if eligible is None:
                            eligible = _eligible(
                                connection,
                                row,
                                materials,
                                cache_capacity,
                                character_id,
                                now,
                                timeout,
                            )
                        if eligible is not None:
                            accepted[key] = eligible | {
                                "lexical": lexical_score(row["search_text"], terms)
                            }
                            if len(accepted) == limit:
                                break
                    # Release the preceding batch before fetching its replacement.
                    del rows
            if len(accepted) == limit or scanned < width:
                found.update(accepted)
                break
            width *= 2
    return tuple(found.values())


@contextmanager
def _candidates(
    connection: psycopg.Connection[DictRow],
    terms: tuple[str, ...],
    vector: str,
    space: VectorSpace,
    limit: int,
    lexical: bool,
    character_id: str,
    now: datetime,
) -> Iterator[psycopg.ServerCursor[DictRow]]:
    matches = ["search_text LIKE likequery(%s)" for _ in terms]
    predicate = " AND (" + " OR ".join(matches) + ")" if lexical else ""
    order = (
        " + ".join(f"({match})::int" for match in matches) + " DESC"
        if lexical
        else "embedding <=> %s::vector"
    )
    permission = (
        "((d.kind='anchor' AND d.owner_id=%s) OR (d.kind='memory' AND EXISTS("
        "SELECT 1 FROM derived_memories m WHERE m.character_id=d.owner_id AND m.id=d.identifier "
        "AND (m.character_id=%s OR (m.kind IN ('fact','preference') AND %s=ANY(m.shared_with))) "
        "AND m.search_eligible AND m.valid_from<=%s AND (m.valid_to IS NULL OR %s<m.valid_to) "
        "AND (m.superseded_at IS NULL OR %s<m.superseded_at))))"
    )
    permission_params = (character_id, character_id, character_id, now, now, now)
    params = (
        vector,
        *permission_params,
        space.model,
        space.revision,
        space.digest,
        space.dimensions,
        *(terms if lexical else permission_params),
        *(terms if lexical else (vector,)),
        limit,
    )
    previous = None
    if lexical:
        setting = connection.execute("SHOW enable_seqscan").fetchone()
        assert setting is not None
        previous = setting["enable_seqscan"]
        # All documents can match; the lexical predicate must still use the stored GIN index.
        connection.execute("SELECT set_config('enable_seqscan','off',true)")
    query = (
        "SELECT d.*,1-(d.embedding <=> %s::vector) semantic," + permission + " AS allowed FROM "
        "(SELECT owner_id,kind,identifier FROM recall_documents d "
        "WHERE ready AND model=%s AND revision=%s AND digest=%s AND dimensions=%s"
        + (predicate if lexical else " AND " + permission)
        + " ORDER BY "
        + order
        + " LIMIT %s) c "
        # Keep payload hydration as primary-key lookups after the index limit.
        "CROSS JOIN LATERAL (SELECT * FROM recall_documents d WHERE "
        "d.owner_id=c.owner_id AND d.kind=c.kind AND d.identifier=c.identifier OFFSET 0) d"
    )
    with connection.cursor("recall_candidates", scrollable=False, withhold=False) as cursor:
        cursor.execute(query, params)
        if lexical:
            connection.execute("SELECT set_config('enable_seqscan',%s,true)", (previous,))
        yield cursor


def _eligible(
    connection: psycopg.Connection[DictRow],
    row: DictRow,
    materials: OrderedDict[str, CandidateMaterial | None],
    cache_capacity: int,
    character_id: str,
    now: datetime,
    timeout: timedelta,
) -> DictRow | None:
    if not row["allowed"]:
        return None
    if row["kind"] == "memory":
        source_ids = row["payload"]["source_turn_ids"]
    else:
        source_ids = [row["payload"]["turn_id"]]
    source_times: dict[str, datetime] = {}
    source_text: dict[str, str] = {}
    for dependency in row["dependencies"]["values"]:
        turn_id = dependency["turn_id"]
        if turn_id not in materials:
            if len(materials) == cache_capacity:
                materials.popitem(last=False)
            materials[turn_id] = read_material(connection, turn_id)
        materials.move_to_end(turn_id)
        material = materials[turn_id]
        if material is None:
            return None
        # SQL no longer owns perception/delivery rules; both readers use the domain decision.
        for reader in dict.fromkeys((dependency["character_id"], character_id)):
            if material.turn.kind == "thought" and material.turn.actor != reader:
                return None
            decision = material_decision(
                character_id=reader,
                session=material.session,
                turn=material.turn,
                actor_is_character=material.actor_is_character,
                receipts=material.receipt_state,
                now=now,
                delivery_timeout=timeout,
            )
            if decision.text is None or dependency["used_end"] > len(decision.text):
                return None
        if turn_id in source_ids:
            source_times[turn_id] = material.turn.occurred_at
            if row["kind"] == "anchor":
                source_text["source_text"] = material.turn.text
    if not source_ids or any(identifier not in source_times for identifier in source_ids):
        return None
    return row | {"occurred_at": max(source_times.values())} | source_text
