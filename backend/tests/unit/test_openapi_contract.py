"""The committed OpenAPI document is the frontend's typed API contract.

``frontend/src/lib/api/openapi.json`` must match the backend. Regenerate both
it and the TypeScript types with ``make api-types``.
"""

import json
import os
from pathlib import Path

from documind.core.config import Settings
from documind.main import create_app
from tests.conftest import SettingsFactory

CONTRACT = Path(__file__).resolve().parents[3] / "frontend" / "src" / "lib" / "api" / "openapi.json"


def test_frontend_contract_is_current(make_settings: SettingsFactory) -> None:
    settings: Settings = make_settings()
    spec = json.dumps(create_app(settings).openapi(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_OPENAPI") == "1":
        CONTRACT.write_text(spec)
    assert CONTRACT.exists(), "run `make api-types`"
    assert CONTRACT.read_text() == spec, "API changed: run `make api-types` and commit the result"
