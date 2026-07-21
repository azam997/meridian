"""FFLogsClient dual-mode tests + sidecar _client() auth precedence.

Covers:
  - legacy client-credentials mode is byte-identical (token POST w/ basic
    auth, queries to /api/v2/client)
  - user-token mode: bearer from the auth store, queries to /api/v2/user
  - refresh on expiry (rotation persisted, no secret sent), refresh failure
    → AuthExpiredError
  - one 401 → forced refresh → retry
  - sidecar _client() precedence: auth.json → config creds → raise

All HTTP is faked at the requests.Session level.

Run from python/:  python tests/test_client_modes.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fflogs_api import (
    API_URL,
    TOKEN_URL,
    USER_API_URL,
    AuthExpiredError,
    FFLogsClient,
)

_PASSED: list[str] = []
_FAILED: list[tuple[str, str]] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        _PASSED.append(name)
        print(f"  [OK  ] {name}")
    else:
        _FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
        raise AssertionError(f"{name}  {detail}".rstrip())


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Scripted requests.Session stand-in. `script` maps a URL to either one
    response or a list popped per call; every call is recorded."""

    def __init__(self, script: dict):
        self.script = {u: (list(r) if isinstance(r, list) else [r])
                       for u, r in script.items()}
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, json=None, headers=None, auth=None, timeout=None):
        self.calls.append((url, {"data": data, "json": json,
                                 "headers": headers, "auth": auth}))
        queue = self.script.get(url)
        if not queue:
            raise AssertionError(f"unexpected POST {url}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


class FakeStore:
    def __init__(self, tokens: dict | None):
        self.tokens = dict(tokens) if tokens else None
        self.saved: list[dict] = []

    def load(self) -> dict | None:
        return dict(self.tokens) if self.tokens else None

    def save(self, t: dict) -> None:
        self.tokens = dict(t)
        self.saved.append(dict(t))

    def delete(self) -> None:
        self.tokens = None


_GQL_OK = {"data": {"probe": 1}}


def test_legacy_mode_unchanged() -> None:
    print()
    print("Test: client-credentials mode (legacy) — token POST + /client queries")
    c = FFLogsClient("CID", "SECRET")
    fake = FakeSession({
        TOKEN_URL: _FakeResponse(200, {"access_token": "CC-TOK", "expires_in": 3600}),
        API_URL: _FakeResponse(200, _GQL_OK),
    })
    c._session = fake
    out = c.query("{ probe }")
    _check("query result unwrapped", out == {"probe": 1}, str(out))
    _check("first call = token endpoint", fake.calls[0][0] == TOKEN_URL)
    _check("basic auth carries the credentials",
           fake.calls[0][1]["auth"] == ("CID", "SECRET"))
    _check("grant_type=client_credentials",
           fake.calls[0][1]["data"] == {"grant_type": "client_credentials"})
    _check("GraphQL goes to /api/v2/client", fake.calls[1][0] == API_URL)
    _check("bearer header set",
           fake.calls[1][1]["headers"]["Authorization"] == "Bearer CC-TOK")
    c.query("{ probe }")
    _check("token reused within expiry (no second token POST)",
           sum(1 for u, _ in fake.calls if u == TOKEN_URL) == 1)


def test_user_mode_fresh_token() -> None:
    print()
    print("Test: user mode — bearer from store, /user endpoint, no token POST")
    store = FakeStore({"access_token": "USR-TOK", "refresh_token": "R1",
                       "expires_at": time.time() + 3600, "user_name": "Me"})
    c = FFLogsClient.for_user(store, "PUBCID")
    fake = FakeSession({USER_API_URL: _FakeResponse(200, _GQL_OK)})
    c._session = fake
    out = c.query("{ probe }")
    _check("query result unwrapped", out == {"probe": 1}, str(out))
    _check("GraphQL goes to /api/v2/user", fake.calls[0][0] == USER_API_URL)
    _check("bearer is the stored user token",
           fake.calls[0][1]["headers"]["Authorization"] == "Bearer USR-TOK")
    _check("no token endpoint call for a fresh token",
           all(u != TOKEN_URL for u, _ in fake.calls))
    _check("nothing re-persisted", store.saved == [])


def test_user_mode_refresh_on_expiry() -> None:
    print()
    print("Test: user mode — expired token triggers a PKCE refresh (no secret)")
    store = FakeStore({"access_token": "OLD", "refresh_token": "R1",
                       "expires_at": time.time() - 10, "user_name": "Me"})
    c = FFLogsClient.for_user(store, "PUBCID")
    fake = FakeSession({
        TOKEN_URL: _FakeResponse(200, {"access_token": "NEW", "refresh_token": "R2",
                                       "expires_in": 7200}),
        USER_API_URL: _FakeResponse(200, _GQL_OK),
    })
    c._session = fake
    c.query("{ probe }")
    tok_calls = [kw for u, kw in fake.calls if u == TOKEN_URL]
    _check("exactly one refresh", len(tok_calls) == 1, str(len(tok_calls)))
    _check("grant_type=refresh_token",
           tok_calls[0]["data"]["grant_type"] == "refresh_token")
    _check("refresh sends client_id but never a secret",
           tok_calls[0]["data"].get("client_id") == "PUBCID"
           and "client_secret" not in tok_calls[0]["data"]
           and tok_calls[0]["auth"] is None)
    _check("rotated tokens persisted",
           store.tokens["access_token"] == "NEW"
           and store.tokens["refresh_token"] == "R2")
    _check("user_name preserved across refresh",
           store.tokens.get("user_name") == "Me")
    gql = [kw for u, kw in fake.calls if u == USER_API_URL]
    _check("query used the refreshed bearer",
           gql[0]["headers"]["Authorization"] == "Bearer NEW")


def test_user_mode_refresh_failure_raises() -> None:
    print()
    print("Test: user mode — dead refresh token → AuthExpiredError")
    store = FakeStore({"access_token": "OLD", "refresh_token": "R1",
                       "expires_at": time.time() - 10})
    c = FFLogsClient.for_user(store, "PUBCID")
    c._session = FakeSession({TOKEN_URL: _FakeResponse(400, {"error": "invalid_grant"})})
    try:
        c.query("{ probe }")
        _check("AuthExpiredError raised", False, "no exception")
    except AuthExpiredError:
        _check("AuthExpiredError raised", True)

    empty = FFLogsClient.for_user(FakeStore(None), "PUBCID")
    empty._session = FakeSession({})
    try:
        empty.query("{ probe }")
        _check("empty store → AuthExpiredError", False, "no exception")
    except AuthExpiredError:
        _check("empty store → AuthExpiredError", True)


def test_user_mode_401_retry_once() -> None:
    print()
    print("Test: user mode — 401 forces one refresh + one retry")
    store = FakeStore({"access_token": "STALE", "refresh_token": "R1",
                       "expires_at": time.time() + 3600})
    c = FFLogsClient.for_user(store, "PUBCID")
    fake = FakeSession({
        USER_API_URL: [_FakeResponse(401, {}), _FakeResponse(200, _GQL_OK)],
        TOKEN_URL: _FakeResponse(200, {"access_token": "FRESH", "refresh_token": "R2",
                                       "expires_in": 7200}),
    })
    c._session = fake
    out = c.query("{ probe }")
    _check("retry succeeded", out == {"probe": 1}, str(out))
    api_calls = [kw for u, kw in fake.calls if u == USER_API_URL]
    _check("exactly two API attempts", len(api_calls) == 2, str(len(api_calls)))
    _check("retry used the refreshed bearer",
           api_calls[1]["headers"]["Authorization"] == "Bearer FRESH")
    _check("one refresh happened",
           sum(1 for u, _ in fake.calls if u == TOKEN_URL) == 1)


def test_sidecar_client_precedence() -> None:
    print()
    print("Test: sidecar _client() precedence — auth.json > config creds > raise")
    import fflogs_auth
    from sidecar import main as sidecar_main

    class FakeClient:
        made: list[str] = []

        def __init__(self, cid, cs):
            FakeClient.made.append(f"cc:{cid}")

        @classmethod
        def for_user(cls, store, cid):
            inst = cls.__new__(cls)
            FakeClient.made.append(f"user:{cid}")
            return inst

    saved_client_cls = sidecar_main.FFLogsClient
    saved_load = sidecar_main.load_config
    saved_auth_path = fflogs_auth.AUTH_PATH
    saved_cache = sidecar_main.CACHE_DIR

    with tempfile.TemporaryDirectory() as scratch:
        scratch_path = Path(scratch)
        try:
            sidecar_main.FFLogsClient = FakeClient
            sidecar_main.CACHE_DIR = scratch_path / "cache"
            fflogs_auth.AUTH_PATH = scratch_path / "auth.json"

            # a) auth.json present (+ config creds present) → user mode wins
            fflogs_auth.AuthStore().save({"access_token": "T",
                                          "refresh_token": "R",
                                          "expires_at": time.time() + 100})
            sidecar_main.load_config = lambda: {"client_id": "CID",
                                                "client_secret": "S",
                                                "oauth_client_id": "PUB"}
            sidecar_main._reset_session_client()
            sidecar_main._client()
            _check("user mode wins when both configured",
                   FakeClient.made == ["user:PUB"], str(FakeClient.made))

            # b) no auth.json, config creds → legacy client-credentials
            FakeClient.made.clear()
            fflogs_auth.AuthStore().delete()
            sidecar_main._reset_session_client()
            sidecar_main._client()
            _check("falls back to client-credentials",
                   FakeClient.made == ["cc:CID"], str(FakeClient.made))

            # c) neither → AuthExpiredError (typed for the UI sign-in gate)
            FakeClient.made.clear()
            sidecar_main.load_config = lambda: {}
            sidecar_main._reset_session_client()
            try:
                sidecar_main._client()
                _check("raises when unconfigured", False, "no exception")
            except AuthExpiredError:
                _check("raises when unconfigured", True)

            # d) auth status reporting matches each state
            sidecar_main.load_config = lambda: {}
            _check("status none", sidecar_main._auth_status() == {"mode": "none"})
            sidecar_main.load_config = lambda: {"client_id": "C", "client_secret": "S"}
            _check("status client_credentials",
                   sidecar_main._auth_status() == {"mode": "client_credentials"})
            fflogs_auth.AuthStore().save({"access_token": "T", "user_name": "Me"})
            _check("status user",
                   sidecar_main._auth_status() == {"mode": "user", "userName": "Me"})
        finally:
            sidecar_main.FFLogsClient = saved_client_cls
            sidecar_main.load_config = saved_load
            sidecar_main.CACHE_DIR = saved_cache
            fflogs_auth.AUTH_PATH = saved_auth_path
            sidecar_main._reset_session_client()


def main() -> int:
    test_legacy_mode_unchanged()
    test_user_mode_fresh_token()
    test_user_mode_refresh_on_expiry()
    test_user_mode_refresh_failure_raises()
    test_user_mode_401_retry_once()
    test_sidecar_client_precedence()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}    {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
