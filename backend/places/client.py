"""
Google Places API (New) client.

TWO-PHASE DESIGN
================
Phase 1 — Text Search (IDs Only, UNLIMITED FREE)
  Endpoint : POST https://places.googleapis.com/v1/places:searchText
  FieldMask: places.id,nextPageToken
  Returns  : place_ids only — no name, no address, no location.
  Cost     : $0, no monthly cap.
  Purpose  : Spray wide across all query families; dedup by place_id.

Phase 2 — Place Details (Pro SKU, 5,000 free/month)
  Endpoint : GET  https://places.googleapis.com/v1/places/{place_id}
  FieldMask: displayName,formattedAddress,types,primaryType,
             location,businessStatus,googleMapsUri
  Cost     : 1 request per unique candidate, after dedup.
  Purpose  : Enrich only the deduplicated set with name/location/type.
             This is where we spend the paid budget — and only once per place.

Why this matters
----------------
If you run 300 queries × 3 pages each = 900 search requests — all FREE.
Those 900 requests might yield 15,000 raw results but only ~600 unique place_ids.
You then spend 600 Pro requests (out of your 5,000 free/month) on enrichment,
instead of 900 Pro requests on search pages. Better, and far more importantly:
the search phase is unlimited so you can run as many queries as you want.
"""

import asyncio
import time
import httpx
from dataclasses import dataclass
from typing import Any

from config import settings

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Phase 1: IDs only — UNLIMITED FREE
IDS_ONLY_MASK = "places.id,nextPageToken"

# Phase 2: Place Details — Pro SKU (5,000 free/month)
# Note: Place Details field mask has NO "places." prefix.
DETAILS_PRO_MASK = ",".join([
    "id",
    "displayName",
    "formattedAddress",
    "types",
    "primaryType",
    "location",
    "businessStatus",
    "googleMapsUri",
])

# Boston metro bounding box (covers city + Framingham/Marlborough/Brockton belt)
BOSTON_RESTRICTION = {
    "rectangle": {
        "low":  {"latitude": 42.2279, "longitude": -71.1912},
        "high": {"latitude": 42.4607, "longitude": -70.9297},
    }
}


@dataclass
class PlaceDetails:
    """Enriched place record from Place Details (Pro tier)."""
    place_id: str
    display_name: str
    formatted_address: str
    types: list[str]
    primary_type: str | None
    latitude: float | None
    longitude: float | None
    business_status: str | None
    google_maps_uri: str | None


@dataclass
class SearchPage:
    place_ids: list[str]
    next_page_token: str | None
    query: str
    page_num: int
    request_ms: int


@dataclass
class SearchResult:
    query: str
    pages: list[SearchPage]
    total_ids: int
    error: str | None = None


def _parse_details(raw: dict, place_id: str) -> PlaceDetails:
    loc = raw.get("location", {})
    name_obj = raw.get("displayName", {})
    return PlaceDetails(
        place_id=place_id,
        display_name=name_obj.get("text", "") if isinstance(name_obj, dict) else str(name_obj),
        formatted_address=raw.get("formattedAddress", ""),
        types=raw.get("types", []),
        primary_type=raw.get("primaryType"),
        latitude=loc.get("latitude"),
        longitude=loc.get("longitude"),
        business_status=raw.get("businessStatus"),
        google_maps_uri=raw.get("googleMapsUri"),
    )


class PlacesClient:
    """Async Google Places (New) client with rate limiting."""

    def __init__(self):
        self._last_request_time: float = 0.0
        self._min_interval = 1.0 / settings.requests_per_second
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self):
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def _rate_limit(self):
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request_time)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_time = time.monotonic()

    # ------------------------------------------------------------------
    # Phase 1: IDs-only text search (UNLIMITED FREE)
    # ------------------------------------------------------------------

    async def _search_page(self, query: str, page_token: str | None, page_num: int) -> SearchPage:
        await self._rate_limit()

        body: dict[str, Any] = {
            "textQuery": query,
            "pageSize": settings.places_page_size,
            "locationRestriction": BOSTON_RESTRICTION,
        }
        if page_token:
            body["pageToken"] = page_token

        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": settings.google_places_api_key,
            "X-Goog-FieldMask": IDS_ONLY_MASK,
        }

        t0 = time.monotonic()
        resp = await self._client.post(SEARCH_URL, json=body, headers=headers)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        resp.raise_for_status()

        data = resp.json()
        place_ids = [p["id"] for p in data.get("places", []) if p.get("id")]

        return SearchPage(
            place_ids=place_ids,
            next_page_token=data.get("nextPageToken"),
            query=query,
            page_num=page_num,
            request_ms=elapsed_ms,
        )

    async def search_all_pages(self, query: str) -> SearchResult:
        """Fetch up to max_pages_per_query pages, IDs only. FREE."""
        pages: list[SearchPage] = []
        page_token: str | None = None

        for page_num in range(1, settings.max_pages_per_query + 1):
            try:
                page = await self._search_page(query, page_token, page_num)
                pages.append(page)
                if not page.next_page_token or page_num >= settings.max_pages_per_query:
                    break
                page_token = page.next_page_token
            except httpx.HTTPStatusError as e:
                return SearchResult(
                    query=query, pages=pages,
                    total_ids=sum(len(p.place_ids) for p in pages),
                    error=f"HTTP {e.response.status_code}: {e.response.text[:400]}",
                )
            except Exception as e:
                return SearchResult(
                    query=query, pages=pages,
                    total_ids=sum(len(p.place_ids) for p in pages),
                    error=str(e),
                )

        return SearchResult(
            query=query, pages=pages,
            total_ids=sum(len(p.place_ids) for p in pages),
        )

    # ------------------------------------------------------------------
    # Phase 2: Place Details enrichment (Pro SKU, 5,000 free/month)
    # ------------------------------------------------------------------

    async def get_place_details(self, place_id: str) -> PlaceDetails | None:
        """
        Fetch full details for one place_id. Costs 1 Pro request.
        Returns None on error (caller should log and skip).
        """
        await self._rate_limit()

        url = DETAILS_URL.format(place_id=place_id)
        headers = {
            "X-Goog-Api-Key": settings.google_places_api_key,
            "X-Goog-FieldMask": DETAILS_PRO_MASK,
        }

        try:
            t0 = time.monotonic()
            resp = await self._client.get(url, headers=headers)
            resp.raise_for_status()
            return _parse_details(resp.json(), place_id)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return None  # place deleted / invalid ID
            raise
