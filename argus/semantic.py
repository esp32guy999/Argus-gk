"""Semantic tool selection — the v2 implementation behind the registry's select() seam.

Ranks tools by embedding-similarity to the user's request so the model only sees the
relevant handful instead of every tool (keeps weak local models sane as lanes grow).
Uses the local nomic-embed-text server on anvil (CPU, free, zero VRAM). Degrades
gracefully: any embedding failure raises, and Registry.select() falls back to all.
"""
from __future__ import annotations

import math

import httpx

DEFAULT_EMBED_URL = "http://anvil:8091/v1/embeddings"
DEFAULT_EMBED_MODEL = "nomic-embed-text"


def embed(texts: list[str], *, url: str = DEFAULT_EMBED_URL,
          model: str = DEFAULT_EMBED_MODEL, timeout: float = 30) -> list[list[float]]:
    """Embed a batch of strings via the OpenAI-compatible embeddings API."""
    resp = httpx.post(url, json={"model": model, "input": texts}, timeout=timeout)
    resp.raise_for_status()
    return [row["embedding"] for row in resp.json()["data"]]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class SemanticSelector:
    """Embeds tool docs once (lazily), then ranks them against each query.

    embed_fn is injectable for testing; defaults to the anvil embeddings client.
    `always` names tools included in every selection regardless of rank — the
    system prompt references lookup_memory and start_background_task by name,
    so they must never be cut by top-k (eval finding, 2026-07-05).
    """

    DEFAULT_ALWAYS = ("lookup_memory", "start_background_task")

    def __init__(self, tools, *, embed_fn=None, top_k: int = 8,
                 always: tuple[str, ...] | None = DEFAULT_ALWAYS):
        self.tools = list(tools)
        self.embed_fn = embed_fn or embed
        self.top_k = top_k
        self.always = tuple(always or ())
        self._vecs: list[list[float]] | None = None

    def _doc(self, t) -> str:
        return f"{t.name}: {t.description} (tags: {', '.join(t.tags)})"

    def _ensure_vectors(self) -> None:
        if self._vecs is None:
            self._vecs = self.embed_fn([self._doc(t) for t in self.tools])

    def rank(self, context: str) -> list:
        """All tools ordered by similarity to context (most similar first).
        Raises on embedding failure so Registry.select() can fall back."""
        if not self.tools:
            return []
        self._ensure_vectors()
        qv = self.embed_fn([context])[0]
        scored = sorted(zip(self.tools, self._vecs),
                        key=lambda tv: _cosine(qv, tv[1]), reverse=True)
        return [t for t, _ in scored]

    def select(self, context: str) -> list:
        """always-include tools + the top_k most similar others (deduped,
        rank order preserved)."""
        ranked = self.rank(context)
        pinned = [t for t in ranked if t.name in self.always]
        rest = [t for t in ranked if t.name not in self.always]
        return pinned + rest[:self.top_k]
