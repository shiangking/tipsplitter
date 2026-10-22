#!/usr/bin/env python3
"""E6 — persistent unattended product execution (the factory entrypoint).

Submits a product idea and drives the full Phase E pipeline:

    idea -> design stage (E1, mandatory baseline)
         -> spec + architecture (GLM) -> parallel build (E4 workers)
         -> verification gates (E2) -> bounded repair loop (E3)
         -> integration -> delivery prep (E5, gated)
         -> evidence-based completion report

Design decisions honored:
  - SQLite persistence via the existing controller pattern (no new infra);
    every state transition is recorded -> restart-safe (`factory.py resume`).
  - Design-first is structurally enforced (compiler refuses to emit frontend
    tasks without an approved design baseline).
  - Untrusted exec gates stay BLOCKED without recorded human approval
    (execution_policy); unattended mode never silently runs them.
  - Product-quality acceptance is BLOCKED while no vision-capable profile
    exists (E2-A evidence; Phase E condition 2) — the report says so.
  - Human approval only where mandated: vision profile addition, GitHub
    push/PR/repo-creation/merge/deploy.

Usage:
    python3 factory.py new "<product idea>" [--name NAME] [--template ID]
    python3 factory.py status <project_id>
    python3 factory.py resume <project_id>
    python3 factory.py report <project_id>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from agent_client import ProductTask, run_task          # noqa: E402
import design                                            # noqa: E402
import gates.runner as runner                            # noqa: E402
from execution_policy import approve_exec, exec_allowed  # noqa: E402
from github import release_readiness                     # noqa: E402
from parallel import (check_manifests, create_worktree,  # noqa: E402
                      init_project_repo, integrate, run_parallel)
from vision_probe import vision_available                # noqa: E402

FACTORY_DB = ROOT / "state" / "factory.db"
PROJECTS_ROOT = ROOT / "projects"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT, idea TEXT,
  status TEXT NOT NULL DEFAULT 'created',   -- created|design|building|verifying|
                                            -- repairing|delivering|completed|blocked|interrupted
  stage TEXT, template_id TEXT, project_dir TEXT,
  orchestration_tag TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS stages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT, stage TEXT, status TEXT,  -- pending|running|done|blocked|failed
  evidence_json TEXT, started_at TEXT, finished_at TEXT,
  UNIQUE(project_id, stage)
);
"""


def db() -> sqlite3.Connection:
    FACTORY_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(FACTORY_DB)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _stage(project_id: str, stage: str, status: str, evidence: dict | None = None):
    c = db()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    c.execute("INSERT INTO stages (project_id, stage, status, evidence_json, "
              "started_at, finished_at) VALUES (?,?,?,?,?,?) "
              "ON CONFLICT(project_id, stage) DO UPDATE SET status=excluded.status, "
              "evidence_json=excluded.evidence_json, finished_at=excluded.finished_at",
              (project_id, stage, status, json.dumps(evidence or {}, default=str),
               now, now))
    c.execute("UPDATE projects SET status=?, updated_at=? WHERE id=?",
              (status, now, project_id))
    c.commit()


def get_project(project_id: str) -> dict:
    row = db().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise SystemExit(f"unknown project: {project_id}")
    return dict(row)


# --------------------------------------------------------------------------
# pipeline stages
# --------------------------------------------------------------------------
def stage_design(project: dict, template_id: str | None) -> dict:
    pdir = Path(project["project_dir"])
    _stage(project["id"], "design", "running")
    ref_src = f"template:{template_id}" if template_id else "template:clean-dashboard-v1"
    ref = design.ingest_reference(ref_src, pdir)
    if not ref.get("ok"):
        _stage(project["id"], "design", "blocked", {"error": ref.get("error")})
        return {"ok": False, "error": ref.get("error")}
    spec = design.build_design_spec(pdir, [ref], ref["system_id"],
                                    project["idea"], vision_available())
    _stage(project["id"], "design", "done",
           {"baseline": spec["baseline"]["type"],
            "template": ref["system_id"], "spec_sha256": spec["spec_sha256"]})
    return {"ok": True, "spec": spec}


def stage_plan(project: dict) -> dict:
    """GLM: product spec + architecture + worker manifests."""
    pdir = Path(project["project_dir"])
    _stage(project["id"], "plan", "running")
    spec = design.require_design_approval(pdir)
    ws = pdir / "agent-workspaces" / "plan"
    ds = spec["baseline"]["design_system"] or {}
    prompt = f"""You are the LEAD ARCHITECT for a new software product.

Product idea: {project['idea']}

Approved design system: {ds.get('title', 'clean-dashboard-v1')}
(design tokens and components: {pdir}/design/design-system.json)

Produce TWO files in your workspace:
1. SPEC.md — product specification: features (max 5, focused MVP), user
   journeys, non-goals, data model.
2. ARCHITECTURE.md — technical plan: exact file list with one-line purpose
   per file, module boundaries, and a WORKER SPLIT section proposing exactly
   two parallel workers with DISJOINT file ownership manifests:
   End the document with these EXACT machine-readable lines (no backticks,
   no table, one line each, space-separated relative file paths):
   WORKER-A: index.html styles.css
   WORKER-B: bill_math.py
   The entry UI file must be named index.html (single-page, no build step,
   pure HTML/CSS/JS) so it can be verified in a real browser.
Rules: pure Python stdlib for any backend logic; no external services; no
credentials; keep it small and genuinely functional. Reply PLAN-COMPLETE."""
    task = ProductTask(task_id="plan", role="planner", profile="glm5.3",
                       prompt=prompt, workspace=ws, timeout_s=1500,
                       max_iterations=60,
                       tags={"task": "plan", "role": "planner",
                             "project": project["id"]},
                       artifacts=["SPEC.md", "ARCHITECTURE.md"])
    rec = run_task(task, project["orchestration_tag"])
    # the artifacts ARE the deliverable: judge success by their presence and
    # the conversation outcome, not by the polling race
    artifacts_ok = all((ws / a).exists() and (ws / a).stat().st_size > 0
                       for a in ("SPEC.md", "ARCHITECTURE.md"))
    rec["succeeded"] = bool(artifacts_ok and
                            rec["result"]["status"] in ("finished", "idle", "timeout"))
    _stage(project["id"], "plan", "done" if rec["succeeded"] else "failed",
           {"conversation_id": rec["conversation_id"],
            "served_model": rec["result"]["usage"].get("served_model"),
            "tokens": rec["result"]["usage"], "spec_sha256": spec["spec_sha256"]})
    return rec


def manifests_from_arch(arch_text: str) -> dict:
    """Parse worker ownership manifests from ARCHITECTURE.md.
    Accepts (a) strict 'WORKER-A: f1 f2' lines and (b) markdown table/list
    rows that contain both a file token and a WORKER-A/WORKER-B marker.
    Returns {} when unparsable — the caller must block, never guess."""
    m = {"worker-a": [], "worker-b": []}
    for line in arch_text.splitlines():
        s = line.strip()
        for key in ("worker-a", "worker-b"):
            up = key.upper()
            if s.upper().startswith(up + ":"):
                toks = [t.strip(",;`*") for t in s[len(up) + 1:].replace(",", " ").split()]
                m[key] += [t for t in toks if t.endswith((".py", ".html", ".css", ".js"))]
    if m["worker-a"] or m["worker-b"]:
        return {k: list(dict.fromkeys(v)) for k, v in m.items() if v}
    for line in arch_text.splitlines():
        s = line.strip()
        has_a, has_b = "WORKER-A" in s.upper(), "WORKER-B" in s.upper()
        if not (has_a or has_b):
            continue
        for tok in s.replace("`", " ").replace("*", " ").replace("|", " ").replace(",", " ").split():
            if tok.endswith((".py", ".html", ".css", ".js")):
                m["worker-a" if has_a else "worker-b"].append(tok.strip(",;"))
    out = {k: list(dict.fromkeys(v)) for k, v in m.items() if v}
    return out if out else {}


def stage_build(project: dict, plan_rec: dict) -> dict:
    """Two parallel DeepSeek workers with worktree isolation (E4)."""
    pdir = Path(project["project_dir"])
    _stage(project["id"], "build", "running")
    arch = (pdir / "agent-workspaces" / "plan" / "ARCHITECTURE.md").read_text()
    init_project_repo(pdir)

    manifests = manifests_from_arch(arch)
    if not manifests:
        _stage(project["id"], "build", "blocked",
               {"reason": "unparsable worker split in ARCHITECTURE.md (no "
                          "WORKER-A:/WORKER-B: lines or ownership table)"})
        return {"ok": False, "blocked": True,
                "reason": "unparsable worker split"}
    ck = check_manifests(manifests)
    if not ck["ok"]:
        _stage(project["id"], "build", "blocked",
               {"reason": "overlapping ownership manifests", "conflicts": ck["conflicts"]})
        return {"ok": False, "blocked": True, "conflicts": ck["conflicts"]}

    wts = {}
    for wid in manifests:
        wts[wid] = create_worktree(pdir, wid)

    def make_fn(wid: str, wt: Path, files: list[str], stop_flag=None):
        other = [f for w, fl in manifests.items() for f in fl if w != wid]
        prompt = f"""You are a SOFTWARE ENGINEER implementing one part of a product.

Your workspace (an isolated git worktree): {wt}
Product idea: {project['idea']}

Architecture plan:
{arch}

You OWN exactly these files: {files}
Do NOT create or modify any other files (especially not: {other}).
Keep the implementation small, functional and clean. The UI entry must be
index.html (single page, inline CSS/JS, responsive, no build step), styled to
conform to the design tokens in {pdir}/design/design-system.json.
Reply BUILD-COMPLETE when done."""
        task = ProductTask(task_id=wid, role="engineer", profile="deepseekv4.1",
                           prompt=prompt, workspace=wt, timeout_s=1500,
                           max_iterations=80,
                           tags={"task": wid, "role": "engineer",
                                 "project": project["id"]})
        rec = run_task(task, project["orchestration_tag"], stop_flag)
        # deterministic product check: every owned file must exist and be
        # non-empty after the worker finishes (vacuous gates never hide this)
        missing = [f for f in files
                   if not (wt / f).exists() or (wt / f).stat().st_size == 0]
        rec["owned_files_missing"] = missing
        rec["status"] = "succeeded" if (rec["succeeded"] and not missing)             else "failed"
        return rec

    def make_runner(wid_, wt_, files_):
        def fn(stop_flag=None):
            return make_fn(wid_, wt_, files_, stop_flag)
        return fn
    runners = [{"id": wid, "depends_on": [],
                "fn": make_runner(wid, Path(wts[wid]["worktree"]),
                                  manifests[wid])}
               for wid in manifests]
    results = run_parallel(runners, max_workers=2)
    failed_workers = [r for r in results if r.get("status") != "succeeded"]
    _stage(project["id"], "build", "done" if not failed_workers else "failed",
           {"workers": [{k: r.get(k) for k in ("id", "status", "conversation_id",
                                             "error")} for r in results],
            "manifests": manifests,
            "worktrees": {k: v["worktree"] for k, v in wts.items()},
            "branches": {k: v["branch"] for k, v in wts.items()}})
    if failed_workers:
        return {"ok": False, "failed_workers": failed_workers}
    return {"ok": True, "results": results, "worktrees": wts,
            "manifests": manifests}


def stage_verify(project: dict, build_rec: dict) -> dict:
    """Verify each worker worktree, integrate, verify integrated tree."""
    pdir = Path(project["project_dir"])
    _stage(project["id"], "verify", "running")
    worker_branches, all_ok = [], True
    from repair import repair_loop
    for wid, wtinfo in build_rec["worktrees"].items():
        wt = Path(wtinfo["worktree"])
        entry = wt / "index.html"
        g = runner.run_static_suite(wt, entry if entry.exists() else None,
                                    pdir / "gate-out" / wid)
        if any(x["status"] == "failed" for x in g):
            # bounded repair ON the worker worktree, routed by gate family
            rep = repair_loop(wt, project["id"], project["orchestration_tag"],
                              entry if entry.exists() else None, g,
                              pdir / "agent-workspaces" / f"repair-{wid}")
            _stage(project["id"], f"repair-{wid}", "done",
                   {"outcome": rep["outcome"], "cycles": rep["cycles"]})
            g = runner.run_static_suite(wt, entry if entry.exists() else None,
                                        pdir / "gate-out" / f"{wid}-post")
        ok = all(x["status"] in ("passed", "unavailable") for x in g)
        all_ok &= ok
        if ok:
            subprocess.run(["git", "add", "-A"], cwd=str(wt), capture_output=True)
            subprocess.run(["git", "commit", "-q", "-m", f"{wid}: implement",
                            "--allow-empty"], cwd=str(wt), capture_output=True)
            worker_branches.append(wtinfo["branch"])
    if not all_ok:
        _stage(project["id"], "verify", "failed",
               {"reason": "worker worktree failed gates before integration"})
        return {"ok": False, "reason": "worker gates failed"}
    integ = integrate(pdir, worker_branches)
    if integ["conflicts"]:
        _stage(project["id"], "verify", "blocked",
               {"reason": "merge conflicts", "conflicts": integ["conflicts"]})
        return {"ok": False, "blocked": True, "conflicts": integ["conflicts"]}
    entry = pdir / "index.html"
    gates = runner.run_static_suite(pdir, entry if entry.exists() else None,
                                    pdir / "gate-out" / "integrated")
    summary = runner.summarize(gates)
    _stage(project["id"], "verify", "done", summary)
    return {"ok": True, "gates": gates, "summary": summary,
            "integration": integ}


def stage_repair(project: dict, verify_rec: dict) -> dict:
    """E3 repair loop (bounded) on the integrated tree."""
    from repair import repair_loop
    pdir = Path(project["project_dir"])
    _stage(project["id"], "repair", "running")
    res = repair_loop(pdir, project["id"], project["orchestration_tag"],
                      pdir / "index.html", verify_rec["gates"],
                      pdir / "agent-workspaces")
    _stage(project["id"], "repair", "done", {"outcome": res["outcome"],
                                             "cycles": res["cycles"]})
    return res


def stage_deliver(project: dict, gates: list[dict], repair_rec: dict) -> dict:
    """Delivery prep (autonomous) + release-readiness report.
    Push/PR remain approval-gated (github.py) and are NOT attempted here."""
    pdir = Path(project["project_dir"])
    _stage(project["id"], "deliver", "running")
    delivery = {"repo": "not-created (gated: requires approval)",
                "branch": subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(pdir),
                    capture_output=True, text=True).stdout.strip(),
                "push": "blocked (requires recorded approval + GITHUB_TOKEN)",
                "pull_request": "prepared, not opened (gated)"}
    report = release_readiness(pdir, gates, repair_rec, delivery)
    (pdir / "COMPLETION-REPORT.json").write_text(json.dumps(report, indent=2,
                                                            default=str))
    _stage(project["id"], "deliver", "done",
           {"report": "COMPLETION-REPORT.json",
            "product_quality": report["product_quality_acceptance"]["status"]})
    return report


# --------------------------------------------------------------------------
def _stage_status(project_id: str, stage: str) -> str:
    row = db().execute("SELECT status FROM stages WHERE project_id=? AND stage=?",
                       (project_id, stage)).fetchone()
    return row["status"] if row else "pending"


def compile_and_run(project_id: str, resume: bool = False) -> int:
    project = get_project(project_id)
    pdir = Path(project["project_dir"])

    # restart-safe: completed stages are never re-run on resume
    if resume and _stage_status(project_id, "design") == "done":
        print("resume: design stage already complete, skipping", flush=True)
        d = {"ok": True}
    else:
        d = stage_design(project, project.get("template_id"))
    if not d.get("ok"):
        print(json.dumps({"project": project_id, "stage": "design",
                          "status": "blocked", "detail": d}, indent=1))
        return 2
    if resume and _stage_status(project_id, "plan") == "done":
        print("resume: plan stage already complete, skipping", flush=True)
        plan_rec = {"succeeded": True}
    elif resume and _stage_status(project_id, "plan") in ("failed", "running"):
        # recovery: a plan conversation may have finished (artifacts on disk)
        # even though the stage was marked failed (timeout race / server 5xx)
        ws = pdir / "agent-workspaces" / "plan"
        if all((ws / a).exists() and (ws / a).stat().st_size > 0
               for a in ("SPEC.md", "ARCHITECTURE.md")):
            print("resume: recovering plan stage from on-disk artifacts",
                  flush=True)
            _stage(project_id, "plan", "done",
                   {"recovered_from_artifacts": True,
                    "spec_sha256": __import__("hashlib").sha256(
                        (ws / ".." / ".." / "design" / "design_spec.json")
                        .resolve().read_bytes()).hexdigest()})
            plan_rec = {"succeeded": True}
        else:
            plan_rec = stage_plan(project)
    else:
        plan_rec = stage_plan(project)
    if not plan_rec.get("succeeded"):
        print(json.dumps({"project": project_id, "stage": "plan",
                          "status": "failed"}, indent=1))
        return 3
    build = None
    if resume and _stage_status(project_id, "build") == "done":
        row = db().execute("SELECT evidence_json FROM stages WHERE "
                           "project_id=? AND stage='build'",
                           (project_id,)).fetchone()
        ev = json.loads(row["evidence_json"] or "{}")
        if ev.get("worktrees") and ev.get("manifests") and ev.get("branches"):
            print("resume: build stage already complete, skipping", flush=True)
            wts = {k: {"worker_id": k, "worktree": v, "branch":
                       ev["branches"][k], "reused": True}
                   for k, v in ev["worktrees"].items()}
            build = {"ok": True, "results": [], "worktrees": wts,
                     "manifests": ev["manifests"]}
        else:
            # legacy evidence row: re-derive from plan + standard layout
            print("resume: build evidence lacks worktree info; re-deriving",
                  flush=True)
            arch = (pdir / "agent-workspaces" / "plan" /
                    "ARCHITECTURE.md").read_text()
            manifests = manifests_from_arch(arch)
            if manifests:
                wts = {}
                for wid in manifests:
                    wt = pdir.parent / f"{pdir.name}-worktrees" / f"{wid}-attempt01"
                    if wt.exists():
                        wts[wid] = {"worker_id": wid, "worktree": str(wt),
                                    "branch": f"worker/{wid}", "reused": True}
                if len(wts) == len(manifests):
                    build = {"ok": True, "results": [], "worktrees": wts,
                             "manifests": manifests}
    if build is None:
        build = stage_build(project, plan_rec)
    plan_attempts = 1
    while not build.get("ok") and build.get("conflicts") and plan_attempts < 2:
        # plan-level bounded retry: GLM must resolve the manifest overlap
        _stage(project_id, "plan", "running",
               {"reason": "manifest overlap; re-planning",
                "conflicts": build["conflicts"]})
        plan_rec = stage_plan_repair(project, build["conflicts"], plan_attempts)
        if not plan_rec.get("succeeded"):
            break
        build = stage_build(project, plan_rec)
        plan_attempts += 1
    if not build.get("ok"):
        print(json.dumps({"project": project_id, "stage": "build",
                          "status": "blocked", "detail": build}, indent=1))
        return 4
    verify = stage_verify(project, build)
    if not verify.get("ok"):
        print(json.dumps({"project": project_id, "stage": "verify",
                          "status": "blocked" if verify.get("blocked") else "failed",
                          "detail": verify}, indent=1))
        return 5
    repair = stage_repair(project, verify)
    final_gates = runner.run_static_suite(
        pdir, pdir / "index.html" if (pdir / "index.html").exists() else None,
        pdir / "gate-out" / "final")
    report = stage_deliver(project, final_gates, repair)

    final_status = "completed" if repair["outcome"] == "passed" else \
        ("blocked" if "blocked" in repair["outcome"] else "completed_with_issues")
    c = db()
    c.execute("UPDATE projects SET status=? WHERE id=?", (final_status, project_id))
    c.commit()
    print(json.dumps({"project": project_id, "final_status": final_status,
                      "repair": {"outcome": repair["outcome"],
                                 "cycles": repair["cycles"]},
                      "product_quality_acceptance":
                          report["product_quality_acceptance"],
                      "report": str(pdir / "COMPLETION-REPORT.json")}, indent=1))
    return 0 if final_status == "completed" else 6


# --------------------------------------------------------------------------
def cmd_new(args):
    pid = f"proj-{int(time.time())}-{args.name or 'product'}".replace(" ", "-")
    pdir = PROJECTS_ROOT / pid
    pdir.mkdir(parents=True, exist_ok=True)
    c = db()
    c.execute("INSERT INTO projects (id, name, idea, status, template_id, "
              "project_dir, orchestration_tag, created_at, updated_at) "
              "VALUES (?,?,?,?,?,?,?,?,?)",
              (pid, args.name or pid, args.idea, "created", args.template,
               str(pdir), f"factory-{int(time.time())}",
               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    c.commit()
    print(json.dumps({"project_id": pid, "dir": str(pdir),
                      "template": args.template or "clean-dashboard-v1"}, indent=1))
    if args.run:
        return compile_and_run(pid)
    return 0


def cmd_status(args):
    p = get_project(args.project_id)
    stages = [dict(r) for r in db().execute(
        "SELECT stage, status, finished_at FROM stages WHERE project_id=? "
        "ORDER BY id", (args.project_id,))]
    print(json.dumps({**p, "stages": stages}, indent=1))


def cmd_report(args):
    p = get_project(args.project_id)
    rp = Path(p["project_dir"]) / "COMPLETION-REPORT.json"
    print(rp.read_text() if rp.exists() else json.dumps(
        {"error": "no completion report yet", "status": p["status"]}, indent=1))


def main():
    ap = argparse.ArgumentParser(description="Product Factory (Phase E)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new"); n.add_argument("idea"); n.add_argument("--name")
    n.add_argument("--template", default=None); n.add_argument("--run",
                                                               action="store_true")
    n.set_defaults(fn=cmd_new)
    rz = sub.add_parser("resume"); rz.add_argument("project_id")
    rz.set_defaults(fn=lambda a: compile_and_run(a.project_id, resume=True))
    s = sub.add_parser("status"); s.add_argument("project_id")
    s.set_defaults(fn=cmd_status)
    r = sub.add_parser("report"); r.add_argument("project_id")
    r.set_defaults(fn=cmd_report)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())


def stage_plan_repair(project: dict, conflicts: list[dict], attempt: int) -> dict:
    """Bounded plan-level retry after a compile-time manifest conflict."""
    pdir = Path(project["project_dir"])
    ws = pdir / "agent-workspaces" / "plan"
    arch_path = ws / "ARCHITECTURE.md"
    prev = arch_path.read_text() if arch_path.exists() else ""
    prompt = f"""You are the LEAD ARCHITECT. Your previous worker split was REJECTED
at compile time: overlapping file ownership between WORKER-A and WORKER-B.
Conflicting files: {json.dumps(conflicts)}

Rewrite the WORKER SPLIT section of ARCHITECTURE.md (edit the file in place at
{arch_path}) so the manifests are strictly DISJOINT — every file belongs to
exactly one worker. Shared modules must be assigned to a single worker or split
into two files. Keep everything else unchanged.
Reply SPLIT-FIXED when done."""
    task = ProductTask(task_id=f"plan-repair-{attempt}", role="architect",
                       profile="glm5.3", prompt=prompt, workspace=ws,
                       timeout_s=420, max_iterations=30,
                       tags={"task": "plan-repair", "attempt": str(attempt),
                             "role": "architect", "project": project["id"]},
                       artifacts=["ARCHITECTURE.md"])
    rec = run_task(task, project["orchestration_tag"])
    return rec
