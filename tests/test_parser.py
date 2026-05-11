"""Unit tests for feed/parser.py — odds extraction and parsing."""

import pytest
from feed.parser import (
    extract_face_a_face_odds,
    extract_odds,
    extract_live_state,
    extract_soccer_score,
    extract_score,
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
        # Compound key: event_key + market_type_id
        assert "l3346070_68" in result
        odds = result["l3346070_68"]["odds"]
        assert odds["A.Muller"] == 1.85
        assert odds["M.Giron"] == 1.95

    def test_extracts_prematch_odds(self, prematch_items):
        result = extract_face_a_face_odds(prematch_items)
        assert "e3346339_68" in result
        odds = result["e3346339_68"]["odds"]
        assert odds["Q.Zheng"] == 1.16
        assert odds["A.Sabalenka"] == 4.50

    def test_live_flag(self, live_items, prematch_items):
        live_result = extract_face_a_face_odds(live_items)
        prematch_result = extract_face_a_face_odds(prematch_items)
        assert live_result["l3346070_68"]["live"] is True
        assert prematch_result["e3346339_68"]["live"] is False

    def test_match_metadata(self, live_items):
        result = extract_face_a_face_odds(live_items)
        match = result["l3346070_68"]
        assert match["player_a"] == "A.Muller"
        assert match["player_b"] == "M.Giron"
        assert match["competition"] == "ATP Rome"

    def test_live_score_extraction(self, live_items):
        result = extract_face_a_face_odds(live_items)
        score = result["l3346070_68"]["score"]
        assert score is not None
        assert score["home"] == 0
        assert score["away"] == 0

    def test_empty_items(self):
        result = extract_face_a_face_odds({})
        assert result == {}

    def test_skips_without_face_a_face_market(self, soccer_live_items):
        # Soccer items have market 1 (1X2), not 68 (Face a Face)
        result = extract_face_a_face_odds(soccer_live_items)
        assert result == {}

    def test_event_id_parsing(self, prematch_items):
        result = extract_face_a_face_odds(prematch_items)
        assert result["e3346339_68"]["event_id"] == 3346339

    def test_market_name_in_result(self, live_items):
        result = extract_face_a_face_odds(live_items)
        assert result["l3346070_68"]["market_name"] == "Face a Face"


class TestExtractOdds:
    def test_extracts_tennis_and_soccer(self, live_items, soccer_live_items):
        items = {**live_items, **soccer_live_items}
        result = extract_odds(items)
        assert "l3346070_68" in result  # tennis Face a Face
        assert "l9999001_1" in result   # soccer 1X2

    def test_soccer_1x2_three_outcomes(self, soccer_live_items):
        result = extract_odds(soccer_live_items)
        odds = result["l9999001_1"]["odds"]
        assert len(odds) == 3
        assert odds["PSG"] == 1.85
        assert odds["Nul"] == 4.00
        assert odds["Marseille"] == 3.50

    def test_soccer_over_under(self, soccer_prematch_items):
        result = extract_odds(soccer_prematch_items)
        assert "e8888001_18" in result
        odds = result["e8888001_18"]["odds"]
        assert len(odds) == 2
        assert odds["Plus de 2.5"] == 1.90
        assert odds["Moins de 2.5"] == 1.80

    def test_market_name_field(self, soccer_live_items):
        result = extract_odds(soccer_live_items)
        assert result["l9999001_1"]["market_name"] == "1X2"

    def test_sport_code_in_result(self, soccer_live_items, live_items):
        items = {**live_items, **soccer_live_items}
        result = extract_odds(items)
        assert result["l3346070_68"]["code"] == "TENN"
        assert result["l9999001_1"]["code"] == "FOOT"

    def test_empty_items(self):
        assert extract_odds({}) == {}


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


class TestExtractSoccerScore:
    def test_basic_soccer_score(self):
        event = {
            "score": {"a": 2, "b": 1},
            "period": {"desc": "2eme mi-temps"},
            "time": {"m": 67, "s": 30},
            "active": True,
        }
        state = extract_soccer_score(event)
        assert state["score_a"] == 2
        assert state["score_b"] == 1
        assert state["period_desc"] == "2eme mi-temps"
        assert state["time_seconds"] == 4050  # 67*60 + 30
        assert state["active"] is True
        assert state["tiebreak"] is False
        assert state["sets"] == []

    def test_nil_nil(self):
        event = {
            "score": {"a": 0, "b": 0},
            "period": {"desc": "1ere mi-temps"},
            "time": {"m": 23, "s": 0},
            "active": True,
        }
        state = extract_soccer_score(event)
        assert state["score_a"] == 0
        assert state["score_b"] == 0
        assert state["time_seconds"] == 1380


class TestExtractScore:
    def test_dispatches_to_tennis(self, live_items):
        raw_event = live_items.get("l3346070", {})
        state = extract_score(raw_event, "TENN")
        assert "sets" in state
        assert state["score_a"] == 0

    def test_dispatches_to_soccer(self, soccer_live_items):
        raw_event = soccer_live_items.get("l9999001", {})
        state = extract_score(raw_event, "FOOT")
        assert state["score_a"] == 2
        assert state["score_b"] == 1
        assert state["sets"] == []
        assert state["period_desc"] == "2eme mi-temps"


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

    def test_extracts_with_custom_markets(self):
        events_detail = {
            "events": [{
                "id": 1,
                "groupedMarkets": [{
                    "markets": [{
                        "id": 100,
                        "marketTypeId": 1,
                        "outcomes": [
                            {"description": "Home", "price": "2,00", "pos": 1},
                            {"description": "Draw", "price": "3,00", "pos": 2},
                            {"description": "Away", "price": "4,00", "pos": 3},
                        ],
                    }],
                }],
            }],
        }
        result = extract_ssr_odds(events_detail, target_markets={1: "1X2"})
        assert result is not None
        assert len(result["odds"]) == 3

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
