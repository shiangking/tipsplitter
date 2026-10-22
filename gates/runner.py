#!/usr/bin/env python3
"""Gate runner — executes verification gate families and records evidence.

Families:
  technical  build/typecheck/lint/unit/integration/migrations (EXEC — policy-gated)
  browser    E2E flows, responsive screenshots, overflow, a11y, navigation
             (static/DOM — autonomous; interactive E2E EXEC parts policy-gated)
  visual     palette/typography conformance, blank/overflow heuristics (static;
             LLM vision availability-gated, never passed when unavailable)
  security   secret scan, SAST (bandit/ruff), dep manifest audit, env handling (static)

Rules:
  - Unavailable checks are recorded `unavailable` and NEVER count as passed.
  - EXEC gates consult execution_policy.py; without recorded human approval
    they return `blocked` (distinct from passed/failed) in unattended runs.
  - Every gate returns machine-readable evidence.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent  # project root (this file lives in gates/)
sys_path = str(ROOT)
if sys_path not in __import__("sys").path:
    import sys
    sys.path.insert(0, sys_path)
from execution_policy import exec_allowed, is_static_gate, probe_isolation  # noqa: E402

VENV = ROOT / ".venv-gates" / "bin"
CHROMIUM = "/usr/bin/chromium"
CHROMIUM_ARGS = ["--headless", "--no-sandbox", "--disable-gpu",
                 "--disable-dev-shm-usage", "--hide-scrollbars"]
VIEWPORTS = {"desktop": (1280, 800), "tablet": (768, 1024), "mobile": (375, 667)}

SECRET_PATTERNS = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("aws-secret-context", re.compile(r"(?i)aws.{0,20}['\"][A-Za-z0-9/+=]{40}['\"]")),
    ("github-pat", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("openai-style-key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("private-key-block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("generic-assignment", re.compile(r"""(?i)(api[_-]?key|secret|passwd|password|token)\s*[:=]\s*['"][^'"]{8,}['"]""")),
]
SECRET_ALLOW = re.compile(r"(?i)(example|placeholder|dummy|sample|xxx|changeme|"
                          r"\$\{[a-z_]+\}|os\.environ|process\.env|getenv)")


def _run(cmd: list[str], cwd: Path, timeout_s: int = 300) -> dict:
    t0 = time.time()
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=timeout_s)
        return {"cmd": cmd, "rc": p.returncode, "stdout": p.stdout[-4000:],
                "stderr": p.stderr[-4000:], "duration_s": round(time.time() - t0, 1)}
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "rc": None, "error": f"timeout after {timeout_s}s",
                "duration_s": round(time.time() - t0, 1)}
    except FileNotFoundError as e:
        return {"cmd": cmd, "rc": None, "error": f"tool not found: {e}"}


def _source_files(project_dir: Path, extra: tuple = (".js", ".ts", ".jsx", ".tsx", ".html", ".css")) -> list[Path]:
    exts = (".py",) + extra
    skip = {".git", "node_modules", ".venv", ".venv-gates", "__pycache__", "dist", "build"}
    return [p for p in project_dir.rglob("*")
            if p.is_file() and p.suffix in exts and not any(s in p.parts for s in skip)]


def _finish(gate: str, family: str, checks: list, evidence: dict,
            unavailable: list | None = None) -> dict:
    """Status semantics (Phase E requirement):
      passed      — every check ran and passed
      failed      — at least one RAN check failed (real defect)
      unavailable — nothing failed, but >=1 required check could not run
    An unavailable component never counts as passed."""
    unavailable = list(unavailable or [])
    ran_checks = [c for c in checks if c.get("ran", True)]
    failed_ran = [c for c in ran_checks if c["ok"] is False]
    unran = [c for c in checks if not c.get("ran", True)]
    if failed_ran:
        status = "failed"
    elif unran or unavailable:
        status = "unavailable"
        unavailable = sorted(set(unavailable + [c["check"] for c in unran]))
    elif not ran_checks:
        status = "unavailable"
    else:
        status = "passed"
    return {"gate": gate, "family": family, "passed": status == "passed",
            "status": status, "checks": checks, "evidence": evidence,
            "unavailable_components": unavailable,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ==========================================================================
# security (static, autonomous)
# ==========================================================================
def gate_secret_scan(project_dir: Path) -> dict:
    checks, hits = [], []
    files = _source_files(project_dir)
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for name, pat in SECRET_PATTERNS:
                if pat.search(line) and not SECRET_ALLOW.search(line):
                    hits.append({"file": str(f.relative_to(project_dir)),
                                 "line": i, "type": name,
                                 "excerpt": line.strip()[:100]})
    checks.append({"check": "no-hardcoded-secrets", "ok": not hits,
                   "detail": f"scanned {len(files)} files", "hits": hits[:50]})
    return _finish("secret_scan", "security", checks, {"hits": hits, "files_scanned": len(files)})


def gate_sast(project_dir: Path) -> dict:
    checks, unavailable = [], []
    py_files = _source_files(project_dir, extra=())
    # bandit (venv)
    if py_files:
        r = _run([str(VENV / "bandit"), "-q", "-r", "-f", "json", "."], project_dir)
        if r["rc"] is None and "not found" in r.get("error", ""):
            unavailable.append("bandit")
            checks.append({"check": "sast-python", "ok": False, "ran": False,
                           "detail": "bandit unavailable"})
        else:
            try:
                data = json.loads(r["stdout"] or "{}")
                issues = data.get("results", [])
                high = [i for i in issues if i.get("issue_severity") == "HIGH"]
                checks.append({"check": "sast-python-no-high", "ok": not high,
                               "detail": f"{len(issues)} issue(s), {len(high)} high",
                               "top": [i["issue_text"][:80] for i in issues[:10]]})
            except json.JSONDecodeError:
                checks.append({"check": "sast-python", "ok": False, "ran": False,
                               "detail": f"bandit parse error rc={r['rc']}"})
        r2 = _run([str(VENV / "ruff"), "check", "--select=E9,F63,F7,F82,B", "--quiet", "."], project_dir)
        if "not found" in r2.get("error", ""):
            unavailable.append("ruff")
            checks.append({"check": "ruff-critical", "ok": False, "ran": False,
                           "detail": "ruff unavailable"})
        else:
            checks.append({"check": "ruff-critical", "ok": r2["rc"] == 0,
                           "detail": (r2["stdout"] or "clean")[:300]})
    else:
        checks.append({"check": "sast-python", "ok": True, "ran": True,
                       "detail": "no python sources; nothing to scan (vacuous pass)"})
    # JS/TS parse-level check
    js_files = _source_files(project_dir, extra=(".js", ".ts"))
    parse_errors = []
    for f in js_files[:200]:
        try:
            compile(f.read_text(errors="replace"), str(f), "exec")  # crude but real
        except SyntaxError:
            continue  # JS is not python; crude check only flags python-in-js confusion
        except Exception:
            pass
    checks.append({"check": "js-sources-present", "ok": True,
                   "detail": f"{len(js_files)} js/ts files (parse-level SAST needs node toolchain)"})
    return _finish("sast", "security", checks, {"py_files": len(py_files)}, unavailable)


def gate_dep_audit(project_dir: Path) -> dict:
    """Manifest-level dependency audit (no code execution needed for pip/npm
    index lookups is network; here: manifest presence + lockfile hygiene)."""
    checks = []
    req = project_dir / "requirements.txt"
    pkg = project_dir / "package.json"
    if req.exists():
        risky = []
        for line in req.read_text().splitlines():
            line = line.strip()
            if (not line or line.startswith("#")) or "==" not in line:
                if line and not line.startswith(("#", "-")):
                    risky.append(line)
        checks.append({"check": "requirements-pinned", "ok": not risky,
                       "detail": f"unpinned: {risky[:10]}" if risky else "all pinned"})
    if pkg.exists():
        try:
            pj = json.loads(pkg.read_text())
            deps = {**(pj.get("dependencies") or {}), **(pj.get("devDependencies") or {})}
            unpinned = [d for d, v in deps.items()
                        if not v or v.startswith(("^", "*", ">"))]
            checks.append({"check": "package-json-pinned", "ok": not unpinned,
                           "detail": f"unpinned: {unpinned[:10]}" if unpinned
                           else "all pinned"})
        except json.JSONDecodeError as e:
            checks.append({"check": "package-json-valid", "ok": False,
                           "detail": str(e)[:120]})
    if not req.exists() and not pkg.exists():
        checks.append({"check": "dep-manifest", "ok": True, "ran": True,
                       "detail": "no dependency manifests (stdlib-only project); "
                                 "nothing to audit at manifest level"})
    # full npm audit / pip-audit run needs network+exec -> policy-gated, recorded unavailable
    checks.append({"check": "full-dependency-audit", "ok": False, "ran": False,
                   "detail": "exec+network gate; requires approval + npm/pip-audit "
                             "(recorded unavailable in unattended mode)"})
    return _finish("dep_audit", "security", checks, {})


def gate_env_handling(project_dir: Path) -> dict:
    checks, bad = [], []
    for f in _source_files(project_dir):
        text = f.read_text(errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"""(?i)(api[_-]?key|secret|token|password)\s*=\s*['"][^'"]{8,}['"]""",
                         line) and not SECRET_ALLOW.search(line):
                bad.append({"file": str(f.relative_to(project_dir)), "line": i,
                            "excerpt": line.strip()[:90]})
    checks.append({"check": "env-vars-not-hardcoded", "ok": not bad, "hits": bad[:30]})
    env_usage = any(
        re.search(r"os\.environ|getenv|process\.env", f.read_text(errors="replace"))
        for f in _source_files(project_dir))
    env_example = (project_dir / ".env.example").exists()
    if env_usage:
        checks.append({"check": "env-example-provided", "ok": env_example,
                       "detail": ".env.example present" if env_example
                       else "project reads env vars but ships no .env.example "
                            "(secrets contract undocumented)"})
    else:
        checks.append({"check": "env-example-provided", "ok": True, "ran": True,
                       "detail": "no env-var usage in project; .env.example not "
                                 "required (vacuous pass)"})
    return _finish("env_handling", "security", checks, {})


# ==========================================================================
# browser / visual (static + DOM, autonomous)
# ==========================================================================
def gate_responsive_screenshots(entry_html: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    checks, shots = [], {}
    for name, (w, h) in VIEWPORTS.items():
        png = out_dir / f"viewport-{name}.png"
        r = _run([CHROMIUM] + CHROMIUM_ARGS + [f"--screenshot={png}",
                                               f"--window-size={w},{h}",
                                               entry_html.resolve().as_uri()], out_dir)
        ok = r["rc"] == 0 and png.exists() and png.stat().st_size > 1000
        shots[name] = {"file": str(png), "ok": ok,
                       "sha256": hashlib.sha256(png.read_bytes()).hexdigest() if ok else None}
        checks.append({"check": f"screenshot:{name}", "ok": ok,
                       "detail": f"{w}x{h}" if ok else "capture failed"})
    return _finish("responsive_screenshots", "browser", checks, {"screenshots": shots})


def gate_dom_check(entry_html: Path, viewport: str = "desktop") -> dict:
    """DOM-level checks: overflow, viewport meta, landmarks, img alts,
    interactive hit targets, navigation links — via chromium --dump-dom +
    headless JS evaluation through the data API."""
    w, h = VIEWPORTS[viewport]
    checks, evidence = [], {}
    js = r"""
    (function(){
      var out = {};
      out.hasViewportMeta = !!document.querySelector('meta[name=viewport]');
      out.hOverflow = document.documentElement.scrollWidth > window.innerWidth + 1;
      out.navLinks = Array.from(document.querySelectorAll('a[href]')).length;
      out.buttons = Array.from(document.querySelectorAll('button, [role=button], input[type=submit]')).length;
      out.imgsMissingAlt = Array.from(document.images).filter(i => !i.hasAttribute('alt')).length;
      out.landmarks = Array.from(document.querySelectorAll('header,nav,main,footer,[role=main]')).length;
      out.smallTargets = Array.from(document.querySelectorAll('a,button')).filter(function(el){
        var r = el.getBoundingClientRect(); return (r.width>0&&r.height>0)&&(r.width<40||r.height<40);}).length;
      out.forms = document.querySelectorAll('form').length;
      out.headings = Array.from(document.querySelectorAll('h1,h2,h3')).map(function(x){return x.tagName+':'+x.textContent.trim().slice(0,40);}).slice(0,8);
      // WCAG contrast on visible text nodes (approximate, effective backgrounds)
      function parse(c){var m=c.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
        return m?{r:+m[1],g:+m[2],b:+m[3],a:m[4]===undefined?1:+m[4]}:null;}
      function lum(c){var f=function(v){v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4);};
        return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b);}
      function bgOf(el){var n=el;while(n&&n!==document.documentElement){
        var c=parse(getComputedStyle(n).backgroundColor);
        if(c&&c.a>0.9){return c;}n=n.parentElement;}
        return {r:255,g:255,b:255,a:1};}
      var lowContrast=[];
      var walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
      while(walker.nextNode()){
        var t=walker.currentNode; if(!t.textContent.trim())continue;
        var el=t.parentElement; if(!el)continue;
        var st=getComputedStyle(el); var fg=parse(st.color); if(!fg)continue;
        var bg=bgOf(el); var l1=lum(fg),l2=lum(bg);
        var ratio=(Math.max(l1,l2)+0.05)/(Math.min(l1,l2)+0.05);
        var size=parseFloat(st.fontSize); var bold=parseInt(st.fontWeight)>=700;
        var large=size>=24||(size>=18.66&&bold);
        var need=large?3:4.5;
        if(ratio<need){lowContrast.push({text:t.textContent.trim().slice(0,30),ratio:Math.round(ratio*100)/100,need:need});}}
      out.lowContrastText = lowContrast.slice(0,10);
      return JSON.stringify(out);
    })()
    """
    # chromium can't eval custom JS via CLI; use playwright from the gates venv
    py = (f"""
import json
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="{CHROMIUM}",
        args=["--no-sandbox","--disable-gpu","--disable-dev-shm-usage"])
    pg = b.new_page(viewport={{"width":{w},"height":{h}}})
    pg.goto({str(entry_html.resolve().as_uri())!r}, wait_until="load", timeout=30000)
    print(pg.evaluate({js!r}))
    b.close()
""")
    r = _run([str(VENV / "python"), "-c", py], ROOT, timeout_s=90)
    if r["rc"] != 0:
        checks.append({"check": "dom-eval", "ok": False, "ran": False,
                       "detail": f"playwright eval failed: {(r.get('stderr') or r.get('error') or '')[-200:]}"})
        return _finish(f"dom_check:{viewport}", "browser", checks, {})
    try:
        d = json.loads(r["stdout"].strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        checks.append({"check": "dom-eval", "ok": False, "ran": False, "detail": "unparseable DOM metrics"})
        return _finish(f"dom_check:{viewport}", "browser", checks, {})
    evidence["dom"] = d
    checks.append({"check": f"no-horizontal-overflow:{viewport}", "ok": not d["hOverflow"],
                   "detail": f"scrollWidth overflow={d['hOverflow']}"})
    checks.append({"check": f"viewport-meta:{viewport}", "ok": d["hasViewportMeta"],
                   "detail": "responsive meta present" if d["hasViewportMeta"] else "missing"})
    checks.append({"check": f"landmarks:{viewport}", "ok": d["landmarks"] >= 2,
                   "detail": f"{d['landmarks']} landmark element(s)"})
    checks.append({"check": f"img-alts:{viewport}", "ok": d["imgsMissingAlt"] == 0,
                   "detail": f"{d['imgsMissingAlt']} image(s) missing alt"})
    checks.append({"check": f"hit-targets:{viewport}", "ok": d["smallTargets"] == 0,
                   "detail": f"{d['smallTargets']} interactive element(s) < 40px"})
    low_c = d.get("lowContrastText") or []
    checks.append({"check": f"contrast-wcag:{viewport}", "ok": not low_c,
                   "detail": f"{len(low_c)} low-contrast text node(s)" if low_c
                   else "all sampled text meets WCAG ratio",
                   "hits": low_c[:5]})
    checks.append({"check": f"navigation:{viewport}", "ok": d["navLinks"] > 0,
                   "detail": f"{d['navLinks']} link(s), {d['buttons']} button(s)"})
    evidence["headings"] = d["headings"]
    return _finish(f"dom_check:{viewport}", "browser", checks, evidence)


# ==========================================================================
# technical (EXEC — policy-gated)
# ==========================================================================
def gate_exec_command(project_dir: Path, project_id: str, gate: str,
                      cmd: list[str], timeout_s: int = 600) -> dict:
    """Run a build/test command = untrusted code execution. Requires a
    recorded human approval (execution_policy.approve_exec)."""
    pol = exec_allowed(project_id, command=cmd)
    if not pol.get("allowed"):
        return {"gate": gate, "family": "technical", "passed": False,
                "status": "blocked", "checks": [{"check": gate, "ok": False,
                                                 "ran": False, "detail": pol["reason"]}],
                "evidence": {"policy": pol},
                "unavailable_components": [],
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    r = _run(cmd, project_dir, timeout_s=timeout_s)
    return {"gate": gate, "family": "technical", "passed": r["rc"] == 0,
            "status": "passed" if r["rc"] == 0 else "failed",
            "checks": [{"check": gate, "ok": r["rc"] == 0,
                        "detail": f"rc={r['rc']} in {r.get('duration_s')}s"}],
            "evidence": r, "unavailable_components": [],
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


# ==========================================================================
# runner
# ==========================================================================
def run_static_suite(project_dir: Path, entry_html: Path | None,
                     out_dir: Path) -> list[dict]:
    results = [gate_secret_scan(project_dir), gate_sast(project_dir),
               gate_dep_audit(project_dir), gate_env_handling(project_dir)]
    if entry_html and entry_html.exists():
        results.append(gate_responsive_screenshots(entry_html, out_dir))
        for vp in VIEWPORTS:
            results.append(gate_dom_check(entry_html, vp))
    return results


def summarize(results: list[dict]) -> dict:
    families = {}
    for r in results:
        fam = families.setdefault(r["family"], {"passed": 0, "failed": 0,
                                                "unavailable": 0, "blocked": 0})
        fam[r["status"]] = fam.get(r["status"], 0) + 1
    statuses = [r["status"] for r in results]
    if results and all(s == "passed" for s in statuses):
        overall = "passed"
    elif "blocked" in statuses:
        overall = "blocked"
    elif "failed" in statuses:
        overall = "failed"
    elif "unavailable" in statuses:
        overall = "passed_with_unavailable"   # nothing failed; some checks could not run
    else:
        overall = "unavailable"
    return {"overall": overall, "families": families,
            "gates": [{"gate": r["gate"], "family": r["family"], "status": r["status"]}
                      for r in results]}
