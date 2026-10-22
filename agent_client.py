#!/usr/bin/env python3
"""Product agent client — runs product-development tasks as REAL OpenHands
conversations (never simulated), reusing the acceptance-proven launch path.

A ProductTask carries: id, role (which model profile), prompt, workspace,
timeout, expected artifacts. The client launches, polls to completion,
collects results + model evidence (served model + token usage from server
stats — the same telemetry that proved model assignment in Phase C).
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from controller import APIError, api, log  # noqa: E402

POLL_S = 5
DEFAULT_TIMEOUT_S = 600
TERMINAL = {"idle", "finished", "error", "stuck", "paused", "stopped"}


class ProductTask:
    def __init__(self, task_id: str, role: str, profile: str, prompt: str,
                 workspace: Path, timeout_s: int = DEFAULT_TIMEOUT_S,
                 max_iterations: int = 80, tags: dict | None = None,
                 artifacts: list[str] | None = None):
        self.id = task_id
        self.role = role                # planner|engineer|designer|reviewer|qa
        self.profile = profile          # glm5.3|deepseekv4.1|qwen3.8flash
        self.prompt = prompt
        self.workspace = Path(workspace)
        self.timeout_s = timeout_s
        self.max_iterations = max_iterations
        self.tags = tags or {}
        self.artifacts = artifacts or []


ROLES = {
    "planner": "glm5.3",
    "architect": "glm5.3",
    "engineer": "deepseekv4.1",
    "designer": "qwen3.8flash",
    "reviewer": "glm5.3",
    "qa": "qwen3.8flash",
}


_LAUNCH_LOCK = threading.Lock()  # serializes conversation creates: the local
# Agent Server 500s ("NoneType has no len()") on concurrent POST creates.
# Execution stays parallel — only creation is serialized.


def launch(task: ProductTask, orchestration_tag: str) -> str:
    with _LAUNCH_LOCK:
        return _launch_locked(task, orchestration_tag)


def _launch_locked(task: ProductTask, orchestration_tag: str) -> str:
    # payload hygiene: the agent server rejects non-string tag values /
    # ids with HTTP 500; coerce and validate at the boundary
    task.id = str(task.id)
    task.tags = {str(k): str(v) for k, v in task.tags.items()}
    task.tags.setdefault("poc", "productfactory")
    task.tags.setdefault("orchestrationid", str(orchestration_tag))
    if not str(task.prompt).strip():
        raise ValueError(f"[{task.id}] empty prompt")
    pid = api("GET", "/api/settings")[1].get("active_agent_profile_id")
    task.workspace.mkdir(parents=True, exist_ok=True)
    cid = str(uuid.uuid4())
    body = {
        "conversation_id": cid,
        "agent_profile_id": pid,
        "agent_launch_additions": {"llm_profile_ref": task.profile},
        "workspace": {"kind": "LocalWorkspace", "working_dir": str(task.workspace)},
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": task.max_iterations,
        "stuck_detection": True,
        "tags": task.tags,
        "initial_message": {"role": "user", "run": True,
                            "content": [{"type": "text", "text": task.prompt}]},
    }
    # bounded launch retry: transient server 5xx windows (concurrent-create
    # races, post-conversation GC) can outlast short backoffs; use patient
    # exponential backoff (~2.5 min total). 4xx (except 429) is non-retryable.
    last_err = None
    for attempt, backoff in enumerate((0, 5, 10, 20, 40, 60), start=1):
        try:
            st, resp = api("POST", "/api/conversations", body, timeout=120)
        except APIError as e:
            last_err = str(e)
            st = 0
        if st == 201:
            log(f"[{task.id}] launched {task.profile} conversation {cid[:8]} "
                f"ws={task.workspace}" + (f" (attempt {attempt})" if attempt > 1 else ""))
            time.sleep(1.5)  # settle window before the next concurrent create
            return cid
        last_err = last_err or f"HTTP {st}"
        if 400 <= st < 500 and st != 429:
            break
        if backoff:
            log(f"[{task.id}] launch attempt {attempt} failed ({last_err}); "
                f"retrying in {backoff}s", level="WARN")
            time.sleep(backoff)
    raise APIError(f"conversation create failed after retries: {last_err}")


def wait(cid: str, timeout_s: int, stop_flag: list | None = None) -> dict:
    t0 = time.time()
    status = None
    while time.time() - t0 < timeout_s:
        if stop_flag and stop_flag[0]:
            return {"status": "interrupted", "elapsed": round(time.time() - t0, 1)}
        time.sleep(POLL_S)
        _, info = api("GET", f"/api/conversations/{cid}")
        status = info.get("execution_status")
        if status in TERMINAL:
            break
    _, info = api("GET", f"/api/conversations/{cid}")
    stats = (info.get("stats") or {}).get("usage_to_metrics") or {}
    usage = {}
    for k, v in stats.items():
        u = v.get("accumulated_token_usage") or {}
        if u.get("prompt_tokens") or u.get("completion_tokens"):
            usage = {"served_model": v.get("model_name"),
                     "prompt_tokens": u.get("prompt_tokens"),
                     "completion_tokens": u.get("completion_tokens")}
            break
    return {"status": status or "timeout", "elapsed": round(time.time() - t0, 1),
            "usage": usage, "info": {"id": info.get("id"),
                                     "title": info.get("title")}}


def stop(cid: str, deadline_s: float = 60) -> bool:
    try:
        api("POST", f"/api/conversations/{cid}/stop")
    except APIError:
        pass
    t0 = time.time()
    while time.time() - t0 < deadline_s:
        time.sleep(2)
        st = api("GET", f"/api/conversations/{cid}")[1].get("execution_status")
        if st in ("paused", "stopped", "idle", "finished", "error"):
            return True
    return False


def collect(task: ProductTask, cid: str) -> dict:
    """Collect final response + declared artifacts from the workspace."""
    out = {"final_response": None, "artifacts": {}}
    try:
        _, fr = api("GET", f"/api/conversations/{cid}/agent_final_response")
        out["final_response"] = (fr or {}).get("response") or ""
    except APIError:
        pass
    for rel in task.artifacts:
        p = task.workspace / rel
        if p.exists() and p.stat().st_size > 0:
            out["artifacts"][rel] = {"sha256": __import__("hashlib").sha256(
                p.read_bytes()).hexdigest(), "bytes": p.stat().st_size}
    return out


def run_task(task: ProductTask, orchestration_tag: str,
             stop_flag: list | None = None) -> dict:
    """Launch → wait → (timeout ⇒ stop) → collect. Full evidence record."""
    cid = launch(task, orchestration_tag)
    res = wait(cid, task.timeout_s, stop_flag)
    if res["status"] == "timeout":
        log(f"[{task.id}] TIMEOUT after {task.timeout_s}s — stopping agent", level="WARN")
        stopped = stop(cid)
        res["stopped_confirmed"] = stopped
    rec = {"task_id": task.id, "role": task.role, "profile": task.profile,
           "conversation_id": cid, "result": res,
           "collected": collect(task, cid)}
    rec["succeeded"] = res["status"] in ("finished", "idle") and \
        all((task.workspace / a).exists() for a in task.artifacts)
    return rec
