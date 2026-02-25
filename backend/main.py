"""
FastAPI backend for the Brazilian Business Finder.

TWO-PHASE PIPELINE:
  Phase 1 — Search  (IDs only, FREE/unlimited):
    POST /api/runs  →  agent runs all search queries, stores place_ids

  Phase 2 — Enrich  (Pro Details, 5,000 free/month):
    POST /api/enrich  →  fetches Place Details for every unenriched candidate

Endpoints:
  GET    /                        - Map (public homepage)
  GET    /admin                   - Admin dashboard
  GET    /how-it-works            - How This Works explainer
  GET    /health                  - Health check (for Fly.io)
  POST   /api/runs                - Start a new search agent run
  GET    /api/runs                - List all runs
  GET    /api/runs/{run_id}       - Get run status and stats
  POST   /api/runs/{run_id}/stop  - Stop a running agent
  POST   /api/enrich              - Enrich all unenriched candidates (Place Details)
  GET    /api/candidates          - All candidates (includes enrichment status)
  GET    /api/candidates/map      - Enriched candidates with lat/lng (for map)
  GET    /api/stats               - Global stats + enrichment counts
  GET    /api/stats/overview      - Comprehensive stats for map sidebar
  GET    /api/queries/{run_id}    - Query-level log for a run
  GET    /api/logs/{run_id}       - Historical structured logs for a run
  WS     /ws/logs/{run_id}        - Live log stream
"""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import aiosqlite

from config import settings
from storage import db as storage
from log.run_logger import RunLogger, subscribe, unsubscribe
from agent.query_bank import SEED_QUERIES, get_query_count
from agent.graph import search_graph
from agent.scorer import run_scoring
from places.client import PlacesClient

# Registry of active run tasks so we can cancel them
_active_runs: dict[str, asyncio.Task] = {}
_stop_flags: dict[str, bool] = {}


def _require_places_key():
    if not settings.google_places_api_key:
        raise HTTPException(
            status_code=503,
            detail="Google Places API key not configured. Set GOOGLE_PLACES_API_KEY in environment.",
        )


def _require_openai_key():
    if not settings.openai_api_key:
        raise HTTPException(
            status_code=503,
            detail="OpenAI API key not configured. Set OPENAI_API_KEY in environment.",
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await storage.init_db()
    yield


app = FastAPI(title="Brazilian Business Finder", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Agent runner (runs as background asyncio task)
# ---------------------------------------------------------------------------

async def run_agent(run_id: str, config: dict):
    """Execute the LangGraph search agent for one run."""
    logger = RunLogger(run_id)
    await logger.info("RUN_START", {
        "run_id": run_id,
        "total_seed_queries": get_query_count(),
        "config": config,
    })

    async with PlacesClient() as client:
        initial_state: dict = {
            "run_id": run_id,
            "logger": logger,
            "places_client": client,
            "pending_queries": list(SEED_QUERIES),
            "completed_queries": [],
            "current_query": None,
            "search_result": None,
            "query_id": None,
            "query_elapsed_ms": 0,
            "novelty_window": [],
            "should_stop": False,
            "stop_reason": None,
            "total_queries_run": 0,
            "total_results_seen": 0,
            "total_new_candidates": 0,
        }

        try:
            async for chunk in search_graph.astream(initial_state, {"recursion_limit": 10000}):
                if _stop_flags.get(run_id):
                    await logger.warn("RUN_STOPPED_BY_USER", {})
                    await storage.mark_run_stopped(run_id, "user_requested")
                    return

            final_stats = {
                "total_queries": initial_state.get("total_queries_run", 0),
                "total_results": initial_state.get("total_results_seen", 0),
                "total_candidates": await storage.get_candidate_count(),
            }
            stop_reason = initial_state.get("stop_reason", "completed")

            await storage.finish_run(run_id, stop_reason, final_stats)
            await logger.info("RUN_COMPLETE", {
                "stop_reason": stop_reason,
                **final_stats,
            })

        except asyncio.CancelledError:
            await logger.warn("RUN_CANCELLED", {})
            await storage.mark_run_stopped(run_id, "cancelled")
        except Exception as e:
            await logger.error("RUN_ERROR", {"error": str(e)})
            await storage.mark_run_stopped(run_id, f"error: {e}")
        finally:
            _active_runs.pop(run_id, None)
            _stop_flags.pop(run_id, None)


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.post("/api/runs")
async def start_run(background_tasks: BackgroundTasks):
    """Start a new search agent run."""
    _require_places_key()
    config = {
        "max_queries_per_run": settings.max_queries_per_run,
        "max_candidates": settings.max_candidates,
        "novelty_floor": settings.novelty_floor,
        "seed_query_count": get_query_count(),
    }
    run_id = await storage.create_run(config)
    _stop_flags[run_id] = False

    task = asyncio.create_task(run_agent(run_id, config))
    _active_runs[run_id] = task

    return {
        "run_id": run_id,
        "status": "started",
        "seed_queries": get_query_count(),
        "config": config,
    }


@app.get("/api/runs")
async def list_runs():
    runs = await storage.list_runs()
    return {"runs": runs}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    run = await storage.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    query_stats = await storage.get_query_stats(run_id)
    candidate_count = await storage.get_candidate_count()
    is_active = run_id in _active_runs
    return {
        **run,
        "query_stats": query_stats,
        "candidate_count": candidate_count,
        "is_active": is_active,
    }


@app.post("/api/runs/{run_id}/stop")
async def stop_run(run_id: str):
    """Signal the agent to stop after its current query."""
    if run_id not in _active_runs:
        raise HTTPException(status_code=404, detail="No active run with that ID")
    _stop_flags[run_id] = True
    return {"run_id": run_id, "status": "stop_requested"}


@app.get("/api/candidates")
async def get_candidates(min_hits: int = 1, limit: int = 500):
    """All deduplicated candidates sorted by hit_count. Includes enrichment status."""
    candidates = await storage.get_all_candidates()
    filtered = [c for c in candidates if c["hit_count"] >= min_hits]
    enrichment = await storage.get_enrichment_counts()
    return {
        "total": len(filtered),
        "enrichment": enrichment,
        "candidates": filtered[:limit],
    }


@app.get("/api/candidates/map")
async def get_map_candidates(min_score: int = 0):
    """
    Enriched candidates with lat/lng for the map page.
    min_score=0  → all candidates with coordinates
    min_score=N  → only candidates scored >= N
    """
    places = await storage.get_map_candidates(min_score=min_score)
    return {"total": len(places), "places": places}


@app.get("/api/stats")
async def get_stats():
    runs = await storage.list_runs()
    enrichment = await storage.get_enrichment_counts()
    score_counts = await storage.get_score_counts()
    return {
        "total_runs": len(runs),
        "active_runs": list(_active_runs.keys()),
        "enrichment": enrichment,
        "scores": score_counts,
        "active_enrichment": _enrichment_task is not None and not _enrichment_task.done(),
        "active_scoring": _scoring_task is not None and not _scoring_task.done(),
        "keys": {
            "places": bool(settings.google_places_api_key),
            "openai": bool(settings.openai_api_key),
        },
    }


@app.get("/api/stats/overview")
async def get_stats_overview():
    """Comprehensive stats for the map sidebar."""
    overview = await storage.get_stats_overview()
    return overview


# ---------------------------------------------------------------------------
# Enrichment — Phase 2 (Pro SKU, 5,000 free/month)
# ---------------------------------------------------------------------------

_enrichment_task: asyncio.Task | None = None


async def _run_enrichment():
    """
    Fetch Place Details for every unenriched candidate.
    Uses the Pro SKU — one request per unique place_id.
    Runs as a background task; stops when all candidates are enriched.
    """
    place_ids = await storage.get_unenriched_place_ids()
    total = len(place_ids)

    if total == 0:
        return

    async with PlacesClient() as client:
        for i, place_id in enumerate(place_ids, 1):
            try:
                details = await client.get_place_details(place_id)
                if details:
                    await storage.enrich_candidate(place_id, {
                        "display_name": details.display_name,
                        "formatted_address": details.formatted_address,
                        "types": details.types,
                        "primary_type": details.primary_type,
                        "latitude": details.latitude,
                        "longitude": details.longitude,
                        "business_status": details.business_status,
                        "google_maps_uri": details.google_maps_uri,
                    })
            except Exception:
                pass  # log handled per-request in client; skip and continue

    global _enrichment_task
    _enrichment_task = None


@app.post("/api/enrich")
async def start_enrichment():
    """
    Start enriching all unenriched candidates with Place Details (Pro SKU).
    Safe to call multiple times — ignores if enrichment already running.
    """
    _require_places_key()
    global _enrichment_task
    if _enrichment_task and not _enrichment_task.done():
        counts = await storage.get_enrichment_counts()
        return {"status": "already_running", "enrichment": counts}

    counts = await storage.get_enrichment_counts()
    if counts["pending"] == 0:
        return {"status": "nothing_to_enrich", "enrichment": counts}

    _enrichment_task = asyncio.create_task(_run_enrichment())
    return {
        "status": "started",
        "pending": counts["pending"],
        "note": f"Will use ~{counts['pending']} Pro API requests (5,000 free/month)",
    }


@app.get("/api/enrich/status")
async def enrichment_status():
    counts = await storage.get_enrichment_counts()
    running = _enrichment_task is not None and not _enrichment_task.done()
    return {"running": running, "enrichment": counts}


# ---------------------------------------------------------------------------
# Scoring — Phase 3 (OpenAI, batched 10 at a time)
# ---------------------------------------------------------------------------

_scoring_task: asyncio.Task | None = None
_scoring_progress: dict = {"done": 0, "total": 0, "last_batch": []}


async def _run_scoring():
    global _scoring_progress, _scoring_task

    async def on_progress(done: int, total: int, batch_results: list):
        _scoring_progress = {"done": done, "total": total, "last_batch": batch_results}

    await run_scoring(progress_callback=on_progress)
    _scoring_task = None


@app.post("/api/score")
async def start_scoring():
    """
    Start scoring all enriched-but-unscored candidates for Brazilian likelihood.
    Uses OpenAI in batches of 10. Safe to call multiple times.
    """
    _require_openai_key()
    global _scoring_task
    if _scoring_task and not _scoring_task.done():
        counts = await storage.get_score_counts()
        return {"status": "already_running", "scores": counts, "progress": _scoring_progress}

    counts = await storage.get_score_counts()
    if counts["pending"] == 0:
        return {"status": "nothing_to_score", "scores": counts}

    _scoring_task = asyncio.create_task(_run_scoring())
    return {
        "status": "started",
        "pending": counts["pending"],
        "model": settings.scoring_model,
        "batch_size": settings.scoring_batch_size,
        "note": f"~{-(counts['pending'] // -settings.scoring_batch_size)} OpenAI requests for {counts['pending']} candidates",
    }


@app.get("/api/score/status")
async def scoring_status():
    counts = await storage.get_score_counts()
    running = _scoring_task is not None and not _scoring_task.done()
    return {
        "running": running,
        "scores": counts,
        "progress": _scoring_progress,
    }


@app.get("/api/queries/{run_id}")
async def get_queries(run_id: str, limit: int = 200):
    """Return query-level stats for a run."""
    async with aiosqlite.connect(settings.database_path) as db_conn:
        db_conn.row_factory = aiosqlite.Row
        async with db_conn.execute(
            """SELECT * FROM search_queries
               WHERE run_id = ?
               ORDER BY id DESC
               LIMIT ?""",
            (run_id, limit),
        ) as cur:
            rows = await cur.fetchall()
            return {"queries": [dict(r) for r in rows]}


@app.get("/api/logs/{run_id}")
async def get_logs(run_id: str, since_id: int = 0, limit: int = 500):
    """Return historical structured logs for a run."""
    logs = await storage.get_run_logs(run_id, since_id=since_id)
    return {"logs": logs[:limit]}


# ---------------------------------------------------------------------------
# WebSocket: live log stream
# ---------------------------------------------------------------------------

@app.websocket("/ws/logs/{run_id}")
async def websocket_logs(websocket: WebSocket, run_id: str):
    """Stream live log events for a run to a connected WebSocket client."""
    await websocket.accept()
    q = subscribe(run_id)

    # Send any logs already in DB (catch-up for late connections)
    existing = await storage.get_run_logs(run_id, since_id=0)
    for log in existing:
        await websocket.send_json(log)

    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=30.0)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                # Send a heartbeat ping
                await websocket.send_json({"event": "PING", "timestamp": ""})
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        unsubscribe(run_id, q)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "keys": {
            "places": bool(settings.google_places_api_key),
            "openai": bool(settings.openai_api_key),
        },
    }


# ---------------------------------------------------------------------------
# Serve frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=settings.frontend_dir), name="static")


@app.get("/")
async def serve_map():
    return FileResponse(f"{settings.frontend_dir}/map.html")


@app.get("/admin")
async def serve_admin():
    return FileResponse(f"{settings.frontend_dir}/index.html")


@app.get("/how-it-works")
async def serve_how_it_works():
    return FileResponse(f"{settings.frontend_dir}/how-it-works.html")
