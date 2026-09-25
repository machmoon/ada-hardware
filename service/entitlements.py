"""The order step's entitlement gate: RevenueCat API v2, and it fails open.

``Prepare fab order`` is the one optional step that runs purely on press
(``case`` and ``sourcing`` are prefetched at ``place`` by
``service/steps.py::_prefetch_case`` and ``_prefetch_sourcing``), so it is the
one step a paid plan can gate without the model call having already been
spent. The desktop sends ``X-Kaleo-App-User-Id`` on every ``/steps`` request;
``app.py`` reads it for ``order`` only and asks this module whether that
customer holds the ``pro`` entitlement.

The check is one documented call, read from
https://www.revenuecat.com/docs/api-v2/customer and
https://www.revenuecat.com/docs/api-v2 on 2026-09-24::

    GET https://api.revenuecat.com/v2/projects/{project_id}/customers/{customer_id}
    Authorization: Bearer <REVENUECAT_SECRET_API_KEY>

whose ``customer`` object carries ``active_entitlements`` inline::

    {"object": "list",
     "items": [{"object": "customer.active_entitlement",
                "entitlement_id": "pro", "expires_at": <ms epoch or null>}],
     "next_page": null, "url": "..."}

The docs say that property is only available on this endpoint, so there is
no second request. Errors are ``{"type": ..., "message": ..., "retryable":
...}`` with 401/403/404/429/5xx, and the customer domain is limited to about
480 requests a minute, which is why :class:`EntitlementGate` keeps a 60 s
cache per app user id: a strip that presses ``order`` twice must not spend
two of them.

Three outcomes, kept apart because they send a person to different fixes:

* ``active``: RevenueCat lists ``pro`` for this customer. The step runs.
* ``inactive``: it lists no such entitlement, or has never seen the customer
  (404 ``resource_missing``: a purchase that never happened created no
  customer). The step is refused with a 402.
* ``unavailable``: the network failed, the call timed out, RevenueCat
  answered 429 or 5xx, its answer could not be read, or it refused the
  configured key (401/403). **The gate fails open**: the step runs and the
  envelope says in words that the entitlement was not checked. A dead
  RevenueCat must not lock a paid feature on an engineer's own laptop, and a
  mistyped key is an operator's problem to read off the log, not a customer's
  to lose a board over. A 401/403 is reported as a configuration error naming
  the variable, never the key.

The HTTP seam is the one ``googleapps/transport.py`` and
``billing/transport.py`` already use: an injectable :class:`Transport`, an
exact-match host allowlist enforced when the request is *constructed* so it
holds for the fakes too, redirects refused so the bearer key cannot follow a
``Location`` off the allowlist, and no message that carries the URL or the
key. The app user id is checked against ``billing/accounts.py``'s account-id
rule (``_ACCOUNT_RE``) before it is put in a path, so one id fits both the
ledger and this route, with one refusal the ledger does not need: an id made
only of dots (``.``, ``..``). The ledger uses the id as a dict key; this
module puts it in a path segment, ``urllib.parse.quote`` leaves a dot alone,
and a proxy that normalises dot segments would send ``/customers/..`` to the
project object instead of a customer, a 200 with no ``active_entitlements``
that reads as unshaped and fails open (see :func:`_valid_app_user_id`).

Off is a sentence, not an absence: with either variable unset
:func:`EntitlementGate.from_env` returns ``None`` and :func:`off_block` says
which one, the way ``service/metering.py::off_block`` does.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

__all__ = [
    "ALLOWED_HOSTS",
    "API_BASE",
    "CACHE_TTL_S",
    "DEFAULT_TIMEOUT_S",
    "ENTITLEMENT",
    "HEADER",
    "ORDER_STEP",
    "PROJECT_VAR",
    "SECRET_VAR",
    "Decision",
    "EntitlementError",
    "EntitlementGate",
    "HttpRequest",
    "HttpResponse",
    "Transport",
    "Verdict",
    "current",
    "decide",
    "ensure_revenuecat_url",
    "is_order_path",
    "off_block",
    "reset_for_tests",
    "urllib_transport",
]

#: The only host this module will ever address. An exact match: a suffix
#: check would wave through ``api.revenuecat.com.evil.example``.
ALLOWED_HOSTS = frozenset({"api.revenuecat.com"})
API_BASE = "https://api.revenuecat.com/v2"

#: The entitlement identifier the ``order`` step needs (the shared contract).
ENTITLEMENT = "pro"
#: The request header the desktop sends on every ``/steps`` request.
HEADER = "X-Kaleo-App-User-Id"
#: The step this gate applies to, and no other.
ORDER_STEP = "order"
SECRET_VAR = "REVENUECAT_SECRET_API_KEY"
PROJECT_VAR = "REVENUECAT_PROJECT_ID"
DEFAULT_TIMEOUT_S = 10.0
CACHE_TTL_S = 60.0
SOURCE = "revenuecat"

#: ``billing/accounts.py``'s ``_ACCOUNT_RE``, verbatim. It admits ``.`` and
#: ``..``, which are safe as a ledger key and not as a path segment, so
#: :func:`_valid_app_user_id` refuses those on top of it.
_APP_USER_ID_RE = re.compile(r"\A[A-Za-z0-9_.:-]{1,128}\Z")

NO_APP_USER_ID = "No app user id was sent; the desktop sends X-Kaleo-App-User-Id."
BAD_APP_USER_ID = (
    "The app user id in X-Kaleo-App-User-Id is not valid: it must be 1 to 128 "
    "characters of letters, digits, '_', '.', ':' or '-', and not only dots."
)


def _valid_app_user_id(app_user_id: str) -> bool:
    """The account-id rule, plus a refusal of an id made only of dots.

    ``urllib.parse.quote(app_user_id, safe="")`` leaves a dot alone, so ``..``
    reaches the wire as ``GET /v2/projects/{project_id}/customers/..``. A
    proxy or CDN that normalises dot segments would answer that with the
    project object (or the customer list for ``.``): a 200 that carries no
    ``active_entitlements`` and so reads as ``unavailable``, which fails open.
    A client that controls the header could pass a configured gate that way,
    and the cache would keep the verdict for the TTL. Refusing the id costs
    nothing: without the normalisation that request is a 404, which is
    ``inactive`` anyway.
    """
    if not _APP_USER_ID_RE.match(app_user_id or ""):
        return False
    return not set(app_user_id) <= {"."}


class EntitlementError(RuntimeError):
    """A RevenueCat call was refused before it was made, or failed.

    ``code`` is a local reason (``bad_host``, ``bad_app_user_id``,
    ``network_error``). Neither ``code`` nor ``detail`` ever carries the URL
    or the key: this object sits in the frame of every transport failure.
    """

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def ensure_revenuecat_url(url: str) -> str:
    """Return ``url`` if it addresses RevenueCat over https, else raise."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https":
        raise EntitlementError(
            "bad_host", f"refusing a non-https scheme {parsed.scheme or '<none>'!r}"
        )
    if parsed.hostname not in ALLOWED_HOSTS:
        raise EntitlementError(
            "bad_host",
            f"refusing to address a non-RevenueCat host {parsed.hostname!r}",
        )
    return url


@dataclass(frozen=True)
class HttpRequest:
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None

    def __post_init__(self) -> None:
        # Enforced here, not inside the real transport, so the guarantee holds
        # for every transport including the test fakes (billing/transport.py).
        ensure_revenuecat_url(self.url)

    def __repr__(self) -> str:
        """Redacted: no URL (it names the customer) and no bearer key."""
        return (
            f"HttpRequest({self.method} {urllib.parse.urlsplit(self.url).hostname}, "
            f"{len(self.headers)} headers, authorization=<redacted>)"
        )


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise EntitlementError(
                "bad_response", f"HTTP {self.status} body is not JSON"
            ) from None


class Transport(Protocol):
    def __call__(self, request: HttpRequest) -> HttpResponse: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a 3xx would carry the bearer key to whatever
    host the ``Location`` names, which is the path the allowlist closes."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def urllib_transport(
    request: HttpRequest, *, timeout: float = DEFAULT_TIMEOUT_S
) -> HttpResponse:
    """The real transport. An HTTP error status is a response, not an
    exception: RevenueCat puts its error JSON in the body of a 4xx."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        ensure_revenuecat_url(request.url),
        data=request.body,
        headers=request.headers,
        method=request.method,
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            return HttpResponse(status=resp.status, body=resp.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(status=exc.code, body=exc.read())
    except urllib.error.URLError as exc:
        # ``reason`` is the socket's own text ("timed out", "Name or service
        # not known"); it never contains the URL.
        raise EntitlementError("network_error", str(exc.reason)) from None
    except (OSError, http.client.HTTPException) as exc:
        # A half-read or malformed answer is the same fact as no answer.
        raise EntitlementError("network_error", str(exc)) from None


def _iso_utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Verdict:
    """One answer about one app user id, with the sentence that explains it.

    ``outcome`` is ``active``, ``inactive`` or ``unavailable``. ``active`` is
    true only for the first; the third is the fail-open case and is never
    dressed as either of the others.
    """

    outcome: str
    active: bool
    checked_at: str
    detail: str
    source: str = SOURCE
    entitlement: str = ENTITLEMENT

    @property
    def unavailable(self) -> bool:
        return self.outcome == "unavailable"

    def block(self) -> dict[str, Any]:
        """The ``entitlement`` block a step envelope carries."""
        if self.unavailable:
            return {
                "checked": False,
                "reason": "unavailable",
                "entitlement": self.entitlement,
                "detail": self.detail,
                "checked_at": self.checked_at,
            }
        return {
            "checked": True,
            "entitlement": self.entitlement,
            "active": self.active,
            "checked_at": self.checked_at,
        }

    def refusal(self) -> dict[str, Any]:
        """The 402 body. ``error`` is what ``app/src/lib/silkscreen/client.ts``
        ``errorFromBody`` shows; the other four keys are the contract."""
        return _refusal(self.detail, self.checked_at)


def _refusal(detail: str, checked_at: str) -> dict[str, Any]:
    return {
        "error": detail,
        "reason": "entitlement_required",
        "entitlement": ENTITLEMENT,
        "detail": detail,
        "checked_at": checked_at,
    }


class EntitlementGate:
    """Ask RevenueCat whether an app user id holds :data:`ENTITLEMENT`.

    ``transport`` and ``now`` are seams: the suite drives a recorded transport
    and a fixed clock, so no test opens a socket or waits out the TTL.
    """

    def __init__(
        self,
        secret: str,
        project_id: str,
        *,
        transport: Transport | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ttl_s: float = CACHE_TTL_S,
        now: Callable[[], float] = time.time,
        entitlement: str = ENTITLEMENT,
    ) -> None:
        if not secret or not project_id:
            raise EntitlementError(
                "bad_config", f"{SECRET_VAR} and {PROJECT_VAR} are both required"
            )
        self._secret = secret
        self.project_id = project_id
        self._timeout_s = timeout_s
        self._transport: Transport = transport or (
            lambda request: urllib_transport(request, timeout=timeout_s)
        )
        self.ttl_s = ttl_s
        self._now = now
        self.entitlement = entitlement
        self._cache: dict[str, tuple[float, Verdict]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> EntitlementGate | None:
        """The gate the environment configures, or ``None`` with either
        variable missing (and then :func:`off_block` says which)."""
        env = os.environ if env is None else env
        secret = (env.get(SECRET_VAR) or "").strip()
        project_id = (env.get(PROJECT_VAR) or "").strip()
        if not secret or not project_id:
            return None
        return cls(secret, project_id)

    def describe(self) -> dict[str, Any]:
        """Configuration, for ``/integrations``-style reporting. No secret."""
        return {
            "enabled": True,
            "project_id": self.project_id,
            "entitlement": self.entitlement,
            "cache_ttl_s": self.ttl_s,
        }

    def _request(self, app_user_id: str) -> HttpRequest:
        # Both segments are percent-encoded so a value that somehow passed
        # validation with a reserved character still cannot change the path.
        url = (
            f"{API_BASE}/projects/{urllib.parse.quote(self.project_id, safe='')}"
            f"/customers/{urllib.parse.quote(app_user_id, safe='')}"
        )
        return HttpRequest(
            url,
            headers={
                "Authorization": f"Bearer {self._secret}",
                "Accept": "application/json",
            },
        )

    def check(self, app_user_id: str) -> Verdict:
        """One verdict for ``app_user_id``, from the cache when it is fresh.

        Raises :class:`EntitlementError` (``bad_app_user_id``) for an id that
        fails the account-id rule; everything else, including a transport
        failure, comes back as a :class:`Verdict` so the caller's one branch
        is ``active`` or not.
        """
        if not _valid_app_user_id(app_user_id):
            raise EntitlementError("bad_app_user_id", BAD_APP_USER_ID)
        now = self._now()
        with self._lock:
            hit = self._cache.get(app_user_id)
            if hit is not None and now - hit[0] < self.ttl_s:
                return hit[1]
        verdict = self._fetch(app_user_id, now)
        with self._lock:
            self._cache[app_user_id] = (now, verdict)
        return verdict

    def _fetch(self, app_user_id: str, now: float) -> Verdict:
        checked_at = _iso_utc(now)
        try:
            response = self._transport(self._request(app_user_id))
        except EntitlementError as exc:
            reason = exc.detail or exc.code
            return self._unavailable(
                checked_at, f"RevenueCat could not be reached ({reason})"
            )
        return self._read(response, checked_at)

    def _read(self, response: HttpResponse, checked_at: str) -> Verdict:
        status = response.status
        if status in (401, 403):
            # A configuration error, in words. The key itself is never shown;
            # the variable name is what the operator needs to go and fix.
            return self._unavailable(
                checked_at,
                f"RevenueCat refused the configured {SECRET_VAR} (HTTP {status}); "
                f"check the key is an API v2 secret key for project "
                f"{self.project_id}",
            )
        if status == 404:
            return Verdict(
                "inactive",
                False,
                checked_at,
                f"RevenueCat has no customer with this app user id, so no "
                f"{self.entitlement} entitlement is active for it.",
                entitlement=self.entitlement,
            )
        if status == 429:
            return self._unavailable(
                checked_at, "RevenueCat rate-limited this service (HTTP 429)"
            )
        if status >= 500:
            return self._unavailable(checked_at, f"RevenueCat answered HTTP {status}")
        if status != 200:
            return self._unavailable(
                checked_at, f"RevenueCat answered an unexpected HTTP {status}"
            )
        try:
            payload = response.json()
        except EntitlementError as exc:
            return self._unavailable(checked_at, f"RevenueCat's answer {exc.detail}")
        # The documented customer object says ``"object": "customer"``. A 200
        # that is anything else (the project object, a list) is not an answer
        # about this customer and is named as such, not read as inactive.
        if not isinstance(payload, dict) or payload.get("object") != "customer":
            return self._unavailable(
                checked_at, "RevenueCat's answer was not a customer object"
            )
        items = _active_items(payload)
        if items is None:
            return self._unavailable(
                checked_at,
                "RevenueCat's answer carried no active_entitlements list",
            )
        for item in items:
            if item.get("entitlement_id") == self.entitlement:
                return Verdict(
                    "active",
                    True,
                    checked_at,
                    f"RevenueCat lists the {self.entitlement} entitlement as "
                    f"active for this app user id{_until(item.get('expires_at'))}.",
                    entitlement=self.entitlement,
                )
        return Verdict(
            "inactive",
            False,
            checked_at,
            f"RevenueCat lists no active {self.entitlement} entitlement for "
            f"this app user id.",
            entitlement=self.entitlement,
        )

    def _unavailable(self, checked_at: str, what: str) -> Verdict:
        detail = (
            f"Entitlement not checked: {what}; the order step was not gated "
            f"for this request."
        )
        sys.stderr.write(f"entitlements: {detail}\n")
        return Verdict(
            "unavailable", False, checked_at, detail, entitlement=self.entitlement
        )


def _active_items(payload: Any) -> list[dict[str, Any]] | None:
    """The ``active_entitlements.items`` list, or ``None`` when the answer is
    not shaped like the documented customer object."""
    if not isinstance(payload, dict):
        return None
    active = payload.get("active_entitlements")
    if not isinstance(active, dict):
        return None
    items = active.get("items")
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]


def _until(expires_at: Any) -> str:
    """`` until <ISO>`` for a documented ms-epoch expiry, else nothing."""
    if isinstance(expires_at, bool) or not isinstance(expires_at, int | float):
        return ""
    if expires_at <= 0:
        return ""
    return f" until {_iso_utc(expires_at / 1000.0)}"


def off_block(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """What the ``order`` envelope says when nothing is gating it: a sentence
    naming the variable, never an absent key a client has to interpret."""
    env = os.environ if env is None else env
    missing = [
        name for name in (SECRET_VAR, PROJECT_VAR) if not (env.get(name) or "").strip()
    ]
    named = missing[0] if missing else SECRET_VAR
    return {
        "checked": False,
        "reason": "not_configured",
        "detail": (
            f"Entitlement not checked: {named} is not set, so the order step "
            f"is not gated on this service."
        ),
    }


def is_order_path(path: str) -> bool:
    """True for ``/steps/<id>/order`` and nothing else.

    Read by ``app.py`` before ``steps.handle_post``, the way
    ``amend.is_lifecycle_path`` is: this is the one step the gate applies to,
    and a start, a place or a route must never be asked for an entitlement.
    """
    parts = [p for p in path.split("?")[0].split("/") if p]
    return len(parts) == 3 and parts[0] == "steps" and parts[2] == ORDER_STEP


@dataclass(frozen=True)
class Decision:
    """What ``app.py`` does with one ``order`` request.

    ``refused`` is the 402 body when the step must not run; otherwise it is
    ``None`` and ``block`` is the ``entitlement`` block for the envelope.
    """

    block: dict[str, Any]
    refused: dict[str, Any] | None = None


def decide(
    gate: EntitlementGate | None,
    app_user_id: str,
    *,
    now: Callable[[], float] = time.time,
) -> Decision:
    """The whole rule for one ``order`` press, in cost order.

    No gate: the off block, and no header is needed. A gate: a missing or
    malformed header is a 402 before any request is made; ``inactive`` is a
    402 with the verdict's sentence; ``active`` and ``unavailable`` both run
    the step and carry their block, because fail-open is the point.
    """
    if gate is None:
        return Decision(block=off_block())
    app_user_id = (app_user_id or "").strip()
    if not app_user_id:
        return Decision(block={}, refused=_refusal(NO_APP_USER_ID, _iso_utc(now())))
    try:
        verdict = gate.check(app_user_id)
    except EntitlementError as exc:
        if exc.code != "bad_app_user_id":
            raise
        return Decision(block={}, refused=_refusal(exc.detail, _iso_utc(now())))
    if verdict.outcome == "inactive":
        return Decision(block=verdict.block(), refused=verdict.refusal())
    return Decision(block=verdict.block())


_CURRENT: EntitlementGate | None = None
_RESOLVED = False
_CURRENT_LOCK = threading.Lock()


def reset_for_tests() -> None:
    global _CURRENT, _RESOLVED
    with _CURRENT_LOCK:
        _CURRENT = None
        _RESOLVED = False


def current() -> EntitlementGate | None:
    """The process's gate, resolved once from the environment.

    At call time and cached, never at import, for the reason
    ``service/metering.py::current`` gives: a test's environment and the
    desktop's ``envfiles.apply_saved_env`` both land after this module is
    imported. One instance per process is also what makes the TTL cache mean
    anything. ``reset_for_tests`` clears it.
    """
    global _CURRENT, _RESOLVED
    with _CURRENT_LOCK:
        if not _RESOLVED:
            _CURRENT = EntitlementGate.from_env()
            _RESOLVED = True
        return _CURRENT
