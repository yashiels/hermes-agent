from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Optional

from gateway.platforms.event import MessageEvent

logger = logging.getLogger("gateway.platforms.base")


class PlatformRunControlsMixin:
    async def _run_gateway_turn_end_hooks(
        self, event: MessageEvent, session_key: str, interrupt_event: asyncio.Event,
        metadata: Optional[dict[str, Any]], delivery_attempted: bool, delivery_succeeded: bool,
    ) -> None:
        from gateway.platforms.base import _reply_anchor_for_event
        hermes_run = getattr(event, "_hermes_run", None)
        if not isinstance(hermes_run, dict) or not self._gateway_run_controls_enabled(self):
            return
        hook_metadata = dict(metadata or {})
        hook_metadata["hermes_run"] = dict(hermes_run)
        preliminary = getattr(event, "_hermes_turn_outcome", "failed")
        outcome = (
            "interrupted"
            if interrupt_event.is_set() or preliminary == "interrupted"
            else "failed"
            if preliminary == "failed" or (delivery_attempted and not delivery_succeeded)
            else "done"
        )
        finalize = getattr(self, "finalize_native_task_card", None)
        if callable(finalize):
            try:
                result = finalize(
                    event.source.chat_id, outcome=outcome,
                    reply_to=_reply_anchor_for_event(event), metadata=hook_metadata,
                )
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("[%s] Native task-card finalization failed", self.name, exc_info=True)
        turn_end = getattr(self, "on_turn_end", None)
        if callable(turn_end):
            try:
                result = turn_end(event.source.chat_id, metadata=hook_metadata, outcome=outcome)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.warning("[%s] Turn-end hook failed", self.name, exc_info=True)


    @staticmethod
    def _gateway_run_controls_enabled(adapter: Any) -> bool:
        enabled = getattr(adapter, "gateway_run_controls_enabled", None)
        if not callable(enabled):
            return False
        try:
            return enabled() is True
        except Exception:
            logger.warning("Adapter run-control capability check failed", exc_info=True)
            return False


    async def _notify_turn_error(self, event: MessageEvent, e: BaseException) -> Optional[dict]:
        """Tell the user a turn failed rather than leaving radio silence (last resort:
        a failing notice is logged, never raised). Returns the thread metadata used."""
        from gateway.platforms.base import _thread_metadata_for_event, diagnostic_wake_muted, t
        _thread_metadata = None
        try:
            _thread_metadata = _thread_metadata_for_event(event)
            error_detail = str(e)[:300] if str(e) else t("gateway.notify.turn_error_no_details")
            with self._media_delivery_scope(event.source):
                content = None if diagnostic_wake_muted(event) else self.warning_text(
                    t("gateway.notify.turn_error_detailed", error_type=type(e).__name__, error_detail=error_detail),
                    t("gateway.notify.turn_error"),
                    logical_platform=event.source.platform, chat_id=event.source.chat_id, metadata=_thread_metadata)
            if content is None:
                return _thread_metadata
            await self.send(chat_id=event.source.chat_id, content=content, metadata=_thread_metadata)
        except Exception as notify_err:
            logger.error(
                "[%s] Failed to send error notification to user: %s", self.name, notify_err, exc_info=True)
        return _thread_metadata


    async def _release_turn_marker(self, event: MessageEvent) -> None:
        """Clear the crash marker handed to this delivery lifecycle only once the final reply is ledgered or
        nothing more is owed (no kill leaves a persisted reply with neither), then wake the drain. Idempotent."""
        if (getattr(event, "_turn_marker_handoff", False) and getattr(event, "_gateway_active_turn_token", None)
                and self.gateway_runner is not None):
            await self.gateway_runner._clear_durable_active_turn(event)
        getattr(event, "_gateway_marker_released", asyncio.Event()).set()
