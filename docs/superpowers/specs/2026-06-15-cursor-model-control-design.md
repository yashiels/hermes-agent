# Cursor Model Control Design

## Goal

Make Hermes' `/model` command control Cursor's model when a Cursor runtime is active, instead of switching Hermes' normal provider/model route. The default Cursor model remains `auto`, because it is Cursor's account-level automatic choice and is the safest low-cost default for this integration.

This spec covers `cursor_headless` and `cursor_pty`. In the current implementation, `cursor_pty` keeps a Hermes-session-to-Cursor-chat mapping but uses Cursor's supported `agent -p --output-format json --resume <chat_id>` path for clean output.

## Current Behavior

Hermes has two separate model concepts:

- Hermes model: `model.default`, `model.provider`, API key, base URL, and API mode.
- Cursor model: the value Hermes passes to Cursor CLI as `agent --model <model>`.

The Cursor runtime currently resolves its model from:

1. `HERMES_CURSOR_MODEL`
2. `model.cursor_model`
3. `model.cursor_headless_model`
4. Cursor CLI default when unset

Display code also recognizes `model.cursor_pty_model`, but the runtime does not yet read it. `/model <name>` still follows the normal Hermes provider-switch path, so it does not update Cursor's `--model` override.

## Design Choice

Use `/model` as a context-sensitive command:

- When `model.agent_runtime` or the live agent `api_mode` is `cursor_headless` or `cursor_pty`, `/model` manages Cursor models.
- When Cursor runtime is not active, `/model` keeps its existing Hermes provider/model behavior.
- When the user explicitly passes `--provider`, Hermes treats that as intent to switch the normal Hermes provider/model and uses the existing flow.

This keeps the familiar command while avoiding a new slash command for a narrow runtime-specific setting.

## Alternatives Considered

### Recommended: Context-Sensitive `/model`

Pros:

- Matches user expectation: the visible model is `cursor:<model>`, so `/model` should change that model.
- Reuses an existing command and picker surface.
- Keeps Cursor-specific behavior scoped to active Cursor runtimes.

Cons:

- `/model` has two meanings depending on runtime.
- The command handler needs a clear escape path for switching away from Cursor.

### Separate `/cursor-model` Command

Pros:

- Very explicit and low risk for existing `/model` behavior.
- Easier to implement incrementally.

Cons:

- Users naturally try `/model`.
- Adds another command for a setting that already appears in the status bar as the active model.

### Only Config/Env

Pros:

- Minimal implementation.
- No command ambiguity.

Cons:

- Poor interactive UX.
- Does not answer the core user expectation that `/model` controls the currently active model.

## Command Semantics

### `/model`

When Cursor runtime is active and no args are provided:

- Print the current Cursor model override.
- Show that `auto` is the recommended low-cost default.
- Show examples:
  - `/model auto`
  - `/model gpt-5.3-codex-low`
  - `/model --refresh`
  - `/model --provider <provider>` to leave Cursor model control and switch Hermes provider/model.

If the UI picker is available, open a Cursor model picker sourced from `agent models`.

### `/model <cursor-model>`

When Cursor runtime is active:

- Validate `<cursor-model>` against `agent models`, unless the model is `auto`.
- Set the live session Cursor model override.
- Update the active agent's Cursor runtime session so the next Cursor turn uses the new model.
- Do not change `model.default` or `model.provider`.
- Do not change `model.agent_runtime`.

The command is session-only by default, matching existing `/model` behavior.

### `/model <cursor-model> --global`

When Cursor runtime is active:

- Save `model.cursor_model: <cursor-model>` to `config.yaml`.
- Keep `model.agent_runtime` unchanged.
- Keep `model.default` and `model.provider` unchanged.

`model.cursor_model` is the canonical persisted Cursor model key for both `cursor_headless` and `cursor_pty`. Existing `model.cursor_headless_model` remains a legacy read fallback. `model.cursor_pty_model` should not become a second persisted key.

### `/model --provider <provider>`

This keeps the existing Hermes model-switch behavior. It is the explicit escape hatch for switching away from Cursor runtime model control.

The command may leave `model.agent_runtime` as-is. Users can disable Cursor routing with:

```text
/codex-runtime auto
```

## Cursor Model Catalog

Add a small helper module, for example `hermes_cli/cursor_models.py`.

Responsibilities:

- Run `agent models`.
- Parse lines shaped like:

```text
auto - Auto (current)
gpt-5.3-codex-low - Codex 5.3 Low
```

- Return structured entries:

```python
CursorModel(id="auto", label="Auto", is_current=True)
```

- Cache results briefly in memory for picker responsiveness.
- Support `/model --refresh` by clearing the cache.
- Fail gracefully when Cursor CLI is missing or auth is expired.

The helper should not encode pricing by hand. Pricing and availability are owned by Cursor and can change. Hermes should prefer `auto` by default and show model names exactly as Cursor reports them.

## Runtime Model Resolution

Consolidate Cursor model resolution into one helper used by display and runtime code:

```text
HERMES_CURSOR_MODEL
model.cursor_model
model.cursor_headless_model
unset -> None, letting Cursor CLI use its default
```

Display maps unset to `auto` for clarity because Cursor CLI reports `Model Auto` when no explicit model override is supplied.

`model.cursor_pty_model` should be removed from display lookup or treated only as a temporary backward-compatible fallback if any local config already uses it.

## Runtime Update Flow

For `cursor_headless`:

- Update `agent._cursor_headless_session._model` when possible.
- Clear its Cursor session ID if the model change should start a fresh model-specific chat.

For `cursor_pty`:

- Update the active `CursorPtySession` model or retire `agent._cursor_pty_session`.
- Because stored state includes `model`, the next turn will only resume a Cursor chat whose saved model matches the new model.
- If there is no matching saved chat, a new Cursor chat is created.

The conservative default is to retire the active Cursor runtime session after a model change. That avoids cross-model conversation contamination and matches the existing state-store model check.

## UI and Display

When Cursor runtime is active:

- Status bar model label remains:
  - `cursor:<model>` for `cursor_headless`
  - `cursor:pty:<model>` for `cursor_pty`
- Provider label remains `Cursor`.
- `/model auto` should update the display to `cursor:auto` or `cursor:pty:auto`.
- Global persistence should update future startup banners.

## Error Handling

- Missing Cursor CLI: show `Cursor Agent CLI not found at 'agent'. Install Cursor CLI.`
- Auth expired: show `Run agent login`.
- Invalid Cursor model: show the model and suggest `/model` or `/model --refresh`.
- `agent models` timeout: keep current model and show a non-fatal error.
- Config persist failure: apply session-only change only if safe, then report that global save failed.

## Testing

Unit tests:

- Cursor model resolution order.
- `agent models` parser with `(current)` and `(default)` annotations.
- `/model <cursor-model>` under `cursor_pty` updates Cursor model state, not `model.default`.
- `/model <cursor-model> --global` writes `model.cursor_model`.
- `/model --provider <provider>` still uses existing Hermes provider switch.
- Invalid Cursor model is rejected with an actionable message.

Integration tests:

- `AIAgent` with `api_mode="cursor_pty"` receives the changed model on the next turn.
- Existing non-Cursor `/model` tests continue to pass.
- Runtime display shows the updated Cursor model.

Manual validation:

```text
/codex-runtime cursor_pty
/model auto --global
/reset
hello
```

Expected banner/status:

```text
cursor:pty:auto · Cursor
```

Expected Cursor CLI command on the turn:

```text
agent -p --output-format json --workspace <workspace> --trust --model auto --resume <cursor_chat_id> <prompt>
```

## Out of Scope

- Mapping Cursor model names to exact dollar costs inside Hermes.
- Replacing Cursor's `auto` model routing logic.
- Adding a new command solely for Cursor model selection.
- Changing Hermes' normal provider model picker when Cursor runtime is inactive.
