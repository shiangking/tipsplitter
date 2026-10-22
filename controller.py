#!/usr/bin/env python3
"""agent-orchestration-poc — minimal multi-agent orchestration controller.

Coordinates independent OpenHands agent-server conversations (one per agent task)
with: persistent SQLite task state, model-specific assignment (LLM profile refs),
dependency tracking, wall-clock timeouts, bounded retries, deterministic
verification (no LLM), and structured evidence reporting.

Design docs: docs/phase-a-findings.md, docs/phase-b-design.md

Safety properties implemented here:
  S1  No unrestricted host execution: before the first task, an isolation probe
      conversation (created with initial_message.run=false -> zero LLM calls)
      verifies that the launch shape yields (a) its own workspace dir, (b) the
      profile's toolset, (c) per-conversation NeverConfirm. Every run re-records
      workspace metadata as evidence.
  S2  Verification commands never run in the caller's or agents' workspaces:
      verify.py copies sources into a fresh temp dir and runs a minimal-env
      subprocess with a hard kill timeout (see verify.py).
  S3  No overlapping retries: before any (re)launch the controller finds every
      non-terminal run of the task, interrupts its conversation AND waits for a
      non-running status before proceeding; retries always use a fresh workspace.

Credentials: the controller never reads or stores API keys. Launches reference
an agent profile by UUID plus an LLM profile by NAME; the server resolves the
key from its own store. All output passes through a redaction filter anyway.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets as pysecrets
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

POC_ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("AOP_STATE_DIR", str(POC_ROOT / "state")))
DB_PATH = STATE_DIR / "orchestration.db"
LOG_PATH = STATE_DIR / "controller.log"
WORKSPACES = POC_ROOT / "workspaces"
ARTIFACTS = POC_ROOT / "artifacts"
REPORTS = POC_ROOT / "reports"
POC_TAG = "agent-orchestration-poc"
NON_ARTIFACT_FILES = {"final_response.md", "workspace_meta.json", "events_tail.json", "run_evidence.json"}

BASE = os.environ.get("AGENT_SERVER_URL", "http://127.0.0.1:18000").rstrip("/")

# ---------------------------------------------------------------- redaction
_SECRET_ENV_NAMES = ("SESSION_API_KEY", "OH_SESSION_API_KEYS_0", "LOCAL_BACKEND_API_KEY",
                     "OH_SECRET_KEY", "AUTOMATION_AGENT_SERVER_API_KEY",
                     "OPENHANDS_AUTOMATION_API_KEY", "AUTOMATION_LOCAL_API_KEY")
_KNOWN_SECRETS = [os.environ[n] for n in _SECRET_ENV_NAMES if os.environ.get(n)]
_SECRET_RE = re.compile(
    r"(?i)((api[_-]?key|token|secret|authorization|password)(\"\])?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)")
_FERNET_RE = re.compile(r"gAAAA[A-Za-z0-9_\-]+")


def redact(text) -> str:
    if text is None:
        return ""
    out = str(text)
    for s in _KNOWN_SECRETS:
        if s:
            out = out.replace(s, "[REDACTED-SESSION-KEY]")
    out = _FERNET_RE.sub("[REDACTED-ENCRYPTED-TOKEN]", out)
    out = _SECRET_RE.sub(r"\1[REDACTED]", out)
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------- logging + db audit
_DBH: list[sqlite3.Connection | None] = [None]


def log(msg: str, level: str = "INFO", task_id: str | None = None, run_id: int | None = None):
    line = f"{now_iso()} [{level}] {redact(msg)}"
    print(line, flush=True)
    try:
        LOG_PATH.open("a").write(line + "\n")
    except OSError:
        pass
    conn = _DBH[0]
    if conn is not None:
        try:
            conn.execute("INSERT INTO events(ts,level,task_id,run_id,message) VALUES(?,?,?,?,?)",
                         (now_iso(), level, task_id, run_id, redact(msg)))
            conn.commit()
        except sqlite3.Error:
            pass


# ---------------------------------------------------------------- HTTP client
class APIError(Exception):
    pass


def _session_key() -> str:
    key = os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0") or ""
    if not key:
        p = Path.home() / ".openhands/agent-canvas/api-key.txt"
        if p.is_file():
            key = p.read_text().strip()
    if not key:
        raise APIError("no session API key available in environment")
    return key


def api(method: str, path: str, body: dict | None = None, timeout: float = 60,
        expect_json: bool = True, tries: int = 3):
    key = _session_key()
    data = json.dumps(body).encode() if body is not None else None
    last_err: Exception | None = None
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(BASE + path, data=data, method=method, headers={
            "X-Session-API-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode()
                if not expect_json:
                    return resp.status, raw
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            if e.code < 500 or attempt == tries:
                raise APIError(f"{method} {path} -> HTTP {e.code}: {redact(detail)}") from e
            last_err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            last_err = e
            if attempt == tries:
                raise APIError(f"{method} {path} failed after {tries} tries: {redact(str(e))}") from e
        time.sleep(2 * attempt)
    raise APIError(f"{method} {path} failed: {last_err}")


# ---------------------------------------------------------------- database
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  title TEXT, model_profile TEXT NOT NULL,
  depends_on TEXT NOT NULL DEFAULT '[]',
  expected_artifacts TEXT NOT NULL DEFAULT '[]',
  verify_spec TEXT, prompt TEXT,
  timeout_s INTEGER, max_retries INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  run_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL, attempt INTEGER NOT NULL,
  conversation_id TEXT NOT NULL,
  llm_profile_ref TEXT, launch_profile TEXT, served_model TEXT,
  token_usage TEXT, iterations_used INTEGER,
  status TEXT NOT NULL DEFAULT 'running',
  started_at TEXT, finished_at TEXT, duration_s REAL, error TEXT,
  UNIQUE(task_id, attempt)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER, task_id TEXT, path TEXT, sha256 TEXT,
  bytes INTEGER, collect_method TEXT, collected_at TEXT
);
CREATE TABLE IF NOT EXISTS verify_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, run_id INTEGER, command TEXT, exit_code INTEGER,
  passed INTEGER, result_json TEXT, recorded_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, level TEXT, task_id TEXT, run_id INTEGER, message TEXT
);
"""


def db() -> sqlite3.Connection:
    if _DBH[0] is None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        _DBH[0] = conn
    return _DBH[0]


def db_exec(sql: str, params: tuple = ()):
    db().execute(sql, params)
    db().commit()


def db_one(sql: str, params: tuple = ()):
    return db().execute(sql, params).fetchone()


def db_all(sql: str, params: tuple = ()):
    return db().execute(sql, params).fetchall()


def set_task(task_id: str, **fields):
    fields["updated_at"] = now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    db_exec(f"UPDATE tasks SET {sets} WHERE id=?", (*fields.values(), task_id))


# ---------------------------------------------------------------- spec + init
def load_tasks_spec(path: Path) -> dict:
    return json.loads(path.read_text())


def init_db_from_spec(spec: dict, spec_path: Path, orchestration_id: str):
    db()
    db_exec("INSERT OR REPLACE INTO meta(key,value) VALUES('orchestration_id',?)", (orchestration_id,))
    db_exec("INSERT OR REPLACE INTO meta(key,value) VALUES('tasks_spec_path',?)", (str(spec_path),))
    for t in spec["tasks"]:
        db_exec("""INSERT INTO tasks(id,title,model_profile,depends_on,expected_artifacts,
                   verify_spec,prompt,timeout_s,max_retries,status,attempts,last_error,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?, 'pending',0,NULL,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title=excluded.title, model_profile=excluded.model_profile,
                     depends_on=excluded.depends_on, expected_artifacts=excluded.expected_artifacts,
                     verify_spec=excluded.verify_spec, prompt=excluded.prompt,
                     timeout_s=excluded.timeout_s, max_retries=excluded.max_retries,
                     updated_at=excluded.updated_at""",
                (t["id"], t.get("title", t["id"]), t["model_profile"],
                 json.dumps(t.get("depends_on", [])),
                 json.dumps(t.get("expected_artifacts", [])),
                 json.dumps(t.get("verify")) if t.get("verify") else None,
                 t["prompt"],
                 t.get("timeout_s", spec["settings"]["default_timeout_s"]),
                 t.get("max_retries", spec["settings"]["default_max_retries"]),
                 now_iso()))
    log(f"initialized {len(spec['tasks'])} tasks; orchestration_id={orchestration_id}")


def topological_order(tasks: list[dict]) -> list[dict]:
    by_id = {t["id"]: t for t in tasks}
    order, seen, temp = [], set(), set()

    def visit(tid: str, stack: tuple = ()):
        if tid in seen:
            return
        if tid in temp or tid in stack:
            raise RuntimeError(f"dependency cycle involving {tid}")
        temp.add(tid)
        for dep in by_id[tid].get("depends_on", []):
            if dep not in by_id:
                raise RuntimeError(f"task {tid} depends on unknown task {dep}")
            visit(dep, stack + (tid,))
        temp.discard(tid)
        seen.add(tid)
        order.append(by_id[tid])

    for t in tasks:
        visit(t["id"])
    return order


def descendants_of(task_id: str, tasks_by_id: dict) -> set:
    out: set[str] = set()
    changed = True
    while changed:
        changed = False
        for t in tasks_by_id.values():
            if t["id"] in out:
                continue
            if task_id in t.get("depends_on", []) or (set(t.get("depends_on", [])) & out):
                out.add(t["id"])
                changed = True
    return out


# ---------------------------------------------------------------- isolation probe (Safeguard 1)
def check_isolation(spec: dict) -> dict:
    """Pre-execution isolation verification. Uses a probe conversation created
    with initial_message.run=false => the agent loop never starts => zero LLM cost.
    Verifies: dedicated workspace dir, tool set from profile, NeverConfirm scope."""
    report: dict = {"probe_conversation_id": None, "probe_deleted": False,
                    "workspace_dir": None, "confirmation_policy": None,
                    "tools": [], "passed": False, "notes": []}
    st, profiles = api("GET", "/api/agent-profiles")
    want = spec["settings"]["agent_profile_name"]
    match = [p for p in profiles.get("profiles", []) if p.get("name") == want]
    if not match:
        raise APIError(f"agent profile {want!r} not found; aborting rather than creating one")
    profile_id = match[0]["id"]
    report["agent_profile"] = {"id": profile_id, "name": match[0]["name"],
                               "llm_profile_ref": match[0].get("llm_profile_ref")}
    db_exec("INSERT OR REPLACE INTO meta(key,value) VALUES('agent_profile_id',?)", (profile_id,))

    probe_ws = WORKSPACES / "_isolation_probe"
    probe_ws.mkdir(parents=True, exist_ok=True)
    cid = str(uuid.uuid4())
    body = {
        "conversation_id": cid,
        "agent_profile_id": profile_id,
        "workspace": {"kind": "LocalWorkspace", "working_dir": str(probe_ws)},
        "confirmation_policy": {"kind": "NeverConfirm"},
        "initial_message": {"role": "user", "run": False,
                            "content": [{"type": "text", "text": "isolation probe - do not run"}]},
    }
    st, created = api("POST", "/api/conversations", body, timeout=120)
    report["probe_conversation_id"] = cid
    # Creation can show a transient 'running' while the conversation initializes
    # (even with run:false). Wait briefly for it to settle before judging.
    info: dict = {}
    settled_status = None
    t0 = time.time()
    while time.time() - t0 < 30:
        try:
            _, info = api("GET", f"/api/conversations/{cid}")
        except APIError:
            time.sleep(2)
            continue
        st_now = info.get("execution_status")
        if st_now != "running":
            settled_status = st_now
            break
        time.sleep(2)
    report["settled_status"] = settled_status
    agent = info.get("agent") or {}
    tools = [t.get("name") for t in (agent.get("tools") or []) if isinstance(t, dict)]
    report["tools"] = tools
    report["confirmation_policy"] = info.get("confirmation_policy")
    ws = info.get("workspace") or {}
    report["workspace_dir"] = ws.get("working_dir") if isinstance(ws, dict) else ws
    checks = {
        "workspace_is_probe_dir": str(probe_ws) in str(report["workspace_dir"]),
        "tools_present": len(tools) > 0,
        "neverconfirm_scoped": (report["confirmation_policy"] or {}).get("kind") == "NeverConfirm",
        # run:false must not start the agent loop: status settles to non-running
        # and no tokens are consumed. A settled status is the evidence.
        "probe_settled_without_agent_loop": settled_status is not None
        and settled_status != "running"
        and not ((info.get("metrics") or {}).get("accumulated_token_usage")),
    }
    report["checks"] = checks
    report["passed"] = all(checks.values())
    report["notes"].append("launch shape: agent_profile_id + agent_launch_additions.llm_profile_ref "
                           "(stored profiles are NOT modified; server resolves credentials itself)")
    report["notes"].append("NeverConfirm is per-conversation in the POC launch body; global "
                           "settings are never PATCHed by this controller")
    report["notes"].append("agent task workspaces are dedicated dirs under "
                           f"{WORKSPACES}; no shared workspace, no repo worktree")
    try:
        api("DELETE", f"/api/conversations/{cid}", timeout=60)
        report["probe_deleted"] = True
    except APIError as e:
        report["notes"].append(f"probe delete failed (harmless): {str(e)[:120]}")
    if not report["passed"]:
        raise APIError(f"isolation probe FAILED: {checks}")
    log(f"isolation probe PASSED: tools={tools} policy={report['confirmation_policy']} "
        f"workspace={report['workspace_dir']}")
    return report


# ---------------------------------------------------------------- prompt rendering
def build_dep_vars(task_id: str) -> tuple[dict[str, str], str]:
    """Dependency artifacts -> prompt variables + deterministic verify summary."""
    variables: dict[str, str] = {}
    summary_parts: list[str] = []
    for dep in json.loads(db_one("SELECT depends_on FROM tasks WHERE id=?", (task_id,))["depends_on"]):
        vrow = db_one("SELECT * FROM verify_results WHERE task_id=? ORDER BY id DESC LIMIT 1", (dep,))
        if vrow:
            summary_parts.append(f"[{dep}] deterministic verification: "
                                 f"{'PASSED' if vrow['passed'] else 'FAILED'} "
                                 f"(exit={vrow['exit_code']}, command: {vrow['command']})")
        texts = []
        for a in db_all("""SELECT * FROM artifacts WHERE task_id=? AND run_id=
                           (SELECT MAX(run_id) FROM artifacts WHERE task_id=?) ORDER BY id""", (dep, dep)):
            if a["path"] in NON_ARTIFACT_FILES:
                continue
            p = ARTIFACTS / dep / a["path"]
            if not p.is_file():
                continue
            content = p.read_text(errors="replace")
            variables[f"{dep}_{Path(a['path']).stem}"] = content
            texts.append(f"----- {dep}/{a['path']} -----\n{content}")
        variables[f"{dep}_text"] = "\n\n".join(texts) if texts else f"(no artifacts from {dep})"
    return variables, ("\n".join(summary_parts) if summary_parts else "(no verification results)")


def render_prompt(template: str, task_id: str, variables: dict[str, str], verify_summary: str,
                  orchestration_id: str) -> str:
    marker_line = f"PLAN-ID: AOP-{orchestration_id}"
    out = template.replace("{marker_line}", marker_line)
    out = out.replace("{orchestration_id}", orchestration_id)
    out = out.replace("{verify_summary}", verify_summary)
    for var, text in variables.items():
        out = out.replace("{" + var + "}", text)
    return out


def render_verify_spec(vspec: dict | None, orchestration_id: str) -> dict | None:
    if not vspec:
        return None
    return json.loads(json.dumps(vspec).replace("{orchestration_id}", orchestration_id))


# ---------------------------------------------------------------- agent run lifecycle
def launch(task: dict, attempt: int, prompt: str, orchestration_id: str,
           settings: dict) -> dict:
    profile_id = db_one("SELECT value FROM meta WHERE key='agent_profile_id'")["value"]
    ws = WORKSPACES / task["id"] / f"attempt-{attempt:02d}"
    if ws.exists():
        archived = ws.with_name(f"attempt-{attempt:02d}.superseded-{int(time.time())}")
        log(f"task {task['id']} attempt {attempt}: workspace exists -> archiving to "
            f"{archived.name} (never reuse a workspace)", level="WARN", task_id=task["id"])
        ws.rename(archived)
    ws.mkdir(parents=True, exist_ok=True)
    cid = str(uuid.uuid4())
    body = {
        "conversation_id": cid,
        "agent_profile_id": profile_id,
        "agent_launch_additions": {"llm_profile_ref": task["model_profile"]},
        "workspace": {"kind": "LocalWorkspace", "working_dir": str(ws)},
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": settings.get("max_iterations", 50),
        "stuck_detection": bool(settings.get("stuck_detection", True)),
        "tags": {"poc": POC_TAG, "task": task["id"], "attempt": str(attempt),
                 "orchestrationid": orchestration_id},
        "initial_message": {"role": "user", "run": True,
                            "content": [{"type": "text", "text": prompt}]},
    }
    st, created = api("POST", "/api/conversations", body, timeout=120)
    if st != 201:
        raise APIError(f"conversation create returned HTTP {st}")
    log(f"launched conversation {cid} task={task['id']} attempt={attempt} "
        f"model_profile={task['model_profile']} workspace={ws}", task_id=task["id"])
    return {"conversation_id": cid, "workspace": str(ws)}


def conversation_status(cid: str) -> dict:
    _, info = api("GET", f"/api/conversations/{cid}")
    return info


def confirm_stopped(cid: str, deadline_s: float = 60) -> bool:
    """Safeguard 3: interrupt if running, then CONFIRM a non-running status."""
    try:
        info = conversation_status(cid)
    except APIError as e:
        log(f"confirm_stopped: status unavailable for {cid}: {str(e)[:150]}", level="WARN")
        return False
    if info.get("execution_status") == "running":
        try:
            api("POST", f"/api/conversations/{cid}/interrupt", timeout=30)
        except APIError as e:
            log(f"interrupt call failed for {cid}: {str(e)[:150]}", level="WARN")
        t0 = time.time()
        while time.time() - t0 < deadline_s:
            time.sleep(2)
            try:
                info = conversation_status(cid)
            except APIError:
                continue
            if info.get("execution_status") != "running":
                break
    stopped = info.get("execution_status") != "running"
    log(f"conversation {cid} stop confirmed={stopped} (status={info.get('execution_status')})")
    return stopped


def wait_for_completion(cid: str, timeout_s: float, poll_s: float, stop_flag: list,
                        idle_grace_s: float = 45) -> tuple[str, dict]:
    t0 = time.time()
    first_idle_seen: float | None = None
    while time.time() - t0 < timeout_s:
        if stop_flag[0]:
            return "interrupted", {}
        try:
            info = conversation_status(cid)
        except APIError as e:
            log(f"status poll error (retrying): {str(e)[:150]}", level="WARN")
            time.sleep(poll_s)
            continue
        st = info.get("execution_status")
        if st == "finished":
            return "finished", info
        if st in ("error", "stuck"):
            return st, info
        if st in ("idle", "paused", "waiting_for_confirmation"):
            # transient right after creation (agent loop starts async); only treat
            # as ended once a final response exists, else allow a grace period
            try:
                _, fr = api("GET", f"/api/conversations/{cid}/agent_final_response")
                resp = (fr or {}).get("response") or ""
            except APIError:
                resp = ""
            if resp.strip():
                return "finished", info
            if first_idle_seen is None:
                first_idle_seen = time.time()
            if time.time() - first_idle_seen > idle_grace_s:
                return f"unexpected_{st}", info
        else:
            first_idle_seen = None  # agent resumed working; reset the grace timer
        time.sleep(poll_s)
    return "timeout", {}


def record_artifact(run_id: int, task_id: str, rel: str, method: str):
    p = ARTIFACTS / task_id / rel
    if not p.is_file():
        return
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    db_exec("""INSERT INTO artifacts(run_id,task_id,path,sha256,bytes,collect_method,collected_at)
               VALUES(?,?,?,?,?,?,?)""",
            (run_id, task_id, rel, h, p.stat().st_size, method, now_iso()))


def collect_results(task: dict, run_id: int, cid: str, ws_dir: str, info: dict) -> str:
    """Collect final response, declared artifacts, and model-usage evidence."""
    task_dir = ARTIFACTS / task["id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    ev = []
    metrics = info.get("metrics") or {}
    # usage lives in stats.usage_to_metrics in this deployment (metrics may be null)
    stats = info.get("stats") or {}
    usage_map = stats.get("usage_to_metrics") or {}
    first_usage = next(iter(usage_map.values())) if usage_map else {}
    served = metrics.get("model_name") or first_usage.get("model_name")
    launch_prof = (info.get("launched_agent_profile") or {}).get("llm_profile_ref")
    tok = metrics.get("accumulated_token_usage") or first_usage.get("accumulated_token_usage") or {}
    db_exec("""UPDATE runs SET served_model=?, launch_profile=?, token_usage=? WHERE run_id=?""",
            (served, launch_prof, json.dumps(tok), run_id))
    ev.append(f"served_model={served} launch_profile={launch_prof} "
              f"tokens(prompt/completion)={tok.get('prompt_tokens')}/{tok.get('completion_tokens')}")

    try:
        st, fr = api("GET", f"/api/conversations/{cid}/agent_final_response")
        resp = (fr or {}).get("response") or ""
    except Exception as e:  # noqa: BLE001
        resp = ""
        ev.append(f"final_response fetch failed: {redact(str(e))[:120]}")
    if resp.strip():
        (task_dir / "final_response.md").write_text(resp)
        record_artifact(run_id, task["id"], "final_response.md", "api:agent_final_response")
        ev.append(f"final_response={len(resp)}B")

    evidence = {
        "conversation_id": cid,
        "execution_status": info.get("execution_status"),
        "launched_agent_profile": info.get("launched_agent_profile"),
        "workspace": info.get("workspace"),
        "tags": info.get("tags"),
        "confirmation_policy": info.get("confirmation_policy"),
        "tools": [t.get("name") for t in ((info.get("agent") or {}).get("tools") or [])
                  if isinstance(t, dict)],
        "metrics_model_name": served,
        "accumulated_token_usage": tok,
        "stats_usage_to_metrics": usage_map,
    }
    (task_dir / "run_evidence.json").write_text(json.dumps(evidence, indent=1))
    ev.append(f"tools={len(evidence['tools'])} workspace_recorded=yes")

    for rel in task.get("expected_artifacts", []):
        got = False
        # The workspace-file endpoint returns the RAW file body (not JSON).
        try:
            st, raw = api("GET", f"/api/conversations/{cid}/workspace/{rel}", expect_json=False)
            if st == 200 and isinstance(raw, str) and raw.strip():
                (task_dir / rel).write_text(raw)
                record_artifact(run_id, task["id"], rel, "api:workspace_file_raw")
                got = True
        except APIError as e:
            ev.append(f"artifact {rel} via API failed: {str(e)[:120]}")
        except Exception as e:  # noqa: BLE001 - never let collection crash a run
            ev.append(f"artifact {rel} via API error: {redact(str(e))[:120]}")
        if not got:
            ws_path = Path(ws_dir) / rel
            if ws_path.is_file():
                (task_dir / rel).write_text(ws_path.read_text(errors="replace"))
                record_artifact(run_id, task["id"], rel, "filesystem_fallback")
                got = True
                ev.append(f"artifact {rel} collected via filesystem fallback")
        ev.append(f"artifact {rel}: collected={got}")

    try:
        st, evresp = api("GET", f"/api/conversations/{cid}/events/search?limit=100")
        events = evresp.get("items") or evresp.get("events") or []
        tail = [{k: (v if isinstance(v, (int, float, bool, type(None))) else redact(json.dumps(v)[:400]))
                 for k, v in e.items() if k in ("id", "kind", "source", "timestamp", "argument")}
                for e in events]
        (task_dir / "events_tail.json").write_text(json.dumps(tail, indent=1))
        ev.append(f"events_tail={len(tail)}")
    except APIError as e:
        ev.append(f"events tail unavailable: {str(e)[:120]}")

    log("collected: " + "; ".join(ev), task_id=task["id"], run_id=run_id)
    return resp


def run_verify(task_id: str, run_id: int, vspec: dict | None) -> bool:
    if not vspec:
        log(f"task {task_id}: no verify spec -> cannot verify; treating as failed",
            level="WARN", task_id=task_id)
        return False
    task_dir = ARTIFACTS / task_id
    spec_file = STATE_DIR / f"verify-spec-{task_id}.json"
    spec_file.write_text(json.dumps(vspec))
    result_file = STATE_DIR / f"verify-result-{task_id}.json"
    result_file.unlink(missing_ok=True)
    cmd = [sys.executable, str(POC_ROOT / "verify.py"), "--task", task_id,
           "--spec", str(spec_file), "--dir", str(task_dir), "--out", str(result_file)]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(STATE_DIR),
           "PYTHONDONTWRITEBYTECODE": "1", "AOP_NONCE": pysecrets.token_hex(8)}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=float(vspec.get("timeout_s", 180)),
                              env=env, cwd=str(POC_ROOT))
        exit_code = proc.returncode
        result = json.loads(result_file.read_text()) if result_file.is_file() else {"error": "no result file"}
    except subprocess.TimeoutExpired:
        result, exit_code = {"error": "verifier timeout"}, 124
    except Exception as e:  # noqa: BLE001
        result, exit_code = {"error": redact(str(e))}, -1
    passed = bool(result.get("passed"))
    db_exec("""INSERT INTO verify_results(task_id,run_id,command,exit_code,passed,result_json,recorded_at)
               VALUES(?,?,?,?,?,?,?)""",
            (task_id, run_id, result.get("command") or vspec.get("command") or vspec.get("type"),
             exit_code, int(passed), json.dumps(result)[:8000], now_iso()))
    log(f"verification for {task_id}: passed={passed} exit={exit_code} "
        f"checks={json.dumps(result.get('checks', result.get('error', '')))[:500]}",
        level="INFO" if passed else "ERROR", task_id=task_id, run_id=run_id)
    return passed


def extract_error_reason(cid: str) -> str:
    try:
        _, evresp = api("GET", f"/api/conversations/{cid}/events/search?limit=100")
        errs = [f"{e.get('kind')}: {redact(str(e.get('argument') or ''))[:300]}"
                for e in (evresp.get("items") or evresp.get("events") or [])
                if e.get("kind") in ("ConversationErrorEvent", "AgentErrorEvent", "ServerErrorEvent")]
        return " | ".join(errs[-3:]) or "no error events captured"
    except APIError as e:
        return f"could not fetch events: {str(e)[:150]}"


# ---------------------------------------------------------------- task execution
def execute_task(task: dict, orchestration_id: str, stop_flag: list,
                 settings: dict, chaos: bool = False) -> str:
    trow = db_one("SELECT * FROM tasks WHERE id=?", (task["id"],))
    timeout_s = float(trow["timeout_s"])
    max_retries = int(trow["max_retries"])
    prior = db_one("SELECT COALESCE(MAX(attempt),0) AS a FROM runs WHERE task_id=?", (task["id"],))
    first_attempt = prior["a"] + 1
    last_error = None
    timed_out = False

    for attempt in range(first_attempt, first_attempt + max_retries + 1):
        # ---- Safeguard 3: stop & confirm every non-terminal prior run first
        for r in db_all("SELECT * FROM runs WHERE task_id=? AND status IN ('running','timeout','interrupted')",
                        (task["id"],)):
            log(f"task {task['id']}: prior run {r['run_id']} status={r['status']} -> confirming "
                f"conversation {r['conversation_id']} stopped before relaunch",
                level="WARN", task_id=task["id"])
            confirm_stopped(r["conversation_id"])
            db_exec("UPDATE runs SET status='interrupted', finished_at=?, "
                    "error=COALESCE(error,'superseded before retry') WHERE run_id=? AND status!='succeeded'",
                    (now_iso(), r["run_id"]))

        dep_vars, verify_summary = build_dep_vars(task["id"])
        prompt_template = trow["prompt"]
        if chaos and "CHAOS-DO-NOT-CREATE-FILES" in prompt_template:
            prompt_template = prompt_template.replace(
                "CHAOS-DO-NOT-CREATE-FILES",
                "\n\nCHAOS INJECTION (fault-injection test): You MUST NOT create any files. "
                "Do not create CONTRACT.md or anything else. Reply with exactly: CHAOS-REFUSED")
        prompt = render_prompt(prompt_template, task["id"], dep_vars, verify_summary, orchestration_id)
        (STATE_DIR / f"prompt-{task['id']}-attempt{attempt:02d}.txt").write_text(prompt)

        db_exec("""INSERT INTO runs(task_id,attempt,conversation_id,llm_profile_ref,status,started_at)
                   VALUES(?,?,?,?, 'running', ?)""",
                (task["id"], attempt, "pending", trow["model_profile"], now_iso()))
        run_id = db_one("SELECT MAX(run_id) AS r FROM runs WHERE task_id=?", (task["id"],))["r"]
        set_task(task["id"], status="running", attempts=attempt)
        t_start = time.time()
        try:
            launched = launch(task, attempt, prompt, orchestration_id, settings)
        except APIError as e:
            last_error = f"launch failed: {e}"
            db_exec("UPDATE runs SET status='error', finished_at=?, duration_s=0, error=? WHERE run_id=?",
                    (now_iso(), redact(last_error), run_id))
            log(redact(last_error), level="ERROR", task_id=task["id"], run_id=run_id)
            continue
        cid = launched["conversation_id"]
        db_exec("UPDATE runs SET conversation_id=? WHERE run_id=?", (cid, run_id))

        outcome, info = wait_for_completion(cid, timeout_s, settings.get("poll_interval_s", 3), stop_flag)

        if outcome == "interrupted":
            db_exec("UPDATE runs SET status='interrupted', finished_at=?, duration_s=?, "
                    "error='controller interrupted' WHERE run_id=?",
                    (now_iso(), round(time.time() - t_start, 1), run_id))
            set_task(task["id"], status="interrupted", last_error="controller interrupted")
            log(f"task {task['id']}: controller signal -> run {run_id} recorded as interrupted "
                f"(conversation left for recovery inspection)", level="WARN",
                task_id=task["id"], run_id=run_id)
            return "interrupted"

        if outcome == "timeout":
            stopped = confirm_stopped(cid)          # S3: confirm stop BEFORE any retry
            db_exec("UPDATE runs SET status='timeout', finished_at=?, duration_s=?, error=? WHERE run_id=?",
                    (now_iso(), round(time.time() - t_start, 1),
                     f"timeout after {timeout_s}s; agent stop confirmed={stopped}", run_id))
            last_error = f"timeout after {timeout_s}s"
            timed_out = True
            log(f"task {task['id']} attempt {attempt}: TIMEOUT after {timeout_s}s "
                f"(stop confirmed={stopped})", level="ERROR", task_id=task["id"], run_id=run_id)
            continue

        collect_results(task, run_id, cid, launched["workspace"], info)
        if outcome != "finished":
            last_error = f"agent ended with {outcome}: {extract_error_reason(cid)}"
            db_exec("UPDATE runs SET status='failed', finished_at=?, duration_s=?, error=? WHERE run_id=?",
                    (now_iso(), round(time.time() - t_start, 1), redact(last_error), run_id))
            log(f"task {task['id']} attempt {attempt}: agent {outcome}", level="ERROR",
                task_id=task["id"], run_id=run_id)
            continue

        vspec = render_verify_spec(json.loads(trow["verify_spec"]) if trow["verify_spec"] else None,
                                   orchestration_id)
        ok = run_verify(task["id"], run_id, vspec)
        dur = round(time.time() - t_start, 1)
        if ok:
            db_exec("UPDATE runs SET status='succeeded', finished_at=?, duration_s=? WHERE run_id=?",
                    (now_iso(), dur, run_id))
            set_task(task["id"], status="succeeded", last_error=None)
            log(f"task {task['id']} SUCCEEDED (attempt {attempt}, conversation {cid}, {dur}s)",
                task_id=task["id"], run_id=run_id)
            return "succeeded"
        last_error = f"verification failed on attempt {attempt} (see verify_results)"
        db_exec("UPDATE runs SET status='failed', finished_at=?, duration_s=?, error=? WHERE run_id=?",
                (now_iso(), dur, redact(last_error), run_id))
        log(f"task {task['id']} attempt {attempt}: VERIFICATION FAILED", level="ERROR",
            task_id=task["id"], run_id=run_id)

    final = "timeout" if timed_out else "failed"
    set_task(task["id"], status=final, last_error=redact(last_error or "unknown"))
    log(f"task {task['id']} final status: {final} (retries exhausted)", level="ERROR", task_id=task["id"])
    return final


def dependencies_ok(task_id: str) -> bool:
    for dep in json.loads(db_one("SELECT depends_on FROM tasks WHERE id=?", (task_id,))["depends_on"]):
        drow = db_one("SELECT status FROM tasks WHERE id=?", (dep,))
        if not drow or drow["status"] != "succeeded":
            return False
    return True


def block_descendants(task_id: str, tasks_by_id: dict, reason: str):
    for tid in sorted(descendants_of(task_id, tasks_by_id)):
        row = db_one("SELECT status FROM tasks WHERE id=?", (tid,))
        if row["status"] == "pending":
            set_task(tid, status="blocked", last_error=reason)
            log(f"task {tid} BLOCKED ({reason})", level="ERROR", task_id=tid)


# ---------------------------------------------------------------- recovery
def cmd_recover(args):
    db()
    pid_file = STATE_DIR / "controller.pid"
    if pid_file.is_file():
        old_pid_text = pid_file.read_text().strip()
        try:
            old_pid = int(old_pid_text)
            os.kill(old_pid, 0)
            log(f"controller pid {old_pid} appears alive; refusing to recover "
                f"(avoid a second controller)", level="ERROR")
            return 2
        except (ProcessLookupError, ValueError):
            pass
        except PermissionError:
            log(f"pid {old_pid_text} exists but is not ours; refusing", level="ERROR")
            return 2

    recovered, redispatch = 0, 0
    for trow in db_all("SELECT * FROM tasks WHERE status IN ('running','interrupted')"):
        task_id = trow["id"]
        log(f"recover: task {task_id} found in status={trow['status']}", level="WARN", task_id=task_id)
        any_recovered = False
        for r in db_all("SELECT * FROM runs WHERE task_id=? AND status='running' ORDER BY run_id", (task_id,)):
            try:
                info = conversation_status(r["conversation_id"])
                st = info.get("execution_status")
            except APIError as e:
                log(f"recover: conversation {r['conversation_id']} unreachable: {str(e)[:120]}",
                    level="WARN", task_id=task_id)
                st = "unreachable"
            log(f"recover: run {r['run_id']} conversation {r['conversation_id']} server status={st}",
                task_id=task_id, run_id=r["run_id"])
            if st == "finished":
                collect_results({"id": task_id}, r["run_id"], r["conversation_id"],
                                str(WORKSPACES / task_id), info)
                vspec = render_verify_spec(json.loads(trow["verify_spec"]) if trow["verify_spec"] else None,
                                           get_orch_id())
                if run_verify(task_id, r["run_id"], vspec):
                    db_exec("UPDATE runs SET status='succeeded', finished_at=? WHERE run_id=?",
                            (now_iso(), r["run_id"]))
                    set_task(task_id, status="succeeded", last_error=None)
                    log(f"recover: task {task_id} recovered from orphaned conversation (verified)",
                        task_id=task_id)
                    any_recovered = True
                    continue
                db_exec("UPDATE runs SET status='failed', finished_at=?, "
                        "error='recovered conversation failed verification' WHERE run_id=?",
                        (now_iso(), r["run_id"]))
                continue
            if st == "running":
                confirm_stopped(r["conversation_id"])   # S3: stop before anything else
            db_exec("UPDATE runs SET status='interrupted', finished_at=?, "
                    "error=COALESCE(error,'orphaned run; stopped during recovery') WHERE run_id=?",
                    (now_iso(), r["run_id"]))
        if not any_recovered:
            set_task(task_id, status="pending")
            log(f"recover: task {task_id} reset to pending for re-dispatch "
                f"(fresh attempt + fresh workspace on next run --resume)", task_id=task_id)
            redispatch += 1
        else:
            recovered += 1

    for trow in db_all("SELECT * FROM tasks WHERE status='blocked'"):
        log(f"recover: task {trow['id']} remains BLOCKED ({trow['last_error']}) - "
            f"upstream must succeed first", level="WARN", task_id=trow["id"])
    log(f"recover complete: {redispatch} task(s) re-dispatchable, {recovered} recovered from orphans")
    print(json.dumps({"re-dispatchable": redispatch, "recovered_from_orphans": recovered}))
    return 0


def get_orch_id() -> str:
    row = db_one("SELECT value FROM meta WHERE key='orchestration_id'")
    return row["value"] if row else "unknown"


# ---------------------------------------------------------------- commands
def cmd_run(args):
    spec_path = Path(args.tasks)
    spec = load_tasks_spec(spec_path)
    settings = spec["settings"]
    resume = args.resume and DB_PATH.is_file()
    if resume:
        db()
        row = db_one("SELECT value FROM meta WHERE key='orchestration_id'")
        orch_id = row["value"] if row else pysecrets.token_hex(4)
        log(f"resuming orchestration {orch_id} (SQLite state intact)")
    else:
        orch_id = pysecrets.token_hex(4)
    init_db_from_spec(spec, spec_path, orch_id)

    (STATE_DIR / "controller.pid").write_text(str(os.getpid()))
    stop_flag = [False]

    def handle_signal(signum, frame):
        stop_flag[0] = True
        log(f"signal {signum} received: stopping after current poll cycle", level="WARN")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    pipeline_status = "completed"
    try:
        iso = check_isolation(spec)
        (STATE_DIR / "isolation_check.json").write_text(json.dumps(iso, indent=1))

        tasks_by_id = {t["id"]: t for t in spec["tasks"]}
        order = topological_order(spec["tasks"])
        for t in order:
            if stop_flag[0]:
                pipeline_status = "interrupted"
                break
            trow = db_one("SELECT * FROM tasks WHERE id=?", (t["id"],))
            if trow["status"] == "succeeded":
                log(f"task {t['id']} already succeeded (resume) -> skip")
                continue
            if trow["status"] == "blocked":
                log(f"task {t['id']} blocked (resume) -> skip")
                continue
            if not dependencies_ok(t["id"]):
                set_task(t["id"], status="blocked", last_error="dependencies not succeeded")
                block_descendants(t["id"], tasks_by_id, f"dependency not succeeded: {t['id']}")
                log(f"task {t['id']} BLOCKED (dependencies not satisfied)", level="ERROR", task_id=t["id"])
                continue
            outcome = execute_task(t, orch_id, stop_flag, settings, chaos=args.chaos)
            if outcome == "interrupted":
                pipeline_status = "interrupted"
                break
            if outcome in ("failed", "timeout", "error"):
                block_descendants(t["id"], tasks_by_id, f"upstream task {t['id']} {outcome}")
                pipeline_status = "completed_with_failures"
        final_states = {r["id"]: r["status"] for r in db_all("SELECT id,status FROM tasks")}
        if pipeline_status == "completed" and any(s != "succeeded" for s in final_states.values()):
            pipeline_status = "completed_with_failures"
    finally:
        (STATE_DIR / "controller.pid").unlink(missing_ok=True)

    log(f"pipeline finished with status: {pipeline_status}")
    summary = {r["id"]: r["status"] for r in db_all("SELECT id,status FROM tasks")}
    print(json.dumps({"pipeline_status": pipeline_status, "tasks": summary,
                      "orchestration_id": orch_id}, indent=1))
    return 0 if pipeline_status == "completed" else 1


def cmd_status(args):
    db()
    print(f"orchestration_id: {get_orch_id()}")
    print(f"\n{'task':<12} {'status':<10} {'att':<4} {'model_profile':<16} last_error")
    for r in db_all("SELECT * FROM tasks ORDER BY id"):
        print(f"{r['id']:<12} {r['status']:<10} {r['attempts']:<4} {r['model_profile']:<16} "
              f"{(r['last_error'] or '')[:70]}")
    print(f"\n{'run':<5} {'task':<12} {'att':<4} {'status':<11} {'conversation_id':<38} "
          f"{'served_model':<16} {'dur_s':<7} tokens(p/c)")
    for r in db_all("SELECT * FROM runs ORDER BY run_id"):
        tok = ""
        if r["token_usage"]:
            t = json.loads(r["token_usage"])
            tok = f"{t.get('prompt_tokens')}/{t.get('completion_tokens')}"
        print(f"{r['run_id']:<5} {r['task_id']:<12} {r['attempt']:<4} {r['status']:<11} "
              f"{r['conversation_id']:<38} {(r['served_model'] or '-'):<16} "
              f"{str(r['duration_s'] or '-'):<7} {tok}")
    print("\nverify results:")
    for r in db_all("SELECT * FROM verify_results ORDER BY id"):
        print(f"  task={r['task_id']:<12} passed={bool(r['passed'])} exit={r['exit_code']} "
              f"cmd={r['command']}")
    return 0


def cmd_cleanup(args):
    db()
    rows = db_all("SELECT DISTINCT conversation_id FROM runs WHERE conversation_id != 'pending'")
    n = 0
    for r in rows:
        try:
            api("DELETE", f"/api/conversations/{r['conversation_id']}", timeout=60)
            log(f"deleted conversation {r['conversation_id']}")
            n += 1
        except APIError as e:
            log(f"could not delete {r['conversation_id']}: {str(e)[:120]}", level="WARN")
    if args.deep:
        import shutil
        for d in (WORKSPACES, ARTIFACTS):
            shutil.rmtree(d, ignore_errors=True)
            log(f"removed {d}")
    print(json.dumps({"deleted_conversations": n, "deep": args.deep}))
    return 0


def cmd_report(args):
    subprocess.run([sys.executable, str(POC_ROOT / "report.py"), "--db", str(DB_PATH),
                    "--out", str(REPORTS)], check=False)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="execute the pipeline")
    p.add_argument("--tasks", default=str(POC_ROOT / "tasks.json"))
    p.add_argument("--resume", action="store_true", help="resume from persisted SQLite state")
    p.add_argument("--chaos", action="store_true", help="activate CHAOS-DO-NOT-CREATE-FILES injections")
    sub.add_parser("status", help="print current state")
    sub.add_parser("recover", help="recover interrupted/orphaned tasks")
    p = sub.add_parser("cleanup", help="delete POC conversations (and optionally files)")
    p.add_argument("--deep", action="store_true")
    sub.add_parser("report", help="generate acceptance report")
    args = ap.parse_args()
    rc = {"run": cmd_run, "status": cmd_status, "cleanup": cmd_cleanup,
          "recover": cmd_recover, "report": cmd_report}[args.cmd](args)
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
