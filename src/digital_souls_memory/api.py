"""HTTP liveness entry point, independent of storage and model providers."""

from fastapi import FastAPI


def health() -> dict[str, str]:
    """Report process liveness without probing or disclosing dependencies."""
    return {"status": "ok"}


def create_app() -> FastAPI:
    app = FastAPI(title="Digital Souls Memory")
    app.get("/health")(health)
    return app
