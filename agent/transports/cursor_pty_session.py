"""Cursor PTY session state and lifecycle helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class CursorPtyState:
    """Persisted mapping from one Hermes session to one Cursor chat."""

    hermes_session_id: str
    cursor_chat_id: str
    workspace: str
    model: Optional[str]
    cursor_cli_version: str
    updated_at: str


def _default_state_path() -> Path:
    hermes_home = os.getenv("HERMES_HOME") or os.path.join(
        os.path.expanduser("~"),
        ".hermes",
    )
    return Path(hermes_home) / "runtime" / "cursor_pty_sessions.json"


class CursorPtyStateStore:
    """Small JSON state store for Hermes-session-to-Cursor-chat mappings."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else _default_state_path()

    def load(self, hermes_session_id: str | None) -> CursorPtyState | None:
        if not hermes_session_id:
            return None
        data = self._read()
        item = data.get(hermes_session_id)
        if not isinstance(item, dict):
            return None
        cursor_chat_id = item.get("cursor_chat_id")
        if not isinstance(cursor_chat_id, str) or not cursor_chat_id.strip():
            return None
        model = item.get("model")
        return CursorPtyState(
            hermes_session_id=hermes_session_id,
            cursor_chat_id=cursor_chat_id.strip(),
            workspace=str(item.get("workspace") or ""),
            model=model.strip() if isinstance(model, str) and model.strip() else None,
            cursor_cli_version=str(item.get("cursor_cli_version") or ""),
            updated_at=str(item.get("updated_at") or ""),
        )

    def save(
        self,
        hermes_session_id: str,
        *,
        cursor_chat_id: str,
        workspace: str,
        model: Optional[str],
        cursor_cli_version: str,
    ) -> None:
        if not hermes_session_id:
            return
        data = self._read()
        data[hermes_session_id] = {
            "cursor_chat_id": cursor_chat_id,
            "workspace": workspace,
            "model": model,
            "cursor_cli_version": cursor_cli_version,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self._write(data)

    def _read(self) -> dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(f"{self._path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_path, self._path)
