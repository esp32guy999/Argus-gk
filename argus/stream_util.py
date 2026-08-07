"""Stream text merge — cumulative vs delta vs re-delivery.

Claude Code / Grok / some OpenAI-compat streams re-send the *full* assistant
text (or a cumulative snapshot) as a new event. Naively doing ``acc += piece``
turns that into an exact echo of the whole answer. This helper is the single
merge rule for agent streams that must yield cumulative text-so-far.
"""
from __future__ import annotations


def merge_stream_text(acc: str, piece: str) -> str:
    """Merge ``piece`` into cumulative ``acc`` without doubling re-sends.

    Cases:
      - empty piece → acc unchanged
      - empty acc → piece
      - piece == acc → unchanged (exact re-delivery)
      - piece.startswith(acc) → piece (cumulative snapshot)
      - acc.startswith(piece) → acc (stale shorter snapshot)
      - piece already contained in acc → acc
      - acc already contained in piece → piece (full rewrite supersedes)
      - else → append as a new paragraph (true multi-block answer)
    """
    if not piece:
        return acc or ""
    if not acc:
        return piece
    if piece == acc:
        return acc
    if piece.startswith(acc):
        return piece
    if acc.startswith(piece):
        return acc
    if piece in acc:
        return acc
    if acc in piece:
        return piece
    return acc + "\n\n" + piece
