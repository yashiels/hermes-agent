from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Dict, Optional

try:
    import discord
except ImportError:
    discord = None

from agent.i18n import t

logger = logging.getLogger(__name__)

EMOJI_APPROVE = "✅"
EMOJI_DENY = "❌"
EMOJI_STOP = "\U0001f6d1"
EMOJI_RETRY = "\U0001f501"
_HANDLED_EMOJI = (EMOJI_APPROVE, EMOJI_DENY, EMOJI_STOP, EMOJI_RETRY)

_APPROVAL_STYLE = {
    "once": ("platform.discord.approval.resolved_once", lambda: discord.Color.green()),
    "session": ("platform.discord.approval.resolved_session", lambda: discord.Color.blue()),
    "always": ("platform.discord.approval.resolved_always", lambda: discord.Color.purple()),
    "deny": ("platform.discord.approval.resolved_deny", lambda: discord.Color.red()),
}


class ReactionControlRegistry:
    MAX_ACTIVE = 500

    def __init__(self) -> None:
        self._active: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self._locks: "OrderedDict[str, asyncio.Lock]" = OrderedDict()

    def _put(self, message_id: Any, entry: Dict[str, Any]) -> None:
        key = str(message_id)
        self._active[key] = entry
        self._active.move_to_end(key)
        while len(self._active) > self.MAX_ACTIVE:
            oldest, _ = self._active.popitem(last=False)
            self._locks.pop(oldest, None)

    def lock_for(self, message_id: Any) -> asyncio.Lock:
        key = str(message_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def register_approval(
        self, message_id: Any, *, session_key: str, require_admin: bool,
        admin_user_ids: Optional[set], expires_at: float, message: Any, view: Any,
    ) -> None:
        self._put(message_id, {
            "kind": "approval", "session_key": session_key, "require_admin": require_admin,
            "admin_user_ids": frozenset(str(a).strip() for a in (admin_user_ids or set()) if str(a).strip()),
            "expires_at": expires_at, "message": message, "view": view,
        })

    def register_run_message(
        self, message_id: Any, *, session_key: str, generation: int, nonce: str,
    ) -> None:
        self._put(message_id, {
            "kind": "run", "session_key": session_key, "generation": generation, "nonce": nonce,
        })

    def get(self, message_id: Any) -> Optional[Dict[str, Any]]:
        key = str(message_id)
        entry = self._active.get(key)
        if entry is None:
            return None
        if entry["kind"] == "approval" and time.time() > entry["expires_at"]:
            self._active.pop(key, None)
            return None
        return entry

    def discard(self, message_id: Any) -> None:
        key = str(message_id)
        self._active.pop(key, None)
        self._locks.pop(key, None)

    def clear_run(self, session_key: str, generation: int) -> None:
        stale = [
            key for key, entry in self._active.items()
            if entry.get("kind") == "run" and entry.get("session_key") == session_key
            and entry.get("generation") == generation
        ]
        for key in stale:
            self._active.pop(key, None)
            self._locks.pop(key, None)


async def resolve_approval_prompt(
    session_key: str, choice: str, actor: str, *, view: Any,
    finalize: Callable[[Any, str], Awaitable[None]],
) -> int:
    from tools.approval import resolve_gateway_approval
    try:
        count = resolve_gateway_approval(session_key, choice)
    except Exception as exc:
        logger.error("[Discord] Failed to resolve gateway approval for session %s: %s", session_key, exc)
        count = 0
    if not count:
        return 0
    logger.info(
        "Discord resolved %d approval(s) for session %s (choice=%s, actor=%s)",
        count, session_key, choice, actor,
    )
    label_key, color_fn = _APPROVAL_STYLE.get(choice, _APPROVAL_STYLE["deny"])
    footer = t("platform.discord.approval.by_user", label=t(label_key), user=actor)
    try:
        await finalize(color_fn(), footer)
    finally:
        callback = getattr(view, "approval_state_callback", None)
        if callback is not None:
            await callback()
    return count


async def _reactor_member_role_ids(adapter: Any, payload: Any) -> Optional[set]:
    member = getattr(payload, "member", None)
    if member is not None:
        try:
            return {getattr(role, "id", None) for role in member.roles}
        except TypeError:
            return None
    guild_id = getattr(payload, "guild_id", None)
    if not guild_id:
        return None
    client = getattr(adapter, "_client", None)
    try:
        guild = client.get_guild(guild_id) if client is not None else None
        if guild is None:
            return None
        fetched = await guild.fetch_member(payload.user_id)
        return {getattr(role, "id", None) for role in fetched.roles}
    except Exception:
        logger.debug("[Discord] Failed to resolve reactor roles for reaction controls", exc_info=True)
        return None


def _reactor_display_name(payload: Any) -> str:
    member = getattr(payload, "member", None)
    name = getattr(member, "display_name", None) or getattr(member, "name", None) if member is not None else None
    return str(name) if name else str(getattr(payload, "user_id", "") or "unknown")


def _session_for_last_final(adapter: Any, message_id: Any) -> Optional[str]:
    message_id = str(message_id)
    for session_key, final_message_id in (getattr(adapter, "_last_final_messages", None) or {}).items():
        if str(final_message_id) == message_id:
            return session_key
    return None


async def _principal_authorized(adapter: Any, payload: Any, *, require_admin: bool, admin_user_ids) -> bool:
    from plugins.platforms.discord.adapter import _discord_principal_authorized
    member_role_ids = await _reactor_member_role_ids(adapter, payload)
    return _discord_principal_authorized(
        getattr(payload, "user_id", None), member_role_ids,
        allowed_user_ids=getattr(adapter, "_allowed_user_ids", None),
        allowed_role_ids=getattr(adapter, "_allowed_role_ids", None),
        require_admin=require_admin, admin_user_ids=admin_user_ids,
    )


def _warn_controls_missing(adapter: Any, action: str) -> None:
    if getattr(adapter, "_gateway_controls_warning_emitted", False):
        return
    adapter._gateway_controls_warning_emitted = True
    logger.warning("[Discord] Reaction %s control is unavailable; gateway controls were not injected", action)


async def _handle_approval_reaction(adapter: Any, payload: Any, entry: Dict[str, Any], choice: str) -> None:
    if not await _principal_authorized(
        adapter, payload, require_admin=entry["require_admin"], admin_user_ids=entry["admin_user_ids"],
    ):
        logger.debug(
            "[Discord] Unauthorized reaction-approval attempt on %s by %s", payload.message_id, payload.user_id,
        )
        return
    registry: ReactionControlRegistry = adapter._reaction_registry
    async with registry.lock_for(payload.message_id):
        entry = registry.get(payload.message_id)
        if entry is None or entry.get("kind") != "approval":
            return
        view = entry.get("view")
        if getattr(view, "resolved", False):
            return
        message = entry.get("message")

        async def _finalize(color: Any, footer: str) -> None:
            embed = message.embeds[0] if getattr(message, "embeds", None) else None
            if embed is not None:
                embed.color = color
                embed.set_footer(text=footer)
            if view is not None:
                view.resolved = True
                disable_all = getattr(view, "_disable_all", None)
                if callable(disable_all):
                    disable_all()
            try:
                await message.edit(embed=embed, view=view)
            except Exception:
                logger.debug("[Discord] Failed to edit approval prompt after reaction resolve", exc_info=True)

        actor = _reactor_display_name(payload)
        count = await resolve_approval_prompt(
            entry["session_key"], choice, actor, view=view, finalize=_finalize,
        )
        if not count:
            logger.debug(
                "[Discord] Reaction approval resolve no-op for session %s (choice=%s): already resolved or expired",
                entry["session_key"], choice,
            )
            return
        registry.discard(payload.message_id)


async def _handle_stop_reaction(adapter: Any, payload: Any, entry: Dict[str, Any]) -> None:
    if not await _principal_authorized(adapter, payload, require_admin=False, admin_user_ids=None):
        logger.debug("[Discord] Unauthorized stop-reaction attempt on %s by %s", payload.message_id, payload.user_id)
        return
    controls = getattr(adapter, "_gateway_controls", None)
    if controls is None:
        _warn_controls_missing(adapter, "stop")
        return
    stopped = await controls.stop_if_current(entry["session_key"], entry["generation"])
    if not stopped:
        logger.debug(
            "[Discord] Reaction stop ignored for session %s generation %s: stale or not current",
            entry["session_key"], entry["generation"],
        )


async def _handle_retry_reaction(adapter: Any, payload: Any) -> None:
    session_key = _session_for_last_final(adapter, payload.message_id)
    if session_key is None:
        return
    if not await _principal_authorized(adapter, payload, require_admin=False, admin_user_ids=None):
        logger.debug("[Discord] Unauthorized retry-reaction attempt on %s by %s", payload.message_id, payload.user_id)
        return
    controls = getattr(adapter, "_gateway_controls", None)
    if controls is None:
        _warn_controls_missing(adapter, "retry")
        return
    retried = await controls.retry_if_last(session_key, str(payload.message_id))
    if not retried:
        logger.debug(
            "[Discord] Reaction retry ignored for session %s: busy or no longer the last answer", session_key,
        )


async def handle_raw_reaction_add(adapter: Any, payload: Any) -> None:
    bot_id = getattr(getattr(adapter, "_client", None), "user", None)
    bot_id = getattr(bot_id, "id", None)
    if bot_id is not None and getattr(payload, "user_id", None) == bot_id:
        return
    if not adapter.reaction_controls_enabled():
        return
    emoji = str(getattr(payload, "emoji", ""))
    if emoji not in _HANDLED_EMOJI:
        return
    registry: ReactionControlRegistry = adapter._reaction_registry
    if emoji in (EMOJI_APPROVE, EMOJI_DENY):
        entry = registry.get(payload.message_id)
        if entry is None or entry.get("kind") != "approval":
            return
        await _handle_approval_reaction(adapter, payload, entry, "once" if emoji == EMOJI_APPROVE else "deny")
    elif emoji == EMOJI_STOP:
        entry = registry.get(payload.message_id)
        if entry is None or entry.get("kind") != "run":
            return
        await _handle_stop_reaction(adapter, payload, entry)
    elif emoji == EMOJI_RETRY:
        await _handle_retry_reaction(adapter, payload)


async def add_approval_hint_reactions(message: Any) -> None:
    for emoji in (EMOJI_APPROVE, EMOJI_DENY):
        try:
            await message.add_reaction(emoji)
        except Exception:
            logger.debug("[Discord] Failed to add hint reaction %s", emoji, exc_info=True)
