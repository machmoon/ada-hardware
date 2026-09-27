"""Who a voice request acts for: ``verify_token`` for the MCP transport.

The order is ``service/app.py::_authorized``'s, so the voice endpoint and the
step routes agree on what a credential means:

1. An ``ada_`` key, when ``SILKSCREEN_API_KEYS_DB`` is set, is checked by
   :func:`service.auth.authenticate`; its account becomes the principal, and
   its public prefix the ``client_id``. Malformed, unknown, revoked and wrong
   are one answer -- 401 -- because ``authenticate`` makes them one.
2. ``MCP_HTTP_TOKEN``, compared in constant time, is the ``local`` account:
   ``billing.accounts.SingleAccountResolver``'s honest default. Every caller
   holding the shared token shares one board history, and ``docs/alexa.md``
   says so.
3. With neither configured, every request is ``local`` and ``anonymous``.
   ``load_config`` refuses to start that way on a non-loopback address.
4. Anything else is ``None``, a 401.

The key travels as ``Authorization: Bearer ada_...`` or in the path,
``/mcp/ada_...``, the form a URL-only connector dialog can hold; the
transport masks the path form in its log.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

from silkscreen.mcp.http import AccessToken

from .config import Config

__all__ = ["LOCAL", "make_verifier"]

LOCAL = "local"


def make_verifier(
    config: Config, *, key_store=None
) -> Callable[[str | None], AccessToken | None]:
    """The ``verify_token`` callable for ``config``.

    ``key_store`` is the ``service.auth.KeyStore`` seam (a ``MemoryKeyStore``
    in the tests); otherwise the one ``SILKSCREEN_API_KEYS_DB`` names is
    opened once, here.
    """
    from service import auth as keys_auth

    keys = key_store
    if keys is None and config.keys_db:
        keys = keys_auth.store_from_env({keys_auth.API_KEYS_DB_ENV: config.keys_db})
    token = config.token

    def verify_token(presented: str | None) -> AccessToken | None:
        if keys is None and not token:
            return AccessToken(LOCAL, "anonymous")
        if not presented:
            return None
        presented = presented.strip()
        if keys is not None and presented.startswith(keys_auth.KEY_PREFIX):
            account = keys_auth.authenticate(keys, presented)
            if account is None:
                return None
            return AccessToken(account.value, presented.partition(".")[0])
        if token and hmac.compare_digest(
            presented.encode("utf-8"), token.encode("utf-8")
        ):
            return AccessToken(LOCAL, "shared-token")
        return None

    return verify_token
