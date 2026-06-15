"""Cursor Agent CLI headless runtime adapter.

Cursor does not expose Codex's app-server JSON-RPC protocol. The supported
automation surface is the `agent -p` headless command, so this adapter owns a
process-per-turn invocation and persists Cursor's returned session id for
subsequent `--resume` calls.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class CursorHeadlessError(RuntimeError):
    """Raised when Cursor Agent CLI cannot complete a headless turn."""


@dataclass
class CursorHeadlessResult:
    """Normalized result of one Cursor headless turn."""

    final_text: str
    session_id: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _coerce_usage_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(value), 0)
        except ValueError:
            return 0
    return 0


def _normalize_usage(raw_usage: Any) -> dict[str, int]:
    if not isinstance(raw_usage, dict):
        return {}
    input_tokens = _coerce_usage_int(
        raw_usage.get("inputTokens", raw_usage.get("input_tokens"))
    )
    output_tokens = _coerce_usage_int(
        raw_usage.get("outputTokens", raw_usage.get("output_tokens"))
    )
    cache_read_tokens = _coerce_usage_int(
        raw_usage.get("cacheReadTokens", raw_usage.get("cache_read_tokens"))
    )
    cache_write_tokens = _coerce_usage_int(
        raw_usage.get("cacheWriteTokens", raw_usage.get("cache_write_tokens"))
    )
    total_tokens = _coerce_usage_int(
        raw_usage.get("totalTokens", raw_usage.get("total_tokens"))
    )
    if not total_tokens:
        total_tokens = (
            input_tokens
            + output_tokens
            + cache_read_tokens
            + cache_write_tokens
        )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
    }


class CursorHeadlessSession:
    """Small stateful wrapper around `agent -p --output-format json`."""

    def __init__(
        self,
        *,
        workspace: str,
        cursor_bin: str = "agent",
        force: bool = False,
        trust: bool = True,
        model: Optional[str] = None,
        runner: Callable[..., Any] = subprocess.run,
        timeout: float = 600,
    ) -> None:
        self._workspace = workspace
        self._cursor_bin = cursor_bin
        self._force = force
        self._trust = trust
        self._model = model
        self._runner = runner
        self._timeout = timeout
        self.session_id: Optional[str] = None

    def run_turn(self, prompt: str) -> CursorHeadlessResult:
        cmd = [
            self._cursor_bin,
            "-p",
            "--output-format",
            "json",
            "--workspace",
            self._workspace,
        ]
        if self._trust:
            cmd.append("--trust")
        if self._force:
            cmd.append("--force")
        if self._model:
            cmd.extend(["--model", self._model])
        if self.session_id:
            cmd.extend(["--resume", self.session_id])
        cmd.append(prompt)

        try:
            proc = self._runner(
                cmd,
                text=True,
                capture_output=True,
                timeout=self._timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CursorHeadlessError(
                "Cursor Agent CLI not found at 'agent'. Install Cursor CLI and run `agent login`."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CursorHeadlessError(
                f"Cursor Agent CLI timed out after {self._timeout:g}s"
            ) from exc

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            detail = (stderr or stdout or f"exit code {proc.returncode}").strip()
            raise CursorHeadlessError(
                f"Cursor Agent CLI failed: {detail}. Run `agent login` if authentication expired."
            )

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise CursorHeadlessError(
                "Cursor Agent CLI returned unparseable JSON output"
            ) from exc
        if not isinstance(payload, dict):
            raise CursorHeadlessError(
                "Cursor Agent CLI returned unparseable JSON output"
            )

        if payload.get("is_error"):
            detail = str(payload.get("result") or payload.get("error") or "unknown error")
            raise CursorHeadlessError(f"Cursor Agent CLI failed: {detail}")

        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id
        final_text = payload.get("result")
        if final_text is None:
            final_text = ""
        result = CursorHeadlessResult(
            final_text=str(final_text),
            session_id=self.session_id,
            usage=_normalize_usage(payload.get("usage")),
            raw=payload,
        )
        return result
