"""Bounded restartable index work; model processing holds no database locks."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import Event, Thread

from digital_souls_memory.evidence.provision import ProvisionUnavailable
from digital_souls_memory.recall.domain import VectorSpace
from digital_souls_memory.recall.indexing import IndexStore, IndexWork
from digital_souls_memory.recall.ports import Embedding

logger = logging.getLogger(__name__)


class IndexWorker:
    def __init__(
        self,
        store: IndexStore,
        embedding: Embedding,
        *,
        space: VectorSpace,
        clock: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.embedding = embedding
        self.space = space
        self.clock = clock
        self.stop = Event()
        self.thread = Thread(target=self._run, name="recall-index", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join()

    def process(self, work: IndexWork) -> None:
        try:
            value = self.store.load(work, now=self.clock())
            if value is not None:
                embedding = self.store.cached(value)
                if embedding is None:
                    embedding = self.embedding.embed(value.input, purpose="passage")
                if self.store.publish(work, input=value, embedding=embedding, now=self.clock()):
                    return
        except (ProvisionUnavailable, RuntimeError):
            logger.warning("Recall index dependency unavailable; durable work will retry")
        self.store.retry(work, not_before=self.clock() + timedelta(seconds=1))

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                for work in self.store.claim(space=self.space, now=self.clock(), limit=32):
                    if self.stop.is_set():
                        break
                    self.process(work)
            except ProvisionUnavailable:
                logger.warning("Recall index storage unavailable; durable work remains pending")
            self.stop.wait(0.05)
