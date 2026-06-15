"""Cursor PTY session state and lifecycle helpers."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from agent.transports.cursor_pty import CursorPtyResult, CursorPtyTransport


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


class CursorPtySession:
    """One Hermes session backed by one long-lived Cursor PTY transport."""

    def __init__(
        self,
        *,
        hermes_session_id: str | None,
        workspace: str,
        cursor_bin: str = "agent",
        model: Optional[str] = None,
        state_store: CursorPtyStateStore | None = None,
        transport_factory: Callable[..., Any] = CursorPtyTransport,
        timeout: float = 600,
    ) -> None:
        self._hermes_session_id = hermes_session_id
        self._workspace = os.path.abspath(
            os.path.expanduser(os.path.expandvars(workspace))
        )
        self._cursor_bin = cursor_bin
        self._model = model
        self._state_store = state_store or CursorPtyStateStore()
        self._transport_factory = transport_factory
        self._timeout = timeout
        self._transport: Any = None

    def run_turn(self, prompt: str) -> CursorPtyResult:
        transport = self._ensure_transport()
        result = transport.run_turn(prompt)
        self._save_state_from_result(result, transport)
        return result

    def close(self) -> None:
        transport = self._transport
        self._transport = None
        close = getattr(transport, "close", None)
        if callable(close):
            close()

    def _ensure_transport(self) -> Any:
        if self._transport is not None:
            return self._transport

        self._transport = self._transport_factory(
            workspace=self._workspace,
            cursor_bin=self._cursor_bin,
            model=self._model,
            cursor_chat_id=self._matching_persisted_chat_id(),
            timeout=self._timeout,
        )
        return self._transport

    def _matching_persisted_chat_id(self) -> str | None:
        state = self._state_store.load(self._hermes_session_id)
        if state is None:
            return None
        if state.workspace != self._workspace:
            return None
        if (state.model or None) != (self._model or None):
            return None
        return state.cursor_chat_id

    def _save_state_from_result(self, result: CursorPtyResult, transport: Any) -> None:
        if not self._hermes_session_id:
            return
        cursor_chat_id = (
            getattr(result, "cursor_chat_id", None)
            or getattr(transport, "cursor_chat_id", None)
        )
        if not cursor_chat_id:
            return
        self._state_store.save(
            self._hermes_session_id,
            cursor_chat_id=cursor_chat_id,
            workspace=self._workspace,
            model=self._model,
            cursor_cli_version=str(getattr(transport, "cursor_cli_version", "") or ""),
        )
