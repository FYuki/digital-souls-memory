"""既存性能runner完了後だけ、全提供記録を監査保存する。"""

import json
import os
import sys
from pathlib import Path

import psycopg

from tests.st.performance import Manifest, run


def main() -> None:
    manifest = Manifest.model_validate_json(Path(sys.argv[1]).read_text())
    passed = run(manifest, Path(sys.argv[2]))
    with psycopg.connect(os.environ["MEMORY_CORE_ST_AUDIT_URL"]) as connection:
        records = connection.execute("SELECT to_jsonb(p) FROM provision_records p").fetchall()
        counts = connection.execute("SELECT count(*) FROM recall_documents WHERE ready").fetchone()
        chunks = connection.execute("SELECT count(*) FROM recall_embeddings").fetchone()
        document = connection.execute(
            "SELECT to_jsonb(d) - 'embedding' FROM recall_documents d "
            "WHERE kind='memory' AND chunk_index=0 LIMIT 1"
        ).fetchone()
        turn = connection.execute("SELECT to_jsonb(t) FROM turns t LIMIT 1").fetchone()
    assert counts is not None and chunks is not None
    Path(sys.argv[2]).with_name("audit.json").write_text(
        json.dumps(
            {
                "records": [r[0] for r in records],
                "ready_documents": counts[0],
                "embeddings": chunks[0],
                "sample_document": document[0] if document else None,
                "sample_turn": turn[0] if turn else None,
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
