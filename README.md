# Meridian

A Windows desktop app that measures how efficiently you played your FFXIV job on a raid
pull. It signs into your FFLogs account, pulls your logs, and compares what you actually
delivered against a job-specific simulated optimal ceiling computed at *your own* GCD
speed — then tells you, located and priced in potency, where the gap came from.

**[Download the latest release](https://github.com/azam997/meridian-releases/releases/latest)**

## What it does

- **Efficiency headline** — delivered potency vs a simulator-derived upper bound
  (a true ceiling: no real parse may exceed it), with rank and percentile against
  the top logs for your job + encounter.
- **Potential Improvements** — the gap decomposed into concrete cards: missed casts,
  clipping, gauge overcap, buff misalignment, opener, deaths — each priced in potency.
- **Timeline** — your rotation next to the idealized one (optimal and
  hold-for-2-min-windows variants), with downtime, buff windows, and tincture overlays.
- **Cast counts** — per-ability counts vs the reference median.
- **Kill Time Theorizer** — what your kill time would look like at higher efficiency.

Downtime is modeled from enemy targetability (not cast gaps), multi-target windows are
credited symmetrically, and deaths are priced but never pardoned by the ceiling.

### Supported jobs

| Role | Jobs |
| --- | --- |
| Tank | Paladin, Warrior, Dark Knight, Gunbreaker |
| Melee | Monk, Dragoon, Ninja, Samurai, Reaper, Viper |
| Physical ranged | Bard, Machinist, Dancer |
| Caster | Black Mage, Summoner, Red Mage, Pictomancer |

Healers are not yet supported — a pure damage ceiling undersells a role that has to
heal, so they're waiting on a healer-appropriate model rather than a bad grade.

Encounters: the current Savage tier + supported ultimates.

## How it's built

Three trees, one app:

- `src/` — React 19 + TypeScript + Vite frontend.
- `src-tauri/` — Tauri 2 (Rust) desktop shell: webview, sidecar process hosting,
  auto-updater.
- `python/` — the analyzer. Runs as a child process ("sidecar") speaking NDJSON over
  stdin/stdout; owns the FFLogs GraphQL client, the per-job rotation simulators, and
  all scoring. The idealized-rotation engine is job-agnostic (`python/jobs/_core/sim/`);
  each job contributes a data bundle + a rotation model, not a copy of the loop.

The UI never talks to FFLogs directly — the wire contract lives in
`src/sidecar/contract.ts` ↔ `python/sidecar/main.py`.

## Development setup

Prerequisites: Node 20+, Rust (stable, for Tauri), Python 3.14+.

```powershell
npm install
pip install -r python/requirements.txt
pip install -r python/requirements-dev.txt   # tests

npm run dev          # frontend only, mock sidecar (no Python/Rust needed)
npm run tauri dev    # the full desktop app with the real sidecar
npm run test         # Python test suite (pytest, parallel)
npm run lint         # eslint
npm run build        # typecheck + frontend bundle
```

### FFLogs credentials (dev)

The shipped app signs in via FFLogs OAuth (PKCE) — no setup needed. For development you
can instead use client-credentials: create a v2 API client at
<https://www.fflogs.com/api/clients>, then copy `config_template.json` to
`~/.fflogs_efficiency_analyzer/config.json` and fill it in:

| Key | Meaning |
| --- | --- |
| `client_id` / `client_secret` | FFLogs v2 API client credentials (dev fallback; the app works without them once signed in) |
| `oauth_client_id` | Optional override of the app's public PKCE client id |
| `is_dev` | Enables the permanent on-disk FFLogs response cache for development |
| `cache_cap_mb` | Disk cache size cap (10–100, default 15) |

## Feedback

Use **Submit Feedback** inside the app — it bundles diagnostics and prefills a GitHub
issue on the [releases repo](https://github.com/azam997/meridian-releases/issues).

## License

[AGPL-3.0](LICENSE) — © 2026 azam997. You may use, modify, and redistribute this
software freely, but if you distribute a modified version — or run one as a network
service — you must publish your source under the same license.

---

This repository is published as a source snapshot from a private working repo, so it
carries a single commit rather than full history.
