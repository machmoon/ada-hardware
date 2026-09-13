"""Prior art: what open-source hardware already solves the request, as a contract.

The pipeline used to invent every design from a blank page. Someone asking
for "a 6-DOF desktop arm that can pick up a cup" is asking for something a
dozen well-maintained open projects already ship -- SO-ARM100, PAROL6,
BCN3D Moveo -- with a bill of materials, printable parts and a controller
that has been built hundreds of times. This module is the deterministic half
of finding and *using* those: the record of a project, the facts pulled out
of its files, and the checks those facts must pass before anything
downstream repeats them. It makes no network call and no model call;
:mod:`silkscreen.agents.prior_art` owns both.

**The model is a witness, not an author** -- :mod:`meetings.intent`'s rule,
applied to a README instead of a transcript. Every fact carries the verbatim
snippet it came from and the path of the file it was read in, and a fact
whose snippet is not in the text that file actually served is dropped and
reported in :attr:`PriorArtResult.dropped`. The value must also be *inside*
its own quote (``"STS3215 Servo 7.4V"`` is only a value if those characters
are in the quote), so a model cannot attach a real sentence to an invented
number. Anything nobody stated stays absent: there is no ``null`` fact, only
no fact, and :meth:`Project.fact` answers ``None``.

**What is not the model's to say.** The license, the star count, the last
push and the repository URL come from GitHub's API response, never from the
model -- PAROL6's README carries a badge whose alt text reads "License: MIT"
over a GPL-3.0 licence file, which is exactly the misreading this avoids.
The mechanical source files (STEP, STL, FreeCAD, ...) are counted from the
repository tree and the Onshape links matched by pattern in the fetched
text, deterministically, each with the path it was found at.

**Ranking** is the OpenSSF criticality_score algorithm, not a bespoke
formula: a weighted arithmetic mean of bounded, log-normalised signals
(``internal/scorer/algorithm/wam/wam.go``, ``input.go`` ``Bounds.Apply``,
``distribution.go`` ``zipfian = log(1+v)``; weights in the style of
``internal/scorer/default_config.yml`` at ossf/criticality_score
``0e76c6a``). Licence permissiveness uses Google licenseclassifier's category
taxonomy (``license_type.go`` at google/licenseclassifier ``3cfbab2``:
restricted / reciprocal / notice / unencumbered / forbidden), extended with
the three CERN Open Hardware Licences, which that list predates -- CERN's own
naming puts ``-P`` permissive, ``-W`` weakly reciprocal and ``-S`` strongly
reciprocal, so they map to notice, reciprocal and restricted.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote as _urlquote

__all__ = [
    "FACT_FIELDS",
    "INT_FIELDS",
    "LICENSE_CATEGORIES",
    "MAX_QUOTE_CHARS",
    "MIN_QUOTE_CHARS",
    "MECHANICAL_KINDS",
    "RELEVANCE_FLOOR",
    "STATUSES",
    "Dropped",
    "Fact",
    "MechanicalFiles",
    "PriorArtResult",
    "PriorArtValidationError",
    "Project",
    "Repo",
    "check_citations",
    "license_category",
    "mechanical_files",
    "normalise",
    "onshape_links",
    "parse_extraction",
    "parse_queries",
    "parse_shortlist",
    "rank_key",
    "repo_from_api",
    "repo_score",
]

# ---------------------------------------------------------------- vocabulary

#: The frozen fact vocabulary. A field outside it is a validation error, not
#: a new column: the downstream prompt block and the step route render these
#: names, so an invented one would silently never be shown.
FACT_FIELDS: frozenset[str] = frozenset(
    {
        "dof",  # joint / axis count, an integer
        "actuator",  # a motor or servo as named: "STS3215 Servo 7.4V"
        "motor_driver",  # a stepper driver, servo bus adapter, ESC
        "controller",  # the MCU or board: "ESP32", "Arduino Mega 2560"
        "supply_voltage",  # "12V", "5V"
        "power_supply",  # "12V 5A power supply"
        "payload",  # as stated, units included
        "reach",  # as stated, units included
        "link_length",  # label names the link
        "joint_range",  # label names the joint
        "bom_item",  # label is the part, quantity when a count is stated
    }
)

#: Fields whose value is a whole number and is checked as one.
INT_FIELDS: frozenset[str] = frozenset({"dof"})

#: Fields where a count is a count of parts. On a measurement ("1 kg
#: payload") a quantity is meaningless, and the live smoke run of 2026-09-13
#: showed a model filling it with the measurement's own number (payload
#: "1 kg" x1, reach "52 cm" x52), so elsewhere it is refused.
QUANTITY_FIELDS: frozenset[str] = frozenset(
    {"bom_item", "actuator", "motor_driver", "controller", "power_supply"}
)

#: Fields that need a label to mean anything ("link 2: 120 mm").
LABELLED_FIELDS: frozenset[str] = frozenset({"link_length", "joint_range"})

#: The frozen status vocabulary of a research run. ``none_found`` is a real
#: answer (searched, nothing relevant); the other two say the question could
#: not be asked, and neither may read as "nothing exists".
STATUSES: frozenset[str] = frozenset(
    {"found", "none_found", "rate_limited", "unavailable"}
)

#: Below this model relevance a project is kept in ``considered``, not
#: ``projects``: a 6,000-star LLM runtime matched by the words "robot arm"
#: is a search hit, not prior art.
RELEVANCE_FLOOR = 0.5

#: A quote this short matches almost any README by accident (``"6"``).
MIN_QUOTE_CHARS = 6

#: A quote this long is the model pasting the file back.
MAX_QUOTE_CHARS = 400

#: Value text over this is a paragraph, not a fact.
MAX_VALUE_CHARS = 160

MAX_QUERIES = 3
MAX_QUERY_CHARS = 64
MAX_QUERY_WORDS = 6
MAX_KNOWN_REPOS = 6
MAX_WHY_CHARS = 240
MAX_FACTS_PER_PROJECT = 40

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")

LICENSE_CATEGORIES: tuple[str, ...] = (
    "unencumbered",
    "notice",
    "reciprocal",
    "restricted",
    "forbidden",
    "unknown",
)

# licenseclassifier/license_type.go, the subset hardware repos carry, keyed by
# the SPDX id GitHub's API reports. CERN-OHL-* are the stated extension.
_LICENSE_TYPE: dict[str, str] = {
    **dict.fromkeys(("CC0-1.0", "Unlicense", "0BSD"), "unencumbered"),
    **dict.fromkeys(
        (
            "MIT",
            "Apache-2.0",
            "BSD-2-Clause",
            "BSD-3-Clause",
            "ISC",
            "CC-BY-3.0",
            "CC-BY-4.0",
            "Zlib",
            "CERN-OHL-P-2.0",
        ),
        "notice",
    ),
    **dict.fromkeys(("MPL-2.0", "EPL-2.0", "EPL-1.0", "CERN-OHL-W-2.0"), "reciprocal"),
    **dict.fromkeys(
        (
            "GPL-2.0",
            "GPL-3.0",
            "LGPL-2.1",
            "LGPL-3.0",
            "CC-BY-SA-3.0",
            "CC-BY-SA-4.0",
            "CC-BY-ND-4.0",
            "CERN-OHL-S-2.0",
            "CERN-OHL-1.2",
            "TAPR-OHL-1.0",
        ),
        "restricted",
    ),
    **dict.fromkeys(
        (
            "AGPL-3.0",
            "CC-BY-NC-3.0",
            "CC-BY-NC-4.0",
            "CC-BY-NC-SA-3.0",
            "CC-BY-NC-SA-4.0",
            "CC-BY-NC-ND-4.0",
            "WTFPL",
        ),
        "forbidden",
    ),
}

#: How reusable each category is, 0..1, for the ranking. The ordering is
#: licenseclassifier's; the numbers are ours (it has none).
_LICENSE_VALUE: dict[str, float] = {
    "unencumbered": 1.0,
    "notice": 1.0,
    "reciprocal": 0.6,
    "restricted": 0.4,
    "unknown": 0.1,
    "forbidden": 0.0,
}

#: Mechanical/electrical source kinds, by file extension.
MECHANICAL_KINDS: dict[str, tuple[str, ...]] = {
    "step": (".step", ".stp"),
    "stl": (".stl",),
    "3mf": (".3mf",),
    "freecad": (".fcstd",),
    "fusion360": (".f3d", ".f3z"),
    "solidworks": (".sldprt", ".sldasm"),
    "openscad": (".scad",),
    "urdf": (".urdf", ".xacro"),
    "kicad": (".kicad_pcb", ".kicad_sch"),
}

_ONSHAPE_RE = re.compile(
    r"https://cad\.onshape\.com/documents/[0-9a-f]{24}[^\s)\]\"'<>]*"
)


class PriorArtValidationError(ValueError):
    """The model's answer could not be used. ``errors`` is the whole batch,
    for one repair prompt -- :class:`silkscreen.netlist.ValidationError`'s
    contract."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__(
            f"{len(self.errors)} problem(s) in the prior-art answer:\n  - "
            + "\n  - ".join(self.errors)
        )


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class Repo:
    """One repository as GitHub's API described it. Never model text."""

    full_name: str
    html_url: str
    description: str | None
    stars: int
    forks: int
    pushed_at: str | None
    archived: bool
    fork: bool
    default_branch: str
    license_spdx: str | None
    topics: tuple[str, ...] = ()

    @property
    def license_category(self) -> str:
        return license_category(self.license_spdx)

    def blob_url(self, path: str) -> str:
        """The citation URL for a file at the default branch."""
        quoted = "/".join(_url_segment(p) for p in path.split("/"))
        return f"{self.html_url}/blob/{_url_segment(self.default_branch)}/{quoted}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "url": self.html_url,
            "description": self.description,
            "stars": self.stars,
            "forks": self.forks,
            "pushed_at": self.pushed_at,
            "archived": self.archived,
            "fork": self.fork,
            "default_branch": self.default_branch,
            "license": self.license_spdx,
            "license_category": self.license_category,
            "topics": list(self.topics),
        }


def _url_segment(text: str) -> str:
    return _urlquote(text, safe="-._~")


@dataclass(frozen=True)
class Fact:
    """One thing a project's own files state, and where."""

    field: str
    value: str
    quote: str
    source: str
    url: str
    label: str | None = None
    quantity: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "label": self.label,
            "quantity": self.quantity,
            "quote": self.quote,
            "source": self.source,
            "url": self.url,
        }


@dataclass(frozen=True)
class Dropped:
    """A fact the citation filter refused, and why -- reported, never silent."""

    repo: str
    field: str
    value: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "field": self.field,
            "value": self.value,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class MechanicalFiles:
    """Source files counted from the repository tree, by kind."""

    counts: dict[str, int] = field(default_factory=dict)
    examples: dict[str, tuple[str, ...]] = field(default_factory=dict)
    onshape: tuple[tuple[str, str], ...] = ()  # (link, path it was found in)
    #: GitHub's ``truncated`` flag: the tree was too large to list in full,
    #: so a zero count may be an unlisted file, and the dict says so.
    truncated: bool = False
    #: False when the tree could not be read at all -- counts mean nothing.
    listed: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "listed": self.listed,
            "truncated": self.truncated,
            "counts": dict(self.counts),
            "examples": {k: list(v) for k, v in self.examples.items()},
            "onshape": [{"url": u, "found_in": p} for u, p in self.onshape],
        }


@dataclass
class Project:
    """A candidate, what was read of it, and what it was found to state."""

    repo: Repo
    score: float
    documents: list[tuple[str, str]] = field(default_factory=list)  # (path, url)
    #: Files that look like a BOM but were not read (a PDF, a spreadsheet,
    #: over the size cap), each with the reason.
    unread: list[tuple[str, str]] = field(default_factory=list)
    mechanical: MechanicalFiles = field(
        default_factory=lambda: MechanicalFiles(listed=False)
    )
    relevance: float | None = None
    why: str | None = None
    facts: list[Fact] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def fact(self, name: str) -> Fact | None:
        """The first fact for ``name``, or None when nobody stated one."""
        for item in self.facts:
            if item.field == name:
                return item
        return None

    @property
    def rank(self) -> float:
        return rank_key(self.relevance, self.score)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo.as_dict(),
            "score": round(self.score, 4),
            "relevance": self.relevance,
            "rank": round(self.rank, 4),
            "why": self.why,
            "documents": [{"path": p, "url": u} for p, u in self.documents],
            "unread": [{"path": p, "reason": r} for p, r in self.unread],
            "mechanical": self.mechanical.as_dict(),
            "facts": [f.as_dict() for f in self.facts],
            "notes": list(self.notes),
        }


@dataclass
class PriorArtResult:
    """What a research run found, and what it could not do, in words."""

    intent: str
    status: str
    queries: list[str] = field(default_factory=list)
    #: Relevant projects, best first.
    projects: list[Project] = field(default_factory=list)
    #: Read but below :data:`RELEVANCE_FLOOR`, or never judged.
    considered: list[Project] = field(default_factory=list)
    dropped: list[Dropped] = field(default_factory=list)
    #: Repository names the model recalled that GitHub does not have.
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    authenticated: bool = False

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(
                f"prior-art status {self.status!r} is not one of {sorted(STATUSES)}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "status": self.status,
            "authenticated": self.authenticated,
            "queries": list(self.queries),
            "projects": [p.as_dict() for p in self.projects],
            "considered": [p.as_dict() for p in self.considered],
            "dropped": [d.as_dict() for d in self.dropped],
            "missing": list(self.missing),
            "warnings": list(self.warnings),
        }

    def brief_text(self, limit: int = 3, facts_per_project: int = 12) -> str | None:
        """A compact block for the plan/propose prompt, or None with nothing
        to say. Only cited facts appear, each with the file it came from, so
        the designer can reuse a proven part without the block itself
        becoming a source of invented ones."""
        if not self.projects:
            return None
        lines = [
            "Prior art -- open-source projects that already build this. Prefer "
            "their proven parts, voltages and controllers where they fit this "
            "board; everything below is quoted from the project's own files:"
        ]
        for index, project in enumerate(self.projects[:limit], start=1):
            repo = project.repo
            licence = repo.license_spdx or "licence not stated"
            lines.append(
                f"{index}. {repo.full_name} ({licence}, {repo.stars} stars) "
                f"{repo.html_url}"
            )
            for item in project.facts[:facts_per_project]:
                label = f" {item.label}:" if item.label else ""
                qty = f" x{item.quantity}" if item.quantity is not None else ""
                lines.append(
                    f"   - {item.field}:{label} {item.value}{qty} [{item.source}]"
                )
            kinds = [f"{n} {k}" for k, n in sorted(project.mechanical.counts.items())]
            if kinds:
                lines.append(f"   - mechanical files: {', '.join(kinds)}")
        return "\n".join(lines)


# ---------------------------------------------------------------- scoring


def license_category(spdx: str | None) -> str:
    """licenseclassifier's category for a GitHub ``spdx_id``.

    ``None`` and ``NOASSERTION`` (GitHub's word for a licence file it could
    not classify) are ``unknown`` -- not permissive: no stated licence means
    no right to reuse, whatever the README implies.
    """
    if not spdx or spdx == "NOASSERTION":
        return "unknown"
    return _LICENSE_TYPE.get(spdx, "unknown")


def _zipf(value: float, upper: float, smaller_is_better: bool = False) -> float:
    """criticality_score ``Input.Value``: bound, invert if asked, log-normalise."""
    v = min(max(value, 0.0), upper)
    if smaller_is_better:
        v = upper - v
    return math.log1p(v) / math.log1p(upper)


def _months_since(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (now - when).total_seconds() / (30.44 * 86400))


#: (signal, weight, upper bound, smaller_is_better) -- default_config.yml's
#: shape. Stars and forks stand in for contributor_count and
#: github_mention_count, which search results do not carry.
_SCORE_INPUTS = (
    ("stars", 2.0, 20_000.0, False),
    ("forks", 1.0, 5_000.0, False),
    ("updated_since", 1.0, 120.0, True),
)
_LICENSE_WEIGHT = 1.0


def repo_score(repo: Repo, *, now: datetime | None = None) -> float:
    """Popularity, recent activity and licence permissiveness, 0..1.

    The weighted arithmetic mean of ``wam.go``: an input with no value (no
    push date) is left out of both the sum and the weights rather than
    counted as zero.
    """
    now = now or datetime.now(UTC)
    signals = {
        "stars": float(repo.stars),
        "forks": float(repo.forks),
        "updated_since": _months_since(repo.pushed_at, now),
    }
    total = weights = 0.0
    for name, weight, upper, smaller in _SCORE_INPUTS:
        value = signals[name]
        if value is None:
            continue
        total += weight * _zipf(value, upper, smaller)
        weights += weight
    total += _LICENSE_WEIGHT * _LICENSE_VALUE[repo.license_category]
    weights += _LICENSE_WEIGHT
    return total / weights


def rank_key(relevance: float | None, score: float) -> float:
    """Final order: relevance to *this* request first, repute second.

    Equal weights, our choice -- criticality_score ranks projects, not
    projects against a request, so it has no precedent for the blend. An
    unjudged project ranks on repute alone at half weight, below any judged
    relevant one.
    """
    if relevance is None:
        return 0.5 * score
    return 0.5 * relevance + 0.5 * score


def repo_from_api(item: Any) -> Repo | None:
    """A :class:`Repo` from a search item or ``GET /repos`` body, or None
    when the object is not that shape."""
    if not isinstance(item, dict):
        return None
    name = item.get("full_name")
    url = item.get("html_url")
    if not isinstance(name, str) or not isinstance(url, str):
        return None
    if not url.startswith("https://github.com/"):
        return None
    licence = item.get("license")
    spdx = licence.get("spdx_id") if isinstance(licence, dict) else None
    topics = item.get("topics")

    def _int(key: str) -> int:
        value = item.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    description = item.get("description")
    pushed = item.get("pushed_at")
    branch = item.get("default_branch")
    return Repo(
        full_name=name,
        html_url=url,
        description=description if isinstance(description, str) else None,
        stars=_int("stargazers_count"),
        forks=_int("forks_count"),
        pushed_at=pushed if isinstance(pushed, str) else None,
        archived=item.get("archived") is True,
        fork=item.get("fork") is True,
        default_branch=branch if isinstance(branch, str) and branch else "main",
        license_spdx=spdx if isinstance(spdx, str) else None,
        topics=tuple(t for t in topics if isinstance(t, str))
        if isinstance(topics, list)
        else (),
    )


# ---------------------------------------------------------------- files


def mechanical_files(tree: Any, *, examples: int = 3) -> MechanicalFiles:
    """Count source files by kind in a ``git/trees?recursive=1`` body."""
    if not isinstance(tree, dict) or not isinstance(tree.get("tree"), list):
        return MechanicalFiles(listed=False)
    counts: dict[str, int] = {}
    found: dict[str, list[str]] = {}
    for entry in tree["tree"]:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue
        lower = path.lower()
        for kind, extensions in MECHANICAL_KINDS.items():
            if lower.endswith(extensions):
                counts[kind] = counts.get(kind, 0) + 1
                if len(found.setdefault(kind, [])) < examples:
                    found[kind].append(path)
                break
    return MechanicalFiles(
        counts=counts,
        examples={k: tuple(v) for k, v in found.items()},
        truncated=tree.get("truncated") is True,
    )


def onshape_links(documents: dict[str, str]) -> tuple[tuple[str, str], ...]:
    """Every Onshape document link in the fetched text, with its file."""
    seen: dict[str, str] = {}
    for path, text in documents.items():
        for match in _ONSHAPE_RE.finditer(text):
            seen.setdefault(match.group(0), path)
    return tuple(seen.items())


# ---------------------------------------------------------------- citations

_DASHES = str.maketrans(
    {c: "-" for c in "‐‑‒–—―−"} | {" ": " ", "‘": "'", "’": "'", "“": '"', "”": '"'}
)
#: Markdown decoration, not words: a model quoting a table row drops the
#: pipes and the bold markers, and an honest quote must survive that.
_MARKUP = re.compile(r"[|*`#>]+")


def normalise(text: str) -> str:
    """One comparable form for a quote and the text it claims to be from.

    meetings/intent.py folds case and whitespace and nothing else. Two
    deliberate additions, both about rendering rather than words: Unicode
    dashes and quotes fold to ASCII (the SO-ARM100 README spells "SO-101"
    with U+2011, which no model reproduces), and Markdown table/emphasis
    punctuation is removed from both sides.
    """
    folded = _MARKUP.sub(" ", text.translate(_DASHES))
    return re.sub(r"\s+", " ", folded).strip().casefold()


_NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "eleven",
    12: "twelve",
}


def _number_in(number: int, text: str) -> bool:
    # A number on its own: the 6 in "7.6V" is no six, and neither is the 6 in
    # "PAROL6" -- a project's name is not a statement of its axis count.
    if re.search(rf"(?<![\w.]){number}(?!\.?\d)", text):
        return True
    word = _NUMBER_WORDS.get(number)
    return bool(word and re.search(rf"\b{word}\b", text))


def _refusal(item: dict[str, Any], haystacks: dict[str, str]) -> str | None:
    """Why ``item`` fails the citation filter, or None when it passes."""
    name, value, source = item["field"], item["value"], item["source"]
    quantity = item.get("quantity")
    if source not in haystacks:
        return f"cites {source!r}, which was not read for this project"
    needle = normalise(item["quote"])
    if needle not in haystacks[source]:
        return f"its quote is not in {source}"
    if name in INT_FIELDS:
        if not _number_in(int(value), needle):
            return f"the number {value} is not in its own quote"
    elif normalise(value) not in needle:
        return "its value is not in its own quote"
    if quantity is not None and not _number_in(quantity, needle):
        return f"the quantity {quantity} is not in its own quote"
    return None


def check_citations(
    repo: Repo,
    raw_facts: list[dict[str, Any]],
    documents: dict[str, str],
) -> tuple[list[Fact], list[Dropped]]:
    """Keep the facts their own files support; report the rest.

    A fact survives only if (1) it cites a file that was actually read for
    this repository, (2) its quote, normalised, is in that file's text as
    shown to the model, and (3) its value -- and its quantity, when it gives
    one -- is inside the quote. Each refusal names which of the three failed.
    """
    kept: list[Fact] = []
    dropped: list[Dropped] = []
    haystacks = {path: normalise(text) for path, text in documents.items()}
    seen: set[tuple[str, str, str | None]] = set()
    for item in raw_facts:
        name, value, quote, source = (
            item["field"],
            item["value"],
            item["quote"],
            item["source"],
        )
        label, quantity = item.get("label"), item.get("quantity")
        reason = _refusal(item, haystacks)
        if reason is not None:
            dropped.append(Dropped(repo.full_name, name, value, reason))
            continue
        key = (name, normalise(value), normalise(label) if label else None)
        if key in seen:
            continue  # the same statement twice is one fact, not a drop
        seen.add(key)
        kept.append(
            Fact(
                field=name,
                value=value,
                quote=quote,
                source=source,
                url=repo.blob_url(source),
                label=label,
                quantity=quantity,
            )
        )
    return kept, dropped


# ---------------------------------------------------------------- parsing


def _json(raw: str) -> Any:
    # Fenced JSON tolerated (netlist.py); no import of the agents layer, which
    # imports this module and not the other way round.
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*|\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    raise PriorArtValidationError(
        [f"the answer was not a JSON object (first 120 chars: {text[:120]!r})"]
    )


def _object(raw: str, key: str) -> tuple[dict[str, Any], list[str]]:
    payload = _json(raw)
    if not isinstance(payload, dict):
        raise PriorArtValidationError(
            [f"the answer must be a JSON object, got {type(payload).__name__}"]
        )
    errors: list[str] = []
    if key not in payload:
        errors.append(f"missing top-level {key!r}")
    return payload, errors


def _opt_text(value: Any, where: str, errors: list[str], limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        errors.append(f"{where} must be a string or null, got {value!r}")
        return None
    text = value.strip()
    if len(text) > limit:
        errors.append(f"{where} is {len(text)} characters; the limit is {limit}")
        return None
    return text or None


def parse_queries(raw: str) -> tuple[list[str], list[str]]:
    """``{"queries": [...], "known_repos": [...]}`` -> (queries, repos).

    Queries are plain keywords: a ``:`` qualifier is refused because the one
    that looks useful, ``in:readme``, was measured (2026-09-13) to return
    awesome-lists and course catalogues for "robot arm" instead of arms.
    """
    payload, errors = _object(raw, "queries")
    queries: list[str] = []
    raw_queries = payload.get("queries")
    if raw_queries is not None and not isinstance(raw_queries, list):
        errors.append("'queries' must be a list of strings")
        raw_queries = []
    for i, q in enumerate(raw_queries or []):
        where = f"queries[{i}]"
        if not isinstance(q, str) or not q.strip():
            errors.append(f"{where} must be a non-empty string, got {q!r}")
            continue
        text = " ".join(q.split())
        if len(text) > MAX_QUERY_CHARS or len(text.split()) > MAX_QUERY_WORDS:
            errors.append(
                f"{where} {text!r} is too long (at most {MAX_QUERY_WORDS} words, "
                f"{MAX_QUERY_CHARS} characters)"
            )
        elif ":" in text:
            errors.append(
                f"{where} {text!r} uses a search qualifier; plain keywords only"
            )
        elif text.casefold() not in {x.casefold() for x in queries}:
            queries.append(text)
    if raw_queries is not None and not queries and not errors:
        errors.append("'queries' must name at least one search")
    if len(queries) > MAX_QUERIES:
        errors.append(f"{len(queries)} queries; at most {MAX_QUERIES}")
    repos: list[str] = []
    raw_repos = payload.get("known_repos", [])
    if not isinstance(raw_repos, list):
        errors.append("'known_repos' must be a list of 'owner/name' strings")
        raw_repos = []
    for i, name in enumerate(raw_repos):
        if not isinstance(name, str) or not _REPO_RE.match(name.strip()):
            errors.append(f"known_repos[{i}] {name!r} is not 'owner/name'")
            continue
        if name.strip().casefold() not in {r.casefold() for r in repos}:
            repos.append(name.strip())
    if len(repos) > MAX_KNOWN_REPOS:
        errors.append(f"{len(repos)} known_repos; at most {MAX_KNOWN_REPOS}")
    if errors:
        raise PriorArtValidationError(errors)
    return queries, repos


def _relevance(value: Any, where: str, errors: list[str]) -> float | None:
    # bool is an int; True must not read as a perfect score (intent.py's rule).
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{where}: 'relevance' must be a number 0..1, got {value!r}")
        return None
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        errors.append(f"{where}: 'relevance' must be between 0 and 1, got {value!r}")
        return None
    return number


def parse_shortlist(raw: str, candidates: list[str], limit: int) -> dict[str, float]:
    """``{"shortlist": [{"repo", "relevance"}]}`` -> {repo: relevance}.

    Every named repo must be one of ``candidates`` (a model cannot shortlist
    a project it was not shown), at most ``limit`` of them, no duplicates.
    """
    payload, errors = _object(raw, "shortlist")
    known = {c.casefold(): c for c in candidates}
    out: dict[str, float] = {}
    items = payload.get("shortlist")
    if items is not None and not isinstance(items, list):
        errors.append("'shortlist' must be a list")
        items = []
    for i, item in enumerate(items or []):
        where = f"shortlist[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        name = item.get("repo")
        canonical = known.get(name.casefold()) if isinstance(name, str) else None
        if canonical is None:
            errors.append(f"{where}: {name!r} is not one of the candidates listed")
            continue
        if canonical in out:
            errors.append(f"{where}: {canonical} is listed twice")
            continue
        relevance = _relevance(item.get("relevance"), where, errors)
        if relevance is not None:
            out[canonical] = relevance
    if len(out) > limit:
        errors.append(f"{len(out)} repos shortlisted; at most {limit}")
    if errors:
        raise PriorArtValidationError(errors)
    return out


def parse_extraction(
    raw: str, candidates: list[str]
) -> dict[str, tuple[float, str | None, list[dict[str, Any]]]]:
    """The extraction answer, structurally validated, every failure batched.

    Returns ``{repo: (relevance, why, raw_facts)}`` for exactly the
    candidates given. Structure is validated here; whether a fact is *true to
    its file* is :func:`check_citations`' job, and a fact failing that is
    dropped, not repaired -- a repair prompt would only teach the model which
    quote to fabricate.
    """
    payload, errors = _object(raw, "projects")
    known = {c.casefold(): c for c in candidates}
    out: dict[str, tuple[float, str | None, list[dict[str, Any]]]] = {}
    items = payload.get("projects")
    if items is not None and not isinstance(items, list):
        errors.append("'projects' must be a list")
        items = []
    for i, item in enumerate(items or []):
        where = f"projects[{i}]"
        if not isinstance(item, dict):
            errors.append(f"{where} must be an object")
            continue
        name = item.get("repo")
        canonical = known.get(name.casefold()) if isinstance(name, str) else None
        if canonical is None:
            errors.append(f"{where}: {name!r} is not one of the projects given")
            continue
        if canonical in out:
            errors.append(f"{where}: {canonical} appears twice")
            continue
        where = f"{canonical}"
        relevance = _relevance(item.get("relevance"), where, errors)
        why = _opt_text(item.get("why"), f"{where}.why", errors, MAX_WHY_CHARS)
        facts_raw = item.get("facts", [])
        if not isinstance(facts_raw, list):
            errors.append(f"{where}.facts must be a list")
            facts_raw = []
        if len(facts_raw) > MAX_FACTS_PER_PROJECT:
            errors.append(
                f"{where} has {len(facts_raw)} facts; at most {MAX_FACTS_PER_PROJECT}"
            )
            facts_raw = []
        facts: list[dict[str, Any]] = []
        for j, fact in enumerate(facts_raw):
            parsed = _parse_fact(fact, f"{where}.facts[{j}]", errors)
            if parsed is not None:
                facts.append(parsed)
        if relevance is not None:
            out[canonical] = (relevance, why, facts)
    missing = [c for c in candidates if c not in out]
    if items is not None and missing and not errors:
        errors.append(f"no entry for {', '.join(missing)}; answer for every project")
    if errors:
        raise PriorArtValidationError(errors)
    return out


def _parse_fact(fact: Any, where: str, errors: list[str]) -> dict[str, Any] | None:
    if not isinstance(fact, dict):
        errors.append(f"{where} must be an object")
        return None
    before = len(errors)
    name = fact.get("field")
    if name not in FACT_FIELDS:
        errors.append(f"{where}: field {name!r} is not one of {sorted(FACT_FIELDS)}")
    value = fact.get("value")
    if isinstance(value, bool) or value is None:
        errors.append(
            f"{where}: 'value' must be stated; leave out a fact nobody stated"
        )
        value_text = ""
    elif name in INT_FIELDS:
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if not isinstance(value, int) or not 0 < value < 64:
            errors.append(
                f"{where}: {name} must be a whole number 1..63, got {value!r}"
            )
        value_text = str(value)
    elif isinstance(value, (int, float)):
        value_text = str(value)
    elif isinstance(value, str) and value.strip():
        value_text = value.strip()
        if len(value_text) > MAX_VALUE_CHARS:
            errors.append(f"{where}: value is over {MAX_VALUE_CHARS} characters")
    else:
        errors.append(f"{where}: 'value' must be a non-empty string, got {value!r}")
        value_text = ""
    quote = fact.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        errors.append(f"{where}: 'quote' must be the verbatim text the fact came from")
    elif not MIN_QUOTE_CHARS <= len(quote.strip()) <= MAX_QUOTE_CHARS:
        errors.append(
            f"{where}: quote is {len(quote.strip())} characters; "
            f"{MIN_QUOTE_CHARS}..{MAX_QUOTE_CHARS}"
        )
    source = fact.get("source")
    if not isinstance(source, str) or not source.strip():
        errors.append(f"{where}: 'source' must be the path of the file quoted")
    label = _opt_text(fact.get("label"), f"{where}.label", errors, MAX_VALUE_CHARS)
    if name in LABELLED_FIELDS and label is None:
        errors.append(f"{where}: a {name} needs a 'label' naming what it measures")
    quantity = fact.get("quantity")
    if quantity is not None and (
        isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1
    ):
        errors.append(f"{where}: 'quantity' must be a positive whole number or null")
    elif quantity is not None and name not in QUANTITY_FIELDS:
        errors.append(
            f"{where}: 'quantity' counts parts, so it must be null on a {name}"
        )
    if len(errors) != before:
        return None
    return {
        "field": name,
        "value": value_text,
        "quote": quote.strip(),
        "source": source.strip(),
        "label": label,
        "quantity": quantity,
    }
