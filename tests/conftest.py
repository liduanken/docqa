from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.resilience
from app.llm import LLMResult
from app.main import create_app
from app.settings import Settings

DOC = (
    "Barclays was founded in 1690 in London. The company headquarters is located in Canary Wharf. "
    "It employs people in many countries.\n\n"
    "Kubernetes clusters are upgraded every quarter. Upgrades are rolled out to dev before prod, "
    "and every upgrade needs a rollback plan."
)


class MockLLM:
    """Scripted provider: each call pops the next item (the last one repeats).

    Exceptions are raised; strings are returned as an LLMResult.
    """

    name = "mock"
    model = "mock-1"

    def __init__(self, *script: str | Exception) -> None:
        self.script = list(script) or ["ok"]
        self.calls: list[tuple[str, str]] = []

    async def generate(self, system: str, user: str) -> LLMResult:
        self.calls.append((system, user))
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return LLMResult(item, 100, 10)


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "max_document_bytes": 2000,
        "llm_max_retries": 1,
        "llm_timeout_seconds": 0.2,
        "circuit_failure_threshold": 2,
        "circuit_reset_seconds": 60,
        "chunk_size_words": 20,
        "chunk_overlap_words": 5,
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def make_client() -> Iterator[Callable[..., TestClient]]:
    clients: list[TestClient] = []

    def factory(provider: Any = None, **overrides: Any) -> TestClient:
        client = TestClient(create_app(make_settings(**overrides), provider))
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(app.resilience, "_backoff_sleep", _no_sleep)


def upload(client: TestClient, text: str = DOC) -> str:
    resp = client.post("/v1/documents", json={"text": text})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["document_id"])
