# E6 Acceptance Evidence — persistent unattended factory execution

Date: 2026-10-22 · project: proj-1792690256-tipsplitter ("TipSplitter")
Entrypoint: `python3 factory.py new --idea "…" --name tipsplitter --template clean-landing-v1 --run`
(and `python3 factory.py resume <project_id>` after interruptions)

## End-to-end pipeline (all REAL conversations, per-stage ledger in state/factory.db)
| Stage | Status | Evidence |
|---|---|---|
| design | done | template `clean-landing-v1`, design_spec sha256 `06949a88…` |
| plan | done | GLM (served `openai/x-test-1`) conv `67981c29-93c0-4ce9-b089-15844af62fc7`, 250,293 tokens; SPEC.md + ARCHITECTURE.md with disjoint worker manifests |
| build | done | 2 PARALLEL DeepSeek workers: worker-a `fd14ba0e-1d58-4075-a5d9-b13c35e932c2` (index.html, styles.css), worker-b `55993ee6-47d1-4709-9ffc-1423f14b1be8` (bill_math.py), both served `openai/x-test-2` |
| verify (worker gates) | failed→repaired | worker-b ruff B017 blind-assert defect caught pre-integration; DeepSeek repair conv `30a1d045-2e6c-4b42-a5e1-41857f74cb62` (served openai/x-test-2) on the worktree; sast re-run: passed |
| verify (integrated) | done | `passed_with_unavailable` — security 3 passed / 1 unavailable, browser 4 passed (desktop+tablet+mobile), 0 failed |
| repair (integrated) | done | outcome `unavailable_components` (dep_audit full-audit is exec+network gated), 0 real defects |
| deliver | done | COMPLETION-REPORT.json; repo **not created**, push/PR **gated** (approval + token required) |

`final_status: completed_with_issues` — issues are availability constraints,
not defects (`unresolved: []`).

## Unattended behavior (condition 2 honored)
`product_quality_acceptance: BLOCKED — "LLM vision review unavailable (no
vision-capable profile; see docs/VISION-CAPABILITY.md). Per Phase E condition 2,
unattended product-quality acceptance is blocked."` — the factory completed all
work it may do autonomously and refused to self-accept product quality.

## Persistence & recovery (demonstrated on a real interrupted run)
The launch-failure window (runs 7–8) left the project at `build blocked` with
design/plan artifacts on disk. `factory.py resume` then:
- skipped completed stages from the SQLite ledger,
- recovered the plan stage from on-disk artifacts (timeout race),
- reused existing worktrees (resume-safe `create_worktree`),
- re-launched ONLY the failed work (2 workers, then 1 repair).

## Defects found by the POC's own verification (and fixed in-session)
1. stop_flag closure bug in stage_build runner → server 500s on `tags.task=null`
   (logs showed `[None]` task ids); fixed with proper closure + payload
   validation at the client boundary (regression test added).
2. `sast` treated empty python sources as failure → now vacuous pass.
3. `env_handling` demanded `.env.example` with no env usage → now conditional.
4. Owned-files existence check added (vacuous gates can no longer hide an
   empty worktree).
5. Plan timeout race: artifact-based success criterion + 1500s budget.
6. Concurrent conversation creates → serialized with lock + settle window;
   patient exponential backoff (0/5/10/20/40/60s) for 5xx; 4xx non-retryable.
7. Manifest parser accepts both strict `WORKER-A:` lines and markdown tables;
   unparsable plans BLOCK (never guessed) and trigger one bounded plan-repair.

## Run IDs
All conversation IDs above are queryable:
`GET /api/conversations/{id}` (session key required, never logged).
Orchestration tag for the project: `factory-1792690256`.
