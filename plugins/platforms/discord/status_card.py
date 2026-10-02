from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import discord

from gateway.platforms.base import SendResult


_DISPLAY_LIMIT = 3800
_TASK_LIMIT = 8
_HISTORY_CARD_LIMIT = 200
_HISTORY_ROW_LIMIT = 300
_HISTORY_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_HISTORY_FILE_MAX_BYTES = 8 * 1024 * 1024 - 1024
_HISTORY_TRUNCATION_NOTICE = "\n\n> Detailed history truncated.\n"
_STATE_LABELS = {
    "running": "running",
    "waiting-approval": "waiting for approval",
    "done": "done",
    "failed": "failed",
    "interrupted": "interrupted",
    "interrupted-restart": "interrupted (gateway restarted)",
}
_TERMINAL_TITLES = {
    "done": "**Done**",
    "failed": "**Failed**",
    "interrupted": "**Stopped**",
    "interrupted-restart": "**Interrupted**",
}
_STATE_COLOURS = {
    "running": 0x5865F2,
    "waiting-approval": 0xF0B232,
    "done": 0x57F287,
    "failed": 0xED4245,
    "interrupted": 0x747F8D,
    "interrupted-restart": 0x747F8D,
}
_TASK_MARKERS = {
    "pending": "○",
    "in_progress": "◐",
    "running": "◐",
    "complete": "✓",
    "completed": "✓",
    "done": "✓",
    "failed": "✗",
    "error": "✗",
}
_TERMINAL_ACTIVE_MARKERS = {
    "failed": "✗",
    "interrupted": "⏹",
    "interrupted-restart": "⏹",
}
_URL_USERINFO_RE = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
_URL_SUFFIX_RE = re.compile(r"(https?://[^\s?#]+)(?:\?[^\s#]*)?(?:#[^\s]*)?", re.IGNORECASE)
_DISCORD_WEBHOOK_RE = re.compile(r"/api/webhooks/[^/\s]+/[^\s?#]+", re.IGNORECASE)
_KEY_VALUE_SECRET_RE = re.compile(
    r"\b(token|api_key|apikey|secret|password|passwd|auth)(\s*=\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
    re.IGNORECASE,
)
_AWS_SECRET_RE = re.compile(
    r"\b(aws_secret)(\s*[:=]?\s*)([A-Za-z0-9/+=_-]{40})(?![A-Za-z0-9/+=_-])",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"\bxox[A-Za-z0-9_-]+", re.IGNORECASE),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])"),
    re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])"),
)


def _elapsed_text(elapsed_s: float) -> str:
    seconds = max(0, int(elapsed_s))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _meta_text(state: str, elapsed_s: float, iteration: int, max_iterations: int) -> str:
    parts = [_STATE_LABELS.get(state, state.replace("-", " ")), _elapsed_text(elapsed_s)]
    if max_iterations > 0:
        parts.append(f"iteration {max(0, int(iteration))}/{int(max_iterations)}")
    elif iteration > 0:
        parts.append(f"iteration {int(iteration)}")
    return "-# " + " · ".join(parts)


def _compact_task_title(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip() or "Task"
    return text if len(text) <= 120 else text[:117].rstrip() + "..."


def _task_marker(task: dict[str, str], state: str) -> str:
    status = str(task.get("status") or "pending").strip().lower()
    if status in {"in_progress", "running"} and state in _TERMINAL_ACTIVE_MARKERS:
        return _TERMINAL_ACTIVE_MARKERS[state]
    return _TASK_MARKERS.get(status, "○")


def _task_line(task: dict[str, str], state: str) -> str:
    title = _compact_task_title(task.get("title") or task.get("id") or "Task")
    return f"{_task_marker(task, state)} {title}"


def _task_texts(tasks: list[dict[str, str]], state: str) -> list[str]:
    visible = list(tasks[-_TASK_LIMIT:])
    lines = []
    hidden = len(tasks) - len(visible)
    if hidden > 0:
        lines.append(f"-# +{hidden} earlier")
    for task in visible:
        lines.append(_task_line(task, state))
    return lines


def _sanitize_history_text(value: Any) -> str:
    text = str(value or "")
    text = _DISCORD_WEBHOOK_RE.sub("/api/webhooks/[REDACTED]", text)
    text = _URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    text = _URL_SUFFIX_RE.sub(r"\1", text)
    text = _KEY_VALUE_SECRET_RE.sub(r"\1\2[REDACTED]", text)
    text = _AWS_SECRET_RE.sub(r"\1\2[REDACTED]", text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def status_card_history_rows(
    tasks: list[dict[str, str]], state: str, *, earlier_rows_omitted: int = 0,
) -> tuple[list[str], int]:
    rows = [
        f"{_task_marker(task, state)} {_compact_task_title(_sanitize_history_text(task.get('title') or task.get('id') or 'Task'))}"
        for task in tasks
    ]
    try:
        upstream_omitted = max(0, int(earlier_rows_omitted))
    except (TypeError, ValueError):
        upstream_omitted = 0
    omitted = upstream_omitted + max(0, len(rows) - _HISTORY_ROW_LIMIT)
    return rows[-_HISTORY_ROW_LIMIT:], omitted


def prune_status_card_history(entries: list[dict[str, Any]], *, now: Optional[float] = None) -> list[dict[str, Any]]:
    cutoff = (time.time() if now is None else now) - _HISTORY_MAX_AGE_SECONDS
    kept = []
    for entry in entries:
        try:
            finished_at = float(entry.get("finished_at"))
        except (TypeError, ValueError):
            continue
        if finished_at < cutoff:
            continue
        bounded = dict(entry)
        rows = bounded.get("rows")
        rows_trimmed = max(0, len(rows) - _HISTORY_ROW_LIMIT) if isinstance(rows, list) else 0
        bounded["rows"] = [str(row) for row in rows[-_HISTORY_ROW_LIMIT:]] if isinstance(rows, list) else []
        try:
            bounded["rows_omitted"] = max(0, int(bounded.get("rows_omitted") or 0)) + rows_trimmed
        except (TypeError, ValueError):
            bounded["rows_omitted"] = rows_trimmed
        kept.append(bounded)
    kept.sort(key=lambda entry: float(entry["finished_at"]))
    return kept[-_HISTORY_CARD_LIMIT:]


def status_card_history_finished_text(finished_at: Any) -> str:
    return datetime.fromtimestamp(float(finished_at), timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def status_card_history_filename(finished_at: Any) -> str:
    stamp = datetime.fromtimestamp(float(finished_at), timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"hermes-run-{stamp}.md"


def _truncate_utf8(data: bytes, limit: int) -> bytes:
    return data[:max(0, limit)].decode("utf-8", errors="ignore").encode("utf-8")


def build_status_card_history_markdown(
    entry: dict[str, Any], *, max_bytes: int = _HISTORY_FILE_MAX_BYTES,
) -> bytes:
    title = _compact_task_title(_sanitize_history_text(entry.get("title") or "Hermes run"))
    try:
        rows_omitted = max(0, int(entry.get("rows_omitted") or 0))
    except (TypeError, ValueError):
        rows_omitted = 0
    omitted_text = f"- {rows_omitted} earlier rows omitted\n" if rows_omitted else ""
    header = (
        f"# {title}\n\n"
        f"- State: {entry.get('state') or 'unknown'}\n"
        f"- Elapsed: {_elapsed_text(float(entry.get('elapsed') or 0.0))}\n"
        f"- Finished: {status_card_history_finished_text(entry.get('finished_at'))}\n"
        f"{omitted_text}\n"
        "## Steps\n"
    ).encode("utf-8")
    notice = _HISTORY_TRUNCATION_NOTICE.encode("utf-8")
    limit = max(1, int(max_bytes) - 1)
    if len(header) >= limit:
        if len(notice) >= limit:
            return _truncate_utf8(notice, limit)
        return _truncate_utf8(header, limit - len(notice)) + notice
    output = bytearray(header)
    truncated = False
    for row in entry.get("rows") or []:
        line = f"- {row}\n".encode("utf-8")
        if len(output) + len(line) + len(notice) > limit:
            truncated = True
            break
        output.extend(line)
    if truncated:
        output.extend(notice)
    return bytes(output)


def render_status_card_texts(
    tasks: list[dict[str, str]], *, title: str, state: str, elapsed_s: float,
    iteration: int, max_iterations: int,
) -> list[str]:
    texts = [_TERMINAL_TITLES.get(state) or str(title or "Hermes run"), _meta_text(state, elapsed_s, iteration, max_iterations)]
    remaining = _DISPLAY_LIMIT - sum(len(text) for text in texts)
    for line in _task_texts(tasks, state):
        if remaining <= 0:
            break
        if len(line) > remaining:
            line = line[: max(0, remaining - 1)] + ("…" if remaining else "")
        texts.append(line)
        remaining -= len(line)
    return texts


class StopCardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"hermes:card:stop:(?P<nonce>[0-9a-f]{16})",
):
    def __init__(self, nonce: str):
        self.nonce = nonce
        super().__init__(discord.ui.Button(
            label="Stop", style=discord.ButtonStyle.danger,
            custom_id=f"hermes:card:stop:{nonce}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match.group("nonce"))

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        adapter = getattr(interaction.client, "_hermes_discord_adapter", None)
        if adapter is None:
            await interaction.followup.send("nothing running", ephemeral=True)
            return
        await adapter.handle_status_card_stop(interaction, self.nonce)


class ShowAllCardButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"hermes:card:all:(?P<nonce>[0-9a-f]{32})",
):
    def __init__(self, nonce: str):
        self.nonce = nonce
        super().__init__(discord.ui.Button(
            label="Show all", style=discord.ButtonStyle.secondary,
            custom_id=f"hermes:card:all:{nonce}",
        ))

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match.group("nonce"))

    async def callback(self, interaction):
        await interaction.response.defer(ephemeral=True)
        adapter = getattr(interaction.client, "_hermes_discord_adapter", None)
        if adapter is None:
            await interaction.followup.send("Detailed history expired.", ephemeral=True)
            return
        await adapter.handle_status_card_show_all(interaction, self.nonce)


class StatusCardView(discord.ui.LayoutView):
    def __init__(
        self, tasks: list[dict[str, str]], *, nonce: Optional[str], title: str,
        state: str, elapsed_s: float, iteration: int, max_iterations: int,
        history_nonce: Optional[str] = None,
    ):
        super().__init__(timeout=None)
        texts = render_status_card_texts(
            tasks, title=title, state=state, elapsed_s=elapsed_s,
            iteration=iteration, max_iterations=max_iterations,
        )
        children: list[Any] = [discord.ui.TextDisplay(texts[0]), discord.ui.TextDisplay(texts[1])]
        if len(texts) > 2:
            children.append(discord.ui.Separator())
            children.extend(discord.ui.TextDisplay(text) for text in texts[2:])
        if state == "running" and nonce:
            children.append(discord.ui.ActionRow(StopCardButton(nonce)))
        if state in _TERMINAL_TITLES and history_nonce and len(tasks) > _TASK_LIMIT:
            children.append(discord.ui.ActionRow(ShowAllCardButton(history_nonce)))
        self.add_item(discord.ui.Container(
            *children, accent_colour=_STATE_COLOURS.get(state, _STATE_COLOURS["running"]),
        ))
        if self.content_length() > _DISPLAY_LIMIT or self.total_children_count > 40:
            raise ValueError("status card exceeds Discord component limits")


@dataclass
class StatusCardFrame:
    tasks: list[dict[str, str]]
    title: str
    state: str
    elapsed_s: float
    iteration: int
    max_iterations: int
    history_nonce: Optional[str] = None


class StatusCardCoalescer:
    def __init__(
        self, edit: Callable[[StatusCardFrame], Awaitable[SendResult]], *,
        on_failure: Optional[Callable[[SendResult], Awaitable[None]]] = None,
        interval: float = 1.5,
    ):
        self._edit = edit
        self._on_failure = on_failure
        self._interval = interval
        self._latest: Optional[StatusCardFrame] = None
        self._task: Optional[asyncio.Task] = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()
        self._stopped = False

    async def submit(self, frame: StatusCardFrame) -> SendResult:
        async with self._lock:
            if self._stopped:
                return SendResult(success=False, error="status card stopped")
            self._latest = frame
            remaining = self._interval - (time.monotonic() - self._last_edit)
            if remaining <= 0 and self._task is None:
                self._latest = None
                result = await self._edit(frame)
                if result.success:
                    self._last_edit = time.monotonic()
                return result
            if self._task is None:
                self._task = asyncio.create_task(self._drain(max(0.0, remaining)))
            return SendResult(success=True)

    async def _drain(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self._lock:
                frame = self._latest
                self._latest = None
                if self._stopped or frame is None:
                    return
                result = await self._edit(frame)
                if result.success:
                    self._last_edit = time.monotonic()
                elif self._on_failure is not None:
                    await self._on_failure(result)
        except asyncio.CancelledError:
            raise
        finally:
            if self._task is asyncio.current_task():
                self._task = None

    async def stop(self, *, flush: bool) -> SendResult:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            frame = self._latest
            self._latest = None
            self._stopped = True
            if flush and frame is not None:
                result = await self._edit(frame)
                if result.success:
                    self._last_edit = time.monotonic()
                return result
        return SendResult(success=True)


@dataclass
class CardState:
    nonce: str
    generation: int
    session_key: str
    chat_id: str
    thread_key: str
    channel_id: str
    message: Any
    tasks: list[dict[str, str]]
    state: str
    started_at: float
    title: str
    elapsed_s: float = 0.0
    iteration: int = 0
    max_iterations: int = 0
    fallback_text: str = ""
    delivery_metadata: dict[str, Any] = field(default_factory=dict)
    fallback_sent: bool = False
    terminal_completed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    coalescer: Optional[StatusCardCoalescer] = None
    refresh_task: Optional[asyncio.Task] = None
    history_nonce: Optional[str] = None

    def frame(self) -> StatusCardFrame:
        return StatusCardFrame(
            tasks=[dict(task) for task in self.tasks], title=self.title, state=self.state,
            elapsed_s=max(self.elapsed_s, time.time() - self.started_at), iteration=self.iteration,
            max_iterations=self.max_iterations,
        )
