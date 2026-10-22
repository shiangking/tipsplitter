# Phase E — OpenHands Product Factory: Closing Report

**Date:** 2026-10-22 · **Scope:** E1–E6, approved with two conditions
(execution policy; vision-blocked product acceptance) — both honored in code.

## What the factory now does end-to-end (all demonstrated with real agent runs)
```
idea → design (design-first, preapproved template or analyzed reference)
     → GLM 5.3 planning (SPEC.md + ARCHITECTURE.md with disjoint worker manifests)
     → 2 parallel DeepSeek workers in isolated git worktrees
     → per-worker verification gates (secret_scan, sast, dep_audit, env_handling,
       responsive_screenshots, dom_check desktop/tablet/mobile)
     → per-worker bounded repair (routed by gate family) → re-verify
     → integration (merge conflicts detected, never silent) → integrated gates
     → integrated repair → delivery report (repo/push/PR gated by approval policy)
     → persistent SQLite ledger at every stage; `factory.py resume` continues
       an interrupted run from its last completed stage
```

## Deliverables
- `factory.py` — unattended entrypoint (`new --run`, `resume`, `status`).
- `design.py`, `gates/runner.py`, `repair.py`, `parallel.py`, `github.py`,
  `execution_policy.py`, `vision_probe.py`, `agent_client.py`, `controller.py`.
- Evidence: `state/E1…`–`E6-EVIDENCE.md`, `docs/VISION-CAPABILITY.md`,
  `state/factory.db` (per-stage ledger), `COMPLETION-REPORT.json` per project.
- Demo products built by the system itself: TipSplitter (E6) and the E4
  parallel-worktree demo project.

## Final E6 result (proj-1792690256-tipsplitter)
- All stages `done`; 4 real conversations: plan/GLM `67981c29…` (served
  x-test-1), workers `fd14ba0e…`, `55993ee6…` (x-test-2), repair `30a1d045…`
  (x-test-2).
- Integrated gates: 0 failed; `passed_with_unavailable` (dep_audit full audit
  is exec+network gated — recorded honestly, never counted as passed).
- `final_status: completed_with_issues`, `unresolved: []`.
- `product_quality_acceptance: BLOCKED` (vision unavailable — condition 2).
- Delivery autonomous actions only; repo creation/push/PR require recorded
  approval + token (enforced by `execution_policy.approve_exec`).

## Conditions honored
1. **Execution policy** — every unattended exec-carrying action (including all
   GitHub mutations) routes through `execution_policy.approve_exec`; the
   isolation probe (`genuine_isolation: false`) is the recorded justification.
2. **Vision-blocked acceptance** — `vision_available()` is the single source
   of truth; no visual gate counts toward acceptance without it, and the
   completion report states exactly why acceptance is blocked.

## Known limitations
1. dep_audit full audit requires approved exec + network; recorded
   `unavailable`, never silently passed.
2. Product-quality acceptance stays BLOCKED until a vision-capable profile is
   added and passes the two-round probe.
3. GitHub push/PR/merge require user-supplied token + recorded approval
   (by design); Actions status via `gh` is likewise token-gated.
4. Agent Server intermittently 500s on conversation creates under load
   ("NoneType has no len()"); mitigated by serialized creates, payload
   validation, patient exponential backoff, and resumable stages.
5. Manifest parsing accepts two documented formats; anything else blocks and
   triggers a bounded plan-repair (max 1) — correctness over availability.
6. Unattended wall-clock budget: a GLM plan cycle can take ~10–25 min
   (250K+ prompt tokens observed); timeouts are stage-specific.
7. Two agents, not N: the worker split comes from the plan (2 workers by
   design for the POC); scaling to N requires manifest-schema generalization.
8. Restart safety is stage-level: an interrupted agent conversation mid-stage
   is re-run on resume (its partial artifacts may be reused only where the
   stage logic allows it — currently plan artifacts and clean worktrees).

## Definition-of-done check
| Requirement | Evidence |
|---|---|
| E1 design-first | `projects/*/design/design_spec.json` gate; frontend tasks cannot compile without an approved baseline |
| E2 gate families | `gates/runner.py` + planted-defect acceptance (E2-EVIDENCE.md) |
| E3 repair | `repair.py`; live run with anti-gaming C3/C4 (E3-EVIDENCE.md) |
| E4 parallel | `parallel.py`; D1–D4 PASS (E4-EVIDENCE.md) |
| E5 GitHub | `github.py`; E5a–F PASS, gated actions (E5-EVIDENCE.md) |
| E6 unattended | `factory.py`; end-to-end run + interruption/recovery (E6-EVIDENCE.md) |
