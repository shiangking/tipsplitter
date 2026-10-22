# Multi-Agent Orchestration POC — Acceptance Report

Generated: 2026-10-22T13:37:34+00:00

## Model-to-task mapping (invocation proof)

| Profile (assigned) | Expected served model |
|---|---|
| `glm5.3` | `openai/x-test-1` |
| `deepseekv4.1` | `openai/x-test-2` |
| `qwen3.8flash` | `openai/x-test-3` |

Each conversation's `stats.usage_to_metrics` records the actual serving model and token usage — the evidence that the assigned profile was used.

## Main pipeline (plan→implement→frontend→review) — `state`

### Agent runs (real OpenHands conversations)

| run | task | att | status | conversation_id | served_model | launch_profile | tokens (prompt/completion) | dur_s |
|---|---|---|---|---|---|---|---|---|
| 1 | plan | 1 | succeeded | `174352d1-a0bd-4eae-a92a-a0edc4e7539d` | `openai/x-test-1` | `glm5.3` | 41306/14327 | 440.9 |
| 2 | implement | 1 | succeeded | `2a0943f9-8020-41c8-9a77-72617a4b6203` | `openai/x-test-2` | `deepseekv4.1` | 77078/5480 | 45.5 |
| 3 | frontend | 1 | succeeded | `ea70af1a-f0a0-463d-9fb0-3411e6d37108` | `openai/x-test-3` | `qwen3.8flash` | 1379979/30909 | 277.4 |
| 4 | review | 1 | succeeded | `4850fe46-f9b9-484a-aef3-0327375674b1` | `openai/x-test-1` | `glm5.3` | 362167/31242 | 394.6 |

### Task states (persisted in SQLite)

| task | assigned profile | final status | attempts |
|---|---|---|---|
| frontend | `qwen3.8flash` | **succeeded** | 1 |
| implement | `deepseekv4.1` | **succeeded** | 1 |
| plan | `glm5.3` | **succeeded** | 1 |
| review | `glm5.3` | **succeeded** | 1 |

### Independent verification results

| task | passed | exit | verifier |
|---|---|---|---|
| frontend | True | None | `static` |
| implement | True | 0 | `python3 -m unittest discover -v` |
| plan | True | None | `marker` |
| review | True | None | `marker` |

## Failure-path demo (doomed+stuck chaos) — `state-chaos`

### Agent runs (real OpenHands conversations)

| run | task | att | status | conversation_id | served_model | launch_profile | tokens (prompt/completion) | dur_s |
|---|---|---|---|---|---|---|---|---|
| 1 | doomed | 1 | failed | `a9835707-21c5-44b8-9709-34d41ef0f772` | `openai/x-test-1` | `glm5.3` | 23127/930 | 12.2 |
| 2 | stuck | 1 | succeeded | `1d894320-857d-4a63-818a-dc1cdabce971` | `openai/x-test-2` | `deepseekv4.1` | 16151/335 | 6.2 |

### Task states (persisted in SQLite)

| task | assigned profile | final status | attempts |
|---|---|---|---|
| doomed | `glm5.3` | **failed** | 1 |
| stuck | `deepseekv4.1` | **succeeded** | 1 |

### Independent verification results

| task | passed | exit | verifier |
|---|---|---|---|
| doomed | False | None | `static` |
| stuck | True | None | `static` |

## Timeout demo (agent sleeps 30s vs 45s deadline) — `state-timeout`

### Agent runs (real OpenHands conversations)

| run | task | att | status | conversation_id | served_model | launch_profile | tokens (prompt/completion) | dur_s |
|---|---|---|---|---|---|---|---|---|
| 1 | slowpoke | 1 | timeout | `6eea4e04-c196-466c-96ce-eb17f9b0c236` | `-` | `-` | 0/0 | 47.3 |

### Task states (persisted in SQLite)

| task | assigned profile | final status | attempts |
|---|---|---|---|
| slowpoke | `deepseekv4.1` | **timeout** | 1 |

## Interrupt/recover demo — `state-recover4`

### Agent runs (real OpenHands conversations)

| run | task | att | status | conversation_id | served_model | launch_profile | tokens (prompt/completion) | dur_s |
|---|---|---|---|---|---|---|---|---|
| 1 | slowpoke | 1 | interrupted | `2632a311-b6b5-4435-ab6c-e909f8d35764` | `-` | `-` | 0/0 | 9.2 |
| 2 | slowpoke | 2 | succeeded | `8d6b10ee-7713-4fbe-9134-785c2a1560ff` | `openai/x-test-2` | `deepseekv4.1` | 24555/689 | 36.3 |

### Task states (persisted in SQLite)

| task | assigned profile | final status | attempts |
|---|---|---|---|
| slowpoke | `deepseekv4.1` | **succeeded** | 2 |

### Independent verification results

| task | passed | exit | verifier |
|---|---|---|---|
| slowpoke | True | None | `static` |

## Demonstrated failure/edge paths

| Scenario | Evidence |
|---|---|
| Failed task distinct from success | `state-chaos`: task `doomed` status=failed (chaos injection refused artifact; verifier exit=1); `stuck` succeeded |
| Execution timeout + agent stop | `state-timeout`: status=timeout after 45s; controller POSTed stop; conversation settled to `paused` (confirmed, not running) |
| Controller interrupted mid-run (SIGINT) | `state-recover4` run 1: status=interrupted, error='controller interrupted'; SQLite state survived process death |
| Recovery + re-dispatch | `recover` reset task to pending; `run --resume` confirmed orphan conversation stopped (S3) before fresh attempt; attempt 2 succeeded (36.3s) |
| Evidence recoverability | `recollect.py` rebuilt all 9 artifact files from server-side event store after local artifacts/ was deleted |

## Credential handling

- No API keys in task prompts, logs, DB, artifacts, or this report.
- Credentials never entered agent code: conversations were launched with `agent_profile_id` + `agent_launch_additions.llm_profile_ref`; the server resolves stored credentials itself.
- The only header used by the controller is the session API key, read from the environment (`OH_KEY`), never written to disk or logs.

## Known limitations

1. Sequential execution only; workspace isolation of concurrent agents not tested.
2. Verification sandbox = disposable temp dir + minimal env + no-shell subprocess; no network-layer isolation available in this environment (no Docker daemon / firejail / bwrap / permitted unshare).
3. `served_model` and token usage are read from the server's `stats.usage_to_metrics` for the conversation — trusted server-side telemetry, not cryptographic proof.
4. Artifacts are collected via the workspace file API from the controller's own host paths; agents could theoretically write outside their workspace (mitigated by per-task isolated dirs + post-hoc verification, not prevented).
5. `run --resume` requires the same `--tasks` file as the original run (state does not pin the task spec); passing a different file mixes task sets in one orchestration.
6. Frontend agent consumed 1.38M prompt tokens (heavy cache re-reads); iteration budgets are set per-task but not globally enforced.

