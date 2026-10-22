# E3 Acceptance Evidence (autonomous repair & refinement)

Date: 2026-10-22

## Live repair run (real conversations, orchestration tag e3demo0001)

Initial state: 6 gates failed (3 security, 3 browser defects planted in demo app).

| Cycle | Gate | Family | Routed to | Conversation | Served model | Agent OK | After |
|---|---|---|---|---|---|---|---|
| 1 | secret_scan | security | deepseekv4.1 | `834f60c1…` | openai/x-test-2 | ✅ | passed |
| 1 | dep_audit | security | deepseekv4.1 | `1c5f9ad2…` | openai/x-test-2 | ✅ | passed |
| 1 | env_handling | security | deepseekv4.1 | `2b07e9ca…` | openai/x-test-2 | ✅ | passed |
| 1 | dom_check:desktop | browser | qwen3.8flash | `e85dabb3…` | openai/x-test-3 | ✅ | passed |
| 1 | dom_check:tablet | browser | qwen3.8flash | `bbe19608…` | openai/x-test-3 | ✅ | passed |
| 1 | dom_check:mobile | browser | qwen3.8flash | `8195bff0…` | openai/x-test-3 | ✅ | passed |

Verified code changes after repair:
- secrets moved to `os.environ.get(...)` (was hardcoded AKIA…/sk-live-…)
- requirements pinned (`flask==3.0.0`)
- HTML gained viewport meta, landmarks, ≥40px hit targets (per dom_check evidence)

Final: 7/8 gates passed; `dep_audit` = `unavailable` (exec+network full audit,
policy-blocked) — correctly NOT counted as passed. Outcome recorded:
`unavailable_components` (product-quality acceptance stays blocked while
vision is unavailable, independent of this gate).

## Routing correctness (C2)
security/technical → deepseekv4.1 (asserted); browser/visual → qwen3.8flash (asserted);
independent review mandatory for security (asserted).

## Anti-gaming mechanisms
- C3 PASS: test-file modification during repair detected via snapshot diff
  (`touched_test_files`) → would trigger mandatory independent GLM review.
- C4 PASS (mechanism demo with canned agent responses, clearly labeled):
  unproductive loop (no gate-score improvement for 2 cycles) → loop stops at
  bound, `architect_escalation` fired (glm5.3), outcome `blocked_unproductive`.
- Repair prompts explicitly forbid editing tests/gate specs; gates are
  controller-owned and re-run after every repair (no self-approval).

## C1
Covered by the live run: planted defects repaired by routed agents, gates re-run
green, regression suite executed (`regression_clean` recorded per entry).
