"""HTTP API. ``create_app()`` is a factory used by tests and by ``uvicorn --factory``."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.llm import NOT_FOUND, SYSTEM_PROMPT, LLMError, LLMProvider, build_provider, build_user_prompt
from app.observability import (
    ANSWERS,
    DOCUMENT_SIZE,
    DOCUMENTS_INGESTED,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    configure_logging,
    configure_tracing,
    log,
    tracer,
)
from app.rag import DocumentStore, InMemoryDocumentStore, RedisDocumentStore, chunk_text, retrieve
from app.resilience import ResilientLLM
from app.settings import Settings

CANNOT_ANSWER = "I can't answer that from the provided document."

# Upstream 429 becomes 503: the client didn't cause it, our provider quota did.
OUTCOME_STATUS = {
    "timeout": 504,
    "rate_limited": 503,
    "unavailable": 503,
    "circuit_open": 503,
    "overloaded": 503,
    "error": 502,
}
_UNMETERED_PATHS = frozenset({"/metrics", "/healthz", "/readyz"})


def max_wire_bytes(max_document_bytes: int) -> int:
    """Largest request body that could still hold a valid document: worst-case JSON escaping plus slack."""
    return 6 * max_document_bytes + 4096


class DocumentIn(BaseModel):
    text: str = Field(min_length=1)


class DocumentOut(BaseModel):
    document_id: str
    chunks: int
    size_bytes: int


class QuestionIn(BaseModel):
    question: str = Field(min_length=1)


class Source(BaseModel):
    chunk_id: int
    text: str
    score: float


class AnswerOut(BaseModel):
    answer: str
    answerable: bool
    sources: list[Source]
    model: str


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    body = {"error": {"code": code, "message": message, "request_id": request_id}}
    return JSONResponse(body, status_code=status, headers=headers)


def _build_store(settings: Settings) -> DocumentStore:
    if settings.store_backend == "redis":
        if settings.redis_url is None:
            raise ValueError("DOCQA_REDIS_URL is required when DOCQA_STORE_BACKEND=redis")
        return RedisDocumentStore(settings.redis_url.get_secret_value(), settings.document_ttl_seconds)
    return InMemoryDocumentStore(settings.max_documents, settings.document_ttl_seconds)


def create_app(settings: Settings | None = None, provider: LLMProvider | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings)
    configure_tracing(settings)
    store = _build_store(settings)
    llm = ResilientLLM(provider or build_provider(settings), settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        log.info(
            "startup",
            version=__version__,
            provider=llm.name,
            model=llm.model,
            store_backend=settings.store_backend,
            max_document_bytes=settings.max_document_bytes,
        )
        yield
        log.info("shutdown")

    app = FastAPI(title="DocQA", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.llm = llm

    @app.middleware("http")
    async def observe(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()

        # Reject absurd bodies up front, without reading them into memory. This is only a coarse DoS cap:
        # JSON escaping can make the wire size up to 6x the UTF-8 size (\uXXXX per CJK char), so the exact
        # limit is enforced on the decoded text in the handler.
        content_length = request.headers.get("content-length", "")
        if content_length.isdigit() and int(content_length) > max_wire_bytes(settings.max_document_bytes):
            response: Response = _error(
                413, "payload_too_large", f"request body exceeds {settings.max_document_bytes} bytes"
            )
            route = "unmatched"
        else:
            with tracer.start_as_current_span("http.request") as span:
                try:
                    response = await call_next(request)
                except Exception:
                    log.exception("unhandled_error")
                    response = _error(500, "internal_error", "internal server error")
                # Route template, never the raw path, keeps span names and metric labels bounded.
                route = getattr(request.scope.get("route"), "path", "unmatched")
                span.update_name(f"{request.method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.status_code", response.status_code)

        if request.url.path not in _UNMETERED_PATHS:
            duration = time.perf_counter() - start
            HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
            HTTP_LATENCY.labels(request.method, route).observe(duration)
            log.info(
                "request",
                method=request.method,
                route=route,
                status_code=response.status_code,
                duration_ms=round(duration * 1000, 1),
            )
        response.headers["x-request-id"] = request_id
        return response

    @app.exception_handler(LLMError)
    async def llm_error(_: Request, exc: LLMError) -> JSONResponse:
        headers = None
        if exc.outcome in {"rate_limited", "unavailable", "overloaded", "circuit_open"}:
            retry_after = llm.breaker.retry_after() if exc.outcome == "circuit_open" else 5
            headers = {"Retry-After": str(retry_after)}
        return _error(OUTCOME_STATUS.get(exc.outcome, 502), f"llm_{exc.outcome}", str(exc), headers)

    # Starlette's (not FastAPI's) HTTPException, so unknown-route 404s are covered too.
    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {404: "not_found", 413: "payload_too_large", 422: "invalid_request"}
        return _error(exc.status_code, codes.get(exc.status_code, "http_error"), str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Only location and message: never echo the submitted input back.
        message = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg')}" for err in exc.errors()
        )
        return _error(422, "invalid_request", message)

    @app.post("/v1/documents", status_code=201, response_model=DocumentOut)
    async def submit_document(body: DocumentIn) -> DocumentOut | JSONResponse:
        size = len(body.text.encode("utf-8"))
        if size > settings.max_document_bytes:
            return _error(
                413, "payload_too_large", f"document is {size} bytes; max is {settings.max_document_bytes}"
            )
        chunks = chunk_text(body.text, settings.chunk_size_words, settings.chunk_overlap_words)
        if not chunks:
            return _error(422, "invalid_request", "document contains no text")
        doc = await store.put(chunks, size)
        DOCUMENTS_INGESTED.inc()
        DOCUMENT_SIZE.observe(size)
        log.info("document_stored", document_id=doc.document_id, size_bytes=size, chunks=len(chunks))
        return DocumentOut(document_id=doc.document_id, chunks=len(chunks), size_bytes=size)

    @app.post("/v1/documents/{document_id}/questions", response_model=AnswerOut)
    async def ask_question(document_id: str, body: QuestionIn) -> AnswerOut | JSONResponse:
        if len(body.question) > settings.max_question_chars:
            return _error(
                422, "invalid_request", f"question exceeds {settings.max_question_chars} characters"
            )
        doc = await store.get(document_id)
        if doc is None:
            return _error(404, "not_found", "document not found or expired")
        structlog.contextvars.bind_contextvars(document_id=document_id)

        with tracer.start_as_current_span("rag.retrieve") as span:
            hits = [
                h
                for h in retrieve(doc.chunks, body.question, settings.top_k)
                if h.score >= settings.min_relevance
            ]
            span.set_attribute("rag.chunks_total", len(doc.chunks))
            span.set_attribute("rag.chunks_selected", len(hits))
            span.set_attribute("rag.top_score", hits[0].score if hits else 0.0)

        if not hits:
            # Nothing relevant retrieved: refuse without spending an LLM call.
            ANSWERS.labels(answerable="false").inc()
            log.info("question_unanswerable", reason="no_relevant_chunks")
            return AnswerOut(answer=CANNOT_ANSWER, answerable=False, sources=[], model=llm.model)

        result = await llm.generate(SYSTEM_PROMPT, build_user_prompt(body.question, [h.text for h in hits]))
        answerable = NOT_FOUND not in result.text
        ANSWERS.labels(answerable=str(answerable).lower()).inc()
        log.info("question_answered", answerable=answerable, sources=len(hits))
        return AnswerOut(
            answer=result.text if answerable else CANNOT_ANSWER,
            answerable=answerable,
            sources=[Source(chunk_id=h.chunk_id, text=h.text, score=h.score) for h in hits]
            if answerable
            else [],
            model=llm.model,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", response_model=None)
    async def readyz() -> dict[str, str] | JSONResponse:
        # Deliberately ignores LLM health: an LLM outage must not empty the Service of endpoints.
        if not await store.ping():
            return _error(503, "store_unavailable", "document store is not reachable")
        return {"status": "ready", "llm_circuit": llm.breaker.state}

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
