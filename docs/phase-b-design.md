# Phase B — Design: Minimal Orchestration Controller

Status: PROPOSED (awaiting approval) · Project: `agent-orchestration-poc` (disposable)

## 1. Goals

Coordinate three independent OpenHands agent runs through a **real** multi-run pipeline:

```
GLM 5.3 (plan) ──► DeepSeek V4.1 Flash (implement) ──► deterministic verify ──► GLM 5.3 (review)
                └─► Qwen 3.8 Flash (frontend, parallel track)
```

with persistent task state, dependency tracking, timeouts, bounded retries, independent
verification, and failure reporting. Sequential execution (plan → implement → verify → review),
with the Qwen frontend task launched after the plan (it does not depend on the backend artifacts).
No parallel writes to a shared workspace until isolation is verified — each task gets its own
workspace directory; artifacts are copied between workspaces by the controller.

## 2. Shape (smallest maintainable controller)

```
agent-orchestration-poc/
├── controller.py        # single-file orchestrator (~400 lines, stdlib only)
├── tasks.yaml           # example task definitions (assignments + dependencies + verify cmds)
├── verify.py            # deterministic verifier (subprocess, no LLM)
├── report.py            # structured report generator (reads SQLite + run logs)
├── workspaces/          # per-task isolated workspaces (git-ignored, disposable)
├── artifacts/           # collected outputs per run (copied from workspaces)
├── state/               # orchestration.db (SQLite) + controller.log (redacted)
└── docs/                # phase-a-findings.md, this design, final report
```

**Deliberate choices**
- **Stdlib only** (urllib, sqlite3, json, argparse, subprocess, signal, pathlib) → no
  dependency installation, no approval friction, nothing to break. PyYAML avoided by using
  JSON for task definitions? — no: `tasks.yaml` kept as JSON-compatible YAML subset parsed
  by a 20-line loader, or plain JSON. Final call at implementation: `tasks.json` if it keeps
  us dependency-free.
- **One file for orchestration logic.** The REST calls it needs are ~8 endpoints; a framework
  would be bigger than the feature.
- **SQLite** for task metadata (per objective) — `state/orchestration.db`.

## 3. Data model

```sql
CREATE TABLE tasks (
  id TEXT PRIMARY KEY,            -- slug, e.g. "plan"
  title TEXT, kind TEXT,          -- plan | implement | frontend | verify_gate | review
  model_profile TEXT NOT NULL,    -- glm5.3 | deepseekv4.1 | qwen3.8flash
  depends_on TEXT,                -- JSON array of task ids
  prompt_template TEXT,           -- may reference {artifact:plan} etc.
  verify_command TEXT,            -- deterministic command, run in artifacts dir
  timeout_s INTEGER, max_retries INTEGER,
  status TEXT,                    -- pending|running|succeeded|failed|blocked|skipped
  attempts INTEGER, last_error TEXT,
  created_at TEXT, updated_at TEXT
);

CREATE TABLE runs (              -- one row per attempt, links to the OpenHands conversation
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, attempt INTEGER,
  conversation_id TEXT,           -- OpenHands conversation UUID (the "agent run id")
  llm_profile_ref TEXT,           -- requested model profile
  served_model TEXT,              -- metrics.model_name (actual)
  launch_profile TEXT,            -- launched_agent_profile.llm_profile_ref (actual)
  token_usage TEXT,               -- accumulated_token_usage JSON (invocation evidence)
  status TEXT,                    -- running|succeeded|failed|timeout|interrupted
  started_at TEXT, finished_at TEXT, duration_s REAL, error TEXT
);

CREATE TABLE artifacts (         -- what each run produced / what was handed to the next run
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, task_id TEXT, path TEXT, sha256 TEXT, bytes INTEGER, collected_at TEXT
);

CREATE TABLE verify_results (    -- deterministic verification, independently recorded
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, run_id INTEGER, command TEXT, exit_code INTEGER,
  passed INTEGER, stdout_tail TEXT, stderr_tail TEXT, recorded_at TEXT
);

CREATE TABLE events (            -- controller audit log (redacted)
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, task_id TEXT, run_id INTEGER, message TEXT
);
```

Task state machine: `pending → running → succeeded | failed`, `failed → pending (retry, bounded)`
else `blocked` when retries exhausted **and** downstream tasks depend on it. A task whose
dependency failed/blocked becomes `skipped` (recorded as blocked-by-dependency in the report).

## 4. Launch template (per run, from verified Phase A mechanism)

```python
POST /api/conversations
{
  "conversation_id": <uuid4>,                       # pre-generated, stored in runs table
  "agent_profile_id": <default profile uuid>,       # read from GET /api/agent-profiles
  "agent_launch_additions": {"llm_profile_ref": task.model_profile},
  "workspace": {"kind": "LocalWorkspace", "working_dir": "<workspaces/<task>/<attempt>>"},
  "confirmation_policy": {"kind": "NeverConfirm"},
  "max_iterations": 50, "stuck_detection": True,
  "tags": {"poc": "agent-orchestration-poc", "task": <task.id>, "attempt": str(n)},
  "initial_message": {"role": "user", "run": True, "content": [{"type": "text", "text": prompt}]}
}
```

Credentials: the launch contains only a profile **name**; the server resolves the key from its
own store. No `X-Expose-Secrets` call is needed, no key is ever read, logged, or written.

## 5. Execution loop (sequential)

```
for task in topological_order(tasks):
    if not deps_succeeded(task): mark skipped/blocked; continue
    for attempt in 1..task.max_retries+1:
        record run(running); launch conversation
        wait_with_timeout(conversation, task.timeout_s)      # poll GET status every 3 s
          on timeout: POST /interrupt → mark run(timeout) → try next attempt
        if status == finished: collect_results() → mark succeeded; break
        else: record failure reason (AgentError/ConversationError events) → retry or fail
    on final failure: mark task failed/blocked; report and stop (fail-fast default)
```

Result collection per run:
1. `GET /agent_final_response` → `artifacts/<task>/final_response.md`
2. `GET /workspace/<path>` for declared expected outputs (e.g. `PLAN.md`, module files) →
   copied into `artifacts/<task>/…` + sha256 recorded
3. `metrics.model_name`, `launched_agent_profile.llm_profile_ref`,
   `accumulated_token_usage` → runs table (invocation evidence)
4. conversation events tail → `artifacts/<task>/events_tail.json` (redacted)

Verification gate (before review): run `verify_command` (e.g. `python -m pytest -q`) in a
sandboxed copy of collected artifacts; write `verify_results` row regardless of outcome;
the GLM review prompt embeds the verification result. **The verifier never calls an LLM.**

## 6. POC task definitions (example, in `tasks.yaml`)

| id | model | depends_on | produces | verify |
|---|---|---|---|---|
| `plan` | glm5.3 | — | `PLAN.md` (must contain marker `PLAN-ID: AOP-<run>` + task breakdown) | grep marker |
| `implement` | deepseekv4.1 | plan | `stringutils.py` + `test_stringutils.py` (from plan) | `python -m pytest -q` |
| `frontend` | qwen3.8flash | plan | `index.html` (standalone demo page using the module's documented API) | marker + `</html>` check |
| `review` | glm5.3 | implement, frontend | `REVIEW.md` (verdict: approve/request_changes + findings) | grep verdict line |

Transfer fidelity check: the `implement`/`frontend`/`review` prompts embed the **verbatim**
plan text (including the plan's unique marker); the report proves the marker appears in the
downstream conversation's events (proving the plan was actually transferred, not invented).

The tasks are intentionally small (single-file module + tests, single HTML page) so runs
complete quickly and deterministically, while still being real: real files, real tests, real review.

## 7. Safety measures

- Disposable project dir only; `git status -sb` clean start; no repo mutations.
- No credentials in prompts/logs/reports (profile names only); log redaction filter for
  anything matching key/token/secret patterns + the session key itself.
- Loopback API only; no ports opened; nothing deployed; no cloud resources.
- Conversation workspaces are fresh dirs under the project; deleted only by explicit `reset`.
- `cleanup` subcommand deletes POC conversations via `DELETE /api/conversations/{id}`.

## 8. Phase D evidence plan

| Claim | Evidence |
|---|---|
| Each model invoked | per-run `metrics.model_name`, `accumulated_token_usage` > 0, conversation ids |
| Correct model per task | `launch_profile` vs `served_model` vs task assignment table |
| Outputs transferred | plan marker present in downstream prompts (events) + artifact sha256 chain |
| States persisted | SQLite before/after dumps; restart resumes from disk |
| Failed ≠ successful | statuses in DB + report; one deliberate failure injection run (`--chaos` flag) |
| Verification independent | `verify_results` rows written by `verify.py` (no LLM), exit codes |
| Interrupt/recovery | demo: SIGINT controller mid-`implement` → restart → orphan detected → re-dispatch or `blocked` |

## 9. Known limitations (to be confirmed during Phase D)

1. Sequential execution only (by design until isolation is verified further); Qwen runs
   after the plan, before/after implement, but controller waits for each run.
2. File collection is per-file (no bulk archive endpoint used in POC).
3. `current_model_id` may be null post-run; use `metrics.model_name` + `stats`.
4. Wall-clock timeout enforced controller-side; server sees the interrupted status.
5. Single-host assumption (loopback API, local workspaces).

## 10. What I will NOT do without further approval

- Install Python packages (stdlib only, so none needed).
- Modify any global OpenHands setting, profile, or provider connection.
- Create automations, webhooks, or cloud resources.
- Run anything in parallel against a shared workspace.
