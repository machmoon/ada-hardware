"""Find open-source hardware that already solves the request, and read it.

The research half of :mod:`silkscreen.prior_art`: GitHub for the projects,
a model for the judgement, and the deterministic checks in the IR module for
everything the model claims. The flow is three model calls around a handful
of GitHub requests, each call validated the :mod:`silkscreen.netlist` way
(every failure batched into one repair prompt, one repair round, then give
up loudly):

1. **queries** -- the model turns the intent into at most three plain
   keyword searches plus the ``owner/name`` of projects it *recalls*.
   Recall is not trusted: each recalled name is looked up with
   ``GET /repos/{owner}/{repo}`` and a name GitHub does not have lands in
   ``missing``. This exists because keyword search alone was measured
   (2026-09-13) to miss the best answer: "robot arm" does not return
   TheRobotStudio/SO-ARM100 (7.4k stars) at all, whose description is
   "Standard Open Arm 100". A model that recalls it, checked by GitHub, does.
2. **shortlist** -- from the search metadata (name, description, topics,
   stars, licence), the model picks the few that are actually prior art for
   this device. The same measured search returns cactus-compute/cactus, an
   LLM runtime, second by stars; reading its README would waste a slot.
3. **extract** -- the README and any text BOM file of each shortlisted
   project go to the model, which returns relevance and cited facts. The
   facts then pass :func:`silkscreen.prior_art.check_citations`; the ones
   whose quote is not in the file are dropped and reported.

Licence, stars, activity and the URL never come from the model -- see the IR
module. Mechanical source files are counted from ``git/trees``.

**The HTTP seam** is :mod:`.distributor`'s (and ``googleapps/transport.py``'s):
a :class:`Transport` callable, a urllib implementation that refuses every
redirect, and an exact-host allowlist enforced when the request is *built*,
so it holds for the recorded transport in the tests as well. Two hosts:

* ``api.github.com`` -- search, repository, readme and tree endpoints.
* ``raw.githubusercontent.com`` -- BOM files. GitHub serves file bytes here
  without counting against the REST rate limit, which matters because an
  unauthenticated client gets 60 core requests an hour. ``GITHUB_TOKEN`` is
  sent to ``api.github.com`` only; raw files of a public repository need no
  credential, so none is sent.

**Rate limits are said out loud.** Unauthenticated, GitHub allows 10
searches a minute and 60 other requests an hour; one research run spends up
to 3 searches, up to 6 repository lookups and two requests per shortlisted
project. A 403/429 whose body or headers say "rate limit" (PyGithub's test,
``github/Requester.py`` ``isPrimaryRateLimitError`` /
``isSecondaryRateLimitError`` at PyGithub ``9152817``) becomes
:class:`GitHubRateLimited`, whose message names the limit, when it resets
and that ``GITHUB_TOKEN`` raises it -- and a run that hit it before finding
anything has status ``"rate_limited"``, never an empty ``"none_found"``.
The request shapes follow the GitHub CLI's search client
(``pkg/search/searcher.go`` at cli/cli ``ba51bb4``: ``q``, ``sort``,
``order``, ``per_page``, ``Accept: application/vnd.github+json``).
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar
from urllib.parse import quote, urlencode, urlsplit

from ..prior_art import (
    FACT_FIELDS,
    RELEVANCE_FLOOR,
    MechanicalFiles,
    PriorArtResult,
    PriorArtValidationError,
    Project,
    Repo,
    check_citations,
    mechanical_files,
    onshape_links,
    parse_extraction,
    parse_queries,
    parse_shortlist,
    repo_from_api,
    repo_score,
)
from .model import Model

__all__ = [
    "ALLOWED_HOSTS",
    "API_HOST",
    "EXTRACT_MARKER",
    "GITHUB_TOKEN",
    "MAX_CANDIDATES",
    "PRIOR_ART_MARKER",
    "QUERY_MARKER",
    "RAW_HOST",
    "SHORTLIST_MARKER",
    "GitHubClient",
    "GitHubError",
    "GitHubRateLimited",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "ensure_allowed_url",
    "research",
    "urllib_transport",
]

#: Appears verbatim in all three prompts; each call adds its own marker, so a
#: ``ScriptedModel.by_marker`` keys on the step (the ``SOURCING_MARKER`` rule).
PRIOR_ART_MARKER = "PRIOR-ART v1"
QUERY_MARKER = "PRIOR-ART-QUERIES v1"
SHORTLIST_MARKER = "PRIOR-ART-SHORTLIST v1"
EXTRACT_MARKER = "PRIOR-ART-EXTRACT v1"

API_HOST = "api.github.com"
RAW_HOST = "raw.githubusercontent.com"
#: Exact matches -- a suffix check would wave through ``api.github.com.evil``.
ALLOWED_HOSTS: frozenset[str] = frozenset({API_HOST, RAW_HOST})

GITHUB_TOKEN = "GITHUB_TOKEN"

#: A search page, a repository, a readme: GitHub caps a readme blob served
#: through the contents API at 1 MB, and its base64 is a third larger.
MAX_JSON_BYTES = 2 << 20
#: A recursive tree. PAROL6's is 3.2 MB (7,994 entries: its docs site is
#: checked in), so the cap sits above that rather than failing a real arm.
MAX_TREE_BYTES = 8 << 20
#: A text BOM file. PAROL6's BOM.md is 12 KB; a 256 KB markdown table is
#: already far more than the excerpt the model is shown.
MAX_FILE_BYTES = 256 << 10

#: How much of each file the model sees. Quotes are checked against exactly
#: this excerpt, never the untruncated file: a quote from past the cut cannot
#: have been read.
README_EXCERPT_CHARS = 16_000
BOM_EXCERPT_CHARS = 12_000

#: Projects read in full (readme + tree + BOM files) per run.
MAX_CANDIDATES = 5
#: BOM files read per project.
MAX_BOM_FILES = 2
#: Search results shown to the shortlist call.
MAX_POOL = 30
SEARCH_PER_PAGE = 10

_USER_AGENT = "silkscreen-prior-art/0.1"
_WARNING_CHARS = 400

_BOM_NAME = re.compile(r"(^|[^a-z])(bom|bill[ _-]?of[ _-]?materials)([^a-z]|$)")
_TEXT_EXTS = (".md", ".markdown", ".csv", ".tsv", ".txt")
_BINARY_BOM_EXTS = (".pdf", ".xlsx", ".xls", ".ods", ".numbers")


# ---------------------------------------------------------------- transport


class GitHubError(RuntimeError):
    """A GitHub request failed or was refused before it was sent.

    ``code`` is short (``not_found``, ``http_500``, ``bad_host``,
    ``network_error``, ``too_large``, ``bad_json``); the message never
    carries a header, so it never carries the token."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class GitHubRateLimited(GitHubError):
    """GitHub refused the request for rate. The message is the warning."""

    def __init__(
        self,
        resource: str,
        reset_epoch: int | None,
        authenticated: bool,
        retry_after_s: int | None = None,
    ):
        self.resource = resource
        self.reset_epoch = reset_epoch
        self.authenticated = authenticated
        self.retry_after_s = retry_after_s
        if retry_after_s is not None:
            when = f"retry in {retry_after_s} s"
        elif reset_epoch is not None:
            when = f"it resets in {max(0, reset_epoch - int(time.time()))} s"
        else:
            when = "GitHub did not say when it resets"
        hint = (
            ""
            if authenticated
            else f"; set {GITHUB_TOKEN} for 5,000 requests an hour instead of 60 "
            "(searches: 30 a minute instead of 10)"
        )
        super().__init__(
            "rate_limited",
            f"GitHub's {resource} rate limit is exhausted ({when}){hint}",
        )


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    #: Lower-cased names; the rate-limit headers are read from here.
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


def ensure_allowed_url(url: str) -> str:
    """``url`` unchanged if it is https to an :data:`ALLOWED_HOSTS` host."""
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise GitHubError("bad_host", f"refusing non-https scheme {parsed.scheme!r}")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise GitHubError(
            "bad_host", f"refusing to send to {parsed.hostname!r}: not a GitHub host"
        )
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is reported as its status: a redirect would carry the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def urllib_transport(timeout_s: float = 15.0) -> Transport:
    """The real transport. A non-2xx is a response, not an exception."""
    opener = urllib.request.build_opener(_NoRedirect())

    def send(request: HttpRequest) -> HttpResponse:
        ensure_allowed_url(request.url)
        req = urllib.request.Request(
            request.url, headers=request.headers, method=request.method
        )
        try:
            with opener.open(req, timeout=timeout_s) as resp:
                headers = {k.lower(): v for k, v in resp.headers.items()}
                return HttpResponse(resp.status, resp.read(MAX_TREE_BYTES + 1), headers)
        except urllib.error.HTTPError as exc:
            with exc:
                headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                return HttpResponse(exc.code, exc.read(MAX_JSON_BYTES + 1), headers)
        except urllib.error.URLError as exc:
            raise GitHubError("network_error", str(exc.reason)) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise GitHubError("network_error", str(exc) or "socket error") from exc

    return send


def _is_rate_limit_message(message: str) -> bool:
    """PyGithub ``Requester.isRateLimitError``, verbatim in substance."""
    text = message.strip().lower()
    return (
        text.startswith("api rate limit exceeded")
        or text.startswith("you have exceeded a secondary rate limit")
        or text.endswith("please retry your request again later.")
        or text.endswith("please wait a few minutes before you try again.")
    )


def _int_header(headers: Mapping[str, str], name: str) -> int | None:
    try:
        return int(float(headers[name]))
    except (KeyError, ValueError):
        return None


class GitHubClient:
    """The few read-only GitHub requests research needs."""

    def __init__(self, transport: Transport | None = None, token: str | None = None):
        self._transport = transport if transport is not None else urllib_transport()
        self._token = (token or "").strip() or None

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def request(
        self, url: str, *, accept: str = "application/vnd.github+json"
    ) -> HttpRequest:
        """The exact request. The host check runs here, for every transport,
        and the token is attached for ``api.github.com`` only."""
        ensure_allowed_url(url)
        headers = {"Accept": accept, "User-Agent": _USER_AGENT}
        if urlsplit(url).hostname == API_HOST:
            headers["X-GitHub-Api-Version"] = "2022-11-28"
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
        return HttpRequest("GET", url, headers)

    def _send(self, url: str, limit: int, **kwargs: Any) -> HttpResponse:
        response = self._transport(self.request(url, **kwargs))
        if response.status in (403, 429):
            message = ""
            try:
                payload = json.loads(response.body.decode("utf-8"))
                if isinstance(payload, dict) and isinstance(
                    payload.get("message"), str
                ):
                    message = payload["message"]
            except (UnicodeDecodeError, ValueError):
                pass
            remaining = _int_header(response.headers, "x-ratelimit-remaining")
            retry_after = _int_header(response.headers, "retry-after")
            if (
                _is_rate_limit_message(message)
                or remaining == 0
                or retry_after is not None
                or response.status == 429
            ):
                raise GitHubRateLimited(
                    response.headers.get("x-ratelimit-resource", "core"),
                    _int_header(response.headers, "x-ratelimit-reset"),
                    self.authenticated,
                    retry_after,
                )
        if len(response.body) > limit:
            raise GitHubError("too_large", f"body over {limit} bytes")
        return response

    def _json(self, url: str, limit: int = MAX_JSON_BYTES) -> Any:
        response = self._send(url, limit)
        if response.status == 404:
            raise GitHubError("not_found", "GitHub answered 404")
        if response.status in (301, 302, 307, 308):
            raise GitHubError(
                "moved",
                "GitHub answered a redirect (a renamed repository?); not followed",
            )
        if response.status != 200:
            raise GitHubError(
                f"http_{response.status}", f"GitHub answered HTTP {response.status}"
            )
        try:
            return json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GitHubError(
                "bad_json", "GitHub answered with a body that was not JSON"
            ) from exc

    def search_repositories(
        self, query: str, per_page: int = SEARCH_PER_PAGE
    ) -> list[Repo]:
        params = urlencode(
            {"q": query, "sort": "stars", "order": "desc", "per_page": per_page}
        )
        payload = self._json(f"https://{API_HOST}/search/repositories?{params}")
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise GitHubError("bad_json", "search answered without an items list")
        return [r for r in (repo_from_api(i) for i in items) if r is not None]

    def repo(self, full_name: str) -> Repo | None:
        """The repository, or None when GitHub has no such name."""
        try:
            payload = self._json(f"https://{API_HOST}/repos/{_path(full_name)}")
        except GitHubError as exc:
            if exc.code in ("not_found", "moved"):
                return None
            raise
        return repo_from_api(payload)

    def readme(self, repo: Repo) -> tuple[str, str] | None:
        """``(path, text)`` of the preferred README, or None when there is none."""
        try:
            payload = self._json(
                f"https://{API_HOST}/repos/{_path(repo.full_name)}/readme"
            )
        except GitHubError as exc:
            if exc.code == "not_found":
                return None
            raise
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            raise GitHubError("bad_json", "readme answered without base64 content")
        path = payload.get("path")
        try:
            text = base64.b64decode(payload.get("content") or "").decode(
                "utf-8", "replace"
            )
        except (ValueError, TypeError) as exc:
            raise GitHubError("bad_json", "readme content was not base64") from exc
        return (path if isinstance(path, str) else "README.md"), text

    def tree(self, repo: Repo) -> Any:
        branch = quote(repo.default_branch, safe="")
        return self._json(
            f"https://{API_HOST}/repos/{_path(repo.full_name)}/git/trees/{branch}"
            "?recursive=1",
            MAX_TREE_BYTES,
        )

    def raw_file(self, repo: Repo, path: str) -> str:
        url = (
            f"https://{RAW_HOST}/{_path(repo.full_name)}/"
            f"{quote(repo.default_branch, safe='')}/{quote(path, safe='/')}"
        )
        response = self._send(url, MAX_FILE_BYTES, accept="text/plain")
        if response.status != 200:
            raise GitHubError(
                f"http_{response.status}", f"raw file answered HTTP {response.status}"
            )
        return response.body.decode("utf-8", "replace")


def _path(full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


# ---------------------------------------------------------------- prompts

_HEADER = (
    "You are a senior hardware engineer researching open-source prior art "
    f"({PRIOR_ART_MARKER})."
)

QUERY_PROMPT = f"""\
{_HEADER} Step: {QUERY_MARKER}.
Before designing anything, find well-regarded open-source hardware projects on
GitHub that already build what the engineer asked for. Respond with ONE JSON
object -- no prose, no code fence:

{{"queries": ["<plain keywords>", ...], "known_repos": ["owner/name", ...]}}

Rules:
1. 1 to 3 GitHub repository-search queries. Plain keywords only (no
   qualifiers such as in: or topic:), 2 to 4 words each. Every word must
   match, and search reads only a repository's name, description and topics,
   so use the words such a project would describe itself with ("robot arm",
   "robotic arm 6dof") and leave out filler like "open source", "DIY" or
   "project", which excludes projects that do not repeat it.
2. known_repos: the GitHub owner/name of well-known open-source projects you
   are confident already solve this, at most 6. Each one is looked up, and a
   name GitHub does not have is reported as a miss. If unsure, leave it out.
"""

SHORTLIST_PROMPT = f"""\
{_HEADER} Step: {SHORTLIST_MARKER}.
Below are GitHub repositories found for the request. Pick the ones that are
real prior art: the design of a comparable physical device (mechanical
files, a bill of materials, electronics), not a software library, a
simulator, a course list or an unrelated project that shares a word.
Respond with ONE JSON object -- no prose, no code fence:

{{"shortlist": [{{"repo": "<owner/name exactly as listed>", "relevance": 0.0-1.0}}]}}

At most {{limit}} entries, best first, only repositories listed below. An
empty shortlist is a correct answer when none of them is prior art.
"""

EXTRACT_PROMPT = f"""\
{_HEADER} Step: {EXTRACT_MARKER}.
Below are files from open-source projects. For EVERY project listed, say how
relevant it is to the request and extract the facts its files state.
Respond with ONE JSON object -- no prose, no code fence:

{{"projects": [
  {{"repo": "<owner/name>",
   "relevance": 0.0-1.0,
   "why": "<one sentence: why this is or is not prior art for the request>",
   "facts": [
     {{"field": "<one of the fields below>",
      "value": "<copied verbatim from inside the quote>",
      "label": "<what it measures, for link_length/joint_range>" | null,
      "quantity": <count of parts stated in the same quote, for bom_item,
                   actuator, motor_driver, controller or power_supply only> | null,
      "quote": "<the exact text, copied character for character, max 400 chars>",
      "source": "<the FILE path exactly as given>"}}
  ]}}
]}}

Fields: {", ".join(sorted(FACT_FIELDS))}.
  dof: number of joints/axes (value a whole number); actuator: a motor or
  servo as named; motor_driver: a stepper driver, servo bus adapter or motor
  control board; controller: the microcontroller or controller board;
  supply_voltage / power_supply: as stated; payload / reach: with units;
  link_length / joint_range: label names the link or joint; bom_item: one
  bill-of-materials line, value the part as written.

Hard rules -- facts breaking these are thrown away automatically:
1. Only what the files SAY. Never add knowledge of your own, never convert
   units, never infer a number. A field nobody stated is simply left out.
2. The quote must be copied exactly from the named file (table pipes and
   bold markers may be dropped). The value must appear inside the quote.
3. Every quote is checked against the file text. Invented quotes are caught.
"""


# ---------------------------------------------------------------- the loop

T = TypeVar("T")


def _ask(
    model: Model,
    base: str,
    parse: Callable[[str], T],
    *,
    step: str,
    max_repairs: int,
    on_event: Callable[[dict[str, Any]], None] | None,
    max_output_tokens: int,
) -> tuple[T | None, list[str]]:
    """One call plus at most ``max_repairs`` repairs; ``(None, errors)`` on
    give-up. ``ModelError`` propagates unwrapped (``propose_circuit``'s rule)."""
    prompt = base
    errors: list[str] = []
    for round_no in range(max_repairs + 1):
        raw = model.generate(
            prompt, temperature=0.0, max_output_tokens=max_output_tokens
        )
        try:
            return parse(raw), []
        except PriorArtValidationError as exc:
            errors = list(exc.errors)
        if on_event is not None:
            on_event(
                {
                    "event": "prior_art.round",
                    "step": step,
                    "round": round_no + 1,
                    "errors": len(errors),
                    "first_error": errors[0][:160] if errors else "",
                }
            )
        if round_no == max_repairs:
            break
        problems = "\n".join(f"  - {e}" for e in errors)
        prompt = (
            f"{base}\nYour previous answer was rejected. Fix ALL of these and "
            f"return the corrected JSON object:\n{problems}\n\n"
            f"Your previous answer was:\n{raw[:8000]}\n"
        )
    return None, errors


_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "that",
        "which",
        "who",
        "can",
        "could",
        "should",
        "would",
        "will",
        "to",
        "of",
        "for",
        "with",
        "and",
        "or",
        "in",
        "on",
        "at",
        "by",
        "from",
        "up",
        "it",
        "its",
        "is",
        "are",
        "be",
        "i",
        "we",
        "me",
        "my",
        "our",
        "want",
        "need",
        "make",
        "build",
        "design",
        "some",
        "like",
    ]
)


def fallback_queries(intent: str) -> list[str]:
    """The query when the model gave none: the first four content words."""
    words = [
        w
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9-]*", intent)
        if w.casefold() not in _STOPWORDS
    ]
    return [" ".join(words[:4])] if words else []


def _candidate_line(repo: Repo) -> str:
    desc = (repo.description or "").replace("\n", " ")[:160]
    topics = ",".join(repo.topics[:8])
    pushed = (repo.pushed_at or "?")[:10]
    return (
        f"- {repo.full_name} | {repo.stars} stars"
        f" | licence {repo.license_spdx or 'none'}"
        f" | pushed {pushed}{' | ARCHIVED' if repo.archived else ''} | {desc}"
        + (f" | topics: {topics}" if topics else "")
    )


def _bom_paths(tree: Any) -> tuple[list[tuple[str, int]], list[tuple[str, str]]]:
    """Text BOM files to read (shallowest first) and binary ones to report."""
    readable: list[tuple[str, int]] = []
    unread: list[tuple[str, str]] = []
    entries = tree.get("tree") if isinstance(tree, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        base = path.rsplit("/", 1)[-1].lower()
        stem = base.rsplit(".", 1)[0]
        if not _BOM_NAME.search(stem):
            continue
        size = entry.get("size") if isinstance(entry.get("size"), int) else 0
        if base.endswith(_TEXT_EXTS):
            if size > MAX_FILE_BYTES:
                unread.append((path, f"over the {MAX_FILE_BYTES // 1024} KB read cap"))
            else:
                readable.append((path, size))
        elif base.endswith(_BINARY_BOM_EXTS):
            unread.append((path, "a PDF or spreadsheet; not read"))
    readable.sort(key=lambda item: (item[0].count("/"), item[0]))
    return readable, unread


def research(
    intent: str,
    *,
    model: Model,
    transport: Transport | None = None,
    token: str | None = None,
    environ: Mapping[str, str] | None = None,
    max_candidates: int = MAX_CANDIDATES,
    max_repairs: int = 1,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    now: datetime | None = None,
) -> PriorArtResult:
    """Find, read and cite the open-source projects that already solve ``intent``.

    ``transport`` defaults to the real urllib one; ``token`` defaults to
    :data:`GITHUB_TOKEN` from ``environ`` (default ``os.environ``).
    ``now`` pins the activity score for tests.

    Never returns a quiet empty list: status ``"rate_limited"`` or
    ``"unavailable"`` when nothing could be searched, ``"none_found"`` when
    the search ran and nothing relevant came back, ``"found"`` otherwise --
    with every partial failure (a repo that could not be read, a recalled
    name GitHub lacks, a fact whose quote was not in its file) in words.

    Raises:
        ModelError: the model could not be reached (not wrapped).
    """
    env = os.environ if environ is None else environ
    client = GitHubClient(
        transport, token if token is not None else env.get(GITHUB_TOKEN)
    )
    result = PriorArtResult(
        intent=intent, status="none_found", authenticated=client.authenticated
    )

    def emit(event: dict[str, Any]) -> None:
        if on_event is not None:
            on_event(event)

    # 1. queries ----------------------------------------------------------
    answer, errors = _ask(
        model,
        f"{QUERY_PROMPT}\nWhat the engineer asked for:\n{intent}\n",
        parse_queries,
        step="queries",
        max_repairs=max_repairs,
        on_event=on_event,
        max_output_tokens=2048,
    )
    if answer is None:
        queries, known = fallback_queries(intent), []
        result.warnings.append(
            "the model gave no usable search queries, so the intent's own words "
            f"were searched: {'; '.join(errors)[:_WARNING_CHARS]}"
        )
    else:
        queries, known = answer
    result.queries = list(queries)

    # 2. search + recalled names ------------------------------------------
    pool: dict[str, Repo] = {}
    stopped: GitHubError | None = None
    for query in queries:
        try:
            hits = client.search_repositories(query)
        except GitHubError as exc:
            stopped = exc
            break
        emit({"event": "prior_art.search", "query": query, "hits": len(hits)})
        for repo in hits:
            pool.setdefault(repo.full_name.casefold(), repo)
    if not isinstance(stopped, GitHubRateLimited):
        for name in known:
            if name.casefold() in pool:
                continue
            try:
                repo = client.repo(name)
            except GitHubError as exc:
                stopped = stopped or exc
                break
            if repo is None:
                result.missing.append(name)
            else:
                pool[repo.full_name.casefold()] = repo
    if result.missing:
        result.warnings.append(
            "recalled by the model but not on GitHub: " + ", ".join(result.missing)
        )
    if stopped is not None:
        if not pool:
            result.status = (
                "rate_limited"
                if isinstance(stopped, GitHubRateLimited)
                else "unavailable"
            )
            result.warnings.append(
                str(stopped)
                if isinstance(stopped, GitHubRateLimited)
                else f"GitHub could not be searched ({stopped})"
            )
            return result
        result.warnings.append(f"searching stopped early: {stopped}")

    forks = [r for r in pool.values() if r.fork]
    scored = sorted(
        (r for r in pool.values() if not r.fork),
        key=lambda r: repo_score(r, now=now),
        reverse=True,
    )[:MAX_POOL]
    if forks:
        result.warnings.append(f"{len(forks)} fork(s) left out of the candidates")
    if not scored:
        return result  # none_found: the search ran and returned nothing usable

    # 3. shortlist --------------------------------------------------------
    names = [r.full_name for r in scored]
    by_name = {r.full_name: r for r in scored}
    shortlist_prompt = (
        SHORTLIST_PROMPT.replace("{limit}", str(max_candidates))
        + f"\nWhat the engineer asked for:\n{intent}\n\nRepositories:\n"
        + "\n".join(_candidate_line(r) for r in scored)
        + "\n"
    )
    picked, errors = _ask(
        model,
        shortlist_prompt,
        lambda raw: parse_shortlist(raw, names, max_candidates),
        step="shortlist",
        max_repairs=max_repairs,
        on_event=on_event,
        max_output_tokens=2048,
    )
    if picked is None:
        # Read the most reputable few instead, unjudged: the extraction call
        # still sees their files and rates them, so an off-topic one lands in
        # ``considered`` rather than being recommended.
        order: list[str] = names[:max_candidates]
        relevance: dict[str, float | None] = dict.fromkeys(order)
        result.warnings.append(
            "the model gave no usable shortlist, so the most-starred candidates "
            "were read unjudged: " + "; ".join(errors)[:_WARNING_CHARS]
        )
    elif not picked:
        return result  # none_found, and the model said so in so many words
    else:
        order = sorted(picked, key=lambda n: picked[n], reverse=True)
        relevance = dict(picked)
    projects = [
        Project(
            repo=by_name[n],
            score=repo_score(by_name[n], now=now),
            relevance=relevance[n],
        )
        for n in order
    ]

    # 4. read -------------------------------------------------------------
    texts: dict[str, dict[str, str]] = {}
    limited: GitHubRateLimited | None = None
    for project in projects:
        repo = project.repo
        docs: dict[str, str] = {}
        if limited is not None:
            project.notes.append(f"not read: {limited}")
            continue
        try:
            readme = client.readme(repo)
            if readme is None:
                project.notes.append("the repository has no README")
            else:
                docs[readme[0]] = readme[1][:README_EXCERPT_CHARS]
            try:
                tree = client.tree(repo)
            except GitHubError as exc:
                if isinstance(exc, GitHubRateLimited):
                    raise
                tree = None
                project.notes.append(f"the file tree could not be listed ({exc})")
            project.mechanical = mechanical_files(tree)
            if project.mechanical.truncated:
                project.notes.append(
                    "GitHub truncated the file tree; file counts are a lower bound"
                )
            readable, unread = _bom_paths(tree)
            project.unread.extend(unread)
            for path, _size in readable[:MAX_BOM_FILES]:
                try:
                    docs[path] = client.raw_file(repo, path)[:BOM_EXCERPT_CHARS]
                except GitHubError as exc:
                    if isinstance(exc, GitHubRateLimited):
                        raise
                    project.unread.append((path, f"could not be fetched ({exc.code})"))
            for path, _size in readable[MAX_BOM_FILES:]:
                project.unread.append((path, f"past the {MAX_BOM_FILES}-file read cap"))
        except GitHubRateLimited as exc:
            limited = exc
            project.notes.append(f"read stopped: {exc}")
        except GitHubError as exc:
            project.notes.append(f"could not be read ({exc})")
        project.documents = [(p, repo.blob_url(p)) for p in docs]
        project.mechanical = MechanicalFiles(
            counts=project.mechanical.counts,
            examples=project.mechanical.examples,
            onshape=onshape_links(docs),
            truncated=project.mechanical.truncated,
            listed=project.mechanical.listed,
        )
        if docs:
            texts[repo.full_name] = docs
    if limited is not None:
        result.warnings.append(f"reading stopped early: {limited}")

    # 5. extract ----------------------------------------------------------
    if texts:
        sections = []
        for name, docs in texts.items():
            files = "\n".join(
                f"--- FILE {path} ---\n{text}\n--- END FILE {path} ---"
                for path, text in docs.items()
            )
            sections.append(
                f"=== PROJECT {name} ===\n{files}\n=== END PROJECT {name} ==="
            )
        extract_prompt = (
            f"{EXTRACT_PROMPT}\nWhat the engineer asked for:\n{intent}\n\n"
            + "\n\n".join(sections)
            + "\n"
        )
        read_names = list(texts)
        extracted, errors = _ask(
            model,
            extract_prompt,
            lambda raw: parse_extraction(raw, read_names),
            step="extract",
            max_repairs=max_repairs,
            on_event=on_event,
            max_output_tokens=16_384,
        )
        if extracted is None:
            result.warnings.append(
                "the model's facts could not be read, so projects are listed with "
                "none: " + "; ".join(errors)[:_WARNING_CHARS]
            )
        else:
            for project in projects:
                entry = extracted.get(project.repo.full_name)
                if entry is None:
                    continue
                relevance, why, raw_facts = entry
                project.relevance, project.why = relevance, why
                kept, dropped = check_citations(
                    project.repo, raw_facts, texts[project.repo.full_name]
                )
                project.facts = kept
                result.dropped.extend(dropped)
                emit(
                    {
                        "event": "prior_art.project",
                        "repo": project.repo.full_name,
                        "relevance": relevance,
                        "facts": len(kept),
                        "dropped": len(dropped),
                    }
                )
    if result.dropped:
        result.warnings.append(
            f"{len(result.dropped)} fact(s) dropped: their quote or value was not "
            "in the file they cited"
        )

    ranked = sorted(projects, key=lambda p: p.rank, reverse=True)
    result.projects = [p for p in ranked if (p.relevance or 0.0) >= RELEVANCE_FLOOR]
    result.considered = [p for p in ranked if (p.relevance or 0.0) < RELEVANCE_FLOOR]
    result.status = "found" if result.projects else "none_found"
    return result
