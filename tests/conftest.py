"""Offline test isolation: never read the operator's real config or reach a live server."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEAD_ADDRESS = "127.0.0.1:9"  # discard port; nothing listens, connects fail fast


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LEGENDS_TEMPORAL_CONFIG", str(tmp_path / "absent-config.json"))
    for var in ("TEMPORAL_ADDRESS", "TEMPORAL_NAMESPACE", "TEMPORAL_API_KEY", "TEMPORAL_TLS_CERT",
                "TEMPORAL_TLS_KEY", "TEMPORAL_TLS_CA"):
        monkeypatch.delenv(var, raising=False)
    yield tmp_path
