"""Audiobook lane — search AudiobookBay (ABB) and download audiobooks.

ABB is scraped DIRECTLY from the Argus host (anvil), which has the VPN that can reach
audiobookbay.lu — the home ISP IP is blocked, so the old nyx audiobook-getter could
never reach ABB. The magnet is then added straight to qBittorrent on glassgarden
(qBt does the torrenting; it needs no ABB access). The nyx getter is OUT of the loop.

Written fresh for Argus (approach inspired by Shane's audiobook-getter, nothing ported;
not Hermes). Module-level functions (search/latest/genres/grab/cover) are reused by
both the chat tools and the Forge audiobook widget (via ui/server.py endpoints).
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
_DEFAULT_GENRES = ["Fantasy", "Sci-Fi", "Thriller", "LitRPG", "Horror",
                   "Romance", "Mystery", "Non-Fiction"]

# Politeness throttle: ABB rate-limits (and serves its homepage) under rapid hits, so
# enforce a minimum gap between requests regardless of how often we call or retry it.
_MIN_INTERVAL = 3.0
_last_request = [0.0]
_cfg_cache: dict | None = None


def has_config(path: str = _CFG) -> bool:
    return os.path.exists(path)


def _cfg() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = yaml.safe_load(open(_CFG)) or {}
    return _cfg_cache


def _throttle() -> None:
    wait = _MIN_INTERVAL - (_time.monotonic() - _last_request[0])
    if wait > 0:
        _time.sleep(wait)
    _last_request[0] = _time.monotonic()


def _abb_get(url: str) -> str:
    _throttle()
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=25, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise ModelRetry(f"audiobook: could not reach AudiobookBay ({e}). Is the VPN up?")
    return r.text


def _abb_base() -> str:
    return str(_cfg()["abb_base"]).rstrip("/")


def _posts(doc) -> list[dict]:
    """Extract result posts (title, url, cover) from a #content listing/search page."""
    out, seen = [], set()
    for post in doc.xpath('//div[@id="content"]//div[contains(@class,"post")]'):
        a = post.xpath('.//h2/a[contains(@href,"/abss/")] | .//h3/a[contains(@href,"/abss/")]')
        if not a:
            continue
        href = a[0].get("href", "")
        if not href or href in seen:
            continue
        title = " ".join(a[0].text_content().split()).strip()
        if not title:
            continue
        seen.add(href)
        cover = ""
        for img in post.xpath(".//img"):
            src = img.get("src", "")
            if any(h in src for h in ("media-amazon", "ptpimg", "imgur", "audiobookbay")):
                cover = src
                break
        out.append({"title": title,
                    "url": href if href.startswith("http") else _abb_base() + href,
                    "cover": cover})
    return out


def search(query: str) -> list[dict]:
    """ABB search, filtered to query-term matches (ABB serves its homepage when rate-
    limited; the throttle spaces requests and the filter rejects homepage noise)."""
    terms = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3]

    def _match(item):
        t = item["title"].lower()
        hits = sum(1 for w in terms if w in t)
        return hits if (hits and (hits >= len(terms) - 1 or hits >= 2 or not terms)) else 0

    for _ in range(3):
        posts = _posts(_lxml.fromstring(_abb_get(f"{_abb_base()}/?s={quote_plus(query)}")))
        ranked = sorted(((_match(p), p) for p in posts), key=lambda x: x[0], reverse=True)
        relevant = [p for s, p in ranked if s > 0]
        if relevant or not terms:
            return relevant or posts
    return []


def latest(limit: int = 24) -> list[dict]:
    """Latest audiobook uploads (the ABB homepage listing)."""
    return _posts(_lxml.fromstring(_abb_get(f"{_abb_base()}/")))[:limit]


def genres() -> list[str]:
    return _cfg().get("genres", _DEFAULT_GENRES)


def cover(page_url: str) -> str:
    """Scrape an ABB book page for its cover image."""
    doc = _lxml.fromstring(_abb_get(page_url))
    for img in doc.xpath("//img"):
        src = img.get("src", "")
        if any(h in src for h in ("media-amazon", "ptpimg", "imgur")):
            return src
    return ""


def _magnet(info_hash: str, title: str = "") -> str:
    mag = f"magnet:?xt=urn:btih:{info_hash}"
    if title:
        mag += f"&dn={quote_plus(title)}"
    for tr in _cfg().get("trackers", []):
        mag += f"&tr={quote_plus(tr)}"
    return mag


def _qbt_add(magnet: str) -> None:
    cfg = _cfg()
    qbt = str(cfg["qbt_url"]).rstrip("/")
    with httpx.Client(timeout=25, headers={"Referer": qbt}) as c:
        login = c.post(f"{qbt}/api/v2/auth/login",
                       data={"username": cfg.get("qbt_user", ""), "password": cfg.get("qbt_pass", "")})
        if login.status_code >= 300:
            raise ModelRetry(f"audiobook: qBittorrent login failed (HTTP {login.status_code}).")
        add = c.post(f"{qbt}/api/v2/torrents/add",
                     data={"urls": magnet, "category": cfg.get("category", "audiobooks")})
        if add.status_code >= 300:
            raise ModelRetry(f"audiobook: qBittorrent rejected the add (HTTP {add.status_code}).")


def grab(page_url: str, title: str = "") -> dict:
    """Grab a specific ABB result by its page URL: scrape hash -> magnet -> qBittorrent."""
    info_hash = _scrape_hash(page_url)
    if not info_hash:
        raise ModelRetry(f"audiobook: could not read the torrent hash for '{title or page_url}'.")
    _qbt_add(_magnet(info_hash, title))
    return {"grabbed": True, "title": title or page_url, "source": "AudiobookBay",
            "note": "downloading; it will appear in the audiobook library once imported"}


def _scrape_hash(page_url: str) -> str | None:
    text = _lxml.fromstring(_abb_get(page_url)).text_content()
    m = re.search(r"info\s*hash[^0-9a-fA-F]{0,20}([a-fA-F0-9]{40})", text, re.I)
    if m:
        return m.group(1).lower()
    found = [h.lower() for h in _HASH.findall(text)]
    return found[0] if found else None


# --- chat tools ---------------------------------------------------------------
def _search_tool(query: str) -> list:
    """Search AudiobookBay for audiobooks by title or author. Use audiobook_get to download one."""
    res = search(query)
    if not res:
        raise ModelRetry(f"no audiobooks found on AudiobookBay for '{query}'. Try another title/author.")
    return [{"title": r["title"]} for r in res[:10]]


def _get_tool(query: str) -> dict:
    """Find and DOWNLOAD an audiobook from AudiobookBay by title/author in one step
    (picks the top match). Asynchronous — report it's downloading."""
    res = search(query)
    if not res:
        raise ModelRetry(f"no audiobooks found on AudiobookBay for '{query}'.")
    return grab(res[0]["url"], res[0]["title"])


def tools(manifest_path: str = _CFG) -> list[Tool]:
    global _cfg_cache, _CFG
    if manifest_path != _CFG:   # allow tests to point at a temp config
        _CFG = manifest_path
        _cfg_cache = None
    _cfg()  # validate config loads
    return [
        Tool(name="audiobook_search",
             description=("Search AudiobookBay for audiobooks by title or author. "
                          "Use audiobook_get to download one."),
             tags=["audiobook", "books", "search", "audiobookbay", "abb", "find", "listen"],
             func=_search_tool, provider="audiobook", example={"query": "Project Hail Mary"}),
        Tool(name="audiobook_get",
             description=("Find and download an audiobook from AudiobookBay by title/"
                          "author in one step. Asynchronous download."),
             tags=["audiobook", "books", "get", "acquire", "download", "audiobookbay", "abb"],
             func=_get_tool, provider="audiobook",
             example={"query": "Project Hail Mary by Andy Weir"}),
    ]
