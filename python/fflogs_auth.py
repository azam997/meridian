"""FFLogs user sign-in: OAuth 2.0 Authorization Code + PKCE (RFC 7636).

The shipped app never carries a client secret — PKCE's code_verifier replaces
it, and FFLogs supports the PKCE flow for exactly this case. The app ships
only a PUBLIC client_id; each user signs into their own (free) FFLogs account
once in the browser, and the resulting tokens are theirs alone (their own
rate limit, revocable without affecting anyone else).

Flow (driven by the sidecar's fflogs_auth_* request kinds; the UI polls):
  1. begin(): bind a loopback port, generate state + PKCE pair, return the
     authorize URL for the frontend to open in the default browser.
  2. FFLogs redirects to http://127.0.0.1:<port>/callback?code=...&state=...
  3. The listener validates state, exchanges the code (code_verifier, no
     secret), fetches the account name from /api/v2/user, persists tokens
     to ~/.fflogs_efficiency_analyzer/auth.json, and shows a "return to the
     app" page.

Token refresh lives in fflogs_api.FFLogsClient (the consumer), not here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from config import AUTH_PATH
from fflogs_api import TOKEN_URL, USER_API_URL, AuthExpiredError  # noqa: F401 (re-export for callers)

AUTHORIZE_URL = "https://www.fflogs.com/oauth/authorize"

# The app's public OAuth client id (safe to ship — client ids are public by
# design; PKCE's code_verifier replaces the secret). Registered at
# https://www.fflogs.com/api/clients with the CALLBACK_PORTS redirect URLs.
# Overridable via config.json "oauth_client_id" for dev against another client.
FFLOGS_PUBLIC_CLIENT_ID = "019f681b-d248-7135-87ff-bf557b5c1866"

# Loopback callback ports — ALL registered as redirect URLs on the FFLogs
# client (comma-separated). begin() binds the first free one.
CALLBACK_PORTS = (53682, 53683, 53684)

# How long a begun sign-in waits for the browser round-trip before expiring.
LOGIN_TIMEOUT_S = 300


def public_client_id(cfg: dict) -> str:
    return cfg.get("oauth_client_id") or FFLOGS_PUBLIC_CLIENT_ID


def challenge_for(verifier: str) -> str:
    """RFC 7636 S256: BASE64URL(SHA256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) per RFC 7636 S256: verifier is 43-128
    URL-safe chars."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    return verifier, challenge_for(verifier)


class AuthStore:
    """Thread-safe persistence of the user's FFLogs tokens at auth.json.
    Shape: {access_token, refresh_token, expires_at (epoch s), user_name}."""

    def __init__(self, path: Path | None = None):
        # AUTH_PATH resolved at call time (module attribute), so tests can
        # repoint fflogs_auth.AUTH_PATH at a scratch dir.
        self._path = path if path is not None else AUTH_PATH
        self._lock = threading.Lock()

    def load(self) -> dict | None:
        with self._lock:
            try:
                if not self._path.exists():
                    return None
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except Exception:
                return None
            return data if isinstance(data, dict) and data.get("access_token") else None

    def save(self, tokens: dict) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")

    def delete(self) -> None:
        with self._lock:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass


@dataclass
class BeginInfo:
    authorize_url: str
    port: int


_SUCCESS_HTML = """<!doctype html><meta charset="utf-8">
<title>Signed in</title>
<body style="font-family:system-ui;background:#111;color:#eee;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><h2>Signed in to FF Logs</h2>
<p>You can close this tab and return to the app.</p></div>"""

_FAIL_HTML = """<!doctype html><meta charset="utf-8">
<title>Sign-in failed</title>
<body style="font-family:system-ui;background:#111;color:#eee;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><h2>Sign-in failed</h2>
<p>{msg}</p><p>Return to the app and try again.</p></div>"""


class _CallbackServer(HTTPServer):
    # Set by AuthSession right after construction.
    auth_session: "AuthSession"
    # Loopback one-shot: no need to linger in TIME_WAIT battles across retries.
    allow_reuse_address = True


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self._respond(404, "not found")
            return
        session = self.server.auth_session  # type: ignore[attr-defined]
        qs = parse_qs(parsed.query)
        ok, msg = session.handle_callback(qs)
        if ok:
            self._respond(200, _SUCCESS_HTML)
        else:
            self._respond(400, _FAIL_HTML.replace("{msg}", msg))

    def _respond(self, code: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        # Keep the NDJSON stderr channel quiet — the session records outcomes.
        pass


class AuthSession:
    """One sign-in attempt. Owns the loopback listener thread; the sidecar
    keeps a single module-level instance (a new begin cancels the prior)."""

    def __init__(self, client_id: str, ports: tuple[int, ...] = CALLBACK_PORTS,
                 store: AuthStore | None = None,
                 timeout_s: float = LOGIN_TIMEOUT_S):
        self.client_id = client_id
        self.store = store or AuthStore()
        self.status = "pending"  # pending | done | expired | cancelled | error
        self.error = ""
        self.user_name = ""
        self._ports = ports
        self._timeout_s = timeout_s
        self._stop = threading.Event()
        self._server: _CallbackServer | None = None
        self._thread: threading.Thread | None = None
        self._verifier = ""
        self._state = ""
        self.redirect_uri = ""

    def begin(self) -> BeginInfo:
        self._verifier, challenge = make_pkce_pair()
        self._state = secrets.token_urlsafe(24)
        port = None
        for p in self._ports:
            try:
                self._server = _CallbackServer(("127.0.0.1", p), _CallbackHandler)
                port = p
                break
            except OSError:
                continue
        if self._server is None or port is None:
            raise RuntimeError(
                "Could not open a local sign-in port (all of "
                f"{', '.join(map(str, self._ports))} are in use). "
                "Close the conflicting app and try again."
            )
        self._server.auth_session = self
        self._server.timeout = 1.0
        self.redirect_uri = f"http://127.0.0.1:{port}/callback"
        params = urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": self._state,
        })
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="fflogs-auth-callback")
        self._thread.start()
        return BeginInfo(f"{AUTHORIZE_URL}?{params}", port)

    def _serve(self) -> None:
        deadline = time.monotonic() + self._timeout_s
        server = self._server
        assert server is not None
        try:
            while (self.status == "pending" and not self._stop.is_set()
                   and time.monotonic() < deadline):
                server.handle_request()  # returns every server.timeout seconds
            if self.status == "pending":
                self.status = "cancelled" if self._stop.is_set() else "expired"
        finally:
            server.server_close()

    def cancel(self) -> None:
        self._stop.set()

    # Called on the listener thread by _CallbackHandler.
    def handle_callback(self, qs: dict[str, list[str]]) -> tuple[bool, str]:
        if self.status != "pending":
            return False, "this sign-in attempt is no longer active"
        if qs.get("state", [""])[0] != self._state:
            # Not our redirect — ignore without burning the attempt (a stray
            # request to the port must not be able to cancel a real sign-in).
            return False, "state mismatch"
        if "error" in qs:
            err = qs.get("error", ["denied"])[0]
            self.error = f"authorization was not granted ({err})"
            self.status = "error"
            return False, self.error
        code = qs.get("code", [""])[0]
        if not code:
            self.error = "no authorization code in callback"
            self.status = "error"
            return False, self.error
        return self._complete(code)

    def _complete(self, code: str) -> tuple[bool, str]:
        try:
            r = requests.post(
                TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "code_verifier": self._verifier,
                },
                timeout=30,
            )
        except requests.RequestException as e:
            self.error = f"token exchange failed: {e}"
            self.status = "error"
            return False, self.error
        if r.status_code != 200:
            # Surface the OAuth error body — e.g. Laravel Passport's
            # "Check the `client_secret` parameter" hint, which means the
            # client id is a CONFIDENTIAL client, not a public/PKCE one.
            detail = ""
            try:
                body = r.json()
                detail = ": " + (body.get("hint") or body.get("error_description")
                                 or body.get("error") or "")
            except Exception:
                pass
            self.error = f"token exchange rejected (HTTP {r.status_code}){detail}"
            self.status = "error"
            return False, self.error
        data = r.json()
        access = data.get("access_token", "")
        if not access:
            self.error = "token response carried no access_token"
            self.status = "error"
            return False, self.error
        self.user_name = self._fetch_user_name(access)
        self.store.save({
            "access_token": access,
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": time.time() + int(data.get("expires_in", 3600)),
            "user_name": self.user_name,
        })
        self.status = "done"
        return True, ""

    def _fetch_user_name(self, access_token: str) -> str:
        """Best-effort display name from /api/v2/user — a failure here must
        not fail the sign-in (the tokens are already good)."""
        try:
            r = requests.post(
                USER_API_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                json={"query": "{ userData { currentUser { name } } }"},
                timeout=30,
            )
            if r.status_code != 200:
                return ""
            return (r.json().get("data", {}).get("userData", {})
                    .get("currentUser", {}) or {}).get("name", "") or ""
        except Exception:
            return ""
