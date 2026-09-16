"""The pipeline stages, as standalone bodies.

Each function is one stage of prompt-to-PCB: read, propose, place, placement
repair, schematic, route, enclosure, sourcing, simulation, and review. They hold
the whole of a stage -- its
model calls, its guards, and the exact events it emits -- so that more than one
driver can run the same stages. The straight line in
:mod:`silkscreen.agents.pipeline` and the ADK workflow in
:mod:`silkscreen.agents.adk` both call these, and therefore emit byte-identical
events; the service and the SPA read those event names, so a driver that grew
its own copy of a stage would silently fork the contract.

``emit`` and ``enter`` come from the driver: ``emit`` publishes one flat event
dict, ``enter`` tells the model wrapper which stage is making its calls. Nothing
here imports :mod:`silkscreen.agents.pipeline` -- that import runs the other way.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Generic, NamedTuple, TypeVar

from ..board import (
    BoardResult,
    build_board,
    footprint_lib_id,
    live_copper,
    route_board,
    write_board,
)
from ..enclosure.board_shape import board_envelope
from ..enclosure.cad import ExportPaths, export_model, export_shape
from ..enclosure.errors import EnclosureError
from ..enclosure.ir import EnclosureSpec
from ..enclosure.kernel import KernelReport
from ..enclosure.snapshot import render_packet
from ..netlist import CircuitSpec
from ..placement.adapter import GeneratedPlacement, repair_generated_board
from ..placement.agent import TextModel
from ..placement.pcb_repair import evaluate
from ..prior_art import PriorArtResult
from ..routing import RouteResult
from ..schematic import build_schematic, write_project, write_schematic
from ..sourcing import SourcingResult, bom_rows
from ..spice.errors import SpiceError
from ..spice.simulators import Simulator
from ..units import NM_PER_MM, to_mm
from ..web_research import PART_FIELDS, ResearchBudget, WebResearchResult
from .datasheet import PartFacts, read_datasheet
from .enclosure import kernel_round, propose_enclosure
from .firecrawl import FirecrawlError
from .mechanism import MechanismResult
from .model import Model, ModelError
from .plan import PlanResult, propose_plan
from .prior_art import GitHubError, Transport, research
from .propose import ProposalAttempt, propose_circuit
from .review import (
    ReviewError,
    ReviewReport,
    ReviewStatus,
    Severity,
    run_review,
)
from .simulate import SimulationResult, simulate_circuit
from .sourcing import propose_sourcing
from .web_research import research_web

__all__ = [
    "research_stage",
    "start_research_stage",
    "Lane",
    "ResearchJob",
    "research_sourcing_context",
    "prior_art_stage",
    "design_brief",
    "plan_stage",
    "read_stage",
    "propose_stage",
    "place_stage",
    "placement_repair_stage",
    "schematic_stage",
    "route_stage",
    "enclosure_stage",
    "start_enclosure_stage",
    "EnclosureJob",
    "sourcing_stage",
    "start_sourcing_stage",
    "SourcingJob",
    "simulate_stage",
    "start_simulation_stage",
    "SimulationJob",
    "SimulationResult",
    "placed_snapshot",
    "review_stage",
    "start_review_stage",
    "ReviewJob",
    "ReviewReport",
    "SchematicArtifacts",
    "NO_ARTIFACTS",
    "EnclosureResult",
    "proposal_fields",
    "MechanismResult",
    "mechanism_stage",
    "start_mechanism_stage",
    "MechanismJob",
]

Emit = Callable[[dict[str, Any]], None]
Enter = Callable[[str], None]

#: How many datasheet reads may be in flight at once. A request naming a whole
#: BOM (a dozen-plus parts) must not fire a dozen simultaneous Gemini calls --
#: that is both a self-inflicted rate-limit hit and, against the live model,
#: real concurrent spend with no backpressure. 4 is generous headroom over the
#: common case (one to three parts) while bounding the worst case.
MAX_CONCURRENT_READS = 4


def _run_coro_blocking(coro):
    """Run ``coro`` to completion from synchronous code, loop or no loop.

    :func:`read_stage` is a plain synchronous function called from two
    contexts: the SDK driver's straight line (no event loop) and the ADK
    node body in :mod:`silkscreen.agents.adk.workflow` (already running
    inside the loop ``adk/runner.py``'s ``_drive`` started with
    ``asyncio.run``). Calling ``asyncio.run`` directly would work in the
    first case and raise "cannot be called from a running event loop" in the
    second, so this mirrors ``adk/runner.py``'s own ``_drive``/
    ``_loop_is_running`` split rather than inventing a second convention:
    with no loop running, drive the coroutine here; with one already
    running, give it a fresh loop on a thread of its own.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    raised: list[BaseException] = []

    def target() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            raised.append(exc)

    thread = threading.Thread(target=target, name="silkscreen-read-gather")
    thread.start()
    thread.join()
    if raised:
        raise raised[0]
    return result[0]


def read_stage(
    agent_model: Model,
    *,
    sheets: dict[str, str] | None,
    preloaded_facts: list[PartFacts] | None,
    emit: Emit,
    enter: Enter,
    unread: list[str] | None = None,
) -> list[PartFacts]:
    """Read every datasheet, returning cached and freshly-read facts together.

    ``unread``, when given, collects one line per datasheet that was asked
    for and could not be read. The ``read.failed`` event says the same thing,
    but ``emit`` is a no-op for every caller that passes no ``on_event`` --
    the CLI included -- and a board designed as though a part were
    undocumented must not be silent about it.

    With no datasheets to read the stage does not run at all and emits nothing,
    rather than reporting an empty pass.

    Uncached parts are read **concurrently**, up to :data:`MAX_CONCURRENT_READS`
    at a time, via ``asyncio.gather`` over a thread per read (each read is a
    blocking download plus a blocking model call, neither of which is async
    here, so ``asyncio.to_thread`` is the bridge). One shared ``agent_model``
    is reused across every concurrent read rather than one client per task:
    ``GeminiModel`` wraps a ``genai.Client`` built once at construction with
    its own ``httpx`` connection pool, the same object every other stage call
    already reuses across sequential requests, and google-genai's client
    issues plain HTTP requests with no shared mutable request state -- there
    is nothing here for concurrent callers to corrupt. ``ScriptedModel``
    (the offline stand-in) is likewise safe: its ``by_marker`` lookup is a
    read-only dict probe, and its ``calls``/``responses`` list mutations are
    single, GIL-atomic list operations, so tests that key responses by a
    per-part marker in the prompt (as every multi-part fixture in this repo
    does) see no cross-talk between concurrent reads.

    Failure isolation follows the same convention :func:`review_stage`'s
    caller applies to review findings and :func:`propose_stage`'s repair loop
    applies to validation errors: one bad part must not sink a batch that
    contains good ones. A part whose read raises (network error, non-PDF
    body, a model that returns no usable pinout) is reported via a
    ``read.failed`` event and simply excluded from the returned facts, so the
    parts that did read still reach ``propose``. Only when *every* requested
    part fails -- there is nothing left for the run to build from -- does the
    stage re-raise (the first failure, since a batch-of-one is the common
    case and a single-part request must keep failing exactly as it did before
    this stage learned to run concurrently).
    """
    facts: list[PartFacts] = list(preloaded_facts or ())
    already = {f.part_number.strip().lower() for f in facts}
    sheets = sheets or {}
    if sheets:
        enter("read")
        emit({"event": "stage.start", "stage": "read"})

    emit_lock = threading.Lock()

    def safe_emit(evt: dict[str, Any]) -> None:
        # Concurrent reads call this from worker threads; the driver's own
        # ``emit`` (a list append, an SSE write, ...) is not guaranteed safe
        # under concurrent callers, so every emission from inside a read is
        # serialised here. The sequential ``read.part`` emissions below need
        # no lock -- they all run on this thread, before any read starts.
        with emit_lock:
            emit(evt)

    to_read: list[tuple[str, str]] = []
    for index, (part_number, url) in enumerate(sheets.items(), start=1):
        # A part supplied both ways is read once; the cached copy wins, since
        # re-reading it is the cost the caller was trying to avoid.
        cached = part_number.strip().lower() in already
        emit(
            {
                "event": "read.part",
                "part": part_number,
                "index": index,
                "total": len(sheets),
                "cached": cached,
            }
        )
        if not cached:
            to_read.append((part_number, url))

    def read_one(part_number: str, url: str) -> PartFacts:
        # The download is the slowest thing in the stage and the only one that
        # can stall on a remote host. Reporting the bytes it landed is what
        # separates "still fetching" from "hung" while someone watches a demo.
        def announce(target: str, **kwargs) -> bytes:
            from .grounding import fetch_pdf

            data = fetch_pdf(target, **kwargs)
            safe_emit(
                {
                    "event": "read.fetch",
                    "part": part_number,
                    "bytes": len(data),
                }
            )
            return data

        return read_datasheet(agent_model, part_number, pdf_url=url, fetch=announce)

    if to_read:

        async def gather_reads() -> list[PartFacts | BaseException]:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_READS)

            async def bound(part_number: str, url: str) -> PartFacts:
                async with semaphore:
                    return await asyncio.to_thread(read_one, part_number, url)

            return await asyncio.gather(
                *(bound(part_number, url) for part_number, url in to_read),
                return_exceptions=True,
            )

        # asyncio.gather preserves the order of its awaitables in the result
        # list regardless of which one finished first, so zipping this back
        # against ``to_read`` keeps ``facts`` in a deterministic, request
        # order -- an order-independent *completion* is fine; an
        # order-independent *result* would not be.
        results = _run_coro_blocking(gather_reads())
        failures: list[BaseException] = []
        for (part_number, _url), result in zip(to_read, results, strict=True):
            if isinstance(result, BaseException):
                failures.append(result)
                emit(
                    {
                        "event": "read.failed",
                        "part": part_number,
                        "error": str(result)[:200],
                    }
                )
                if unread is not None:
                    unread.append(
                        f"{part_number}: the datasheet could not be read "
                        f"({str(result)[:160]}); the board was designed and "
                        f"reviewed without its facts"
                    )
                continue
            facts.append(result)

        if failures and len(failures) == len(to_read):
            raise failures[0]

    if sheets:
        emit(
            {
                "event": "stage.done",
                "stage": "read",
                "parts": len(facts),
                "pins": sum(len(f.pins) for f in facts),
                "requirements": sum(len(f.requirements) for f in facts),
            }
        )
    return facts


def research_stage(
    agent_model: Model,
    *,
    intent: str,
    research: bool,
    emit: Emit,
    enter: Enter,
    transport: Any = None,
    environ: Any = None,
    budget: ResearchBudget | None = None,
) -> WebResearchResult | None:
    """Research the request on the web (Firecrawl), or None when off.

    Opt-in, the :func:`prior_art_stage` rule: model calls and paid Firecrawl
    pages nobody pressed for, so ``research=False`` emits nothing. What it
    found reaches the designer only through :func:`design_brief` and the
    sourcing prompt, as cited facts.

    It never fails the run. With no ``FIRECRAWL_API_KEY`` the result is
    status ``unconfigured`` and a ``research.refused`` event says so in words
    -- not a silent skip, and not a failed stage. A Firecrawl error or a
    ``ValueError`` that escapes becomes status ``unavailable`` after a
    ``research.failed`` event. A ``ModelError`` or a callback exception
    propagates, as from every other stage.
    """
    if not research:
        return None
    enter("research")
    emit({"event": "stage.start", "stage": "research"})
    try:
        result = research_web(
            intent,
            model=agent_model,
            transport=transport,
            environ=environ,
            budget=budget if budget is not None else ResearchBudget(),
            on_event=emit,
        )
    except (FirecrawlError, ValueError) as exc:
        detail = f"web research failed: {exc}"
        emit({"event": "research.failed", "stage": "research", "detail": detail[:200]})
        result = WebResearchResult(intent=intent, status="unavailable", warnings=[detail])
    if result.status == "unconfigured":
        emit(
            {
                "event": "research.refused",
                "stage": "research",
                "detail": result.warnings[0][:200] if result.warnings else "",
            }
        )
    emit(
        {
            "event": "stage.done",
            "stage": "research",
            "status": result.status,
            "queries": len(result.queries),
            "pages": result.pages,
            "findings": len(result.findings),
            "dropped": len(result.dropped),
            "stops": len(result.stops),
            "warnings": len(result.warnings),
        }
    )
    return result


T = TypeVar("T")


class Lane(Generic[T]):
    """One stage in flight on its own thread, or already settled.

    :meth:`result` joins the thread and then either returns what the stage
    returned or re-raises exactly what it raised -- a ``ModelError``, or an
    event callback that hung up -- so a failure in the background carries the
    same meaning at the join as it would have had in line. A lane that was
    never started settles at once with ``default`` and no events: ``None``
    for the optional lanes, the ``SKIPPED`` report for the critic.

    One class for the six background lanes (research, critic, case,
    sourcing, simulation, mechanism). They were six copies of this body until
    2026-09-16; the old names stay as aliases below.
    """

    def __init__(
        self, thread: threading.Thread | None = None, default: T | None = None
    ) -> None:
        self._thread = thread
        self._result: T | None = default
        self._error: BaseException | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def wait(self) -> None:
        """Block until the stage has finished. Raises nothing; idempotent."""
        if self._thread is not None:
            self._thread.join()

    def result(self) -> T | None:
        self.wait()
        if self._error is not None:
            raise self._error
        return self._result

    def _run(self, work: Callable[[], T]) -> None:
        try:
            self._result = work()
        except BaseException as exc:  # noqa: BLE001 -- re-raised at the join
            self._error = exc


class ReviewJob(Lane[ReviewReport]):
    """The critic's lane: never started, it settles to the ``SKIPPED`` report
    :func:`review_stage` would have returned (the review-off no-op)."""

    def __init__(self, thread: threading.Thread | None = None) -> None:
        super().__init__(thread, default=ReviewReport(status=ReviewStatus.SKIPPED))


ResearchJob = Lane
EnclosureJob = Lane
SourcingJob = Lane
SimulationJob = Lane
MechanismJob = Lane


def start_research_stage(
    agent_model: Model,
    *,
    intent: str,
    research: bool,
    emit: Emit,
    enter: Enter,
    transport: Any = None,
    environ: Any = None,
    budget: ResearchBudget | None = None,
    daemon: bool = False,
) -> ResearchJob:
    """Run :func:`research_stage` on a worker thread from the moment the
    intent arrives.

    Both drivers call this first -- before the datasheets are read, before
    prior art and the plan -- and join it right before propose, so the web is
    being read while those stages run and its cited findings are in the brief
    propose designs against. The research budget
    (:class:`~silkscreen.web_research.ResearchBudget`, a wall clock among its
    axes) is what bounds the wait at the join.

    **Model calls overlap.** Unlike the enclosure/sourcing/simulation lanes,
    this lane runs while the driver thread may be reading datasheets or
    planning, so two worker-model calls can be in flight at once. That is the
    point -- research that waited for them would add its whole latency to the
    run -- and it keeps the offline suite deterministic for the reason the
    three background lanes do: every research prompt carries its own step
    marker, so a :class:`~silkscreen.agents.model.ScriptedModel` keyed by
    marker answers it the same way whatever the interleaving.

    ``enter("research")`` is called here, on the calling thread, before the
    thread starts: the event tap takes the first thread to enter a stage as
    the driver's, and a worker that entered first would take that role. With
    ``research`` off nothing is started and nothing is emitted.
    """
    if not research:
        return ResearchJob()
    enter("research")
    job = ResearchJob()

    def work() -> WebResearchResult | None:
        return research_stage(
            agent_model,
            intent=intent,
            research=True,
            emit=emit,
            enter=enter,
            transport=transport,
            environ=environ,
            budget=budget,
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-research", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job


def research_sourcing_context(result: WebResearchResult | None) -> str | None:
    """The part facts web research cited, for the sourcing prompt, or None."""
    if result is None:
        return None
    return result.brief_text(fields=PART_FIELDS)


def prior_art_stage(
    agent_model: Model,
    *,
    intent: str,
    prior_art: bool,
    emit: Emit,
    enter: Enter,
    transport: Transport | None = None,
) -> PriorArtResult | None:
    """Find the open-source projects that already build this, or None when off.

    Opt-in, the `plan_stage` rule: up to three model calls and a handful of
    GitHub requests nobody pressed for, so `prior_art=False` emits nothing.
    It runs before plan and propose, and what it found reaches the designer
    only through :func:`design_brief`, as cited facts.

    It never fails the run on GitHub's account: a rate limit or an outage is
    a status and a warning on the result (`research` never raises for
    those), and anything else GitHub-shaped that escapes is turned into
    status `unavailable` here. A `ModelError` propagates, the plan stage's
    convention.
    """
    if not prior_art:
        return None
    enter("prior_art")
    emit({"event": "stage.start", "stage": "prior_art"})
    try:
        result = research(intent, model=agent_model, transport=transport, on_event=emit)
    except (GitHubError, ValueError) as exc:
        detail = f"prior-art research failed: {exc}"
        emit(
            {"event": "prior_art.failed", "stage": "prior_art", "detail": detail[:200]}
        )
        result = PriorArtResult(intent=intent, status="unavailable", warnings=[detail])
    emit(
        {
            "event": "stage.done",
            "stage": "prior_art",
            "status": result.status,
            "projects": len(result.projects),
            "dropped": len(result.dropped),
            "warnings": len(result.warnings),
        }
    )
    return result


def design_brief(
    plan_result: PlanResult | None,
    prior_art_result: PriorArtResult | None,
    research_result: WebResearchResult | None = None,
) -> str | None:
    """What propose designs against: the plan's brief, the prior art, then
    the cited web research.

    Both drivers call this so they cannot hand propose different text.
    """
    parts = [
        plan_result.plan.brief_text()
        if plan_result is not None and plan_result.plan is not None
        else None,
        prior_art_result.brief_text() if prior_art_result is not None else None,
        research_result.brief_text() if research_result is not None else None,
    ]
    joined = "\n\n".join(p for p in parts if p)
    return joined or None


def plan_stage(
    agent_model: Model,
    *,
    intent: str,
    plan: bool,
    max_repairs: int,
    emit: Emit,
    enter: Enter,
) -> PlanResult | None:
    """Expand a thin intent into a validated brief, or None when off.

    Opt-in, because it is a model call nobody pressed for: `plan=False` skips
    it entirely and emits nothing, the `read_stage` rule for a stage with
    nothing to do.

    It never fails the run. A model that cannot produce a usable plan gives a
    `PlanResult` whose `plan` is None and whose `warnings` say so, and the
    propose stage designs from the bare intent exactly as it did before -- an
    unplanned board is honest, and losing a board because its brief would not
    parse is not a trade worth making.
    """
    if not plan:
        return None
    enter("plan")
    emit({"event": "stage.start", "stage": "plan"})
    try:
        result = propose_plan(
            agent_model,
            intent,
            max_repairs=max_repairs,
            on_event=emit,
        )
    except ValueError as exc:
        # The sourcing_stage rule: a bad answer is a warning, not a dead run.
        detail = f"the plan could not be made: {exc}"
        emit({"event": "plan.failed", "stage": "plan", "detail": detail[:200]})
        emit({"event": "stage.done", "stage": "plan", "planned": False})
        return PlanResult(plan=None, warnings=[detail])
    emit(
        {
            "event": "stage.done",
            "stage": "plan",
            "planned": result.plan is not None,
            "warnings": len(result.warnings),
        }
    )
    return result


def propose_stage(
    agent_model: Model,
    *,
    intent: str,
    facts: list[PartFacts],
    max_repairs: int,
    emit: Emit,
    enter: Enter,
    propose_on_event: Emit | None,
    brief: str | None = None,
) -> tuple[CircuitSpec, list[ProposalAttempt]]:
    """Ask for a circuit and repair it until it validates.

    ``propose_on_event`` is handed to :func:`propose_circuit` unchanged, so the
    repair loop's own ``propose.round`` events reach the same stream -- or none
    at all when the driver has no listener.
    """
    enter("propose")
    emit({"event": "stage.start", "stage": "propose"})
    spec, attempts = propose_circuit(
        agent_model,
        intent,
        facts=facts,
        brief=brief,
        max_repairs=max_repairs,
        on_event=propose_on_event,
    )
    emit(
        {
            "event": "stage.done",
            "stage": "propose",
            "parts": spec.part_count(),
            "nets": spec.net_count(),
            "repair_rounds": max(0, len(attempts) - 1),
        }
    )
    return spec, attempts


def place_stage(
    spec: CircuitSpec,
    *,
    time_limit_s: float | None,
    emit: Emit,
    enter: Enter,
) -> BoardResult:
    """Solve placement for the accepted circuit. The only model-free stage."""
    enter("place")
    emit(
        {
            "event": "stage.start",
            "stage": "place",
            "time_limit_s": (
                None if time_limit_s is None else float(time_limit_s)
            ),
        }
    )
    board = build_board(spec, time_limit_s=time_limit_s)
    width_mm, height_mm = board.size_mm
    emit(
        {
            "event": "stage.done",
            "stage": "place",
            "solver_status": board.solver_status,
            "board_mm": [width_mm, height_mm],
            "wirelength_mm": (
                None if board.wirelength_nm is None else board.wirelength_nm / NM_PER_MM
            ),
            "warnings": len(board.warnings),
        }
    )
    return board


def placement_repair_stage(
    board: BoardResult,
    *,
    profile: str | None,
    policy: str,
    feedback: dict[str, Any] | None,
    model: TextModel | None,
    fallback_model: TextModel | None,
    max_turns: int,
    emit: Emit,
    enter: Enter,
) -> GeneratedPlacement | None:
    """Verifier-gate a generated placement before schematic emission and routing."""
    if not profile:
        return None
    enter("placement_repair")
    emit(
        {
            "event": "stage.start",
            "stage": "placement_repair",
            "profile": profile,
            "policy": policy,
        }
    )
    result = repair_generated_board(
        board,
        profile=profile,
        policy=policy,
        feedback=feedback,
        model=model,
        fallback_model=fallback_model,
        max_turns=max_turns,
    )
    before = result.run.start
    after = result.run.board
    before_score = evaluate(before, result.run.profile)
    after_score = evaluate(after, result.run.profile)
    emit(
        {
            "event": "stage.done",
            "stage": "placement_repair",
            "profile": profile,
            "policy": result.run.policy,
            "requested_policy": result.requested_policy,
            "completed": result.run.completed,
            "applied": result.applied,
            "moves": sum(len(step.accepted) for step in result.run.steps),
            "hard_before": before_score.hard,
            "hard_after": after_score.hard,
            "policy_fallback": result.policy_fallback,
        }
    )
    return result


class SchematicArtifacts(NamedTuple):
    """The files the schematic stage wrote, or three Nones when it did not run."""

    schematic_path: Path | None = None
    project_path: Path | None = None
    placed_board_path: Path | None = None


#: What the schematic stage returns when it does not run, and the default a
#: driver passes when it has nothing to report. A module-level singleton
#: because it is immutable and shared, and because a NamedTuple constructed in
#: an argument default is the mutable-default footgun's shape even when it is
#: not one.
NO_ARTIFACTS = SchematicArtifacts()


def schematic_stage(
    spec: CircuitSpec,
    board: BoardResult,
    *,
    output: str | Path | None,
    emit_stages: bool,
    emit: Emit,
    enter: Enter,
    title: str | None = None,
) -> SchematicArtifacts:
    """Draw the sheet and leave every stage on disk, not just the last one.

    The only stage body that writes files. The rest hand their results back and
    let the driver's tail decide; this one cannot, because the three artifacts
    it produces have no other owner and both drivers must produce them
    identically. With no ``output`` there is nowhere to put them, so the stage
    does not run and emits nothing -- the same convention ``read_stage`` follows
    with no datasheets.

    The schematic goes out before any copper exists, because it is the artifact
    a person reads first and it does not depend on placement succeeding well.
    """
    out_path = Path(output) if output is not None else None
    if out_path is None or not emit_stages:
        return NO_ARTIFACTS

    enter("schematic")
    emit({"event": "stage.start", "stage": "schematic"})
    stem = out_path.stem
    sheet = build_schematic(
        spec,
        footprints={p.ref: footprint_lib_id(p) for p in board.parts},
    )
    board.warnings.extend(sheet.warnings)
    artifacts = SchematicArtifacts(
        schematic_path=write_schematic(
            sheet,
            out_path.with_name(f"{stem}.kicad_sch"),
            project_name=stem,
            # The title block names the request, and carries the real date:
            # a sheet dated to the emitter's fixed default would be a lie.
            title=title,
            today=datetime.date.today(),
        ),
        project_path=write_project(
            out_path.with_name(f"{stem}.kicad_pro"), project_name=stem, spec=spec
        ),
        placed_board_path=write_board(
            board, out_path.with_name(f"{stem}.placed.kicad_pcb")
        ),
    )
    emit(
        {
            "event": "stage.done",
            "stage": "schematic",
            "symbols": len(sheet.symbols),
            "warnings": len(sheet.warnings),
        }
    )
    return artifacts


def route_stage(
    board: BoardResult,
    *,
    route: bool,
    emit: Emit,
    enter: Enter,
    live: LiveCopper | None = None,
) -> RouteResult | None:
    """Lay copper on the placed board. Skipped -- and silent -- when off.

    Mutates ``board`` in place, so the ``.kicad_pcb`` the driver's tail writes
    carries the tracks. Nets the router could not finish are named in the
    result and in ``board.unrouted_nets``; a caller reporting the board as
    routed without reading them is the failure the router exists to avoid.

    ``live`` is the seam for showing the copper in an open editor *while*
    the router runs (the desktop's KiCad bridge). With it set, every net the
    router commits or lifts is announced twice: as a ``route.net`` event on
    the normal stream (net, action, counts -- never the copper itself, the
    stream stays small) and as a call to ``live(action, net, copper)`` with
    the copper in KiCad's own frame from :func:`silkscreen.board.live_copper`.
    Without it nothing is announced and the event stream is exactly what it
    was, which the offline tests pin.
    """
    if not route:
        return None
    enter("route")
    emit({"event": "stage.start", "stage": "route"})
    if live is None:
        result = route_board(board)
    else:

        def on_net(action: str, net: str, tracks, vias) -> None:
            emit(
                {
                    "event": "route.net",
                    "stage": "route",
                    "net": net,
                    "action": action,
                    "tracks": len(tracks),
                    "vias": len(vias),
                }
            )
            live(action, net, live_copper(board, tracks, vias))

        result = route_board(board, on_net=on_net)
    emit(
        {
            "event": "stage.done",
            "stage": "route",
            "tracks": len(result.tracks),
            "vias": len(result.vias),
            "routed_nets": len(result.routed),
            "unrouted_nets": len(result.unrouted),
            "copper_mm": round(result.routed_length_nm / NM_PER_MM, 3),
        }
    )
    return result


#: ``live(name, seq, path)`` -- one finished case solid (``board``, ``base``
#: or ``lid``) written as STEP at ``path``, in build order.
LiveCase = Callable[[str, int, Path], None]

#: ``live(action, net, copper)`` -- the copper dict from
#: :func:`silkscreen.board.live_copper`; ``action`` is ``"committed"`` or
#: ``"lifted"``.
LiveCopper = Callable[[str, str, dict[str, Any]], None]


class EnclosureResult(NamedTuple):
    """What the enclosure stage produced (docs/ai-cad-plan.md v3).

    There is one engine -- the build123d/OCCT kernel -- so there is no
    ``engine`` field to read and no ``.scad`` to ship: the v1 OpenSCAD
    emitter, its fit receipt and its rendered flag were removed on
    2026-09-08. ``step_text`` is the STEP (ISO 10303-21) assembly as text,
    which is ASCII and self-contained and therefore rides the one-shot
    ``/generate`` JSON the way the ``.scad`` used to.
    """

    spec: EnclosureSpec
    step_text: str
    repair_rounds: int
    #: The kernel's clause-by-clause receipt -- the only receipt.
    kernel: KernelReport | None = None
    #: The STEP/STL files :func:`~silkscreen.enclosure.cad.export_model`
    #: wrote, or None when nothing durable was written (no directory: the
    #: one-shot route exports into a scratch directory and keeps only the
    #: text).
    exports: ExportPaths | None = None
    #: The advisory snapshot PNGs, in :data:`~silkscreen.enclosure.snapshot.VIEWS`
    #: order; empty when they were not rendered (never a failure).
    snapshots: tuple[Path, ...] = ()
    #: The text the model was shown. Never enters an event or the one-shot
    #: response.
    brief: str = ""


def proposal_fields(proposal: Any) -> tuple[Any, int, Any, Any, str]:
    """``(spec, repair_rounds, model, kernel, brief)`` from a proposal.

    :func:`~silkscreen.agents.enclosure.propose_enclosure` returns an
    ``EnclosureProposal`` NamedTuple; this reads it by name so a caller that
    only wants some of the fields does not depend on the tuple's arity.
    """
    return (
        proposal.spec,
        proposal.repair_rounds,
        getattr(proposal, "model", None),
        getattr(proposal, "kernel", None),
        getattr(proposal, "brief", "") or "",
    )


def enclosure_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    enclosure: bool,
    enclosure_style: str,
    rigorous: bool = False,
    output: str | Path | None,
    emit_stages: bool,
    export_dir: str | Path | None = None,
    stem: str = "enclosure",
    emit: Emit,
    enter: Enter,
    live: LiveCase | None = None,
    restyle: bool = False,
    style_model: Model | None = None,
) -> EnclosureResult | None:
    """Propose, verify, and emit a case for the placed board. Opt-in.

    No-ops silently when the run did not ask for an enclosure (the
    ``route_stage`` pattern). A case needs only the outline and the part
    envelopes -- :mod:`~silkscreen.enclosure.board_shape` reads ``Edge.Cuts``
    and footprints and never looks at copper -- so both drivers start this
    stage on a worker thread as soon as placement is done, through
    :func:`start_enclosure_stage`, and join it before review so the critic
    still closes the stream. This body is what that thread runs; called
    directly it is the same stage, in line.

    The stage measures the board by writing it to a scratch file and reading
    it back through :func:`~silkscreen.enclosure.board_shape.board_envelope`
    -- the envelope is derived from ``.kicad_pcb`` text, the same artifact the
    caller receives, not from in-memory state the file might not carry.

    ``rigorous`` selects the proposal loop's temperament (default fast: one
    repair round, a failing kernel clause riding the receipt rather than
    blocking; ``True`` restores the strict verify-and-repair loop -- see
    :func:`~silkscreen.agents.enclosure.propose_enclosure`).

    One engine: the build123d/OCCT kernel (docs/ai-cad-plan.md v3). Files go
    to one directory: ``export_dir`` when given (the steps service passes the
    session's directory), else beside ``output`` when it is set and
    ``emit_stages`` is on (the ``schematic_stage`` filesystem rule --
    ``--board-only`` promises only the routed board). ``stem`` names them:
    ``<stem>.step``, ``<stem>-base.stl``, ``<stem>-lid.stl``, plus the
    advisory snapshot PNGs (plan decision 19); a snapshot failure is a
    warning event, never a failed stage (decision 17).

    With **no** directory nothing durable is written and ``exports`` is
    ``None``, but ``step_text`` is still produced: the model is exported into
    a scratch directory and the STEP read back, so the one-shot
    ``/generate`` JSON still carries a complete case. ``--board-only`` is the
    same case: it declines the stage *files*, and by then the model call and
    the kernel build have already been spent, so withholding the text would
    discard a case that has already been paid for rather than save anything.

    Failure never fails the run (plan decision 5): any
    :class:`~silkscreen.enclosure.errors.EnclosureError` -- an exhausted
    repair budget, or a
    :class:`~silkscreen.enclosure.errors.KernelUnavailable` refusal when the
    ``cad`` extra is not installed -- as well as a ``ValueError`` from
    measuring the board and an ``OSError`` from writing files is caught here,
    surfaced as a visible ``enclosure.failed`` event carrying the message,
    and answered with ``None``; the board is still the product. There is no
    lesser case to fall back to and none is invented. Everything else,
    callback exceptions and
    :class:`~silkscreen.agents.model.ModelError` included, propagates as it
    does from every other stage.
    """
    if not enclosure:
        return None
    enter("enclosure")
    emit({"event": "stage.start", "stage": "enclosure"})
    exports: ExportPaths | None = None
    snapshots: tuple[Path, ...] = ()
    directory: Path | None
    if export_dir is not None:
        directory = Path(export_dir)
    elif output is not None and emit_stages:
        directory = Path(output).parent
    else:
        directory = None
    try:
        with tempfile.TemporaryDirectory(prefix="silkscreen-enclosure-") as tmp:
            measured = write_board(board, Path(tmp) / "board.kicad_pcb")
            envelope = board_envelope(measured)
        # The loop's kernel report is the receipt, build warnings included,
        # so the stage never re-verifies what was already verified. Fast mode
        # (the default) never lets a failing clause block; ``rigorous``
        # restores the strict repair loop.
        # The live show: each finished solid is written as its own STEP under
        # ``<directory>/live`` and announced, so a CAD window can be told to
        # swap it in while the next one is still being built. Only with a
        # directory -- there is nowhere to put a live file otherwise -- and
        # only when asked; the event stream is untouched by default.
        on_stage = None
        if live is not None and directory is not None:
            live_dir = directory / "live"
            live_dir.mkdir(parents=True, exist_ok=True)
            seq = 0

            def on_stage(name: str, shape) -> None:
                nonlocal seq
                seq += 1
                path = export_shape(shape, live_dir / f"{stem}-{seq:02d}-{name}.step")
                emit(
                    {
                        "event": "enclosure.stage",
                        "stage": "enclosure",
                        "name": name,
                        "seq": seq,
                        "file": path.name,
                    }
                )
                live(name, seq, path)

        proposal = propose_enclosure(
            agent_model,
            envelope,
            style_hint=enclosure_style,
            rigorous=rigorous,
            on_event=emit,
            on_stage=on_stage,
        )
        if restyle:
            proposal = _restyled(
                style_model or agent_model, proposal, envelope, enclosure_style, emit
            )
        spec, repair_rounds, model, kernel, brief = proposal_fields(proposal)
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
            exports = export_model(model, directory, stem)
            step_text = exports.step.read_text(encoding="utf-8")
            try:
                snapshots = tuple(render_packet(model, directory))
            except Exception as exc:  # noqa: BLE001 - advisory, never the gate
                emit(
                    {
                        "event": "enclosure.warning",
                        "warning": f"snapshots not rendered: {exc}"[:160],
                    }
                )
        else:
            # The one-shot /generate JSON path: nothing durable is wanted,
            # but the response must still carry a complete case. STEP is
            # ASCII and self-contained, so exporting into a scratch
            # directory and reading it back loses nothing.
            with tempfile.TemporaryDirectory(prefix="silkscreen-step-") as tmp:
                scratch = export_model(model, Path(tmp), stem)
                step_text = scratch.step.read_text(encoding="utf-8")
    except (EnclosureError, ValueError, OSError) as exc:
        emit({"event": "enclosure.failed", "error": str(exc)[:160]})
        return None

    emit(
        {
            "event": "stage.done",
            "stage": "enclosure",
            "cutouts": len(spec.cutouts),
            "lid": spec.lid,
            "wall_mm": round(to_mm(spec.wall_nm), 3),
            "repair_rounds": repair_rounds,
            "kernel_passed": None if kernel is None else bool(kernel.passed),
            "kernel_failed": [] if kernel is None else list(kernel.failed),
            "exports": (
                []
                if exports is None
                else [
                    exports.step.name,
                    exports.base_stl.name,
                    exports.lid_stl.name,
                ]
            ),
        }
    )
    return EnclosureResult(
        spec=spec,
        step_text=step_text,
        repair_rounds=repair_rounds,
        kernel=kernel,
        exports=exports,
        snapshots=snapshots,
        brief=brief,
    )


def placed_snapshot(board: BoardResult) -> BoardResult:
    """A copy of ``board`` that :func:`route_stage` cannot mutate underneath.

    Routing rebinds ``tracks``/``vias``/``unrouted_nets``/``routed_nets`` and
    extends ``warnings`` in place; the schematic stage extends ``warnings``
    too. Every field the router or the sheet touches gets its own container
    here, so a thread measuring the placed board reads geometry that holds
    still. ``parts`` is shared on purpose: nothing after placement moves a
    part, and the placement is exactly what the case is built around.
    """
    return dataclasses.replace(
        board,
        warnings=list(board.warnings),
        tracks=list(board.tracks),
        vias=list(board.vias),
        unrouted_nets=dict(board.unrouted_nets),
        routed_nets=list(board.routed_nets),
    )


def enclosure_edit_stage(
    board: BoardResult,
    previous: EnclosureResult,
    edits: dict[str, Any],
    *,
    rigorous: bool = False,
    export_dir: str | Path,
    stem: str = "enclosure",
    emit: Emit,
    enter: Enter,
    live: LiveCase | None = None,
) -> EnclosureResult:
    """Rebuild the case from ``previous`` with ``edits`` applied. **No model.**

    The direct-edit path of docs/ai-cad-plan.md v5: a person changes a
    number or a choice on a finished case (``wall_mm``, ``lid``, a cutout's
    face -- the spec's own vocabulary, see
    :func:`~silkscreen.enclosure.ir.apply_edits`) and the kernel builds and
    verifies the result through :func:`~silkscreen.agents.enclosure.kernel_round`,
    the same function the model's proposals go through. Bounds and
    cross-checks hold exactly as they do for the model, and every failure is
    one batched :class:`~silkscreen.enclosure.errors.EnclosureValidationError`.

    Unlike :func:`enclosure_stage` this raises rather than answering None: an
    edit that does not build is the person's to fix, and the previous case is
    still on disk untouched until the export below succeeds. The kernel
    report is attached whether or not it passed; ``rigorous`` only decides
    whether a failing report is an error (there is no repair loop -- the
    repairer here is the person). ``repair_rounds`` is 0 and ``brief`` names
    the edits, so a receipt can say the case was edited by hand rather than
    designed.
    """
    from ..enclosure.errors import EnclosureValidationError
    from ..enclosure.ir import apply_edits, spec_to_dict

    enter("enclosure")
    emit({"event": "stage.start", "stage": "enclosure", "edit": True})
    spec = apply_edits(previous.spec, edits)
    directory = Path(export_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="silkscreen-enclosure-") as tmp:
        measured = write_board(board, Path(tmp) / "board.kicad_pcb")
        envelope = board_envelope(measured)
    on_stage = None
    if live is not None:
        live_dir = directory / "live"
        live_dir.mkdir(parents=True, exist_ok=True)
        seq = 0

        def on_stage(name: str, shape) -> None:
            nonlocal seq
            seq += 1
            path = export_shape(shape, live_dir / f"{stem}-{seq:02d}-{name}.step")
            emit(
                {
                    "event": "enclosure.stage",
                    "stage": "enclosure",
                    "name": name,
                    "seq": seq,
                    "file": path.name,
                }
            )
            live(name, seq, path)

    model, kernel, errors = kernel_round(
        spec, envelope, rigorous, 0, emit, on_stage=on_stage
    )
    if model is None or errors:
        raise EnclosureValidationError(errors or ["the kernel built nothing"])
    exports = export_model(model, directory, stem)
    step_text = exports.step.read_text(encoding="utf-8")
    snapshots: tuple[Path, ...] = ()
    try:
        snapshots = tuple(render_packet(model, directory))
    except Exception as exc:  # noqa: BLE001 - advisory, never the gate
        emit(
            {
                "event": "enclosure.warning",
                "warning": f"snapshots not rendered: {exc}"[:160],
            }
        )
    before = spec_to_dict(previous.spec)
    changed = sorted(k for k in edits if before.get(k) != edits[k])
    brief = "edited by hand: " + (", ".join(changed) if changed else "no field changed")
    emit(
        {
            "event": "stage.done",
            "stage": "enclosure",
            "edit": True,
            "changed": changed,
            "cutouts": len(spec.cutouts),
            "lid": spec.lid,
            "wall_mm": round(to_mm(spec.wall_nm), 3),
            "repair_rounds": 0,
            "kernel_passed": None if kernel is None else bool(kernel.passed),
            "kernel_failed": [] if kernel is None else list(kernel.failed),
            "exports": [exports.step.name, exports.base_stl.name, exports.lid_stl.name],
        }
    )
    return EnclosureResult(
        spec=spec,
        step_text=step_text,
        repair_rounds=0,
        kernel=kernel,
        exports=exports,
        snapshots=snapshots,
        brief=brief,
    )


def _restyled(model: Model, proposal, envelope, style_hint: str, emit: Emit):
    """The design pass over an accepted case; the plain case when it fails.

    :func:`~silkscreen.agents.enclosure_style.restyle_enclosure` handles every
    script failure itself. A model outage here is caught too, unlike in the
    proposal: the verified case already exists and has been paid for, so an
    unreachable model costs the styling, never the case -- and says so.
    """
    from .enclosure_style import restyle_enclosure

    try:
        outcome = restyle_enclosure(
            model, proposal, envelope, style_hint=style_hint, on_event=emit
        )
    except ModelError as exc:
        emit(
            {
                "event": "enclosure.warning",
                "warning": f"restyle not run, the plain case ships: {exc}"[:160],
            }
        )
        return proposal
    for warning in outcome.warnings:
        emit({"event": "enclosure.warning", "warning": warning[:160]})
    return outcome.proposal


def start_enclosure_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    enclosure: bool,
    enclosure_style: str,
    rigorous: bool = False,
    output: str | Path | None,
    emit_stages: bool,
    export_dir: str | Path | None = None,
    stem: str = "enclosure",
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
    live: LiveCase | None = None,
    restyle: bool = False,
    style_model: Model | None = None,
) -> EnclosureJob:
    """Run :func:`enclosure_stage` on a worker thread from a placed snapshot.

    Called right after placement, so the case is being designed while the
    schematic is drawn and the copper laid, and is ready by the time the run
    -- or the engineer, in step mode -- gets to it. The board handed to the
    thread is :func:`placed_snapshot` of ``board``, taken here, before any
    later stage has touched it.

    ``emit`` and ``enter`` are called from the worker thread. The drivers'
    ``emit`` serialises callbacks and their model wrapper keeps the entered
    stage per thread, so the thread's events carry ``stage: "enclosure"``
    whatever the main thread is doing, and the main thread's stage names are
    never overwritten by it. Events therefore interleave with the schematic
    and route stages in wall-clock order; within the enclosure lane the
    order is the frozen one.

    ``export_dir`` and ``stem`` are handed to :func:`enclosure_stage`
    unchanged, so a background case lands its STEP, STLs and preview in the
    same place an in-line one would.

    With ``enclosure`` off nothing is started and the returned job settles
    to ``None`` at once. ``daemon`` marks the thread so an owner that may
    forget the job (a step session evicted mid-design) never keeps the
    process alive for it; a driver that always joins leaves it off.
    """
    if not enclosure:
        return EnclosureJob()
    snapshot = placed_snapshot(board)
    job = EnclosureJob()

    def work() -> EnclosureResult | None:
        return enclosure_stage(
            agent_model,
            snapshot,
            enclosure=True,
            enclosure_style=enclosure_style,
            rigorous=rigorous,
            output=output,
            emit_stages=emit_stages,
            export_dir=export_dir,
            stem=stem,
            emit=emit,
            enter=enter,
            live=live,
            restyle=restyle,
            style_model=style_model,
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-enclosure", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job


def sourcing_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    emit: Emit,
    enter: Enter,
    probe: Callable[[str], str] | None = None,
    context: str | None = None,
) -> SourcingResult:
    """Source the placed board's parts: MPN proposals, probed datasheets, BOM.

    Needs only the placed parts -- ref, value, land pattern -- so like the
    enclosure it is started on a worker thread as soon as placement is done
    (:func:`start_sourcing_stage`) and collected by the sourcing or order
    step. This body is what that thread runs; called directly it is the same
    stage, in line.

    ``probe`` is the datasheet probe seam of
    :func:`~silkscreen.agents.sourcing.propose_sourcing` (default: the real
    network probe); the service pins it offline in tests. ``context`` is
    handed to it unchanged -- the cited web research's part facts
    (:func:`research_sourcing_context`), or None.

    Failure never fails the run: a ``ValueError`` -- a
    :class:`~silkscreen.sourcing.SourcingValidationError` that escaped the
    repair loop, a probe answering outside the vocabulary -- is caught here,
    surfaced as a visible ``sourcing.failed`` event, and answered with the
    deterministic :func:`~silkscreen.sourcing.bom_rows` (every status
    ``"none"``) carrying one warning that names the failure. The board is
    still the product; an unsourced BOM is still a BOM. Everything else,
    callback exceptions and :class:`~silkscreen.agents.model.ModelError`
    included, propagates as it does from every other stage.
    """
    enter("sourcing")
    emit({"event": "stage.start", "stage": "sourcing"})
    try:
        result = propose_sourcing(
            agent_model, bom_rows(board), probe=probe, on_event=emit, context=context
        )
    except ValueError as exc:
        emit({"event": "sourcing.failed", "error": str(exc)[:160]})
        return SourcingResult(
            bom_rows(board),
            warnings=[f"parts were not sourced: {str(exc)[:200]}"],
        )
    emit(
        {
            "event": "stage.done",
            "stage": "sourcing",
            "parts": len(result.parts),
            "verified": result.verified,
            "proposed": result.proposed,
            "unresolved": result.unresolved,
        }
    )
    return result


def start_sourcing_stage(
    agent_model: Model,
    board: BoardResult,
    *,
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
    probe: Callable[[str], str] | None = None,
    context: str | None = None,
) -> SourcingJob:
    """Run :func:`sourcing_stage` on a worker thread from a placed snapshot.

    Called right after placement, so the parts are being looked up while
    the engineer reviews the board in KiCad, and the BOM is ready by the
    time the run -- or the engineer, in step mode -- gets to the order. The
    board handed to the thread is :func:`placed_snapshot` of ``board``,
    taken here, before any later stage has touched it.

    ``emit`` and ``enter`` are called from the worker thread, under the
    same rules as :func:`start_enclosure_stage`: the drivers' ``emit``
    serialises callbacks and their model wrapper keeps the entered stage
    per thread, so the thread's events carry ``stage: "sourcing"`` whatever
    the main thread is doing. ``daemon`` marks the thread so an owner that
    may forget the job (a step session evicted mid-run) never keeps the
    process alive for it; a driver that always joins leaves it off.
    """
    snapshot = placed_snapshot(board)
    job = SourcingJob()

    def work() -> SourcingResult:
        return sourcing_stage(
            agent_model, snapshot, emit=emit, enter=enter, probe=probe, context=context
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-sourcing", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job


def simulate_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    intent: str = "",
    simulator: Simulator | str | None = None,
    emit: Emit,
    enter: Enter,
) -> SimulationResult:
    """Verify the circuit's behaviour in SPICE against clauses derived from
    the intent. Opt-in; the caller holds the switch (the sourcing rule).

    Needs only the validated spec -- no placement, no copper -- so like the
    enclosure and the BOM it is started on a worker thread as soon as
    placement is done (:func:`start_simulation_stage`) and joined after
    them, before review, so a scripted model answers the three lanes in a
    fixed order. This body is what that thread runs; called directly it is
    the same stage, in line.

    ``simulator`` is the seam of :func:`~silkscreen.agents.simulate.
    simulate_circuit`: ``None`` finds ngspice on this machine, and the suite
    passes a fake so the verdict path runs with no simulator installed.

    Four of the five statuses are decided inside
    :func:`~silkscreen.agents.simulate.simulate_circuit` and reported on
    ``stage.done`` as facts, not failures: ``unsimulatable`` (a part with no
    behaviour, named), ``unavailable`` (no simulator, with the install
    hint), ``no_testbench`` (the model gave nothing usable within the
    budget) and ``ran``. The fifth is this stage's: a
    :class:`~silkscreen.spice.errors.SpiceError` from the simulator -- a
    singular matrix, a timeout, a strict-promoted warning -- or a
    ``ValueError`` that escaped the loop is caught here, surfaced as a
    visible ``simulation.failed`` event, and answered with status
    ``"failed"`` naming it. The board is still the product. Everything
    else, callback exceptions and :class:`~silkscreen.agents.model.
    ModelError` included, propagates as it does from every other stage.

    **A failed clause never fails the run.** It becomes a finding on the
    result (``blocker`` if the model marked the clause critical, else
    ``warning``), kept on ``result.findings`` rather than the critic's list
    because the two have different provenance and the critic's
    :class:`~silkscreen.agents.review.Finding` has no field to say so.
    """
    enter("simulation")
    emit({"event": "stage.start", "stage": "simulation"})
    try:
        result = simulate_circuit(
            agent_model, spec, intent=intent, simulator=simulator, on_event=emit
        )
    except (SpiceError, ValueError) as exc:
        emit({"event": "simulation.failed", "error": str(exc)[:160]})
        result = SimulationResult(
            status="failed",
            detail=f"{type(exc).__name__}: {str(exc)[:200]}",
            warnings=[f"circuit was not simulated: {str(exc)[:200]}"],
        )
    emit(
        {
            "event": "stage.done",
            "stage": "simulation",
            "status": result.status,
            "passed": result.passed,
            "clauses": len(result.clauses),
            "failed": len(result.failed),
            "blockers": len(result.blockers),
            "parts": list(result.parts),
            "warnings": len(result.warnings),
        }
    )
    return result


def start_simulation_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    intent: str = "",
    simulator: Simulator | str | None = None,
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
) -> SimulationJob:
    """Run :func:`simulate_stage` on a worker thread.

    Called right after placement, beside the enclosure and the BOM, so the
    circuit is being verified while the sheet is drawn and the copper laid.
    The spec is immutable from ``propose`` onward, so no snapshot is taken.
    ``emit`` and ``enter`` are called from the worker thread under the same
    rules as :func:`start_enclosure_stage`: the thread's events carry
    ``stage: "simulation"`` whatever the main thread is doing. ``daemon``
    marks the thread so an owner that may forget the job never keeps the
    process alive for it; a driver that always joins leaves it off.
    """
    job = SimulationJob()

    def work() -> SimulationResult:
        return simulate_stage(
            agent_model,
            spec,
            intent=intent,
            simulator=simulator,
            emit=emit,
            enter=enter,
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-simulation", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job


def review_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    facts: list[PartFacts],
    review: bool,
    refute: bool = False,
    emit: Emit,
    enter: Enter,
) -> ReviewReport:
    """Argue against the design. Skipped entirely -- and silently -- if off.

    Returns a :class:`~silkscreen.agents.review.ReviewReport` rather than a
    bare list because the three outcomes -- ran and found nothing, ran and
    could not be read, never ran -- are three different facts and a list
    length can only carry one of them. A :class:`~silkscreen.agents.review.
    ReviewError` is caught here and reported: the board is the product and a
    critic that answered gibberish must not destroy it, which is exactly the
    isolation the sourcing stage applies to its own model failure. What it
    must not do is come back looking clean, so the failure rides the report,
    a ``review.failed`` event, and every consumer that renders a verdict.
    """
    if not review:
        return ReviewReport(status=ReviewStatus.SKIPPED)
    enter("review")
    emit({"event": "stage.start", "stage": "review"})
    try:
        outcome = run_review(agent_model, spec, facts=facts, refute=refute)
    except ReviewError as exc:
        detail = str(exc)
        emit({"event": "review.failed", "stage": "review", "detail": detail[:200]})
        emit(
            {
                "event": "stage.done",
                "stage": "review",
                "findings": 0,
                "blockers": 0,
                "status": ReviewStatus.FAILED.value,
            }
        )
        return ReviewReport(status=ReviewStatus.FAILED, detail=detail)
    for line in outcome.dropped:
        # Said out loud: a critic answer the filter had to throw away is not
        # the same as a critic that found nothing there.
        emit({"event": "review.dropped", "stage": "review", "detail": line[:200]})
    for line in outcome.merged:
        # A merge hides a row the critic wrote, exactly as a drop does, so it
        # is announced on the same terms rather than only counted.
        emit({"event": "review.merged", "stage": "review", "detail": line[:200]})
    emit(
        {
            "event": "stage.done",
            "stage": "review",
            "findings": len(outcome.findings),
            "blockers": sum(
                1 for f in outcome.findings if f.severity is Severity.BLOCKER
            ),
            "dropped": len(outcome.dropped),
            "merged": len(outcome.merged),
            "status": ReviewStatus.OK.value,
        }
    )
    return ReviewReport(
        status=ReviewStatus.OK,
        findings=outcome.findings,
        dropped=outcome.dropped,
        merged=outcome.merged,
    )


def start_review_stage(
    agent_model: Model,
    spec: CircuitSpec,
    *,
    facts: list[PartFacts],
    review: bool,
    refute: bool = False,
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
) -> ReviewJob:
    """Run :func:`review_stage` on a worker thread, starting at the spec.

    The critic reads the validated :class:`~silkscreen.netlist.CircuitSpec`
    and the datasheet facts and nothing else -- not the placement, not the
    schematic, not the copper. It was nonetheless the last thing a run did,
    so its whole latency sat on the tail, after the board was otherwise
    finished, and (with sourcing on) behind the sourcing join as well.
    Started here it overlaps :func:`place_stage`, whose CP-SAT budget is the
    effort level's (5 s by default) and which makes no model call at all, so
    the critic costs nothing the engineer waits for.

    **Determinism.** The driver joins this job before it starts the enclosure,
    sourcing and simulation lanes, so exactly one ``agent_model`` call is ever
    in flight while the critic runs: place and placement repair call the
    worker model not at all (placement repair has its own ``model``/
    ``fallback_model`` seam). The model-call order becomes read, plan,
    propose, review, then the three background lanes -- one fixed sequence, so
    a :class:`~silkscreen.agents.model.ScriptedModel` answers a run the same
    way twice and the SDK and ADK drivers agree.

    With ``review`` off nothing is started, no thread exists and no event is
    emitted; the job settles immediately on the ``SKIPPED`` report.
    ``daemon`` marks the thread so an owner that may abandon the job never
    keeps the process alive for it; a driver that always joins leaves it off.
    """
    if not review:
        return ReviewJob()
    job = ReviewJob()

    def work() -> ReviewReport:
        return review_stage(
            agent_model,
            spec,
            facts=facts,
            review=True,
            refute=refute,
            emit=emit,
            enter=enter,
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-review", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job


def mechanism_stage(
    agent_model: Model,
    intent: str,
    *,
    board_spec: CircuitSpec | None = None,
    prior_art: Any = None,
    output: str | Path | None = None,
    emit_stages: bool = True,
    export_dir: str | Path | None = None,
    stem: str = "mechanism",
    emit: Emit,
    enter: Enter,
) -> MechanismResult:
    """Propose, build, verify and export a mechanism (a printed arm). Opt-in;
    the caller holds the switch (the sourcing and simulation rule).

    Needs the intent, and uses two things the run already has when it has
    them: the validated circuit (``board_spec``) so the arm's actuators are
    ones the board's servo driver can command, and the prior art. It needs
    no placement, so both drivers start it on a worker thread beside the
    other background lanes (:func:`start_mechanism_stage`) and join it last.

    Files follow :func:`enclosure_stage`'s rule: ``export_dir`` when given,
    else beside ``output`` when it is set and ``emit_stages`` is on
    (``mechanism.step`` plus one printed-orientation STL per part), else
    nothing durable -- but the STEP text is still produced from a scratch
    export, so the one-shot ``/generate`` JSON carries a whole arm.

    **Never None.** Every ending is a status on :class:`MechanismResult`, in
    words: ``unavailable`` (no ``cad`` extra; no model call was spent),
    ``failed`` (nothing built within the repair budget, or the export
    raised), ``kernel_failed`` (built and exported; the failing clauses are
    named) and ``passed``. A board/arm interface mismatch rides
    ``warnings``. A :class:`~silkscreen.agents.model.ModelError` or a
    callback exception propagates, as from every other stage. Imports
    lazily, so a run that never asks pays nothing.
    """
    from ..enclosure.errors import KernelUnavailable
    from ..mechanism.cad import export_mechanism
    from ..mechanism.errors import MechanismError
    from .mechanism import MechanismResult, interface_warnings, propose_mechanism

    enter("mechanism")
    emit({"event": "stage.start", "stage": "mechanism"})
    if export_dir is not None:
        directory: Path | None = Path(export_dir)
    elif output is not None and emit_stages:
        directory = Path(output).parent
    else:
        directory = None
    result: MechanismResult
    try:
        proposal = propose_mechanism(
            agent_model, intent, prior_art=prior_art, board_spec=board_spec,
            on_event=emit,
        )
    except KernelUnavailable as exc:
        emit({"event": "mechanism.failed", "error": str(exc)[:160]})
        result = MechanismResult(status="unavailable", detail=str(exc),
                                 warnings=[f"no mechanism was built: {exc}"])
    except (MechanismError, ValueError) as exc:
        emit({"event": "mechanism.failed", "error": str(exc)[:160]})
        result = MechanismResult(
            status="failed", detail=f"{type(exc).__name__}: {str(exc)[:400]}",
            warnings=[f"no mechanism was built: {str(exc)[:200]}"],
        )
    else:
        report = proposal.kernel
        warnings = interface_warnings(proposal.spec, board_spec)
        exports = None
        try:
            if directory is not None:
                exports = export_mechanism(proposal.model, directory, stem)
                step_text = exports.step.read_text(encoding="utf-8")
            else:
                with tempfile.TemporaryDirectory(prefix="silkscreen-mechanism-") as tmp:
                    step_text = export_mechanism(proposal.model, tmp, stem).step.read_text(
                        encoding="utf-8"
                    )
        except (MechanismError, OSError) as exc:
            emit({"event": "mechanism.failed", "error": str(exc)[:160]})
            result = MechanismResult(
                status="failed",
                detail=f"built and verified, but not exported: {str(exc)[:300]}",
                spec=proposal.spec, repair_rounds=proposal.repair_rounds,
                kernel=report, brief=proposal.brief,
                warnings=[*warnings, f"mechanism files were not written: {exc}"],
            )
        else:
            passed = report is not None and report.passed
            detail = (
                "every kernel clause passed" if passed
                else "failing clauses: " + ", ".join(report.failed)
            )
            result = MechanismResult(
                status="passed" if passed else "kernel_failed", detail=detail,
                spec=proposal.spec, step_text=step_text,
                repair_rounds=proposal.repair_rounds, kernel=report,
                exports=exports, brief=proposal.brief, warnings=warnings,
            )
    kernel = result.kernel
    emit({
        "event": "stage.done",
        "stage": "mechanism",
        "status": result.status,
        "joints": 0 if result.spec is None else len(result.spec.joints),
        "repair_rounds": result.repair_rounds,
        "kernel_passed": None if kernel is None else bool(kernel.passed),
        "kernel_failed": [] if kernel is None else list(kernel.failed),
        "warnings": len(result.warnings),
        "exports": (
            [] if result.exports is None
            else [p.name for p in (result.exports.step, *result.exports.stls)]
        ),
    })
    return result


def start_mechanism_stage(
    agent_model: Model,
    intent: str,
    *,
    board_spec: CircuitSpec | None = None,
    prior_art: Any = None,
    output: str | Path | None = None,
    emit_stages: bool = True,
    export_dir: str | Path | None = None,
    stem: str = "mechanism",
    emit: Emit,
    enter: Enter,
    daemon: bool = False,
) -> MechanismJob:
    """Run :func:`mechanism_stage` on a worker thread. The caller holds the
    switch: off means do not call this, and use a bare :class:`MechanismJob`
    (the sourcing rule)."""
    job = MechanismJob()

    def work() -> MechanismResult:
        return mechanism_stage(
            agent_model, intent, board_spec=board_spec, prior_art=prior_art,
            output=output, emit_stages=emit_stages, export_dir=export_dir,
            stem=stem, emit=emit, enter=enter,
        )

    thread = threading.Thread(
        target=job._run, args=(work,), name="silkscreen-mechanism", daemon=daemon
    )
    job._thread = thread
    thread.start()
    return job
