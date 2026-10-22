# E4 Acceptance Evidence (parallel multi-agent execution)

Date: 2026-10-22 · orchestration tag e4demo0001

## D1 — two REAL agents in parallel worktrees, integrated and verified PASS
- Workers launched 21 ms apart (truly concurrent): `8f4ecec5…` (format-worker)
  and `9fe813cb…` (stats-worker), both deepseekv4.1 (served openai/x-test-2).
- Wall time for both conversations + commits: 34 s (vs ~60 s+ sequential).
- Each worker created exactly its owned module (src/stats.py, src/format.py);
  zero ownership violations (`git status` diff vs manifests).
- Integration: clean merges of worker/stats-worker + worker/format-worker,
  0 conflicts; both modules present on integrated tree and FUNCTIONAL
  (mean=2.5, median=2.0, format_currency(1234.561)='$1,234.56').
- Integrated tree re-verified: repair loop fixed the 4 baseline defects
  (env_handling + 3× dom_check) via conversations `2b7fd9e1`, `6cd15223`,
  `782a7922`, `bbf090a6`; final gates: 0 failed, only `dep_audit`
  full-audit unavailable (exec-gated). Aggregate verdict: `passed_with_unavailable`.

## D2 — disjoint ownership manifests enforced PASS
- Overlapping manifests rejected at compile time (src/util.py → conflict).
- runtime `manifest_violation` cross-checks `git status` per worker.

## D3 — merge conflicts detected, never silent PASS
- Injected two branches both editing index.html → integrate() reported
  conflicts per branch with file lists; merge aborted; repo left clean
  (`git status --porcelain` has no UU entries).
- Robustness fix made during testing: porcelain conflict codes (UU/AA/…)
  now parsed correctly (last token = filename).

## D4 — interrupted parallel phase never corrupts PASS (mechanism test)
- stop_flag during running wave → workers return `interrupted`, dependents
  recorded `blocked`, never executed.
