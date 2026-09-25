"""The order step's entitlement gate (``service/entitlements.py``).

Offline throughout: every gate is built over a recorded transport, so the
real request construction (URL, method, bearer header) is asserted without a
socket, and the app-level tests drive ``POST /steps/<id>/order`` through the
same scripted model ``test_steps`` uses, with the gate pinned on ``Handler``
the way ``model_factory`` is. The two properties that matter most have a test
each: a dead RevenueCat fails open (the step still runs, and the envelope says
the entitlement was not checked), and no key or URL reaches any message.
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from service import entitlements, steps
from service.app import Handler, make_server
from service.cache import MemoryFactStore
from service.entitlements import (
    BAD_APP_USER_ID,
    ENTITLEMENT,
    HEADER,
    NO_APP_USER_ID,
    Decision,
    EntitlementError,
    EntitlementGate,
    HttpRequest,
    HttpResponse,
    decide,
    is_order_path,
    off_block,
)
from service.tests.test_app import scripted, url

SECRET = "sk_SECRETVALUE9f9f9f9f"
PROJECT = "proj1ab2c3d4"
USER = "0d5f6f4e-2c1e-4c9a-9a1e-7f3b2a1c0d9e"
ISO = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

OFF_BLOCK = {
    "checked": False,
    "reason": "not_configured",
    "detail": (
        "Entitlement not checked: REVENUECAT_SECRET_API_KEY is not set, so the "
        "order step is not gated on this service."
    ),
}


def customer(*entitlement_ids: str, expires_at: int | None = None) -> bytes:
    """A documented ``customer`` object with the given active entitlements."""
    return json.dumps(
        {
            "object": "customer",
            "id": USER,
            "project_id": PROJECT,
            "active_entitlements": {
                "object": "list",
                "items": [
                    {
                        "object": "customer.active_entitlement",
                        "entitlement_id": ident,
                        "expires_at": expires_at,
                    }
                    for ident in entitlement_ids
                ],
                "next_page": None,
                "url": f"/v2/projects/{PROJECT}/customers/{USER}/active_entitlements",
            },
        }
    ).encode()


class Recorded:
    """A transport that answers from a script and remembers every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[HttpRequest] = []

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Clock:
    def __init__(self, at: float = 1_790_000_000.0):
        self.at = at

    def __call__(self) -> float:
        return self.at


def gate_over(*answers, clock: Clock | None = None) -> tuple[EntitlementGate, Recorded]:
    transport = Recorded(*answers)
    gate = EntitlementGate(
        SECRET, PROJECT, transport=transport, now=clock or Clock()
    )
    return gate, transport


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"REVENUECAT_SECRET_API_KEY": SECRET},
        {"REVENUECAT_PROJECT_ID": PROJECT},
        {"REVENUECAT_SECRET_API_KEY": "  ", "REVENUECAT_PROJECT_ID": PROJECT},
    ],
)
def test_from_env_is_none_without_both_variables(env):
    assert EntitlementGate.from_env(env) is None


def test_from_env_builds_a_gate_with_both_variables():
    gate = EntitlementGate.from_env(
        {"REVENUECAT_SECRET_API_KEY": SECRET, "REVENUECAT_PROJECT_ID": PROJECT}
    )
    assert gate is not None
    assert gate.describe() == {
        "enabled": True,
        "project_id": PROJECT,
        "entitlement": "pro",
        "cache_ttl_s": 60.0,
    }
    assert SECRET not in json.dumps(gate.describe())


def test_off_block_names_the_variable_that_is_missing():
    assert off_block({}) == OFF_BLOCK
    assert off_block({"REVENUECAT_PROJECT_ID": PROJECT}) == OFF_BLOCK
    only_project_missing = off_block({"REVENUECAT_SECRET_API_KEY": SECRET})
    assert "REVENUECAT_PROJECT_ID is not set" in only_project_missing["detail"]
    assert only_project_missing["reason"] == "not_configured"


def test_current_resolves_once_from_the_environment(monkeypatch):
    entitlements.reset_for_tests()
    monkeypatch.delenv("REVENUECAT_SECRET_API_KEY", raising=False)
    monkeypatch.delenv("REVENUECAT_PROJECT_ID", raising=False)
    assert entitlements.current() is None
    monkeypatch.setenv("REVENUECAT_SECRET_API_KEY", SECRET)
    monkeypatch.setenv("REVENUECAT_PROJECT_ID", PROJECT)
    # Cached: the environment changing under a running process does not
    # change the gate, the same rule metering.current follows.
    assert entitlements.current() is None
    entitlements.reset_for_tests()
    assert isinstance(entitlements.current(), EntitlementGate)
    entitlements.reset_for_tests()


# --- the three outcomes ------------------------------------------------------


def test_active_when_revenuecat_lists_pro():
    expires = 1_795_000_000_000  # ms, the documented unit
    gate, transport = gate_over(HttpResponse(200, customer("pro", expires_at=expires)))
    verdict = gate.check(USER)

    assert verdict.outcome == "active" and verdict.active is True
    assert verdict.source == "revenuecat" and verdict.entitlement == "pro"
    assert ISO.match(verdict.checked_at)
    assert "until 2026-11-18T11:06:40Z" in verdict.detail
    assert verdict.block() == {
        "checked": True,
        "entitlement": "pro",
        "active": True,
        "checked_at": verdict.checked_at,
    }

    # The documented request, exactly: GET, the v2 customer path, bearer auth.
    [request] = transport.requests
    assert request.method == "GET"
    assert request.url == (
        f"https://api.revenuecat.com/v2/projects/{PROJECT}/customers/{USER}"
    )
    assert request.headers["Authorization"] == f"Bearer {SECRET}"
    assert request.body is None


def test_inactive_when_pro_is_not_among_the_active_entitlements():
    gate, _ = gate_over(HttpResponse(200, customer("other", "trial")))
    verdict = gate.check(USER)
    assert verdict.outcome == "inactive" and verdict.active is False
    assert "no active pro entitlement" in verdict.detail
    assert verdict.block()["checked"] is True and verdict.block()["active"] is False
    refusal = verdict.refusal()
    assert refusal["reason"] == "entitlement_required"
    assert refusal["entitlement"] == ENTITLEMENT
    assert refusal["detail"] == verdict.detail == refusal["error"]
    assert refusal["checked_at"] == verdict.checked_at


def test_inactive_when_no_entitlement_is_active_at_all():
    gate, _ = gate_over(HttpResponse(200, customer()))
    assert gate.check(USER).outcome == "inactive"


def test_an_unknown_customer_is_inactive_not_an_outage():
    body = json.dumps(
        {"type": "resource_missing", "message": "No resource was found"}
    ).encode()
    gate, _ = gate_over(HttpResponse(404, body))
    verdict = gate.check(USER)
    assert verdict.outcome == "inactive" and verdict.active is False
    assert "no customer with this app user id" in verdict.detail


@pytest.mark.parametrize(
    "answer, expected",
    [
        (EntitlementError("network_error", "timed out"), "timed out"),
        (HttpResponse(500, b"<html>upstream</html>"), "HTTP 500"),
        (HttpResponse(503, b""), "HTTP 503"),
        (HttpResponse(429, b'{"type":"rate_limit_error"}'), "rate-limited"),
        (HttpResponse(200, b"not json"), "not JSON"),
        (HttpResponse(200, b'{"object":"customer"}'), "no active_entitlements"),
        (
            HttpResponse(200, b'{"object":"project","id":"proj1ab2c3d4"}'),
            "not a customer object",
        ),
        (HttpResponse(200, b'{"object":"list","items":[]}'), "not a customer object"),
        (HttpResponse(302, b""), "unexpected HTTP 302"),
    ],
    ids=[
        "network",
        "500",
        "503",
        "429",
        "not-json",
        "unshaped",
        "project-object",
        "list-object",
        "redirect",
    ],
)
def test_unavailable_fails_open(answer, expected, capsys):
    gate, _ = gate_over(answer)
    verdict = gate.check(USER)

    assert verdict.outcome == "unavailable" and verdict.unavailable
    assert verdict.active is False
    assert expected in verdict.detail
    assert verdict.detail.startswith("Entitlement not checked:")
    assert verdict.detail.endswith("the order step was not gated for this request.")
    assert verdict.block() == {
        "checked": False,
        "reason": "unavailable",
        "entitlement": "pro",
        "detail": verdict.detail,
        "checked_at": verdict.checked_at,
    }
    # Fail open: the decision runs the step and says why it was not checked.
    decision = decide(gate, USER)
    assert decision.refused is None
    assert decision.block["reason"] == "unavailable"
    assert "entitlements:" in capsys.readouterr().err


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_is_a_configuration_error_in_words(status, capsys):
    gate, _ = gate_over(HttpResponse(status, b'{"type":"authentication_error"}'))
    verdict = gate.check(USER)
    assert verdict.outcome == "unavailable"
    assert "REVENUECAT_SECRET_API_KEY" in verdict.detail
    assert f"HTTP {status}" in verdict.detail
    assert SECRET not in verdict.detail
    assert SECRET not in capsys.readouterr().err
    assert decide(gate, USER).refused is None


# --- cache -------------------------------------------------------------------


def test_cache_hit_avoids_a_second_request():
    clock = Clock()
    gate, transport = gate_over(HttpResponse(200, customer("pro")), clock=clock)

    first = gate.check(USER)
    second = gate.check(USER)
    assert first is second
    assert len(transport.requests) == 1

    # Another id is another customer: its own request.
    other = "5b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
    assert gate.check(other).outcome == "active"
    assert len(transport.requests) == 2

    # Fresh for 60 s, then asked again.
    clock.at += 59.0
    gate.check(USER)
    assert len(transport.requests) == 2
    clock.at += 2.0
    gate.check(USER)
    assert len(transport.requests) == 3


def test_an_outage_is_cached_too_so_a_dead_api_is_not_asked_per_press():
    gate, transport = gate_over(EntitlementError("network_error", "timed out"))
    assert gate.check(USER).outcome == "unavailable"
    assert gate.check(USER).outcome == "unavailable"
    assert len(transport.requests) == 1


# --- allowlist, ids, secrecy -------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.example/v2/projects/p/customers/c",
        "https://api.revenuecat.com.evil.example/v2/projects/p/customers/c",
        "http://api.revenuecat.com/v2/projects/p/customers/c",
    ],
)
def test_host_allowlist_refuses_another_host_at_construction(bad):
    with pytest.raises(EntitlementError) as excinfo:
        HttpRequest(bad, headers={"Authorization": f"Bearer {SECRET}"})
    assert excinfo.value.code == "bad_host"
    assert SECRET not in str(excinfo.value)
    assert bad not in str(excinfo.value)


def test_the_allowlist_is_exact_so_the_real_host_passes():
    request = HttpRequest("https://api.revenuecat.com/v2/projects/p/customers/c")
    assert request.method == "GET"


@pytest.mark.parametrize(
    "bad_id",
    [
        "",
        "$RCAnonymousID:abc",
        "a b",
        "x" * 129,
        "id/with/slash",
        "id?x=1",
        # Pass the ledger's regex and would reach the wire as a dot segment,
        # which a normalising proxy turns into a request for the project
        # object or the customer list: a 200 that would fail open.
        ".",
        "..",
        "...",
    ],
)
def test_a_bad_app_user_id_is_refused_before_any_request(bad_id):
    gate, transport = gate_over(HttpResponse(200, customer("pro")))
    with pytest.raises(EntitlementError) as excinfo:
        gate.check(bad_id)
    assert excinfo.value.code == "bad_app_user_id"
    assert transport.requests == []


def test_no_key_or_url_appears_in_any_message(capsys):
    outcomes = [
        HttpResponse(200, customer("pro")),
        HttpResponse(200, customer()),
        HttpResponse(404, b"{}"),
        HttpResponse(401, b"{}"),
        HttpResponse(429, b"{}"),
        HttpResponse(500, b""),
        HttpResponse(200, b"\xff"),
        EntitlementError("network_error", "timed out"),
    ]
    texts: list[str] = []
    reprs: list[str] = []
    for index, answer in enumerate(outcomes):
        gate, transport = gate_over(answer)
        verdict = gate.check(f"{USER}-{index}")
        texts.append(verdict.detail)
        texts.append(json.dumps(verdict.block()))
        texts.append(json.dumps(verdict.refusal()))
        reprs.extend(repr(r) for r in transport.requests)
    with pytest.raises(EntitlementError) as excinfo:
        gate.check("$RCAnonymousID:abc")
    texts.append(str(excinfo.value))
    texts.append(capsys.readouterr().err)

    for text in texts:
        assert SECRET not in text
        assert "https://" not in text
        assert "/v2/" not in text
        assert "api.revenuecat.com" not in text
    # The request's repr sits in the frame of every transport failure: the
    # bare host may show, the path (it names the customer) and the key never.
    assert reprs
    for text in reprs:
        assert SECRET not in text
        assert "https://" not in text
        assert "/v2/" not in text
        assert "<redacted>" in text


# --- the decision app.py acts on ---------------------------------------------


def test_decide_with_no_gate_is_the_off_block(monkeypatch):
    monkeypatch.delenv("REVENUECAT_SECRET_API_KEY", raising=False)
    monkeypatch.delenv("REVENUECAT_PROJECT_ID", raising=False)
    assert decide(None, "") == Decision(block=OFF_BLOCK)
    assert decide(None, USER) == Decision(block=OFF_BLOCK)


def test_decide_without_an_app_user_id_is_a_402_before_any_request():
    gate, transport = gate_over(HttpResponse(200, customer("pro")))
    decision = decide(gate, "   ", now=Clock())
    assert decision.refused == {
        "error": NO_APP_USER_ID,
        "reason": "entitlement_required",
        "entitlement": "pro",
        "detail": NO_APP_USER_ID,
        "checked_at": "2026-09-21T14:13:20Z",
    }
    assert transport.requests == []


def test_decide_with_a_malformed_app_user_id_is_a_402_in_words():
    gate, transport = gate_over(HttpResponse(200, customer("pro")))
    decision = decide(gate, "$RCAnonymousID:abc")
    assert decision.refused is not None
    assert decision.refused["reason"] == "entitlement_required"
    assert decision.refused["detail"] == BAD_APP_USER_ID
    assert transport.requests == []


def test_decide_inactive_refuses_and_active_carries_the_block():
    gate, _ = gate_over(HttpResponse(200, customer()))
    refused = decide(gate, USER)
    assert refused.refused is not None
    assert refused.refused["reason"] == "entitlement_required"

    gate, _ = gate_over(HttpResponse(200, customer("pro")))
    allowed = decide(gate, USER)
    assert allowed.refused is None
    assert allowed.block["checked"] is True and allowed.block["active"] is True


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/steps/abc/order", True),
        ("/steps/abc/order?x=1", True),
        ("/steps/abc/place", False),
        ("/steps/abc/case", False),
        ("/steps/abc/sourcing", False),
        ("/steps/abc", False),
        ("/steps", False),
        ("/order", False),
        ("/steps/abc/order/extra", False),
    ],
)
def test_is_order_path(path, expected):
    assert is_order_path(path) is expected


# --- through the service -----------------------------------------------------


def fake_probe(url: str) -> str:
    """Offline stand-in for ``probe_pdf``: nothing is a PDF here."""
    return "not_pdf"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    monkeypatch.setenv("SILKSCREEN_KICAD_LIVE_PYTHON", str(tmp_path / "missing-python"))
    # No real KiCad in the order step; the export is test_kicad_cli.py's.
    monkeypatch.setenv("KICAD_CLI", str(tmp_path / "missing-kicad-cli"))
    monkeypatch.delenv("REVENUECAT_SECRET_API_KEY", raising=False)
    monkeypatch.delenv("REVENUECAT_PROJECT_ID", raising=False)
    monkeypatch.setattr(steps, "PROBE", fake_probe)
    entitlements.reset_for_tests()
    steps.reset_sessions()
    monkeypatch.setattr(Handler, "model_factory", staticmethod(scripted))
    monkeypatch.setattr(Handler, "store", MemoryFactStore())
    srv = make_server(port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    steps.reset_sessions()
    entitlements.reset_for_tests()


def post(srv, path, payload, headers=None):
    req = urllib.request.Request(
        url(srv, path),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def routed_session(srv) -> str:
    """Start, place and route, so ``order`` is the next step."""
    status, body = post(
        srv, "/steps", {"intent": "build me a toy car", "time_limit_s": 5}
    )
    assert status == 200, body
    sid = body["session"]
    for step in ("place", "route"):
        status, body = post(srv, f"/steps/{sid}/{step}", {})
        assert status == 200, body
        assert "entitlement" not in body, step
    return sid


def pin_gate(monkeypatch, *answers) -> Recorded:
    gate, transport = gate_over(*answers)
    monkeypatch.setattr(Handler, "entitlement_gate_factory", staticmethod(lambda: gate))
    return transport


def test_order_envelope_carries_the_off_block_when_nothing_gates_it(server):
    sid = routed_session(server)
    status, body = post(server, f"/steps/{sid}/order", {}, {HEADER: USER})
    assert status == 200, body
    assert body["step"] == "order" and "orderable" in body["order"]
    assert body["entitlement"] == OFF_BLOCK


def test_order_is_402_without_the_header_when_gated(server, monkeypatch):
    transport = pin_gate(monkeypatch, HttpResponse(200, customer("pro")))
    # Decided before the session is even looked up: no session exists here.
    status, body = post(server, "/steps/nosuch/order", {})
    assert status == 402, body
    assert body["reason"] == "entitlement_required"
    assert body["entitlement"] == "pro"
    assert body["detail"] == NO_APP_USER_ID == body["error"]
    assert ISO.match(body["checked_at"])
    assert transport.requests == []


def test_order_is_402_when_the_customer_is_not_entitled(server, monkeypatch):
    transport = pin_gate(monkeypatch, HttpResponse(200, customer("other")))
    status, body = post(server, "/steps/nosuch/order", {}, {HEADER: USER})
    assert status == 402, body
    assert body["reason"] == "entitlement_required"
    assert body["entitlement"] == "pro"
    assert "no active pro entitlement" in body["detail"]
    assert body["error"] == body["detail"]
    assert ISO.match(body["checked_at"])
    [request] = transport.requests
    assert request.url.endswith(f"/customers/{USER}")


def test_other_steps_are_never_gated(server, monkeypatch):
    transport = pin_gate(monkeypatch, HttpResponse(200, customer("other")))
    # An unentitled customer, no header at all: place is refused for its
    # order (404, no such session), never for an entitlement.
    status, body = post(server, "/steps/nosuch/place", {})
    assert status == 404, body
    assert transport.requests == []


def test_order_runs_when_entitled_and_carries_the_block(server, monkeypatch):
    transport = pin_gate(monkeypatch, HttpResponse(200, customer("pro")))
    sid = routed_session(server)
    status, body = post(server, f"/steps/{sid}/order", {}, {HEADER: USER})
    assert status == 200, body
    assert "orderable" in body["order"]
    assert body["entitlement"] == {
        "checked": True,
        "entitlement": "pro",
        "active": True,
        "checked_at": body["entitlement"]["checked_at"],
    }
    assert ISO.match(body["entitlement"]["checked_at"])
    assert len(transport.requests) == 1


def test_order_runs_when_revenuecat_is_down_and_says_so(server, monkeypatch):
    transport = pin_gate(monkeypatch, EntitlementError("network_error", "timed out"))
    sid = routed_session(server)
    status, body = post(server, f"/steps/{sid}/order", {}, {HEADER: USER})
    assert status == 200, body
    assert "orderable" in body["order"]
    block = body["entitlement"]
    assert block["checked"] is False and block["reason"] == "unavailable"
    assert block["entitlement"] == "pro"
    assert "timed out" in block["detail"]
    assert "not gated for this request" in block["detail"]
    assert ISO.match(block["checked_at"])
    assert len(transport.requests) == 1
