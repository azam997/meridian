"""Analyze a Meridian Companion capture headlessly; emit a compact digest JSON.

The Phase 2 entry point (spec revision 2026-07-27): the plugin spawns this
one-shot after each pull and renders the digest in-game, post-pull only. The
pipeline is exactly the replay oracle's: parse → LocalCaptureClient →
analyze_pull → _compare_all_aspects(refs=[]) → _build_response. Refs-relative
numbers (percentile, rank, refAvg*) are meaningless on a local run and are
not part of the digest.

The digest is a stable, flat schema for the C# side; severity mirrors the
desktop UI's thresholds (>=500 bad, >=200 warn, else info). Analysis errors
still produce a parseable digest with an "error" key (exit code 2).

Run from python/:
    python scripts/analyze_capture.py CAPTURE.ndjson [--out DIGEST.json]
        [--player NAME] [--full-out FULL.json] [--top 8]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DIGEST_SCHEMA = 1


def _severity(lost_potency: float) -> str:
    if lost_potency >= 500:
        return "bad"
    if lost_potency >= 200:
        return "warn"
    return "info"


def _fail(out_path: Path, message: str, notes: list[str]) -> int:
    digest = {"schema": DIGEST_SCHEMA, "error": message, "notes": notes}
    out_path.write_text(json.dumps(digest, indent=2), encoding="utf-8")
    print(f"analysis failed: {message}", file=sys.stderr)
    return 2


def build_digest(capture_path: Path, player_override: str | None, top: int,
                 full_out: Path | None) -> tuple[dict[str, Any], int]:
    """(digest, exit_code). Imports are deferred so --help stays instant."""
    from local_capture import LocalCaptureClient, parse_capture_text
    from mitplan.comp import canonical_job_name
    from jobs import analyze_pull, is_supported
    from sidecar.main import _build_response, _compare_all_aspects

    notes: list[str] = []
    text = capture_path.read_text(encoding="utf-8")
    capture = parse_capture_text(text)
    client = LocalCaptureClient(capture)
    code = f"local:{(capture.meta or {}).get('captureId', capture_path.stem)}"
    fight = capture.summary["fights"][0]
    actors = capture.summary["masterData"]["actors"]

    if player_override:
        actor = next((a for a in actors
                      if a["type"] == "Player"
                      and str(a["name"]).lower() == player_override.lower()), None)
        if actor is None:
            raise SystemExit(f"player {player_override!r} not in the capture")
    else:
        # The collector registers the local player as wire actor 1.
        actor = next((a for a in actors if a["id"] == 1 and a["type"] == "Player"), None)
        if actor is None:
            raise SystemExit("capture has no local player actor (id 1)")

    player = str(actor["name"])
    job = canonical_job_name(actor.get("subType") or "")
    if job is None or not is_supported(job):
        return ({
            "schema": DIGEST_SCHEMA,
            "error": f"no analyzer support for job {actor.get('subType') or 'Unknown'}",
            "player": player,
            "outcome": (capture.end or {}).get("outcome"),
            "notes": notes,
        }, 2)

    started = time.monotonic()
    you = analyze_pull(job, client, code, fight["id"], ranking_name=player, label="You")
    comparisons = _compare_all_aspects(job, you, [])
    response = _build_response(job, you, [], comparisons)
    analysis_ms = int((time.monotonic() - started) * 1000)

    if full_out is not None:
        full_out.write_text(json.dumps(response), encoding="utf-8")

    headline = response.get("headline") or {}
    improvements = response.get("improvements") or []
    clipping = ((response.get("aspectStates") or {}).get("Clipping") or {}).get("clipping") or {}

    if headline.get("multiTargetDisclaimed"):
        notes.append("multi-target pull without references — improvement cards are "
                     "suppressed and the ceiling may over-count cleave windows")
    if job in ("White Mage", "Scholar", "Astrologian", "Sage"):
        notes.append("healer analyzed without a mit-plan heal budget — required heal "
                     "GCDs count against efficiency")

    downtime = headline.get("downtimeTierA") or []
    delivered = headline.get("yourPotency") or 0
    idealized = headline.get("yourIdealizedPotency") or 0

    digest: dict[str, Any] = {
        "schema": DIGEST_SCHEMA,
        "capture": capture_path.name,
        "player": player,
        "job": job,
        "outcome": (capture.end or {}).get("outcome"),
        "durationSec": round(you.fight_duration_s, 1),
        "efficiencyPct": headline.get("efficiencyPct"),
        "efficiencyPctLenient": headline.get("efficiencyPctLenient"),
        "deliveredPotency": delivered,
        "idealizedPotency": idealized,
        "recoverablePotency": max(0, round(idealized - delivered)),
        "effectiveGcdSec": clipping.get("effectiveGcdSec"),
        "idleSec": clipping.get("totalIdleSec"),
        "clipSec": clipping.get("totalClipSec"),
        "downtimeSec": round(sum(w["endSec"] - w["startSec"] for w in downtime), 1),
        "downtimeSource": headline.get("downtimeSource"),
        "deaths": headline.get("deaths") or [],
        "deathsLostPotency": headline.get("deathsLostPotency") or 0,
        "multiTargetDisclaimed": bool(headline.get("multiTargetDisclaimed")),
        "isProgPull": bool(headline.get("isProgPull")),
        "bossPercentage": headline.get("bossPercentage"),
        "terminalDeathSec": headline.get("terminalDeathSec"),
        "improvements": [
            {
                "kind": item.get("kind"),
                "abilityName": item.get("abilityName"),
                "timeSec": item.get("timeSec"),
                "lostPotency": item.get("lostPotency"),
                "summary": item.get("summary"),
                "severity": _severity(item.get("lostPotency") or 0),
            }
            for item in improvements[:top]
        ],
        "improvementCount": len(improvements),
        "notes": notes,
        "analysisMs": analysis_ms,
    }
    return digest, 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("capture", help="path to a Meridian Companion .ndjson capture")
    ap.add_argument("--out", help="digest JSON path (default: <capture>.analysis.json)")
    ap.add_argument("--player", help="player name (default: the capture's actor 1)")
    ap.add_argument("--full-out", help="also write the full sidecar response here")
    ap.add_argument("--top", type=int, default=8, help="max improvement cards (default 8)")
    opts = ap.parse_args()

    capture_path = Path(opts.capture)
    out_path = Path(opts.out) if opts.out else capture_path.with_suffix(".analysis.json")
    full_out = Path(opts.full_out) if opts.full_out else None

    try:
        digest, exit_code = build_digest(capture_path, opts.player, opts.top, full_out)
    except SystemExit:
        raise
    except Exception as error:  # any analysis failure must stay parseable
        return _fail(out_path, f"{type(error).__name__}: {error}", [])

    out_path.write_text(json.dumps(digest, indent=2), encoding="utf-8")
    if digest.get("error"):
        print(f"analysis failed: {digest['error']}", file=sys.stderr)
    else:
        print(f"{digest['player']} ({digest['job']}) {digest['outcome']}: "
              f"{digest['efficiencyPct']}% efficiency, "
              f"{digest['improvementCount']} improvements, "
              f"{digest['analysisMs']} ms analysis")
    print(f"digest: {out_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
