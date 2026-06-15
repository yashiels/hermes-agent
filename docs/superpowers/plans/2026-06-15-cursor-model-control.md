# Cursor Model Control Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/model` control Cursor CLI's `--model` override when `cursor_headless` or `cursor_pty` is active.

**Architecture:** Add a small Cursor model helper for catalog parsing and config resolution. Update `cli.py` to divert `/model` to Cursor-specific handling only when a Cursor runtime is active and no `--provider` escape hatch is present. Retire active Cursor runtime sessions on model changes so the next turn uses the new model and does not resume a stale model-scoped chat.

**Tech Stack:** Python, existing Hermes CLI config helpers, Cursor CLI `agent models`, pytest via `scripts/run_tests.sh`.

---

### Task 1: Cursor Model Helper

**Files:**
- Create: `hermes_cli/cursor_models.py`
- Test: `tests/hermes_cli/test_cursor_models.py`

- [ ] **Step 1: Write failing tests**

```python
def test_parse_cursor_models_marks_current():
    from hermes_cli.cursor_models import parse_cursor_models_output
    models = parse_cursor_models_output("auto - Auto (current)\ngpt-5.3-codex-low - Codex 5.3 Low\n")
    assert models[0].id == "auto"
    assert models[0].label == "Auto"
    assert models[0].is_current is True
    assert models[1].id == "gpt-5.3-codex-low"


def test_resolve_cursor_model_prefers_env(monkeypatch):
    from hermes_cli.cursor_models import resolve_cursor_model
    monkeypatch.setenv("HERMES_CURSOR_MODEL", "auto")
    assert resolve_cursor_model({"model": {"cursor_model": "gpt-5.3-codex-low"}}) == "auto"


def test_resolve_cursor_model_uses_canonical_config():
    from hermes_cli.cursor_models import resolve_cursor_model
    assert resolve_cursor_model({"model": {"cursor_model": "gpt-5.3-codex-low"}}) == "gpt-5.3-codex-low"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/hermes_cli/test_cursor_models.py`
Expected: FAIL because `hermes_cli.cursor_models` does not exist.

- [ ] **Step 3: Implement helper**

Create `CursorModel`, `parse_cursor_models_output`, `list_cursor_models`, `clear_cursor_models_cache`, `resolve_cursor_model`, and `cursor_model_display_label`.

- [ ] **Step 4: Run tests to verify pass**

Run: `scripts/run_tests.sh tests/hermes_cli/test_cursor_models.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hermes_cli/cursor_models.py tests/hermes_cli/test_cursor_models.py
git commit -m "feat(cursor): add Cursor model helpers"
```

### Task 2: Use Shared Cursor Model Resolution

**Files:**
- Modify: `agent/codex_runtime.py`
- Modify: `hermes_cli/agent_runtime_display.py`
- Test: `tests/agent/test_cursor_headless_runtime.py`
- Test: `tests/hermes_cli/test_cursor_runtime_display.py`

- [ ] **Step 1: Write failing tests**

Add tests proving `model.cursor_model` and `model.cursor_pty_model` resolution is consistent, with `model.cursor_model` as canonical and `cursor_pty_model` only a fallback.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/agent/test_cursor_headless_runtime.py tests/hermes_cli/test_cursor_runtime_display.py`
Expected: FAIL until runtime/display use the shared helper.

- [ ] **Step 3: Implement shared resolution**

Replace local Cursor model resolution logic with `hermes_cli.cursor_models.resolve_cursor_model` and display with `cursor_model_display_label`.

- [ ] **Step 4: Run tests to verify pass**

Run: `scripts/run_tests.sh tests/agent/test_cursor_headless_runtime.py tests/hermes_cli/test_cursor_runtime_display.py tests/hermes_cli/test_cursor_models.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/codex_runtime.py hermes_cli/agent_runtime_display.py tests/agent/test_cursor_headless_runtime.py tests/hermes_cli/test_cursor_runtime_display.py
git commit -m "refactor(cursor): share Cursor model resolution"
```

### Task 3: Cursor-Aware `/model`

**Files:**
- Modify: `cli.py`
- Test: `tests/cli/test_cursor_model_command.py`

- [ ] **Step 1: Write failing tests**

Test that `_handle_model_switch("/model auto --global")` under `cursor_pty` saves `model.cursor_model`, does not save `model.default`, updates the live display model, and retires `_cursor_pty_session`.

- [ ] **Step 2: Run tests to verify failure**

Run: `scripts/run_tests.sh tests/cli/test_cursor_model_command.py`
Expected: FAIL because `/model` still calls the normal provider switch path.

- [ ] **Step 3: Implement Cursor `/model` path**

In `_handle_model_switch`, when a Cursor runtime is active and `--provider` is absent:

- `/model` prints current Cursor model and examples.
- `/model --refresh` clears the Cursor model cache.
- `/model <name>` validates against `agent models` when available.
- `/model <name> --global` saves `model.cursor_model`.
- Active Cursor runtime sessions are retired so the next turn uses the new model.

- [ ] **Step 4: Run tests to verify pass**

Run: `scripts/run_tests.sh tests/cli/test_cursor_model_command.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli.py tests/cli/test_cursor_model_command.py
git commit -m "feat(cursor): route model command to Cursor runtime"
```

### Task 4: Verification

**Files:**
- No source changes expected.

- [ ] **Step 1: Run focused suite**

Run:

```bash
scripts/run_tests.sh tests/hermes_cli/test_cursor_models.py tests/cli/test_cursor_model_command.py tests/agent/test_cursor_pty_runtime.py tests/agent/test_cursor_headless_runtime.py tests/run_agent/test_cursor_headless_integration.py tests/run_agent/test_cursor_pty_integration.py tests/hermes_cli/test_agent_runtime_switch.py tests/hermes_cli/test_codex_runtime_switch.py tests/hermes_cli/test_cursor_runtime_provider.py tests/hermes_cli/test_cursor_runtime_display.py tests/agent/test_usage_pricing.py
```

Expected: PASS.

- [ ] **Step 2: Optional live smoke**

Run a live `CursorPtyTransport` turn with `model="auto"` only if user accepts spending Cursor credits.

- [ ] **Step 3: Commit any verification-only doc/test updates**

Commit only if files changed.
