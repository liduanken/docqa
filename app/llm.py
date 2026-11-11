"""LLM providers: a deterministic fake for keyless runs, and any OpenAI-compatible API."""

from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.rag import tokenize
from app.settings import Settings

NOT_FOUND = "NOT_FOUND"

SYSTEM_PROMPT = (
    "You answer questions about a document. Answer strictly from the provided context. "
    f"If the context does not contain the answer, reply exactly {NOT_FOUND}. "
    "Do not use outside knowledge. Be concise."
)


def build_user_prompt(question: str, chunks: Sequence[str]) -> str:
    context = "\n\n".join(f"[{i}] {chunk}" for i, chunk in enumerate(chunks))
    return f"### Context\n{context}\n\n### Question\n{question}"


@dataclass(frozen=True)
class LLMResult:
    text: str
    prompt_tokens: int
    completion_tokens: int


class LLMError(Exception):
    outcome = "error"
    retryable = False


class LLMTimeoutError(LLMError):
    outcome = "timeout"
    retryable = True


class LLMRateLimitedError(LLMError):
    outcome = "rate_limited"
    retryable = True


class LLMUnavailableError(LLMError):
    outcome = "unavailable"
    retryable = True


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, system: str, user: str) -> LLMResult: ...


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_CHUNK_PREFIX_RE = re.compile(r"^\[\d+\]\s*")


class FakeLLM:
    """Extractive stand-in: returns the context sentence that best overlaps the question.

    Lets the whole service run with no API key, and supports latency/failure injection.
    """

    name = "fake"

    def __init__(self, model: str, latency: float = 0.0, failure_rate: float = 0.0) -> None:
        self.model = model
        self._latency = latency
        self._failure_rate = failure_rate

    async def generate(self, system: str, user: str) -> LLMResult:
        if self._latency:
            await asyncio.sleep(self._latency)
        if self._failure_rate and random.random() < self._failure_rate:  # noqa: S311
            raise LLMUnavailableError("injected failure")
        context, _, question = user.partition("\n\n### Question\n")
        q_tokens = set(tokenize(question))
        best, best_overlap = NOT_FOUND, 0
        for raw in _SENTENCE_RE.split(context.removeprefix("### Context\n")):
            sentence = _CHUNK_PREFIX_RE.sub("", raw.strip())
            overlap = len(q_tokens & set(tokenize(sentence)))
            if overlap > best_overlap:
                best, best_overlap = sentence, overlap
        return LLMResult(best, approx_tokens(system + user), approx_tokens(best))


class OpenAICompatibleLLM:
    name = "openai"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.model = settings.llm_model
        self._max_tokens = settings.llm_max_output_tokens
        headers = {}
        if settings.llm_api_key is not None:
            headers["Authorization"] = f"Bearer {settings.llm_api_key.get_secret_value()}"
        # `client` exists so tests can inject an httpx.MockTransport.
        self._client = client or httpx.AsyncClient(
            base_url=settings.llm_base_url, headers=headers, timeout=settings.llm_timeout_seconds
        )

    async def generate(self, system: str, user: str) -> LLMResult:
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        try:
            resp = await self._client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError("provider timed out") from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(f"transport error: {type(exc).__name__}") from exc
        if resp.status_code == 429:
            raise LLMRateLimitedError("provider rate limited the request")
        if resp.status_code >= 500:
            raise LLMUnavailableError(f"provider returned {resp.status_code}")
        if resp.status_code >= 400:
            raise LLMError(f"provider rejected the request ({resp.status_code})")
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        usage = data.get("usage") or {}
        return LLMResult(
            text,
            usage.get("prompt_tokens") or approx_tokens(system + user),
            usage.get("completion_tokens") or approx_tokens(text),
        )


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "openai":
        return OpenAICompatibleLLM(settings)
    return FakeLLM(settings.llm_model, settings.fake_llm_latency_seconds, settings.fake_llm_failure_rate)
