"""Build the updater feed (latest.json) + the release body for a GitHub release.

`tauri build` with createUpdaterArtifacts produces the NSIS setup exe (the
update artifact itself) plus a `.sig` minisign signature — but NOT the
latest.json feed the updater polls. This script writes it, and also copies
the setup exe to a SPACE-FREE canonical filename: GitHub converts spaces in
release asset names to dots, which would silently break the URL written into
the feed (a no-op today with productName "Meridian", but kept so a future
rename can't silently break the feed). Minisign signatures are
filename-independent, so the rename is safe.

Release notes come from `src/data/changelog.json` — the SAME file the app
bundles for its "What's new" popup and Version history tab, so a release is
written once. This script renders that entry to markdown (`release-notes.md`,
for `gh release create --notes-file`) and puts its one-line `summary` in the
feed's `notes` (what the in-app update pill shows as its tooltip).

Usage (from the repo root, after `npm run release`):
    python scripts/make_latest_json.py
    python scripts/make_latest_json.py --print-notes   # preview, no build needed
Then upload the files it prints to the GitHub release:
    gh release create v<version> <renamed-setup.exe> latest.json \
        --repo <owner>/<releases-repo> --title v<version> --notes-file <notes.md>
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAURI_CONF = ROOT / "src-tauri" / "tauri.conf.json"
CHANGELOG = ROOT / "src" / "data" / "changelog.json"
BUNDLE_DIR = ROOT / "src-tauri" / "target" / "release" / "bundle" / "nsis"

# Must match the release repo the updater endpoint in tauri.conf.json points at.
RELEASES_REPO = "azam997/meridian-releases"


def render_markdown(entry: dict) -> str:
    """Render a changelog entry to the GitHub release body.

    `summary` is the lead paragraph; each section is an optional `## heading`,
    an optional body paragraph, and an optional bullet list.
    """
    parts = [entry["summary"].strip()]
    for section in entry.get("sections") or []:
        block = []
        if section.get("heading"):
            block.append(f"## {section['heading']}")
        if section.get("body"):
            block.append(section["body"].strip())
        if section.get("items"):
            block.append("\n".join(f"- {item}" for item in section["items"]))
        if block:
            parts.append("\n\n".join(block))
    return "\n\n".join(parts) + "\n"


def load_entry(version: str) -> dict | None:
    """The changelog entry for `version`, or None. Warns when the newest entry
    isn't the version being shipped (usually means the entry was forgotten and
    the app's titlebar is showing a stale version — APP_VERSION is derived from
    changelog[0])."""
    log = json.loads(CHANGELOG.read_text(encoding="utf-8"))
    if log and log[0].get("version") != version:
        print(f"!! changelog's newest entry is v{log[0].get('version')} but this "
              f"build is v{version} — the app's titlebar/About will read "
              f"v{log[0].get('version')}", file=sys.stderr)
    return next((e for e in log if e.get("version") == version), None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--notes", default="",
                    help="override the release body (default: rendered from changelog.json)")
    ap.add_argument("--print-notes", action="store_true",
                    help="print the rendered release body and exit (no build required)")
    ap.add_argument("--repo", default=RELEASES_REPO, help="owner/repo serving the GitHub release")
    args = ap.parse_args()

    conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    version = conf["version"]

    entry = load_entry(version)
    if args.notes:
        body = args.notes
        summary = args.notes
    elif entry is None:
        print(f"!! no changelog entry for v{version} — add one to "
              f"{CHANGELOG.relative_to(ROOT)} (or pass --notes to override)",
              file=sys.stderr)
        return 1
    else:
        body = render_markdown(entry)
        summary = entry["summary"].strip()

    if args.print_notes:
        print(body, end="")
        return 0

    setups = sorted(BUNDLE_DIR.glob("*-setup.exe"))
    if not setups:
        print(f"!! no *-setup.exe under {BUNDLE_DIR} — run `npm run release` first",
              file=sys.stderr)
        return 1
    setup = setups[-1]
    sig = setup.with_name(setup.name + ".sig")
    if not sig.exists():
        print(f"!! missing {sig.name} — was TAURI_SIGNING_PRIVATE_KEY set during the build?",
              file=sys.stderr)
        return 1

    flat_name = f"Meridian_{version}_x64-setup.exe"
    flat_path = BUNDLE_DIR / flat_name
    if setup != flat_path:
        shutil.copy2(setup, flat_path)

    feed = {
        "version": version,
        # One line — this is the update pill's tooltip, not a release page.
        "notes": summary,
        "pub_date": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "platforms": {
            "windows-x86_64": {
                "signature": sig.read_text(encoding="utf-8").strip(),
                "url": f"https://github.com/{args.repo}/releases/download/v{version}/{flat_name}",
            }
        },
    }
    latest = BUNDLE_DIR / "latest.json"
    latest.write_text(json.dumps(feed, indent=2), encoding="utf-8")

    notes_path = BUNDLE_DIR / "release-notes.md"
    notes_path.write_text(body, encoding="utf-8")

    print(f">> wrote {latest}")
    print(f">> wrote {notes_path}")
    print(f">> upload these two assets to the v{version} release on {args.repo}:")
    print(f"   {flat_path}")
    print(f"   {latest}")
    print(f'   gh release create v{version} "{flat_path}" "{latest}" '
          f'--repo {args.repo} --title v{version} --notes-file "{notes_path}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
