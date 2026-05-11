"""Odds extraction and parsing for Unibet sports data."""

import logging
from datetime import datetime, timezone

from api.client import TARGET_MARKETS

logger = logging.getLogger("unibet_feed")

# ── MARKET TYPE CONSTANTS (kept for backward compat) ──────────────────────────────
MARKET_FACE_A_FACE = 68


def parse_float_price(price_str: str | None) -> float:
    """Parse Unibet price format: '1,85' → 1.85, '60,00' → 60.0"""
    if not price_str:
        return 0.0
    return float(str(price_str).replace(",", "."))


def _extract_market_odds(
    items: dict,
    market_type_id: int,
    sport_code: str | None = None,
    market_name: str = "1X2",
) -> dict[str, dict]:
    """Extract odds for a specific market type, optionally filtered by sport code.

    Returns {event_key: {match_id, event_id, match, player_a, player_b, ...}}
    """
    result: dict[str, dict] = {}
    markets_by_event: dict[str, list[dict]] = {}
    outcomes_by_market: dict[str, list[dict]] = {}

    for key, val in items.items():
        if not isinstance(val, dict):
            continue
        if key.startswith("m") and val.get("markettypeId") == market_type_id:
            parent = val.get("parent", "")
            if parent:
                markets_by_event.setdefault(parent, []).append({"id": key, **val})
        elif key.startswith("o"):
            parent = val.get("parent", "")
            if parent:
                outcomes_by_market.setdefault(parent, []).append({"id": key, **val})

    for event_key, event_val in items.items():
        if not isinstance(event_val, dict):
            continue
        if not (event_key.startswith("l") or event_key.startswith("e")):
            continue

        code = event_val.get("code", "TENN")
        if sport_code and code != sport_code:
            continue

        mkt_list = markets_by_event.get(event_key, [])
        if not mkt_list:
            continue

        for mkt in mkt_list:
            outcomes = outcomes_by_market.get(mkt["id"], [])
            if len(outcomes) < 2:
                continue

            odds = {}
            for o in sorted(outcomes, key=lambda x: x.get("pos", 0)):
                name = o.get("desc", f"Player{len(odds)+1}")
                price = o.get("price", "0")
                odds[name] = parse_float_price(price)

            is_live = event_key.startswith("l")
            score = None
            if is_live:
                score = {
                    "home": event_val.get("score", {}).get("a"),
                    "away": event_val.get("score", {}).get("b"),
                    "sets": event_val.get("set", []),
                    "match_score": event_val.get("match", []),
                    "current_set": event_val.get("currSet"),
                    "max_sets": event_val.get("max"),
                    "period": event_val.get("period", {}).get("desc"),
                    "active": event_val.get("active"),
                    "time": event_val.get("time"),
                }

            evt_id = event_key.lstrip("le")

            # Build a compound key: event_key + market_id for multi-market support
            compound_key = f"{event_key}_{market_type_id}"
            result[compound_key] = {
                "match_id":     event_key,
                "event_id":     int(evt_id) if evt_id.isdigit() else evt_id,
                "match":        event_val.get("desc", event_val.get("description", "")),
                "player_a":     event_val.get("a") or (event_val.get("opponentA") or {}).get("label", ""),
                "player_b":     event_val.get("b") or (event_val.get("opponentB") or {}).get("label", ""),
                "competition":  event_val.get("pdesc", ""),
                "start":        event_val.get("start", ""),
                "live":         is_live,
                "code":         code,
                "odds":         odds,
                "market_id":    mkt["id"],
                "market_name":  market_name,
                "market_type":  market_type_id,
                "score":        score,
                "parent":       event_val.get("parent", ""),
            }

    return result


def extract_face_a_face_odds(items: dict) -> dict[str, dict]:
    """Extract Face a Face (Moneyline) odds from a flat items dict (backward compat).

    Returns {event_key: {match_id, odds: {PlayerA: float, PlayerB: float}, ...}}
    Note: compound keys are used internally (event_key_marketId), callers should
    iterate values rather than relying on key format.
    """
    return _extract_market_odds(items, 68, sport_code=None, market_name="Face a Face")


def extract_odds(items: dict) -> dict[str, dict]:
    """Extract all configured market odds across all enabled sports.

    Returns {compound_key: {match_id, odds, code, market_name, ...}}
    Compound key format: {event_key}_{market_type_id}
    """
    result: dict[str, dict] = {}
    for sport_code, markets in TARGET_MARKETS.items():
        for market_id, market_name in markets.items():
            extracted = _extract_market_odds(items, market_id, sport_code, market_name)
            result.update(extracted)
    return result


def extract_ssr_odds(events_detail: dict, target_markets: dict[int, str] | None = None) -> dict | None:
    """Extract odds from SSR EventsDetail data for configured markets."""
    if target_markets is None:
        target_markets = {68: "Face a Face"}

    events = events_detail.get("events", [])
    if not events:
        return None

    ev = events[0]
    event_id = f"e{ev.get('id', '')}"
    grouped = ev.get("groupedMarkets", [])

    for group in grouped:
        for mkt in group.get("markets", []):
            mkt_type = mkt.get("marketTypeId")
            if mkt_type not in target_markets:
                continue

            outcomes = mkt.get("outcomes", [])
            if len(outcomes) < 2:
                continue

            odds = {}
            for o in sorted(outcomes, key=lambda x: x.get("pos", 0)):
                name = o.get("description", f"Player{len(odds)+1}")
                price = o.get("price", "0")
                odds[name] = parse_float_price(price)

            return {
                "match_id":     event_id,
                "event_id":     ev.get("id"),
                "match":        f"{list(odds.keys())[0] if odds else '?'} vs {list(odds.keys())[1] if len(odds) > 1 else '?'}",
                "player_a":     list(odds.keys())[0] if odds else "",
                "player_b":     list(odds.keys())[1] if len(odds) > 1 else "",
                "competition":  "",
                "start":        ev.get("start", ""),
                "live":         False,
                "code":         ev.get("sportCode", "TENN"),
                "odds":         odds,
                "market_id":    str(mkt.get("id", "")),
                "market_name":  target_markets[mkt_type],
                "market_type":  mkt_type,
                "score":        None,
                "parent":       ev.get("parent", ""),
            }

    return None


def extract_live_state(event: dict) -> dict:
    """Extract live match state from a live event dict (tennis)."""
    score = event.get("score", {})
    sets = event.get("set", [])
    period = event.get("period", {})

    return {
        "is_live":    True,
        "score_a":    score.get("a"),
        "score_b":    score.get("b"),
        "sets":       [{"a": s.get("a"), "b": s.get("b"), "win": s.get("win")} for s in sets],
        "current_set": event.get("currSet"),
        "max_sets":    event.get("max"),
        "period_desc": period.get("desc"),
        "active":      event.get("active"),
        "time_seconds": (event.get("time") or {}).get("m", 0) * 60 + (event.get("time") or {}).get("s", 0),
        "tiebreak":    event.get("tieBreak", False),
    }


def extract_soccer_score(event: dict) -> dict:
    """Extract live match state from a soccer event dict."""
    score_obj = event.get("score", {})
    period = event.get("period", {})
    time_obj = event.get("time", {})

    return {
        "is_live":     True,
        "score_a":     score_obj.get("a"),
        "score_b":     score_obj.get("b"),
        "sets":        [],
        "current_set": None,
        "max_sets":    None,
        "period_desc": period.get("desc"),
        "active":      event.get("active"),
        "time_seconds": time_obj.get("m", 0) * 60 + time_obj.get("s", 0),
        "tiebreak":    False,
    }


def extract_score(event: dict, sport_code: str) -> dict:
    """Extract live score state, dispatching by sport code."""
    if sport_code == "FOOT":
        return extract_soccer_score(event)
    return extract_live_state(event)


def build_match_slug(event: dict) -> str:
    """Build a URL-friendly slug for a match."""
    a = event.get("a") or (event.get("opponentA") or {}).get("label", "")
    b = event.get("b") or (event.get("opponentB") or {}).get("label", "")
    if a and b:
        a_slug = a.lower().replace(" ", "-").replace(".", "").replace("'", "")
        b_slug = b.lower().replace(" ", "-").replace(".", "").replace("'", "")
        return f"{a_slug}-vs-{b_slug}"
    return ""


def parse_start_time(start_str: str) -> datetime | None:
    """Parse Unibet start time format 'YYMMDDHHmm' → datetime."""
    if not start_str or len(start_str) < 10:
        return None
    try:
        return datetime.strptime(start_str[:10], "%y%m%d%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
