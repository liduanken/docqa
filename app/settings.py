"""Runtime configuration. Every field maps to a ``DOCQA_<FIELD>`` environment variable."""

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCQA_", extra="ignore")

    # LLM provider. "openai" means any OpenAI-compatible chat-completions API.
    llm_provider: Literal["fake", "openai"] = "fake"
    llm_model: str = "fake-extractive-1"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None  # never logged
    llm_timeout_seconds: float = Field(default=15.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=5)
    llm_max_concurrency: int = Field(default=16, ge=1)
    llm_max_output_tokens: int = 400
    circuit_failure_threshold: int = 5
    circuit_reset_seconds: float = 30.0
    # USD per 1k tokens, used only for the cost metric.
    llm_price_prompt_per_1k: float = 0.0
    llm_price_completion_per_1k: float = 0.0

    # Fault injection for the fake LLM (demos and chaos testing).
    fake_llm_latency_seconds: float = 0.0
    fake_llm_failure_rate: float = Field(default=0.0, ge=0, le=1)

    # Input limits and document store.
    max_document_bytes: int = 100_000
    max_question_chars: int = 1_000
    max_documents: int = 1_000  # in-memory LRU cap
    document_ttl_seconds: int = Field(default=86_400, ge=60)
    chunk_size_words: int = 120
    chunk_overlap_words: int = 20
    top_k: int = Field(default=3, ge=1, le=10)
    min_relevance: float = Field(default=0.1, ge=0, le=1)  # below this, refuse instead of guessing
    store_backend: Literal["memory", "redis"] = "memory"
    redis_url: SecretStr | None = None

    # Observability.
    log_level: str = "INFO"
    trace_content: bool = True  # prompt and completion on spans
    otel_exporter_otlp_endpoint: str | None = None
    service_name: str = "docqa"
