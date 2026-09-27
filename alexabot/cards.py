"""The cards the simulated Alexa+ screen shows, built from tool results.

Reviewer finding M7: the MCP client is the agent, so the page gets a card only
because the agent hands the tool's ``structuredContent`` over. The hook in
:mod:`alexabot.agent` does that, and :func:`from_tool` turns it into a card.
It is pure: a tool name and the exact ``structuredContent`` the tool returned
go in, a card comes out, and the page renders nothing the tool did not supply.
An unknown value is **omitted**, never shown as ``0``.

Field names are APL's ``alexa-layouts`` names, read in
alexa/apl-suggester ``src/configs/templates/{detail-image-left,
headline-light,text-list-light}.json`` at ``fb235dd`` (names only; nothing
copied): ``AlexaHeader`` (``headerTitle``, ``headerSubtitle``,
``headerAttributionText``), ``AlexaDetail`` (``imageSource`` as
``image.src``, ``imageCaption`` as ``image.caption``, ``primaryText``,
``secondaryText``, ``button1Text`` and ``button2Text`` as ``buttons``),
``AlexaTextList`` (``listItems[{primaryText, secondaryText, tertiaryText}]``)
and ``AlexaFooter`` (``hintText``). So a card could later be bound into an APL
document. The one deviation: APL's footer says "Try, 'Alexa, ...'" and ours
never says "Alexa" -- this is a simulation, and the page must not pass for one.

:func:`board_svg` draws the routed board with
:func:`silkscreen.audit.render.render_svg`: a second, independent
``.kicad_pcb`` reader that draws copper and wraps each part in
``<g data-ref="U1" class="part">``, which is how a finding highlights the
parts it names. ``service/steps.py`` writes no image of its own.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = [
    "ATTRIBUTION",
    "STATE_LABELS",
    "board_svg",
    "from_tool",
    "highlighted",
]

ATTRIBUTION = "Simulated Alexa+ experience"

STATE_LABELS = {
    "reading": "Planning",
    "questions": "Waiting on you",
    "proposing": "Drafting the schematic",
    "drafted": "Schematic drafted",
    "placing": "Placing parts",
    "routing": "Routing copper",
    "reviewing": "Reviewing",
    "done": "Done",
    "failed": "Stopped",
}
STALLED_LABEL = "Taking longer than usual"

_STEPS = (("plan", "Plan"), ("questions", "Questions"), ("schematic", "Schematic"),
          ("place", "Place"), ("route", "Route"), ("review", "Review"))
#: Which step each state is on; ``drafted`` has finished the schematic and
#: waits on the person before placing.
_STATE_STEP = {"reading": 0, "questions": 1, "proposing": 2, "drafted": 3,
               "placing": 3, "routing": 4, "reviewing": 5, "done": 6}
_FAILED_STEP = {"reading": 0, "proposing": 2, "placing": 3, "routing": 4,
                "reviewing": 5}
_WORKING = frozenset({"reading", "proposing", "placing", "routing", "reviewing"})
_SEVERITY_ORDER = ("blocker", "marginal", "note")
SEVERITY_LABELS = {
    "blocker": "Blocker: will not work as drawn",
    "marginal": "Marginal",
    "note": "Note",
}
_STAGE_WORDS = {
    "reading": "planning the board",
    "proposing": "drafting the schematic",
    "placing": "placing the parts",
    "routing": "routing the copper",
    "reviewing": "reviewing the design",
    "restart": "the service restarted",
}
UNROUTED_SHOWN = 5


def _chip(text: str, utterance: str, tool: str,
          arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"text": text, "utterance": utterance,
            "hint": {"tool": tool, "arguments": dict(arguments or {})}}


def _hint_text(buttons: Sequence[Mapping[str, Any]]) -> str | None:
    if not buttons:
        return None
    words = str(buttons[0]["utterance"]).rstrip(".")
    if len(words) > 1 and words[0].isupper() and not words[1].isupper():
        words = words[0].lower() + words[1:]
    return f"Try “{words}”"


def _mm(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _percent(fraction: float, unrouted: int) -> str:
    """Whole percent, floored while anything is unrouted (the speech rule)."""
    value = fraction * 100
    return f"{round(value) if not unrouted else int(value)}%"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _steps(sc: Mapping[str, Any]) -> list[dict[str, str]]:
    state = sc.get("state")
    answers = sc.get("answers") or []
    if state == "failed":
        stage = (sc.get("failure") or {}).get("stage")
        summary = sc.get("summary") or {}
        if stage not in _FAILED_STEP:
            # A restart does not say where it was; the summary does.
            stage = ("reviewing" if summary.get("routed_fraction") is not None
                     else "routing" if summary.get("board_mm") else
                     "placing" if summary.get("parts") is not None else "reading")
        at = _FAILED_STEP[stage]
        statuses = ["done"] * at + ["failed"] + ["todo"] * (len(_STEPS) - at - 1)
    else:
        at = _STATE_STEP.get(str(state), 0)
        statuses = ["done"] * min(at, len(_STEPS))
        if at < len(_STEPS):
            statuses.append("current" if state in _WORKING or state == "questions"
                            else "todo")
        statuses += ["todo"] * (len(_STEPS) - len(statuses))
    if not answers and statuses[1] == "done":
        statuses[1] = "skipped"
    return [{"key": key, "label": label, "status": status}
            for (key, label), status in zip(_STEPS, statuses, strict=True)]


def _stats(summary: Mapping[str, Any]) -> list[dict[str, str]]:
    stats = []
    parts, nets = summary.get("parts"), summary.get("nets")
    if isinstance(parts, int):
        stats.append({"label": "Parts", "value": str(parts)})
    if isinstance(nets, int):
        stats.append({"label": "Nets", "value": str(nets)})
    size = summary.get("board_mm")
    if isinstance(size, (list, tuple)) and len(size) == 2:
        stats.append({"label": "Board",
                      "value": f"{_mm(size[0])} × {_mm(size[1])} mm"})
    fraction = summary.get("routed_fraction")
    if isinstance(fraction, (int, float)):
        stats.append({"label": "Routed",
                      "value": _percent(fraction, len(summary.get("unrouted") or []))})
    return stats


def _primary(summary: Mapping[str, Any]) -> str | None:
    bits = []
    parts = summary.get("parts")
    if isinstance(parts, int):
        bits.append(_plural(parts, "part"))
    size = summary.get("board_mm")
    if isinstance(size, (list, tuple)) and len(size) == 2:
        bits.append(f"{_mm(size[0])} × {_mm(size[1])} mm")
    fraction = summary.get("routed_fraction")
    unrouted = summary.get("unrouted")
    if isinstance(fraction, (int, float)) and unrouted is not None:
        bits.append("fully routed" if not unrouted else
                    f"{_percent(fraction, len(unrouted))} routed, "
                    f"{_plural(len(unrouted), 'net')} left")
    return " · ".join(bits) or None


def _review(summary: Mapping[str, Any]) -> dict[str, Any]:
    review = summary.get("review") or {}
    status = review.get("status") or "not_run"
    if status == "ok":
        findings = summary.get("findings") or []
        counts = {key: 0 for key in _SEVERITY_ORDER}
        for finding in findings:
            key = str(finding.get("severity", "")).lower()
            counts[key if key in counts else "note"] += 1
        if findings:
            label = " · ".join(
                _plural(counts[key], key) for key in _SEVERITY_ORDER if counts[key]
            )
        elif summary.get("datasheets_read"):
            label = "Nothing flagged against the datasheets it read. Not a sign-off."
        else:
            label = "Nothing flagged. First pass: no datasheets were read."
        return {"status": "ok", "label": label, "findings": len(findings),
                "blockers": counts["blocker"]}
    if status == "failed":
        out = {"status": "failed", "label": "Review failed: nothing is known"}
        if review.get("detail"):
            out["detail"] = str(review["detail"])
        return out
    return {"status": "not_run", "label": "Review not run: nothing is known yet"}


def _board_buttons(sc: Mapping[str, Any], summary: Mapping[str, Any]) -> list:
    sid = sc["session_id"]
    state = sc.get("state")
    question = sc.get("question")
    if state == "questions" and question:
        return [
            _chip(f"Use the default: {question['default']}", str(question["default"]),
                  "answer_design_questions",
                  {"session_id": sid,
                   "answers": [{"index": question["index"],
                                "answer": question["default"]}]}),
            _chip("You choose the rest", "You choose", "answer_design_questions",
                  {"session_id": sid, "you_choose": True}),
        ]
    if state == "drafted":
        return [_chip("Place and route it", "Place and route it", "continue_design",
                      {"session_id": sid})]
    if state in _WORKING:
        return [_chip("How's it going?", "How's it going?", "board_status",
                      {"session_id": sid})]
    if state == "done":
        findings = summary.get("findings") or []
        review = (summary.get("review") or {}).get("status")
        buttons = []
        if review == "ok" and findings:
            blocker = any(str(f.get("severity")).lower() == "blocker"
                          for f in findings)
            words = "Explain the first blocker" if blocker else \
                "Explain the first finding"
            buttons.append(_chip(words, words, "explain_finding",
                                 {"session_id": sid, "which": 1}))
        buttons.append(_chip("My boards", "What boards have I made?",
                             "recall_my_boards"))
        return buttons
    if state == "failed":
        return [
            _chip("Start it over", f"Start it over: {sc.get('intent', '')}".strip(),
                  "start_board_design", {"intent": sc.get("intent", "")}),
            _chip("My boards", "What boards have I made?", "recall_my_boards"),
        ]
    return []


def _board_card(
    sc: Mapping[str, Any],
    *,
    images: bool,
    highlight_refs: Sequence[str],
    exists: Callable[[str], bool],
) -> dict[str, Any]:
    sid = str(sc["session_id"])
    state = str(sc.get("state"))
    summary = sc.get("summary") or {}
    label = STATE_LABELS.get(state, state)
    if sc.get("stalled"):
        label = f"{label} · {STALLED_LABEL}"
    card: dict[str, Any] = {
        "id": f"board:{sid}",
        "type": "board",
        "sessionId": sid,
        "state": state,
        "stateLabel": label,
        "headerTitle": "Ada",
        "headerSubtitle": str(sc.get("intent") or ""),
        "headerAttributionText": ATTRIBUTION,
        "badges": ["Scripted practice board"] if sc.get("scripted") else [],
        "steps": _steps(sc),
        "secondaryText": str(sc.get("speech") or ""),
        "stalled": bool(sc.get("stalled")),
        "question": None,
        "answers": [
            {"ask": a["ask"], "answer": a["answer"], "source": a["source"]}
            for a in sc.get("answers") or []
        ],
        "stats": _stats(summary),
        "unrouted": None,
        "review": None,
        "listItems": [],
        "image": None,
        "imageNote": None,
        "files": None,
        "highlightRefs": list(highlight_refs),
        "failure": None,
        "notes": [str(n) for n in summary.get("notes") or []],
        "scripted": bool(sc.get("scripted")),
    }
    primary = _primary(summary)
    if primary:
        card["primaryText"] = primary
    if isinstance(sc.get("elapsed_s"), (int, float)):
        card["elapsedS"] = sc["elapsed_s"]
    question = sc.get("question")
    if isinstance(question, Mapping):
        card["question"] = {"index": question["index"], "ask": question["ask"],
                            "default": question["default"],
                            "remaining": question["remaining"]}
    unrouted = summary.get("unrouted")
    if unrouted is not None:
        card["unrouted"] = [{"net": str(u.get("net", "")),
                             "reason": str(u.get("reason", ""))} for u in unrouted]
        card["unroutedShown"] = UNROUTED_SHOWN
    if state == "done":
        card["review"] = _review(summary)
        if card["review"]["status"] == "ok":
            card["listItems"] = [
                {"number": f["number"], "severity": str(f.get("severity", "")),
                 "primaryText": str(f.get("title", "")),
                 "secondaryText": ", ".join(f.get("refs") or []),
                 "refs": list(f.get("refs") or []),
                 "utterance": f"Explain finding {f['number']}",
                 "hint": {"tool": "explain_finding",
                          "arguments": {"session_id": sid, "which": f["number"]}}}
                for f in summary.get("findings") or []
            ]
    board_path = ((summary.get("files") or {}).get("board")) or None
    if board_path and images and exists(board_path):
        parts = summary.get("parts")
        what = f"{_plural(parts, 'part')}" if isinstance(parts, int) else "its parts"
        card["image"] = {"src": f"/api/boards/{sid}/board.svg",
                         "alt": f"The routed board: {what}",
                         "caption": "Drawn from the routed KiCad board"}
        # The file name only: a server's directory layout (and a home
        # directory's user name) is nothing a listener needs to see.
        card["files"] = {"board": f"/api/boards/{sid}/board.kicad_pcb",
                         "boardPath": Path(str(board_path)).name}
    elif board_path and not images:
        card["imageNote"] = "The board file is on the server that made it."
    elif state not in ("done", "failed"):
        card["imageNote"] = "The board is drawn once it is routed."
    if state == "failed":
        failure = sc.get("failure") or {}
        stage = str(failure.get("stage") or "")
        card["failure"] = {
            "stage": stage,
            "text": f"Stopped while {_STAGE_WORDS.get(stage, 'working on it')}",
            "reason": str(failure.get("reason") or ""),
            "ordered": "Nothing was ordered.",
        }
    card["buttons"] = _board_buttons(sc, summary)
    hint = _hint_text(card["buttons"])
    if hint:
        card["hintText"] = hint
    return card


def _finding_card(sc: Mapping[str, Any]) -> dict[str, Any]:
    sid = str(sc["session_id"])
    which = int(sc.get("which") or 1)
    count = sc.get("count")
    finding = sc.get("finding")
    card: dict[str, Any] = {
        "id": f"finding:{sid}:{which}",
        "type": "finding",
        "sessionId": sid,
        "headerTitle": (f"Finding {which} of {count}" if isinstance(count, int)
                        and count else f"Finding {which}"),
        "headerAttributionText": ATTRIBUTION,
        "reviewStatus": sc.get("review_status"),
        "severity": None,
        "highlightRefs": [],
        "buttons": [],
    }
    if isinstance(finding, Mapping):
        severity = str(finding.get("severity", "")).lower()
        card.update({
            "severity": severity if severity in SEVERITY_LABELS else "note",
            "severityLabel": SEVERITY_LABELS.get(
                severity, str(finding.get("severity") or "Note")),
            "primaryText": str(finding.get("title", "")),
            "secondaryText": str(finding.get("detail", "")),
            "parts": list(finding.get("parts") or []),
            "refs": list(finding.get("refs") or []),
            "citation": finding.get("citation") or None,
            "highlightRefs": list(finding.get("refs") or []),
        })
        if finding.get("suggested_fix"):
            card["suggestedFix"] = str(finding["suggested_fix"])
            card["fixNote"] = "Not applied. Ada never changes a board on its own."
        if isinstance(count, int) and which < count:
            card["buttons"].append(_chip(
                "Next finding", f"Explain finding {which + 1}", "explain_finding",
                {"session_id": sid, "which": which + 1}))
    else:
        # Not run or failed: the tool's own sentence, and nothing to highlight.
        card["primaryText"] = str(sc.get("speech") or "")
    card["buttons"].append(_chip("Back to the board", "How did that board go?",
                                 "board_status", {"session_id": sid}))
    card["hintText"] = _hint_text(card["buttons"])
    return card


def _boards_card(sc: Mapping[str, Any]) -> dict[str, Any]:
    query = sc.get("query")
    boards = sc.get("boards") or []
    total = int(sc.get("total") or 0)
    if query:
        subtitle = f"{len(boards)} of {total} matching “{query}”"
    else:
        subtitle = f"{len(boards)} of {total}"
    items = [
        {"sessionId": b["session_id"], "primaryText": b["intent"],
         # The headline already opens with the state ("done: 3 parts, ...",
         # "failed while ..."): speech.headline writes it that way.
         "secondaryText": str(b["headline"]),
         "createdAt": b["created_at"], "state": b["state"],
         "utterance": "How did that board go?",
         "hint": {"tool": "board_status",
                  "arguments": {"session_id": b["session_id"]}}}
        for b in boards
    ]
    card = {
        "id": "boards", "type": "boards", "headerTitle": "Your boards",
        "headerSubtitle": subtitle, "headerAttributionText": ATTRIBUTION,
        "total": total, "query": query, "listItems": items,
        "secondaryText": str(sc.get("speech") or ""),
        "buttons": [],
    }
    if items:
        card["hintText"] = "Try “how did that board go?”"
    return card


_STATUS_TOOLS = frozenset({"start_board_design", "answer_design_questions",
                           "continue_design", "board_status"})


def from_tool(
    tool: str,
    structured: Mapping[str, Any] | None,
    *,
    images: bool = True,
    highlight_refs: Sequence[str] = (),
    exists: Callable[[str], bool] = os.path.exists,
) -> dict[str, Any] | None:
    """The card for one tool result, or ``None`` when it makes none.

    ``images`` is whether this host can read the board files the result names
    (off when the sim talks to another alexabot); ``highlight_refs`` are the
    refs a finding card last named for this board.
    """
    if not isinstance(structured, Mapping) or not structured:
        return None
    if tool in _STATUS_TOOLS and structured.get("session_id"):
        return _board_card(structured, images=images,
                           highlight_refs=highlight_refs, exists=exists)
    if tool == "explain_finding" and structured.get("session_id"):
        return _finding_card(structured)
    if tool == "recall_my_boards":
        return _boards_card(structured)
    return None


def highlighted(card: Mapping[str, Any], refs: Sequence[str]) -> dict[str, Any]:
    """``card`` (a board card) with ``refs`` highlighted."""
    return {**card, "highlightRefs": list(refs)}


def board_svg(path: str | Path) -> str:
    """The routed board at ``path`` as one self-contained SVG, no legend.

    No review runs here: the card's findings come from the tool. The legend is
    off because it would print "0 blocker" for a drawing that has no findings
    marked -- a quiet zero.
    """
    from silkscreen.audit.effort import profile_for
    from silkscreen.audit.geometry import load_audit_board
    from silkscreen.audit.render import render_svg
    from silkscreen.audit.result import AuditResult

    result = AuditResult(
        board=load_audit_board(path), profile=profile_for("quick"), findings=[],
        skipped_reason="drawn for a card; no review ran here",
    )
    svg = render_svg(result, show_legend=False)
    return svg.replace('aria-label="Reviewed board with findings marked"',
                       'aria-label="The routed board"', 1)
