"""Stand-ins shared by the alexabot suite (not a test file itself).

:class:`FakeSteps` answers with the envelope shapes ``service/steps.py``
answers with -- recorded, not invented: the keys are the ones
``steps.start``, ``_propose_and_draw``, ``_place``, ``_route`` and
``_review`` put on the wire -- and records which thread made each call, so
a test can prove no ``steps`` call ever ran on a request thread.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from collections.abc import Callable
from typing import Any

from silkscreen.mcp.http import AccessToken, auth_context_var
from silkscreen.mcp.server import handle

QUESTIONS = [
    {"ask": "How much current should the 3.3 volt rail supply?",
     "default": "up to 500 milliamps"},
    {"ask": "Do you want a power indicator LED?", "default": "no LED"},
]

FINDINGS = [
    {"severity": "note", "title": "No thermal relief on the tab",
     "detail": "Copper area sets the usable current.", "parts": ["AMS1117-3.3"],
     "refs": ["U1"], "citation": "", "suggested_fix": ""},
    {"severity": "blocker", "title": "VOUT has no bulk capacitor",
     "detail": "The regulator needs bulk capacitance on its output to stay "
     "stable. Without it the loop oscillates.",
     "parts": ["AMS1117-3.3", "C2"], "refs": ["U1", "C2"],
     "citation": "AMS1117-3.3 datasheet p.9",
     "suggested_fix": "Add a 22uF tantalum from VOUT to GND."},
    {"severity": "marginal", "title": "Input capacitor is smaller than recommended",
     "detail": "10uF works.", "parts": ["C1"], "refs": ["C1"], "citation": "",
     "suggested_fix": "Raise C1 to 22uF."},
]


class FakeSteps:
    """``service.steps``'s ``start_once``/``advance``, recorded.

    ``gates`` holds an :class:`threading.Event` per call name (``start_once``,
    ``propose``, ``place``, ``route``, ``review``) that the call waits on;
    ``fail`` an exception per call name, raised instead of answering.
    """

    def __init__(
        self,
        *,
        questions: list[dict[str, str]] | None = None,
        plan_ok: bool = True,
        unrouted: dict[str, str] | None = None,
        completion: float = 1.0,
        findings: list[dict[str, Any]] | None = None,
        review_status: str = "ok",
    ) -> None:
        self.questions = QUESTIONS if questions is None else questions
        self.plan_ok = plan_ok
        self.unrouted = unrouted or {}
        self.completion = completion
        self.findings = FINDINGS if findings is None else findings
        self.review_status = review_status
        self.gates: dict[str, threading.Event] = {}
        self.fail: dict[str, BaseException] = {}
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._count = 0

    def _enter(self, name: str, **record: Any) -> None:
        with self._lock:
            self.calls.append(
                {"call": name, "thread": threading.current_thread(), **record}
            )
        gate = self.gates.get(name)
        if gate is not None:
            assert gate.wait(timeout=10), f"the test never opened the {name} gate"
        if name in self.fail:
            raise self.fail[name]

    def names(self) -> list[str]:
        return [c["call"] for c in self.calls]

    def start_once(self, payload, *, model, store, idempotency_key=""):
        self._enter("start_once", payload=dict(payload), key=idempotency_key)
        with self._lock:
            self._count += 1
            session = f"steps{self._count}"
        plan = (
            {"ok": True, "plan": {"questions": list(self.questions)}, "warnings": []}
            if self.plan_ok
            else {"ok": False, "plan": None,
                  "warnings": ["the plan could not be read after one repair"]}
        )
        return {"session": session, "step": "plan", "stage": "planned",
                "files": {}, "plan": plan}

    def advance(self, session_id, step, payload, *, model):
        self._enter(step, session=session_id, payload=dict(payload))
        files = {"schematic": f"/boards/{session_id}/b.kicad_sch",
                 "project": f"/boards/{session_id}/b.kicad_pro"}
        if step == "propose":
            return {"session": session_id, "stage": "proposed", "files": files,
                    "parts": 3, "nets": 3}
        if step == "place":
            return {"session": session_id, "stage": "placed", "files": files,
                    "status": "optimal", "board_mm": [16.4, 8.8]}
        if step == "route":
            files["board"] = f"/boards/{session_id}/b.kicad_pcb"
            return {"session": session_id, "stage": "routed", "files": files,
                    "routing": {"completion": self.completion,
                                "unrouted": dict(self.unrouted)}}
        if step == "review":
            return {"session": session_id, "stage": "routed", "files": files,
                    "findings": list(self.findings),
                    "review": {"status": self.review_status, "detail": None}}
        raise AssertionError(f"unexpected step {step}")


def wait_for(
    predicate: Callable[[], Any], timeout: float = 10.0, interval: float = 0.01
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout} s")


@contextlib.contextmanager
def as_account(account: str, client_id: str = "test"):
    """Run tool calls as ``account``, the way the HTTP transport sets it."""
    token = auth_context_var.set(AccessToken(account, client_id))
    try:
        yield
    finally:
        auth_context_var.reset(token)


def call(toolset, name: str, arguments: dict[str, Any] | None = None, req_id=1):
    """One ``tools/call`` through the real ``handle``; the ``result``."""
    response = handle(
        {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
         "params": {"name": name, "arguments": arguments or {}}},
        toolset=toolset,
    )
    assert "result" in response, response
    return response["result"]


def ok(result: dict[str, Any]) -> dict[str, Any]:
    """The structured content of a successful call, checked for the shape."""
    assert result["isError"] is False, result["content"][0]["text"]
    structured = result["structuredContent"]
    assert result["content"][0]["text"] == structured["speech"]
    assert json.loads(result["content"][1]["text"]) == structured
    return structured


def error(result: dict[str, Any]) -> str:
    assert result["isError"] is True, result
    assert "structuredContent" not in result
    assert len(result["content"]) == 1
    return result["content"][0]["text"]
