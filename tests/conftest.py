import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from server.powerups import load_powerups   # noqa: E402
from server.track import load_tracks        # noqa: E402


@pytest.fixture(scope="session")
def phys():
    with open(os.path.join(ROOT, "shared", "physics.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def tracks():
    t, errors = load_tracks(os.path.join(ROOT, "shared", "tracks"))
    assert not errors, errors
    return t


@pytest.fixture
def track(tracks):
    return tracks["test_ring"]


@pytest.fixture
def powerups():
    return load_powerups(os.path.join(ROOT, "shared", "powerups.json"), seed=42)
