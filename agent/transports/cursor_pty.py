"""Cursor Agent CLI PTY transport.

Cursor's supported ``agent -p`` headless mode creates one process per turn.
This transport drives Cursor's interactive CLI over a PTY but passes each
prompt as the process' initial argv prompt. Follow-up text typed into the
interactive prompt bar is intentionally avoided because that UI is not a
stable automation protocol. A persisted Cursor chat id gives each Hermes
session isolated cross-turn context without using global ``--continue``.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import termios
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


PINNED_CURSOR_CLI_VERSION = "2026.06.15-03-48-54-da23e37"
_MARKER_PREFIX = "HERMES_CURSOR_DONE_"
_ANSI_RE = re.compile(
    r"""
    \x1b
    (?:
        \[[0-?]*[ -/]*[@-~]     # CSI
        |\][^\x07]*(?:\x07|\x1b\\) # OSC
        |[@-_]                    # two-byte sequences
    )
    """,
    re.VERBOSE,
)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class CursorPtyError(RuntimeError):
    """Raised when Cursor Agent CLI PTY runtime cannot complete a turn."""


@dataclass
class CursorPtyResult:
    """Normalized result of one interactive Cursor turn."""

    final_text: str
    cursor_chat_id: Optional[str] = None
    usage: dict[str, int] = field(default_factory=dict)
    raw_output: str = ""


def _env_truthy(name: str) -> bool:
    return (os.getenv(name, "") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "y",
    }


def parse_cursor_cli_version(output: str) -> Optional[str]:
    """Extract the Cursor CLI build id from ``agent about`` output."""

    for line in output.splitlines():
        match = re.match(r"\s*CLI\s+Version\s+(.+?)\s*$", line, flags=re.I)
        if match:
            value = match.group(1).strip()
            return value or None
    return None


def check_cursor_cli_version(
    cursor_bin: str = "agent",
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> str:
    """Validate that the Cursor CLI is the version this PTY parser targets."""

    try:
        proc = runner(
            [cursor_bin, "about"],
            text=True,
            capture_output=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        raise CursorPtyError(
            "Cursor Agent CLI not found at 'agent'. Install Cursor CLI and run `agent login`."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CursorPtyError("Cursor Agent CLI version check timed out") from exc

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode != 0:
        detail = (stderr or stdout or f"exit code {proc.returncode}").strip()
        raise CursorPtyError(f"Cursor Agent CLI version check failed: {detail}")

    version = parse_cursor_cli_version(f"{stdout}\n{stderr}")
    if not version:
        raise CursorPtyError("Cursor Agent CLI version check returned no CLI Version")
    if (
        version != PINNED_CURSOR_CLI_VERSION
        and not _env_truthy("HERMES_CURSOR_PTY_ALLOW_UNPINNED")
    ):
        raise CursorPtyError(
            "unsupported Cursor CLI version "
            f"{version}; expected {PINNED_CURSOR_CLI_VERSION}. "
            "Set HERMES_CURSOR_PTY_ALLOW_UNPINNED=1 to bypass."
        )
    return version


def _new_marker(user_text: str) -> str:
    while True:
        marker = f"{_MARKER_PREFIX}{uuid.uuid4()}"
        if marker not in user_text:
            return marker


def build_prompt_with_marker(user_text: str) -> tuple[str, str]:
    """Frame a user prompt and ask Cursor to end with an out-of-band marker.

    The exact marker string is deliberately not present in the prompt. PTYs can
    echo typed input, and an echoed exact marker would look like completion.
    """

    marker = _new_marker(user_text)
    nonce = marker.removeprefix(_MARKER_PREFIX)
    prompt = (
        f"{user_text.rstrip()}\n\n"
        "When your response is complete, print one final line that is formed "
        "by joining these two strings with no separator, and do not wrap it "
        "in markdown:\n"
        f"{_MARKER_PREFIX}\n"
        f"{nonce}"
    )
    return prompt, marker


def strip_terminal_control(text: str) -> str:
    """Remove ANSI/control sequences that Cursor's interactive UI emits."""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_RE.sub("", text)
    text = _CONTROL_CHARS_RE.sub("", text)
    while "\b" in text:
        text = re.sub(r".\x08", "", text)
    return text


def _is_noise_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped in {"Thinking...", "Initializing agent..."}:
        return True
    prefixes = (
        "Welcome to Cursor",
        "Available Tools",
        "Available Skills",
        "Runtime:",
        "Session:",
        "Ctrl+C",
        "● ",
        "❯",
        "─",
        "═",
        "╭",
        "╰",
        "│",
    )
    return stripped.startswith(prefixes)


def _trim_outer_blank_lines(lines: list[str]) -> list[str]:
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def extract_response_until_marker(output: str, marker: str) -> str:
    """Extract assistant text before the completion marker from PTY output."""

    cleaned = strip_terminal_control(output)
    marker_idx = cleaned.rfind(marker)
    if marker_idx < 0:
        raise CursorPtyError("Cursor PTY turn completed without completion marker")

    before = cleaned[:marker_idx]
    prior_marker_idx = before.rfind(marker)
    if prior_marker_idx >= 0:
        before = before[prior_marker_idx + len(marker):]

    lines = before.splitlines()
    lines = _trim_outer_blank_lines(lines)
    while lines and _is_noise_line(lines[0]):
        lines.pop(0)
        lines = _trim_outer_blank_lines(lines)
    return "\n".join(lines).strip()


class CursorPtyTransport:
    """PTY-backed Cursor Agent CLI turn runner with per-session chat context."""

    def __init__(
        self,
        *,
        workspace: str,
        cursor_bin: str = "agent",
        model: Optional[str] = None,
        cursor_chat_id: Optional[str] = None,
        version_checker: Callable[[str], str] = check_cursor_cli_version,
        spawn_factory: Optional[Callable[[list[str]], tuple[Any, int]]] = None,
        runner: Callable[..., Any] = subprocess.run,
        timeout: float = 600,
    ) -> None:
        self._workspace = workspace
        self._cursor_bin = cursor_bin
        self._model = model
        self._cursor_chat_id = cursor_chat_id
        self._version_checker = version_checker
        self._spawn_factory = spawn_factory
        self._runner = runner
        self._timeout = timeout
        self._process: Any = None
        self._master_fd: Optional[int] = None
        self.cursor_cli_version: Optional[str] = None

    @property
    def cursor_chat_id(self) -> Optional[str]:
        return self._cursor_chat_id

    def build_command(self, prompt: Optional[str] = None) -> list[str]:
        cmd = [self._cursor_bin, "--workspace", self._workspace]
        if self._model:
            cmd.extend(["--model", self._model])
        if self._cursor_chat_id:
            cmd.extend(["--resume", self._cursor_chat_id])
        if prompt is not None:
            cmd.append(prompt)
        return cmd

    def run_turn(self, prompt: str) -> CursorPtyResult:
        self._ensure_cursor_cli_version()
        self._ensure_cursor_chat_id()
        cmd = [
            self._cursor_bin,
            "-p",
            "--output-format",
            "json",
            "--workspace",
            self._workspace,
            "--trust",
        ]
        if self._model:
            cmd.extend(["--model", self._model])
        if self._cursor_chat_id:
            cmd.extend(["--resume", self._cursor_chat_id])
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
            raise CursorPtyError(
                "Cursor Agent CLI not found at 'agent'. Install Cursor CLI and run `agent login`."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CursorPtyError(
                f"Cursor Agent CLI timed out after {self._timeout:g}s"
            ) from exc

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            detail = (stderr or stdout or f"exit code {proc.returncode}").strip()
            raise CursorPtyError(
                f"Cursor Agent CLI failed: {detail}. Run `agent login` if authentication expired."
            )

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise CursorPtyError(
                "Cursor Agent CLI returned unparseable JSON output"
            ) from exc
        if not isinstance(payload, dict):
            raise CursorPtyError("Cursor Agent CLI returned unparseable JSON output")
        if payload.get("is_error"):
            detail = str(payload.get("result") or payload.get("error") or "unknown error")
            raise CursorPtyError(f"Cursor Agent CLI failed: {detail}")

        session_id = payload.get("session_id")
        if isinstance(session_id, str) and session_id.strip():
            self._cursor_chat_id = session_id.strip()
        final_text = payload.get("result")
        if final_text is None:
            final_text = ""

        from agent.transports.cursor_headless import _normalize_usage

        return CursorPtyResult(
            final_text=str(final_text),
            cursor_chat_id=self._cursor_chat_id,
            usage=_normalize_usage(payload.get("usage")),
            raw_output=stdout,
        )

    def close(self) -> None:
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        proc = self._process
        self._process = None
        if proc is None:
            return
        poll = getattr(proc, "poll", None)
        returncode = poll() if callable(poll) else None
        if returncode is not None:
            return
        terminate = getattr(proc, "terminate", None)
        wait = getattr(proc, "wait", None)
        kill = getattr(proc, "kill", None)
        if callable(terminate):
            try:
                terminate()
            except Exception:
                pass
        if callable(wait):
            try:
                wait(timeout=2)
                return
            except Exception:
                pass
        if callable(kill):
            try:
                kill()
            except Exception:
                pass

    def _ensure_cursor_cli_version(self) -> None:
        if self.cursor_cli_version:
            return
        self.cursor_cli_version = self._version_checker(self._cursor_bin)

    def _ensure_cursor_chat_id(self) -> None:
        if self._cursor_chat_id:
            return
        try:
            proc = self._runner(
                [self._cursor_bin, "create-chat"],
                text=True,
                capture_output=True,
                timeout=30,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CursorPtyError(
                "Cursor Agent CLI not found at 'agent'. Install Cursor CLI and run `agent login`."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CursorPtyError("Cursor Agent CLI create-chat timed out") from exc
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            detail = (stderr or stdout or f"exit code {proc.returncode}").strip()
            raise CursorPtyError(f"Cursor Agent CLI create-chat failed: {detail}")
        chat_id = stdout.strip().splitlines()[-1].strip() if stdout.strip() else ""
        if not chat_id:
            raise CursorPtyError("Cursor Agent CLI create-chat returned no chat id")
        self._cursor_chat_id = chat_id

    def _start_turn_process(self, framed_prompt: str) -> None:
        self.close()
        cmd = self.build_command(framed_prompt)
        try:
            if self._spawn_factory is not None:
                proc, master_fd = self._spawn_factory(cmd)
            else:
                proc, master_fd = self._spawn(cmd)
        except FileNotFoundError as exc:
            raise CursorPtyError(
                "Cursor Agent CLI not found at 'agent'. Install Cursor CLI and run `agent login`."
            ) from exc
        self._process = proc
        self._master_fd = master_fd

    @staticmethod
    def _spawn(cmd: list[str]) -> tuple[subprocess.Popen[bytes], int]:
        master_fd, slave_fd = pty.openpty()
        try:
            winsize = struct.pack("HHHH", 40, 120, 0, 0)
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        finally:
            os.close(slave_fd)
        try:
            os.set_blocking(master_fd, False)
        except AttributeError:
            pass
        return proc, master_fd

    def _read_until_marker(self, marker: str) -> str:
        assert self._master_fd is not None
        chunks: list[str] = []
        deadline = time.monotonic() + self._timeout
        marker_seen_at: Optional[float] = None

        while True:
            now = time.monotonic()
            if now >= deadline:
                raise CursorPtyError(
                    f"Cursor PTY turn timed out after {self._timeout:g}s"
                )

            proc = self._process
            poll = getattr(proc, "poll", None)
            if callable(poll) and poll() is not None:
                raw = "".join(chunks)
                if marker in strip_terminal_control(raw):
                    return raw
                raise CursorPtyError("Cursor Agent CLI exited before completing turn")

            timeout = min(0.2, max(deadline - now, 0.0))
            readable, _, _ = select.select([self._master_fd], [], [], timeout)
            if not readable:
                if marker_seen_at is not None and time.monotonic() - marker_seen_at > 0.25:
                    return "".join(chunks)
                continue

            try:
                chunk = os.read(self._master_fd, 4096)
            except BlockingIOError:
                continue
            except OSError as exc:
                raise CursorPtyError(f"Cursor PTY read failed: {exc}") from exc
            if not chunk:
                continue
            chunks.append(chunk.decode("utf-8", errors="replace"))

            cleaned = strip_terminal_control("".join(chunks))
            if marker in cleaned and marker_seen_at is None:
                marker_seen_at = time.monotonic()
