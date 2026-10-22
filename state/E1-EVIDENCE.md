# E1 Acceptance Evidence (design-first development)

Date: 2026-10-22

| Test | Result | Evidence |
|---|---|---|
| A1: compiler refuses frontend tasks without approved design_spec | PASS | `DesignGateError: design-first violation: no design/design_spec.json` |
| A1b: hook accepts after template baseline ingested | PASS | baseline type `template`, spec sha256 `9c816e5e21ed…` |
| A2: template → design_spec → representative screen at 3 viewports | PASS | screenshots 1280×800 / 768×1024 / 375×667, blank-ratio 0.58–0.65, primary-color checks pass |
| A3: defective screen (blank page) fails representative-screen gate | PASS | failed checks: not-blank:desktop/tablet, primary-color-present:desktop/tablet; repair routing verified in E3-C2 |
| A4: design_spec tamper evidence | PASS | sha256 stable when unchanged, changes on modification |
| Vision component availability-gated | PASS | `vision_review.status=unavailable`, `unavailable_components=[llm_vision_review]` (E2-A: no vision-capable profile) |
| URL reference ingestion | see run log | live capture stored with sha256 |

Notes: gate runs fully deterministic (chromium headless + Pillow). LLM vision review
is structurally present but reports `unavailable` until a vision-capable profile is
approved (see docs/VISION-CAPABILITY.md). Unattended product-quality acceptance
remains blocked per condition 2 while vision is unavailable.
