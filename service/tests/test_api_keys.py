"""Per-account API keys (service/auth.py) and the bearer gate that reads them."""

from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from http.server import ThreadingHTTPServer

import pytest

from service import app as app_module
from service import auth, logs


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        yield auth.MemoryKeyStore()
    else:
        s = auth.SqliteKeyStore(tmp_path / "keys.db")
        yield s
        s.close()


def test_a_minted_key_resolves_to_its_account_and_is_never_stored(store):
    key, record = auth.create_key(store, "acme", "ci runner", now=10.0)
    assert key.startswith("ada_") and key.split(".")[0] == record.prefix
    assert len(key.split(".")[1]) == 32
    assert record.hashed == auth.hash_key(key) and key not in record.hashed
    assert auth.authenticate(store, key).value == "acme"
    assert auth.authenticate(store, f"  {key}  ").value == "acme"


@pytest.mark.parametrize(
    "mangle",
    [
        lambda k: k[:-1] + ("A" if k[-1] != "A" else "B"),  # wrong secret
        lambda k: k.replace(".", ""),                         # no separator
        lambda k: "ada_zzzzzzzz." + k.split(".")[1],          # unknown prefix
        lambda k: k.replace("ada_", "sk_"),                   # not ours
        lambda k: k + "x",                                    # wrong length
        lambda k: "",
    ],
)
def test_every_refusal_is_the_same_none(store, mangle):
    key, _ = auth.create_key(store, "acme")
    assert auth.authenticate(store, mangle(key)) is None


def test_a_revoked_key_stops_working_and_stays_listed(store):
    key, record = auth.create_key(store, "acme")
    assert store.revoke(record.prefix, 20.0) is True
    assert store.revoke(record.prefix, 21.0) is False
    assert auth.authenticate(store, key) is None
    (listed,) = store.all()
    assert listed.prefix == record.prefix and listed.revoked_at == 20.0


def test_the_sqlite_store_survives_a_reopen_and_is_private(tmp_path):
    path = tmp_path / "keys.db"
    first = auth.SqliteKeyStore(path)
    key, record = auth.create_key(first, "acme")
    first.close()
    assert path.stat().st_mode & 0o077 == 0
    again = auth.SqliteKeyStore(path)
    try:
        assert auth.authenticate(again, key).value == "acme"
        with pytest.raises(ValueError, match="already exists"):
            again.add(record)
    finally:
        again.close()


def test_a_bad_account_name_is_refused(store):
    from billing.errors import ConfigError

    with pytest.raises(ConfigError):
        auth.create_key(store, "has space")


def test_the_cli_mints_lists_and_revokes(tmp_path, monkeypatch):
    monkeypatch.setenv(auth.API_KEYS_DB_ENV, str(tmp_path / "cli.db"))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert auth._main(["create", "--account", "acme", "--name", "laptop"]) == 0
    key = out.getvalue().strip()
    assert "shown once" in err.getvalue()
    prefix = key.split(".")[0]
    out = io.StringIO()
    with redirect_stdout(out):
        assert auth._main(["list"]) == 0
    assert out.getvalue().split("\t")[:3] == [prefix, "acme", "active"]
    with redirect_stdout(io.StringIO()):
        assert auth._main(["revoke", prefix]) == 0
    with redirect_stderr(io.StringIO()):
        assert auth._main(["revoke", prefix]) == 1
    monkeypatch.delenv(auth.API_KEYS_DB_ENV)
    with redirect_stderr(io.StringIO()):
        assert auth._main(["list"]) == 2


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setenv(auth.API_KEYS_DB_ENV, str(tmp_path / "gate.db"))
    monkeypatch.setenv(logs.LOG_FORMAT_ENV, "json")
    monkeypatch.delenv(app_module.ACCESS_TOKEN_ENV, raising=False)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def _status(url: str, token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                    timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_the_gate_admits_a_live_key_names_its_account_and_refuses_the_rest(
    server, capfd
):
    store = auth.store_from_env()
    key, record = auth.create_key(store, "acme")
    route = f"{server}/models"
    assert _status(route, None) == 401
    assert _status(route, "ada_nope.nope") == 401
    assert _status(route, key) != 401
    assert _status(f"{server}/healthz", None) == 200  # probes stay exempt
    store.revoke(record.prefix, 1.0)
    assert _status(route, key) == 401
    records = [json.loads(line) for line in capfd.readouterr().err.splitlines()
               if line.startswith("{")]
    admitted = [r for r in records if r.get("path") == "/models"
                and r.get("status") != 401]
    assert admitted and all(r["account_id"] == "acme" for r in admitted)
    refused = [r for r in records if r.get("status") == 401]
    assert refused and all("account_id" not in r for r in refused)


def test_the_shared_token_still_works_beside_keys(server, monkeypatch):
    monkeypatch.setenv(app_module.ACCESS_TOKEN_ENV, "shared-secret")
    assert _status(f"{server}/models", "shared-secret") != 401
    assert _status(f"{server}/models", "wrong") == 401
