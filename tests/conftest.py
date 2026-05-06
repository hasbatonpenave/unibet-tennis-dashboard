"""Shared test fixtures for Unibet tennis dashboard tests."""

import json
import os
import pytest

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file from tests/fixtures/."""
    path = os.path.join(FIXTURES_DIR, name)
    with open(path) as f:
        return json.load(f)


# ── Captured API fixtures ─────────────────────────────────────────────────────────
# These represent real Unibet API responses.

@pytest.fixture
def live_items():
    """Live events + markets + outcomes from /current/events/live/topmarket."""
    return {
        "l3346070": {
            "desc": "A.Muller vs M.Giron",
            "a": "A.Muller",
            "b": "M.Giron",
            "code": "TENN",
            "pdesc": "ATP Rome",
            "start": "2605061200",
            "score": {"a": 0, "b": 0},
            "set": [],
            "currSet": 1,
            "max": 3,
            "period": {"desc": "1er set"},
            "active": True,
            "parent": "p58535539",
        },
        "m185435979": {
            "parent": "l3346070",
            "markettypeId": 68,
            "desc": "Face à Face",
            "periodId": 0,
        },
        "o684085990": {
            "parent": "m185435979",
            "price": "1,85",
            "desc": "A.Muller",
            "pos": 1,
        },
        "o684085991": {
            "parent": "m185435979",
            "price": "1,95",
            "desc": "M.Giron",
            "pos": 2,
        },
    }


@pytest.fixture
def prematch_items():
    """Prematch event + markets + outcomes from lvs-api listing."""
    return {
        "e3346339": {
            "desc": "Q.Zheng vs A.Sabalenka",
            "a": "Q.Zheng",
            "b": "A.Sabalenka",
            "code": "TENN",
            "pdesc": "WTA Madrid",
            "start": "2605081400",
            "eType": "G",
            "parent": "p58535600",
        },
        "m185500001": {
            "parent": "e3346339",
            "markettypeId": 68,
            "desc": "Face à Face",
        },
        "o685000001": {
            "parent": "m185500001",
            "price": "1,16",
            "desc": "Q.Zheng",
            "pos": 1,
        },
        "o685000002": {
            "parent": "m185500001",
            "price": "4,50",
            "desc": "A.Sabalenka",
            "pos": 2,
        },
    }


@pytest.fixture
def mixed_items(live_items, prematch_items):
    """Combined live + prematch items dict."""
    return {**live_items, **prematch_items}
