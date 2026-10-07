from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from typing import Any, Optional

from gateway.session import SessionSource
from gateway.turn_context import TurnContext

logger = logging.getLogger("gateway.run")


class GatewayTurnControlsMixin:
    def _run_agent_schedule_bubble_cleanup(self, response: Any, _cleanup_adapter: Any, turn_ctx: TurnContext) -> None:
        """Schedule deletion of tracked temporary progress bubbles after the final response lands.

        Failed runs keep them as breadcrumbs. Only on adapters with ``delete_message``; failures swallowed."""
        from gateway.run import safe_schedule_threadsafe
        _cleanup_msg_ids, session_key = turn_ctx._cleanup_msg_ids, turn_ctx.session_key
        if not (
            turn_ctx._cleanup_progress
            and _cleanup_adapter is not None
            and _cleanup_msg_ids
            and session_key
            and isinstance(response, dict)
            and not response.get("failed")
            and hasattr(_cleanup_adapter, "register_post_delivery_callback")
        ):
            return
        _ids_snapshot = list(_cleanup_msg_ids)
        _chat_id_snapshot = turn_ctx.source.chat_id
        _loop_snapshot = asyncio.get_running_loop()

        def _cleanup_temp_bubbles() -> None:
            async def _delete_all() -> None:
                for _mid in _ids_snapshot:
                    with suppress(Exception):
                        await _cleanup_adapter.delete_message(_chat_id_snapshot, _mid)
            with suppress(Exception):
                safe_schedule_threadsafe(
                    _delete_all(), _loop_snapshot, logger=logger,
                    log_message="Temp bubble cleanup scheduling error",
                )

        try:
            _cleanup_adapter.register_post_delivery_callback(
                session_key, _cleanup_temp_bubbles, generation=turn_ctx.run_generation,
            )
        except Exception as _rpe:
            logger.debug("Post-delivery cleanup registration failed: %s", _rpe)


    @staticmethod
    def _native_task_cards_enabled_for_turn(
        adapter: Any, tool_progress_explicit: bool, progress_mode: str,
    ) -> bool:
        if not hasattr(adapter, "native_task_cards_enabled"):
            return False
        if tool_progress_explicit and progress_mode == "off":
            return False
        try:
            return bool(adapter.native_task_cards_enabled())
        except Exception:
            logger.debug("Native task-card config check failed", exc_info=True)
            return False


    def _new_adapter_run_token(
        self, source: SessionSource, session_key: Optional[str], run_generation: Optional[int],
    ) -> Optional[dict[str, Any]]:
        adapter = self._delivery_adapter_for(source)
        enabled = getattr(adapter, "gateway_run_controls_enabled", None)
        if not callable(enabled):
            return None
        try:
            if enabled() is not True:
                return None
        except Exception:
            logger.warning("Adapter run-control capability check failed", exc_info=True)
            return None
        if not session_key or run_generation is None:
            return None
        return {
            "session_key": session_key,
            "generation": int(run_generation),
            "nonce": secrets.token_hex(8),
        }


    async def _run_agent_edit_streamed_message(
        self, _sc, source, response, content, *, _sk, ok, fail_result: str, fail_exc: str,
    ) -> None:
        """Edit the stream consumer's message in place with ``content``; on success mark
        ``response["already_sent"]`` and log ``ok``. A returned failure logs ``fail_result`` as
        ``(session, error)`` and an exception logs ``fail_exc`` as ``(session, exc)``; either way
        ``already_sent`` stays unset so the normal final send delivers the content."""
        try:
            _res = await _sc.adapter.edit_message(
                chat_id=source.chat_id, message_id=_sc.message_id, content=content, finalize=True,
            )
        except Exception as _edit_err:
            logger.warning(fail_exc, _sk, _edit_err, exc_info=True)
            return
        if not getattr(_res, "success", True):
            logger.warning(fail_result, _sk, getattr(_res, "error", None))
            return
        response["already_sent"] = True
        logger.info(*ok)
