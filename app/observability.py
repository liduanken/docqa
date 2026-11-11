"""Metrics, structured logging and tracing.

Metric names and labels are contracts: the PrometheusRule alerts depend on them.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram

from app.settings import Settings

# --- Metrics -------------------------------------------------------------------------------
HTTP_REQUESTS = Counter("docqa_http_requests_total", "HTTP requests", ["method", "route", "code"])
HTTP_LATENCY = Histogram(
    "docqa_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
# outcome: success, timeout, rate_limited, unavailable, error, circuit_open, overloaded
LLM_REQUESTS = Counter("docqa_llm_requests_total", "LLM calls by outcome", ["provider", "model", "outcome"])
LLM_LATENCY = Histogram(
    "docqa_llm_request_duration_seconds",
    "LLM call latency per attempt",
    ["provider", "model"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)
LLM_TOKENS = Counter("docqa_llm_tokens_total", "LLM tokens", ["provider", "model", "type"])
LLM_COST = Counter("docqa_llm_cost_usd_total", "Estimated LLM spend in USD", ["provider", "model"])
LLM_INFLIGHT = Gauge("docqa_llm_inflight_requests", "LLM calls in flight", ["provider"])
LLM_CIRCUIT_OPEN = Gauge("docqa_llm_circuit_open", "1 if the LLM circuit breaker is open", ["provider"])
ANSWERS = Counter("docqa_answers_total", "Answers by whether the document could answer", ["answerable"])
DOCUMENTS_INGESTED = Counter("docqa_documents_ingested_total", "Documents stored")
DOCUMENT_SIZE = Histogram(
    "docqa_document_size_bytes", "Ingested document size", buckets=(1e3, 5e3, 1e4, 5e4, 1e5, 5e5, 1e6)
)

# --- Logging -------------------------------------------------------------------------------
_REDACTED_KEYS = frozenset(
    {"text", "document", "question", "answer", "prompt", "content", "api_key", "authorization", "redis_url"}
)


def _add_trace_ids(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    ctx = trace.get_current_span().get_span_context()
    if ctx.is_valid:
        event_dict["trace_id"] = format(ctx.trace_id, "032x")
        event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Safety net: document content and secrets never reach the logs, even by mistake."""
    for key in _REDACTED_KEYS & event_dict.keys():
        event_dict[key] = "[REDACTED]"
    return event_dict


def configure_logging(settings: Settings) -> None:
    level = logging.getLevelName(settings.log_level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_trace_ids,
            _redact,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            level if isinstance(level, int) else logging.INFO
        ),
        logger_factory=structlog.PrintLoggerFactory(sys.stdout),
        cache_logger_on_first_use=False,  # tests reconfigure the logger
    )
    logging.getLogger("uvicorn.access").disabled = True  # our middleware logs requests instead


log = structlog.get_logger("docqa")

# --- Tracing -------------------------------------------------------------------------------
_tracing_configured = False


def configure_tracing(settings: Settings) -> None:
    """Idempotent. Always installs a provider so trace IDs exist for log correlation."""
    global _tracing_configured
    if _tracing_configured:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": settings.service_name}))
    if settings.otel_exporter_otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        endpoint = settings.otel_exporter_otlp_endpoint.rstrip("/") + "/v1/traces"
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    # Deliberately no console exporter: it would print prompt content to stdout.
    trace.set_tracer_provider(provider)
    _tracing_configured = True


tracer = trace.get_tracer("docqa")
