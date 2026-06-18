"""Model-agnostic web tools: search + fetch.

`web_search` is pluggable across backends (Tavily / Brave / SerpAPI with a key,
or keyless DuckDuckGo HTML as the default). `web_fetch` retrieves a URL and
reduces it to readable text. These are our own tools — no provider's built-in
web tool is required, so they work with any model.
"""
from __future__ import annotations

import html
import os
import re
import urllib.parse

import httpx

_UA = "Mozilla/5.0 (compatible; EuronAgent/0.3)"
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_DDG_RESULT = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)


def _strip_html(raw: str) -> str:
    raw = _SCRIPT.sub(" ", raw)
    text = _TAG.sub(" ", raw)
    text = html.unescape(text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def web_fetch(url: str, max_chars: int = 8000, timeout: int = 20) -> tuple[bool, str]:
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, follow_redirects=True, timeout=timeout)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return False, f"fetch failed: {e}"
    ctype = r.headers.get("content-type", "")
    body = r.text if "html" in ctype or "text" in ctype or not ctype else r.text
    text = _strip_html(body) if "html" in ctype else body
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… (truncated)"
    return True, f"# {url}\n\n{text}"


def web_search(
    query: str,
    provider: str = "duckduckgo",
    api_key: str = "",
    max_results: int = 5,
) -> tuple[bool, str]:
    try:
        if provider == "tavily" and api_key:
            return _tavily(query, api_key, max_results)
        if provider == "brave" and api_key:
            return _brave(query, api_key, max_results)
        if provider == "serpapi" and api_key:
            return _serpapi(query, api_key, max_results)
        return _duckduckgo(query, max_results)
    except Exception as e:  # noqa: BLE001
        return False, f"search failed: {e}"


def _fmt(results: list[dict]) -> tuple[bool, str]:
    if not results:
        return True, "(no results)"
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title', '')}\n   {r.get('url', '')}\n   {r.get('snippet', '')}")
    return True, "\n".join(lines)


def _duckduckgo(query: str, n: int) -> tuple[bool, str]:
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    r = httpx.get(url, headers={"User-Agent": _UA}, follow_redirects=True, timeout=20)
    r.raise_for_status()
    results = []
    for href, title in _DDG_RESULT.findall(r.text)[:n]:
        results.append({"title": _strip_html(title), "url": html.unescape(href), "snippet": ""})
    return _fmt(results)


def _tavily(query: str, key: str, n: int) -> tuple[bool, str]:
    r = httpx.post(
        "https://api.tavily.com/search",
        json={"api_key": key, "query": query, "max_results": n},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    results = [
        {"title": x.get("title"), "url": x.get("url"), "snippet": x.get("content", "")[:200]}
        for x in data.get("results", [])
    ]
    return _fmt(results)


def _brave(query: str, key: str, n: int) -> tuple[bool, str]:
    r = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        params={"q": query, "count": n},
        timeout=20,
    )
    r.raise_for_status()
    web = r.json().get("web", {}).get("results", [])
    results = [
        {"title": x.get("title"), "url": x.get("url"), "snippet": x.get("description", "")}
        for x in web
    ]
    return _fmt(results)


def _serpapi(query: str, key: str, n: int) -> tuple[bool, str]:
    r = httpx.get(
        "https://serpapi.com/search.json",
        params={"q": query, "api_key": key, "num": n},
        timeout=20,
    )
    r.raise_for_status()
    org = r.json().get("organic_results", [])[:n]
    results = [
        {"title": x.get("title"), "url": x.get("link"), "snippet": x.get("snippet", "")}
        for x in org
    ]
    return _fmt(results)


def search_config_from_env() -> tuple[str, str]:
    """Pick a search provider based on which API key env var is set."""
    if os.getenv("TAVILY_API_KEY"):
        return "tavily", os.environ["TAVILY_API_KEY"]
    if os.getenv("BRAVE_API_KEY"):
        return "brave", os.environ["BRAVE_API_KEY"]
    if os.getenv("SERPAPI_API_KEY"):
        return "serpapi", os.environ["SERPAPI_API_KEY"]
    return "duckduckgo", ""
