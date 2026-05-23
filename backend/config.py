from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    openai_api_key: str = ""
    census_api_key: str = ""
    firecrawl_api_key: str = ""   # Cloud API key (fc-...) — not needed for local
    firecrawl_api_url: str = ""   # Self-hosted URL, e.g. http://localhost:3002
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/propintel"
    redis_url: str = "redis://localhost:6379"
    llm_model: str = "gpt-4o"
    log_level: str = "INFO"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
