# Brazilian Business Finder — Boston Metro

An agentic AI pipeline that systematically discovers, enriches, and scores Brazilian-owned and Brazilian-themed businesses across the Boston metro area, producing a geocoded, filterable public map.

Built for research use by the City of Boston Planning Department.

---

## Overview

Finding Brazilian businesses in Boston manually is slow, incomplete, and hard to keep current. This tool automates the process:

1. **Search** — fires 347 seed queries against Google Places (free, unlimited tier) collecting place IDs
2. **Expand** — an AI agent generates additional queries every 25 searches based on what's been found
3. **Dedup** — collapses duplicate results; the same churrascaria appearing in 30 queries counts once
4. **Enrich** — fetches name, address, type, and coordinates for each unique candidate (Pro tier, 5k free/month)
5. **Score** — GPT-4o-mini rates each business 0–100 for Brazilian likelihood, batched 10 at a time
6. **Map** — interactive Leaflet map with score filtering, shareable URLs, and a statistics sidebar

The pipeline is cost-efficient by design: the most expensive API call (Place Details) is made only once per unique business, not once per search result page.

---

## Screenshots

| Map (default: score ≥ 75) | Admin Dashboard |
|---|---|
| Filterable markers colored by score | Three-phase pipeline controls + live log |

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, uvicorn |
| AI agent | LangGraph (StateGraph), OpenAI GPT-4o-mini |
| Places data | Google Places API (New) — Text Search + Place Details |
| Storage | SQLite via aiosqlite |
| Frontend | Vanilla HTML/JS/CSS — no build step |
| Map | Leaflet.js + MarkerCluster, OpenStreetMap tiles (free) |
| Deployment | Fly.io with persistent volume |

---

## Project Structure

```
brazilian-businesses/
├── Dockerfile
├── fly.toml
├── .dockerignore
├── README.md
│
├── backend/
│   ├── main.py              # FastAPI app — all endpoints, WebSocket, background tasks
│   ├── config.py            # Pydantic settings (reads .env / environment variables)
│   ├── start.sh             # Local dev launcher (creates venv, installs deps, starts server)
│   ├── requirements.txt
│   ├── .env.example
│   │
│   ├── agent/
│   │   ├── query_bank.py    # 347 seed queries across 6 query families
│   │   ├── nodes.py         # LangGraph node functions (select, search, process, expand, stop)
│   │   ├── graph.py         # StateGraph definition
│   │   └── scorer.py        # OpenAI Brazilian likelihood scorer (batched, 0–100)
│   │
│   ├── places/
│   │   └── client.py        # Google Places async client — IDs-only search + Place Details
│   │
│   ├── storage/
│   │   └── db.py            # SQLite schema, migrations, and all async CRUD
│   │
│   └── log/
│       └── run_logger.py    # RunLogger: SQLite + JSONL file + WebSocket broadcast
│
└── frontend/
    ├── map.html             # Public map (homepage at /)
    ├── index.html           # Admin dashboard (at /admin)
    ├── how-it-works.html    # Pipeline explainer page (at /how-it-works)
    ├── app.js               # Admin dashboard JS — WebSocket, table, CSV export
    └── style.css            # Shared dark terminal theme
```

---

## Quick Start (Local)

### Prerequisites

- Python 3.12+
- A Google Cloud project with the **Places API (New)** enabled
- An OpenAI API key

> **No API keys?** The app still starts and serves the map and existing data. Keys are only required to run new searches, enrichment, or scoring.

### 1. Clone and configure

```bash
cd backend
cp .env.example .env
# Edit .env and add your keys
```

`.env` file:

```env
GOOGLE_PLACES_API_KEY=AIza...
OPENAI_API_KEY=sk-...
```

### 2. Start the server

```bash
cd backend
./start.sh
```

The script creates a virtualenv, installs dependencies, and starts uvicorn with auto-reload. The server runs on **http://localhost:8000**.

| URL | Page |
|---|---|
| `http://localhost:8000/` | Map (public homepage) |
| `http://localhost:8000/admin` | Admin dashboard |
| `http://localhost:8000/how-it-works` | Pipeline explainer |

---

## Running the Pipeline

### Phase 1 — Search (free, unlimited)

Open the Admin dashboard (`/admin`) and click **▶ Start Run**.

The LangGraph agent works through all 347 seed queries, calling the Google Places Text Search API in IDs-only mode (no name/address — just place IDs, which is completely free). Every 25 queries it asks GPT-4o-mini to suggest additional search terms based on the businesses found so far.

**Stopping conditions:**
- All seed + generated queries exhausted
- `MAX_QUERIES_PER_RUN` limit reached (default: 300)
- `MAX_CANDIDATES` limit reached (default: 3000)
- Novelty rate drops below `NOVELTY_FLOOR` (default: 5% — fewer than 1 in 20 results is new)

Results are deduplicated in real time. Hit count (how many queries found the same place) is a signal used later in scoring.

### Phase 2 — Enrich (Pro tier, 5,000 free/month)

Click **⬆ Enrich All Pending** in the Admin dashboard.

Fetches Place Details for every candidate that doesn't yet have coordinates: name, formatted address, business type, GPS lat/lng, Google Maps link. One API request per unique place ID.

**Why not enrich during search?**
The same business can appear in dozens of search result pages. By running IDs-only search first and deduplicating, you spend one enrichment call per business rather than one per result page. With 347 queries × 3 pages each (1,041 pages) but only ~600 unique businesses, this roughly halves the Pro API cost while allowing unlimited search volume.

### Phase 3 — Score (OpenAI)

Click **⭐ Score All Pending** in either the Admin dashboard or the map.

Sends enriched candidates to GPT-4o-mini in batches of 10. Each business receives a score (0–100) and a one-sentence reason.

**Score calibration:**

| Score | Meaning | Examples |
|---|---|---|
| 90–100 | Unmistakably Brazilian | Churrascaria Palace, Padaria Brasileira, Casa do Brasil |
| 75–89 | High confidence | Names with picanha, feijoada, coxinha, pão de queijo |
| 50–74 | Moderate | Some Portuguese cues, ambiguous |
| 20–49 | Weak indicators | Generic names, broad query matches only |
| 1–19 | Probably not | Brazilian wax salons, açaí chains, accidental matches |
| 0 | Not Brazilian | National chains, clearly wrong results |

Scoring is intentionally conservative — false positives (non-Brazilian businesses on the map) are treated as more harmful than false negatives (missing some real businesses).

---

## Map Features

- **Score threshold slider** — filter to show only businesses above a confidence level; defaults to ≥ 75
- **Preset buttons** — All / 50+ / 75+ / 90+
- **Shareable URLs** — `/#min=75` encodes the current filter; share button copies the exact URL
- **Statistics sidebar** — score distribution bars, top business types, search coverage stats
- **Score-colored markers** — green (high) → orange → dark red (low); size scales with score
- **Marker clusters** — tap a cluster to expand; count shown in each cluster bubble
- **Rich popups** — name, address, score + reason, matched search queries, Google Maps link

**Marker colors:**

| Color | Score |
|---|---|
| 🟢 `#00c853` dark green | 90–100 |
| 🟩 `#43a047` forest green | 75–89 |
| 🟠 `#f57c00` dark amber | 50–74 |
| 🔴 `#e53935` red | 20–49 |
| 🟣 `#880e4f` dark magenta | 1–19 |
| ⬛ `#455a64` slate | Not scored |

---

## API Reference

All endpoints are served from the FastAPI backend. Interactive docs available at `/docs` (Swagger UI).

### Runs (Phase 1 — Search)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/runs` | Start a new search agent run |
| `GET` | `/api/runs` | List all runs |
| `GET` | `/api/runs/{run_id}` | Run status, stats, active flag |
| `POST` | `/api/runs/{run_id}/stop` | Signal agent to stop after current query |

### Enrichment (Phase 2)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/enrich` | Enrich all unenriched candidates |
| `GET` | `/api/enrich/status` | Enrichment progress counts |

### Scoring (Phase 3)

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/score` | Score all unscored enriched candidates |
| `GET` | `/api/score/status` | Scoring progress + counts |

### Data

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/candidates` | All candidates with enrichment status |
| `GET` | `/api/candidates/map` | Enriched candidates with coords; `?min_score=N` to filter |
| `GET` | `/api/stats` | Global stats + active task flags + key status |
| `GET` | `/api/stats/overview` | Score distribution, top types, coverage stats (map sidebar) |
| `GET` | `/api/queries/{run_id}` | Per-query log for a run |
| `GET` | `/api/logs/{run_id}` | Historical structured logs |

### Utilities

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check — returns key configuration status |
| `WS` | `/ws/logs/{run_id}` | Live log stream (WebSocket) |

---

## Configuration

All settings are read from environment variables (or `.env` in the `backend/` directory).

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_PLACES_API_KEY` | *(none)* | Google Places API (New) key — required for Phase 1 & 2 |
| `OPENAI_API_KEY` | *(none)* | OpenAI key — required for Phase 3 scoring and query expansion |
| `DATABASE_PATH` | `data/candidates.db` | SQLite database file path |
| `LOG_PATH` | `data/logs` | Directory for per-run JSONL log files |
| `FRONTEND_DIR` | `../frontend` | Path to frontend directory (relative to `backend/`) |
| `MAX_QUERIES_PER_RUN` | `300` | Hard cap on queries per run |
| `MAX_CANDIDATES` | `3000` | Stop searching once this many unique candidates found |
| `NOVELTY_FLOOR` | `0.05` | Stop if fewer than 5% of recent results are new |
| `NOVELTY_WINDOW_SIZE` | `10` | Rolling window size for novelty rate calculation |
| `PLACES_PAGE_SIZE` | `20` | Results per search page (max 20) |
| `MAX_PAGES_PER_QUERY` | `3` | Pages to fetch per query (max 60 results/query) |
| `REQUESTS_PER_SECOND` | `2.0` | Rate limit for Places API calls |
| `OPENAI_MODEL` | `gpt-4o-mini` | Model for query expansion |
| `SCORING_MODEL` | `gpt-4o-mini` | Model for Brazilian likelihood scoring |
| `SCORING_BATCH_SIZE` | `10` | Businesses per OpenAI scoring request |

---

## Search Query Design

The 347 seed queries are organized into six families, targeting different ways a Brazilian business might appear on Google Maps:

| Family | Examples | Count |
|---|---|---|
| **A. Food vocabulary** | `churrascaria Boston`, `pão de queijo Boston`, `brigadeiro Boston` | ~40 |
| **B. Service types** | `Brazilian wax Boston`, `Brazilian immigration lawyer Boston` | ~20 |
| **C. Portuguese language** | `comida brasileira Boston`, `mercado brasileiro Boston` | ~30 |
| **D. Brand patterns** | `Casa do Brasil`, `Sabor do Brasil`, `Cantinho Brasileiro` | ~25 |
| **E. Priority × neighborhood** | `churrascaria Framingham`, `pão de queijo Allston` | ~180 |
| **F. Catch-all geographic** | `Brazilian Framingham`, `Brazilian Brockton` | ~50 |

Neighborhoods include all 23 Boston metro areas with significant Brazilian populations: Allston, Somerville, Cambridge, Framingham, Marlborough, Brockton, Everett, Malden, and others.

The LangGraph agent also generates additional queries via GPT-4o-mini every 25 searches, expanding coverage based on business names and types found so far.

---

## Google Places API Tiers

Understanding the API tiers is important for cost planning:

| Tier | Fields | Free quota | Cost above quota |
|---|---|---|---|
| **IDs Only** | `places.id` only | **Unlimited** | $0 |
| **Essentials** | address, location, types | 10,000/month | $17/1,000 |
| **Pro** | displayName, primaryType, businessStatus | 5,000/month | $17/1,000 |
| **Enterprise** | phone, website, rating, reviews | 1,000/month | $25/1,000 |

This tool uses:
- **IDs Only** for all text searches (Phase 1) — free regardless of volume
- **Pro** for Place Details enrichment (Phase 2) — one call per unique business

With 5,000 free Pro calls/month and typical runs finding 400–1,100 unique businesses, Phase 2 is free within the monthly quota for most use cases.

---

## Deployment (Fly.io)

### First deploy

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh
fly auth login

# Create the app (choose a unique name, then update fly.toml)
fly apps create your-app-name

# Create the persistent data volume
fly volumes create data --size 1 --region iad

# Set API key secrets (app works without these, just can't run new searches)
fly secrets set GOOGLE_PLACES_API_KEY=AIza... OPENAI_API_KEY=sk-...

# Deploy
fly deploy
```

### Upload existing database

The Fly.io volume starts empty. To upload your local database:

```bash
# Delete the empty file created on first boot (if it exists)
fly ssh console -C "rm -f /data/candidates.db"

# Upload your local database
fly sftp put backend/data/candidates.db /data/candidates.db
```

### Subsequent deploys

```bash
fly deploy
```

The volume at `/data` persists across deploys. The SQLite database and log files are never touched by a redeploy.

### Cost

Fly.io free tier includes 3 shared-CPU VMs and 3GB total volume storage. This app is configured to auto-stop when idle (`auto_stop_machines = "stop"`) and auto-start on request, so it costs nothing when not in use beyond the volume storage (~$0.15/GB/month).

---

## Data Export

From the Admin dashboard (`/admin`), the **↓ Export CSV** button downloads all candidates as a spreadsheet with columns: place ID, name, address, type, score, score reason, hit count, matched queries, Google Maps URL, enrichment status, timestamps.

This CSV can be loaded directly into QGIS, ArcGIS, Excel, or any GIS tool for further analysis.

---

## Development Notes

### Adding more seed queries

Edit `backend/agent/query_bank.py`. Queries are assembled in `build_seed_queries()`. Add new terms to the relevant section or extend the neighborhood list.

### Adjusting scoring calibration

Edit the `SYSTEM_PROMPT` in `backend/agent/scorer.py`. The calibration anchors (score ranges with concrete examples) are the most important part — keep them consistent to get reproducible scores across batches.

### SQLite schema changes

Add migrations to the `MIGRATIONS` list in `backend/storage/db.py`. Migrations run before the DDL on every startup (they're caught silently if the column already exists), so they're safe to run against an existing database.

### Extending the geographic coverage

The Boston bounding box is defined in `backend/places/client.py`:

```python
BOSTON_RESTRICTION = {
    "rectangle": {
        "low":  {"latitude": 42.2279, "longitude": -71.1912},
        "high": {"latitude": 42.4607, "longitude": -70.9297},
    }
}
```

Adjust these coordinates or switch to `locationBias` (soft preference rather than hard restriction) for broader coverage.

---

## License

Research tool built for the City of Boston Planning Department. Contact for reuse.
