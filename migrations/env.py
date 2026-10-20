"""Use psycopg 3 through SQLAlchemy solely to run Alembic migrations."""

import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

url = make_url(os.environ["MEMORY_DATABASE_URL"])
if url.drivername not in {"postgresql", "postgresql+psycopg"}:
    raise ValueError("MEMORY_DATABASE_URL must use PostgreSQL with psycopg 3")
url = url.set(drivername="postgresql+psycopg")

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    with create_engine(url, poolclass=pool.NullPool).connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
