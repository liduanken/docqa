"""Controlled failure around the LLM: timeout, bounded retries, load shedding and a circuit breaker."""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from typing import Literal

from opentelemetry.trace import Span, Status, StatusCode

from app.llm import LLMError, LLMProvider, LLMResult, LLMTimeoutError
from app.observability import (
    LLM_CIRCUIT_OPEN,
    LLM_COST,
    LLM_INFLIGHT,
    LLM_LATENCY,
    LLM_REQUESTS,
    LLM_TOKENS,
    log,
    tracer,
)
from app.settings import Settings


class CircuitOpenError(LLMError):
    outcome = "circuit_open"
    retryable = False


class LLMOverloadedError(LLMError):
    outcome = "overloaded"
    retryable = False


# Tests monkeypatch this, never asyncio.sleep globally (which would also break the timeout test).
_backoff_sleep = asyncio.sleep

State = Literal["closed", "open", "half_open"]


class CircuitBreaker:
    def __init__(
        self, failure_threshold: int, reset_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> State:
        if self._opened_at is None:
            return "closed"
        return "half_open" if self._clock() - self._opened_at >= self._reset else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def retry_after(self) -> int:
        if self._opened_at is None:
            return 0
        return max(1, int(self._reset - (self._clock() - self._opened_at)))

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self.state == "half_open" or self._failures >= self._threshold:
            self._opened_at = self._clock()


class ResilientLLM:
    def __init__(self, provider: LLMProvider, settings: Settings) -> None:
        self.provider = provider
        self._settings = settings
        self._semaphore = asyncio.Semaphore(settings.llm_max_concurrency)
        self.breaker = CircuitBreaker(settings.circuit_failure_threshold, settings.circuit_reset_seconds)
        self._labels = {"provider": provider.name, "model": provider.model}
        # Evaluated at scrape time: an open breaker turns half-open without any request passing through.
        LLM_CIRCUIT_OPEN.labels(provider=provider.name).set_function(
            lambda: int(self.breaker.state == "open")
        )

    @property
    def name(self) -> str:
        return self.provider.name

    @property
    def model(self) -> str:
        return self.provider.model

    def _count(self, outcome: str) -> None:
        LLM_REQUESTS.labels(**self._labels, outcome=outcome).inc()

    async def generate(self, system: str, user: str) -> LLMResult:
        with tracer.start_as_current_span("llm.generate") as span:
            span.set_attribute("gen_ai.system", self.provider.name)
            span.set_attribute("gen_ai.request.model", self.provider.model)
            if self._settings.trace_content:
                span.set_attribute("gen_ai.prompt", user)
            try:
                result = await self._guarded(system, user, span)
            except LLMError as err:
                span.set_status(Status(StatusCode.ERROR, err.outcome))
                span.set_attribute("error.type", err.outcome)
                raise
            span.set_attribute("gen_ai.usage.input_tokens", result.prompt_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", result.completion_tokens)
            if self._settings.trace_content:
                span.set_attribute("gen_ai.completion", result.text)
            return result

    async def _guarded(self, system: str, user: str, span: Span) -> LLMResult:
        if not self.breaker.allow():
            self._count("circuit_open")
            raise CircuitOpenError("LLM circuit breaker is open")
        if self._semaphore.locked():
            self._count("overloaded")
            raise LLMOverloadedError("LLM concurrency limit reached")  # shed, don't queue
        inflight = LLM_INFLIGHT.labels(provider=self.provider.name)
        async with self._semaphore:
            inflight.inc()
            try:
                return await self._with_retries(system, user, span)
            finally:
                inflight.dec()

    async def _with_retries(self, system: str, user: str, span: Span) -> LLMResult:
        attempts = self._settings.llm_max_retries + 1
        for attempt in range(1, attempts + 1):
            start = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    self.provider.generate(system, user), timeout=self._settings.llm_timeout_seconds
                )
            except (TimeoutError, LLMError) as exc:
                err = exc if isinstance(exc, LLMError) else LLMTimeoutError("LLM call timed out")
                LLM_LATENCY.labels(**self._labels).observe(time.perf_counter() - start)
                self._count(err.outcome)
                log.warning("llm_call_failed", attempt=attempt, outcome=err.outcome, error=str(err))
                if not err.retryable or attempt == attempts:
                    if err.retryable:
                        self.breaker.record_failure()  # only provider-health failures trip the breaker
                    raise err from exc
                span.add_event("retry", {"attempt": attempt, "outcome": err.outcome})
                await _backoff_sleep(random.uniform(0, min(4.0, 0.25 * 2**attempt)))  # noqa: S311 full jitter
                continue
            latency = time.perf_counter() - start
            self.breaker.record_success()
            self._record_success(result, latency)
            span.set_attribute("llm.attempts", attempt)
            return result
        raise AssertionError("unreachable")

    def _record_success(self, result: LLMResult, latency: float) -> None:
        s = self._settings
        cost = (
            result.prompt_tokens * s.llm_price_prompt_per_1k
            + result.completion_tokens * s.llm_price_completion_per_1k
        ) / 1000
        self._count("success")
        LLM_LATENCY.labels(**self._labels).observe(latency)
        LLM_TOKENS.labels(**self._labels, type="prompt").inc(result.prompt_tokens)
        LLM_TOKENS.labels(**self._labels, type="completion").inc(result.completion_tokens)
        LLM_COST.labels(**self._labels).inc(cost)
        log.info(
            "llm_call_succeeded",
            latency_ms=round(latency * 1000, 1),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cost_usd=round(cost, 6),
        )
