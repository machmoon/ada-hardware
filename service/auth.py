"""Per-account API keys: who is calling, revocably.

``docs/production-plan.md`` section 3, item 1: "Identity: API keys per
account. Without it, nothing else can be metered or revoked." Until now the
service had one shared bearer token (``SILKSCREEN_ACCESS_TOKEN``), which says
*that* a caller may spend money but never *who*.

The design is djangorestframework-api-key's
(florimondmanca/djangorestframework-api-key
``src/rest_framework_api_key/crypto.py`` at 3a1609f, MIT):

- a key is ``<prefix>.<secret>``, a short random prefix and a 32-character
  random secret, shown to its owner exactly once;
- the store keeps the prefix in the clear, to find the row and to name the
  key in a list, and a SHA-512 of the whole key -- no salt and no slow
  password hash, because a 32-character random secret cannot be guessed from
  a dictionary (that project's ``Sha512ApiKeyHasher`` argument);
- verification compares hashes in constant time.

Ours are spelled ``ada_<prefix>.<secret>`` so a leaked key is recognisable
in a log or a paste, the reason Stripe's ``sk_`` and GitHub's ``ghp_``
prefixes exist.

Storage is a seam (:class:`KeyStore`), the ``billing`` ledger's move:
:class:`SqliteKeyStore` today, a Postgres store behind the same five methods
when the service runs as more than one task. Revocation is a timestamp, never
a delete, so "which key made this run" survives the key.

Turning it on: set ``SILKSCREEN_API_KEYS_DB`` to a SQLite path. The shared
token keeps working beside it until it is unset.

    python -m service.auth create --account acme --name "ci runner"
    python -m service.auth list
    python -m service.auth revoke ada_Ab12Cd34
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import os
import secrets
import sqlite3
import string
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from billing.accounts import AccountId

__all__ = [
    "API_KEYS_DB_ENV",
    "KEY_PREFIX",
    "ApiKey",
    "KeyStore",
    "MemoryKeyStore",
    "SqliteKeyStore",
    "authenticate",
    "create_key",
    "hash_key",
    "store_from_env",
]

API_KEYS_DB_ENV = "SILKSCREEN_API_KEYS_DB"
KEY_PREFIX = "ada_"
PREFIX_LENGTH = 8
SECRET_LENGTH = 32
_ALPHABET = string.ascii_letters + string.digits


@dataclass(frozen=True)
class ApiKey:
    """One key's record. Never holds the key itself."""

    prefix: str
    hashed: str
    account: AccountId
    name: str
    created_at: float
    revoked_at: float | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class KeyStore(Protocol):
    def add(self, key: ApiKey) -> None: ...
    def by_prefix(self, prefix: str) -> ApiKey | None: ...
    def all(self) -> list[ApiKey]: ...
    def revoke(self, prefix: str, when: float) -> bool: ...
    def close(self) -> None: ...


def hash_key(key: str) -> str:
    return hashlib.sha512(key.encode("utf-8")).hexdigest()


def _random(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def create_key(
    store: KeyStore, account: str, name: str = "", *, now: float | None = None
) -> tuple[str, ApiKey]:
    """Mint a key for ``account``. Returns the key (show it once) and its record."""
    acct = AccountId(account)
    while True:
        prefix = KEY_PREFIX + _random(PREFIX_LENGTH)
        if store.by_prefix(prefix) is None:
            break
    key = f"{prefix}.{_random(SECRET_LENGTH)}"
    record = ApiKey(
        prefix=prefix,
        hashed=hash_key(key),
        account=acct,
        name=name[:80],
        created_at=time.time() if now is None else now,
    )
    store.add(record)
    return key, record


def authenticate(store: KeyStore, presented: str) -> AccountId | None:
    """The account a presented key belongs to, or ``None``.

    ``None`` covers every refusal -- malformed, unknown prefix, wrong secret,
    revoked -- so a caller cannot tell which check failed.
    """
    prefix, dot, secret = presented.strip().partition(".")
    if (
        not dot
        or not prefix.startswith(KEY_PREFIX)
        or len(prefix) != len(KEY_PREFIX) + PREFIX_LENGTH
        or len(secret) != SECRET_LENGTH
    ):
        return None
    record = store.by_prefix(prefix)
    # Hash either way, so an unknown prefix costs what a known one does.
    candidate = hash_key(presented.strip())
    if record is None:
        hmac.compare_digest(candidate, candidate)
        return None
    if not hmac.compare_digest(candidate, record.hashed) or not record.active:
        return None
    return record.account


class MemoryKeyStore:
    """For tests and for a single process that needs nothing durable."""

    def __init__(self) -> None:
        self._rows: dict[str, ApiKey] = {}
        self._lock = threading.Lock()

    def add(self, key: ApiKey) -> None:
        with self._lock:
            if key.prefix in self._rows:
                raise ValueError(f"key prefix {key.prefix} already exists")
            self._rows[key.prefix] = key

    def by_prefix(self, prefix: str) -> ApiKey | None:
        with self._lock:
            return self._rows.get(prefix)

    def all(self) -> list[ApiKey]:
        with self._lock:
            return sorted(self._rows.values(), key=lambda k: (k.created_at, k.prefix))

    def revoke(self, prefix: str, when: float) -> bool:
        with self._lock:
            row = self._rows.get(prefix)
            if row is None or row.revoked_at is not None:
                return False
            self._rows[prefix] = ApiKey(
                row.prefix, row.hashed, row.account, row.name, row.created_at, when
            )
            return True

    def close(self) -> None:
        pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    prefix TEXT PRIMARY KEY,
    hashed TEXT NOT NULL,
    account TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    revoked_at REAL
)
"""


class SqliteKeyStore:
    """A file on disk; WAL and ``synchronous=FULL``, the ledger's settings,
    because a revocation that a power cut forgets is a key that still works."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self._path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(_SCHEMA)
        self._db.commit()
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    @staticmethod
    def _row(row: tuple) -> ApiKey:
        prefix, hashed, account, name, created_at, revoked_at = row
        return ApiKey(prefix, hashed, AccountId(account), name, created_at, revoked_at)

    def add(self, key: ApiKey) -> None:
        with self._lock, self._db:
            try:
                self._db.execute(
                    "INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, ?)",
                    (key.prefix, key.hashed, key.account.value, key.name,
                     key.created_at, key.revoked_at),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"key prefix {key.prefix} already exists") from exc

    def by_prefix(self, prefix: str) -> ApiKey | None:
        with self._lock:
            row = self._db.execute(
                "SELECT prefix, hashed, account, name, created_at, revoked_at"
                " FROM api_keys WHERE prefix = ?",
                (prefix,),
            ).fetchone()
        return None if row is None else self._row(row)

    def all(self) -> list[ApiKey]:
        with self._lock:
            rows = self._db.execute(
                "SELECT prefix, hashed, account, name, created_at, revoked_at"
                " FROM api_keys ORDER BY created_at, prefix"
            ).fetchall()
        return [self._row(r) for r in rows]

    def revoke(self, prefix: str, when: float) -> bool:
        with self._lock, self._db:
            done = self._db.execute(
                "UPDATE api_keys SET revoked_at = ?"
                " WHERE prefix = ? AND revoked_at IS NULL",
                (when, prefix),
            )
        return done.rowcount == 1

    def close(self) -> None:
        with self._lock:
            self._db.close()


_STORES: dict[str, SqliteKeyStore] = {}
_STORES_LOCK = threading.Lock()


def store_from_env(environ: dict[str, str] | None = None) -> KeyStore | None:
    """The configured store, opened once per path, or ``None`` (no keys)."""
    env = os.environ if environ is None else environ
    path = (env.get(API_KEYS_DB_ENV) or "").strip()
    if not path:
        return None
    with _STORES_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = _STORES[path] = SqliteKeyStore(path)
        return store


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m service.auth",
                                     description="Manage Ada API keys.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create", help="mint a key and print it once")
    create.add_argument("--account", required=True)
    create.add_argument("--name", default="")
    sub.add_parser("list", help="every key's prefix, account and state")
    revoke = sub.add_parser("revoke", help="revoke a key by its prefix")
    revoke.add_argument("prefix")
    args = parser.parse_args(argv)

    store = store_from_env()
    if store is None:
        print(f"error: set {API_KEYS_DB_ENV} to the key database path", file=sys.stderr)
        return 2
    if args.command == "create":
        key, record = create_key(store, args.account, args.name)
        print(key)
        print(f"# {record.prefix} for {record.account}; shown once, not stored",
              file=sys.stderr)
        return 0
    if args.command == "list":
        for k in store.all():
            state = "active" if k.active else "revoked"
            print(f"{k.prefix}\t{k.account}\t{state}\t{k.name}")
        return 0
    if store.revoke(args.prefix, time.time()):
        print(f"revoked {args.prefix}")
        return 0
    print(f"error: no active key {args.prefix}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
