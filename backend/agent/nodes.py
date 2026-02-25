"""
LangGraph node functions for the Brazilian business search agent.

Graph flow:
  START → select_query → execute_search → process_results
        ↑                                       |
        └───────────────────────────────────────┤ (loop)
                                                ↓
                              [every EXPAND_EVERY queries]
                                        expand_queries (OpenAI)
                                                |
                                                ↓
                              check_termination → END (if done)
                                        |
                                        └→ select_query (if continuing)
"""

import asyncio
import json
import time
from typing import Any

from openai import AsyncOpenAI

from config import settings
from places.client import PlacesClient
from storage import db as storage
from log.run_logger import RunLogger

# How often (in queries) to call OpenAI for query expansion
EXPAND_EVERY = 25

openai_client = AsyncOpenAI(api_key=settings.openai_api_key)


# ---------------------------------------------------------------------------
# Node: select_query
# ---------------------------------------------------------------------------

async def select_query(state: dict) -> dict:
    """Pick the next pending query, skipping already-executed ones."""
    logger: RunLogger = state["logger"]
    pending: list[str] = state["pending_queries"]
    done_set: set[str] = set(state["completed_queries"])

    # Find first query not yet executed
    next_query = None
    remaining = []
    for q in pending:
        if q.lower() not in done_set:
            if next_query is None:
                next_query = q
            else:
                remaining.append(q)
        # already done queries are just dropped

    if next_query is None:
        await logger.info("QUERY_EXHAUSTED", {"pending": 0})
        return {
            **state,
            "current_query": None,
            "pending_queries": [],
            "should_stop": True,
            "stop_reason": "query_space_exhausted",
        }

    await logger.info("QUERY_SELECTED", {
        "query": next_query,
        "remaining": len(remaining),
        "completed": len(done_set),
    })

    return {
        **state,
        "current_query": next_query,
        "pending_queries": remaining,
    }


# ---------------------------------------------------------------------------
# Node: execute_search
# ---------------------------------------------------------------------------

async def execute_search(state: dict) -> dict:
    """Call the Places API for the current query and log outcomes."""
    logger: RunLogger = state["logger"]
    query: str = state["current_query"]
    run_id: str = state["run_id"]
    places_client: PlacesClient = state["places_client"]

    query_id = await storage.log_query_start(run_id, query)
    t0 = time.monotonic()

    result = await places_client.search_all_pages(query)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    pages_fetched = len(result.pages)
    total_results = result.total_results

    if result.error:
        await storage.log_query_complete(
            query_id, pages_fetched, total_results,
            new_candidates=0, duplicate_candidates=0,
            duration_ms=elapsed_ms, error=result.error,
        )
        await logger.error("SEARCH_ERROR", {
            "query": query,
            "error": result.error,
            "pages_fetched": pages_fetched,
        })
        # Still mark as completed so we don't retry
        done = set(state["completed_queries"])
        done.add(query.lower())
        return {
            **state,
            "completed_queries": list(done),
            "search_result": None,
            "query_id": query_id,
            "query_elapsed_ms": elapsed_ms,
        }

    await logger.info("SEARCH_COMPLETE", {
        "query": query,
        "pages": pages_fetched,
        "ids_found": total_results,
        "ms": elapsed_ms,
        "tier": "IDs-only (free/unlimited)",
    })

    return {
        **state,
        "search_result": result,
        "query_id": query_id,
        "query_elapsed_ms": elapsed_ms,
    }


# ---------------------------------------------------------------------------
# Node: process_results
# ---------------------------------------------------------------------------

async def process_results(state: dict) -> dict:
    """Deduplicate and persist results, update novelty tracking."""
    logger: RunLogger = state["logger"]
    query: str = state["current_query"]
    query_id: int = state["query_id"]
    elapsed_ms: int = state["query_elapsed_ms"]
    result = state["search_result"]

    done_set = set(state["completed_queries"])
    done_set.add(query.lower())

    total_queries_run = state["total_queries_run"] + 1
    total_results_seen = state["total_results_seen"]
    total_new_candidates = state["total_new_candidates"]

    if result is None:
        # Error case - no results to process
        return {
            **state,
            "completed_queries": list(done_set),
            "total_queries_run": total_queries_run,
            "novelty_window": state["novelty_window"] + [0],
        }

    # Collect all place_ids from all pages (IDs-only response)
    all_place_ids: list[str] = []
    for page in result.pages:
        all_place_ids.extend(page.place_ids)

    new_count = 0
    dupe_count = 0

    for place_id in all_place_ids:
        is_new = await storage.upsert_place_id(place_id, query)
        if is_new:
            new_count += 1
            total_new_candidates += 1
            await logger.debug("NEW_PLACE_ID", {
                "place_id": place_id,
                "query": query,
            })
        else:
            dupe_count += 1

    total_results_seen += len(all_place_ids)

    await storage.log_query_complete(
        query_id,
        pages_fetched=len(result.pages),
        results_total=len(all_place_ids),
        new_candidates=new_count,
        duplicate_candidates=dupe_count,
        duration_ms=elapsed_ms,
    )

    novelty_rate = new_count / max(len(all_place_ids), 1)
    novelty_window = (state["novelty_window"] + [new_count])[-settings.novelty_window_size:]

    total_candidates = await storage.get_candidate_count()

    await logger.info("RESULTS_PROCESSED", {
        "query": query,
        "new": new_count,
        "dupes": dupe_count,
        "novelty_rate": round(novelty_rate, 3),
        "total_candidates": total_candidates,
        "total_queries_run": total_queries_run,
    })

    return {
        **state,
        "completed_queries": list(done_set),
        "total_queries_run": total_queries_run,
        "total_results_seen": total_results_seen,
        "total_new_candidates": total_new_candidates,
        "novelty_window": novelty_window,
    }


# ---------------------------------------------------------------------------
# Node: expand_queries (OpenAI-powered)
# ---------------------------------------------------------------------------

async def expand_queries(state: dict) -> dict:
    """
    Every EXPAND_EVERY queries, ask OpenAI to suggest new query terms
    based on what has been found so far.
    """
    logger: RunLogger = state["logger"]
    total_queries_run: int = state["total_queries_run"]

    # Only expand at the right interval
    if total_queries_run % EXPAND_EVERY != 0:
        return state

    await logger.info("EXPAND_START", {"queries_run_so_far": total_queries_run})

    # In IDs-only mode we don't have business names yet, so use high-hit queries
    # as the signal: queries that found lots of NEW place_ids are clearly productive.
    done_queries = state["completed_queries"][-40:]  # most recent completed queries
    total_candidates = await storage.get_candidate_count()

    prompt = f"""You are helping find Brazilian-owned or Brazilian-themed businesses in the Boston metro area.

We are running Google Places text search queries (IDs-only, free tier) to collect place_ids before enrichment.

Current stats:
- Queries run so far: {total_queries_run}
- Unique place_ids collected: {total_candidates}

Sample of queries already run (last 40):
{json.dumps(done_queries, indent=2)}

Suggest 10-15 NEW search query strings that might discover MORE Brazilian businesses we haven't found yet.

Rules:
- Each query should be a short phrase someone might search on Google Maps
- Focus on: Portuguese words, Brazilian food terms, Brazilian cultural terms, business name patterns
- Avoid near-duplicates of queries already run
- Include neighborhood variants for queries likely to have geographic spread
- Boston metro neighborhoods: Allston, Brighton, East Boston, Everett, Chelsea, Somerville, Framingham, Marlborough, Brockton

Return ONLY a JSON array of strings, no explanation:
["query 1", "query 2", ...]"""

    try:
        resp = await openai_client.chat.completions.create(
            model=settings.openai_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=500,
        )
        raw = resp.choices[0].message.content.strip()

        # Parse JSON array from response
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            new_queries = json.loads(raw[start:end])
            new_queries = [q for q in new_queries if isinstance(q, str) and q.strip()]
        else:
            new_queries = []

        await logger.info("EXPAND_DONE", {
            "new_queries_suggested": len(new_queries),
            "queries": new_queries,
        })

        # Prepend new queries to the pending list (run them next)
        updated_pending = new_queries + list(state["pending_queries"])
        return {**state, "pending_queries": updated_pending}

    except Exception as e:
        await logger.error("EXPAND_ERROR", {"error": str(e)})
        return state


# ---------------------------------------------------------------------------
# Node: check_termination
# ---------------------------------------------------------------------------

async def check_termination(state: dict) -> dict:
    """Decide whether to continue or stop the loop."""
    logger: RunLogger = state["logger"]

    if state.get("should_stop"):
        return state

    total_queries_run = state["total_queries_run"]
    total_new_candidates = state["total_new_candidates"]
    novelty_window: list[int] = state["novelty_window"]
    pending: list[str] = state["pending_queries"]

    # Hard caps
    if total_queries_run >= settings.max_queries_per_run:
        reason = f"max_queries_reached ({settings.max_queries_per_run})"
        await logger.warn("STOPPING", {"reason": reason})
        return {**state, "should_stop": True, "stop_reason": reason}

    total_candidates = await storage.get_candidate_count()
    if total_candidates >= settings.max_candidates:
        reason = f"max_candidates_reached ({settings.max_candidates})"
        await logger.warn("STOPPING", {"reason": reason})
        return {**state, "should_stop": True, "stop_reason": reason}

    # No more queries
    if not pending:
        reason = "query_space_exhausted"
        await logger.info("STOPPING", {"reason": reason})
        return {**state, "should_stop": True, "stop_reason": reason}

    # Novelty check: if the rolling window is large enough and avg new-per-query is tiny
    if len(novelty_window) >= settings.novelty_window_size:
        avg_new = sum(novelty_window) / len(novelty_window)
        total_per_query = settings.places_page_size  # rough denominator
        novelty_rate = avg_new / total_per_query
        if novelty_rate < settings.novelty_floor:
            reason = f"novelty_floor_reached (avg {avg_new:.1f} new/query)"
            await logger.warn("STOPPING", {"reason": reason, "novelty_rate": novelty_rate})
            return {**state, "should_stop": True, "stop_reason": reason}

    return {**state, "should_stop": False}
