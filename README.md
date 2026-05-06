# Unibet Tennis — Real-Time Odds Dashboard

Reverse-engineered Unibet.fr tennis odds monitor. Live odds, scores, price history for ATP/WTA matches with Server-Sent Events streaming.

## Quick Start

```bash
pip install -r requirements.txt
python server.py
```

Open http://localhost:5333 in a browser, or use the frontend dashboards:
- `frontend/dashboard.html` — Live odds table (sortable, color-coded price movement)
- `frontend/chart.html` — Odds timeline chart (Chart.js, selectable match/player)
- `frontend/stream.html` — Raw SSE event log (debug view)

## Architecture

```
┌──────────────────────┐
│  Unibet.fr API       │  REST endpoints (live odds snapshots, event listings)
│  /services-api/...   │  Token auth via X-LVS-HSToken header
│  /lvs-api/...        │
└──────┬───────────────┘
       │  aiohttp (poll every 3s + delta every 2s)
       ▼
┌──────────────────────┐
│  api/                │  Token manager, HTTP client, data models
│  token.py            │  TokenManager — X-LVS-HSToken acquisition
│  client.py           │  Live/delta/event-listing fetchers + SSL connector
│  models.py           │  OddsUpdate, MatchMeta, PricePoint dataclasses
└──────┬───────────────┘
       │
       ▼
┌──────────────────────┐
│  feed/               │  Async feed engine
│  manager.py          │  run() — main poll loop with delta basket tracking
│  parser.py           │  Odds extraction (Face à Face, SSR, live state)
│  circuit_breaker.py  │  CircuitBreaker — opens after N consecutive failures
└──────┬───────────────┘
       │  asyncio.Queue (maxsize configurable)
       ▼
┌──────────────────────┐
│  server/             │  FastAPI + SSE + Broadcaster
│  app.py              │  Endpoints, lifespan, consume_feed() consumer
│  broadcaster.py      │  SSE fan-out with dead-client eviction
│                      │
│  GET /stream  (SSE)  │──▶ Browser clients (EventSource)
│  GET /prices (REST)  │
│  GET /markets         │
│  GET /history (SQL)   │
│  GET /status          │
│  GET /live-scores     │
└──────┬───────────────┘
       │  _db_queue (thread-safe)
       ▼
┌──────────────────────┐
│  storage/            │  SQLite persistence
│  sqlite.py           │  Daemon writer (500ms batch flush), read connection
└──────────────────────┘

config.py               pydantic-settings (UNIBET_ env prefix)
server.py               Entry point
```

## Package Structure

```
unibet_tennis_dashboard/
├── api/
│   ├── __init__.py
│   ├── token.py          TokenManager class
│   ├── client.py         HTTP functions, URL constants, SSL connector
│   └── models.py         OddsUpdate, MatchMeta, PricePoint
├── feed/
│   ├── __init__.py
│   ├── manager.py        run(), _refresh_match_list(), _odds_changed()
│   ├── parser.py         extract_face_a_face_odds(), extract_live_state(), etc.
│   └── circuit_breaker.py CircuitBreaker class
├── server/
│   ├── __init__.py
│   ├── app.py            FastAPI app, consume_feed(), all endpoints
│   └── broadcaster.py    SSE subscriber management + fan-out
├── storage/
│   ├── __init__.py
│   └── sqlite.py         _sqlite_writer(), query_history(), read connection
├── tests/
│   ├── conftest.py       Shared fixtures (captured API responses)
│   ├── test_parser.py    Odds extraction unit tests
│   ├── test_circuit_breaker.py  Open/close/reset behavior
│   └── test_storage.py   Write → flush → read round-trip
├── config.py             pydantic-settings (UNIBET_ prefix)
├── server.py             Entry point
├── requirements.txt
└── README.md
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/stream` | SSE — initial snapshot then live odds updates |
| GET | `/prices` | Current in-memory odds snapshot with price movements |
| GET | `/markets` | Active matches with full metadata |
| GET | `/status` | Feed health (streams, updates, SSE clients, matches) |
| GET | `/live-scores` | Live match scores only |
| GET | `/history?match_id=X&selection=Y&limit=500` | Price history from SQLite |

## Configuration

All settings via environment variables with `UNIBET_` prefix (pydantic-settings):

| Variable | Default | Description |
|----------|---------|-------------|
| `UNIBET_PORT` | 5333 | Server port |
| `UNIBET_DB_PATH` | `unibet_tennis_prices.db` | SQLite database path |
| `UNIBET_POLL_INTERVAL` | 3.0 | Seconds between live odds polls |
| `UNIBET_DELTA_POLL_INTERVAL` | 2.0 | Seconds between delta polls |
| `UNIBET_MATCH_REFRESH_MIN` | 5.0 | Minutes between full match list refresh |
| `UNIBET_MAX_MATCH_AGE_H` | 48.0 | Match window in hours |
| `UNIBET_DB_FLUSH_INTERVAL` | 0.5 | SQLite batch flush interval (seconds) |
| `UNIBET_DB_BATCH_SIZE` | 100 | Max rows before forced flush |
| `UNIBET_CIRCUIT_BREAKER_THRESHOLD` | 5 | Consecutive errors before pausing |
| `UNIBET_CIRCUIT_BREAKER_COOLDOWN` | 300.0 | Pause duration (seconds) |
| `UNIBET_SSL_VERIFY` | false | SSL certificate verification |
| `UNIBET_CONNECTION_POOL_LIMIT` | 20 | aiohttp connection pool |
| `UNIBET_FEED_QUEUE_MAXSIZE` | 20000 | asyncio.Queue capacity |
| `UNIBET_SSE_QUEUE_MAXSIZE` | 500 | Per-client SSE queue capacity |

## Latency Improvements

- **Delta basket polling**: Uses `/delta/events/live/topmarket/from/{basket}` for incremental updates — only fetches changed outcomes, not full snapshots
- **SQLite flush 500ms**: Batch writes flush every 500ms (down from 2s), cutting max chart staleness from ~5s to ~3s
- **Persistent read connection**: Single read-only SQLite connection (WAL mode, `query_only=ON`) shared across `/history` requests — no per-request connect overhead
- **Lock contention reduced**: `_detect_movement()` runs outside `prices_lock`, JSON serialization happens outside the lock, and endpoints deep-copy under lock then serialize outside

## Reverse-Engineered Unibet API

### Endpoints Discovered

| Endpoint | Purpose |
|----------|---------|
| `GET /lvs-api/acc/token` | Auth token acquisition |
| `GET /services-api/sportsbookdata/current/events/live/topmarket` | Live events + markets + outcomes (one flat dict) |
| `GET /services-api/sportsbookdata/delta/events/live/topmarket/from/{basket}` | Incremental odds updates |
| `GET /lvs-api/next/50/p{pathId}?...` | Event listing by competition (no odds) |
| `GET /paris-tennis/{cat}/{league}/{id}/{slug}` | Match page with SSR state (contains odds) |

### Data Model

The `/services-api/` returns a flat `items` dict keyed by prefix:

| Prefix | Type | Example | Content |
|--------|------|---------|---------|
| `l` | Live Event | `l3346070` | Match metadata, score, `parent` |
| `e` | Event (prematch) | `e3345964` | Match metadata, `eType=G` |
| `m` | Market | `m185435979` | `markettypeId=68` (Face à Face), `parent` |
| `o` | Outcome | `o684085990` | `price="1,85"`, `desc`, `parent` |
| `p` | Path | `p58535539` | Competition grouping |

### Market Types (Tennis)

| markettypeId | Description |
|-------------|-------------|
| 68 | Face à Face (Moneyline — Player A / Player B) |
| 69 | Vainqueur du set (Set winner) |
| 361 | Total des jeux (Total games O/U) |
| 840 | Score Exact (Exact score) |
| 10098 | Nombre de sets (Number of sets) |
| 120997 | Joueur remportera au moins 1 set |

## Stack

- Python 3.11+
- FastAPI + uvicorn
- aiohttp (async HTTP client)
- SQLite (WAL mode, daemon thread writer, persistent read connection)
- Server-Sent Events (no WebSocket dependency)
- pydantic-settings (configuration)
- Chart.js (CDN, chart page only)

## Running Tests

```bash
pytest tests/ -v
```
