# Meridian
**[Download the latest release](https://github.com/azam997/meridian-releases/releases/latest)**

For disclosure, yes AI tools played a large part in creating this. I am a software professional, and I have adhered to best practice while creating this app even if I was not writing the majority of the actual code. I have ensured that there are no strange network requests (obviously, it does make calls to the FFLogs API), the app is not designed to and does not collect data, and it is fully open sourced.

Meridian is a desktop app that uses the FFLogs API to gather information about an encounter (currently supporting Heavyweight savage and DMU), calculates downtime and forced movements via log data, and simulates how much potency you could deliver during the kill time achieved in your log. This let's non-BiS and non-parse party players measure how well they performed within their circumstances; without worrying about Crits or if your Dragoon was doing backflips and delayed his buff slightly. The app then provides feedback on your rotation compared to the simulated one, and can help suggest better timings or ability usage. Also included, for healers, is a mitigation/heal planner that attempts to measure "unavoidable" damage from mechanics and come up with a plan to mitigate and heal through it for the whole party, and stacks it against your used mit. The efficiency score there is graded against how many "locked" GCDs you had to spend healing. There is also a Kill Time Theorizer to help a user infer what combo they should aim for given a specific kill time, for players that want to theorycraft rotations based on kill time and factor in downtime/mechanics. Prog logs ARE supported (they infer a kill time), and ultimates are broken down by Phases to help players maximize their rotation within a phase instead of just big picture - since DPS checks must be met.

## Features
- Research top player's rotations on a timeline, allowing for side by side comparison to yourself, them, and the simulator.
- Measure your "efficiency" (potency delivered) instead of just DPS
- Detects downtime and forced movements (particularly important for melees that have to disengage)
- attempts to model job specific mechanics (example, MCH squeezing flamethrower for a tick when a boss returns, SAM meditate/third eye, etc)
- Directly shows top 10 players for the job's rotations to help understand what high level players are doing
- A full breakdown of where the efficiency fell off compared to the simulator, with feedback on how to improve
- Mitigation/Heal planner for healers / party mit (includes preset PF mit plan for DMU, only supporting one at the moment as they need to be imported and converted to a data structure the code can read)
- Kill Time Theorizer, given an estimated kill time and a party composition, will simulate the best rotation for that kill time, including downtime, to simulate optimal rotation for that time.
- Supports connecting your FFLogs account through their API or pasting individual logs
- Prog Log feedback
- By phase ultimate rotation breakdown (some phases have tighter squeezes than others)


## Dev Builds
THIS IS ONLY FOR BUILDING DEV BUILDS IF YOU WISH TO TINKER WITH IT - FOR STANDARD INSTALLS PLEASE USE THE LINK TO THE SETUP EXE!
**[Download the latest release](https://github.com/azam997/meridian-releases/releases/latest)**

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

File bugs and suggestions as issues on the
[releases repo](https://github.com/azam997/meridian-releases/issues).

## License

[AGPL-3.0](LICENSE) — © 2026 azam997. You may use, modify, and redistribute this
software freely, but if you distribute a modified version — or run one as a network
service — you must publish your source under the same license.

---

This repository is published as source snapshots from a private working repo: one
commit per export rather than full development history. Each release is tagged
(`v<version>`), so the exact source of every released build stays available here.
