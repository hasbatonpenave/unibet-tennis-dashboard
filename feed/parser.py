"""Odds extraction and parsing for Unibet tennis data."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("unibet_feed")

# ── MARKET TYPE CONSTANTS (imported here to avoid circular deps) ──────────────────
MARKET_FACE_A_FACE = 68


def parse_float_price(price_str: str | None) -> float:
    """Parse Unibet price format: '1,85' → 1.85, '60,00' → 60.0"""
    if not price_str:
        return 0.0
    return float(str(price_str).replace(",", "."))


def extract_face_a_face_odds(items: dict) -> dict[str, dict]:
    """Extract Face à Face (Moneyline) odds from a flat items dict.

    Returns {event_id: {match, competition, live, odds: {PlayerA: float, PlayerB: float}, ...}}
    """
    result: dict[str, dict] = {}

    markets_by_event: dict[str, list[dict]] = {}
    outcomes_by_market: dict[str, list[dict]] = {}

    for key, val in items.items():
        if not isinstance(val, dict):
            continue
        if key.startswith("m") and val.get("markettypeId") == MARKET_FACE_A_FACE:
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
            result[event_key] = {
                "match_id":     event_key,
                "event_id":     int(evt_id) if evt_id.isdigit() else evt_id,
                "match":        event_val.get("desc", event_val.get("description", "")),
                "player_a":     event_val.get("a") or (event_val.get("opponentA") or {}).get("label", ""),
                "player_b":     event_val.get("b") or (event_val.get("opponentB") or {}).get("label", ""),
                "competition":  event_val.get("pdesc", ""),
                "start":        event_val.get("start", ""),
                "live":         is_live,
                "code":         event_val.get("code", "TENN"),
                "odds":         odds,
                "market_id":    mkt["id"],
                "score":        score,
                "parent":       event_val.get("parent", ""),
            }

    return result


def extract_ssr_odds(events_detail: dict) -> dict | None:
    """Extract Face à Face odds from SSR EventsDetail data."""
    events = events_detail.get("events", [])
    if not events:
        return None

    ev = events[0]
    event_id = f"e{ev.get('id', '')}"
    grouped = ev.get("groupedMarkets", [])

    for group in grouped:
        for mkt in group.get("markets", []):
            if mkt.get("marketTypeId") == MARKET_FACE_A_FACE:
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
                    "score":        None,
                    "parent":       ev.get("parent", ""),
                }

    return None


def extract_live_state(event: dict) -> dict:
    """Extract live match state from a live event dict."""
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
