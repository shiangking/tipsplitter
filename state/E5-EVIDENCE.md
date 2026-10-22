# E5 Acceptance Evidence (GitHub integration & staging delivery)

Date: 2026-10-22

## Tests (all PASS, no credentials exposed)
| Test | Result |
|---|---|
| E5a repo init / milestone commit / feature branch | PASS — commit `18228af…` on `feature/initial` |
| E5b push without token | PASS — `blocked_by: missing_token` (no crash, no silent push) |
| E5c merge_to_main without approval | PASS — `ApprovalRequired` raised (never autonomous) |
| E5d push without recorded approval | PASS — `ApprovalRequired` raised before any network call |
| E5e pre-push secret scan blocker | PASS — planted `AKIA…` key → `blocked_by: secret_scan` with evidence; token never enters remote URL or logs (scrubbed from output) |
| E5f release-readiness honesty | PASS — `product_quality_acceptance: BLOCKED` both when no visual gates ran AND when vision is unavailable (E2-A evidence consulted via `vision_probe.vision_available()`) |

## Approval model (enforced in code, not convention)
- Autonomous: init, branches, milestone commits, PR-text prep, readiness reports.
- Hard-gated (`execution_policy.approve_exec(project, approver, 'github:<action>')`):
  repo creation, push, PR, merge-to-main, permission changes, deployment.
- GITHUB_TOKEN only via env/secrets API; redacted from all captured output.

## Live push/PR (deferred — documented)
Executing a real push/PR requires the user to (a) approve `github:push` /
`github:open_pr` scopes and (b) name the private target repository. The
mechanism is implemented and unit-tested; it was not exercised against a real
remote in this session for that reason. Actions status via `gh` is implemented
(`actions_status`) and likewise token-gated.

## Staging delivery
`factory.stage_deliver` produces `COMPLETION-REPORT.json` (release readiness,
gate evidence, repair summary, delivery state) without any push/deploy.
