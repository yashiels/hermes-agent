"""Archive Discord threads when Hermes sessions are pruned.

When ``hermes sessions prune`` (or auto-prune) deletes ended sessions from
state.db, matching Discord thread sessions should be archived and their
``sessions.json`` index entries removed so stale thread mappings do not
accumulate.

Uses the Discord REST API with ``DISCORD_BOT_TOKEN``. Requires the bot role
to have **Manage Threads** permission in the guild.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Set

from utils import atomic_replace

logger = logging.getLogger(__name__)

DISCORD_API_BASE = "https://discord.com/api/v10"


def _get_bot_token() -> Optional[str]:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    try:
        from hermes_cli.config import get_env_value

        token = (get_env_value("DISCORD_BOT_TOKEN") or "").strip()
    except Exception:
        token = ""
    return token or None


def _archive_enabled(explicit: Optional[bool]) -> bool:
    if explicit is not None:
        return bool(explicit)
    try:
        from hermes_cli.config import load_config

        cfg = (load_config().get("sessions") or {})
        return bool(cfg.get("archive_discord_threads_on_prune", True))
    except Exception:
        return True


def discord_thread_id_from_origin(origin: Dict[str, Any]) -> Optional[str]:
    """Return a Discord thread channel ID from a SessionSource dict, if any."""
    if not origin or origin.get("platform") != "discord":
        return None
    if origin.get("chat_type") != "thread":
        return None
    return str(origin.get("thread_id") or origin.get("chat_id") or "") or None


def collect_discord_threads_for_sessions(
    sessions_dir: Path,
    session_ids: Iterable[str],
    *,
    exclude_thread_ids: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Map Discord thread IDs to display names for pruned session IDs.

    Returns ``{thread_id: display_name}``. Duplicate thread IDs are deduped.
    """
    wanted = {str(sid) for sid in session_ids if sid}
    if not wanted:
        return {}

    exclude: Set[str] = {str(tid) for tid in (exclude_thread_ids or []) if tid}
    sessions_file = sessions_dir / "sessions.json"
    if not sessions_file.exists():
        return {}

    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s for Discord thread archive: %s", sessions_file, exc)
        return {}

    threads: Dict[str, str] = {}
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("session_id") or "") not in wanted:
            continue
        origin = entry.get("origin") or {}
        thread_id = discord_thread_id_from_origin(origin)
        if not thread_id or thread_id in exclude:
            continue
        name = entry.get("display_name") or origin.get("chat_name") or thread_id
        threads[thread_id] = str(name)
    return threads


def remove_sessions_json_entries(
    sessions_dir: Path,
    session_ids: Iterable[str],
) -> int:
    """Drop ``sessions.json`` entries whose ``session_id`` was pruned."""
    wanted = {str(sid) for sid in session_ids if sid}
    if not wanted:
        return 0

    sessions_file = sessions_dir / "sessions.json"
    if not sessions_file.exists():
        return 0

    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s for session index cleanup: %s", sessions_file, exc)
        return 0

    if not isinstance(data, dict):
        return 0

    removed_keys = [
        key for key, entry in data.items()
        if isinstance(entry, dict) and str(entry.get("session_id") or "") in wanted
    ]
    if not removed_keys:
        return 0

    for key in removed_keys:
        data.pop(key, None)

    sessions_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(sessions_dir), suffix=".tmp", prefix=".sessions_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        atomic_replace(tmp_path, sessions_file)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return len(removed_keys)


def _discord_request(
    method: str,
    path: str,
    token: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = 15,
) -> Any:
    url = f"{DISCORD_API_BASE}{path}"
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "Hermes-Agent (https://github.com/NousResearch/hermes-agent)",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status == 204:
            return None
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else None


def archive_discord_thread(token: str, thread_id: str) -> str:
    """Archive one Discord thread. Returns ``archived``, ``skipped``, or ``error``."""
    try:
        info = _discord_request("GET", f"/channels/{thread_id}", token)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        logger.debug("Discord GET channel %s failed (%s): %s", thread_id, exc.code, body)
        return "error"

    if not isinstance(info, dict):
        return "error"

    if info.get("thread_metadata", {}).get("archived"):
        return "skipped"

    try:
        result = _discord_request(
            "PATCH",
            f"/channels/{thread_id}",
            token,
            body={"archived": True},
        )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        logger.warning(
            "Discord archive thread %s failed (%s): %s",
            thread_id,
            exc.code,
            body,
        )
        return "error"

    if isinstance(result, dict) and result.get("thread_metadata", {}).get("archived"):
        return "archived"
    return "error"


def archive_threads_for_removed_sessions(
    removed_session_ids: Iterable[str],
    sessions_dir: Optional[Path],
    *,
    enabled: Optional[bool] = None,
    exclude_thread_ids: Optional[Iterable[str]] = None,
) -> Dict[str, int]:
    """Archive Discord threads and clean ``sessions.json`` for pruned sessions."""
    result = {
        "archived": 0,
        "skipped": 0,
        "errors": 0,
        "sessions_json_removed": 0,
        "threads_found": 0,
    }

    session_ids = [str(sid) for sid in removed_session_ids if sid]
    if not session_ids or sessions_dir is None:
        return result

    if not _archive_enabled(enabled):
        logger.debug("Discord thread archive on prune is disabled")
        return result

    token = _get_bot_token()
    if not token:
        logger.debug("DISCORD_BOT_TOKEN not configured; skipping thread archive")
        return result

    sessions_path = Path(sessions_dir)
    threads = collect_discord_threads_for_sessions(
        sessions_path,
        session_ids,
        exclude_thread_ids=exclude_thread_ids,
    )
    result["threads_found"] = len(threads)

    for thread_id in threads:
        status = archive_discord_thread(token, thread_id)
        result[status] = result.get(status, 0) + 1

    try:
        result["sessions_json_removed"] = remove_sessions_json_entries(
            sessions_path,
            session_ids,
        )
    except Exception as exc:
        logger.warning("Failed to clean sessions.json after prune: %s", exc)

    if result["archived"] or result["errors"]:
        logger.info(
            "Discord thread archive after session prune: archived=%d skipped=%d errors=%d "
            "sessions_json_removed=%d",
            result["archived"],
            result["skipped"],
            result["errors"],
            result["sessions_json_removed"],
        )

    return result
