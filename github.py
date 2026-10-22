#!/usr/bin/env python3
"""E5 — GitHub integration and staging delivery.

Safety model (approval gates enforced IN CODE, not just by convention):
  - ALWAYS ALLOWED (autonomous, ordinary coding ops):
      repo init, branches, milestone commits, staging-prep artifacts,
      release-readiness report drafting, PR TEXT preparation.
  - HARD GATED (require explicit, recorded human approval):
      * creating a new GitHub repository
      * changing repository permissions/visibility
      * pushing (needs a GITHUB_TOKEN provided via secure channel)
      * opening a pull request (into the user-approved private repo)
      * merging to main
      * any deployment
  - Pre-push secret scan is a hard blocker (security gate evidence required).
  - The token NEVER appears in prompts, logs, reports, or git remotes; it is
    passed via env (GITHUB_TOKEN) / the server secrets API when agents need it.

Approvals are recorded in exec-approvals.db via execution_policy.approve_exec
(scope='github:<action>'), each with an approver name and timestamp.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent
import sys
sys.path.insert(0, str(ROOT))
from execution_policy import _db  # recorded approvals
from gates.runner import gate_secret_scan  # pre-push blocker
from vision_probe import vision_available   # E2-A evidence = single source of truth


class ApprovalRequired(RuntimeError):
    """Raised when an approval-gated GitHub action is attempted."""


def _approval(project_id: str, action: str) -> dict:
    c = _db()
    row = c.execute("SELECT * FROM exec_approvals WHERE project_id=? AND scope=?",
                    (project_id, f"github:{action}")).fetchone()
    if row is None:
        raise ApprovalRequired(
            f"github:{action} requires recorded human approval "
            f"(execution_policy.approve_exec('{project_id}', approver, "
            f"'github:{action}'))")
    return dict(row)


def sh(cmd: list[str], cwd: Path, timeout_s: int = 120) -> tuple[int, str]:
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                       timeout=timeout_s)
    return p.returncode, (p.stdout + p.stderr).strip()


# --------------------------------------------------------------------------
# autonomous, ungated operations
# --------------------------------------------------------------------------
def init_repo(project_dir: Path, project_id: str) -> dict:
    sh(["git", "init", "-q"], project_dir) if not (project_dir / ".git").exists() else None
    sh(["git", "config", "user.email", "orchestrator@product-factory.local"], project_dir)
    sh(["git", "config", "user.name", "Product Factory Orchestrator"], project_dir)
    return {"repo": str(project_dir), "initialized": True}


def commit_milestone(project_dir: Path, message: str) -> dict:
    sh(["git", "add", "-A"], project_dir)
    rc, out = sh(["git", "commit", "-q", "-m", message, "--allow-empty"], project_dir)
    rc2, sha = sh(["git", "rev-parse", "HEAD"], project_dir)
    return {"committed": rc == 0 or "nothing to commit" in out,
            "sha": sha if rc2 == 0 else None, "message": message}


def create_feature_branch(project_dir: Path, name: str) -> dict:
    rc, out = sh(["git", "checkout", "-q", "-b", name], project_dir)
    if rc != 0:
        sh(["git", "checkout", "-q", name], project_dir)
    return {"branch": name}


# --------------------------------------------------------------------------
# gated operations
# --------------------------------------------------------------------------
def push_branch(project_dir: Path, project_id: str, repo: str, branch: str,
                token_env: str = "GITHUB_TOKEN") -> dict:
    """Push to the approved PRIVATE repo. Gated twice: recorded approval +
    pre-push secret scan. Token only via env, never in the remote URL/logs."""
    _approval(project_id, "push")
    scan = gate_secret_scan(project_dir)
    if scan["status"] == "failed":
        return {"pushed": False, "blocked_by": "secret_scan",
                "evidence": scan["checks"][0].get("hits", [])[:20]}
    token = os.environ.get(token_env, "")
    if not token:
        return {"pushed": False, "blocked_by": "missing_token",
                "detail": f"set {token_env} via secure channel; never in files"}
    # explicit check: token must not be embedded in the configured remote
    rc, remote = sh(["git", "remote", "get-url", "origin"], project_dir)
    if rc == 0 and token in remote:
        sh(["git", "remote", "set-url", "origin",
            f"https://github.com/{repo}.git"], project_dir)
    rc, out = sh(["git", "remote", "get-url", "origin"], project_dir)
    if rc != 0:
        sh(["git", "remote", "add", "origin", f"https://github.com/{repo}.git"],
           project_dir)
    env_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    p = subprocess.run(["git", "push", "-q", "-u", env_url, f"{branch}:{branch}"],
                       cwd=str(project_dir), capture_output=True, text=True,
                       timeout=300)
    ok = p.returncode == 0
    # scrub any token that could leak into captured output
    safe_out = (p.stdout + p.stderr).replace(token, "***")
    return {"pushed": ok, "branch": branch, "repo": repo,
            "output_tail": safe_out[-300:] if not ok else ""}


def open_pr(project_dir: Path, project_id: str, repo: str, branch: str,
            title: str, body: str, token_env: str = "GITHUB_TOKEN") -> dict:
    """Open a PR into the approved repo. Gated: recorded approval + token."""
    _approval(project_id, "open_pr")
    token = os.environ.get(token_env, "")
    if not token:
        return {"opened": False, "blocked_by": "missing_token"}
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    body_file = project_dir.parent / f"pr-body-{int(time.time())}.md"
    body_file.write_text(body)
    try:
        p = subprocess.run(["gh", "pr", "create", "-R", repo, "--head", branch,
                            "--title", title, "--body-file", str(body_file)],
                           capture_output=True, text=True, env=env, timeout=120)
        url = ""
        for line in p.stdout.splitlines():
            if line.startswith("http"):
                url = line.strip()
        return {"opened": p.returncode == 0, "pr_url": url,
                "error": p.stderr.replace(token, "***")[-300:] if p.returncode else ""}
    finally:
        body_file.unlink(missing_ok=True)


def actions_status(repo: str, branch: str, token_env: str = "GITHUB_TOKEN") -> dict:
    token = os.environ.get(token_env, "")
    if not token:
        return {"available": False, "blocked_by": "missing_token"}
    env = dict(os.environ)
    env["GH_TOKEN"] = token
    p = subprocess.run(["gh", "run", "list", "-R", repo, "--branch", branch,
                        "--limit", "5", "--json", "status,conclusion,headSha,name"],
                       capture_output=True, text=True, env=env, timeout=60)
    if p.returncode != 0:
        return {"available": False, "error": p.stderr[-200:]}
    try:
        return {"available": True, "runs": json.loads(p.stdout or "[]")}
    except json.JSONDecodeError:
        return {"available": False, "error": "unparseable gh output"}


def merge_to_main(project_dir: Path, project_id: str, pr_number: int) -> dict:
    """ALWAYS gated — merging to main is never autonomous in Phase E."""
    _approval(project_id, "merge_to_main")  # raises unless explicitly approved
    return {"merged": False, "detail": "merge approval recorded; use gh api "
            f"repos/*/pulls/{pr_number}/merge-async explicitly"}


# --------------------------------------------------------------------------
def release_readiness(project_dir: Path, gates: list[dict],
                      repair_summary: dict, delivery: dict) -> dict:
    """Structured, evidence-based release-readiness report."""
    def fam(f):
        return [g for g in gates if g["family"] == f]
    vision_unavailable = not vision_available()
    visual_gates = fam("visual") + [g for g in fam("browser")
                                    if g["gate"].startswith(("dom_check", "responsive"))]
    if not visual_gates:
        pq = {"status": "BLOCKED",
              "reason": "no visual/product gates ran for this build; product "
                        "quality cannot be accepted without product evidence"}
    elif vision_unavailable:
        pq = {"status": "BLOCKED",
              "reason": "LLM vision review unavailable (no vision-capable profile; "
                        "see docs/VISION-CAPABILITY.md). Per Phase E condition 2, "
                        "unattended product-quality acceptance is blocked."}
    else:
        pq = {"status": "REQUIRES-REVIEW",
              "reason": "visual gates ran; independent vision critique required "
                        "before acceptance"}
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "technical_gates": {g["gate"]: g["status"] for g in fam("technical")},
        "browser_gates": {g["gate"]: g["status"] for g in fam("browser")},
        "visual_gates": {g["gate"]: g["status"] for g in fam("visual")},
        "security_gates": {g["gate"]: g["status"] for g in fam("security")},
        "product_quality_acceptance": pq,
        "repair_summary": repair_summary,
        "delivery": delivery,
        "unresolved": [g["gate"] for g in gates if g["status"] in ("failed", "blocked")],
    }
