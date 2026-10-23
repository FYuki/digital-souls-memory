FROM python:3.12.3-slim-bookworm
RUN pip install --no-cache-dir uv==0.8.22
WORKDIR /app
COPY pyproject.toml uv.lock .python-version README.md ./
COPY src src
RUN uv sync --frozen
COPY alembic.ini ./
COPY migrations migrations
COPY tests tests
COPY tools/core_st_db.py tools/core_st_db.py
ENV PATH="/app/.venv/bin:$PATH"
