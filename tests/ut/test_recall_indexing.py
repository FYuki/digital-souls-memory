from datetime import datetime, timedelta

import pytest

from digital_souls_memory.recall.domain import EmbeddingBatch
from digital_souls_memory.recall.indexing import IndexInput, IndexWork
from digital_souls_memory.recall.inputs import InputLabel, LocalOnly
from digital_souls_memory.recall.worker import IndexWorker
from tests.evidence_support import RECORDED, turn
from tests.memory_support import memory
from tests.recall_fixtures import SPACE, candidate_for
from tests.recall_support import RecordingEmbedding

pytestmark = pytest.mark.ut


class IndexDouble:
    def __init__(self) -> None:
        self.work = IndexWork(
            ref=candidate_for(memory(), sources=(turn(),)).ref,
            version=1,
            token="claim",
            space=SPACE,
        )
        self.value: IndexInput | None = IndexInput(
            candidate=candidate_for(memory(), sources=(turn(),)),
            input=LocalOnly(texts=("合成検索文書",), labels=(InputLabel("sensitive", "v1"),)),
        )
        self.embedding = EmbeddingBatch(space=SPACE, vectors=((1.0,) + (0.0,) * 1023,))
        self.reuse = False
        self.current = True
        self.published: list[IndexInput] = []
        self.retries: list[datetime] = []

    def claim(self, *, space: object, now: datetime, limit: int) -> tuple[IndexWork, ...]:
        assert space == SPACE and limit >= 1
        return (self.work,)

    def load(self, work: IndexWork, *, now: datetime) -> IndexInput | None:
        assert work == self.work and now == RECORDED
        return self.value

    def cached(self, value: IndexInput) -> EmbeddingBatch | None:
        assert value == self.value
        return self.embedding if self.reuse else None

    def publish(
        self, work: IndexWork, *, input: IndexInput, embedding: EmbeddingBatch, now: datetime
    ) -> bool:
        assert work == self.work and now == RECORDED and embedding == self.embedding
        if not self.current:
            return False
        self.published.append(input)
        return True

    def retry(self, work: IndexWork, *, not_before: datetime) -> None:
        assert work == self.work
        self.retries.append(not_before)


@pytest.mark.parametrize("reuse", [False, True])
def test_index_worker_publishes_labeled_passage_or_reuses_persisted_embedding(reuse: bool) -> None:
    store = IndexDouble()
    store.reuse = reuse
    embedding = RecordingEmbedding()
    worker = IndexWorker(store, embedding, space=SPACE, clock=lambda: RECORDED)
    worker.process(store.work)
    assert store.published == [store.value]
    assert store.retries == []
    assert [purpose for _, purpose in embedding.calls] == ([] if reuse else ["passage"])
    if not reuse:
        assert store.value is not None
        assert embedding.calls[0][0] == store.value.input


@pytest.mark.parametrize("failure", ["missing", "model", "changed"])
def test_index_worker_keeps_failed_or_superseded_work_retryable(failure: str) -> None:
    store = IndexDouble()
    embedding = RecordingEmbedding()
    if failure == "missing":
        store.value = None
    elif failure == "model":
        embedding.fail = True
    else:
        store.current = False
    worker = IndexWorker(store, embedding, space=SPACE, clock=lambda: RECORDED)
    worker.process(store.work)
    assert store.published == []
    assert store.retries == [RECORDED + timedelta(seconds=1)]
    store.value = IndexDouble().value
    store.current = True
    embedding.fail = False
    worker.process(store.work)
    assert store.published == [store.value]
