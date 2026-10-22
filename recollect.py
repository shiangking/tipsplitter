#!/usr/bin/env python3
"""Recollect artifacts for recorded runs from their live conversations.

Evidence recoverability utility: the SQLite DB persists conversation IDs and
expected artifacts; this tool refetches them from the agent server and rebuilds
the artifacts/ tree. Useful after local artifact loss (as demonstrated when a
cleanup --deep removed artifacts/ while the DB and server-side conversations
remained intact).

Usage:
    python3 recollect.py [state_dir]

Default state_dir: value of AOP_STATE_DIR or ./state
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from controller import APIError, api, redact  # noqa: E402


def recollect(state_dir: Path) -> int:
    db = sqlite3.connect(state_dir / "orchestration.db")
    db.row_factory = sqlite3.Row
    tasks = {r["id"]: r for r in db.execute("SELECT id, expected_artifacts FROM tasks")}
    runs = db.execute(
        "SELECT run_id, task_id, attempt, status, conversation_id FROM runs ORDER BY run_id"
    ).fetchall()

    artifacts_root = state_dir.parent / "artifacts"
    rebuilt, missing = 0, 0
    for run in runs:
        if run["status"] != "succeeded":
            continue
        cid = run["conversation_id"]
        artifacts = json.loads(tasks[run["task_id"]]["expected_artifacts"])
        task_dir = artifacts_root / run["task_id"]
        task_dir.mkdir(parents=True, exist_ok=True)
        files = []
        try:
            _, fr = api("GET", f"/api/conversations/{cid}/agent_final_response")
            resp = (fr or {}).get("response") or ""
            if resp.strip():
                (task_dir / "final_response.md").write_text(resp)
                files.append("final_response.md")
        except (APIError, Exception) as e:  # noqa: BLE001
            print(f"  run {run['run_id']}: final_response unavailable: {redact(str(e))[:100]}")
        for rel in artifacts or []:
            got = False
            # 1) try the live workspace file endpoint
            try:
                st, raw = api("GET", f"/api/conversations/{cid}/workspace/{rel}",
                              expect_json=False)
                if st == 200 and isinstance(raw, str) and raw.strip():
                    (task_dir / rel).write_text(raw)
                    files.append(f"{rel} (workspace)")
                    got = True
            except (APIError, Exception):  # noqa: BLE001
                pass
            # 2) fall back to the server-side event store: file_editor 'create'
            #    ActionEvents carry the full file_text
            if not got:
                content = None
                try:
                    _, evd = api("GET", f"/api/conversations/{cid}/events/search?limit=100")
                    for e in evd.get("items") or []:
                        if e.get("kind") != "ActionEvent":
                            continue
                        a = e.get("action") or {}
                        if (a.get("command") == "create"
                                and str(a.get("path", "")).endswith("/" + rel)
                                and a.get("file_text")):
                            content = a["file_text"]  # keep the LAST write
                except (APIError, Exception) as e:  # noqa: BLE001
                    print(f"  run {run['run_id']}: events unavailable: "
                          f"{redact(str(e))[:100]}")
                if content:
                    (task_dir / rel).write_text(content)
                    files.append(f"{rel} (from events)")
                    got = True
            if not got:
                missing += 1
                print(f"  run {run['run_id']}: artifact {rel} unavailable")
        print(f"  run {run['run_id']} task={run['task_id']} conv={cid[:8]}… "
              f"rebuilt: {files or 'none'}")
        rebuilt += len(files)
    print(f"recollect complete: {rebuilt} file(s) rebuilt, {missing} unavailable")
    return 0


if __name__ == "__main__":
    sd = Path(sys.argv[1] if len(sys.argv) > 1
              else os.environ.get("AOP_STATE_DIR", "state"))
    sys.exit(recollect(sd))
