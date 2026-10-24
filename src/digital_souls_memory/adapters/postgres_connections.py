"""Connection supply with independent operation and read-snapshot lifetimes."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import DictRow, dict_row

from digital_souls_memory.evidence.provision import ProvisionUnavailable


class PostgresConnections:
    def __init__(
        self,
        url: str,
        *,
        configure: Callable[[psycopg.Connection[DictRow]], None] | None = None,
    ) -> None:
        self.url = url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.configure = configure
        self.bound: psycopg.Connection[DictRow] | None = None
        self.reading = False

    @contextmanager
    def request(self) -> Iterator[None]:
        try:
            with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5) as connection:
                if self.configure is not None:
                    self.configure(connection)
                self.bound = connection
                try:
                    yield
                finally:
                    self.bound = None
        except psycopg.Error as error:
            raise ProvisionUnavailable() from error

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[DictRow]]:
        if self.bound is None:
            with psycopg.connect(self.url, row_factory=dict_row, connect_timeout=5) as connection:
                if self.configure is not None:
                    self.configure(connection)
                yield connection
        elif self.reading:
            yield self.bound
        else:
            try:
                yield self.bound
                self.bound.commit()
            except BaseException:
                self.bound.rollback()
                raise

    @contextmanager
    def snapshot(self) -> Iterator[None]:
        with self.connection() as connection:
            connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            self.reading = True
            try:
                yield
            finally:
                self.reading = False
