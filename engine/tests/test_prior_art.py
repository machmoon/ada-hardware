"""Prior art: the IR's checks, the GitHub seam, research end to end, the stage.

Offline throughout. GitHub is a recorded transport replaying responses
captured from the real API on 2026-09-13 (``fixtures/prior_art/``, trimmed:
search items cut to six and to the fields read, trees cut to a few dozen
entries, READMEs and the PAROL6 BOM cut to their first few KB, the
rate-limit body's client IP replaced with a documentation address). The
fixture text is quoted from TheRobotStudio/SO-ARM100 (Apache-2.0) and
Source-Robotics/PAROL6-Desktop-robot-arm (GPL-3.0) for test purposes only.
The model half is a ``ScriptedModel`` keyed on the three step markers.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from silkscreen.agents import ModelError, ScriptedModel
from silkscreen.agents.prior_art import (
    EXTRACT_MARKER,
    PRIOR_ART_MARKER,
    QUERY_MARKER,
    SHORTLIST_MARKER,
    GitHubClient,
    GitHubError,
    GitHubRateLimited,
    HttpRequest,
    HttpResponse,
    ensure_allowed_url,
    fallback_queries,
    research,
)
from silkscreen.agents.stages import design_brief, prior_art_stage
from silkscreen.prior_art import (
    PriorArtResult,
    PriorArtValidationError,
    Repo,
    check_citations,
    license_category,
    mechanical_files,
    normalise,
    onshape_links,
    parse_extraction,
    parse_queries,
    parse_shortlist,
    repo_from_api,
    repo_score,
)

FIXTURES = Path(__file__).parent / "fixtures" / "prior_art"
NOW = datetime(2026, 9, 13, tzinfo=UTC)
SO100 = "TheRobotStudio/SO-ARM100"
PAROL6 = "Source-Robotics/PAROL6-Desktop-robot-arm"
MOVEO = "BCN3D/BCN3D-Moveo"
CACTUS = "cactus-compute/cactus"


def _bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _json(name: str):
    return json.loads(_bytes(name))


def _readme_text(name: str) -> str:
    return base64.b64decode(_json(name)["content"]).decode("utf-8")


NOT_FOUND = HttpResponse(404, _bytes("repo_not_found.json"))


class Recorded:
    """Replays captured GitHub responses by path; records every request."""

    def __init__(self, routes: dict[str, HttpResponse] | None = None):
        api = "https://api.github.com"
        raw = "https://raw.githubusercontent.com"
        self.routes = {
            f"{api}/search/repositories": HttpResponse(
                200, _bytes("search_robot_arm.json")
            ),
            f"{api}/repos/{SO100}": HttpResponse(200, _bytes("repo_SO-ARM100.json")),
            f"{api}/repos/Annin-Robotics/AR4": NOT_FOUND,
            f"{api}/repos/{SO100}/readme": HttpResponse(
                200, _bytes("readme_SO-ARM100.json")
            ),
            f"{api}/repos/{SO100}/git/trees/main": HttpResponse(
                200, _bytes("tree_SO-ARM100.json")
            ),
            f"{api}/repos/{PAROL6}/readme": HttpResponse(
                200, _bytes("readme_PAROL6.json")
            ),
            f"{api}/repos/{PAROL6}/git/trees/main": HttpResponse(
                200, _bytes("tree_PAROL6.json")
            ),
            f"{raw}/{PAROL6}/main/BOM/BOM.md": HttpResponse(
                200, _bytes("bom_PAROL6.md")
            ),
        }
        self.routes.update(routes or {})
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        ensure_allowed_url(request.url)
        self.requests.append(request)
        parts = urlsplit(request.url)
        key = f"{parts.scheme}://{parts.hostname}{parts.path}"
        return self.routes.get(key, NOT_FOUND)


def _rate_limited() -> HttpResponse:
    recorded = _json("rate_limited_search.json")
    return HttpResponse(
        recorded["status"], json.dumps(recorded["body"]).encode(), recorded["headers"]
    )


QUERIES = json.dumps(
    {
        "queries": ["robot arm"],
        "known_repos": [SO100, "Annin-Robotics/AR4"],
    }
)
SHORTLIST = json.dumps(
    {
        "shortlist": [
            {"repo": SO100, "relevance": 0.9},
            {"repo": PAROL6, "relevance": 0.85},
            {"repo": MOVEO, "relevance": 0.7},
        ]
    }
)

SO100_ROW = "STS3215 Servo 7.4V, 1/345 gear (C001)    | 6"
EXTRACT = json.dumps(
    {
        "projects": [
            {
                "repo": SO100,
                "relevance": 0.95,
                "why": "A 3D-printed desktop arm with a full BOM.",
                "facts": [
                    {
                        "field": "actuator",
                        "value": "STS3215 Servo 7.4V, 1/345 gear (C001)",
                        "quantity": 6,
                        # Quoted without the table pipes, as a model does.
                        "quote": "STS3215 Servo 7.4V, 1/345 gear (C001) 6",
                        "source": "README.md",
                        "label": None,
                    },
                    {
                        "field": "motor_driver",
                        "value": "Motor Control Board",
                        "quote": "| Motor Control Board                         | 1 ",
                        "source": "README.md",
                    },
                    {
                        "field": "power_supply",
                        # Invented: the README says nothing about 24V.
                        "value": "24V 10A power supply",
                        "quote": "24V 10A power supply for the follower arm",
                        "source": "README.md",
                    },
                    {
                        "field": "controller",
                        "value": "ESP32",  # not in its (real) quote
                        "quote": "Motor Control Board",
                        "source": "README.md",
                    },
                ],
            },
            {
                "repo": PAROL6,
                "relevance": 0.8,
                "why": "A desktop 6-axis arm with stepper actuators.",
                "facts": [
                    {
                        "field": "actuator",
                        "value": "Nema 17",
                        "quote": "Stepper 1 | Nema 17 | 1 | 16Ncm, 42x42x20mm",
                        "source": "BOM/BOM.md",
                    },
                    {
                        "field": "dof",
                        "value": 6,
                        "quote": "PAROL6 is a high-performance 3D-printed desktop "
                        "robotic arm",
                        "source": "README.md",
                    },
                    {
                        "field": "bom_item",
                        "value": "Gearbox 20:1",
                        "quote": "Gearbox 20:1 | Nema 17 20:1 | 2",
                        "quantity": 2,
                        "source": "BOM/BOM.md",
                    },
                    {
                        "field": "bom_item",
                        "value": "Gearbox 10:1",
                        "quote": "Gearbox 10:1 | Nema 17 10:1 | 1",
                        "source": "BOM/BOM_PDF_Legacy.pdf",  # never read
                    },
                ],
            },
        ]
    }
)


def _model(**overrides: str) -> ScriptedModel:
    by_marker = {
        QUERY_MARKER: QUERIES,
        SHORTLIST_MARKER: SHORTLIST,
        EXTRACT_MARKER: EXTRACT,
    }
    by_marker.update(overrides)
    return ScriptedModel(by_marker=by_marker)


def _repo(**kw) -> Repo:
    base = dict(
        full_name="o/r",
        html_url="https://github.com/o/r",
        description=None,
        stars=100,
        forks=10,
        pushed_at="2026-09-01T00:00:00Z",
        archived=False,
        fork=False,
        default_branch="main",
        license_spdx="MIT",
    )
    base.update(kw)
    return Repo(**base)


# ---------------------------------------------------------------- the IR


def test_license_categories_follow_licenseclassifier_plus_cern_ohl():
    assert license_category("Apache-2.0") == "notice"
    assert license_category("CC0-1.0") == "unencumbered"
    assert license_category("GPL-3.0") == "restricted"
    assert license_category("CC-BY-NC-SA-4.0") == "forbidden"
    assert license_category("CERN-OHL-P-2.0") == "notice"
    assert license_category("CERN-OHL-W-2.0") == "reciprocal"
    assert license_category("CERN-OHL-S-2.0") == "restricted"
    # No licence is no right to reuse, not "permissive".
    assert license_category(None) == "unknown"
    assert license_category("NOASSERTION") == "unknown"


def test_repo_score_rewards_stars_activity_and_permissive_licences():
    good = _repo(stars=5000, license_spdx="Apache-2.0")
    assert repo_score(good, now=NOW) > repo_score(_repo(stars=50), now=NOW)
    assert repo_score(good, now=NOW) > repo_score(
        _repo(stars=5000, license_spdx="GPL-3.0"), now=NOW
    )
    stale = _repo(
        stars=5000, license_spdx="Apache-2.0", pushed_at="2016-01-01T00:00:00Z"
    )
    assert repo_score(good, now=NOW) > repo_score(stale, now=NOW)
    for repo in (good, stale, _repo(stars=0, forks=0, license_spdx=None)):
        assert 0.0 <= repo_score(repo, now=NOW) <= 1.0
    # A missing push date is left out of the mean, not scored as zero.
    assert repo_score(_repo(pushed_at=None), now=NOW) > 0


def test_repo_facts_come_from_the_api_not_the_readme():
    # PAROL6's README badge alt text says "License: MIT"; the API says GPL-3.0.
    items = {i["full_name"]: i for i in _json("search_robot_arm.json")["items"]}
    parol = repo_from_api(items[PAROL6])
    assert parol is not None and parol.license_spdx == "GPL-3.0"
    assert "License: MIT" in _readme_text("readme_PAROL6.json")
    assert parol.blob_url("BOM/BOM.md") == (
        f"https://github.com/{PAROL6}/blob/main/BOM/BOM.md"
    )
    assert repo_from_api({"full_name": "x/y", "html_url": "https://evil/x/y"}) is None


def test_mechanical_files_are_counted_from_the_tree():
    files = mechanical_files(_json("tree_SO-ARM100.json"))
    assert files.listed and not files.truncated
    assert files.counts["step"] >= 10 and files.counts["stl"] >= 3
    assert all(p.lower().endswith(".step") for p in files.examples["step"])
    assert mechanical_files(None).listed is False
    assert mechanical_files({"tree": [], "truncated": True}).truncated is True


def test_onshape_links_are_matched_with_their_file():
    link = "https://cad.onshape.com/documents/0123456789abcdef01234567/w/abc"
    found = onshape_links({"README.md": f"CAD: [onshape]({link}) and more"})
    assert found == ((link, "README.md"),)


def test_normalise_folds_unicode_dashes_and_markdown_tables():
    readme = _readme_text("readme_SO-ARM100.json")
    assert "SO‑101" in readme  # U+2011, as the file spells it
    assert normalise("the SO-101 is") in normalise(readme)
    assert normalise(SO100_ROW.replace("|", "")) in normalise(readme)


def test_citation_filter_keeps_true_facts_and_reports_every_other_one():
    repo = repo_from_api(_json("repo_SO-ARM100.json"))
    readme = _readme_text("readme_SO-ARM100.json")
    raw = json.loads(EXTRACT)["projects"][0]["facts"]
    parsed = parse_extraction(
        json.dumps({"projects": [{"repo": SO100, "relevance": 1, "facts": raw}]}),
        [SO100],
    )[SO100][2]
    kept, dropped = check_citations(repo, parsed, {"README.md": readme})
    assert [(f.field, f.value, f.quantity) for f in kept] == [
        ("actuator", "STS3215 Servo 7.4V, 1/345 gear (C001)", 6),
        ("motor_driver", "Motor Control Board", None),
    ]
    assert kept[0].url == f"https://github.com/{SO100}/blob/main/README.md"
    reasons = {d.value: d.reason for d in dropped}
    assert reasons["24V 10A power supply"] == "its quote is not in README.md"
    assert reasons["ESP32"] == "its value is not in its own quote"


def test_citation_filter_checks_numbers_quantities_and_sources():
    repo = _repo()
    docs = {"README.md": "A six axis arm. Uses 7.6V servos. Qty 4 of the M3 screw."}

    def fact(**kw):
        base = {
            "field": "dof",
            "value": "6",
            "quote": "A six axis arm",
            "source": "README.md",
            "label": None,
            "quantity": None,
        }
        base.update(kw)
        return base

    kept, dropped = check_citations(
        repo,
        [
            fact(),
            fact(),  # a duplicate statement is one fact, not a drop
            fact(quote="Uses 7.6V servos"),  # the 6 in 7.6 is not a six
            fact(
                field="bom_item",
                value="M3 screw",
                quote="Qty 4 of the M3 screw",
                quantity=5,
            ),
            fact(source="docs/elsewhere.md"),
        ],
        docs,
    )
    assert len(kept) == 1
    assert [d.reason for d in dropped] == [
        "the number 6 is not in its own quote",
        "the quantity 5 is not in its own quote",
        "cites 'docs/elsewhere.md', which was not read for this project",
    ]


def test_parse_queries_batches_every_problem():
    with pytest.raises(PriorArtValidationError) as info:
        parse_queries(
            json.dumps(
                {
                    "queries": ["robot arm in:readme", "a b c d e f g h", 7],
                    "known_repos": ["not a repo"],
                }
            )
        )
    assert len(info.value.errors) == 4
    queries, repos = parse_queries(
        "```json\n"
        + json.dumps({"queries": ["robot arm", "Robot  arm"], "known_repos": [SO100]})
        + "\n```"
    )
    assert queries == ["robot arm"] and repos == [SO100]


def test_parse_shortlist_refuses_repos_it_was_not_shown():
    with pytest.raises(PriorArtValidationError) as info:
        parse_shortlist(
            json.dumps(
                {
                    "shortlist": [
                        {"repo": "made/up", "relevance": 0.9},
                        {"repo": SO100, "relevance": True},
                        {"repo": PAROL6, "relevance": 1.5},
                    ]
                }
            ),
            [SO100, PAROL6],
            limit=5,
        )
    assert len(info.value.errors) == 3
    assert parse_shortlist('{"shortlist": []}', [SO100], 5) == {}


def test_parse_extraction_batches_structure_errors():
    bad = {
        "projects": [
            {
                "repo": SO100,
                "relevance": 0.9,
                "facts": [
                    {
                        "field": "colour",
                        "value": "red",
                        "quote": "a red arm",
                        "source": "README.md",
                    },
                    {
                        "field": "payload",
                        "value": None,
                        "quote": "payload unknown",
                        "source": "README.md",
                    },
                    {
                        "field": "joint_range",
                        "value": "±90°",
                        "quote": "joint range ±90°",
                        "source": "README.md",
                    },
                ],
            }
        ]
    }
    with pytest.raises(PriorArtValidationError) as info:
        parse_extraction(json.dumps(bad), [SO100, PAROL6])
    text = "\n".join(info.value.errors)
    assert "'colour'" in text and "must be stated" in text and "label" in text
    # A quantity counts parts; on a measurement it is refused (seen live:
    # payload "1 kg" with quantity 1, reach "52 cm" with quantity 52).
    measured = {
        "projects": [
            {
                "repo": SO100,
                "relevance": 1,
                "facts": [
                    {
                        "field": "reach",
                        "value": "52 cm",
                        "quantity": 52,
                        "quote": "52 cm reach",
                        "source": "README.md",
                    }
                ],
            }
        ]
    }
    with pytest.raises(PriorArtValidationError, match="counts parts"):
        parse_extraction(json.dumps(measured), [SO100])
    with pytest.raises(PriorArtValidationError, match="no entry for"):
        parse_extraction(
            json.dumps({"projects": [{"repo": SO100, "relevance": 1}]}), [SO100, PAROL6]
        )


def test_result_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        PriorArtResult(intent="x", status="empty")


# ---------------------------------------------------------------- the seam


def test_the_allowlist_holds_at_request_construction():
    client = GitHubClient(transport=lambda r: pytest.fail("must not send"))
    for url in (
        "https://api.github.com.evil.example/search",
        "http://api.github.com/search",
        "https://github.com/o/r",
    ):
        with pytest.raises(GitHubError, match="bad_host"):
            client.request(url)


def test_token_goes_to_the_api_host_only():
    client = GitHubClient(transport=Recorded(), token="ghp_secret")
    assert client.request("https://api.github.com/x").headers["Authorization"] == (
        "Bearer ghp_secret"
    )
    assert (
        "Authorization"
        not in client.request(
            "https://raw.githubusercontent.com/o/r/main/BOM.md"
        ).headers
    )
    assert (
        "Authorization"
        not in GitHubClient(Recorded()).request("https://api.github.com/x").headers
    )


def test_a_recorded_rate_limit_says_so_in_words():
    client = GitHubClient(
        transport=Recorded(
            {"https://api.github.com/search/repositories": _rate_limited()}
        )
    )
    with pytest.raises(GitHubRateLimited) as info:
        client.search_repositories("robot arm")
    message = str(info.value)
    assert "search rate limit" in message and "GITHUB_TOKEN" in message
    assert info.value.reset_epoch == 1789338642


def test_a_plain_403_is_not_mistaken_for_a_rate_limit():
    body = json.dumps({"message": "Resource not accessible"}).encode()
    client = GitHubClient(
        transport=Recorded(
            {"https://api.github.com/search/repositories": HttpResponse(403, body)}
        )
    )
    with pytest.raises(GitHubError) as info:
        client.search_repositories("robot arm")
    assert not isinstance(info.value, GitHubRateLimited)
    assert info.value.code == "http_403"


# ---------------------------------------------------------------- research


def test_research_end_to_end_against_recorded_github():
    transport = Recorded()
    model = _model()
    events: list[dict] = []
    result = research(
        "a 6-DOF desktop robotic arm that can pick up a cup",
        model=model,
        transport=transport,
        environ={},
        on_event=events.append,
        now=NOW,
    )
    assert result.status == "found"
    assert result.authenticated is False
    assert result.missing == ["Annin-Robotics/AR4"]
    assert any("Annin-Robotics/AR4" in w for w in result.warnings)

    names = [p.repo.full_name for p in result.projects]
    assert names[:2] == [SO100, PAROL6]
    so100, parol = result.projects[0], result.projects[1]
    assert (
        so100.repo.license_spdx == "Apache-2.0" and parol.repo.license_spdx == "GPL-3.0"
    )
    assert [f.value for f in so100.facts] == [
        "STS3215 Servo 7.4V, 1/345 gear (C001)",
        "Motor Control Board",
    ]
    assert so100.mechanical.counts["step"] >= 10
    assert {f.field for f in parol.facts} == {"actuator", "bom_item"}
    # "PAROL6 is a ... robotic arm" names the project, it does not state six
    # axes; the README never says how many, so the field stays empty.
    assert parol.fact("dof") is None
    assert parol.fact("payload") is None  # nobody stated one
    assert ("BOM/BOM_PDF_Legacy.pdf", "a PDF or spreadsheet; not read") in parol.unread
    assert ("BOM/BOM.md", f"https://github.com/{PAROL6}/blob/main/BOM/BOM.md") in (
        parol.documents
    )

    # Moveo: shortlisted, but its README and tree are not in the recording.
    moveo = next(
        p for p in result.projects + result.considered if p.repo.full_name == MOVEO
    )
    assert "the repository has no README" in moveo.notes
    assert moveo.facts == []

    dropped = {(d.repo, d.value): d.reason for d in result.dropped}
    assert dropped[(SO100, "24V 10A power supply")] == "its quote is not in README.md"
    assert (PAROL6, "Gearbox 10:1") in dropped
    assert dropped[(PAROL6, "6")] == "the number 6 is not in its own quote"
    assert any("dropped" in w for w in result.warnings)

    # Every prompt carries the shared marker; the shortlist saw the pool.
    assert all(PRIOR_ART_MARKER in c["prompt"] for c in model.calls)
    assert len(model.calls) == 3
    shortlist_prompt = next(
        c["prompt"] for c in model.calls if SHORTLIST_MARKER in c["prompt"]
    )
    assert CACTUS in shortlist_prompt and SO100 in shortlist_prompt

    # Only GitHub was addressed, and no credential was sent without a token.
    assert {urlsplit(r.url).hostname for r in transport.requests} == {
        "api.github.com",
        "raw.githubusercontent.com",
    }
    assert all("Authorization" not in r.headers for r in transport.requests)
    search = next(r for r in transport.requests if "/search/" in r.url)
    assert parse_qs(urlsplit(search.url).query)["q"] == ["robot arm"]

    assert [e["step"] for e in events if e["event"] == "prior_art.round"] == []
    assert {e["repo"] for e in events if e["event"] == "prior_art.project"} == {
        SO100,
        PAROL6,
    }

    block = result.brief_text()
    assert "STS3215 Servo 7.4V, 1/345 gear (C001) x6 [README.md]" in block
    assert "24V" not in block and "ESP32" not in block
    assert (
        json.loads(json.dumps(result.as_dict()))["projects"][0]["repo"]["full_name"]
        == SO100
    )


def test_rate_limited_before_anything_is_found_is_not_none_found():
    transport = Recorded(
        {"https://api.github.com/search/repositories": _rate_limited()}
    )
    result = research("a robot arm", model=_model(), transport=transport, environ={})
    assert result.status == "rate_limited"
    assert result.projects == []
    assert any("GITHUB_TOKEN" in w for w in result.warnings)


def test_rate_limited_while_reading_keeps_what_was_found():
    transport = Recorded(
        {f"https://api.github.com/repos/{PAROL6}/readme": _rate_limited()}
    )
    result = research(
        "a robot arm", model=_model(), transport=transport, environ={}, now=NOW
    )
    assert result.status == "found"
    parol = next(p for p in result.projects if p.repo.full_name == PAROL6)
    assert any("rate limit" in n for n in parol.notes) and parol.facts == []
    assert any(w.startswith("reading stopped early") for w in result.warnings)


def test_unreachable_github_is_unavailable_in_words():
    def down(request: HttpRequest) -> HttpResponse:
        raise GitHubError("network_error", "timed out")

    result = research("a robot arm", model=_model(), transport=down, environ={})
    assert result.status == "unavailable"
    assert any("could not be searched" in w for w in result.warnings)


def test_an_empty_shortlist_is_none_found_and_reads_nothing():
    transport = Recorded()
    result = research(
        "a robot arm",
        model=_model(**{SHORTLIST_MARKER: '{"shortlist": []}'}),
        transport=transport,
        environ={},
    )
    assert result.status == "none_found"
    assert not any("/readme" in r.url for r in transport.requests)


def test_unusable_queries_fall_back_to_the_intents_own_words():
    transport = Recorded()
    result = research(
        "a 6-DOF desktop robotic arm that can pick up a cup",
        model=_model(
            **{
                QUERY_MARKER: "not json at all",
                EXTRACT_MARKER: json.dumps(
                    {
                        "projects": [
                            {
                                "repo": PAROL6,
                                "relevance": 0.8,
                                "facts": json.loads(EXTRACT)["projects"][1]["facts"][
                                    :1
                                ],
                            }
                        ]
                    }
                ),
            }
        ),
        transport=transport,
        environ={},
        now=NOW,
    )
    assert (
        result.queries
        == fallback_queries("a 6-DOF desktop robotic arm that can pick up a cup")
        == ["6-DOF desktop robotic arm"]
    )
    assert any("no usable search queries" in w for w in result.warnings)
    # SO-ARM100 was only reachable by recall, so the scripted shortlist that
    # names it is refused twice -- and the most-starred candidates are read
    # unjudged instead, rated by the extraction call.
    assert any("read unjudged" in w for w in result.warnings)
    assert result.status == "found"
    assert [p.repo.full_name for p in result.projects] == [PAROL6]
    assert CACTUS in {p.repo.full_name for p in result.considered}


def test_a_token_from_the_environment_is_used():
    transport = Recorded()
    result = research(
        "a robot arm",
        model=_model(),
        transport=transport,
        environ={"GITHUB_TOKEN": "ghp_x"},
        now=NOW,
    )
    assert result.authenticated
    api = [r for r in transport.requests if "api.github.com" in r.url]
    assert api and all(r.headers["Authorization"] == "Bearer ghp_x" for r in api)


def test_a_model_outage_is_not_wrapped():
    class Down:
        def generate(self, prompt, **kwargs):
            raise ModelError("quota")

    with pytest.raises(ModelError):
        research("a robot arm", model=Down(), transport=Recorded(), environ={})


# ---------------------------------------------------------------- the stage


def test_stage_off_is_silent_and_on_reports_its_outcome(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    events: list[dict] = []
    stages: list[str] = []
    assert (
        prior_art_stage(
            _model(),
            intent="x",
            prior_art=False,
            emit=events.append,
            enter=stages.append,
        )
        is None
    )
    assert events == [] and stages == []

    result = prior_art_stage(
        _model(),
        intent="a robot arm",
        prior_art=True,
        emit=events.append,
        enter=stages.append,
        transport=Recorded(),
    )
    assert stages == ["prior_art"]
    assert events[0] == {"event": "stage.start", "stage": "prior_art"}
    assert events[-1]["event"] == "stage.done" and events[-1]["status"] == "found"
    assert events[-1]["projects"] == len(result.projects)


def test_design_brief_carries_the_prior_art_to_propose():
    found = PriorArtResult(intent="x", status="found")
    assert design_brief(None, None) is None
    assert design_brief(None, found) is None  # nothing found, nothing added
    result = research(
        "a robot arm", model=_model(), transport=Recorded(), environ={}, now=NOW
    )
    brief = design_brief(None, result)
    assert brief.startswith("Prior art") and SO100 in brief


# ---------------------------------------------------------------- the pipeline


# test_agents.py's GOOD_CIRCUIT: a circuit the validator is known to accept.
TINY_CIRCUIT = {
    "devices": {
        "AMS1117-3.3": {"pins": {"GND": "1", "VOUT": "2", "VIN": "3"}},
    },
    "passives": {
        "c_in": {"type": "capacitor", "value": "22uF"},
        "c_out": {"type": "capacitor", "value": "22uF"},
    },
    "nets": {
        "VIN": ["AMS1117-3.3.VIN", "c_in.1"],
        "GND": ["AMS1117-3.3.GND", "c_in.2", "c_out.2"],
        "+3V3": ["AMS1117-3.3.VOUT", "c_out.1"],
    },
}


@pytest.mark.parametrize("engine", ["sdk", "adk"])
def test_generate_pcb_hands_the_prior_art_to_propose(engine, monkeypatch):
    if engine == "adk":
        pytest.importorskip("google.adk")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from silkscreen.agents import generate_pcb

    model = _model()
    model.responses.append(json.dumps(TINY_CIRCUIT))
    result = generate_pcb(
        model,
        "a 6-DOF desktop robotic arm that can pick up a cup",
        review=False,
        route=False,
        time_limit_s=5.0,
        prior_art=True,
        prior_art_transport=Recorded(),
        engine=engine,
    )
    assert result.prior_art is not None and result.prior_art.status == "found"
    propose = [c["prompt"] for c in model.calls if PRIOR_ART_MARKER not in c["prompt"]]
    assert len(propose) == 1
    assert "Prior art" in propose[0] and SO100 in propose[0]
    assert "STS3215 Servo 7.4V" in propose[0]


def test_generate_pcb_without_prior_art_makes_no_github_call(monkeypatch):
    from silkscreen.agents import generate_pcb

    model = ScriptedModel(responses=[json.dumps(TINY_CIRCUIT)])
    result = generate_pcb(
        model,
        "a divider",
        review=False,
        route=False,
        time_limit_s=5.0,
        prior_art_transport=lambda r: pytest.fail("no request when off"),
        engine="sdk",
    )
    assert result.prior_art is None
    assert all("Prior art" not in c["prompt"] for c in model.calls)
