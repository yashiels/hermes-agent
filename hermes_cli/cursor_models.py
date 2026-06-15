"""Cursor Agent CLI model catalog and selection helpers."""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


class CursorModelError(RuntimeError):
    """Raised when Cursor model discovery cannot complete."""


@dataclass(frozen=True)
class CursorModel:
    id: str
    label: str
    is_current: bool = False
    is_default: bool = False


_MODEL_LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.:-]+)\s+-\s+(.+?)\s*$")
_CACHE_TTL_SECONDS = 60.0
_CACHE: tuple[float, list[CursorModel]] | None = None


def parse_cursor_models_output(output: str) -> list[CursorModel]:
    """Parse `agent models` output into model entries."""

    models: list[CursorModel] = []
    for line in (output or "").splitlines():
        match = _MODEL_LINE_RE.match(line)
        if not match:
            continue
        model_id = match.group(1).strip()
        label = match.group(2).strip()
        if not model_id or not label:
            continue
        label_lower = label.lower()
        is_current = "(current)" in label_lower
        is_default = "(default)" in label_lower
        label = re.sub(r"\s+\((?:current|default)\)", "", label, flags=re.I).strip()
        models.append(
            CursorModel(
                id=model_id,
                label=label,
                is_current=is_current,
                is_default=is_default,
            )
        )
    return models


def clear_cursor_models_cache() -> None:
    global _CACHE
    _CACHE = None


def list_cursor_models(
    *,
    cursor_bin: str = "agent",
    runner: Callable[..., Any] = subprocess.run,
    use_cache: bool = True,
) -> list[CursorModel]:
    """Return Cursor models from `agent models`, with a short in-memory cache."""

    global _CACHE
    now = time.monotonic()
    if use_cache and _CACHE is not None:
        ts, models = _CACHE
        if now - ts <= _CACHE_TTL_SECONDS:
            return list(models)

    try:
        proc = runner(
            [cursor_bin, "models"],
            text=True,
            capture_output=True,
            timeout=20,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CursorModelError(
            "Cursor Agent CLI not found at 'agent'. Install Cursor CLI."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CursorModelError("Cursor Agent CLI model list timed out") from exc

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode != 0:
        detail = (stderr or stdout or f"agent models exited {proc.returncode}").strip()
        raise CursorModelError(detail)

    models = parse_cursor_models_output(stdout)
    if use_cache:
        _CACHE = (now, list(models))
    return models


def resolve_cursor_model(config: Optional[dict[str, Any]] = None) -> str | None:
    """Resolve the Cursor model override Hermes should pass to `agent --model`."""

    env_model = (os.getenv("HERMES_CURSOR_MODEL", "") or "").strip()
    if env_model:
        return env_model

    config = config if isinstance(config, dict) else {}
    model_cfg = config.get("model")
    candidates: list[Any] = [config.get("HERMES_CURSOR_MODEL")]
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
    return None


def cursor_model_display_label(config: Optional[dict[str, Any]] = None) -> str:
    """Return the Cursor model label to show in status/banner UI."""

    return resolve_cursor_model(config) or "auto"


def is_known_cursor_model(model: str, models: list[CursorModel]) -> bool:
    wanted = (model or "").strip().lower()
    return bool(wanted) and any(item.id.lower() == wanted for item in models)
