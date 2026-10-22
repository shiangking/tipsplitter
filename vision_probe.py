#!/usr/bin/env python3
"""E2-A vision capability probe — empirical, per-profile, ground-truth verified.

For each LLM profile, this launches two throwaway OpenHands conversations:

  Round 1 (code-image): the model must read a 6-char code from a rendered PNG
  (text drawn by PIL at large size). Verifies actual image comprehension.

  Round 2 (screenshot-analysis): the model must answer structural questions
  about a rendered mock UI screenshot (number of buttons, page type, primary
  button label). Mirrors the real visual-review gate's use case.

A profile passes a round only if its reply matches the ground truth. Results
are recorded to evidence JSON + docs/VISION-CAPABILITY.md. No vision gate may
claim success for a profile that fails here.

Usage: python3 vision_probe.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).parent))
from controller import APIError, api, log  # noqa: E402

ROOT = Path(__file__).parent
EVIDENCE_DIR = ROOT / "state" / "vision-probe"
DOC_PATH = ROOT / "docs" / "VISION-CAPABILITY.md"
PROFILES = ["glm5.3", "deepseekv4.1", "qwen3.8flash"]
IO_TIMEOUT_S = 150          # per-image reply wait
POLL_S = 3


# --------------------------------------------------------------------------
# image construction
# --------------------------------------------------------------------------
def _font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_code_image(code: str) -> str:
    """Render a code image and return a PNG data URL."""
    W, H = 640, 320
    img = PILImage.new("RGB", (W, H), (245, 246, 250))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, W - 20, H - 20], outline=(60, 70, 90), width=4)
    d.text((40, 45), "VERIFICATION IMAGE", font=_font(30), fill=(30, 40, 60))
    d.text((40, 95), "Enter this code exactly:", font=_font(24), fill=(60, 70, 90))
    d.text((40, 150), code, font=_font(84), fill=(10, 30, 120))
    d.text((40, 265), f"Background shade: {img.getpixel((W // 2, H // 2))[:3]}",
           font=_font(18), fill=(90, 100, 110))
    path = EVIDENCE_DIR / f"probe-code-{code}.png"
    img.save(path)
    return _data_url(path)


def make_mock_ui_image(primary_label: str, n_buttons: int) -> str:
    """Render a simple mock dashboard UI and return a PNG data URL."""
    W, H = 900, 560
    img = PILImage.new("RGB", (W, H), (238, 240, 245))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 70], fill=(28, 34, 51))                    # navbar
    d.text((28, 22), "Acme Dashboard", font=_font(26), fill=(255, 255, 255))
    d.rectangle([0, 70, 190, H], fill=(35, 42, 62))                  # sidebar
    for i, item in enumerate(["Overview", "Reports", "Settings"]):
        d.text((28, 100 + i * 44), item, font=_font(20), fill=(200, 205, 215))
    # three stat cards
    for i, (label, val) in enumerate([("Users", "1,204"), ("Revenue", "$8,310"),
                                      ("Errors", "3")]):
        x0 = 220 + i * 220
        d.rounded_rectangle([x0, 100, x0 + 200, 200], 10, fill=(255, 255, 255))
        d.text((x0 + 16, 118), label, font=_font(18), fill=(90, 100, 115))
        d.text((x0 + 16, 150), val, font=_font(30), fill=(20, 30, 50))
    # buttons row
    labels = [primary_label] + ["Cancel", "More"][: n_buttons - 1]
    for i, lab in enumerate(labels):
        x0 = 220 + i * 180
        fill = (30, 110, 220) if i == 0 else (200, 205, 212)
        txt = (255, 255, 255) if i == 0 else (40, 45, 55)
        d.rounded_rectangle([x0, 250, x0 + 160, 300], 8, fill=fill)
        d.text((x0 + 20, 265), lab, font=_font(20), fill=txt)
    # chart placeholder
    d.rectangle([220, 330, W - 30, H - 30], fill=(255, 255, 255))
    for i in range(12):
        x0 = 240 + i * 50
        h = 40 + (i * 37) % 130
        d.rectangle([x0, H - 40 - h, x0 + 34, H - 40], fill=(70, 130, 220))
    path = EVIDENCE_DIR / f"probe-ui-{primary_label.lower()}-{n_buttons}.png"
    img.save(path)
    return _data_url(path)


def _data_url(path: Path) -> str:
    import base64
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


# --------------------------------------------------------------------------
# conversation mechanics (throwaway, tag-scoped, deleted afterwards)
# --------------------------------------------------------------------------
def launch_probe(profile: str, prompt: str, image_url: str, seq: str) -> str:
    # reuse the acceptance-proven launch path: active profile + per-launch
    # llm_profile_ref override (server resolves credentials; none touch agent code)
    pid = api("GET", "/api/settings")[1].get("active_agent_profile_id")
    cid = str(uuid.uuid4())
    body = {
        "conversation_id": cid,
        "agent_profile_id": pid,
        "agent_launch_additions": {"llm_profile_ref": profile},
        "workspace": {"kind": "LocalWorkspace",
                      "working_dir": str(EVIDENCE_DIR / f"ws-{profile}-{seq}")},
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": 5,
        "stuck_detection": True,
        "tags": {"poc": "visionprobe", "profile": profile, "round": seq},
        "initial_message": {"role": "user", "run": True, "content": [
            {"type": "text", "text": prompt},
            {"type": "image", "image_urls": [image_url]},
        ]},
    }
    st, _ = api("POST", "/api/conversations", body, timeout=120)
    if st != 201:
        raise APIError(f"{profile}: conversation create returned HTTP {st}")
    return cid


def wait_reply(cid: str, deadline_s: float = IO_TIMEOUT_S) -> tuple[str, dict]:
    t0 = time.time()
    status = None
    while time.time() - t0 < deadline_s:
        time.sleep(POLL_S)
        _, info = api("GET", f"/api/conversations/{cid}")
        status = info.get("execution_status")
        if status in ("idle", "finished", "error", "stuck", "paused"):
            break
    try:
        _, fr = api("GET", f"/api/conversations/{cid}/agent_final_response")
        resp = (fr or {}).get("response") or ""
    except APIError:
        resp = ""
    usage = (info.get("stats") or {}).get("usage_to_metrics") or {}
    return resp.strip(), {"status": status, "usage": usage}


def delete_probe(cid: str):
    try:
        api("DELETE", f"/api/conversations/{cid}")
    except APIError:
        pass


# --------------------------------------------------------------------------
# rounds
# --------------------------------------------------------------------------
def round_code(profile: str) -> dict:
    code = f"VP{uuid.uuid4().hex[:4].upper()}"
    img = make_code_image(code)
    prompt = (
        "Look at the attached verification image and reply with ONLY the code "
        "shown in large text, in the format VP-XXXX. No other text."
    )
    cid = launch_probe(profile, prompt, img, "code")
    resp, meta = wait_reply(cid)
    delete_probe(cid)
    got = re.sub(r"[^A-Z0-9]", "", resp.upper())
    ok = code in got
    return {"round": "code-image", "profile": profile, "conversation_id": cid,
            "ground_truth": code, "reply_excerpt": resp[:120], "passed": ok,
            "status": meta["status"], "usage": meta["usage"],
            "evidence_image": str(EVIDENCE_DIR / f"probe-code-{code}.png")}


def round_ui(profile: str) -> dict:
    primary = f"{uuid.uuid4().hex[:6].capitalize()}Now"
    n_buttons = 3
    img = make_mock_ui_image(primary, n_buttons)
    prompt = (
        "The attached image is a screenshot of a web app dashboard. Answer in "
        "this exact format, one per line:\n"
        f"PRIMARY_BUTTON=<label of the blue primary button>\n"
        f"BUTTON_COUNT=<total number of buttons visible>\n"
        "No other text."
    )
    cid = launch_probe(profile, prompt, img, "ui")
    resp, meta = wait_reply(cid)
    delete_probe(cid)
    pm = re.search(r"PRIMARY_BUTTON\s*=\s*(.+)", resp, re.I)
    bm = re.search(r"BUTTON_COUNT\s*=\s*(\d+)", resp, re.I)
    ok_label = bool(pm) and primary.lower() in pm.group(1).strip().lower()
    ok_count = bool(bm) and int(bm.group(1)) == n_buttons
    return {"round": "screenshot-analysis", "profile": profile,
            "conversation_id": cid, "ground_truth": {
                "primary_label": primary, "button_count": n_buttons},
            "reply_excerpt": resp[:200], "passed": ok_label and ok_count,
            "status": meta["status"], "usage": meta["usage"],
            "evidence_image": str(EVIDENCE_DIR /
                                  f"probe-ui-{primary.lower()}-{n_buttons}.png")}


# --------------------------------------------------------------------------
def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT / "docs").mkdir(exist_ok=True)
    results = []
    for profile in PROFILES:
        for fn in (round_code, round_ui):
            try:
                r = fn(profile)
            except APIError as e:
                r = {"round": fn.__name__, "profile": profile, "passed": False,
                     "error": str(e)[:200]}
            log(f"vision probe {profile}/{r['round']}: "
                f"{'PASS' if r.get('passed') else 'FAIL'}"
                + (f" ({r.get('error', '')[:80]})" if r.get("error") else ""))
            results.append(r)

    verdict = {}
    for profile in PROFILES:
        rs = [r for r in results if r["profile"] == profile]
        verdict[profile] = {
            "code_image": any(r["passed"] for r in rs if r["round"] == "code-image"),
            "screenshot_analysis": any(r["passed"] for r in rs
                                       if r["round"] == "screenshot-analysis"),
        }
        verdict[profile]["vision_capable"] = all(verdict[profile].values())

    ev = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
          "method": "2 rounds per profile, ground-truth verified, throwaway "
                    "conversations (deleted after)",
          "results": results, "verdict": verdict}
    out = EVIDENCE_DIR / "vision-probe-evidence.json"
    out.write_text(json.dumps(ev, indent=2))

    lines = ["# Vision Capability Matrix (E2-A probe)",
             "",
             f"Generated: {ev['generated_at']}",
             "",
             "| Profile | code-image round | screenshot-analysis round | Vision-capable |",
             "|---|---|---|---|"]
    for p in PROFILES:
        v = verdict[p]
        lines.append(f"| `{p}` | {'✅' if v['code_image'] else '❌'} | "
                     f"{'✅' if v['screenshot_analysis'] else '❌'} | "
                     f"**{'YES' if v['vision_capable'] else 'NO'}** |")
    lines += ["",
              "Probe method: for each profile two throwaway conversations were "
              "launched with an image attachment (PNG data URL). Round 1 requires "
              "reading a 6-character code from a rendered image; round 2 requires "
              "structural analysis of a rendered mock UI screenshot. Answers were "
              "checked against ground truth known only to this script. Probe "
              "conversations are deleted after each round; evidence images and "
              "raw replies are kept under `state/vision-probe/`.",
              "",
              "**Policy:** visual-review gates may only use profiles marked "
              "vision-capable here. If none qualify, visual gates report "
              "`unavailable` and block unattended product-quality acceptance."]
    DOC_PATH.write_text("\n".join(lines) + "\n")
    log(f"vision probe complete -> {out}")
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())


def vision_available() -> bool:
    """Authoritative vision-capability lookup (single source of truth:
    the recorded E2-A probe evidence). Returns True only if at least one
    profile passed BOTH probe rounds."""
    ev = EVIDENCE_DIR / "vision-probe-evidence.json"
    if not ev.exists():
        return False
    try:
        data = json.loads(ev.read_text())
        return any(v.get("vision_capable")
                   for v in (data.get("verdict") or {}).values())
    except (json.JSONDecodeError, OSError):
        return False
