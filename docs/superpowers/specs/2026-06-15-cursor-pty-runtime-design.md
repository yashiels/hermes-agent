# Cursor PTY Runtime Design

## Goal

Add an experimental Hermes runtime that keeps one interactive Cursor Agent CLI process alive per Hermes session. This reduces per-turn Cursor startup overhead while preserving thread isolation: each Hermes conversation gets its own Cursor process and Cursor chat context.

The runtime is intentionally PTY-based because the installed Cursor CLI does not expose a documented local chat app-server. The hidden `agent worker-server` command was probed and found to expose local helper HTTP routes such as `/ping`, `/openFile`, `/getDiagnostics`, `/getIndexingStatus`, `/getRepositoryInfo`, and `/kill`; it is not a prompt/turn server.

## Current Context

Hermes already supports:

- `codex_app_server`: one persistent Codex app-server subprocess per Hermes session, driven over documented JSON-RPC.
- `cursor_headless`: one `agent -p --output-format json` process per Hermes turn, using Cursor's supported CLI automation surface.

The `cursor_headless` path is stable but slow because Cursor initializes the CLI, MCPs, model manager, and workspace context on each turn. The PTY runtime keeps the interactive Cursor process warm across turns for the same Hermes session.

## Runtime Selection

Add `model.agent_runtime: cursor_pty`.

Runtime names:

- `cursor_headless`: supported Cursor CLI process-per-turn runtime.
- `cursor_pty`: experimental persistent interactive Cursor CLI runtime.
- `codex_app_server`: existing Codex app-server runtime.

Banner/status should render the new runtime as `cursor:pty`, with runtime detail `Cursor PTY (model: <model>)`.

## Session Isolation

The core invariant is:

```text
Hermes session A -> Cursor PTY A -> Cursor chat A
Hermes session B -> Cursor PTY B -> Cursor chat B
Hermes session C -> Cursor PTY C -> Cursor chat C
```

Rules:

- Never use one global Cursor process for all Hermes sessions.
- Never use `agent --continue`, because it can resume the latest Cursor chat from another Hermes session.
- Never share a Cursor chat ID between Hermes sessions.
- Reuse a Cursor process only inside the same Hermes `session_id`.
- If Hermes cannot prove a Cursor chat belongs to the current Hermes session, start a fresh Cursor chat.

Sharing the same Cursor workspace path, such as `/Users/yashielsookdeo`, is allowed. That shares filesystem context and indexing, not conversation memory.

## Resume Behavior

The runtime may resume Cursor state only when it has an explicit mapping from the current Hermes session to a Cursor chat ID.

Default behavior:

- New Hermes session with no mapping: launch a fresh interactive Cursor chat.
- Existing Hermes session with a saved Cursor chat ID: launch Cursor with `--resume <cursor_chat_id>`.
- Existing live Hermes session with a running PTY: reuse that PTY directly.
- Missing, invalid, or ambiguous mapping: start fresh and replace the mapping.

The mapping is stored in Hermes-owned runtime state, keyed by Hermes `session_id`.

## Version Pinning

On startup, the runtime runs `agent about` or an equivalent lightweight probe and parses the CLI version.

Default behavior:

- If the version is `2026.06.15-03-48-54-da23e37`, continue.
- If the version differs, refuse to start and return a clear error that names the installed and expected versions.
- If `HERMES_CURSOR_PTY_ALLOW_UNPINNED=1`, continue despite the mismatch and log a warning.

This protects Hermes from silently parsing a changed terminal UI after a Cursor update.

## Components

### PTY Transport

Create `agent/transports/cursor_pty.py`.

Responsibilities:

- Spawn an interactive Cursor Agent CLI process under a pseudo-terminal.
- Build the command:
  - `agent`
  - `--workspace <workspace>`
  - `--model <model>` when configured
  - `--resume <cursor_chat_id>` only when the current Hermes session owns that ID
- Send prompts into the PTY.
- Read terminal output incrementally.
- Strip ANSI control sequences and Cursor terminal chrome.
- Detect turn completion from the pinned Cursor terminal output.
- Capture raw PTY logs for redacted diagnostics.
- Terminate the process on close.

The transport should use an internal PTY implementation such as `pexpect` when available, or Python `pty` plus `selectors` if no dependency is already present. `tmux` is not the primary transport.

### Optional Tmux Debug Mode

Add optional debug mode:

- `HERMES_CURSOR_PTY_DEBUG_TMUX=1`
- Start one named tmux session per Hermes session, such as `hermes-cursor-<session-id-short>`.
- Use it only for human inspection when PTY parsing fails.

Even in tmux debug mode, each Hermes session gets its own tmux session. There is no shared tmux session.

### Session Adapter

Create `agent/transports/cursor_pty_session.py`.

Responsibilities:

- Own one Cursor PTY process per Hermes `AIAgent` session.
- Start lazily on first turn.
- Load and save the Hermes-session-to-Cursor-chat mapping.
- Reuse the live PTY for later turns in the same Hermes session.
- Retire and respawn the PTY on process death, parse failure, or timeout.
- Project assistant text and usage metadata into Hermes' standard turn result shape.

### Runtime State

Create a small state store for Cursor PTY mappings. The state can live under Hermes' existing state directory, for example:

```text
~/.hermes/runtime/cursor_pty_sessions.json
```

State entry shape:

```json
{
  "hermes_session_id": {
    "cursor_chat_id": "cursor-chat-id",
    "workspace": "/Users/yashielsookdeo",
    "model": "auto",
    "cursor_cli_version": "2026.06.15-03-48-54-da23e37",
    "updated_at": "2026-06-15T12:00:00Z"
  }
}
```

Writes should be atomic. Corrupt state should be ignored with a warning rather than blocking Hermes startup.

### Runtime Hook

Extend `agent/codex_runtime.py` with `run_cursor_pty_turn`.

Responsibilities:

- Lazily construct `CursorPtySession`.
- Resolve workspace using the same Cursor workspace config as `cursor_headless`.
- Resolve model using the same Cursor model config.
- Project final assistant text into Hermes messages.
- Reuse Cursor subscription-included cost handling.
- Return the same dict shape as other runtime paths.

`agent/conversation_loop.py` adds an early branch for `agent.api_mode == "cursor_pty"`.

### CLI and Display

Update runtime validation and display helpers:

- `hermes_cli/codex_runtime_switch.py`
- `hermes_cli/runtime_provider.py`
- `hermes_cli/agent_runtime_display.py`
- `hermes_cli/banner.py`
- `cli.py`
- `agent/agent_init.py`
- `run_agent.py`

The new runtime should be selectable using the same `/codex-runtime` command family until Hermes has a provider-neutral command name.

## Data Flow

1. User sends a message in Hermes.
2. Hermes sees `api_mode == "cursor_pty"`.
3. `run_cursor_pty_turn` starts or reuses the `CursorPtySession` for the current Hermes `session_id`.
4. The session loads any Cursor chat ID mapped to that exact Hermes session.
5. The session starts Cursor interactively in a PTY if needed.
6. Hermes sends the user prompt into the PTY.
7. The transport reads and cleans terminal output until turn completion.
8. Hermes appends the assistant text to `messages`.
9. Hermes records usage/cost status when available.
10. The PTY stays alive only for that Hermes session.

## Prompt Framing

PTY prompts should be framed to make parsing reliable:

- Wrap the user's message with a generated turn marker.
- Ask Cursor to end every response with the exact marker.
- Remove the marker from the final assistant text before returning it to Hermes.

Example internal prompt suffix:

```text

When you are completely finished, print exactly this line on its own:
HERMES_CURSOR_TURN_DONE:<uuid>
```

The marker is regenerated for every turn. If the marker appears in the user's original text, generate another marker.

## Error Handling

Startup failures:

- Missing `agent`: return "Cursor Agent CLI not found".
- Not logged in: return "Run `agent login`".
- Version mismatch: return a pinned-version error.
- PTY spawn failure: include redacted PTY diagnostics.
- Resume failure: retire mapping and start fresh once for the same Hermes session.

Turn failures:

- Marker timeout: retire the PTY and return a partial error.
- Process exit: retire the PTY and return a partial error.
- Parse failure: retain raw redacted logs, retire the PTY, and return a partial error.
- Interrupt: send Ctrl+C to that session's PTY only.

Fallback:

- If `HERMES_CURSOR_PTY_FALLBACK=headless` is set, startup or parse failures may delegate the turn to `cursor_headless`.
- Default fallback is disabled so PTY issues are visible during development.

## Security

The runtime inherits Cursor's own permissions and workspace behavior. Hermes does not grant extra filesystem authority beyond what Cursor CLI already receives.

Diagnostics must redact:

- API keys and bearer tokens.
- Authorization headers.
- OAuth tokens.
- Cursor request IDs when adjacent to auth data.
- Shell command lines containing secrets.

Raw terminal logs should stay local and should not be appended to Hermes conversation messages.

## Testing

Unit tests:

- Runtime config accepts `cursor_pty`.
- Banner/status formatting renders `cursor:pty`.
- Version pin parser accepts the expected version and rejects mismatches.
- State store writes atomically and ignores corrupt JSON.
- Separate Hermes session IDs get separate `CursorPtySession` objects.
- A resumed Hermes session uses only its own saved Cursor chat ID.
- The transport never uses `agent --continue`.
- Prompt marker generation avoids collisions with user text.

Fake PTY tests:

- One-turn success with marker completion.
- Two-turn reuse inside one Hermes session.
- Two Hermes sessions produce separate fake PTY processes.
- Timeout without marker retires the PTY.
- Process exit retires the PTY.

Live smoke test:

- Gated by `HERMES_CURSOR_PTY_LIVE=1`.
- Starts the real Cursor CLI against a temporary workspace.
- Sends a minimal prompt.
- Verifies final text and process reuse for a second turn.

The live test is not part of the default suite because it depends on a logged-in Cursor account and pinned terminal output.

## Rollout

1. Add state store and unit tests.
2. Add fake PTY transport and parsing tests.
3. Add real PTY transport behind version pinning.
4. Wire runtime selector and display.
5. Run the default fake test suite.
6. Run a gated live smoke locally before using the runtime as the normal Hermes runtime.

## Non-Goals

- Do not remove `cursor_headless`.
- Do not replace Codex app-server.
- Do not depend on Cursor Cloud Agent or private cloud workers.
- Do not auto-update Cursor CLI.
- Do not use a global Cursor process.
- Do not use `agent --continue`.
- Do not share Cursor chat state across Hermes sessions.
