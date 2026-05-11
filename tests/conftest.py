"""Shared test fixtures for Unibet sports dashboard tests."""

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
    """Live tennis events + markets + outcomes from /current/events/live/topmarket."""
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
            "desc": "Face a Face",
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
    """Prematch tennis event + markets + outcomes from lvs-api listing."""
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
            "desc": "Face a Face",
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


@pytest.fixture
def soccer_live_items():
    """Live soccer event + 1X2 market with 3 outcomes."""
    return {
        "l9999001": {
            "desc": "PSG vs Marseille",
            "a": "PSG",
            "b": "Marseille",
            "code": "FOOT",
            "pdesc": "Ligue 1",
            "start": "2605102000",
            "score": {"a": 2, "b": 1},
            "period": {"desc": "2eme mi-temps"},
            "time": {"m": 67, "s": 30},
            "active": True,
            "parent": "p17",
        },
        "m9999001": {
            "parent": "l9999001",
            "markettypeId": 1,
            "desc": "1X2",
        },
        "o9999001": {"parent": "m9999001", "price": "1,85", "desc": "PSG", "pos": 1},
        "o9999002": {"parent": "m9999001", "price": "4,00", "desc": "Nul", "pos": 2},
        "o9999003": {"parent": "m9999001", "price": "3,50", "desc": "Marseille", "pos": 3},
    }


@pytest.fixture
def soccer_prematch_items():
    """Prematch soccer event + Over/Under 2.5 market."""
    return {
        "e8888001": {
            "desc": "Barcelona vs Real Madrid",
            "a": "Barcelona",
            "b": "Real Madrid",
            "code": "FOOT",
            "pdesc": "La Liga",
            "start": "2605121900",
            "eType": "G",
            "parent": "p35",
        },
        "m8888001": {
            "parent": "e8888001",
            "markettypeId": 18,
            "desc": "Over/Under 2.5",
        },
        "o8888001": {"parent": "m8888001", "price": "1,90", "desc": "Plus de 2.5", "pos": 1},
        "o8888002": {"parent": "m8888001", "price": "1,80", "desc": "Moins de 2.5", "pos": 2},
    }
