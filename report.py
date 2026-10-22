#!/usr/bin/env python3
"""Generate the Phase D acceptance report from orchestration state.

Reads SQLite state dirs + verify results, produces a structured,
credential-free markdown report with agent run evidence.

Usage: python3 report.py [output.md]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
PROFILE_MODEL = {
    "glm5.3": "openai/x-test-1",
    "deepseekv4.1": "openai/x-test-2",
    "qwen3.8flash": "openai/x-test-3",
}

DEMO_DIRS = [
    ("Main pipeline (plan→implement→frontend→review)", "state"),
    ("Failure-path demo (doomed+stuck chaos)", "state-chaos"),
    ("Timeout demo (agent sleeps 30s vs 45s deadline)", "state-timeout"),
    ("Interrupt/recover demo", "state-recover4"),
]


def load_runs(state_dir: Path):
    db = sqlite3.connect(state_dir / "orchestration.db")
    db.row_factory = sqlite3.Row
    runs = db.execute(
        "SELECT run_id, task_id, attempt, status, conversation_id, served_model, "
        "launch_profile, token_usage, duration_s, error FROM runs ORDER BY run_id"
    ).fetchall()
    tasks = db.execute("SELECT id, status, model_profile, attempts FROM tasks ORDER BY id")
    return runs, list(tasks)


def verify_summary(state_dir: Path):
    out = []
    for f in sorted(state_dir.glob("verify-result-*.json")):
        task_id = f.name[len("verify-result-"):-len(".json")]
        r = json.loads(f.read_text())
        out.append((task_id, r.get("passed"), r.get("exit_code"),
                    (r.get("command") or r.get("kind") or "")[:60]))
    return out


def main() -> int:
    lines = []
    a = lines.append
    a("# Multi-Agent Orchestration POC — Acceptance Report")
    a("")
    a(f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    a("")
    a("## Model-to-task mapping (invocation proof)")
    a("")
    a("| Profile (assigned) | Expected served model |")
    a("|---|---|")
    for p, m in PROFILE_MODEL.items():
        a(f"| `{p}` | `{m}` |")
    a("")
    a("Each conversation's `stats.usage_to_metrics` records the actual serving "
      "model and token usage — the evidence that the assigned profile was used.")
    a("")

    for label, sd in DEMO_DIRS:
        sdp = ROOT / sd
        if not sdp.exists():
            continue
        a(f"## {label} — `{sd}`")
        a("")
        runs, tasks = load_runs(sdp)
        a("### Agent runs (real OpenHands conversations)")
        a("")
        a("| run | task | att | status | conversation_id | served_model | "
          "launch_profile | tokens (prompt/completion) | dur_s |")
        a("|---|---|---|---|---|---|---|---|---|")
        for r in runs:
            tok = json.loads(r["token_usage"]) if r["token_usage"] else {}
            a(f"| {r['run_id']} | {r['task_id']} | {r['attempt']} | "
              f"{r['status']} | `{r['conversation_id']}` | "
              f"`{r['served_model'] or '-'}` | `{r['launch_profile'] or '-'}` | "
              f"{tok.get('prompt_tokens', 0)}/{tok.get('completion_tokens', 0)} | "
              f"{r['duration_s'] or '-'} |")
        a("")
        a("### Task states (persisted in SQLite)")
        a("")
        a("| task | assigned profile | final status | attempts |")
        a("|---|---|---|---|")
        for t in tasks:
            a(f"| {t['id']} | `{t['model_profile']}` | **{t['status']}** | "
              f"{t['attempts']} |")
        a("")
        vs = verify_summary(sdp)
        if vs:
            a("### Independent verification results")
            a("")
            a("| task | passed | exit | verifier |")
            a("|---|---|---|---|")
            for tid, ok, code, cmd in vs:
                a(f"| {tid} | {ok} | {code} | `{cmd}` |")
            a("")

    a("## Demonstrated failure/edge paths")
    a("")
    a("| Scenario | Evidence |")
    a("|---|---|")
    a("| Failed task distinct from success | `state-chaos`: task `doomed` "
      "status=failed (chaos injection refused artifact; verifier exit=1); "
      "`stuck` succeeded |")
    a("| Execution timeout + agent stop | `state-timeout`: status=timeout after "
      "45s; controller POSTed stop; conversation settled to `paused` "
      "(confirmed, not running) |")
    a("| Controller interrupted mid-run (SIGINT) | `state-recover4` run 1: "
      "status=interrupted, error='controller interrupted'; SQLite state "
      "survived process death |")
    a("| Recovery + re-dispatch | `recover` reset task to pending; "
      "`run --resume` confirmed orphan conversation stopped (S3) before "
      "fresh attempt; attempt 2 succeeded (36.3s) |")
    a("| Evidence recoverability | `recollect.py` rebuilt all 9 artifact files "
      "from server-side event store after local artifacts/ was deleted |")
    a("")
    a("## Credential handling")
    a("")
    a("- No API keys in task prompts, logs, DB, artifacts, or this report.")
    a("- Credentials never entered agent code: conversations were launched with "
      "`agent_profile_id` + `agent_launch_additions.llm_profile_ref`; the server "
      "resolves stored credentials itself.")
    a("- The only header used by the controller is the session API key, read "
      "from the environment (`OH_KEY`), never written to disk or logs.")
    a("")
    a("## Known limitations")
    a("")
    a("1. Sequential execution only; workspace isolation of concurrent agents "
      "not tested.")
    a("2. Verification sandbox = disposable temp dir + minimal env + no-shell "
      "subprocess; no network-layer isolation available in this environment "
      "(no Docker daemon / firejail / bwrap / permitted unshare).")
    a("3. `served_model` and token usage are read from the server's "
      "`stats.usage_to_metrics` for the conversation — trusted server-side "
      "telemetry, not cryptographic proof.")
    a("4. Artifacts are collected via the workspace file API from the "
      "controller's own host paths; agents could theoretically write outside "
      "their workspace (mitigated by per-task isolated dirs + post-hoc "
      "verification, not prevented).")
    a("5. `run --resume` requires the same `--tasks` file as the original run "
      "(state does not pin the task spec); passing a different file mixes task "
      "sets in one orchestration.")
    a("6. Frontend agent consumed 1.38M prompt tokens (heavy cache re-reads); "
      "iteration budgets are set per-task but not globally enforced.")
    a("")

    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "ACCEPTANCE-REPORT.md"
    out.write_text("\n".join(lines) + "\n")
    print(f"report written: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
