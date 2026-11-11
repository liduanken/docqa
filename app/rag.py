"""Chunking, lexical retrieval and document storage.

Retrieval is TF-IDF cosine similarity: deterministic, dependency-free and easy to explain.
``retrieve()`` is the seam where embeddings and a vector store would plug in.
"""

from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Protocol

import redis.asyncio as aioredis

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have how in is it its of on or that the this to was were "
    "what when where which who why will with does do did can".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    step = max(size - overlap, 1)
    chunks: list[str] = []
    for start in range(0, len(words), step):
        chunks.append(" ".join(words[start : start + size]))
        if start + size >= len(words):
            break
    return chunks


@dataclass(frozen=True)
class ScoredChunk:
    chunk_id: int
    text: str
    score: float


def retrieve(chunks: list[str], question: str, k: int) -> list[ScoredChunk]:
    q_tokens = tokenize(question)
    if not q_tokens or not chunks:
        return []
    chunk_counts = [Counter(tokenize(c)) for c in chunks]
    n = len(chunks)
    df: Counter[str] = Counter()
    for counts in chunk_counts:
        df.update(counts.keys())

    def idf(term: str) -> float:
        return math.log((n + 1) / (df[term] + 1)) + 1

    def weighted(counts: Counter[str]) -> dict[str, float]:
        return {t: c * idf(t) for t, c in counts.items()}

    q_vec = weighted(Counter(q_tokens))
    q_norm = math.sqrt(sum(v * v for v in q_vec.values()))
    scored: list[ScoredChunk] = []
    for i, counts in enumerate(chunk_counts):
        c_vec = weighted(counts)
        c_norm = math.sqrt(sum(v * v for v in c_vec.values()))
        dot = sum(w * c_vec.get(t, 0.0) for t, w in q_vec.items())
        score = dot / (q_norm * c_norm) if q_norm and c_norm else 0.0
        scored.append(ScoredChunk(chunk_id=i, text=chunks[i], score=round(score, 4)))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:k]


@dataclass(frozen=True)
class StoredDocument:
    document_id: str
    chunks: list[str]
    size_bytes: int


class DocumentStore(Protocol):
    async def put(self, chunks: list[str], size_bytes: int) -> StoredDocument: ...
    async def get(self, document_id: str) -> StoredDocument | None: ...
    async def count(self) -> int: ...
    async def ping(self) -> bool: ...


class InMemoryDocumentStore:
    """Per-pod store with LRU eviction and TTL. Fine for one replica; use Redis to scale out."""

    def __init__(self, max_documents: int, ttl_seconds: int) -> None:
        self._max = max_documents
        self._ttl = ttl_seconds
        self._docs: OrderedDict[str, tuple[float, StoredDocument]] = OrderedDict()

    async def put(self, chunks: list[str], size_bytes: int) -> StoredDocument:
        doc = StoredDocument(uuid.uuid4().hex, chunks, size_bytes)
        self._docs[doc.document_id] = (time.monotonic(), doc)
        while len(self._docs) > self._max:
            self._docs.popitem(last=False)
        return doc

    async def get(self, document_id: str) -> StoredDocument | None:
        entry = self._docs.get(document_id)
        if entry is None:
            return None
        created, doc = entry
        if time.monotonic() - created > self._ttl:
            del self._docs[document_id]
            return None
        self._docs.move_to_end(document_id)
        return doc

    async def count(self) -> int:
        return len(self._docs)

    async def ping(self) -> bool:
        return True


class RedisDocumentStore:
    """Shared store so any replica can answer questions about any document."""

    def __init__(self, url: str, ttl_seconds: int) -> None:
        self._redis = aioredis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        self._ttl = ttl_seconds

    @staticmethod
    def _key(document_id: str) -> str:
        return f"docqa:doc:{document_id}"

    async def put(self, chunks: list[str], size_bytes: int) -> StoredDocument:
        doc = StoredDocument(uuid.uuid4().hex, chunks, size_bytes)
        payload = json.dumps({"chunks": chunks, "size_bytes": size_bytes})
        await self._redis.set(self._key(doc.document_id), payload, ex=self._ttl)
        return doc

    async def get(self, document_id: str) -> StoredDocument | None:
        raw = await self._redis.get(self._key(document_id))
        if raw is None:
            return None
        data = json.loads(raw)
        return StoredDocument(document_id, data["chunks"], data["size_bytes"])

    async def count(self) -> int:
        return -1  # unknown without a full key scan

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False
