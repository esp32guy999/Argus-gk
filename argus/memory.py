"""Curated-doc memory — Phase 1A of the memory system (retrieval-first, no schema).

Embeds the homelab's hand-maintained docs (tools.md, the memory dir, ...) into an
IN-MEMORY index at boot and ranks chunks against a query. Zero persistent state:
the docs stay the source of truth, this index is purely derived and rebuilt on
startup. Brute-force cosine is instant at homelab scale (a few hundred chunks).

This is deliberately minimal. A `facts` table, learned facts, supersession, trust
tiers etc. (Phase 1B+) are added ONLY if retrieval over the docs proves useful but
insufficient. See docs/memory-design.md.
"""
from __future__ import annotations

import os
import pathlib

from .semantic import embed as _default_embed, _cosine

# Homelab curated docs = the source of truth for hosts/IPs/ports/quirks. Override
# with ARGUS_MEMORY_SOURCES (colon-separated files/dirs; dirs expand to *.md).
_DEFAULT_SOURCES = [
    "/home/shane/tools.md",
    "/home/shane/.claude/projects/-home-shane/memory",
    "/home/shane/argus/notes",          # agent-saved notes (created by save_note)
]


def _resolve_sources(sources: list[str] | None) -> list[pathlib.Path]:
    raw = sources or (os.environ.get("ARGUS_MEMORY_SOURCES", "").split(":")
                      if os.environ.get("ARGUS_MEMORY_SOURCES") else _DEFAULT_SOURCES)
    files: list[pathlib.Path] = []
    for s in raw:
        p = pathlib.Path(s)
        if p.is_dir():
            files.extend(sorted(p.glob("*.md")))
        elif p.is_file():
            files.append(p)
    return files


def _chunk(text: str, source: str, max_chars: int = 1200) -> list[dict]:
    """Heading-scoped chunks. Each chunk carries its source + nearest markdown
    heading as context so a retrieved snippet is self-describing."""
    chunks: list[dict] = []
    heading = ""
    buf: list[str] = []
    size = 0

    def flush():
        nonlocal buf, size
        body = "\n".join(buf).strip()
        if body:
            prefix = f"[{source}] {heading}".strip()
            chunks.append({"source": source, "heading": heading,
                           "text": f"{prefix}\n{body}"})
        buf, size = [], 0

    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            flush()
            heading = line.strip().lstrip("#").strip()
            continue
        if size + len(line) > max_chars and buf:
            flush()
        buf.append(line)
        size += len(line) + 1
    flush()
    return chunks


class DocMemory:
    """An in-memory, embedded index of curated doc chunks."""

    def __init__(self, chunks: list[dict], vectors: list[list[float]], embed_fn=None):
        self.chunks = chunks
        self.vectors = vectors
        # Remember the embedder used to build, so search always matches it.
        self.embed_fn = embed_fn or _default_embed

    @classmethod
    def build(cls, sources: list[str] | None = None, *, embed_fn=None,
              max_chars: int = 1200, batch: int = 16) -> "DocMemory":  # anvil:8091 500s past ~16/req
        embed_fn = embed_fn or _default_embed
        chunks: list[dict] = []
        for path in _resolve_sources(sources):
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            chunks.extend(_chunk(text, path.name, max_chars))
        # Embed in batches; if a batch fails (the embeddings server caps tokens/req),
        # fall back to per-item so one oversized chunk can't sink the whole index.
        kept: list[dict] = []
        vectors: list[list[float]] = []
        for i in range(0, len(chunks), batch):
            group = chunks[i:i + batch]
            try:
                vectors.extend(embed_fn([c["text"] for c in group]))
                kept.extend(group)
            except Exception:
                for c in group:
                    try:
                        vectors.append(embed_fn([c["text"]])[0])
                        kept.append(c)
                    except Exception:
                        pass  # skip an unembeddable chunk rather than fail the build
        return cls(kept, vectors, embed_fn)

    def search(self, query: str, k: int = 5, *, embed_fn=None) -> list[dict]:
        embed_fn = embed_fn or self.embed_fn
        if not self.chunks:
            return []
        qv = embed_fn([query])[0]
        scored = sorted(
            ((c, _cosine(qv, v)) for c, v in zip(self.chunks, self.vectors)),
            key=lambda cv: cv[1], reverse=True,
        )
        return [{"source": c["source"], "text": c["text"], "score": round(s, 3)}
                for c, s in scored[:k]]


# --- module-level singleton, built in build_registry, read by the tool ---------
_INDEX: DocMemory | None = None


def build_index(sources: list[str] | None = None, *, embed_fn=None) -> DocMemory | None:
    """Build (or rebuild) the index. Degrades gracefully: any failure (e.g. the
    embeddings server is down) leaves the index None and the tool reports it."""
    global _INDEX
    try:
        _INDEX = DocMemory.build(sources, embed_fn=embed_fn)
    except Exception:
        _INDEX = None
    return _INDEX


def get_index() -> DocMemory | None:
    return _INDEX


def add_note(source: str, text: str) -> bool:
    """Append a note's chunks to the LIVE index so it's searchable immediately,
    without a full rebuild. No-op (returns False) if the index isn't built."""
    idx = _INDEX
    if idx is None:
        return False
    chunks = _chunk(text, source)
    if not chunks:
        return False
    idx.vectors.extend(idx.embed_fn([c["text"] for c in chunks]))
    idx.chunks.extend(chunks)
    return True
