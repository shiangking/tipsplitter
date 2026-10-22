#!/usr/bin/env python3
"""Deterministic verifier for agent-orchestration-poc.

Runs OUTSIDE any LLM. Two modes:
  1. agent-task verification  : python3 verify.py --task <id> --spec <verify.json> --dir <artifacts dir> --result <out.json>
  2. sandboxed unittest run   : python3 verify.py --sandbox-unittest --src <dir with collected artifacts> --out <result.json>

The unittest sandbox (safeguard 2) copies sources into a fresh temp directory and runs
`python3 -m unittest` as a subprocess with:
  - a fresh cwd (never the caller's workspace, never the agents' workspaces)
  - PYTHONDONTWRITEBYTECODE=1, no network-relevant env, minimal env
  - a hard wall-clock timeout; the child is killed on expiry
  - temp dir deleted afterwards
No shell is spawned: argv vectors only (no sh -c), so no shell injection surface.

This runtime is itself a disposable Linux container (verify with `uname`), so "disposable
sandbox" here means: throwaway directory + minimal-env subprocess + kill-on-timeout +
post-run deletion. Docker/unshare are unavailable INSIDE this container (documented
limitation in the acceptance report); the outer container boundary is the isolation layer.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def _tail(text: str, n: int = 4000) -> str:
    text = text or ""
    return text[-n:]


def run_unittest_sandbox(src_dir: Path, timeout_s: float) -> dict:
    """Copy collected sources to a fresh temp dir and run unittest discover there."""
    result: dict = {
        "kind": "unittest",
        "command": "python3 -m unittest discover -v",
        "exit_code": None,
        "passed": False,
        "stdout_tail": "",
        "stderr_tail": "",
        "timeout_s": timeout_s,
        "sandbox_dir": None,  # never leaked into reports; internal only
        "sandbox_removed": False,
    }
    sandbox = Path(tempfile.mkdtemp(prefix="aop-verify-"))
    result["sandbox_dir"] = str(sandbox)
    try:
        # Copy ONLY declared source files - nothing else from agent workspaces.
        copied = []
        for name in sorted(os.listdir(src_dir)):
            p = src_dir / name
            if p.is_file() and (name.endswith(".py") or name == "PLAN.md"):
                shutil.copy2(p, sandbox / name)
                copied.append(name)
        result["copied_files"] = copied
        if not any(c.startswith("test_") for c in copied):
            result["error"] = "no test module collected"
            result["stderr_tail"] = "verifier: no test_* module in collected artifacts"
            return result

        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(sandbox),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "LC_ALL": "C",
            "LANG": "C",
            "TMPDIR": str(sandbox),
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", "discover", "-v"],
                cwd=str(sandbox),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            result["exit_code"] = proc.returncode
            result["stdout_tail"] = _tail(proc.stdout)
            result["stderr_tail"] = _tail(proc.stderr)
            result["passed"] = proc.returncode == 0
        except subprocess.TimeoutExpired as e:
            result["exit_code"] = 124
            result["error"] = f"timeout after {timeout_s}s (child killed)"
            result["stdout_tail"] = _tail((e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""))
            result["stderr_tail"] = _tail((e.stderr or b"").decode() if isinstance(e.stderr, bytes) else (e.stderr or ""))
        return result
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
        result["sandbox_removed"] = not sandbox.exists()


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_agent_task(task_id: str, spec: dict, artifacts_dir: Path) -> dict:
    """Verify one agent task's collected artifacts against its declared criteria."""
    out: dict = {"task_id": task_id, "kind": spec.get("type"), "passed": False,
                 "checks": [], "command": None, "exit_code": None,
                 "stdout_tail": "", "stderr_tail": ""}
    vtype = spec.get("type")

    def check(name: str, ok: bool, detail: str = ""):
        out["checks"].append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            out["passed"] = False

    if vtype == "unittest":
        needs = spec.get("needs", [])
        missing = [n for n in needs if not (artifacts_dir / n).is_file()]
        check("required_files_present", not missing, f"missing={missing}" if missing else f"{needs}")
        if missing:
            return out
        res = run_unittest_sandbox(artifacts_dir, timeout_s=float(spec.get("timeout_s", 120)))
        out.update({k: res[k] for k in ("exit_code", "stdout_tail", "stderr_tail")})
        out["command"] = res["command"]
        check("unittest_exit_zero", res["exit_code"] == 0,
              f"exit={res['exit_code']} (0 = all tests passed)")
        out["passed"] = all(c["ok"] for c in out["checks"])

    elif vtype == "marker":
        fname = spec["file"]
        f = artifacts_dir / fname
        if not f.is_file():
            check("artifact_exists", False, f"{fname} not found in collected artifacts")
            return out
        text = f.read_text(errors="replace")
        marker = spec["marker"]
        check("unique_marker_present", marker in text, f"marker={marker!r} in {fname}")
        for s in spec.get("must_contain", []):
            check(f"contains:{s[:40]}", s in text, f"{s!r} in {fname}")
        out["passed"] = all(c["ok"] for c in out["checks"])

    elif vtype == "static":
        fname = spec["file"]
        f = artifacts_dir / fname
        if not f.is_file():
            check("artifact_exists", False, f"{fname} not found in collected artifacts")
            return out
        text = f.read_text(errors="replace")
        for s in spec.get("must_contain", []):
            check(f"contains:{s[:40]}", s in text, f"{s!r} in {fname}")
        out["passed"] = all(c["ok"] for c in out["checks"])

    else:
        out["error"] = f"unknown verify type {vtype!r}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task", help="task id (agent-task verification mode)")
    ap.add_argument("--spec", required=True, help="path to verify spec JSON")
    ap.add_argument("--dir", required=True, help="artifacts directory to verify")
    ap.add_argument("--out", required=True, help="where to write the result JSON")
    args = ap.parse_args()

    spec = json.loads(Path(args.spec).read_text())
    artifacts = Path(args.dir).resolve()
    if not artifacts.is_dir():
        print(json.dumps({"error": f"artifacts dir not found: {artifacts}"}))
        return 2

    if spec.get("type") == "unittest":
        res = run_unittest_sandbox(artifacts, timeout_s=float(spec.get("timeout_s", 120)))
        res["task_id"] = args.task
    else:
        res = verify_agent_task(args.task, spec, artifacts)

    Path(args.out).write_text(json.dumps(res, indent=1))
    print(json.dumps({"task_id": args.task, "passed": res.get("passed"),
                      "exit_code": res.get("exit_code"),
                      "checks": len(res.get("checks", []))}))
    return 0 if res.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
