"""ASGI entry point: ``uvicorn documind.asgi:app``."""

from documind.main import create_app

app = create_app()
