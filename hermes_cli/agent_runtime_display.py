"""Display labels for external agent runtimes."""

from __future__ import annotations

import os
from typing import Any, Optional


def cursor_headless_model_label(config: Optional[dict[str, Any]] = None) -> str:
    """Return the Cursor model label Hermes passes to `agent --model`."""
    env_model = (os.getenv("HERMES_CURSOR_MODEL", "") or "").strip()
    if env_model:
        return env_model
    config = config if isinstance(config, dict) else {}
    model_cfg = config.get("model")
    candidates = [config.get("HERMES_CURSOR_MODEL")]
    if isinstance(model_cfg, dict):
        candidates.extend(
            [
                model_cfg.get("cursor_model"),
                model_cfg.get("cursor_headless_model"),
                model_cfg.get("cursor_pty_model"),
            ]
        )
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "default"


def _runtime_from_config(config: Optional[dict[str, Any]]) -> str:
    if not isinstance(config, dict):
        return "auto"
    try:
        from hermes_cli.codex_runtime_switch import get_current_runtime

        return get_current_runtime(config)
    except Exception:
        return "auto"


def _active_runtime(
    api_mode: Optional[str],
    config: Optional[dict[str, Any]],
) -> str:
    mode = (api_mode or "").strip().lower()
    config_runtime = _runtime_from_config(config)
    if mode in {"cursor_headless", "cursor_pty"}:
        return mode
    if config_runtime in {"cursor_headless", "cursor_pty"}:
        return config_runtime
    if mode == "cursor_headless" or config_runtime == "cursor_headless":
        return "cursor_headless"
    return mode or config_runtime


def active_model_display_label(
    model: str,
    *,
    api_mode: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
) -> str:
    """Return the model label users should see for the active runtime."""
    runtime = _active_runtime(api_mode, config)
    if runtime == "cursor_headless":
        return f"cursor:{cursor_headless_model_label(config)}"
    if runtime == "cursor_pty":
        return f"cursor:pty:{cursor_headless_model_label(config)}"
    return model


def active_provider_display_label(
    default: str,
    *,
    api_mode: Optional[str] = None,
    config: Optional[dict[str, Any]] = None,
) -> str:
    """Return the provider label users should see for the active runtime."""
    runtime = _active_runtime(api_mode, config)
    if runtime in {"cursor_headless", "cursor_pty"}:
        return "Cursor"
    return default


def active_runtime_summary(config: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Return a startup-banner summary for external agent runtimes."""
    runtime = _runtime_from_config(config)
    if runtime == "cursor_headless":
        return f"Cursor headless (model: {cursor_headless_model_label(config)})"
    if runtime == "cursor_pty":
        return f"Cursor PTY (model: {cursor_headless_model_label(config)})"
    if runtime == "codex_app_server":
        return "codex app-server (terminal/file ops/MCP run inside codex)"
    return None
