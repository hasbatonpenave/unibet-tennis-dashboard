"""Unit tests for feed/parser.py — odds extraction and parsing."""

import pytest
from feed.parser import (
    extract_face_a_face_odds,
    extract_live_state,
    extract_ssr_odds,
    parse_float_price,
    build_match_slug,
    parse_start_time,
)


class TestParseFloatPrice:
    def test_simple_decimal(self):
        assert parse_float_price("1,85") == 1.85

    def test_integer_price(self):
        assert parse_float_price("60,00") == 60.0

    def test_small_odds(self):
        assert parse_float_price("1,01") == 1.01

    def test_large_odds(self):
        assert parse_float_price("501,00") == 501.0


class TestExtractFaceAFaceOdds:
    def test_extracts_live_odds(self, live_items):
        result = extract_face_a_face_odds(live_items)
        assert "l3346070" in result
        odds = result["l3346070"]["odds"]
        assert odds["A.Muller"] == 1.85
        assert odds["M.Giron"] == 1.95

    def test_extracts_prematch_odds(self, prematch_items):
        result = extract_face_a_face_odds(prematch_items)
        assert "e3346339" in result
        odds = result["e3346339"]["odds"]
        assert odds["Q.Zheng"] == 1.16
        assert odds["A.Sabalenka"] == 4.50

    def test_live_flag(self, live_items, prematch_items):
        live_result = extract_face_a_face_odds(live_items)
        prematch_result = extract_face_a_face_odds(prematch_items)
        assert live_result["l3346070"]["live"] is True
        assert prematch_result["e3346339"]["live"] is False

    def test_match_metadata(self, live_items):
        result = extract_face_a_face_odds(live_items)
        match = result["l3346070"]
        assert match["player_a"] == "A.Muller"
        assert match["player_b"] == "M.Giron"
        assert match["competition"] == "ATP Rome"

    def test_live_score_extraction(self, live_items):
        result = extract_face_a_face_odds(live_items)
        score = result["l3346070"]["score"]
        assert score is not None
        assert score["home"] == 0
        assert score["away"] == 0

    def test_empty_items(self):
        result = extract_face_a_face_odds({})
        assert result == {}

    def test_skips_non_tennis_events(self):
        items = {
            "l1": {"desc": "Football match", "code": "FOOT"},
        }
        result = extract_face_a_face_odds(items)
        assert "l1" not in result

    def test_skips_events_without_face_a_face_market(self):
        items = {
            "e1": {"desc": "Tennis match", "code": "TENN"},
            "o1": {"parent": "m999", "price": "2,00", "desc": "Player A", "pos": 1},
        }
        result = extract_face_a_face_odds(items)
        assert result == {}

    def test_event_id_parsing(self, prematch_items):
        result = extract_face_a_face_odds(prematch_items)
        assert result["e3346339"]["event_id"] == 3346339


class TestExtractLiveState:
    def test_basic_score(self):
        event = {
            "score": {"a": 6, "b": 4},
            "set": [{"a": 6, "b": 4, "win": True}],
            "currSet": 2,
            "max": 3,
            "period": {"desc": "2eme set"},
            "active": True,
            "time": {"m": 45, "s": 30},
            "tieBreak": False,
        }
        state = extract_live_state(event)
        assert state["score_a"] == 6
        assert state["score_b"] == 4
        assert len(state["sets"]) == 1
        assert state["sets"][0]["win"] is True
        assert state["current_set"] == 2
        assert state["max_sets"] == 3
        assert state["period_desc"] == "2eme set"
        assert state["active"] is True
        assert state["time_seconds"] == 2730  # 45*60 + 30
        assert state["tiebreak"] is False

    def test_tiebreak(self):
        event = {
            "score": {"a": 6, "b": 6},
            "set": [],
            "tieBreak": True,
            "period": {"desc": "Jeu decisif"},
            "active": True,
            "time": {"m": 120, "s": 0},
        }
        state = extract_live_state(event)
        assert state["tiebreak"] is True
        assert state["time_seconds"] == 7200


class TestExtractSSROdds:
    def test_extracts_from_events_detail(self):
        events_detail = {
            "events": [{
                "id": 3346339,
                "start": "2605081400",
                "sportCode": "TENN",
                "parent": "p58535600",
                "groupedMarkets": [{
                    "markets": [{
                        "id": 185500001,
                        "marketTypeId": 68,
                        "outcomes": [
                            {"description": "Q.Zheng", "price": "1,16", "pos": 1},
                            {"description": "A.Sabalenka", "price": "4,50", "pos": 2},
                        ],
                    }],
                }],
            }],
        }
        result = extract_ssr_odds(events_detail)
        assert result is not None
        assert result["player_a"] == "Q.Zheng"
        assert result["player_b"] == "A.Sabalenka"
        assert result["odds"]["Q.Zheng"] == 1.16
        assert result["odds"]["A.Sabalenka"] == 4.50
        assert result["live"] is False

    def test_returns_none_for_empty(self):
        assert extract_ssr_odds({}) is None
        assert extract_ssr_odds({"events": []}) is None

    def test_returns_none_without_face_a_face(self):
        detail = {
            "events": [{
                "id": 1,
                "groupedMarkets": [{
                    "markets": [{"marketTypeId": 69, "outcomes": []}],
                }],
            }],
        }
        assert extract_ssr_odds(detail) is None


class TestBuildMatchSlug:
    def test_basic_slug(self):
        event = {"a": "A.Muller", "b": "M.Giron"}
        assert build_match_slug(event) == "amuller-vs-mgiron"

    def test_removes_special_chars(self):
        event = {"a": "A.Zverev", "b": "C.Alcaraz"}
        slug = build_match_slug(event)
        assert slug == "azverev-vs-calcaraz"

    def test_empty_when_missing_players(self):
        assert build_match_slug({}) == ""
        assert build_match_slug({"a": "Player1"}) == ""

    def test_uses_opponent_labels(self):
        event = {
            "opponentA": {"label": "N.Djokovic"},
            "opponentB": {"label": "J.Sinner"},
        }
        assert build_match_slug(event) == "ndjokovic-vs-jsinner"


class TestParseStartTime:
    def test_parses_valid_format(self):
        dt = parse_start_time("2605081400")
        assert dt is not None
        assert dt.month == 5
        assert dt.day == 8
        assert dt.hour == 14
        assert dt.minute == 0

    def test_returns_none_for_invalid(self):
        assert parse_start_time("") is None
        assert parse_start_time("abc") is None
        assert parse_start_time(None) is None

    def test_returns_none_for_short_string(self):
        assert parse_start_time("2605") is None
