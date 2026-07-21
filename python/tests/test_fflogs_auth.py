"""Tests for fflogs_auth.py — the PKCE sign-in machinery.

Hermetic: the token endpoint is monkeypatched; the only network is a real
GET to the session's own 127.0.0.1 loopback listener (that's the code
under test).

Run from python/:  python tests/test_fflogs_auth.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fflogs_auth
from fflogs_auth import AuthSession, AuthStore, challenge_for, make_pkce_pair

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


def _get_status(url: str) -> tuple[int, str]:
    """GET that returns (status, body) instead of raising on 4xx."""
    try:
        with urllib.request.urlopen(url) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _wait_status(session: AuthSession, want_not: str = "pending",
                 timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while session.status == want_not and time.time() < deadline:
        time.sleep(0.05)
    return session.status


def test_pkce_s256_rfc_vector() -> None:
    """RFC 7636 appendix B test vector."""
    print()
    print("Test: PKCE S256 matches the RFC 7636 vector")
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    _check("challenge matches RFC vector",
           challenge_for(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
           challenge_for(verifier))
    v, c = make_pkce_pair()
    _check("generated verifier is 43+ chars", len(v) >= 43, str(len(v)))
    _check("generated pair is self-consistent", challenge_for(v) == c)
    v2, _ = make_pkce_pair()
    _check("verifiers are unique per call", v != v2)


def test_auth_store_roundtrip() -> None:
    print()
    print("Test: AuthStore round-trip / delete / corrupt-file handling")
    with tempfile.TemporaryDirectory() as scratch:
        path = Path(scratch) / "sub" / "auth.json"
        store = AuthStore(path)
        _check("load on missing file is None", store.load() is None)
        tokens = {"access_token": "at", "refresh_token": "rt",
                  "expires_at": 123.0, "user_name": "Someone"}
        store.save(tokens)
        _check("round-trip preserves payload", store.load() == tokens)
        path.write_text("{not json", encoding="utf-8")
        _check("corrupt file loads as None", store.load() is None)
        store.save(tokens)
        store.delete()
        _check("delete removes the file", not path.exists())
        store.delete()
        _check("delete is idempotent", store.load() is None)


def test_store_default_path_is_late_bound() -> None:
    print()
    print("Test: AuthStore default path follows fflogs_auth.AUTH_PATH")
    saved = fflogs_auth.AUTH_PATH
    with tempfile.TemporaryDirectory() as scratch:
        try:
            fflogs_auth.AUTH_PATH = Path(scratch) / "auth.json"
            store = AuthStore()
            store.save({"access_token": "x"})
            _check("write landed at the patched path",
                   (Path(scratch) / "auth.json").exists())
        finally:
            fflogs_auth.AUTH_PATH = saved


def test_authorize_url_shape() -> None:
    print()
    print("Test: begin() authorize URL carries the PKCE + CSRF params")
    with tempfile.TemporaryDirectory() as scratch:
        session = AuthSession("CID123", store=AuthStore(Path(scratch) / "auth.json"))
        info = session.begin()
        try:
            parsed = urllib.parse.urlparse(info.authorize_url)
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            _check("authorize endpoint",
                   info.authorize_url.startswith("https://www.fflogs.com/oauth/authorize?"))
            _check("response_type=code", qs.get("response_type") == "code")
            _check("client_id passthrough", qs.get("client_id") == "CID123")
            _check("redirect_uri matches bound port",
                   qs.get("redirect_uri") == f"http://127.0.0.1:{info.port}/callback")
            _check("S256 method", qs.get("code_challenge_method") == "S256")
            _check("challenge matches the session verifier",
                   qs.get("code_challenge") == challenge_for(session._verifier))
            _check("state present", len(qs.get("state", "")) > 16)
            _check("bound port is from the registered set",
                   info.port in fflogs_auth.CALLBACK_PORTS, str(info.port))
        finally:
            session.cancel()


def test_full_loopback_flow_mocked_exchange() -> None:
    """Real browser-side GET to the loopback listener; token endpoint mocked."""
    print()
    print("Test: full loopback callback flow (mocked token exchange)")
    posted: list[dict] = []

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        if url == fflogs_auth.TOKEN_URL:
            posted.append(dict(data))
            return _FakeResponse(200, {
                "access_token": "AT-1", "refresh_token": "RT-1", "expires_in": 7200,
            })
        if url == fflogs_auth.USER_API_URL:
            return _FakeResponse(200, {
                "data": {"userData": {"currentUser": {"name": "Probe User"}}},
            })
        raise AssertionError(f"unexpected POST {url}")

    saved_post = fflogs_auth.requests.post
    with tempfile.TemporaryDirectory() as scratch:
        store = AuthStore(Path(scratch) / "auth.json")
        session = AuthSession("CID123", store=store)
        info = session.begin()
        try:
            fflogs_auth.requests.post = fake_post

            code, _body = _get_status(
                f"http://127.0.0.1:{info.port}/callback?code=X&state=WRONG")
            _check("wrong state rejected with 400", code == 400, str(code))
            _check("session still pending after bad state",
                   session.status == "pending", session.status)

            code, body = _get_status(
                f"http://127.0.0.1:{info.port}/callback?code=THECODE&state="
                + urllib.parse.quote(session._state))
            _check("success page returned", code == 200 and "Signed in" in body,
                   f"{code} {body[:80]}")
            _check("session done", _wait_status(session) == "done", session.status)
            _check("exchange used the authorization_code grant",
                   bool(posted) and posted[0].get("grant_type") == "authorization_code")
            _check("exchange sent the code_verifier (PKCE, no secret)",
                   "code_verifier" in posted[0] and "client_secret" not in posted[0])
            _check("exchange echoed the redirect_uri",
                   posted[0].get("redirect_uri") == session.redirect_uri)
            saved = store.load() or {}
            _check("tokens persisted", saved.get("access_token") == "AT-1"
                   and saved.get("refresh_token") == "RT-1")
            _check("expiry recorded ahead of now",
                   saved.get("expires_at", 0) > time.time() + 3600)
            _check("user name captured", saved.get("user_name") == "Probe User")
        finally:
            fflogs_auth.requests.post = saved_post
            session.cancel()


def test_denied_expired_cancelled() -> None:
    print()
    print("Test: denied callback, session timeout, explicit cancel")
    with tempfile.TemporaryDirectory() as scratch:
        store = AuthStore(Path(scratch) / "auth.json")

        session = AuthSession("CID123", store=store)
        info = session.begin()
        code, _body = _get_status(
            f"http://127.0.0.1:{info.port}/callback?error=access_denied&state="
            + urllib.parse.quote(session._state))
        _check("denied callback returns 400", code == 400, str(code))
        _check("denied → error status", _wait_status(session) == "error",
               session.status)
        _check("no tokens persisted on denial", store.load() is None)

        fast = AuthSession("CID123", store=store, timeout_s=0.5)
        fast.begin()
        _check("unattended session expires", _wait_status(fast) == "expired",
               fast.status)

        cancelled = AuthSession("CID123", store=store)
        cancelled.begin()
        cancelled.cancel()
        _check("cancel → cancelled status",
               _wait_status(cancelled) == "cancelled", cancelled.status)


def main() -> int:
    test_pkce_s256_rfc_vector()
    test_auth_store_roundtrip()
    test_store_default_path_is_late_bound()
    test_authorize_url_shape()
    test_full_loopback_flow_mocked_exchange()
    test_denied_expired_cancelled()

    print()
    print("=" * 60)
    print(f"Passed: {len(_PASSED)}    Failed: {len(_FAILED)}")
    if _FAILED:
        for n, d in _FAILED:
            print(f"  - {n}    {d}")
    return 0 if not _FAILED else 1


if __name__ == "__main__":
    sys.exit(main())
