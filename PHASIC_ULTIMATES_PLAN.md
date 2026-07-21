# Savage / Ultimates tab split + per-phase (phasic) ultimate analysis — implementation plan

> Authored 2026-07-17 (planning session; main @ 3bc20f7). Status: **planning complete, implementation NOT started.**
> All file/line references verified against that commit; FFLogs phase-API facts verified LIVE against the real API (not guessed).
> Implement as five independently shippable PRs, in order. Related backlog: NEXT_STEPS "PROG LOGS follow-ups" (a)/(d) — this plan delivers (d)'s v1 lens and leaves (a) (phase-aware projector) as an explicit follow-up on the same seam.

## Context — why

The app's "Encounter" nav tab currently mixes the Savage tier and ultimates in one flat encounter list. The user wants:

1. **Rename "Encounter" → "Savage"**, scoped to savage encounters, behaving exactly as today.
2. **A replicated "Ultimates" tab** (reusing SetupView, not forking it) scoped to ultimate encounters.
3. The split is groundwork for **per-phase ("phasic") analysis of ultimates**: ultimates have phase DPS checks that force overspending resources in some windows and banking them for others. Players (especially proggers) want per-phase optimization feedback, not just whole-pull efficiency.
4. **Prog logs matter most** (wipes, via the shipped Prog-logs-v1 flow). Desired: analyze what the top-10 clears do per phase and flag when the player's saving/spending deviates from their patterns (e.g. "refs bank gauge into P4; you entered P4 with 20").

## Verified facts (do not re-derive; trust these)

### FFLogs API (live-probed 2026-07-17 with the repo's own client)
- `ReportFight.phaseTransitions: [{id: Int, startTime: Float}]` — **report-relative ms** (subtract `fight.startTime`). Live Dancing Mad kill (report `TQ2AHxDzqngv1f3F`, fight 25): transitions `[{1,36810158},{2,37018971},{3,37238298},{4,37543177},{5,37706313}]`, fight span 36810158–37929573 → P1≈209s, P2≈219s, P3≈305s, P4≈163s, P5≈223s.
- `Report.phases: [EncounterPhases {encounterID, separatesWipes, phases: [PhaseMetadata {id, name, isIntermission}]}]` — encounter 1085 returns named phases: "P1: Kefka", "P2: Forsaken Kefka", "P3: Exdeath and Chaos", "P4: Kefka Says", "P5: Ultima Kefka". **Not currently fetched by the repo.**
- `ReportFight` also has `lastPhase`, `lastPhaseAsAbsoluteIndex`, `lastPhaseIsIntermission` (lastPhase/lastPhaseIsIntermission already fetched).

### Backend (python/)
- `encounters.py`: `AAC_HEAVYWEIGHT_ENCOUNTERS` (ids 101–105, zone 73, diff 101) vs `ULTIMATE_ENCOUNTERS` `[(1085, "Dancing Mad (Ultimate)")]` (zone 76, diff 100); `ZONE_GROUPS=[(zone,diff,[ids])]`; `_ULTIMATE_IDS`; helpers `encounter_difficulty(id)` / `zone_difficulty(zone)`. **No savage/ultimate tag reaches the wire anywhere** — the frontend cannot currently tell them apart.
- `fflogs_api.py` `_REPORT_SUMMARY_FIELDS` (~44–63) **already fetches** per fight: `lastPhase lastPhaseIsIntermission phaseTransitions { id startTime }` + fightPercentage/bossPercentage/enemyNPCs. Adding `Report.phases` (names) to the selection requires bumping `_SUMMARY_FIELDS_V` (in `sidecar/dev_cache.py:206`, currently 3 → 4) so cached summaries refetch. `tests/test_refs_cache.py:218–228` references the constant symbolically (survives a bump).
- Dormant "phase-aware seam" (intentionally left during Prog-logs-v1): `jobs/_core/prog.py` `ProgContext.phase_transitions` (captured, unused) and `jobs/_core/kill_projection.py` `ProjectionInputs.last_phase`/`.phase_transitions` (carried, unused by the v1 uniform active-rate projector).
- `sidecar/main.py`: `get_catalog` (~327) emits flat `encounters:[{id,name}]`; `list_setup` (~712) emits `{encounters:[{id,name,totalKills,bestParsePct}], pullsByEncounterId}`; `list_prog_pulls` (~623) emits wipes `{reportCode,fightId,durationS,fightPercentage,bossPercentage,lastPhase,label}`; `run_analysis` (~837) → `_analyze_and_build` (~1006): heal-lock → `analyze_pull(you)` → `_get_refs` → `_inject_tier_b` → `_inject_melee_downtime` → `_inject_multi_target` → `_compare_all_aspects` → `_build_response` (~2614).
- **Zero-kill gap (critical for PR 1)**: `fflogs_api.py::_encounters_from_zone_rankings` (~720–736) drops encounters with `totalKills <= 0`. A prog-only character gets **no ultimate row** from `list_setup` → the Ultimates tab would be empty and the prog flow dead. `list_setup` must synthesize zero-kill ultimate rows.
- **Refs are full `ModuleResult`s** (`_build_refs` ~1165: top-10 rankings → full `analyze_pull` per ref, 6-worker pool). Each ref retains `norm_casts: tuple[(t_s, ability_id)]`, its Abilities track (ships to the UI for reference lanes), `downtime_windows`, `fight_duration_s`, all aspect states. Each ref's report summary (with `phaseTransitions`) is already prefetched into the summary cache → **per-phase ref segmentation costs zero extra round trips**.
- Gauge model: `jobs/_core/job.py::GaugeModel {name, generators, spenders, cap, value_p_per_unit, cap_boosts}` on `JobData.gauges`. **No gauge-over-time track exists.** `jobs/_aspects/overcap.py::compute_overcap_for_gauge` already walks `norm_casts` with a running balance (discards it); `jobs/_core/entry_gauge.py` (`measure_entry_gauge`, `seed_entry_gauge`, `EntryState`) is the mid-fight-continuation seeding machinery (used for M12S-P2), reusable per phase.
- Downtime Tier-A (`_core/downtime_sources.py`) segments by targetability; ultimate phase relays already handled there (Kefka P2 silent-despawn tail cap ~62–69). Phase boundaries should come from `phaseTransitions`; Tier-A windows provide within-phase uptime.
- Tests: pytest from `python/`; `test_contract_snapshot.py` gates `_build_response` shape changes (regen `UPDATE_SNAPSHOT=1`); **no fixture currently carries phaseTransitions**; prog tests build synthetic fights. `conftest.py` stubs xivapi.

### Frontend (src/)
- Tabs: `App.tsx` `NAV` (~71–77) — `{id:'setup', label:'Encounter'}` + dashboard/timeline/counts; `View` union `src/state/appState.ts:12–22`; Resources nav hardcoded ~566–603. `setView('setup')`/`view==='setup'` sites: App.tsx 318, 364, 447, 631, 686, 700, 706 + HomeView `onNavigate` at 628.
- `SetupView.tsx`: flat encounter `<select>` (~551–571) fed by `listSetupCached` (`list_setup`); selection = string label via `encMatches`. Prog UI lives in the "Pull & references" card: `pullSource: 'kills'|'wipes'` toggle (~577–597; default `'kills'` at line 89), lazy `listProgPulls` (~172–200), paste-report fallback (~206–244); healers forced to `'kills'`. **Job confirmation is component-local** (`jobConfirmed`, line 72) — with two tab instances it must be lifted into App state via the existing `onJobConfirmed` callback.
- Types with **no ultimate/difficulty flag**: `Encounter` (appState.ts:36–41), `SetupEncounter` (contract.ts:203–208), `Catalog.encounters` (contract.ts:260–269). Zero ultimate special-casing in src/ (only mock fixtures, mock.ts:131/295 already list 1085).
- Persistence: `src/state/persist.ts` (key `...lastSelection.v3`) saves job/encounter/encounterId/pull/refsBucket; active tab not persisted.
- `refsPrefetch.ts` warms ALL catalog encounters for the saved job (`enqueueJob` ~171–189) — ultimates already in the warm matrix.
- Dashboard: prog framing card (DashboardView.tsx ~666–685), KPI grid, "Where the potency went" stack, then `<JobPanels/>` at DashboardView.tsx:1087 (JobProfile registry, `src/jobs/index.ts`). Phase UI = new **encounter-driven** panel (registry is job-driven — the plan places it as a shared panel near :1087, not per-job). `TimelineView` gets `analysis` + `focus` — phase bands overlay there (existing `downtimeA`/`downtimeB` bands ~1028–1046, `tl-band` CSS pattern).
- Contract conventions: snake_case → `_camelize` on emit; **additive fields need no PROTOCOL_VERSION bump**; severity is frontend-side from lostPotency.

## Product decisions (confirmed with the user 2026-07-17)

1. **Phasic depth v1**: per-phase pattern-vs-refs metrics **plus** per-phase delivered-vs-idealized obtained by *slicing the existing whole-fight sim timeline at phase boundaries*. **No per-phase re-optimized sim ceiling in v1** (explicitly deferred; cross-phase banking makes "optimal per phase" ambiguous anyway).
2. **Ultimates catalog**: Dancing Mad only (the one wired ultimate). FRU/legacy ults out of scope (legacy = level-synced, wrong kits for the current sims).
3. **Ultimates tab defaults**: pull source defaults to **wipes** ("In progress") on the Ultimates tab; Savage keeps "Completed".

## Architecture decisions (up front)

- **Category on the wire**: additive `category: 'savage' | 'ultimate'` on catalog + setup encounters; source of truth = new `encounters.py::encounter_category(id)` (from `_ULTIMATE_IDS`). No PROTOCOL_VERSION bump (additive).
- **Two nav ids, one component**: new `View` id `'ultimates'`; both tabs render the *same* `SetupView` with a `category` prop. No fork.
- **Phases are job-agnostic + data-gated**: presence of `fight.phaseTransitions` drives everything. Pure computation in new `jobs/_core/phases.py` + `jobs/_core/phase_metrics.py`; orchestration/serialization in `sidecar/main.py` (mirroring the multi_target/tincture pass pattern). Savage pulls emit **no new keys** → snapshots byte-identical.
- **Deviations are their own dashboard panel, NOT improvements cards** (v1): they aren't potency-priced, so the lostPotency/severity/≥2-children conventions don't apply honestly.
- **Healers**: unchanged flow (planner routing, forced `'kills'`). Phase bands may render for any analysis carrying phases; phase *deviations* suppressed for locked-healer runs (comparisons are rank-suppressed there anyway).
- **Phase-aware kill projection**: explicit follow-up, out of scope (the `ProjectionInputs` seam stays untouched).

## PR 1 — Savage / Ultimates tab split (ship first)

Backend:
- `encounters.py`: add `encounter_category(encounter_id) -> "ultimate" | "savage"`.
- `sidecar/main.py::get_catalog` (~327): each encounter dict gains `"category"`.
- `sidecar/main.py::list_setup` (~712): each encounter gains `category`; **append synthesized rows** `{"id", "name", "totalKills": 0, "bestParsePct": None, "category": "ultimate"}` for `ULTIMATE_ENCOUNTERS` missing from the response (fixes the zero-kill gap). Savage rows byte-identical.

Frontend:
- `contract.ts`: `SetupEncounter` + `Catalog.encounters` gain `category?: 'savage'|'ultimate'` (absent ⇒ `'savage'`); tag the mock client's encounters (`mock.ts` already lists 1085).
- `appState.ts`: `View` union gains `'ultimates'`; `Encounter` gains `category?`.
- `App.tsx`:
  - NAV: `{id:'setup', label:'Savage'}` (renamed) + new `{id:'ultimates', label:'Ultimates', Icon: (e.g. lucide Skull/Flame), needs: null}`.
  - Duplicate the `view==='setup'` render block for `'ultimates'` passing `category="ultimate"`; key each instance by `(lodestoneId, category)`.
  - `lastSetupTab: 'setup'|'ultimates'` state, set on nav clicks, hydrated from persistence; route the error-fallback/EmptyHint `setView('setup')` sites (318, 364, 447, 686, 700, 706) through it.
  - `onJobConfirmed`: also `setState(cur => ({...cur, job}))` so the sibling tab mounts confirmed.
- `SetupView.tsx`:
  - New prop `category`; filter `data.encounters` by `(e.category ?? 'savage') === category` after `listSetupCached` (~129) — everything downstream (select at 551–571, recent pulls, speculative pre-analysis) self-scopes.
  - `pullSource` default (line 89): `category === 'ultimate' ? 'wipes' : 'kills'` (healers still derive to `'kills'`; toggle stays hidden for them).
  - Empty-state: the "No {job} logs found" error (~136–139) only for savage; on ultimates with 0 kills + source `'kills'`, show an inline hint pointing at "In progress".
- `persist.ts`: `Persisted` gains `activeSetupTab?` (additive; keep the v3 key — `Partial` read tolerates absence).
- `refsPrefetch.ts` (optional polish): order the warm queue active-encounter → same-category → rest (needs catalog `category`).

Ship check: `npm run build` + `npm run lint` + `npm run dev` (mock shows both tabs); `cd python; python -m pytest -n auto` (contract snapshot untouched — `_build_response` unchanged).

## PR 2 — Phase plumbing

- `fflogs_api.py` `_REPORT_SUMMARY_FIELDS` (~44–63): add top-level report field `phases { encounterID separatesWipes phases { id name isIntermission } }` (sibling of `fights(...)`; shared by `get_report_summary` + batched `get_report_summaries`, so no drift). Update docstring ~263.
- `dev_cache.py`: `_SUMMARY_FIELDS_V = 4` (comment: v4 = report-level `phases`).
- **New `jobs/_core/phases.py`** (pure): frozen `Phase {id, name, start_s, end_s, is_intermission}`; `encounter_phase_names(report, encounter_id)`; `phase_segments(report, fight, *, full_end_ms=None)` — report-relative ms → fight-relative s, sort/dedupe/clamp ≥0, phase ends at next transition (last at `full_end_ms or fight["endTime"]`), name fallback `f"P{id}"`, returns `()` when transitions absent (savage no-ops); `downtime_overlap_s(phase, windows)`; `split_casts_by_phase(norm_casts, phases)`.
- `module_result.py`: `ModuleResult.phases: tuple = ()` (additive default; in-process refs cache unaffected).
- `jobs/__init__.py::analyze_pull`: stash `orig_end_ms = fight["endTime"]` **before** the prog terminal-death clamp (~253); after `resolve_downtime` (~269) compute `phase_segments(report, fight, full_end_ms=orig_end_ms)` → `ModuleResult(phases=...)` (~318). Zero extra round trips; runs identically for you and every ref.
- `sidecar/main.py::_build_response` after the prog block (~2803), **only when `you.phases` non-empty**: emit `"phases": [{id, name, startSec, endSec, isIntermission, downtimeSec, reached, completed}]` where `reached = start_s < full_end` (wipe duration) and `completed = end_s <= scored_end + 0.5` (terminal-death-clamped).
- `contract.ts`: `PhaseInfoJson`; `AnalysisResult.phases?: PhaseInfoJson[]`.
- Tests: new `tests/test_phases.py` (synthetic dicts): ms→s; first transition == fight start (live shape above); unsorted/dup transitions; missing `report["phases"]` → `P{n}`; wipe spans full duration but `completed` respects the scored end; savage → `()`; overlap math. **`test_contract_snapshot.py` must pass with zero snapshot changes.**

## PR 3 — Timeline phase bands + prog phase framing

- `TimelineView.tsx`: render phase separators + slim labeled bands (intermissions tinted) next to the existing `downtimeA`/`downtimeB` band overlay (~1028–1046), following the `tl-band tier-a/tier-b` CSS pattern (add `tl-band phase`); tooltip = name, span, downtime within.
- `DashboardView.tsx` prog card (~666–685): when `analysis.phases` exists, name the wipe phase — "You wiped in **P4: Kefka Says**" (phase containing `terminalDeathSec`, else match `h.lastPhase` by id).
- No backend changes; savage renders nothing.

## PR 4 — Per-phase metrics, ref aggregation, deviations, sliced ceiling

**New `jobs/_core/phase_metrics.py`** (pure, job-agnostic — consumes `JobData.gauges`, never job names):
- `GaugePhaseStats {name, entry, exit, generated, spent, overcapped}`; `PhaseMetrics {phase_id, partial, active_s, gcd_casts, total_casts, casts_by_ability, gauges, pot_used}`.
- `compute_phase_metrics(norm_casts, phases, gauges, downtime_windows, *, end_s=None, tincture_windows=(), is_gcd=None)`:
  - Gauge replay = one forward walk per `GaugeModel` (same family as `entry_gauge.measure_entry_gauge` / `overcap.compute_overcap_for_gauge`): entry seeded via `measure_entry_gauge` (deepest-deficit → continuations never go negative), clamp `[0, cap]`, `"all"` spenders spend current balance, clamped surplus accumulates `overcapped`; record balance at each boundary → entry/exit. v1 ignores `cap_boosts` / `spend_hook` conditionals (documented; thresholds absorb noise).
  - `end_s` truncates the walk (wipe scored end; also reused for refs' time-matched partial-phase prefix). `active_s` = span − downtime overlap (deaths not subtracted in v1 — note in code). `is_gcd` via `ability_metadata` (hermetic under conftest stub).
- `aggregate_phase_metrics(per_ref)` → per-phase-id median/p25/p75 for gcd_casts, gcd rate (per active s), per-gauge exit/overcapped/spent/generated, pot-phase vote %, per-ability medians.
- `detect_deviations(user, agg, phases, *, ref_count)` — all suppressed when `ref_count < 5`; only phase ids present for both sides:
  - `gauge_exit` (completed, non-final phases): `|user.exit − median| > max(0.25·cap, 1.5·IQR)` → "Refs bank ~80 Kenki into P4; you entered with 20" (both directions).
  - `overcap_phase`: `user.overcapped > p75` and ≥2 units above median.
  - `pot_phase`: ≥70% of refs pot in phase p; user potted elsewhere / not at all despite reaching p.
  - `gcd_pace` (completed phases ≥20 s): user GCD rate < ref median × 0.93.

**Sliced ceiling** (the second half of the confirmed v1 scope): per-phase delivered-vs-idealized from the **existing whole-fight artifacts** — no new sim runs:
- Delivered side: sum per-cast scored potency of the user's casts with timestamp inside each phase; idealized side: same sum over the whole-fight idealized sim timeline's casts. If per-cast potency isn't already exposed by the `_score_timeline` adapters, add a per-cast potency breakdown hook to the shared scoring scaffolding (`jobs/_core/sim/scoring.py`) rather than per-job code.
- Attribution rule: a cast's full potency belongs to the phase containing its timestamp (DoT/buff spillover across boundaries is a documented approximation).
- Emit per phase `deliveredPotency` / `idealizedPotency` and frame in UI as **"share of the whole-fight ceiling delivered in this phase"** — explicitly *not* a per-phase re-optimized ceiling (that's the deferred v2).

**`sidecar/main.py`**: new `_build_phase_analysis(job, you, refs) -> dict | None` called from `_build_response` right after the `phases` emission; `None` unless `you.phases`. User metrics with `end_s = scored_end`; each ref segmented by **its own** `r.phases`; for the user's final *partial* phase, recompute refs' that-phase metrics with `end_s = ref_phase.start_s + (user_scored_end − user_phase.start_s)` (elapsed-time-matched prefix). Deviations skipped for locked-healer runs. Per-ability medians only where `|user − median| ≥ 2` (`notableCasts`); `_ensure_ability_meta` for those ids. Emitted as `"phaseAnalysis"`.

Wire shape (camelized; mirror in `contract.ts`):
```ts
phaseAnalysis?: {
  user: { phaseId: number; partial: boolean; activeSec: number; gcdCasts: number; totalCasts: number;
          deliveredPotency: number; idealizedPotency: number;
          gauges: { name: string; entry: number; exit: number; generated: number; spent: number; overcapped: number }[];
          potUsed: boolean }[];
  refs: { phaseId: number; refCount: number;
          gcdCasts: Stat3; gcdRate: Stat3;                       // Stat3 = {median, p25, p75}
          gauges: { name: string; exit: Stat3; overcapped: Stat3; spent: Stat3; generated: Stat3 }[];
          potPct: number;
          notableCasts: { abilityId: number; yourCasts: number; refMedian: number }[] }[];
  deviations: { phaseId: number; kind: 'gauge_exit'|'overcap_phase'|'pot_phase'|'gcd_pace';
                gauge?: string; abilityId?: number; yourValue: number; refValue: number; text: string }[];
};
```

Tests (`tests/test_phase_metrics.py`): conservation invariant per phase (`entry + generated − spent − overcapped == exit`); `"all"` spender; cap clamp; entry seeding; truncation equivalence (`end_s=t` ≡ walk over casts ≤ t); aggregation medians on synthetic refs; every deviation trigger + the `ref_count < 5` suppression; sliced-ceiling sums partition the whole-fight totals (Σ per-phase == whole-fight, both sides); `_build_phase_analysis` → `None` for `phases=()`. Re-run `test_contract_snapshot.py` — byte-identical.

## PR 5 — Dashboard phase panel + E2E verification

- **New `src/views/PhasePanel.tsx`**, rendered by DashboardView when `analysis.phaseAnalysis` is present, inserted directly above `<JobPanels/>` (DashboardView.tsx:1087). Gated by *data presence*, not the JobProfile registry (registry is job-driven; this panel is encounter-driven — deliberate).
  - Per-phase rows: name + span + reached/partial/not-reached chip; active time; GCDs vs ref median (count + rate); phase delivered/idealized share; per-gauge exit "you 20 / refs ~80" mini-bar; overcap; pot chip vs `potPct`.
  - Deviation callouts beneath, reusing the prog card's `.findings .finding` markup; severity frontend-side *without* `severityFor` (no lostPotency): `warn` for `gauge_exit`/`pot_phase`, `info` otherwise. NOT merged into the improvements ledger.
  - Partial-phase annotation: "compared over the first M:SS of this phase (time-matched vs refs)".
- **Fixture capture** (best-effort, separate commit): follow `python/scripts/add_*_fixtures.py` to capture a real Dancing Mad kill + wipe carrying `phaseTransitions` + `Report.phases`; pin segmentation + `phaseAnalysis` presence. Synthetic tests stand alone if impractical.

## Verification (end-to-end)

1. `cd python; python -m pytest -n auto` (all suites; `test_contract_snapshot.py` byte-identical in PRs 2/4).
2. `npm run build` + `npm run lint`; `npm run dev` for mock-tab sanity.
3. `npm run tauri dev` (run-app skill): Ultimates tab → Dancing Mad listed even with 0 kills → source defaults to "In progress" → paste/discover a wipe → run → prog card names the wipe phase, PhasePanel renders (phases the user reached only), Timeline shows phase bands. Then a Dancing Mad **kill** (all phases completed, deviations vs top-10).
4. Savage regression: run a savage kill → no phase bands/panel; Savage tab behaves exactly as the old Encounter tab.

## Follow-ups (explicitly deferred)

Phase-aware kill projection (consume the `ProjectionInputs.last_phase/phase_transitions` seam with per-phase ref burn rates); per-phase re-optimized sim ceiling (entry_gauge-seeded per-phase sims); per-phase CountsView filter; zero-kill synthesized rows for savage; potency-pricing deviations into the improvements ledger; healer phase deviations; `spend_hook`/`cap_boosts` fidelity in the phase gauge replay; more ultimates (FRU = encounters.py entry + gate sweep; legacy ults need per-level job data — separate project).

## Top risks

1. **Phase-data quirks** (looping/repeated phase ids in some ultimates, absent transitions, unnamed phases) → defensive `phase_segments` (sort/dedupe/clamp, `P{n}` fallback), data-presence gating, refs segmented by their own transitions, deviations restricted to shared phase ids with ≥5 refs.
2. **Savage byte-identity regression** → absent-key discipline for `phases`/`phaseAnalysis`, additive `ModuleResult` default, `test_contract_snapshot.py` with zero regeneration expected.
3. **`_SUMMARY_FIELDS_V` bump cold-invalidates all cached summaries** → acceptable by design (batched summaries, size-capped cache); `test_refs_cache.py` is symbolic and survives.
4. **Gauge replay model error** (RPR free spends, MCH `"all"` Queen battery, GNB cap_boosts) → entry seeding, conservative thresholds (0.25·cap + IQR), conservation-invariant tests; fidelity deferred.
5. **Two-tab state/routing confusion** (job confirmation loss, fallbacks landing on the wrong tab) → lift job into App state at `onJobConfirmed`, route fallbacks via persisted `lastSetupTab`, key each SetupView instance by `(character, category)`.

## Critical files

- `python/sidecar/main.py` (get_catalog, list_setup, `_build_response`, new `_build_phase_analysis`)
- `python/jobs/__init__.py` (analyze_pull phase stash), new `python/jobs/_core/phases.py` + `phase_metrics.py`, `python/jobs/_core/module_result.py`
- `python/fflogs_api.py` (`_REPORT_SUMMARY_FIELDS`) + `python/sidecar/dev_cache.py` (`_SUMMARY_FIELDS_V` 3→4)
- `python/encounters.py` (`encounter_category`)
- `src/App.tsx`, `src/views/SetupView.tsx`, `src/state/appState.ts`, `src/state/persist.ts`, `src/sidecar/contract.ts` (+ mock.ts), `src/state/refsPrefetch.ts`
- `src/views/TimelineView.tsx`, `src/views/DashboardView.tsx`, new `src/views/PhasePanel.tsx`
