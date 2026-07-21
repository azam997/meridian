"""One-shot interactive probe of the FFLogs PKCE flow + /api/v2/user.

De-risks the shipped sign-in before trusting it in the app:
  1. Full PKCE round-trip (browser opens; loopback callback; code exchange).
  2. /api/v2/user: currentUser (who am I), rateLimitData (IMPORTANT: note
     whether limitPerHour looks per-user or shared-per-client), and one
     analysis-shaped report query to prove the user endpoint serves the
     same GraphQL schema the analyzer uses.
  3. Token metadata: expires_in, refresh_token presence, and one live
     refresh grant.

Prereqs: an FFLogs API client (https://www.fflogs.com/api/clients) with
redirect URLs http://127.0.0.1:53682/callback,http://127.0.0.1:53683/callback,
http://127.0.0.1:53684/callback registered.

Run (from python/):
    python scripts/fflogs_pkce_probe.py --client-id <PUBLIC_CLIENT_ID> [--report <code>]
The client id may also come from config.json's "oauth_client_id".
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

import fflogs_auth  # noqa: E402
from config import load_config  # noqa: E402
from fflogs_api import TOKEN_URL, USER_API_URL  # noqa: E402


def _gql(token: str, query: str) -> dict:
    r = requests.post(
        USER_API_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query},
        timeout=60,
    )
    print(f"   HTTP {r.status_code}")
    body = r.json()
    if "errors" in body:
        print(f"   GraphQL errors: {body['errors']}")
    return body.get("data") or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", default="", help="FFLogs public client id")
    ap.add_argument("--report", default="", help="a report code to probe the events query with")
    args = ap.parse_args()

    cid = args.client_id or fflogs_auth.public_client_id(load_config())
    if not cid:
        print("!! no client id (pass --client-id or set oauth_client_id in config.json)")
        return 2

    # --- 1. PKCE round-trip (isolated store: don't clobber a real sign-in) ---
    store = fflogs_auth.AuthStore(Path.cwd() / "pkce_probe_auth.json")
    session = fflogs_auth.AuthSession(cid, store=store)
    info = session.begin()
    print(f">> opening browser (port {info.port}):\n   {info.authorize_url}")
    webbrowser.open(info.authorize_url)
    while session.status == "pending":
        time.sleep(1)
    print(f">> sign-in status: {session.status} {session.error}")
    if session.status != "done":
        return 1
    tokens = store.load() or {}
    print(f">> access token: {len(tokens['access_token'])} chars; "
          f"refresh_token present: {bool(tokens.get('refresh_token'))}; "
          f"expires in {(tokens['expires_at'] - time.time()) / 3600:.1f} h")

    tok = tokens["access_token"]

    # --- 2a. Who am I ---
    print(">> currentUser:")
    data = _gql(tok, "{ userData { currentUser { id name } } }")
    print(f"   {data.get('userData', {}).get('currentUser')}")

    # --- 2a'. Claimed characters (powers the in-app character picker) ---
    print(">> currentUser.characters (name/lodestoneID/server/region/DC):")
    data = _gql(tok, """{ userData { currentUser { characters {
        name lodestoneID
        server { name region { compactName } subregion { name } }
    } } } }""")
    chars = (data.get("userData", {}).get("currentUser") or {}).get("characters")
    print(f"   {json.dumps(chars)[:400] if chars else 'NONE (claim a character on fflogs.com)'}")

    # --- 2b. Rate limit attribution ---
    print(">> rateLimitData (compare across two different FFLogs accounts to "
          "confirm per-user attribution — a shared pool would show the same "
          "pointsSpentThisHour for both):")
    data = _gql(tok, "{ rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }")
    print(f"   {data.get('rateLimitData')}")

    # --- 2c. Analysis-shaped queries ---
    print(">> worldData zone probe (rankings-adjacent shape):")
    data = _gql(tok, "{ worldData { expansions { id name } } }")
    ok_world = bool(data.get("worldData", {}).get("expansions"))
    print(f"   expansions: {'OK' if ok_world else 'MISSING'}")

    ok_report = True
    if args.report:
        print(f">> report events probe ({args.report}):")
        data = _gql(tok, f'''{{ reportData {{ report(code: "{args.report}") {{
            title
            fights(killType: Encounters) {{ id name }}
        }} }} }}''')
        rep = data.get("reportData", {}).get("report")
        ok_report = bool(rep)
        print(f"   report: {json.dumps(rep)[:200] if rep else 'MISSING'}")
    else:
        print(">> (pass --report <code> to probe the report/events schema too)")

    # --- 3. Refresh grant ---
    print(">> refresh grant:")
    r = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": tokens.get("refresh_token", ""),
        "client_id": cid,
    }, timeout=30)
    print(f"   HTTP {r.status_code}; "
          f"rotated refresh_token: {bool(r.status_code == 200 and r.json().get('refresh_token'))}")

    store.delete()
    verdict = ok_world and ok_report and r.status_code == 200
    print(f">> VERDICT: {'PASS' if verdict else 'CHECK OUTPUT ABOVE'}")
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())
