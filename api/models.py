"""Data models for Unibet sports odds feed."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OddsUpdate:
    """A single odds update pushed through the feed queue."""
    source: str
    match_id: str
    market: str
    odds: dict[str, float]
    meta: dict
    live: bool
    score: Optional[dict]
    ts: float


@dataclass
class MatchMeta:
    """Metadata for a sports match."""
    match: str = ""
    player_a: str = ""
    player_b: str = ""
    competition: str = ""
    date: str = ""
    live: bool = False
    code: str = "TENN"
    sport: str = "tennis"
    score_a: Optional[int] = None
    score_b: Optional[int] = None
    sets: list = field(default_factory=list)
    current_set: Optional[int] = None
    max_sets: Optional[int] = None
    period_desc: Optional[str] = None
    active: Optional[bool] = None
    time_seconds: int = 0
    tiebreak: bool = False


@dataclass
class PricePoint:
    """A single price data point for charting."""
    ts: float
    odd: float
