"""Web research: what the open web says about building the request, as a contract.

:mod:`silkscreen.prior_art` reads GitHub repositories. This module is the
same idea pointed at the rest of the web -- build logs, vendor application
notes, forum threads, product pages -- found by a search engine and scraped
to markdown by Firecrawl. It is the deterministic half: the budget, the
record of every query and page, the facts pulled out of those pages and the
checks they must pass. It makes no network call and no model call;
:mod:`silkscreen.agents.web_research` owns both.

**The shape is dzhng/deep-research's** (``src/deep-research.ts`` at
``1f8f3e2``): a request becomes a few search queries each carrying a
*research goal*; each query's results are read into learnings plus
follow-up questions; and while depth remains, the goal and the follow-ups
seed the next, narrower round, with breadth halved each level
(``newBreadth = Math.ceil(breadth / 2)``). Two deliberate differences, both
about what a hardware pipeline can trust:

* **A learning is a cited fact, never free text.** deep-research's learnings
  are the model's prose. Here every one is a :class:`~silkscreen.prior_art.Fact`
  with a verbatim quote and the URL of the page it was read on, and it goes
  through :func:`silkscreen.prior_art.cite` -- the exact filter a GitHub
  README fact goes through. A fact whose quote is not in the page excerpt the
  model was shown is dropped and reported in :attr:`WebResearchResult.dropped`.
  The model is a witness, not an author. The quote rule follows
  langchain-ai/open_deep_research's ``summarize_webpage_prompt``
  (``src/open_deep_research/prompts.py`` at ``1b7d2e8``), which keeps
  ``key_excerpts`` verbatim beside a summary; there is no summary here,
  because a summary is exactly the unverifiable part.
* **There is no final report.** deep-research's ``writeFinalReport`` and
  open_deep_research's ``compress_research`` hand everything to a model to
  write up. The write-up here is :meth:`WebResearchResult.brief_text`, a
  deterministic rendering of the cited facts with numbered sources -- the
  open_deep_research citation format (``[1] Title: URL``) without a model
  in between that could reword a number.

**Bounded autonomy.** open_deep_research caps its supervisor with
``max_researcher_iterations`` and ``max_concurrent_research_units``
(``configuration.py``) and ends a researcher on ``ResearchComplete``; a
branch here ends when the model gives no follow-ups (the same signal) and
the whole run is held to a :class:`ResearchBudget` -- breadth, depth, a page
budget (Firecrawl bills per scraped page) and a wall clock. Every stop is a
sentence in :attr:`WebResearchResult.stops`, never a silently shorter list.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from .prior_art import (
    FACT_FIELDS,
    Dropped,
    Fact,
    _parse_fact,
    cite,
    normalise,
)

__all__ = [
    "DEFAULT_BUDGET",
    "MAX_BREADTH",
    "MAX_DEPTH",
    "MAX_PAGES",
    "MAX_PAGES_PER_QUERY",
    "MAX_WALL_CLOCK_S",
    "PAGE_EXCERPT_CHARS",
    "PART_FIELDS",
    "WEB_FACT_FIELDS",
    "WEB_STATUSES",
    "QueryRecord",
    "ResearchBudget",
    "SerpQuery",
    "WebResearchResult",
    "WebResearchValidationError",
    "WebSource",
    "cite_pages",
    "fact_key",
    "follow_up_breadth",
    "parse_learnings",
    "parse_serp_queries",
]

# ---------------------------------------------------------------- vocabulary

#: The GitHub vocabulary plus what a web page about a device states that an
#: arm's README rarely does. Frozen for the same reason :data:`FACT_FIELDS`
#: is: the prompt block renders these names, so an invented one would never
#: be shown.
WEB_FACT_FIELDS: frozenset[str] = FACT_FIELDS | frozenset(
    {
        "sensor",  # "BME280", "MPU-6050 IMU"
        "interface",  # a bus or protocol as named: "CAN bus", "I2C"
        "battery",  # "2S 18650", "LiPo 3.7V 2000mAh"
        "connector",  # "XT60", "JST-XH 2.54mm"
        "mechanical_part",  # "MR105 bearing", "GT2 belt"
        "design_rule",  # a stated practice: "decouple each servo rail"
    }
)

#: The fields that name something you buy -- what the sourcing prompt is shown.
PART_FIELDS: frozenset[str] = frozenset(
    {
        "actuator",
        "motor_driver",
        "controller",
        "power_supply",
        "bom_item",
        "sensor",
        "battery",
        "connector",
        "mechanical_part",
    }
)

#: ``found`` and ``none_found`` are answers. The other three say the question
#: could not be asked -- and ``unconfigured`` (no ``FIRECRAWL_API_KEY``) is
#: a refusal in words, never an empty ``none_found``.
WEB_STATUSES: frozenset[str] = frozenset(
    {"found", "none_found", "rate_limited", "unavailable", "unconfigured"}
)

# deep-research's defaults are breadth 4, depth 2 and five results a search
# (``src/run.ts``, ``src/deep-research.ts``). Every search there scrapes every
# result, and Firecrawl bills per scraped page, so the defaults here are one
# notch lower on each axis and a page budget sits over the lot. The caps are
# refused, not clamped: a caller asking for depth 5 learns it cannot have it.
MAX_BREADTH = 4
MAX_DEPTH = 3
MAX_PAGES_PER_QUERY = 5
MAX_PAGES = 40
MAX_WALL_CLOCK_S = 300.0

#: How much of each page the model sees, and exactly what quotes are checked
#: against. deep-research trims a page to 25,000 characters and
#: open_deep_research to ``max_content_length`` 50,000; three pages at that
#: size is one prompt of 75-150 KB per query, so a page is cut shorter here.
PAGE_EXCERPT_CHARS = 8_000

MAX_QUERY_CHARS = 120
MAX_GOAL_CHARS = 400
MAX_FOLLOW_UP_CHARS = 200
MAX_FACTS_PER_ROUND = 24
MAX_TITLE_CHARS = 160


class WebResearchValidationError(ValueError):
    """The model's answer could not be used; ``errors`` is the whole batch,
    for one repair prompt (:class:`silkscreen.netlist.ValidationError`)."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            f"{len(self.errors)} problem(s) in the web-research answer:\n  - "
            + "\n  - ".join(self.errors)
        )


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class ResearchBudget:
    """How much autonomy one run gets. Validated whole, every problem at once."""

    breadth: int = 3
    depth: int = 2
    pages_per_query: int = 3
    max_pages: int = 20
    wall_clock_s: float = 120.0

    def __post_init__(self) -> None:
        errors: list[str] = []
        for name, value, cap in (
            ("breadth", self.breadth, MAX_BREADTH),
            ("depth", self.depth, MAX_DEPTH),
            ("pages_per_query", self.pages_per_query, MAX_PAGES_PER_QUERY),
            ("max_pages", self.max_pages, MAX_PAGES),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"{name} must be a whole number, got {value!r}")
            elif not 1 <= value <= cap:
                errors.append(f"{name} must be 1..{cap}, got {value}")
        wall = self.wall_clock_s
        if isinstance(wall, bool) or not isinstance(wall, (int, float)):
            errors.append(f"wall_clock_s must be a number, got {wall!r}")
        elif not (math.isfinite(wall) and 0 < wall <= MAX_WALL_CLOCK_S):
            errors.append(
                f"wall_clock_s must be in (0, {MAX_WALL_CLOCK_S:g}], got {wall}"
            )
        if errors:
            raise ValueError("research budget refused: " + "; ".join(errors))

    def as_dict(self) -> dict[str, Any]:
        return {
            "breadth": self.breadth,
            "depth": self.depth,
            "pages_per_query": self.pages_per_query,
            "max_pages": self.max_pages,
            "wall_clock_s": self.wall_clock_s,
        }


DEFAULT_BUDGET = ResearchBudget()


def follow_up_breadth(breadth: int) -> int:
    """deep-research's ``Math.ceil(breadth / 2)``: each level is narrower."""
    return max(1, math.ceil(breadth / 2))


@dataclass(frozen=True)
class SerpQuery:
    """One search and why it is being run (deep-research's ``researchGoal``)."""

    query: str
    goal: str


@dataclass
class WebSource:
    """A result Firecrawl returned, and whether its text was read."""

    url: str
    title: str | None
    query: str
    depth: int
    #: Characters of markdown shown to the model; 0 when none was.
    chars: int = 0
    read: bool = False
    #: Why it was not read -- Firecrawl's scrape error, or "already read".
    note: str | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "host": self.host,
            "title": self.title,
            "query": self.query,
            "depth": self.depth,
            "read": self.read,
            "chars": self.chars,
            "note": self.note,
        }


@dataclass
class QueryRecord:
    """What one query did: results, pages read, facts kept and dropped."""

    query: str
    goal: str
    depth: int
    results: int = 0
    read: int = 0
    facts: int = 0
    dropped: int = 0
    follow_ups: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "goal": self.goal,
            "depth": self.depth,
            "results": self.results,
            "read": self.read,
            "facts": self.facts,
            "dropped": self.dropped,
            "follow_ups": list(self.follow_ups),
            "notes": list(self.notes),
        }


@dataclass
class WebResearchResult:
    """What a web research run found, what it read, and where it stopped."""

    intent: str
    status: str
    budget: ResearchBudget = DEFAULT_BUDGET
    queries: list[QueryRecord] = field(default_factory=list)
    sources: list[WebSource] = field(default_factory=list)
    #: Cited facts, in the order they were found, de-duplicated.
    findings: list[Fact] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    #: Every budget stop, in words. Empty only when nothing was cut short.
    stops: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pages: int = 0
    model_calls: int = 0
    elapsed_s: float = 0.0

    def __post_init__(self) -> None:
        if self.status not in WEB_STATUSES:
            raise ValueError(
                f"web-research status {self.status!r} is not one of "
                f"{sorted(WEB_STATUSES)}"
            )

    def _numbered(self, facts: list[Fact]) -> list[tuple[int, str, str | None]]:
        titles = {s.url: s.title for s in self.sources}
        order: list[str] = []
        for fact in facts:
            if fact.url not in order:
                order.append(fact.url)
        return [(i, url, titles.get(url)) for i, url in enumerate(order, start=1)]

    def brief_text(
        self,
        *,
        fields: frozenset[str] | None = None,
        limit: int = 30,
    ) -> str | None:
        """The cited facts as a prompt block with numbered sources, or None.

        Only kept facts appear, each tagged with the number of the page it was
        quoted from, so the designer can reuse a stated part without the block
        becoming a source of invented ones. ``fields`` narrows it (the sourcing
        prompt is shown :data:`PART_FIELDS` only). The heading says a page can
        be wrong: a citation proves the page said it, not that it is true.
        """
        facts = [f for f in self.findings if fields is None or f.field in fields]
        facts = facts[:limit]
        if not facts:
            return None
        numbered = self._numbered(facts)
        index = {url: i for i, url, _ in numbered}
        lines = [
            "Web research -- what pages on the web say about building this. "
            "Each line is quoted from the page numbered after it; a page can be "
            "wrong, so prefer a stated part where it fits this board and "
            "never copy a number that contradicts a datasheet:"
        ]
        for fact in facts:
            label = f" {fact.label}:" if fact.label else ""
            qty = f" x{fact.quantity}" if fact.quantity is not None else ""
            lines.append(
                f"- {fact.field}:{label} {fact.value}{qty} [{index[fact.url]}]"
            )
        lines.append("Sources:")
        for i, url, title in numbered:
            lines.append(f"[{i}] {(title or url)[:MAX_TITLE_CHARS]}: {url}")
        return "\n".join(lines)

    def mechanism_context(self) -> dict[str, Any] | None:
        """The findings in the ``{"projects": [...]}`` shape
        :func:`silkscreen.agents.mechanism.prior_art_block` reads, one entry
        per cited page, or None with nothing cited."""
        if not self.findings:
            return None
        pages: dict[str, dict[str, Any]] = {}
        titles = {s.url: s.title for s in self.sources}
        for fact in self.findings:
            page = pages.setdefault(
                fact.url,
                {"source": fact.url, "title": titles.get(fact.url) or "", "facts": []},
            )
            page["facts"].append(
                {k: v for k, v in fact.as_dict().items() if v is not None}
            )
        return {"projects": list(pages.values())}

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "status": self.status,
            "budget": self.budget.as_dict(),
            "pages": self.pages,
            "model_calls": self.model_calls,
            "elapsed_s": round(self.elapsed_s, 3),
            "queries": [q.as_dict() for q in self.queries],
            "sources": [s.as_dict() for s in self.sources],
            "findings": [f.as_dict() for f in self.findings],
            "dropped": [
                {"source": d.repo, "field": d.field, "value": d.value,
                 "reason": d.reason}
                for d in self.dropped
            ],
            "stops": list(self.stops),
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------- citations


def cite_pages(
    raw_facts: list[dict[str, Any]], pages: dict[str, str]
) -> tuple[list[Fact], list[Dropped]]:
    """:func:`silkscreen.prior_art.cite` over scraped pages.

    ``pages`` is ``{url: excerpt shown to the model}``. A fact's ``source``
    is the URL it quotes; each :class:`Dropped` names that URL, so a report
    says which page a refused quote claimed to be from.
    """
    kept: list[Fact] = []
    dropped: list[Dropped] = []
    for item in raw_facts:
        got, lost = cite(item["source"], [item], pages, url_for=lambda url: url)
        kept.extend(got)
        dropped.extend(lost)
    return kept, dropped


def fact_key(fact: Fact) -> tuple[str, str, str | None]:
    """Two pages stating the same part are one finding (the first cited)."""
    return (
        fact.field,
        normalise(fact.value),
        normalise(fact.label) if fact.label else None,
    )


# ---------------------------------------------------------------- parsing


def _json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*|\s*```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            payload = json.loads(match.group(0)) if match else None
        except json.JSONDecodeError:
            payload = None
        if payload is None:
            raise WebResearchValidationError(
                [f"the answer was not a JSON object (first 120 chars: {text[:120]!r})"]
            ) from None
    if not isinstance(payload, dict):
        raise WebResearchValidationError(
            [f"the answer must be a JSON object, got {type(payload).__name__}"]
        )
    return payload


def parse_serp_queries(raw: str, limit: int) -> list[SerpQuery]:
    """``{"queries": [{"query", "research_goal"}]}`` -> at most ``limit``.

    deep-research's ``generateSerpQueries`` schema, validated the
    :mod:`silkscreen.netlist` way: every problem batched, duplicates (by
    case-folded text) refused rather than silently merged so the repair
    prompt can say which.
    """
    payload = _json(raw)
    errors: list[str] = []
    items = payload.get("queries")
    if not isinstance(items, list):
        raise WebResearchValidationError(["'queries' must be a list of objects"])
    out: list[SerpQuery] = []
    seen: set[str] = set()
    for i, item in enumerate(items):
        where = f"queries[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object with 'query' and 'research_goal'")
            continue
        query, goal = item.get("query"), item.get("research_goal")
        if not isinstance(query, str) or not query.strip():
            errors.append(f"{where}.query must be a non-empty string")
            continue
        text = " ".join(query.split())
        if len(text) > MAX_QUERY_CHARS:
            errors.append(f"{where}.query is over {MAX_QUERY_CHARS} characters")
            continue
        if not isinstance(goal, str) or not goal.strip():
            errors.append(f"{where}.research_goal must be a non-empty string")
            continue
        if len(goal.strip()) > MAX_GOAL_CHARS:
            errors.append(f"{where}.research_goal is over {MAX_GOAL_CHARS} characters")
            continue
        if text.casefold() in seen:
            errors.append(f"{where}.query {text!r} repeats an earlier query")
            continue
        seen.add(text.casefold())
        out.append(SerpQuery(text, " ".join(goal.split())))
    if not out and not errors:
        errors.append("'queries' must name at least one search")
    if len(out) > limit:
        errors.append(f"{len(out)} queries; at most {limit}")
    if errors:
        raise WebResearchValidationError(errors)
    return out


def parse_learnings(
    raw: str, *, max_follow_ups: int
) -> tuple[list[dict[str, Any]], list[str]]:
    """``{"facts": [...], "follow_up_questions": [...]}`` -> (raw facts, follow-ups).

    Structure only. Whether a fact is *true to its page* is
    :func:`cite_pages`' job, and a fact failing that is dropped, never
    repaired -- a repair prompt would only teach the model which quote to
    fabricate (the :func:`silkscreen.prior_art.parse_extraction` rule).
    """
    payload = _json(raw)
    errors: list[str] = []
    facts_raw = payload.get("facts")
    if not isinstance(facts_raw, list):
        errors.append("'facts' must be a list (empty when the pages state nothing)")
        facts_raw = []
    if len(facts_raw) > MAX_FACTS_PER_ROUND:
        errors.append(f"{len(facts_raw)} facts; at most {MAX_FACTS_PER_ROUND}")
        facts_raw = []
    facts: list[dict[str, Any]] = []
    for j, fact in enumerate(facts_raw):
        parsed = _parse_fact(fact, f"facts[{j}]", errors, WEB_FACT_FIELDS)
        if parsed is not None:
            facts.append(parsed)
    follow_raw = payload.get("follow_up_questions", [])
    follow_ups: list[str] = []
    if not isinstance(follow_raw, list):
        errors.append("'follow_up_questions' must be a list of strings")
        follow_raw = []
    for k, question in enumerate(follow_raw):
        if not isinstance(question, str) or not question.strip():
            errors.append(f"follow_up_questions[{k}] must be a non-empty string")
        elif len(question.strip()) > MAX_FOLLOW_UP_CHARS:
            errors.append(
                f"follow_up_questions[{k}] is over {MAX_FOLLOW_UP_CHARS} characters"
            )
        else:
            follow_ups.append(" ".join(question.split()))
    if len(follow_ups) > max_follow_ups:
        errors.append(
            f"{len(follow_ups)} follow-up questions; at most {max_follow_ups}"
        )
    if errors:
        raise WebResearchValidationError(errors)
    return facts, follow_ups

