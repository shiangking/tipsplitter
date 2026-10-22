# Phase A — Environment Inspection Findings

Date: 2025-10-22 · Inspector: engineering-lead agent (this conversation)
Method: installed package versions, live OpenAPI schema (`/openapi.json`), and one disposable
smoke-test conversation. No endpoints were guessed; no global settings were modified.

## 1. Installed components

| Component | Version | Where |
|---|---|---|
| openhands-sdk | 1.57.0 | `/usr/local/lib/python3.13/site-packages/openhands/sdk` |
| openhands-agent-server | 1.57.0 | `/usr/local/lib/python3.13/site-packages/openhands/agent_server` |
| openhands-tools | 1.57.0 | (tool executors: bash, file editor, browser, task tracker) |
| openhands-workspace | 1.57.0 | workspace primitives |
| openhands-automation | 1.23.2 | `python -m uvicorn openhands.automation.app:app` (separate process) |
| Agent Canvas UI | static server | port 8000 |

## 2. Services discovered (all loopback-only)

| Service | URL | Evidence |
|---|---|---|
| Agent Server (agent-server API) | `http://127.0.0.1:18000` | `AGENT_SERVER_URL` env; `/health`, `/openapi.json` (200) |
| Automation Service | `http://127.0.0.1:18001` | uvicorn `openhands.automation.app:app`; `/health` → `{"status":"ok"}` |
| Agent Canvas UI | `http://localhost:8000` | static server process |

The Automation API is **not** mounted on the agent server (`/api/automation/v1` on :18000 → 404);
it is a separate service on :18001. Note: the user-facing skill text defaults to `http://localhost:8001`
— on this deployment the real ports are 18000 (agent server) and 18001 (automation).

## 3. Conversation lifecycle API (from OpenAPI, 148 paths)

- `POST /api/conversations` — body `StartConversationRequest` (only `workspace` is required).
  Returns `201` with conversation id and `execution_status`.
- `GET /api/conversations/{id}` — status polling; `ConversationExecutionStatus` ∈
  `idle, running, paused, waiting_for_confirmation, finished, error, stuck, deleting`.
- `POST /api/conversations/{id}/run` / `/pause` / `/interrupt` — lifecycle control.
- `POST /api/conversations/{id}/events` (`SendMessageRequest`, `run: true`) — send follow-up work.
- `GET /api/conversations/{id}/events/search` — paged event history (kinds incl.
  `MessageEvent`, `ActionEvent`, `ObservationEvent`, `AgentErrorEvent`,
  `ConversationErrorEvent`, `TokenEvent`, `ConversationStateUpdateEvent`).
- `GET /api/conversations/{id}/agent_final_response` — final agent answer (`{"response": str}`).
- `GET /api/conversations/{id}/workspace/{file_path}` — read files produced in the run workspace.
- `GET /api/conversations/{id}/workspace` — workspace metadata.
- `DELETE /api/conversations/{id}` — dispose a conversation.
- `GET /api/conversations/search` — list/search conversations (recovery/discovery).

`StartConversationRequest` highlights (verified from schema):
`workspace` (required, `LocalWorkspace{working_dir}`), `worktree`, `agent_profile_id`,
`agent_settings`, `agent_launch_additions{llm_profile_ref}`, `initial_message{role, content, run}`,
`max_iterations` (default 500), `stuck_detection`, `confirmation_policy{kind: NeverConfirm}`,
`tags`, `secrets`/`secrets_encrypted`, `title_llm_profile`, `autotitle`.

## 4. Model configuration per run — VERIFIED by smoke test

Saved LLM profiles (from `GET /api/profiles`, keys only, no secret values printed):

| Profile name | Model id | api_key_set | provider_connection_id |
|---|---|---|---|
| `glm5.3` | `openai/x-test-1` | true | `08f3dd…` (Soclaas OpenAI-compatible) |
| `deepseekv4.1` | `openai/x-test-2` | true | same |
| `qwen3.8flash` | `openai/x-test-3` | true | same |

(`base_url` resolves through the shared provider connection: `https://soclaas-api.comp.nus.edu.sg/v1`.)

One agent profile exists: `default` (id `fa6ee148-…`), `llm_profile_ref: glm5.3`, `agent_kind: openhands`.

**Mechanism chosen (non-mutating, credential-safe):**

```
POST /api/conversations
  agent_profile_id: "<default profile uuid>"
  agent_launch_additions: { "llm_profile_ref": "<glm5.3 | deepseekv4.1 | qwen3.8flash>" }
```

`AgentLaunchAdditions.llm_profile_ref` (schema): *"LLM profile to launch an OpenHands Agent
Profile with instead of its own `llm_profile_ref`. Recorded in `launched_agent_profile`;
the stored profile is not modified."*

Smoke test (conversation `a5470b40-0cb2-44cc-a361-277e0d74c7ab`, created and deleted):
- created 201 → ran → `finished` in ~2 s;
- `launched_agent_profile.llm_profile_ref == "deepseekv4.1"` (profile override worked);
- `agent_final_response.response == "SMOKE-OK"` (result collection works);
- the API key never left the server: the launch carries only a profile *name*; the server
  resolves credentials from its own store.

## 5. Evidence primitives for "the right model actually ran"

- `ConversationInfo.launched_agent_profile.llm_profile_ref` — profile requested at launch.
- `ConversationInfo.current_model_id` / `Metrics.model_name` — model actually serving.
- `Metrics.accumulated_token_usage` (`prompt_tokens`, `completion_tokens`, …) — non-zero
  token usage proves real LLM invocation (a stub would show zero usage).
- `ConversationStats` (per-conversation LLM call metrics).

## 6. Workspace isolation & execution permissions

- Each run gets its own `LocalWorkspace(working_dir=...)` — verified in smoke test.
- `worktree: true` can add a dedicated git worktree per conversation
  (`/tmp/conversation-worktrees/<id>/<project>`) — relevant when the workspace sits inside a repo.
- `confirmation_policy: NeverConfirm` (default) lets agents execute without human gating;
  per-conversation `security_analyzer` remains available.
- Tools come from the agent profile (terminal, file_editor, task_tracker, browser, …);
  the server-side tools catalog lists `terminal`, `file_editor`, `task_tracker`,
  `browser_tool_set`, `canvas_ui`, etc. The delegated conversation can be restricted by
  launching with explicit `agent_settings.tools` instead of a profile.
- `max_iterations` + `stuck_detection` are server-side loop backstops; wall-clock timeout is
  controller-side (see design).

## 7. Automation Server capabilities

`http://127.0.0.1:18001/api/automation/v1` — automations (cron/event triggers), runs,
uploads, webhooks. Currently zero automations. It schedules **independent** runs; it has no
first-class notion of a dependency DAG, no result-to-prompt piping between runs, and no
per-task retry policy. Verdict: useful for scheduled recurring jobs; **not** the right tool
for a coordinated plan → implement → verify → review pipeline. A small custom controller
(same host, loopback API) is the correct orchestration layer.

## 8. Missing capabilities / limitations found

1. No dependency-graph orchestration in either service — must live in the controller.
2. No first-class "collect artifacts into a bundle" API — controller reads
   `workspace/{path}` per file (plus direct filesystem fallback; same host).
3. `current_model_id` was `null` in the smoke test's final state (it is populated while a
   run is active; `metrics.model_name` / `stats` carry the served model after completion).
4. Wall-clock timeouts are not a server feature — controller must enforce them (interrupt + mark).
5. Automation runs are one-shot scripts, not resumable state machines — SQLite state stays ours.
