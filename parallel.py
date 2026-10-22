#!/usr/bin/env python3
"""E4 — dependency-aware parallel multi-agent execution.

Isolation model:
  - a shared project GIT REPO (init'd by this module)
  - each worker gets its own GIT WORKTREE + branch (filesystem isolation via
    git, not via directories — enables clean integration and conflict detection)
  - each worker has an explicit FILE-OWNERSHIP manifest (from the architecture
    plan); overlapping manifests are a compile-time error
  - integration: merge verified worker branches into an integration branch,
    re-run the gate suite on the integrated tree; conflicts -> GLM resolution
    task with both diffs as evidence

Scheduler: topological wave execution — tasks whose dependencies are all
satisfied run concurrently (bounded by max_workers, default 2). Sequential
mode (max_workers=1) remains the fallback everywhere.

The controller stays the sole writer of persistent state; worker conversations
only touch their own worktree.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from agent_client import ProductTask, run_task

MAX_WORKERS = 2  # readiness to raise: see phase-e-plan §E4 (measured throughput)


def sh(cmd: list[str], cwd: Path, timeout_s: int = 120) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       timeout=timeout_s)
    return p.returncode, (p.stdout + p.stderr).strip()


# --------------------------------------------------------------------------
# repo + worktree management
# --------------------------------------------------------------------------
def init_project_repo(project_dir: Path) -> dict:
    """git init + initial commit (idempotent)."""
    gitdir = project_dir / ".git"
    if not gitdir.exists():
        rc, out = sh(["git", "init", "-q"], project_dir)
        if rc != 0:
            raise RuntimeError(f"git init failed: {out}")
    rc, out = sh(["git", "config", "user.email", "orchestrator@product-factory.local"],
                 project_dir)
    rc, out = sh(["git", "config", "user.name", "Product Factory Orchestrator"],
                 project_dir)
    sh(["git", "add", "-A"], project_dir)
    rc, out = sh(["git", "commit", "-q", "-m", "baseline before parallel workers",
                  "--allow-empty"], project_dir)
    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"], project_dir)[1]
    return {"repo": str(project_dir), "baseline_branch": branch}


def create_worktree(project_dir: Path, worker_id: str, attempt: int = 1) -> dict:
    """Create an isolated git worktree + branch for a worker.
    Resume-safe: an existing worktree/branch from a previous attempt is
    REUSED only if clean; otherwise a fresh attempt directory is created."""
    wt_root = project_dir.parent / f"{project_dir.name}-worktrees"
    wt_root.mkdir(exist_ok=True)
    branch = f"worker/{worker_id}"
    wt = wt_root / f"{worker_id}-attempt{attempt:02d}"
    if wt.exists():
        dirty = sh(["git", "status", "--porcelain"], wt)[1].strip()
        if not dirty:
            return {"worker_id": worker_id, "worktree": str(wt), "branch": branch,
                    "reused": True}
        wt = wt_root / f"{worker_id}-attempt{attempt:02d}-{int(time.time())}"
    rc, out = sh(["git", "worktree", "add", "-b", branch, str(wt)], project_dir)
    if rc != 0:
        # branch may already exist from an interrupted run
        if "already exists" in out:
            rc, out = sh(["git", "worktree", "add", str(wt), branch], project_dir)
        elif "is a missing but already registered worktree" in out or "already registered" in out:
            sh(["git", "worktree", "prune"], project_dir)
            rc, out = sh(["git", "worktree", "add", str(wt), branch], project_dir) if \
                (wt.exists() is False) else (rc, out)
        if rc != 0:
            # last resort: fresh timestamped worktree on a new branch
            sh(["git", "worktree", "prune"], project_dir)
            wt = wt_root / f"{worker_id}-attempt{attempt:02d}-{int(time.time())}"
            sh(["git", "branch", "-D", branch], project_dir)
            rc, out = sh(["git", "worktree", "add", "-b", branch, str(wt)],
                         project_dir)
            if rc != 0:
                raise RuntimeError(f"worktree add failed: {out}")
    return {"worker_id": worker_id, "worktree": str(wt), "branch": branch}


def check_manifests(manifests: dict[str, list[str]]) -> dict:
    """Ownership manifests must be disjoint — overlap is a compile-time error."""
    owners: dict[str, str] = {}
    conflicts = []
    for worker, files in manifests.items():
        for f in files:
            f = f.strip("/")
            if f in owners and owners[f] != worker:
                conflicts.append({"file": f, "workers": [owners[f], worker]})
            owners[f] = worker
    return {"ok": not conflicts, "conflicts": conflicts}


def worktree_changes(project_dir: Path, wt: Path) -> list[str]:
    rc, out = sh(["git", "status", "--porcelain"], wt)
    changed = [ln[3:].strip() for ln in out.splitlines() if ln.strip()]
    return changed


def manifest_violation(manifest: list[str], changed: list[str]) -> list[str]:
    """Files changed outside the worker's declared ownership."""
    owned = {f.strip("/") for f in manifest}
    return [c for c in changed if c not in owned]


def commit_worktree(wt: Path, message: str) -> str:
    sh(["git", "add", "-A"], wt)
    rc, out = sh(["git", "commit", "-q", "-m", message], wt)
    rc2, sha = sh(["git", "rev-parse", "HEAD"], wt)
    return sha if rc2 == 0 else ""


def integrate(project_dir: Path, worker_branches: list[str],
              integration_branch: str = "integration") -> dict:
    """Merge verified worker branches into the integration branch."""
    sh(["git", "checkout", "-q", integration_branch], project_dir) if \
        integration_branch in sh(["git", "branch", "--list", integration_branch],
                                 project_dir)[1] else \
        sh(["git", "checkout", "-q", "-b", integration_branch], project_dir)
    merged, conflicts = [], []
    for br in worker_branches:
        rc, out = sh(["git", "merge", "--no-ff", "-q", "-m",
                      f"integrate {br}", br], project_dir)
        if rc == 0:
            merged.append(br)
        else:
            conflicted = [ln.split()[-1] for ln in
                          sh(["git", "status", "--porcelain"], project_dir)[1]
                          .splitlines() if ln.strip()[:2] in ("UU", "AA", "DD", "AU",
                                                              "UA", "DU", "UD")]
            conflicts.append({"branch": br, "files": conflicted})
            sh(["git", "merge", "--abort"], project_dir)
    return {"merged": merged, "conflicts": conflicts}


# --------------------------------------------------------------------------
# wave scheduler
# --------------------------------------------------------------------------
def waves(tasks: list[dict]) -> list[list[dict]]:
    """Topological waves: each wave = tasks whose deps are in prior waves."""
    done, out = set(), []
    pending = list(tasks)
    while pending:
        ready = [t for t in pending
                 if all(d in done for d in t.get("depends_on", []))]
        if not ready:
            raise RuntimeError(f"dependency cycle among: "
                               f"{[t['id'] for t in pending]}")
        out.append(ready)
        done |= {t["id"] for t in ready}
        pending = [t for t in pending if t["id"] not in done]
    return out


def run_parallel(task_runners: list[dict], max_workers: int = MAX_WORKERS,
                 stop_flag: list | None = None) -> list[dict]:
    """task_runners: [{id, depends_on, fn}] where fn(stop_flag) -> record.
    Runs ready tasks concurrently (threads), waves in dependency order."""
    import threading
    results, waves_list = [], waves(task_runners)
    for wave in waves_list:
        ready = wave[:max_workers] if max_workers else wave
        # (overflow beyond max_workers waits for the next slot — simple barrier)
        threads, out_map = [], {}
        lock = threading.Lock()

        def wrapped(t):
            if stop_flag and stop_flag[0]:
                out_map[t["id"]] = {"id": t["id"], "status": "interrupted"}
                return
            try:
                rec = t["fn"](stop_flag)
            except Exception as e:  # noqa: BLE001
                rec = {"id": t["id"], "status": "error", "error": str(e)[:300]}
            with lock:
                out_map[t["id"]] = rec

        for t in ready:
            th = threading.Thread(target=wrapped, args=(t,))
            th.start()
            threads.append(th)
            if len(threads) >= max_workers:
                for th in threads:
                    th.join()
                threads = []
        for th in threads:
            th.join()
        results.extend(out_map.values())
        # blocked if any in this wave failed hard (dependents can't run)
        if any(r.get("status") in ("error", "interrupted") for r in out_map.values()):
            for t in [x for x in task_runners
                      if x["id"] not in {r["id"] for r in results}]:
                results.append({"id": t["id"], "status": "blocked"})
            break
    return results


def verify_worker_output(project_dir: Path, wt: Path, manifest: list[str],
                         gates_fn) -> dict:
    """Deterministic verification before integration: ownership + gates."""
    changed = worktree_changes(project_dir, wt)
    violations = manifest_violation(manifest, changed)
    gate_res = gates_fn(wt)
    return {"changed_files": changed, "ownership_violations": violations,
            "gates": gate_res, "integrable": not violations and
            all(g["status"] in ("passed", "unavailable") for g in gate_res)}
