"""
LangGraph StateGraph for the Brazilian business search agent.

State schema and graph wiring.
"""

from typing import Any
from langgraph.graph import StateGraph, END, START

from agent.nodes import (
    select_query,
    execute_search,
    process_results,
    expand_queries,
    check_termination,
)


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class AgentState(dict):
    """
    Typed dict (as plain dict for LangGraph compatibility).

    Keys:
      run_id          str           - unique run identifier
      logger          RunLogger     - scoped logger (not serialized)
      places_client   PlacesClient  - shared HTTP client (not serialized)

      pending_queries list[str]     - queries not yet executed
      completed_queries list[str]   - queries already executed (normalized lowercase)
      current_query   str | None    - query being executed right now

      search_result   Any | None    - result from places client
      query_id        int | None    - DB id of the current search_queries row
      query_elapsed_ms int          - ms taken for last search

      novelty_window  list[int]     - new candidates per query, rolling window
      should_stop     bool
      stop_reason     str | None

      total_queries_run    int
      total_results_seen   int
      total_new_candidates int
    """


def build_graph():
    """Build and compile the LangGraph search loop."""

    def route_after_termination(state: dict) -> str:
        if state.get("should_stop") or not state.get("pending_queries"):
            return "end"
        return "select_query"

    builder = StateGraph(dict)

    builder.add_node("select_query", select_query)
    builder.add_node("execute_search", execute_search)
    builder.add_node("process_results", process_results)
    builder.add_node("expand_queries", expand_queries)
    builder.add_node("check_termination", check_termination)

    builder.add_edge(START, "select_query")
    builder.add_edge("select_query", "execute_search")
    builder.add_edge("execute_search", "process_results")
    builder.add_edge("process_results", "expand_queries")
    builder.add_edge("expand_queries", "check_termination")

    builder.add_conditional_edges(
        "check_termination",
        route_after_termination,
        {"select_query": "select_query", "end": END},
    )

    return builder.compile()


# Compile once at import time
search_graph = build_graph()
