"""Graphiti native remove_episode against a disposable Neo4j, with fixture LLM."""

import asyncio
import json
import os
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

ROOT = Path(os.environ.get("OSS_PROBE_ROOT", "/tmp/phase1-oss-reassessment"))
os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
_connect = socket.socket.connect


def loopback_only(sock, address):
    if sock.family != socket.AF_UNIX and address != ("127.0.0.1", 18957):
        raise RuntimeError(f"Network disabled by probe: {address}")
    return _connect(sock, address)


socket.socket.connect = loopback_only

from graphiti_core import Graphiti  # noqa: E402
from graphiti_core.cross_encoder.client import CrossEncoderClient  # noqa: E402
from graphiti_core.driver.neo4j_driver import Neo4jDriver  # noqa: E402
from graphiti_core.edges import EntityEdge, EpisodicEdge  # noqa: E402
from graphiti_core.embedder.client import EmbedderClient  # noqa: E402
from graphiti_core.llm_client import LLMClient  # noqa: E402
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode  # noqa: E402
from graphiti_core.utils.maintenance.edge_operations import _dedupe_extracted_edge  # noqa: E402


async def scenario(fail_rebuild=False):
    group = "phase1-" + uuid4().hex
    driver = Neo4jDriver("bolt://127.0.0.1:18957", "neo4j", "unused")
    llm = Mock(spec=LLMClient)
    llm.config = Mock()
    llm.config.model = "fixture-only"
    llm.generate_response = AsyncMock(
        side_effect=RuntimeError("synthetic summary failure") if fail_rebuild else None,
        return_value={"summaries": [{"name": "Aoi", "summary": "Aoi likes tea."}]},
    )
    embedder = Mock(spec=EmbedderClient)
    embedder.create = AsyncMock(return_value=[0.1] * 384)
    reranker = Mock(spec=CrossEncoderClient)
    graph = Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder, cross_encoder=reranker)
    now = datetime.now(UTC)
    try:
        await graph.build_indices_and_constraints()
        old = EpisodicNode(
            name="old",
            group_id=group,
            source=EpisodeType.message,
            source_description="synthetic turn",
            content="Aoi likes tea. Secret SYNTHETIC_SECRET.",
            valid_at=now,
            episode_metadata={"source_turn_ids": ["turn-old"]},
        )
        keep = EpisodicNode(
            name="keep",
            group_id=group,
            source=EpisodeType.message,
            source_description="synthetic turn",
            content="Aoi likes tea.",
            valid_at=now + timedelta(seconds=1),
        )
        a = EntityNode(
            name="Aoi",
            group_id=group,
            summary="Aoi secret SYNTHETIC_SECRET",
            name_embedding=[0.1] * 384,
        )
        b = EntityNode(name="Tea", group_id=group, summary="Tea", name_embedding=[0.1] * 384)
        supported = EntityEdge(
            source_node_uuid=a.uuid,
            target_node_uuid=b.uuid,
            group_id=group,
            name="LIKES",
            fact="Aoi likes tea",
            episodes=[old.uuid],
            fact_embedding=[0.1] * 384,
            created_at=now,
        )
        secret = EntityEdge(
            source_node_uuid=a.uuid,
            target_node_uuid=b.uuid,
            group_id=group,
            name="SECRET",
            fact="SYNTHETIC_SECRET",
            episodes=[old.uuid],
            fact_embedding=[0.1] * 384,
            created_at=now,
        )
        repeated = supported.model_copy(update={"uuid": str(uuid4()), "episodes": [keep.uuid]})
        resolved, _, _ = await _dedupe_extracted_edge(llm, repeated, [supported], [], keep)
        assert resolved.uuid == supported.uuid and set(resolved.episodes) == {old.uuid, keep.uuid}
        # The semantic verdict is a fixture; native resolution and DB persistence
        # still choose a single canonical edge with multiple episode sources.
        llm.generate_response.side_effect = None
        llm.generate_response.return_value = {"duplicate_facts": [0], "contradicted_facts": []}
        paraphrase = repeated.model_copy(update={"uuid": str(uuid4()), "fact": "Aoi prefers tea"})
        resolved_semantic, _, _ = await _dedupe_extracted_edge(
            llm,
            paraphrase,
            [supported],
            [],
            keep,
        )
        assert resolved_semantic.uuid == supported.uuid
        llm.generate_response.return_value = {
            "summaries": [{"name": "Aoi", "summary": "Aoi likes tea."}]
        }
        llm.generate_response.side_effect = (
            RuntimeError("synthetic summary failure") if fail_rebuild else None
        )
        old.entity_edges = [supported.uuid, secret.uuid]
        keep.entity_edges = [supported.uuid]
        for node in [old, keep, a, b]:
            await node.save(driver)
        for edge in [supported, secret]:
            await edge.save(driver)
        one, _, _ = await driver.execute_query(
            "MATCH ()-[r:RELATES_TO {uuid:$uuid}]->() RETURN count(r) AS n",
            uuid=supported.uuid,
        )
        assert one[0]["n"] == 1
        for episode in [old, keep]:
            for entity in [a, b]:
                await EpisodicEdge(
                    source_node_uuid=episode.uuid,
                    target_node_uuid=entity.uuid,
                    group_id=group,
                    created_at=now,
                ).save(driver)
        roundtrip = await EpisodicNode.get_by_uuid(driver, old.uuid)
        metadata_retained = roundtrip.episode_metadata == old.episode_metadata
        await graph.remove_episode(old.uuid)
        shared = await EntityEdge.get_by_uuid(driver, supported.uuid)
        removed, _, _ = await driver.execute_query(
            "MATCH ()-[r:RELATES_TO {uuid:$uuid}]->() RETURN count(r) AS n", uuid=secret.uuid
        )
        a_after = await EntityNode.get_by_uuid(driver, a.uuid)
        assert shared.episodes == [keep.uuid]
        assert removed[0]["n"] == 0
        assert llm.generate_response.await_count > 0
        if fail_rebuild:
            assert "SYNTHETIC_SECRET" in a_after.summary
        else:
            assert a_after.summary == "Aoi likes tea."
        return {
            "execution": "PASS",
            "fixture_llm_failure": fail_rebuild,
            "shared_edge_keeps_remaining_source": True,
            "exact_duplicate_reuses_one_fact": True,
            "paraphrase_with_fixture_verdict_reuses_one_fact": True,
            "unsupported_edge_deleted": True,
            "summary_rebuilt": not fail_rebuild,
            "delete_raises_on_rebuild_failure": False if fail_rebuild else None,
            "old_summary_survives_rebuild_failure": fail_rebuild,
            "low_level_episode_metadata_roundtrip": metadata_retained,
        }
    finally:
        await driver.execute_query("MATCH (n {group_id:$group}) DETACH DELETE n", group=group)
        await graph.close()


async def main():
    result = {
        "candidate": "graphiti",
        "models": "fixture LLM and embeddings; real Neo4j",
        "observations": [
            await scenario(False),
            await scenario(True),
        ],
    }
    (ROOT / "graphiti-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
