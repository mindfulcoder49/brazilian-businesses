from pydantic_settings import BaseSettings
from pydantic import Field
import os


class Settings(BaseSettings):
    google_places_api_key: str
    openai_api_key: str

    database_path: str = "data/candidates.db"
    log_path: str = "data/logs"

    # Agent loop controls
    max_queries_per_run: int = 300
    max_candidates: int = 3000
    novelty_window_size: int = 10     # rolling window for novelty rate
    novelty_floor: float = 0.05       # stop if new-candidate rate drops below 5%

    # Places API controls
    places_page_size: int = 20
    max_pages_per_query: int = 3      # up to 60 results per query
    requests_per_second: float = 2.0  # conservative rate limit

    # OpenAI model for query expansion (agent loop)
    openai_model: str = "gpt-4o-mini"

    # OpenAI model for Brazilian likelihood scoring
    # Set SCORING_MODEL=gpt-4o-mini in .env if gpt-4o-mini is unavailable
    scoring_model: str = "gpt-4o-mini"

    # Scoring batch size (places per OpenAI request)
    scoring_batch_size: int = 10

    model_config = {"env_file": ".env"}


settings = Settings()

# Ensure data directories exist
os.makedirs(os.path.dirname(settings.database_path), exist_ok=True)
os.makedirs(settings.log_path, exist_ok=True)
