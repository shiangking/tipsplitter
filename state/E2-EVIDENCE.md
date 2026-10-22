# E2 Acceptance Evidence (verification gates)

Date: 2026-10-22

| Test | Result | Evidence |
|---|---|---|
| B1: vision probe per profile | DONE (negative) | docs/VISION-CAPABILITY.md — all 3 profiles refuse images (0 tokens); LLM visual review = `unavailable` |
| B2: planted defects detected | PASS | hardcoded AWS key + sk-live token → secret_scan FAILED; unpinned `flask` → dep_audit FAILED; hardcoded secret + no .env.example → env_handling FAILED; missing viewport meta, 1 landmark, <40px hit target, 2 low-contrast texts → dom_check FAILED at all 3 viewports |
| B2-clean: conforming project passes | PASS | clean project: secret_scan/sast/responsive_screenshots/dom_check×3 all passed |
| B3: exec gate without approval | PASS | status=`blocked` with policy reason (no unattended untrusted execution) |
| B3b: missing tools → unavailable, never passed | PASS | sast with empty venv → status=`unavailable`, unavailable=[bandit,ruff], passed=False |
| B4: reproducibility | PASS | two consecutive runs produce identical gate statuses |

Gate semantics: `passed` = every check ran & passed; `failed` = >=1 ran check failed;
`unavailable` = required component missing (never counts as passed); `blocked` =
policy-blocked execution (untrusted code without recorded approval).

Isolation inspection (condition 1): docker daemon unreachable, unshare (user/net)
denied, chroot denied, CapEff=0, no firejail/bwrap → NO genuine isolation in this
runtime. Recorded by execution_policy.probe_isolation() (runtime re-probe supported).
