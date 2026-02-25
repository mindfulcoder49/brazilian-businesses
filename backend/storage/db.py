"""
SQLite storage layer using aiosqlite.

Candidates go through two phases:
  1. Search phase  — place_id stored, enriched_at IS NULL
  2. Enrich phase  — all rich fields populated, enriched_at set

The map only shows enriched candidates (they have lat/lng).
"""

import aiosqlite
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from config import settings

DB_PATH = settings.database_path

DDL = """
CREATE TABLE IF NOT EXISTS candidates (
    place_id          TEXT PRIMARY KEY,
    -- populated after enrichment
    display_name      TEXT,
    formatted_address TEXT,
    types             TEXT DEFAULT '[]',   -- JSON array
    primary_type      TEXT,
    latitude          REAL,
    longitude         REAL,
    business_status   TEXT,
    google_maps_uri   TEXT,
    enriched_at       TEXT,               -- NULL until Phase 2 enrichment runs
    -- populated at search time
    query_sources     TEXT NOT NULL DEFAULT '[]',   -- JSON array of query strings
    hit_count         INTEGER NOT NULL DEFAULT 1,
    first_seen_query  TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_queries (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               TEXT NOT NULL,
    query_text           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    pages_fetched        INTEGER DEFAULT 0,
    results_total        INTEGER DEFAULT 0,
    new_candidates       INTEGER DEFAULT 0,
    duplicate_candidates INTEGER DEFAULT 0,
    executed_at          TEXT,
    completed_at         TEXT,
    duration_ms          INTEGER,
    error_message        TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           TEXT PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'running',
    total_queries    INTEGER DEFAULT 0,
    total_results    INTEGER DEFAULT 0,
    total_candidates INTEGER DEFAULT 0,
    started_at       TEXT NOT NULL,
    completed_at     TEXT,
    stop_reason      TEXT,
    config           TEXT
);

CREATE TABLE IF NOT EXISTS run_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    level     TEXT NOT NULL,
    event     TEXT NOT NULL,
    data      TEXT,
    timestamp TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_candidates_enriched  ON candidates(enriched_at);
CREATE INDEX IF NOT EXISTS idx_candidates_score     ON candidates(brazil_score);
CREATE INDEX IF NOT EXISTS idx_sq_run_id            ON search_queries(run_id);
CREATE INDEX IF NOT EXISTS idx_run_logs_run_id      ON run_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_run_logs_timestamp   ON run_logs(timestamp);
"""

MIGRATIONS = [
    "ALTER TABLE candidates ADD COLUMN enriched_at TEXT",
    "ALTER TABLE candidates ADD COLUMN brazil_score REAL",
    "ALTER TABLE candidates ADD COLUMN score_reason TEXT",
    "ALTER TABLE candidates ADD COLUMN scored_at TEXT",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Migrations run FIRST so any new columns exist before DDL tries to
        # create indexes on them. Each ALTER is caught silently — it either
        # succeeds (column didn't exist) or errors (column already there).
        for sql in MIGRATIONS:
            try:
                await db.execute(sql)
                await db.commit()
            except Exception:
                pass  # column already exists, or table doesn't exist yet (new install)

        # Now create tables + indexes. CREATE TABLE/INDEX IF NOT EXISTS is safe
        # to run repeatedly; columns added by migrations above are now present.
        await db.executescript(DDL)


# ---------------------------------------------------------------------------
# Run management
# ---------------------------------------------------------------------------

async def create_run(run_config: dict) -> str:
    run_id = str(uuid.uuid4())
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO runs (run_id, status, started_at, config) VALUES (?, 'running', ?, ?)",
            (run_id, _now(), json.dumps(run_config)),
        )
        await db.commit()
    return run_id


async def finish_run(run_id: str, stop_reason: str, stats: dict):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE runs SET status='completed', completed_at=?, stop_reason=?,
               total_queries=?, total_results=?, total_candidates=?
               WHERE run_id=?""",
            (_now(), stop_reason,
             stats.get("total_queries", 0), stats.get("total_results", 0),
             stats.get("total_candidates", 0), run_id),
        )
        await db.commit()


async def mark_run_stopped(run_id: str, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE runs SET status='stopped', completed_at=?, stop_reason=? WHERE run_id=?",
            (_now(), reason, run_id),
        )
        await db.commit()


async def get_run(run_id: str) -> dict | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def list_runs() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM runs ORDER BY started_at DESC") as cur:
            return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Candidate — Phase 1: store place_id from IDs-only search (FREE)
# ---------------------------------------------------------------------------

async def upsert_place_id(place_id: str, query: str) -> bool:
    """
    Record a place_id found during IDs-only text search.
    Returns True if this is a NEW candidate (never seen before).
    On duplicate: increment hit_count, append query to query_sources.
    enriched_at is left NULL — enrichment happens separately.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT hit_count, query_sources FROM candidates WHERE place_id=?",
            (place_id,),
        ) as cur:
            existing = await cur.fetchone()

        now = _now()
        if existing is None:
            await db.execute(
                """INSERT INTO candidates
                   (place_id, query_sources, hit_count, first_seen_query, created_at, updated_at)
                   VALUES (?, ?, 1, ?, ?, ?)""",
                (place_id, json.dumps([query]), query, now, now),
            )
            await db.commit()
            return True
        else:
            sources = json.loads(existing["query_sources"])
            if query not in sources:
                sources.append(query)
            await db.execute(
                "UPDATE candidates SET hit_count=hit_count+1, query_sources=?, updated_at=? WHERE place_id=?",
                (json.dumps(sources), now, place_id),
            )
            await db.commit()
            return False


# ---------------------------------------------------------------------------
# Candidate — Phase 2: enrich with Place Details (Pro SKU)
# ---------------------------------------------------------------------------

async def enrich_candidate(place_id: str, details: dict):
    """Update a candidate with enriched data from Place Details API."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE candidates SET
               display_name=?, formatted_address=?, types=?, primary_type=?,
               latitude=?, longitude=?, business_status=?, google_maps_uri=?,
               enriched_at=?, updated_at=?
               WHERE place_id=?""",
            (
                details.get("display_name"),
                details.get("formatted_address"),
                json.dumps(details.get("types", [])),
                details.get("primary_type"),
                details.get("latitude"),
                details.get("longitude"),
                details.get("business_status"),
                details.get("google_maps_uri"),
                _now(),
                _now(),
                place_id,
            ),
        )
        await db.commit()


async def get_unenriched_place_ids() -> list[str]:
    """Return place_ids that have no location data yet (latitude IS NULL)."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT place_id FROM candidates WHERE latitude IS NULL ORDER BY hit_count DESC"
        ) as cur:
            return [r[0] for r in await cur.fetchall()]


async def get_enrichment_counts() -> dict:
    """Count based on latitude presence — works for records inserted before enriched_at existed."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT
               COUNT(*) as total,
               SUM(CASE WHEN latitude IS NOT NULL THEN 1 ELSE 0 END) as enriched,
               SUM(CASE WHEN latitude IS NULL     THEN 1 ELSE 0 END) as pending
               FROM candidates"""
        ) as cur:
            row = await cur.fetchone()
            return {"total": row[0] or 0, "enriched": row[1] or 0, "pending": row[2] or 0}


# ---------------------------------------------------------------------------
# Candidate reads
# ---------------------------------------------------------------------------

async def get_all_candidates() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM candidates ORDER BY hit_count DESC, created_at ASC"
        ) as cur:
            results = []
            for r in await cur.fetchall():
                d = dict(r)
                d["types"] = json.loads(d["types"] or "[]")
                d["query_sources"] = json.loads(d["query_sources"] or "[]")
                d["enriched"] = d["latitude"] is not None
                results.append(d)
            return results


async def get_map_candidates(min_score: int = 0) -> list[dict]:
    """
    Return enriched candidates with valid coordinates.
    min_score=0  → all candidates with lat/lng (scored and unscored)
    min_score>0  → only candidates where brazil_score >= min_score
    """
    if min_score > 0:
        where = "WHERE latitude IS NOT NULL AND longitude IS NOT NULL AND brazil_score >= ?"
        params: tuple = (min_score,)
    else:
        where = "WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
        params = ()

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""SELECT place_id, display_name, formatted_address, primary_type,
                       latitude, longitude, business_status, google_maps_uri,
                       hit_count, query_sources, brazil_score, score_reason
                FROM candidates
                {where}
                ORDER BY brazil_score DESC NULLS LAST, hit_count DESC""",
            params,
        ) as cur:
            results = []
            for r in await cur.fetchall():
                d = dict(r)
                d["query_sources"] = json.loads(d["query_sources"] or "[]")
                results.append(d)
            return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

async def get_unscored_candidates() -> list[dict]:
    """Enriched candidates (have lat/lng) that haven't been scored yet."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT place_id, display_name, formatted_address, primary_type,
                      types, hit_count, query_sources
               FROM candidates
               WHERE latitude IS NOT NULL AND brazil_score IS NULL
               ORDER BY hit_count DESC"""
        ) as cur:
            results = []
            for r in await cur.fetchall():
                d = dict(r)
                d["types"] = json.loads(d["types"] or "[]")
                d["query_sources"] = json.loads(d["query_sources"] or "[]")
                results.append(d)
            return results


async def set_score(place_id: str, score: int, reason: str):
    """Persist a score for one candidate."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE candidates SET brazil_score=?, score_reason=?, scored_at=? WHERE place_id=?",
            (score, reason, _now(), place_id),
        )
        await db.commit()


async def get_score_counts() -> dict:
    """Distribution stats for scoring progress."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT
               COUNT(*) as total,
               SUM(CASE WHEN brazil_score IS NOT NULL               THEN 1 ELSE 0 END) as scored,
               SUM(CASE WHEN brazil_score IS NULL AND latitude IS NOT NULL THEN 1 ELSE 0 END) as pending,
               SUM(CASE WHEN brazil_score >= 75                     THEN 1 ELSE 0 END) as high,
               SUM(CASE WHEN brazil_score >= 50 AND brazil_score < 75 THEN 1 ELSE 0 END) as medium,
               SUM(CASE WHEN brazil_score < 50  AND brazil_score IS NOT NULL THEN 1 ELSE 0 END) as low
               FROM candidates"""
        ) as cur:
            r = await cur.fetchone()
            return {
                "total": r[0] or 0,
                "scored": r[1] or 0,
                "pending": r[2] or 0,
                "high_confidence": r[3] or 0,    # score >= 75
                "medium_confidence": r[4] or 0,  # 50-74
                "low_confidence": r[5] or 0,     # < 50
            }


async def get_candidate_count() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM candidates") as cur:
            return (await cur.fetchone())[0]


# ---------------------------------------------------------------------------
# Search query tracking
# ---------------------------------------------------------------------------

async def log_query_start(run_id: str, query_text: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO search_queries (run_id, query_text, status, executed_at) VALUES (?, ?, 'running', ?)",
            (run_id, query_text, _now()),
        )
        await db.commit()
        return cur.lastrowid


async def log_query_complete(
    query_id: int, pages_fetched: int, results_total: int,
    new_candidates: int, duplicate_candidates: int, duration_ms: int,
    error: str | None = None,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE search_queries SET
               status=?, pages_fetched=?, results_total=?,
               new_candidates=?, duplicate_candidates=?,
               completed_at=?, duration_ms=?, error_message=?
               WHERE id=?""",
            ("error" if error else "done", pages_fetched, results_total,
             new_candidates, duplicate_candidates, _now(), duration_ms, error, query_id),
        )
        await db.commit()


async def get_query_stats(run_id: str) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            """SELECT COUNT(*),
               SUM(CASE WHEN status='done'  THEN 1 ELSE 0 END),
               SUM(CASE WHEN status='error' THEN 1 ELSE 0 END),
               SUM(results_total), SUM(new_candidates), SUM(duplicate_candidates)
               FROM search_queries WHERE run_id=?""",
            (run_id,),
        ) as cur:
            r = await cur.fetchone()
            return {
                "total": r[0] or 0, "done": r[1] or 0, "errors": r[2] or 0,
                "total_results": r[3] or 0, "total_new": r[4] or 0, "total_dupes": r[5] or 0,
            }


# ---------------------------------------------------------------------------
# Run logs
# ---------------------------------------------------------------------------

async def append_log(run_id: str, level: str, event: str, data: Any = None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO run_logs (run_id, level, event, data, timestamp) VALUES (?, ?, ?, ?, ?)",
            (run_id, level, event, json.dumps(data) if data is not None else None, _now()),
        )
        await db.commit()


async def get_run_logs(run_id: str, since_id: int = 0) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM run_logs WHERE run_id=? AND id>? ORDER BY id ASC",
            (run_id, since_id),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]
