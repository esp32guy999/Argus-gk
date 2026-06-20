"""Web lane — give the agent the public internet: search + a text-only page reader.

- web_search(query): Brave Search API (BRAVE_API_KEY) with a DuckDuckGo HTML
  fallback when no key is set / Brave errors. Returns concise {title, url, snippet}.
- web_fetch(url): fetch a page and return its readable text — a "text browser".
  script/style/nav stripped, tags removed, whitespace collapsed, truncated to fit
  the model's context (ARGUS_WEB_FETCH_CHARS, default 6000).

Native-style tools, one required param each. Errors become teaching ModelRetry msgs.
Zero extra deps (stdlib HTMLParser); a readability lib (trafilatura) would improve
extraction quality later. No SSRF guard — the shell lane already grants full network
access, so web_fetch adds no new exposure on this single-user box.
"""
from __future__ import annotations

import html as _html
import os
import re
from html.parser import HTMLParser
from urllib.parse import unquote

import httpx
from pydantic_ai.exceptions import ModelRetry

from ..registry import Tool

_UA = "Mozilla/5.0 (compatible; ArgusBot/1.0; +homelab)"
_FETCH_MAX = int(os.environ.get("ARGUS_WEB_FETCH_CHARS", "6000"))
_SEARCH_N = 5


# --- web_fetch ---------------------------------------------------------------
class _TextExtractor(HTMLParser):
    """Collect visible text + the <title>, dropping non-content elements."""
    _SKIP = {"script", "style", "noscript", "svg", "nav", "footer",
             "header", "form", "aside", "button"}
    _BLOCK = {"p", "br", "div", "li", "h1", "h2", "h3", "h4", "h5", "tr", "section", "article"}

    def __init__(self):
        super().__init__()
        self._skip = 0
        self._in_title = False
        self.title = ""
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title and not self.title:
            self.title = text
        self.parts.append(text)


def _html_to_text(raw: str) -> tuple[str, str]:
    """Return (title, readable_text) from an HTML string."""
    p = _TextExtractor()
    try:
        p.feed(raw)
    except Exception:
        pass
    buf = ""
    for part in p.parts:
        buf += "\n" if part == "\n" else part + " "
    buf = _html.unescape(buf)
    buf = re.sub(r"[ \t]+", " ", buf)
    buf = re.sub(r"\n[ \t]*(\n[ \t]*)+", "\n\n", buf)
    return p.title, buf.strip()


def web_fetch(url: str) -> dict:
    """Fetch a web page and return its readable text (a text-only browser).
    Use this to READ a specific URL — e.g. a result from web_search. Returns the
    page title and text (truncated). Pass a full URL."""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    try:
        resp = httpx.get(url, headers={"User-Agent": _UA}, timeout=20,
                         follow_redirects=True)
    except httpx.RequestError as e:
        raise ModelRetry(f"web_fetch could not reach {url}: {e}. Check the URL.")
    if resp.status_code >= 400:
        raise ModelRetry(f"web_fetch: {url} returned HTTP {resp.status_code}.")
    ctype = resp.headers.get("content-type", "")
    if "html" not in ctype and "text" not in ctype:
        return {"url": str(resp.url), "text": f"[non-text content: {ctype or 'unknown'}]"}
    # Prefer trafilatura (readability boilerplate removal); fall back to the stdlib
    # extractor so the tool still works if trafilatura is absent.
    title, text = _html_to_text(resp.text)
    try:
        import trafilatura
        body = trafilatura.extract(resp.text, include_comments=False, include_tables=True)
        if body and len(body) > 50:
            text = body
    except Exception:
        pass
    return {"url": str(resp.url), "title": title, "text": text[:_FETCH_MAX],
            "truncated": len(text) > _FETCH_MAX}


# --- web_search --------------------------------------------------------------
def _parse_brave(data: dict) -> list[dict]:
    results = (data.get("web") or {}).get("results") or []
    out = []
    for x in results[:_SEARCH_N]:
        out.append({
            "title": x.get("title", ""),
            "url": x.get("url", ""),
            "snippet": re.sub(r"<[^>]+>", "", x.get("description", "") or ""),
        })
    return out


def _ddg(query: str) -> list[dict]:
    """Keyless fallback: scrape DuckDuckGo's HTML endpoint (best-effort)."""
    try:
        r = httpx.get("https://html.duckduckgo.com/html/", params={"q": query},
                      headers={"User-Agent": _UA}, timeout=15, follow_redirects=True)
    except httpx.RequestError as e:
        raise ModelRetry(f"web_search failed (no Brave key, DuckDuckGo unreachable): {e}")
    hits, html = [], r.text
    for m in re.finditer(r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        href, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        uddg = re.search(r"uddg=([^&]+)", href)
        url = unquote(uddg.group(1)) if uddg else _html.unescape(href)
        hits.append({"title": _html.unescape(title), "url": url, "snippet": ""})
        if len(hits) >= _SEARCH_N:
            break
    if not hits:
        raise ModelRetry("web_search returned no results. Try rephrasing the query.")
    return hits


def web_search(query: str) -> list:
    """Search the public internet and return the top results (title, url, snippet).
    Use this to look things up online (docs, settings, current info) that aren't in
    the homelab knowledge base. To read a result in full, pass its url to web_fetch."""
    key = os.environ.get("BRAVE_API_KEY")
    if key:
        try:
            r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                          params={"q": query, "count": _SEARCH_N},
                          headers={"X-Subscription-Token": key,
                                   "Accept": "application/json"}, timeout=15)
            if r.status_code < 400:
                hits = _parse_brave(r.json())
                if hits:
                    return hits
        except httpx.RequestError:
            pass  # fall back to DuckDuckGo
    return _ddg(query)


def tools() -> list[Tool]:
    return [
        Tool(
            name="web_search",
            description=("Search the public internet for information not in the homelab "
                         "knowledge base (docs, product settings, current facts). Returns "
                         "top results with title, url, snippet."),
            tags=["web", "search", "internet", "research", "lookup", "online"],
            func=web_search,
            example={"query": "Bambu P1S PETG print temperature recommended"},
        ),
        Tool(
            name="web_fetch",
            description=("Fetch a web page and return its readable text (a text-only "
                         "browser). Use to read a URL in full, e.g. a web_search result."),
            tags=["web", "fetch", "browse", "url", "internet", "read", "page"],
            func=web_fetch,
            example={"url": "https://wiki.bambulab.com/en/p1/manual"},
        ),
    ]
