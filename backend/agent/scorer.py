"""
OpenAI-powered Brazilian likelihood scorer.

Processes enriched candidates in batches of BATCH_SIZE, asking the model
to score each 0-100 for probability of being a Brazilian-owned or
Brazilian-themed business.

Design priorities:
  - Reduce false positives above all else. Conservative calibration.
  - Batch 10 places per OpenAI call to share the system prompt cost.
  - Low temperature (0.1) for consistent, reproducible scoring.
  - query_sources (which search queries found the place) is treated as
    the strongest available signal.
"""

import asyncio
import json

from openai import AsyncOpenAI

from config import settings
from storage import db as storage

BATCH_SIZE = settings.scoring_batch_size

openai_client = AsyncOpenAI(api_key=settings.openai_api_key)

# ---------------------------------------------------------------------------
# Calibration prompt — this is the most important part of the scorer
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert at identifying Brazilian-owned or Brazilian-themed businesses in the United States, specifically in the Boston metro area.

You will receive batches of place records. For each, output a score 0-100 representing the likelihood the place is Brazilian-owned or Brazilian-themed.

═══════════════════════════════════════════════════════════
SCORING CALIBRATION  (anchor these firmly — be conservative)
═══════════════════════════════════════════════════════════

90–100 · UNMISTAKABLY BRAZILIAN
  The name explicitly and unambiguously identifies the place as Brazilian
  using Portuguese words or Brazilian cultural references with no other
  reasonable interpretation.
  ✓ "Churrascaria Palace", "Padaria Brasileira", "Casa do Brasil",
    "Café Brasileiro", "Mercado Brasileiro", "Restaurante Mineiro",
    "Sabor do Brasil", "Cantinho Brasileiro", "Lanchonete Brasileira"
  ✓ Name contains: Churrascaria, Padaria, Lanchonete, Mercado Brasileiro,
    Brasileiro/a, Do Brasil, Mineiro/a, Carioca, Paulista (in food context)

75–89 · HIGH CONFIDENCE — strong indicators
  Strong Brazilian cues but slightly less explicit than above.
  ✓ Name contains specific Brazilian food terms: Picanha, Churrasco, Feijoada,
    Coxinha, Brigadeiro, Pão de Queijo, Espetinho, Tapioca (in food context)
  ✓ Appeared in 3+ highly-specific Brazilian search queries
    (coxinha, brigadeiro, pão de queijo, mercado brasileiro, padaria brasileira)
  ✓ Portuguese name that strongly implies Brazilian context
    ("Sabor de Minas", "Cantinho do Brasil", "Gosto Brasileiro")

50–74 · MODERATE — possible but uncertain
  Some Brazilian indicators but could plausibly be non-Brazilian.
  ✓ Generic Portuguese words in name that could apply to multiple cultures
    (Portuguese from Portugal also uses Portuguese — not always Brazilian)
  ✓ English name + appeared in several specific Brazilian food searches
  ✓ Types suggest food business + hit_count ≥ 3 from Brazilian queries
  ✗ Do NOT score 50+ based solely on appearing in broad queries

20–49 · WEAK INDICATORS — unlikely to be Brazilian
  Little concrete evidence of Brazilian connection.
  ✓ "Tropical", "Rio", "Copa", "Verde" type names — could be Brazilian or generic
  ✓ Appeared only in broad queries ("Brazilian restaurant Boston")
    with no name-level confirmation
  ✓ Brazilian wax salons: "Brazilian" refers to the wax technique, not ownership;
    score these 10-30 unless other strong signals exist
  ✓ Açaí-only matches: many açaí sellers have no Brazilian connection beyond
    the ingredient; score 15-35 unless name/other signals confirm

1–19 · PROBABLY NOT BRAZILIAN
  Minimal or incidental connection to Brazil.
  ✓ National/international chains that appeared because they serve açaí
    (Smoothie King, Jamba Juice, etc.)
  ✓ Generic American businesses that matched due to broad query terms
  ✓ Beauty salons where "Brazilian" only modifies a service name
    ("Brazilian Wax by [Name]", "Brazilian Blowout Salon")
  ✓ Businesses with no Portuguese words, no food connection,
    and only appeared in non-specific queries

0 · NOT BRAZILIAN
  Known non-Brazilian chains or clearly accidental search match.

═══════════════════════════════════════════════════════════
EVIDENCE INTERPRETATION — how to use the input fields
═══════════════════════════════════════════════════════════

query_sources — THE MOST IMPORTANT FIELD
  These are the exact search queries that returned this place. Use them as evidence.

  STRONG evidence queries (appearing here is a major positive signal):
    churrascaria, picanha, feijoada, coxinha, brigadeiro, pão de queijo,
    pastelaria, padaria brasileira, lanchonete brasileira, mercado brasileiro,
    acai (only if name also has Brazilian cues), Brazilian owned, comida brasileira

  MODERATE evidence queries:
    Brazilian restaurant, Brazilian bakery, Brazilian market, Brazilian café,
    Brazilian grocery, Brazilian owned business + neighborhood name

  WEAK evidence queries (do NOT over-weight these):
    Brazilian wax, acai bowl, acai (generic), Brazilian salon, Brazilian beauty

hit_count — how many DIFFERENT queries returned this place
  More queries = stronger signal, but only if those queries were specific.
  A hit_count of 8 from weak queries is less valuable than hit_count of 3
  from strong queries. Read query_sources carefully.

types — place categories from Google
  restaurant, bakery, grocery_store, meal_takeaway, cafe → supports Brazilian
  beauty_salon, hair_care, spa → requires other strong signals to score high

display_name — the business name
  This is your strongest single signal. Portuguese food/culture words in the
  name should dominate the score. Generic English names require strong
  query_sources evidence to reach 50+.

═══════════════════════════════════════════════════════════
CRITICAL RULES
═══════════════════════════════════════════════════════════
- Score CONSERVATIVELY. A borderline case gets 35, not 55.
- When in doubt, score LOWER. False positives pollute the map.
- "Brazilian wax" alone → score 15 or below.
- National chains (Whole Foods, Starbucks, etc.) → score 0.
- Açaí smoothie chains (not clearly Brazilian) → score 10-25.
- Never let a high hit_count alone push a score above 40 without
  name-level or strong query evidence.

Return ONLY a valid JSON array in the same order as the input.
No explanation, no markdown, no code block. Just the array:
[{"place_id": "...", "score": <integer 0-100>, "reason": "<one concise sentence explaining the score>"}]"""


# ---------------------------------------------------------------------------
# Core scoring logic
# ---------------------------------------------------------------------------

def _format_for_scoring(c: dict) -> dict:
    """Trim a candidate to only the fields useful for scoring."""
    return {
        "place_id": c["place_id"],
        "name": c.get("display_name") or "(unknown)",
        "address": c.get("formatted_address") or "",
        "primary_type": c.get("primary_type") or "",
        "types": c.get("types") or [],
        "hit_count": c.get("hit_count", 1),
        "query_sources": (c.get("query_sources") or [])[:20],  # cap to keep tokens reasonable
    }


async def _score_batch(candidates: list[dict]) -> list[dict]:
    """
    Send one batch to OpenAI and return scored results.
    Returns list of {place_id, score, reason}.
    Raises on API error — caller handles retries/skipping.
    """
    formatted = [_format_for_scoring(c) for c in candidates]

    user_msg = (
        f"Score these {len(formatted)} places for Brazilian likelihood.\n\n"
        f"PLACES:\n{json.dumps(formatted, indent=2)}"
    )

    resp = await openai_client.chat.completions.create(
        model=settings.scoring_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.1,   # low = consistent calibration across batches
        max_tokens=1200,
    )

    raw = resp.choices[0].message.content.strip()

    # Extract JSON array even if model wraps it in markdown
    start = raw.find("[")
    end = raw.rfind("]") + 1
    if start < 0 or end <= start:
        raise ValueError(f"No JSON array in response: {raw[:300]}")

    results = json.loads(raw[start:end])

    # Validate and clamp scores
    out = []
    for item in results:
        if not isinstance(item, dict) or "place_id" not in item:
            continue
        score = max(0, min(100, int(item.get("score", 0))))
        out.append({
            "place_id": item["place_id"],
            "score": score,
            "reason": str(item.get("reason", ""))[:300],
        })
    return out


# ---------------------------------------------------------------------------
# Runner — iterates all unscored enriched candidates
# ---------------------------------------------------------------------------

async def run_scoring(progress_callback=None):
    """
    Score all enriched-but-unscored candidates in batches.
    progress_callback(done, total, batch_results) is called after each batch.
    """
    candidates = await storage.get_unscored_candidates()
    total = len(candidates)
    done = 0

    for i in range(0, total, BATCH_SIZE):
        batch = candidates[i: i + BATCH_SIZE]
        batch_results = []

        try:
            batch_results = await _score_batch(batch)

            # Build a lookup so we can match results back to place_ids
            result_map = {r["place_id"]: r for r in batch_results}

            for c in batch:
                result = result_map.get(c["place_id"])
                if result:
                    await storage.set_score(
                        c["place_id"],
                        score=result["score"],
                        reason=result["reason"],
                    )

        except Exception as e:
            # On any error: skip this batch, continue with the rest
            # Unscored candidates can be re-run on the next scoring pass
            batch_results = [{"error": str(e)}]

        done += len(batch)
        if progress_callback:
            await progress_callback(done, total, batch_results)

        # Gentle pause between batches to stay within rate limits
        await asyncio.sleep(0.3)
