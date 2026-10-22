#!/usr/bin/env python3
"""E3 — autonomous repair loop with routing and anti-gaming rules.

When a gate fails:
  1. classify: family + defect type → owning agent profile
       security  → engineer (deepseek) then MANDATORY independent reviewer (glm)
       technical → engineer (deepseek)
       browser/visual → designer (qwen)
       architecture-level failures → architect (glm)
  2. build an evidence-rich repair prompt (gate JSON, failing checks, files)
  3. run repair as a REAL agent conversation (agent_client)
  4. re-run the failing gates; run regression (full static suite)
  5. anti-gaming:
       - gate specs are controller-owned; repair prompt instructs the agent
         never to weaken tests; post-repair diff checks test/gate files
       - test-file modifications → mandatory independent reviewer task
       - unproductive-loop breaker: no improvement for N cycles → escalate
         to architect once → then blocked
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from agent_client import ProductTask, run_task
import gates.runner as runner

MAX_CYCLES = 3
UNPRODUCTIVE_LIMIT = 2
TEST_MARKERS = ("test_", "/tests/", "_test.", "spec", "verify")


def sha256_of(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else ""


def classify(family: str, failed_checks: list[str]) -> dict:
    if family == "security":
        return {"role": "engineer", "profile": "deepseekv4.1",
                "requires_independent_review": True}
    if family in ("browser", "visual"):
        return {"role": "designer", "profile": "qwen3.8flash",
                "requires_independent_review": False}
    if family == "technical":
        return {"role": "engineer", "profile": "deepseekv4.1",
                "requires_independent_review": False}
    return {"role": "architect", "profile": "glm5.3",
            "requires_independent_review": False}


def touched_test_files(project_dir: Path, since_sha: dict[str, str]) -> list[str]:
    """Diff detector: which test/spec files changed since the repair started?"""
    changed = []
    for p in project_dir.rglob("*"):
        if not p.is_file() or any(s in p.parts for s in
                                  (".git", "node_modules", ".venv", "__pycache__")):
            continue
        rel = str(p.relative_to(project_dir))
        if any(m in rel.lower() for m in TEST_MARKERS):
            if sha256_of(p) != since_sha.get(rel):
                changed.append(rel)
    return changed


def snapshot_hashes(project_dir: Path) -> dict[str, str]:
    snap = {}
    for p in project_dir.rglob("*"):
        if p.is_file() and not any(s in p.parts for s in
                                   (".git", "node_modules", ".venv", "__pycache__")):
            snap[str(p.relative_to(project_dir))] = sha256_of(p)
    return snap


def build_repair_prompt(gate_result: dict, project_dir: Path,
                        entry_hint: str, cycle: int) -> str:
    failed = [c for c in gate_result["checks"] if c.get("ok") is False
              and c.get("ran", True)]
    ev = json.dumps({"gate": gate_result["gate"], "family": gate_result["family"],
                     "failed_checks": failed,
                     "evidence": {k: v for k, v in gate_result.get("evidence", {}).items()
                                  if k in ("dom", "screenshots", "hits")}},
                    default=str, indent=1)
    return f"""You are the REPAIR ENGINEER for a software product (repair cycle {cycle}).

Project directory (your workspace): {project_dir}
Entry UI file (if relevant): {entry_hint}

A verification gate FAILED. Fix the defects in the project files. Rules:
1. Fix the ROOT CAUSE in product code. NEVER edit, weaken, or delete tests,
   verification specs, or gate definitions — that is a violation and will be
   detected by an independent reviewer.
2. Keep changes minimal and targeted.
3. Do not touch files outside the project directory.
4. When done, reply with REPAIR-COMPLETE and a one-line summary per fix.

Failing gate evidence (machine-generated):
{ev}
"""


def review_prompt(project_dir: Path, changed_tests: list[str],
                  gate_result: dict) -> str:
    return f"""You are an INDEPENDENT REVIEWER (architecture/correctness).

Project directory (inspect it): {project_dir}

A repair agent just modified these test/verification files:
{changed_tests or "(none)"}

The failing gate was: {gate_result['gate']} ({gate_result['family']}).
Determine whether the repair fixed the real defect or weakened/cheated the
verification (edited tests to pass, deleted assertions, loosened specs).
Inspect the actual files. Reply with exactly one line first:
VERDICT: APPROVE   or   VERDICT: REJECT
then a short justification.
"""


# --------------------------------------------------------------------------
def repair_loop(project_dir: Path, project_id: str, orchestration_tag: str,
                entry_html: Path, initial_gates: list[dict],
                workspaces_dir: Path, stop_flag: list | None = None,
                max_cycles: int = MAX_CYCLES) -> dict:
    """Bounded repair loop. Returns a structured outcome record."""
    history, cycle = [], 0
    last_scores = []
    gates = initial_gates

    while cycle < max_cycles:
        failed_gates = [g for g in gates if g["status"] == "failed"]
        if not failed_gates:
            break
        cycle += 1
        for g in failed_gates:
            cls = classify(g["family"], [c["check"] for c in g["checks"]
                                         if c.get("ok") is False])
            before = snapshot_hashes(project_dir)
            ws = workspaces_dir / f"repair-{g['gate'].replace(':', '-').replace('/', '-')}-c{cycle}"
            task = ProductTask(
                task_id=f"repair-{g['gate']}-c{cycle}", role=cls["role"],
                profile=cls["profile"],
                prompt=build_repair_prompt(g, project_dir, str(entry_html), cycle),
                workspace=ws, timeout_s=600, max_iterations=60,
                tags={"task": f"repair-{g['gate']}", "attempt": str(cycle),
                      "role": cls["role"], "project": project_id},
                artifacts=[])
            if stop_flag and stop_flag[0]:
                return {"outcome": "interrupted", "cycles": cycle, "history": history}
            rec = run_task(task, orchestration_tag, stop_flag)
            after = snapshot_hashes(project_dir)
            changed_tests = touched_test_files(project_dir, before)

            # anti-gaming: test/spec edits => mandatory independent review
            review = None
            if changed_tests:
                rws = workspaces_dir / f"review-c{cycle}"
                rtask = ProductTask(
                    task_id=f"review-c{cycle}", role="reviewer", profile="glm5.3",
                    prompt=review_prompt(project_dir, changed_tests, g),
                    workspace=rws, timeout_s=420, max_iterations=30,
                    tags={"task": "independent-review", "attempt": str(cycle),
                          "role": "reviewer", "project": project_id})
                review = run_task(rtask, orchestration_tag, stop_flag)
                verdict = ""
                fr = (review.get("collected") or {}).get("final_response") or ""
                for line in fr.splitlines():
                    if line.strip().upper().startswith("VERDICT:"):
                        verdict = line.split(":", 1)[1].strip().upper()
                        break
                review["verdict"] = verdict or "UNPARSEABLE"

            # re-verify: rerun failing gate family + full regression
            rerun = runner.run_static_suite(project_dir, entry_html,
                                            workspaces_dir / f"gate-out-c{cycle}")
            still_failed = [x for x in rerun if x["status"] == "failed"]
            score = len(rerun) - len(still_failed)
            last_scores.append(score)

            entry = {"cycle": cycle, "gate": g["gate"], "family": g["family"],
                     "routed_to": cls["profile"], "conversation_id": rec["conversation_id"],
                     "agent_succeeded": rec["succeeded"], "served_model":
                     rec.get("result", {}).get("usage", {}).get("served_model"),
                     "changed_test_files": changed_tests,
                     "independent_review": (None if review is None else
                                            {"verdict": review["verdict"],
                                             "conversation_id": review["conversation_id"]}),
                     "gates_after": runner.summarize(rerun),
                     "regression_clean": not still_failed}
            history.append(entry)

            # unproductive-loop breaker
            if len(last_scores) >= UNPRODUCTIVE_LIMIT and \
                    len(set(last_scores[-UNPRODUCTIVE_LIMIT:])) == 1 and \
                    still_failed:
                # no improvement for N cycles -> architect escalation, once
                esc = ProductTask(
                    task_id=f"architect-escalation-c{cycle}", role="architect",
                    profile="glm5.3",
                    prompt=("You are the ARCHITECT. A repair loop failed to make "
                            "progress on this project: " + str(project_dir) +
                            "\nAnalyze the remaining gate failures and propose a "
                            "structural fix as a numbered list."),
                    workspace=workspaces_dir / "architect-escalation",
                    timeout_s=420, max_iterations=30,
                    tags={"task": "architect-escalation", "attempt": str(cycle),
                          "role": "architect", "project": project_id})
                escalation = run_task(esc, orchestration_tag, stop_flag)
                history.append({"cycle": cycle, "event": "architect_escalation",
                                "conversation_id": escalation["conversation_id"]})
                return {"outcome": "blocked_unproductive", "cycles": cycle,
                        "history": history, "remaining_failures":
                        [x["gate"] for x in still_failed]}

            # independent review rejection => treat cycle as failed, count it
            if review and review.get("verdict") == "VERDICT: REJECT":
                return {"outcome": "blocked_review_rejected", "cycles": cycle,
                        "history": history, "remaining_failures":
                        [x["gate"] for x in still_failed]}

            gates = rerun

    final = runner.summarize(gates)
    outcome = ("passed" if final["overall"] == "passed"
               else ("unavailable_components" if
                     any(g["status"] == "unavailable" for g in gates)
                     else "failed_gates_remain"))
    return {"outcome": outcome, "cycles": cycle, "history": history,
            "final": final}
