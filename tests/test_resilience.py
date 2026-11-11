import asyncio
import time

import httpx
import pytest

from app.llm import (
    LLMError,
    LLMRateLimitedError,
    LLMResult,
    LLMTimeoutError,
    LLMUnavailableError,
    OpenAICompatibleLLM,
)
from app.resilience import CircuitBreaker
from tests.conftest import MockLLM, make_settings, upload

QUESTION = {"question": "Where is the headquarters?"}


class SlowLLM(MockLLM):
    async def generate(self, system: str, user: str) -> LLMResult:
        self.calls.append((system, user))
        await asyncio.sleep(5)
        return LLMResult("too late", 1, 1)


def _ask(client):  # type: ignore[no-untyped-def]
    return client.post(f"/v1/documents/{upload(client)}/questions", json=QUESTION)


def test_slow_llm_times_out(make_client, no_backoff):
    resp = _ask(make_client(SlowLLM(), llm_timeout_seconds=0.05, llm_max_retries=0))
    assert resp.status_code == 504
    assert resp.json()["error"]["code"] == "llm_timeout"


def test_rate_limit_is_retried(make_client, no_backoff):
    llm = MockLLM(LLMRateLimitedError("slow down"), "ok")
    resp = _ask(make_client(llm))
    assert resp.status_code == 200
    assert len(llm.calls) == 2


def test_unavailable_exhausts_retries(make_client, no_backoff):
    llm = MockLLM(LLMUnavailableError("down"))
    resp = _ask(make_client(llm, llm_max_retries=2))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "llm_unavailable"
    assert "Retry-After" in resp.headers
    assert len(llm.calls) == 3


def test_non_retryable_error_fails_fast(make_client, no_backoff):
    llm = MockLLM(LLMError("bad request"))
    resp = _ask(make_client(llm))
    assert resp.status_code == 502
    assert len(llm.calls) == 1


def test_circuit_opens_and_sheds_load(make_client, no_backoff):
    llm = MockLLM(LLMUnavailableError("down"))
    client = make_client(llm, llm_max_retries=0, circuit_failure_threshold=2)
    doc_id = upload(client)
    url = f"/v1/documents/{doc_id}/questions"
    assert client.post(url, json=QUESTION).status_code == 503
    assert client.post(url, json=QUESTION).status_code == 503
    third = client.post(url, json=QUESTION)
    assert third.json()["error"]["code"] == "llm_circuit_open"
    assert int(third.headers["Retry-After"]) >= 1
    assert len(llm.calls) == 2  # the open circuit made no provider call
    assert 'docqa_llm_circuit_open{provider="mock"} 1.0' in client.get("/metrics").text
    assert client.get("/readyz").status_code == 200  # an LLM outage keeps pods Ready


def test_circuit_gauge_clears_without_traffic(make_client, no_backoff):
    client = make_client(
        MockLLM(LLMUnavailableError("down")), llm_max_retries=0, circuit_failure_threshold=1,
        circuit_reset_seconds=0.05,
    )  # fmt: skip
    client.post(f"/v1/documents/{upload(client)}/questions", json=QUESTION)
    assert 'docqa_llm_circuit_open{provider="mock"} 1.0' in client.get("/metrics").text
    time.sleep(0.1)  # breaker turns half-open with no request passing through
    assert 'docqa_llm_circuit_open{provider="mock"} 0.0' in client.get("/metrics").text


def test_circuit_half_open_recovery():
    now = [0.0]
    breaker = CircuitBreaker(failure_threshold=2, reset_seconds=30, clock=lambda: now[0])
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open" and not breaker.allow()
    now[0] = 31
    assert breaker.state == "half_open" and breaker.allow()
    breaker.record_failure()  # a failed probe reopens immediately
    assert breaker.state == "open"
    now[0] = 62
    breaker.record_success()
    assert breaker.state == "closed"


def _openai(handler) -> OpenAICompatibleLLM:  # type: ignore[no-untyped-def]
    settings = make_settings(llm_provider="openai", llm_model="gpt-test")
    client = httpx.AsyncClient(base_url="https://llm.test/v1", transport=httpx.MockTransport(handler))
    return OpenAICompatibleLLM(settings, client=client)


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, LLMRateLimitedError), (500, LLMUnavailableError), (503, LLMUnavailableError), (400, LLMError)],
)
def test_openai_status_mapping(status, error):
    llm = _openai(lambda _req: httpx.Response(status, json={}))
    with pytest.raises(error) as exc_info:
        asyncio.run(llm.generate("sys", "user"))
    assert type(exc_info.value) is error


def test_openai_read_timeout_maps_to_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMTimeoutError):
        asyncio.run(_openai(handler).generate("sys", "user"))


def test_openai_parses_answer_and_usage():
    payload = {
        "choices": [{"message": {"content": " Canary Wharf. "}}],
        "usage": {"prompt_tokens": 42, "completion_tokens": 3},
    }
    result = asyncio.run(_openai(lambda _req: httpx.Response(200, json=payload)).generate("s", "u"))
    assert result == LLMResult("Canary Wharf.", 42, 3)
