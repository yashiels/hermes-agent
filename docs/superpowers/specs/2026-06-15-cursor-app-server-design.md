# Cursor App-Server Runtime Design

## Goal

Add an experimental Hermes runtime that keeps Cursor Agent CLI's hidden worker server alive across turns, so Hermes can use the user's Cursor account with lower per-turn startup overhead than the supported `cursor_headless` process-per-turn path.

The runtime is explicitly private-protocol based. It targets the pinned local Cursor CLI build `2026.06.15-03-48-54-da23e37` and must fail closed when the installed Cursor CLI version differs, unless the user opts into unpinned execution.

## Current Context

Hermes already supports:

- `codex_app_server`: one persistent Codex app-server subprocess per Hermes session, driven over documented JSON-RPC.
- `cursor_headless`: one `agent -p --output-format json` process per Hermes turn, using Cursor's supported CLI automation surface.

Cursor CLI does not expose a documented local equivalent to `codex app-server`. The installed Cursor CLI does expose a hidden `worker-server` command. The bundled code references a UNIX socket path, `worker.sock`, JSON-RPC helpers, `AGENT_CLI_SOCKET_PATH`, `AGENT_CLI_LOG_PATH`, and `AGENT_CLI_WORKER_OPTIONS`.

This design uses that hidden surface, isolated behind a new runtime so the stable `cursor_headless` path remains available.

## Runtime Selection

Add `model.agent_runtime: cursor_app_server`.

Runtime names:

- `cursor_headless`: supported Cursor CLI process-per-turn runtime.
- `cursor_app_server`: experimental persistent Cursor worker-server runtime.
- `codex_app_server`: existing Codex app-server runtime.

Banner/status should render the new runtime as `cursor:app-server`, with the runtime detail `Cursor app-server (model: <model>)`.

## Version Pinning

On startup, the runtime runs `agent about` or an equivalent lightweight probe and parses the CLI version.

Default behavior:

- If the version is `2026.06.15-03-48-54-da23e37`, continue.
- If the version differs, refuse to start and return a clear error that names the installed and expected versions.
- If `HERMES_CURSOR_APP_SERVER_ALLOW_UNPINNED=1`, continue despite the mismatch and log a warning.

This protects Hermes from silently speaking the wrong private protocol after a Cursor update.

## Components

### Low-Level Client

Create `agent/transports/cursor_app_server.py`.

Responsibilities:

- Create a private runtime directory under the Hermes session temp/log area.
- Allocate a UNIX socket path and log path.
- Spawn `agent worker-server` with:
  - `AGENT_CLI_SOCKET_PATH=<socket>`
  - `AGENT_CLI_LOG_PATH=<log>`
  - `AGENT_CLI_WORKER_OPTIONS=<json>`
- Wait for the socket to become connectable.
- Connect to the socket.
- Send and receive JSON-RPC messages.
- Capture worker logs for redacted diagnostics.
- Terminate the worker process on close.

The client should be intentionally small and private to this runtime. It should not share the Codex app-server client because Cursor's protocol is not documented and may differ in framing, lifecycle, or method names.

### Session Adapter

Create `agent/transports/cursor_app_server_session.py`.

Responsibilities:

- Own one Cursor worker process per Hermes `AIAgent` session.
- Start the worker lazily on first turn.
- Discover or validate the method sequence required to create/resume a Cursor conversation.
- Send user turns to Cursor.
- Stream assistant output and final results back into Hermes' message list.
- Track Cursor conversation/session IDs for later turns.
- Retire and respawn the worker on protocol errors, dead sockets, or turn timeouts.

The adapter mirrors `CodexAppServerSession` at the Hermes boundary, but its internals stay Cursor-specific.

### Runtime Hook

Extend `agent/codex_runtime.py` with `run_cursor_app_server_turn`.

Responsibilities:

- Lazily construct `CursorAppServerSession`.
- Resolve workspace using the same Cursor workspace config as `cursor_headless`.
- Resolve model using the same Cursor model config.
- Project final assistant text into Hermes messages.
- Reuse Cursor usage accounting and subscription-included cost handling.
- Return the same dict shape as the other runtime paths.

`agent/conversation_loop.py` adds an early branch for `agent.api_mode == "cursor_app_server"`.

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

1. User sends a message to Hermes.
2. Hermes sees `api_mode == "cursor_app_server"`.
3. `run_cursor_app_server_turn` starts or reuses `CursorAppServerSession`.
4. The session ensures the hidden Cursor worker is running and connected.
5. Hermes sends the user message through the Cursor private protocol.
6. Cursor worker emits streaming events and a final result.
7. Hermes projects assistant text into `messages`.
8. Hermes records usage, cost status, session metadata, and memory review hooks.
9. The worker stays alive for the next turn.

## Error Handling

Startup failures:

- Missing `agent`: return "Cursor Agent CLI not found".
- Not logged in: return "Run `agent login`".
- Version mismatch: return a pinned-version error.
- Socket never appears: include the redacted worker log tail.
- Worker exits before handshake: include exit code and redacted log tail.

Turn failures:

- Protocol method failure: retire the session and return a partial error.
- Dead socket: retire the session and allow the next turn to respawn.
- Turn timeout: terminate the worker, retire the session, and return a partial error.
- Unknown event shape: log the raw event after redaction and fail closed.

Fallback:

- If `HERMES_CURSOR_APP_SERVER_FALLBACK=headless` is set, startup/protocol failures may delegate the turn to `cursor_headless`.
- Default fallback is disabled so private-protocol breakage is visible during development.

## Security

The runtime inherits Cursor's own permissions and workspace behavior. Hermes does not grant extra filesystem authority beyond what Cursor CLI already receives.

Diagnostics must redact:

- API keys and bearer tokens.
- Authorization headers.
- OAuth tokens.
- Cursor request IDs when adjacent to auth data.
- Shell command lines containing secrets.

The runtime should avoid printing the full `AGENT_CLI_WORKER_OPTIONS` payload if it ever contains account or auth state.

## Testing

Unit tests:

- Runtime config accepts `cursor_app_server`.
- Banner/status formatting renders `cursor:app-server`.
- Version pin parser accepts the expected version and rejects mismatches.
- Low-level client builds the expected spawn environment.
- Socket wait handles success, timeout, and worker exit.
- Session retires on protocol errors and timeouts.
- Runtime returns the standard Hermes turn dict shape.

Fake integration tests:

- A fake UNIX-socket JSON-RPC server simulates a Cursor worker.
- Tests cover one-turn success, two-turn session reuse, usage projection, and protocol failure.

Live smoke test:

- Gated by `HERMES_CURSOR_APP_SERVER_LIVE=1`.
- Starts the real hidden worker against a temporary workspace.
- Sends a minimal prompt.
- Verifies final text and session reuse.

The live test is not part of the default suite because it depends on a logged-in Cursor account and private CLI behavior.

## Rollout

1. Add the transport and session behind tests.
2. Wire the runtime selector and display.
3. Add config documentation in the spec or CLI help text.
4. Run the fake integration suite by default.
5. Run one live smoke locally before using the runtime in normal Hermes.

## Non-Goals

- Do not remove `cursor_headless`.
- Do not replace Codex app-server.
- Do not depend on Cursor Cloud Agent or private cloud workers.
- Do not auto-update Cursor CLI.
- Do not support arbitrary Cursor CLI versions until the private protocol is understood for those versions.
