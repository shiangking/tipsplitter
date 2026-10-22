#!/usr/bin/env python3
"""Execution policy — codifies the isolation requirement (Phase E condition 1).

Isolation inspection (2026-10-22, recorded in docs/PHASE-E-STATUS.md):
  - docker CLI present, but no daemon/socket reachable -> no container isolation
  - unshare (user/net/pid) -> Operation not permitted
  - chroot -> Operation not permitted (CapEff=0000000000000000)
  - no firejail/bwrap/podman

Therefore NO genuine filesystem or network isolation exists inside this
runtime. Policy:
  - STATIC gates (parse-level, secret scans, palette/DOM checks, browser
    rendering of already-written files) -> always autonomous.
  - EXEC gates (run project build/test scripts = untrusted code execution)
    -> require a recorded human approval for the specific project. In
    unattended mode they are BLOCKED, never silently skipped or passed.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent
EXEC_APPROVALS_DB = ROOT / "state" / "exec-approvals.db"

_EXEC_SCHEMA = """
CREATE TABLE IF NOT EXISTS exec_approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  approver TEXT NOT NULL,
  scope TEXT NOT NULL,            -- e.g. 'build-and-test'
  command_prefixes TEXT NOT NULL, -- JSON allowlist
  approved_at TEXT NOT NULL,
  expires_at TEXT,
  UNIQUE(project_id, scope)
);
"""


def _db() -> sqlite3.Connection:
    EXEC_APPROVALS_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(EXEC_APPROVALS_DB)
    c.row_factory = sqlite3.Row
    c.executescript(_EXEC_SCHEMA)
    return c


# --------------------------------------------------------------------------
def probe_isolation() -> dict:
    """Runtime re-probe of isolation capabilities. Evidence, not assumptions."""
    caps = {"docker_daemon": False, "unshare_user": False, "unshare_net": False,
            "chroot": False, "firejail": False, "bwrap": False}
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=10)
        caps["docker_daemon"] = r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass
    for key, args in (("unshare_user", ["--user", "--map-root-user"]),
                      ("unshare_net", ["--net"])):
        try:
            r = subprocess.run(["unshare"] + args + ["true"],
                               capture_output=True, timeout=10)
            caps[key] = r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    caps["chroot"] = False  # probed 2026-10-22: Operation not permitted (CapEff=0)
    for tool, key in (("firejail", "firejail"), ("bwrap", "bwrap")):
        try:
            subprocess.run([tool, "--version"], capture_output=True, timeout=5)
            caps[key] = True
        except (OSError, subprocess.TimeoutExpired):
            pass
    caps["genuine_isolation"] = (caps["docker_daemon"] or caps["firejail"]
                                 or caps["bwrap"])
    caps["probed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return caps


# --------------------------------------------------------------------------
def approve_exec(project_id: str, approver: str, scope: str = "build-and-test",
                 command_prefixes: list[str] | None = None,
                 expires_at: str | None = None) -> dict:
    """Record an explicit human approval for untrusted-code execution on a
    project. This is the ONLY way exec gates run in this deployment."""
    c = _db()
    c.execute("INSERT OR REPLACE INTO exec_approvals "
              "(project_id, approver, scope, command_prefixes, approved_at, expires_at) "
              "VALUES (?,?,?,?,?,?)",
              (project_id, approver, scope,
               json.dumps(command_prefixes or ["python3", "npm", "npx", "node",
                                               "pip", "pytest"]),
               time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), expires_at))
    c.commit()
    return {"project_id": project_id, "scope": scope, "approved": True}


def exec_allowed(project_id: str, scope: str = "build-and-test",
                 command: list[str] | None = None) -> dict:
    """Check whether an exec gate may run for this project right now."""
    c = _db()
    row = c.execute("SELECT * FROM exec_approvals WHERE project_id=? AND scope=?",
                    (project_id, scope)).fetchone()
    if row is None:
        return {"allowed": False, "reason": "no recorded human approval for "
                f"untrusted-code execution (project={project_id}, scope={scope}); "
                "isolation is unavailable in this runtime, so exec gates require "
                "explicit approval (see execution_policy.py docstring)"}
    if row["expires_at"] and row["expires_at"] < time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                time.gmtime()):
        return {"allowed": False, "reason": f"approval expired at {row['expires_at']}"}
    if command:
        prefixes = json.loads(row["command_prefixes"])
        if command and command[0] not in prefixes:
            return {"allowed": False, "reason":
                    f"command '{command[0]}' not in approved prefixes {prefixes}"}
    return {"allowed": True, "approver": row["approver"],
            "approved_at": row["approved_at"]}


def is_static_gate(gate_name: str) -> bool:
    """Static gates never execute project code and are always autonomous."""
    STATIC = {"secret_scan", "sast_parse", "dep_manifest_audit", "env_handling",
              "dom_computed_styles", "palette_conformance", "overflow_dom",
              "responsive_screenshots", "static_a11y", "dom_navigation",
              "representative_screen", "blank_ratio", "primary_color_present"}
    return gate_name in STATIC


if __name__ == "__main__":
    import sys
    caps = probe_isolation()
    print(json.dumps(caps, indent=2))
    if len(sys.argv) > 1 and sys.argv[1] == "approve":
        approve_exec(sys.argv[2], sys.argv[3])
        print(f"recorded approval for project {sys.argv[2]} by {sys.argv[3]}")
