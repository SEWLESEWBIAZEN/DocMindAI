"""Integration fixtures. Requires `docker compose up -d postgres redis minio`
and a .env (see .env.example). Run with: uv run pytest -m integration
"""

import pytest

from documind.core.config import Settings


@pytest.fixture(scope="session")
def live_settings() -> Settings:
    return Settings()  # reads the environment / .env like the real app


@pytest.fixture(scope="session")
def test_database_url(live_settings: Settings) -> str:
    """URL of the dedicated `<db>_test` database created by the Postgres init script."""
    from sqlalchemy.engine import make_url

    url = make_url(live_settings.database_url.get_secret_value())
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)
