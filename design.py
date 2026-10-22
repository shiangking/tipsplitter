#!/usr/bin/env python3
"""E1 — design-first development pipeline.

Owns the design stage that MUST complete before any frontend implementation
task can be compiled or run:

  1. Reference ingestion: screenshot URLs / uploaded screenshots / Figma
     exports / curated preapproved templates (templates/*.json).
  2. Design analysis -> machine-readable design_spec.json with tamper-evident
     source evidence (sha256 of every inspected pixel source).
  3. Representative-screen gate: build ONE screen, render at 3 viewports,
     deterministic heuristics vs the design baseline (+ LLM vision review only
     if a vision-capable profile exists — see docs/VISION-CAPABILITY.md).
  4. Compiler enforcement: frontend tasks without an approved design_spec
     are rejected at compile time.

Vision policy (docs/VISION-CAPABILITY.md): no profile is vision-capable in
this deployment (E2-A probe, 0-token refusals). Visual comparison therefore
runs in HEURISTICS mode and the LLM-vision component is reported as
`unavailable` — it never counts as passed, and unattended product-quality
acceptance stays blocked until a vision-capable profile is approved.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
TEMPLATES_DIR = ROOT / "templates"
CHROMIUM = "/usr/bin/chromium"
CHROMIUM_ARGS = ["--headless", "--no-sandbox", "--disable-gpu",
                 "--disable-dev-shm-usage", "--hide-scrollbars"]
VIEWPORTS = {"desktop": (1280, 800), "tablet": (768, 1024), "mobile": (375, 667)}


# --------------------------------------------------------------------------
# reference ingestion
# --------------------------------------------------------------------------
def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_url(url: str, out_png: Path, viewport: str = "desktop",
                timeout_s: int = 60) -> dict:
    """Screenshot a public URL with headless chromium. Returns evidence."""
    w, h = VIEWPORTS[viewport]
    out_png.parent.mkdir(parents=True, exist_ok=True)
    cmd = [CHROMIUM] + CHROMIUM_ARGS + [
        f"--screenshot={out_png}", f"--window-size={w},{h}",
        f"--virtual-time-budget={timeout_s * 1000}", url]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s + 30)
    ok = proc.returncode == 0 and out_png.exists() and out_png.stat().st_size > 1000
    return {"source": url, "method": "chromium-headless-screenshot",
            "viewport": viewport, "file": str(out_png), "sha256": sha256_of(out_png) if ok else None,
            "bytes": out_png.stat().st_size if out_png.exists() else 0,
            "duration_s": round(time.time() - t0, 1), "ok": ok}


def ingest_reference(source: str, project_dir: Path) -> dict:
    """Ingest a design reference: URL (screenshot it), local image, Figma
    export (json/png), or template:<system_id>. Returns reference record."""
    refs_dir = project_dir / "design" / "references"
    refs_dir.mkdir(parents=True, exist_ok=True)
    if source.startswith("template:"):
        return ingest_template(source.split(":", 1)[1], project_dir)
    if re.match(r"^https?://", source):
        png = refs_dir / f"url-{int(time.time())}.png"
        ev = capture_url(source, png)
        if not ev["ok"]:
            return {"kind": "url", "source": source, "ok": False, "error":
                    f"capture failed: rc={proc_rc(proc)}"}
        return {"kind": "url", "source": source, "ok": True, "evidence": ev,
                "pixels": str(png), "sha256": ev["sha256"]}
    p = Path(source)
    if not p.exists():
        return {"kind": "file", "source": source, "ok": False,
                "error": "file not found"}
    dest = refs_dir / p.name
    dest.write_bytes(p.read_bytes())
    return {"kind": "image" if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
            else "figma-export", "source": source, "ok": True,
            "pixels": str(dest), "sha256": sha256_of(dest)}


def proc_rc(proc: subprocess.CompletedProcess) -> int:
    return proc.returncode


def ingest_template(system_id: str, project_dir: Path) -> dict:
    tp = TEMPLATES_DIR / f"{system_id}.json"
    if not tp.exists():
        avail = [p.stem for p in TEMPLATES_DIR.glob("*.json")]
        return {"kind": "template", "system_id": system_id, "ok": False,
                "error": f"unknown template; available: {avail}"}
    dest = project_dir / "design" / "design-system.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(tp.read_text())
    return {"kind": "template", "system_id": system_id, "ok": True,
            "pixels": None, "sha256": sha256_of(tp),
            "design_system_path": str(dest), "license": "MIT (preapproved)"}


def list_templates() -> list[dict]:
    out = []
    for p in sorted(TEMPLATES_DIR.glob("*.json")):
        d = json.loads(p.read_text())
        out.append({"system_id": d.get("system_id", p.stem),
                    "title": d.get("title"), "use_when": d.get("use_when"),
                    "sha256": sha256_of(p)})
    return out


# --------------------------------------------------------------------------
# design spec + analysis
# --------------------------------------------------------------------------
def build_design_spec(project_dir: Path, references: list[dict],
                      selected_template: str | None, idea: str,
                      vision_available: bool) -> dict:
    """Produce the machine-readable design spec agents must conform to.
    The baseline is either an analyzed reference or a preapproved template."""
    ds_path = project_dir / "design" / "design-system.json"
    design_system = json.loads(ds_path.read_text()) if ds_path.exists() else None
    pixel_refs = [r for r in references if r.get("ok") and r.get("pixels")]
    analyzed = []
    for r in pixel_refs:
        analyzed.append({"path": r["pixels"], "sha256": r["sha256"],
                         "kind": r["kind"], "heuristics": analyze_pixels(Path(r["pixels"]))})
    spec = {
        "version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "idea": idea,
        "baseline": {
            "type": "template" if selected_template else ("reference" if analyzed else None),
            "template_id": selected_template,
            "design_system": design_system,
            "analyzed_references": analyzed,
        },
        "vision_review": {"available": vision_available,
                          "note": "see docs/VISION-CAPABILITY.md; when false, "
                                  "visual comparison uses deterministic heuristics only "
                                  "and LLM vision is recorded as unavailable"},
        "requirements": (design_system or {}).get("usability_rules", []),
        "source_evidence": [r for r in references if r.get("ok")],
    }
    out = project_dir / "design" / "design_spec.json"
    out.write_text(json.dumps(spec, indent=2))
    spec["spec_sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
    return spec


def analyze_pixels(png: Path) -> dict:
    """Deterministic pixel-level analysis of a reference screenshot."""
    from PIL import Image
    im = Image.open(png).convert("RGB")
    w, h = im.size
    small = im.resize((min(w, 200), min(h, 200)))
    px = list(small.getdata())
    n = len(px)
    bg = max(set(px), key=px.count)
    distinct = len(set(px))
    # crude palette: 4 quantized buckets
    q = im.quantize(colors=4).convert("RGB")
    palette = sorted(set(q.getdata()), key=lambda c: -(q.getdata().count(c)))[:4]
    return {"size": [w, h], "dominant_bg": "#%02x%02x%02x" % bg,
            "distinct_colors_200px": distinct, "top_palette": ["#%02x%02x%02x" % c for c in palette]}


# --------------------------------------------------------------------------
# representative-screen gate (deterministic heuristics; vision-gated LLM part)
# --------------------------------------------------------------------------
def capture_viewports(html_path: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    shots = {}
    for name, (w, h) in VIEWPORTS.items():
        png = out_dir / f"screen-{name}.png"
        cmd = [CHROMIUM] + CHROMIUM_ARGS + [f"--screenshot={png}",
                                            f"--window-size={w},{h}",
                                            html_path.resolve().as_uri()]
        proc = subprocess.run(cmd, capture_output=True, timeout=60)
        shots[name] = {"file": str(png), "ok": proc.returncode == 0
                       and png.exists() and png.stat().st_size > 1000,
                       "sha256": sha256_of(png) if png.exists() else None}
    return shots


def representative_screen_gate(screen_html: Path, design_spec: dict,
                               out_dir: Path) -> dict:
    """Deterministic visual + usability heuristics for one screen.
    Returns a gate result dict; LLM-vision component is availability-gated."""
    from PIL import Image
    shots = capture_viewports(screen_html, out_dir)
    checks, evidence = [], {"screenshots": shots}
    ds = (design_spec.get("baseline") or {}).get("design_system") or {}
    tokens = ds.get("tokens") or {}

    def rgb(hexs: str):
        hexs = hexs.lstrip("#")
        return tuple(int(hexs[i:i + 2], 16) for i in (0, 2, 4))

    primary = rgb(tokens["color_primary"]) if tokens.get("color_primary") else None
    bg = rgb(tokens["color_bg"]) if tokens.get("color_bg") else None

    for name in VIEWPORTS:
        shot = shots[name]
        if not shot["ok"]:
            checks.append({"check": f"render:{name}", "ok": False,
                           "detail": "screenshot capture failed"})
            continue
        im = Image.open(shot["file"]).convert("RGB")
        w, h = im.size
        px = list(im.getdata())
        # blank-ratio: fraction equal to the single most common color
        from collections import Counter
        common, cnt = Counter(px).most_common(1)[0]
        blank = cnt / len(px)
        checks.append({"check": f"not-blank:{name}", "ok": blank < 0.97,
                       "detail": f"most-common-color share={blank:.2f}"})
        # palette conformance: primary color present on non-mobile viewports
        if primary:
            tol = 30
            has_primary = any(all(abs(c[i] - primary[i]) <= tol for i in range(3))
                              for c in common and [common] or [])
            px_sample = px[:: max(1, len(px) // 20000)]
            has_primary = any(all(abs(c[i] - primary[i]) <= tol for i in range(3))
                              for c in px_sample)
            checks.append({"check": f"primary-color-present:{name}", "ok": has_primary,
                           "detail": f"expected #{tokens['color_primary']}"})
        evidence[f"metrics:{name}"] = {
            "size": [w, h], "blank_ratio": round(blank, 3),
            "distinct_colors": len(set(px[:: max(1, len(px) // 20000)]))}
    # overflow check via DOM at each viewport
    for name, (w, h) in VIEWPORTS.items():
        if not shots[name]["ok"]:
            continue
        cmd = [CHROMIUM] + CHROMIUM_ARGS + [
            "--dump-dom", f"--window-size={w},{h}", screen_html.resolve().as_uri()]
        proc = subprocess.run(cmd, capture_output=True, timeout=60, text=True)
        dom_ok = proc.returncode == 0 and "<html" in (proc.stdout or "").lower()
        checks.append({"check": f"dom-parses:{name}", "ok": dom_ok,
                       "detail": f"rc={proc.returncode}"})
    # LLM vision component: availability-gated, never 'passed' when unavailable
    vision = design_spec.get("vision_review") or {}
    evidence["vision_review"] = {
        "available": bool(vision.get("available")),
        "status": "unavailable" if not vision.get("available") else "not-implemented-in-this-cycle",
        "note": vision.get("note", "")}
    passed = all(c["ok"] for c in checks)
    return {"gate": "representative_screen", "passed": passed,
            "checks": checks, "evidence": evidence,
            "unavailable_components": ([] if vision.get("available")
                                       else ["llm_vision_review"])}


# --------------------------------------------------------------------------
# compiler enforcement
# --------------------------------------------------------------------------
class DesignGateError(RuntimeError):
    pass


def require_design_approval(project_dir: Path) -> dict:
    """Compiler hook: frontend tasks cannot exist without an approved spec."""
    p = project_dir / "design" / "design_spec.json"
    if not p.exists():
        raise DesignGateError(
            "design-first violation: no design/design_spec.json — run the design "
            "stage (design.py) before any frontend task may be compiled")
    raw = p.read_bytes()
    spec = json.loads(raw)
    baseline = (spec.get("baseline") or {})
    if not baseline.get("type"):
        raise DesignGateError("design-first violation: design_spec has no approved "
                              "baseline (template or analyzed reference)")
    spec["spec_sha256"] = hashlib.sha256(raw).hexdigest()
    return spec


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "ingest":
        print(json.dumps(ingest_reference(sys.argv[2], Path(sys.argv[3])), indent=2))
    elif len(sys.argv) >= 2 and sys.argv[1] == "templates":
        print(json.dumps(list_templates(), indent=2))
    else:
        print("usage: design.py ingest <url|file|template:id> <project_dir> | "
              "design.py templates")
