"""Audiobook lane — search AudiobookBay (ABB) and download audiobooks.

ABB is scraped DIRECTLY from the Argus host (anvil), which has the VPN that can reach
audiobookbay.lu — the home ISP IP is blocked, so the old nyx audiobook-getter could
never reach ABB. The magnet is then added straight to qBittorrent on glassgarden
(qBt does the torrenting; it needs no ABB access). The nyx getter is OUT of the loop.

Written fresh for Argus (the approach is inspired by Shane's audiobook-getter, but
nothing is ported — and it's not Hermes). Uses lxml for parsing.

Tools:
- audiobook_search(query): ABB HTML search -> {title, url}.
- audiobook_get(query): search -> scrape the page's info-hash -> build magnet ->
  add to qBittorrent in one call. Asynchronous: it downloads then appears in the library.
"""
from __future__ import annotations

import os
import re
import time as _time
from urllib.parse import quote_plus

import httpx
import yaml
from lxml import html as _lxml
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
_HASH = re.compile(r"\b[a-fA-F0-9]{40}\b")
_CFG = "config/audiobook.yaml"


def has_config(path: str = _CFG) -> bool:
    return os.path.exists(path)


def tools(manifest_path: str = _CFG) -> list[Tool]:
    cfg = yaml.safe_load(open(manifest_path)) or {}
    abb = str(cfg["abb_base"]).rstrip("/")
    qbt = str(cfg["qbt_url"]).rstrip("/")
    quser, qpass = cfg.get("qbt_user", ""), cfg.get("qbt_pass", "")
    category = cfg.get("category", "audiobooks")
    trackers = cfg.get("trackers", [])

    def _abb_get(url: str) -> str:
        try:
            r = httpx.get(url, headers={"User-Agent": _UA}, timeout=25, follow_redirects=True)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ModelRetry(f"audiobook: could not reach AudiobookBay ({e}). "
                             "Is the VPN up on this host?")
        return r.text

    def _scrape_results(query: str) -> list[dict]:
        doc = _lxml.fromstring(_abb_get(f"{abb}/?s={quote_plus(query)}"))
        seen, out = set(), []
        # Results live in #content; each post's TITLE is the h2/h3 anchor (other
        # /abss/ anchors like "Audiobook Details" and the sidebar widget are excluded).
        for a in doc.xpath('//div[@id="content"]//h2/a[contains(@href,"/abss/")] | '
                           '//div[@id="content"]//h3/a[contains(@href,"/abss/")]'):
            href = a.get("href", "")
            if not href or "/abss/" not in href or href in seen:
                continue
            title = " ".join(a.text_content().split()).strip()
            if not title:
                continue
            seen.add(href)
            out.append({"title": title,
                        "url": href if href.startswith("http") else abb + href})
        return out

    def _search(query: str) -> list[dict]:
        # ABB intermittently serves its homepage (same #content layout) when rate-
        # limited, so keep only results that actually match the query terms, and retry
        # once if a fetch comes back with none (a fresh request usually returns results).
        terms = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3]

        def _match(item):
            t = item["title"].lower()
            hits = sum(1 for w in terms if w in t)
            return hits if (hits and (hits >= len(terms) - 1 or hits >= 2 or not terms)) else 0

        for attempt in range(3):
            results = _scrape_results(query)
            relevant = sorted(((_match(r), r) for r in results), key=lambda x: x[0], reverse=True)
            relevant = [r for score, r in relevant if score > 0]
            if relevant or not terms:
                return relevant or results
            if attempt < 2:
                _time.sleep(2.5)   # ABB served its homepage (rate-limit) — back off + retry
        return []

    def _scrape_hash(page_url: str) -> str | None:
        doc = _lxml.fromstring(_abb_get(page_url))
        # Prefer a hash right after an "Info Hash" label; else a lone 40-hex on the page.
        text = doc.text_content()
        m = re.search(r"info\s*hash[^0-9a-fA-F]{0,20}([a-fA-F0-9]{40})", text, re.I)
        if m:
            return m.group(1).lower()
        found = _HASH.findall(text)
        return found[0].lower() if len(set(h.lower() for h in found)) == 1 else (
            found[0].lower() if found else None)

    def _magnet(info_hash: str, title: str = "") -> str:
        mag = f"magnet:?xt=urn:btih:{info_hash}"
        if title:
            mag += f"&dn={quote_plus(title)}"
        for tr in trackers:
            mag += f"&tr={quote_plus(tr)}"
        return mag

    def _qbt_add(magnet: str) -> None:
        with httpx.Client(timeout=25, headers={"Referer": qbt}) as c:
            login = c.post(f"{qbt}/api/v2/auth/login",
                           data={"username": quser, "password": qpass})
            if login.status_code >= 300:   # this qBt returns 200/204 on success
                raise ModelRetry(f"audiobook: qBittorrent login failed (HTTP {login.status_code}).")
            add = c.post(f"{qbt}/api/v2/torrents/add",
                         data={"urls": magnet, "category": category})
            if add.status_code >= 300:
                raise ModelRetry(f"audiobook: qBittorrent rejected the add (HTTP {add.status_code}).")

    def audiobook_search(query: str) -> list:
        """Search AudiobookBay for audiobooks by title or author. Returns matching
        titles. Use audiobook_get to actually download one."""
        results = _search(query)
        if not results:
            raise ModelRetry(f"no audiobooks found on AudiobookBay for '{query}'. "
                             "Try a different title or author.")
        return [{"title": r["title"]} for r in results[:10]]

    def audiobook_get(query: str) -> dict:
        """Find and DOWNLOAD an audiobook from AudiobookBay by title/author in one step
        (picks the top match). Starts the torrent in qBittorrent; it appears in the
        audiobook library once downloaded. Asynchronous — report it's downloading."""
        results = _search(query)
        if not results:
            raise ModelRetry(f"no audiobooks found on AudiobookBay for '{query}'.")
        best = results[0]
        info_hash = _scrape_hash(best["url"])
        if not info_hash:
            raise ModelRetry(f"audiobook: found '{best['title']}' but could not read its "
                             "torrent hash from the page. Try audiobook_search and another title.")
        _qbt_add(_magnet(info_hash, best["title"]))
        return {"grabbed": True, "title": best["title"], "source": "AudiobookBay",
                "note": "downloading; it will appear in the audiobook library once imported"}

    return [
        Tool(name="audiobook_search",
             description=("Search AudiobookBay for audiobooks by title or author. "
                          "Use audiobook_get to download one."),
             tags=["audiobook", "books", "search", "audiobookbay", "abb", "find", "listen"],
             func=audiobook_search, provider="audiobook", example={"query": "Project Hail Mary"}),
        Tool(name="audiobook_get",
             description=("Find and download an audiobook from AudiobookBay by title/"
                          "author in one step. Asynchronous download."),
             tags=["audiobook", "books", "get", "acquire", "download", "audiobookbay", "abb"],
             func=audiobook_get, provider="audiobook",
             example={"query": "Project Hail Mary by Andy Weir"}),
    ]
