"""
Seed query bank for Brazilian business discovery in Boston metro.

Organized into families for systematic coverage.
All queries are combined with neighborhood suffixes for geographic spread.
"""

# ── Core food/business terms ────────────────────────────────────────────────
FOOD_TERMS = [
    "Brazilian restaurant",
    "churrascaria",
    "Brazilian steakhouse",
    "Brazilian bakery",
    "padaria brasileira",
    "lanchonete brasileira",
    "Brazilian cafe",
    "cafezinho",
    "Brazilian market",
    "mercado brasileiro",
    "Brazilian grocery",
    "Brazilian food",
    "feijoada",
    "picanha",
    "coxinha",
    "brigadeiro",
    "pão de queijo",
    "pao de queijo",
    "açaí",
    "acai",
    "acai bowl Brazilian",
    "pastelaria",
    "Brazilian pastry",
    "espetinho",
    "churrasco",
    "Brazilian buffet",
    "comida brasileira",
    "sabor brasileiro",
    "Brazilian pizza",
    "Brazilian juice bar",
    "tapioca brasileira",
]

# ── Service/non-food businesses ─────────────────────────────────────────────
SERVICE_TERMS = [
    "Brazilian salon",
    "Brazilian beauty salon",
    "Brazilian hair salon",
    "Brazilian barbershop",
    "Brazilian nail salon",
    "Brazilian wax salon",
    "Brazilian owned business",
    "Brazilian owned store",
    "Brazilian travel agency",
    "Brazilian immigration",
    "Brazilian church",
    "Igreja brasileira",
    "Brazilian community center",
    "Brazilian clothing store",
    "Brazilian boutique",
    "Brazilian meat market",
    "Brazilian butcher",
    "Brazilian imports",
    "produtos brasileiros",
]

# ── Portuguese-language queries (catches non-English-optimized listings) ────
PORTUGUESE_TERMS = [
    "restaurante brasileiro Boston",
    "mercado brasileiro Boston",
    "padaria brasileira Boston",
    "lanchonete brasileira Boston",
    "comida brasileira Boston",
    "churrascaria Boston",
    "salão brasileiro Boston",
    "produtos brasileiros Boston",
    "sabor do Brasil Boston",
    "casa do Brasil Boston",
    "cantinho brasileiro Boston",
    "sabor mineiro Boston",
    "comida mineira Boston",
    "nordestino Boston",
    "culinária brasileira Boston",
]

# ── Brand pattern fragments (catches named businesses) ──────────────────────
BRAND_PATTERNS = [
    "Casa do Brasil",
    "Sabor do Brasil",
    "Cantinho Brasileiro",
    "Mineiro Boston",
    "Mineira Boston",
    "Brasil Grill",
    "Brazil Grill",
    "Tropical Boston",
    "Rio Boston",
    "Copa Boston",
    "Verde Amarelo",
    "Ipanema Boston",
    "Copacabana Boston",
    "Saudades Boston",
    "Saudade Boston",
]

# ── Boston metro neighborhoods for geographic coverage ──────────────────────
NEIGHBORHOODS = [
    "Allston",
    "Brighton",
    "East Boston",
    "Everett",
    "Chelsea",
    "Somerville",
    "Cambridge",
    "Medford",
    "Malden",
    "Revere",
    "Framingham",     # large Brazilian population
    "Marlborough",    # large Brazilian population
    "Dorchester",
    "Roxbury",        # some Brazilian presence
    "Jamaica Plain",
    "South End",
    "Brookline",
    "Newton",
    "Waltham",
    "Watertown",
    "Quincy",
    "Brockton",       # known Brazilian community
    "Lowell",
]

# ── High-priority terms that get neighborhood variants ──────────────────────
PRIORITY_TERMS = [
    "Brazilian restaurant",
    "churrascaria",
    "Brazilian bakery",
    "mercado brasileiro",
    "padaria brasileira",
    "Brazilian market",
    "feijoada",
    "picanha",
    "acai",
    "Brazilian owned",
]


def build_seed_queries() -> list[str]:
    """
    Build the full list of seed queries.

    Strategy:
    1. All food terms with "Boston" appended
    2. All service terms with "Boston" appended
    3. Portuguese-language terms (already have Boston)
    4. Brand patterns with "Boston"
    5. High-priority terms × all neighborhoods (for geographic spread)
    6. Plain "Brazilian" + neighborhood (catch-all)
    """
    queries: list[str] = []
    seen: set[str] = set()

    def add(q: str):
        normalized = q.strip().lower()
        if normalized not in seen:
            seen.add(normalized)
            queries.append(q.strip())

    # Family A: food terms → Boston
    for term in FOOD_TERMS:
        add(f"{term} Boston")

    # Family B: service terms → Boston
    for term in SERVICE_TERMS:
        add(f"{term} Boston")

    # Family C: Portuguese terms (already include Boston)
    for term in PORTUGUESE_TERMS:
        add(term)

    # Family D: brand patterns → Boston
    for pattern in BRAND_PATTERNS:
        add(f"{pattern} Boston")

    # Family E: priority terms × neighborhoods (geographic spread)
    for term in PRIORITY_TERMS:
        for hood in NEIGHBORHOODS:
            add(f"{term} {hood}")

    # Family F: catch-all "Brazilian" per neighborhood
    for hood in NEIGHBORHOODS:
        add(f"Brazilian {hood}")
        add(f"brasileiro {hood}")

    return queries


# Singleton for use throughout the app
SEED_QUERIES: list[str] = build_seed_queries()


def get_query_count() -> int:
    return len(SEED_QUERIES)
