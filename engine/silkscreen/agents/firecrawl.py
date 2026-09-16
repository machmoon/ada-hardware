"""Firecrawl search-and-scrape, stdlib only, behind the repo's HTTP seam.

One endpoint is used: ``POST https://api.firecrawl.dev/v2/search`` with
``scrapeOptions``, which searches the web *and* returns each result's page as
markdown in the same response. That is exactly the call dzhng/deep-research
makes (``firecrawl.search(query, {timeout, limit, scrapeOptions: {formats:
['markdown']}})`` in ``src/deep-research.ts`` at ``1f8f3e2``), on the current
v2 path. The request and response shapes are Firecrawl's own, read from
firecrawl/firecrawl ``cc06662``:

* request -- ``apps/python-sdk/firecrawl/v2/methods/search.py``
  ``_prepare_search_request`` (``query``, ``limit`` 1..100, ``timeout`` in
  milliseconds, ``scrapeOptions`` camelCased) and
  ``v2/utils/validation.py`` ``prepare_scrape_options`` (``formats``,
  ``onlyMainContent``);
* auth -- ``v2/utils/http_client.py`` ``_prepare_headers``:
  ``Authorization: Bearer <key>``;
* response -- ``{"success": true, "data": {"web": [...]}}``, each item a
  document (``url``, ``title``, ``description``, ``markdown``, ``metadata``)
  when scraped, or a bare result when not; a result Firecrawl could not
  scrape has no ``markdown`` and a ``metadata.error``
  (``apps/api/src/__tests__/snips/v2/search.test.ts`` "works with scrape");
* errors -- ``v2/utils/error_handler.py`` ``handle_response_error``: the
  body's ``error`` string, and one class per status (400, 401, 402 payment
  required, 403 website not supported, 408, 429 rate limit, 500).

What is *not* the SDK's, on purpose. The SDK retries a 502 three times with
backoff and follows ``requests``' redirects; here a call is one request,
because the caller holds a page and wall-clock budget and a retry is spend
nobody budgeted, and a 3xx is refused because it would carry the bearer key
to wherever it points (``agents/distributor.py``'s and
``googleapps/transport.py``'s rule). The SDK accepts any ``api_url``; here
the host is pinned by an exact-match allowlist enforced when the request is
*built*, so it holds for the recorded transport in the tests too -- a
self-hosted Firecrawl is therefore not supported, and that is stated rather
than half-allowed. The search results' own URLs are data: this module never
requests them (Firecrawl did the fetching), so no scraped page's host ever
sees a request from here.
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

__all__ = [
    "ALLOWED_HOSTS",
    "API_HOST",
    "FIRECRAWL_API_KEY",
    "SEARCH_URL",
    "FirecrawlClient",
    "FirecrawlError",
    "FirecrawlRateLimited",
    "HttpRequest",
    "HttpResponse",
    "SearchHit",
    "Transport",
    "api_key_from_env",
    "ensure_allowed_url",
    "urllib_transport",
]

API_HOST = "api.firecrawl.dev"
#: Exact matches -- a suffix check would wave through ``api.firecrawl.dev.evil``.
ALLOWED_HOSTS: frozenset[str] = frozenset({API_HOST})
SEARCH_URL = f"https://{API_HOST}/v2/search"

FIRECRAWL_API_KEY = "FIRECRAWL_API_KEY"

#: A search of five scraped pages. Each page is typically tens of KB of
#: markdown; 8 MB is far above a real answer and far below a runaway one.
MAX_RESPONSE_BYTES = 8 << 20
#: Firecrawl's own ceiling on ``limit`` (``_validate_search_request``).
MAX_LIMIT = 100
#: The SDK's ceiling on ``timeout`` (300,000 ms).
MAX_TIMEOUT_MS = 300_000

_USER_AGENT = "silkscreen-web-research/0.1"
_DETAIL_CHARS = 200

#: ``handle_response_error``'s status table, as short codes.
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    402: "payment_required",
    403: "website_not_supported",
    408: "timeout",
    429: "rate_limited",
    500: "server_error",
}


class FirecrawlError(RuntimeError):
    """A Firecrawl request failed or was refused before it was sent.

    ``code`` is short (``unauthorized``, ``payment_required``, ``http_502``,
    ``bad_host``, ``redirect_refused``, ``network_error``, ``too_large``,
    ``bad_json``); the message never carries a header, so never the key.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class FirecrawlRateLimited(FirecrawlError):
    """A 429: the account's rate limit, said so in words."""


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    #: Socket timeout for this one request, in seconds.
    timeout_s: float = 60.0


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def ensure_allowed_url(url: str) -> str:
    """``url`` unchanged if it is https to :data:`API_HOST`, else refused."""
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise FirecrawlError("bad_host", f"refusing non-https scheme {parsed.scheme!r}")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise FirecrawlError(
            "bad_host", f"refusing to send to {parsed.hostname!r}: not Firecrawl's API"
        )
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is reported as its status: a redirect would carry the key."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def urllib_transport() -> Transport:
    """The real transport. A non-2xx is a response, not an exception."""
    opener = urllib.request.build_opener(_NoRedirect())

    def send(request: HttpRequest) -> HttpResponse:
        ensure_allowed_url(request.url)
        req = urllib.request.Request(
            request.url,
            data=request.body,
            headers=request.headers,
            method=request.method,
        )
        try:
            with opener.open(req, timeout=request.timeout_s) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return HttpResponse(resp.status, resp.read(MAX_RESPONSE_BYTES + 1), headers)
        except urllib.error.HTTPError as exc:
            with exc:
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                return HttpResponse(exc.code, exc.read(64 << 10), headers)
        except urllib.error.URLError as exc:
            raise FirecrawlError("network_error", str(exc.reason)) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise FirecrawlError("network_error", str(exc) or "socket error") from exc

    return send


def api_key_from_env(environ: Mapping[str, str] | None = None) -> str | None:
    """``FIRECRAWL_API_KEY``, stripped, or None when unset or blank."""
    env = os.environ if environ is None else environ
    value = env.get(FIRECRAWL_API_KEY, "")
    if not isinstance(value, str):
        return None
    return value.strip() or None


@dataclass(frozen=True)
class SearchHit:
    """One web result. ``markdown`` is None when Firecrawl did not scrape it,
    and then ``error`` says why when Firecrawl said."""

    url: str
    title: str | None
    description: str | None
    markdown: str | None
    error: str | None = None
    status_code: int | None = None


def _hit(item: Any) -> SearchHit | None:
    if not isinstance(item, dict):
        return None
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    url = item.get("url") or metadata.get("sourceURL") or metadata.get("url")
    if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
        return None
    title = item.get("title") or metadata.get("title")
    description = item.get("description") or metadata.get("description")
    markdown = item.get("markdown")
    error = metadata.get("error")
    status = metadata.get("statusCode")
    return SearchHit(
        url=url,
        title=title.strip() if isinstance(title, str) and title.strip() else None,
        description=description if isinstance(description, str) else None,
        markdown=markdown if isinstance(markdown, str) and markdown.strip() else None,
        error=str(error)[:_DETAIL_CHARS] if error else None,
        status_code=status if isinstance(status, int) and not isinstance(status, bool) else None,
    )


class FirecrawlClient:
    """The one read Firecrawl request web research needs."""

    def __init__(self, api_key: str, transport: Transport | None = None):
        key = (api_key or "").strip()
        if not key:
            raise FirecrawlError("unconfigured", f"{FIRECRAWL_API_KEY} is not set")
        self._key = key
        self._transport = transport if transport is not None else urllib_transport()

    def search_request(self, query: str, *, limit: int, timeout_ms: int) -> HttpRequest:
        """The exact request. The host check runs here, for every transport."""
        ensure_allowed_url(SEARCH_URL)
        if not query.strip():
            raise ValueError("a Firecrawl search needs a non-empty query")
        limit = max(1, min(int(limit), MAX_LIMIT))
        timeout_ms = max(1_000, min(int(timeout_ms), MAX_TIMEOUT_MS))
        payload = {
            "query": query,
            "limit": limit,
            "timeout": timeout_ms,
            "scrapeOptions": {"formats": ["markdown"], "onlyMainContent": True},
        }
        return HttpRequest(
            "POST",
            SEARCH_URL,
            {
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": _USER_AGENT,
            },
            json.dumps(payload).encode("utf-8"),
            # The socket waits a little past the server's own timeout, so a
            # slow scrape comes back as Firecrawl's 408, not a socket error.
            timeout_s=timeout_ms / 1000 + 10,
        )

    def search(self, query: str, *, limit: int, timeout_ms: int = 60_000) -> list[SearchHit]:
        """Search and scrape. Raises :class:`FirecrawlError` for every failure."""
        response = self._transport(
            self.search_request(query, limit=limit, timeout_ms=timeout_ms)
        )
        if 300 <= response.status < 400:
            raise FirecrawlError(
                "redirect_refused",
                f"Firecrawl answered HTTP {response.status}; not followed (it "
                "would carry the API key)",
            )
        if len(response.body) > MAX_RESPONSE_BYTES:
            raise FirecrawlError("too_large", f"body over {MAX_RESPONSE_BYTES} bytes")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            payload = None
        message = ""
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            message = payload["error"][:_DETAIL_CHARS]
        if response.status != 200:
            code = _STATUS_CODES.get(response.status, f"http_{response.status}")
            detail = f"Firecrawl answered HTTP {response.status}" + (
                f": {message}" if message else ""
            )
            if response.status == 429:
                raise FirecrawlRateLimited(code, detail)
            raise FirecrawlError(code, detail)
        if not isinstance(payload, dict):
            raise FirecrawlError("bad_json", "Firecrawl answered with a body that was not JSON")
        if payload.get("success") is not True:
            raise FirecrawlError("unsuccessful", message or "Firecrawl said success: false")
        data = payload.get("data")
        web = data.get("web") if isinstance(data, dict) else None
        if web is None and isinstance(data, dict):
            return []  # no web results for this query: a real, empty answer
        if not isinstance(web, list):
            raise FirecrawlError("bad_json", "search answered without a data.web list")
        return [hit for hit in (_hit(item) for item in web) if hit is not None]
