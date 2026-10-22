# Phase E Implementation Plan — Product Factory

**Status:** PROPOSED — awaiting approval
**Date:** 2026-10-22
**Basis:** Inspection of `agent-orchestration-poc/` (2,093 lines across 6 modules) + live environment probes executed this session.

---

## 1. What inspection found (evidence, not assumptions)

### 1.1 Reusable POC components (preserved, extended — not rebuilt)

| Component | Evidence of fitness | Phase E reuse |
|---|---|---|
| `controller.py` (988 L) | Acceptance-proven: 4-model pipeline, model evidence via `stats.usage_to_metrics`, timeout w/ stop-confirmation, SIGINT→`interrupted`→`recover`→re-dispatch loop | Core execution engine; gains parallel scheduler (E4), GitHub tasks (E5), product pipeline compiler (E1/E6) |
| `verify.py` (192 L) | Sandbox = fresh temp dir + minimal env + no-shell subprocess; unittest gate + static gates; never counts unavailable as passed | Gate engine substrate; new gate families plug into its result schema (`passed/checks/exit_code/evidence`) |
| `recollect.py` / `report.py` | Full artifact recovery from server event store proven | Extended for screenshots, PR links, gate evidence |
| SQLite state | Survived process death in acceptance tests | Add tables (§3.1) — no new infrastructure |
| Isolation probe | Proved per-conversation workspace, tool scoping, zero-token idle | Reused per project and per parallel worker |

### 1.2 Environment probes executed this session

| Probe | Result | Consequence |
|---|---|---|
| Chromium | **v154 headless works** (`--no-sandbox` required; dom-dump + valid 1280×800 PNG screenshots) | Browser gates possible without new infra; Playwright optional |
| Playwright (pip) | Wheel downloadable (1.64.0, aarch64); not installed | **Needs approval** — richer E2E (clicks, forms, multi-page) |
| Node/npm | v24.21.0, registry reachable (292 ms ping) | Real product builds (Vite/React) viable; per-project `npm install` needs approval |
| `gh` CLI | v2.103.0 installed; api.github.com reachable (HTTP 200) | E5 viable; `GITHUB_TOKEN` **not set** — user must supply (via `request_secret`) |
| Git identity | `user.name`/`user.email` unset in this environment | E5 must set per-project repo-local identity (not global) |
| Conversation API | `SendMessageRequest.content` accepts `ImageContent{image_urls}` — confirmed from OpenAPI schema | Vision gate *architecturally* possible — **empirical per-model test still required (E2-A)** |
| Secrets | `POST /api/conversations/{id}/secrets` exists | GitHub tokens can be injected server-side; never in prompts |
| Docker | Daemon unreachable (unchanged from Phase C) | Sandbox remains temp-dir + minimal-env + no-shell; network-layer isolation stays a documented limitation |
| Disk / egress | 206 GB free; npm + GitHub egress open | No storage or network blockers |

---

## 2. Architecture: smallest maintainable evolution

**Principle:** extend the proven controller; add new *modules* and *gate families*, not new infrastructure. No Redis, no queues, no Kubernetes. All state stays in the existing SQLite pattern.

```
agent-orchestration-poc/
├── controller.py          # EXTEND: parallel scheduler (E4), pipeline compiler hooks, GitHub tasks (E5)
├── verify.py              # EXTEND: gate registry (technical + product gates)
├── gates/                 # NEW: gate implementations, one file per family
│   ├── technical.py       #   build, typecheck, lint, unit, integration, migrations (E2)
│   ├── browser.py         #   chromium/playwright E2E, responsive shots, overflow, a11y (E2)
│   ├── visual.py          #   deterministic heuristics + LLM vision review (availability-gated) (E1/E2)
│   └── security.py        #   dep audit, secret scan, SAST, env handling (E2)
├── design.py              # NEW: reference ingestion, design-spec model, template library (E1)
├── repair.py              # NEW: failure classification + routing + bounded repair loop (E3)
├── parallel.py            # NEW: worktree isolation + integration + conflict handling (E4)
├── github.py              # NEW: repo/branch/commit/PR/Actions, token via conversation secrets (E5)
├── factory.py             # NEW: E6 entrypoint — product idea → full pipeline (unattended)
├── vision_probe.py        # NEW (E2-A): empirical per-model image-input test; writes VISION-CAPABILITY.md
├── templates/             # NEW: curated preapproved design systems (MIT-licensed)
└── state/ (SQLite)        # EXTENDED schema (below)
```

### 2.1 SQLite schema additions (migration, not replacement)

```sql
projects(id, name, idea_spec_json, status, created_at, updated_at)
design_specs(project_id, version, source, source_evidence_json, spec_json, approved_baseline, created_at)
gates(id, project_id, task_id, family, name, status,      -- ran|passed|failed|unavailable|blocked
      evidence_json, attempt, started_at, finished_at)
repairs(id, project_id, gate_id, classified_as, assigned_profile, instruction,
        repair_task_id, outcome, regression_ok, cycle, created_at)
workers(id, project_id, task_id, worktree_path, branch, status, launched_at)
integrations(id, project_id, strategy, base_branch, conflicts_json, outcome, evidence_json)
deliveries(id, project_id, repo_url, branch, pr_url, pr_number, actions_status,
           release_report_json, created_at)
```

---

## 3. Milestone plan (E1 → E6, in the requested priority order)

### E1 — Design-first development  *(Priority One)*

**Scope.** Mandatory design stage for frontend-heavy projects. Reference ingestion (URLs, screenshots, Figma exports, template library), design analysis → machine-readable `design_spec.json`, representative-screen build → render → screenshot → compare → repair loop.

**Files.** `design.py` (new), `gates/visual.py` (new), `templates/` (new, 2 seeded systems: a clean dashboard system and a marketing/landing system, both MIT-licensed), controller task-compiler changes, `vision_probe.py` (see E2-A).

**Workflow enforced in code:**
1. Pipeline compiler **refuses to emit** any frontend implementation task unless a `design_specs` row exists with `approved_baseline=true` (user-supplied reference analyzed, or curated template selected).
2. If no reference supplied: template library is consulted automatically (unattended-safe); user selection recorded as the acceptance baseline.
3. Representative-screen gate: Qwen builds ONE screen → controller screenshots it at 1280/768/375 widths → gate compares against design direction → defects routed back to Qwen (max 2 cycles) → only then does the full-frontend task unlock. **A visibly defective design never gets expanded across the app.**
4. Reference inspection claims are evidence-backed: URL fetches store the actual screenshot bytes; the design_spec records `source_evidence` (file hashes, capture timestamps). No "I looked at it" without pixels.

**Dependencies.** System chromium (present). Vision probe result (E2-A) determines whether LLM-based visual comparison is available; without it, comparison = deterministic heuristics (layout metrics, blank-region ratio, overflow detection, palette sampling via Pillow) and the vision component is marked `unavailable`.

**Acceptance tests.**
- A1: compiler run with a frontend task and no design spec → build fails fast with a clear error.
- A2: template-selected project → design_spec generated, representative screen built, 3 viewport screenshots exist on disk with nonzero size and distinct widths.
- A3: injected visual defect (e.g., broken CSS) → representative-screen gate fails, repair task routed to `qwen3.8flash`, cycle recorded, re-gate passes.
- A4: design_spec hash referenced by the final report matches the approved baseline (tamper-evidence).

**Failure/recovery.** Screen render timeout → gate `failed` with screenshot evidence; repair loop exhaustion → project `blocked` at design stage (not silently proceeded).

**Complexity.** Medium (2–3 sessions). **Limitations:** Figma support = exported JSON/images, not live API; URL references require public reachability; vision-compare quality depends on E2-A probe outcome.

---

### E2 — Browser, visual, functional & security verification  *(Priority Two)*

**E2-A — Vision capability probe (first, blocks everything visual).**
`vision_probe.py` launches one throwaway conversation per profile (`glm5.3`, `deepseekv4.1`, `qwen3.8flash`) sending a `SendMessageRequest` with an `ImageContent` data-URL PNG containing a known code rendered in the image, then checks whether the model's answer demonstrates actual image comprehension (answer verified against ground truth). Results written to `docs/VISION-CAPABILITY.md` + DB. **No vision gate may claim success based on an unverified model.**

**E2-B — Gate families** (all emit machine-readable evidence into `gates`):

| Family | Gates | Tooling |
|---|---|---|
| `technical` | build, typecheck, lint, unit, integration, migrations | per-project toolchain (npm/vite, mypy/ruff or equivalents chosen by GLM in architecture task) run in the existing no-shell sandbox pattern |
| `browser` | E2E flows (nav, forms, states: loading/empty/success/error), responsive 1280/768/375, horizontal-overflow detection, per-viewport screenshots | Playwright (approved install) **or** chromium-subprocess fallback if Playwright approval declined |
| `visual` | palette/typography conformance vs design_spec, blank-region & overflow heuristics, hierarchy metrics | Pillow (present) + optional LLM vision review (E2-A gated) |
| `security` | dependency audit (`pip-audit` / `npm audit`), secret scan (regex + entropy), SAST (`bandit` or `semgrep`), env-var handling check, auth/input-validation checklist | all controller-side, evidence JSON includes tool versions |

**Rule carried over unchanged:** an unavailable check is recorded `unavailable` and never counts as passed; the product-quality gate reports it as such.

**Dependencies (needing approval, bundled):** `pip install playwright pip-audit bandit` + per-project `npm install` at build time. No Docker.

**Acceptance tests.**
- B1: vision probe → per-profile verdict with evidence; Qwen result recorded either way.
- B2: demo product with 3 intentionally planted defects (unit-test failure, responsive overflow, fake AWS key in a fixture) → all three respective gates fail with evidence; fixed product → all pass.
- B3: gates table shows `unavailable` (not `passed`) when a tool is missing — verified by removing one tool from PATH in a test run.
- B4: every gate's evidence JSON is schema-valid and reproducible (re-run produces same verdict on unchanged inputs).

**Complexity.** High (largest milestone; 3–4 sessions). **Limitations:** no network-layer sandbox (documented); axe-core a11y via node sidecar if approved, else heuristics-only a11y marked accordingly.

---

### E3 — Autonomous repair & refinement

**Scope.** Gate failures → classification → routing → targeted repair → re-verify → regression → record. Multiple meaningful cycles, bounded.

**Files.** `repair.py` (new), controller integration, gates plumbing.

**Routing (as specified):** backend/technical → `deepseekv4.1`; frontend/visual/browser-UI → `qwen3.8flash`; architecture → `glm5.3`; security → responsible engineer + mandatory independent GLM review. Each repair task receives reproducible evidence (failing gate JSON, logs, screenshots, file paths) — never a vague "fix it".

**Anti-gaming rules enforced in code:**
- Acceptance criteria live in gate specs owned by the controller; repair prompts cannot alter them. **Weakening tests = immediate cycle abort + `blocked`.**
- Diff review: repairs touching test files or gate specs trigger an automatic independent GLM review task.
- No self-approval: a repair is "done" only when the *controller-owned* gate re-runs green.
- Unproductivity detector: N consecutive repair cycles with no gate-score improvement → stop, classify `unproductive_loop`, escalate to GLM for architecture-level diagnosis, then `blocked` if unresolved.

**Acceptance tests.**
- C1: planted backend bug → repair routed to deepseek, gate re-run green, regression suite green, `repairs` row complete.
- C2: planted frontend bug → routed to qwen (not deepseek) — routing correctness asserted.
- C3: repair attempt that edits a test to make it pass → detected, cycle aborted, status `blocked`, incident recorded.
- C4: two seeded unfixable defects → loop stops at bound, final report lists them as unresolved (not passed).

**Complexity.** Medium-high (2 sessions). **Limitations:** classification is prompt+heuristic based (GLM-assisted); misrouting possible → mitigated by review task and cycle bounds.

---

### E4 — Parallel multi-agent execution

**Scope.** Dependency-aware parallel workers with **git worktree** isolation (stronger than directories; enables clean integration), starting at 2 workers.

**Files.** `parallel.py` (new), controller scheduler changes (topological wave execution — existing `topological_order` already computes dependency structure; waves = ready-set parallelism), `integrations` flow.

**Design.**
- Worker tasks run in `worktrees/<task>-<attempt>` off a shared project repo; each worker owns an explicit file-ownership manifest (from the architecture plan); overlap between manifests = compile-time error.
- Integration task (after all workers verify green): merge worktree branches into the integration branch; conflicts → GLM-led conflict-resolution task with both diffs as evidence; re-run full gate suite on the integrated tree.
- Readiness to raise concurrency from 2: measured sustained throughput (tokens/min per profile during E2/E3 runs — recorded, not assumed), zero integration failures across ≥2 projects, workspace-isolation re-probe passing with parallel conversations live.

**Acceptance tests.**
- D1: 2 independent modules → 2 parallel conversations, disjoint worktrees (path assertion), both succeed, integration merges cleanly, integrated tree passes gates.
- D2: overlapping file manifests rejected at compile time.
- D3: injected merge conflict → conflict task launched, resolved or `blocked`, never silent.
- D4: SIGINT during parallel phase → all worker runs recorded `interrupted`, `recover` re-dispatches only unfinished workers.

**Complexity.** Medium-high (2 sessions). **Limitations:** agents in this environment share the host FS — worktrees + ownership manifests reduce, but cannot cryptographically prevent, cross-writes; documented.

---

### E5 — GitHub integration & staging delivery

**Scope.** Repo init, branches, milestone commits, push to an approved **private** repo, PR with evidence, Actions verification, release-readiness report.

**Files.** `github.py` (new), report.py extensions, delivery table flow.

**Mechanics & safety:**
- `GITHUB_TOKEN` obtained via `request_secret` (one call; referenced as env var, never echoed). Server-side injection into conversations via `POST /api/conversations/{id}/secrets` when agents need it — token never enters a prompt or log.
- Pre-push secret scan is a hard blocker (reuses E2 security gate output).
- **Approval gates (explicit):** creating a new GitHub repo; changing repo permissions; merging to main; deploying anywhere. **Not gated (autonomous per spec):** local repo init, branches, commits, preparing PR text, staging-prep artifacts, opening a PR *into the approved existing private repo* once the token exists.
- Actions verification: controller polls check-runs; failure → E3 repair loop with CI logs as evidence.
- Release-readiness report = gates summary + PR link + Actions runs + screenshots + unresolved issues; generated by extended `report.py`.

**Acceptance tests.**
- E1: project with token present → branch pushed to approved private repo, PR opens with evidence table + screenshots, nothing merged to main.
- E2: secret-scan blocker → push refused, incident recorded.
- E3: merge-to-main attempt without explicit approval → refused by code, not just policy.
- E4: token absent → E5 tasks become `blocked` with actionable message; rest of pipeline still completes locally.

**Dependencies.** GitHub PAT (user, via `request_secret`), an approved private repo name. **Complexity.** Medium (1–2 sessions). **Limitations:** Actions minutes/quota belong to the user's account; preview deployments require external hosting → out of scope unless requested (staging preview = PR + local artifact bundle + screenshots).

---

### E6 — Persistent unattended product execution

**Scope.** The end experience: submit idea → return to finished product. One command runs everything within the approval gates already defined.

**Files.** `factory.py` (new entrypoint), project-spec schema (`product.json`: idea, features, constraints, design preference optional), controller checkpointing (already exists via SQLite), watchdog.

**Flow:** `factory.py new "idea..."` → compiles product pipeline (spec task → architecture task → design stage (E1) → parallel build (E4, if manifests allow) → verification (E2) → repair cycles (E3) → integration → delivery-prep (E5) → completion report). Long runs are restart-safe: every state transition already persists; `factory.py resume` continues; a watchdog subcommand reaps stalled conversations (`stuck` detection exists server-side) and re-dispatches.

**Acceptance tests.**
- F1: full unattended run of a small real product (e.g., "personal finance tracker web app") → PR-ready branch + gates green or explicitly `blocked` with reasons + completion report; zero human input mid-run.
- F2: kill the controller mid-run; restart with `resume` → continues without duplicating completed tasks (idempotency via existing run state).
- F3: final report distinguishes TECHNICAL gate results, PRODUCT gate results, and `unavailable` gates — no silent conflation.

**Complexity.** Medium (1–2 sessions, mostly composition of prior milestones). **Limitations:** truly long products span model-context limits → mitigated by spec→plan→task decomposition and worktree scoping; throughput ceiling of the shared provider is the practical bound on concurrency.

---

## 4. Answers to the two mandated questions

**How design-first is enforced:** structurally, not by convention — the task compiler cannot emit frontend tasks without an approved `design_specs` row; the representative-screen gate is a hard dependency of the full-frontend task; reference claims require stored pixel evidence; the unattended default is a curated preapproved template so the baseline always exists. A defective representative screen blocks expansion (max 2 repair cycles, then blocked-for-review rather than propagate).

**How the product-quality gate differs from ordinary technical verification:** technical gates (build/test/security) are deterministic, binary, and fully ownable by the controller. Product gates (visual, usability, navigation clarity, hierarchy, responsiveness-as-experience) are *evidence-based judgments*: they combine deterministic heuristics (overflow, blank-ratio, palette/typography conformance, viewport integrity) with an LLM vision review **only if E2-A proves the vision capability**, and they report `unavailable` explicitly when a component (vision, a11y tooling) cannot run — the report then says "visual review unavailable", never "visual review passed". The product gate's acceptance baseline is the approved design system, and its failure routes to the *designer* agent (Qwen) with screenshots, not just to "make tests pass".

---

## 5. Dependencies requiring approval (bundled for one decision)

| Item | Purpose | Risk / mitigation |
|---|---|---|
| `pip install playwright pip-audit bandit` (+ `pytest` optional) | E2 browser E2E + security gates | Standard PyPI packages; versions pinned; venv-scoped |
| Per-project `npm install` at build time | Real product builds (Vite/React/etc.) | Confined to disposable project dirs |
| `GITHUB_TOKEN` (user-provided secret) + approved private repo name | E5 push/PR/Actions | Scoped token recommended (repo scope); injected via conversation secrets; never logged |

**No** new infrastructure (no Docker images, no Redis, no external services). Docker remains unavailable; its absence is compensated by the existing sandbox pattern and documented as a limitation.

## 6. Safety posture (delta from Phase C)

- All agent writes confined to project worktrees/workspaces (launch workspace + ownership manifests; verified by post-run diff scope check).
- Verifier and security gates run controller-side in the existing no-shell, temp-dir, minimal-env pattern.
- All existing guarantees preserved: credentials never in prompts/logs/reports; tokens via secrets API; Agent Server stays local; no global settings changes; approval gates for repo-creation/permissions/merge/deploy; git history and gate evidence are append-only records.
- New: pre-push secret scan hard-blocks delivery; test-file edits by repair agents trigger mandatory independent review; unproductive-loop breaker.

## 7. Suggested execution order & effort

| Milestone | Sessions | Risk |
|---|---|---|
| E2-A vision probe | 0.5 | Determines E1/E2 visual-gate design |
| E1 design-first | 2–3 | Medium |
| E2 verification | 3–4 | Highest (tool surface) |
| E3 repair loop | 2 | Medium |
| E4 parallel | 2 | Medium-high |
| E5 GitHub | 1–2 | Low-medium (token dependency) |
| E6 unattended | 1–2 | Composition |

Each milestone lands with its acceptance tests executed and evidence recorded in the same style as the Phase C acceptance report. Sequential mode remains available and is the fallback at every stage.
