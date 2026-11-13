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
            logger.error("Post-delivery cleanup registration failed: %s", _rpe, exc_info=True)


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


    async def _run_agent_mark_streamed_delivery(self, response: Any, turn_ctx: TurnContext) -> None:
        """Set ``response["already_sent"]`` when streaming already delivered the final reply.

        Never when the agent failed (unseen error), on "(empty)", or on a finite (`-z`, `chat -q`) turn:
        its viewer prints only the completion text, never the deltas (#139666). Both flags reflect call
        success, not content: a mismatch with the recorded turn-final payload never suppresses."""
        from gateway.run_turn_final_reconcile import edit_streamed_message, owns_reconcilable_preview
        from gateway.session_finite import finite_turn_required
        _sc, source, session_key = turn_ctx.stream_consumer_holder[0], turn_ctx.source, turn_ctx.session_key
        if not isinstance(response, dict) or response.get("failed") or finite_turn_required() is True:
            return
        _final = response.get("final_response") or ""
        _is_empty_sentinel = not _final or _final == "(empty)"
        # response_previewed: only suppress if that EXACT text was delivered, not unrelated commentary.
        # Unrelated commentary/progress must not be mistaken for the final response (#14238).
        _previewed = bool(response.get("response_previewed"))
        _content_delivered = bool(_sc and getattr(_sc, "final_content_delivered", False))
        # #71643: a *successful* finalize edit can still carry only the last preview snapshot — deltas
        # generated between that edit and stream completion never reach any API call, and both suppression
        # flags are set from the call's success rather than its content. Reconcile the consumer's recorded
        # turn-final payload against the completed response: on a demonstrable mismatch (False) neither
        # final_response_sent nor final_content_delivered may suppress the normal final send. False also
        # covers payload-less multi-message split delivery (#78541). None (no record on a non-split legacy
        # path) keeps legacy trust; the failed-finalize family (#51828 / #33793) is unaffected because those
        # paths leave the flags False or record the complete fallback payload.
        _stale_finalized = False
        if _content_delivered and not _is_empty_sentinel:
            _matcher = getattr(_sc, "delivered_final_matches", None)
            if callable(_matcher):
                with suppress(Exception):
                    _stale_finalized = _matcher(_final) is False
            if _stale_finalized:
                _content_delivered = False
        # Plugin hooks may append content after streaming finished — then send the final version.
        _transformed = bool(response.get("response_transformed"))
        # Suppress the normal send only when the actual final reply reached the user.
        _streamed = self._run_agent_stream_confirmed_final_delivery(_sc, _final, previewed=_previewed)
        if _is_empty_sentinel:
            return
        _sk = session_key or "?"
        if not _transformed and (_streamed or _content_delivered):
            logger.info("Suppressing normal final send for session %s: final delivery already confirmed (streamed=%s previewed=%s content_delivered=%s).", _sk, _streamed, _previewed, _content_delivered)
            # A stream send counted as shown on a timeout may be lost: the ledger resends it marked.
            response["already_sent"], response["delivery_uncertain"] = True, getattr(_sc, "delivery_uncertain", False) is True
        elif not _transformed and _stale_finalized and _sc is not None:
            # Stale finalize: edit the streamed message up to the complete response (on failure the
            # normal send delivers). Not for split delivery — message_id is only the LAST chunk.
            _sc_msg_id = _sc.message_id
            if getattr(_sc, "_turn_split_delivery", False):
                logger.info(
                    "Stale streamed finalize detected for session %s on a multi-message split; skipping the in-place reconciliation edit and delivering the complete response via normal final send (#78541).",
                    _sk,
                )
            elif _sc_msg_id and _sc_msg_id != "__no_edit__" and getattr(_sc, "adapter", None) is not None:
                await edit_streamed_message(
                    _sc, source, response, _final, sk=_sk,
                    ok=("Reconciled stale streamed finalize for session %s: edited message %s with the complete response (#71643).", _sk, _sc_msg_id),
                    fail_result="Stale-finalize reconciliation edit failed for session %s (%s); sending complete response via normal final send.",
                    fail_exc="Stale-finalize reconciliation edit failed for session %s: %s; sending complete response via normal final send.",
                )
            else:
                logger.info(
                    "Stale streamed finalize detected for session %s with no editable message; delivering complete response via normal final send (#71643).",
                    _sk,
                )
        elif _transformed and _sc is not None:
            # Transformed after streaming: edit the streamed message instead of sending a duplicate.
            if _sc.message_id:
                await edit_streamed_message(
                    _sc, source, response, response["final_response"], sk=_sk,
                    ok=("Edited streamed message %s for session %s to include plugin-transformed content.", _sc.message_id, _sk),
                    fail_result="Transformed-final edit failed for session %s (%s); sending transformed response via normal final send.",
                    fail_exc="Failed to edit streamed message for session %s: %s",
                )
        elif not _transformed and owns_reconcilable_preview(_sc, getattr(source, "chat_id", ""), _final):
            # Unconfirmed final, one real preview on screen (a flood-frozen bubble, a cancelled
            # consumer): finish it in place; a plain final send would post the answer twice.
            await edit_streamed_message(
                _sc, source, response, _sc._clean_for_display(_final), sk=_sk,
                ok=("Finished streamed preview %s in place for session %s (final not confirmed).", _sc.message_id, _sk),
                fail_result="Unconfirmed-final preview edit failed for session %s (%s); sending normally.",
                fail_exc="Unconfirmed-final preview edit failed for session %s: %s; sending normally.")
        elif _sc is not None and getattr(_sc, "stream_deltas_enabled", True):
            # DUPLICATE-RISK DIAGNOSTIC: a stream consumer existed but suppression did NOT fire; log
            # the decision inputs ("signal never set" vs "ack-pending race"). Skipped for consumers
            # never fed the final's deltas (interim-only wiring, #105341) — they cannot have raced
            # the normal final send, so the warning would be a guaranteed false positive.
            logger.warning(
                "Normal final-send NOT suppressed despite active stream consumer for session %s: "
                "streamed=%s previewed=%s content_delivered=%s transformed=%s final_len=%d — "
                "possible duplicate send (see wecom ack-timeout RCA).",
                _sk, _streamed, _previewed, _content_delivered, _transformed, len(_final),
            )
        if (
            response.get("already_sent")
            and _sc is not None
            and getattr(_sc, "message_id", None)
            and getattr(_sc, "message_id", None) != "__no_edit__"
            and getattr(_sc, "adapter", None) is not None
            and session_key
            and isinstance((turn_ctx._status_thread_metadata or {}).get("hermes_run"), dict)
        ):
            await self._record_final_message(
                _sc.adapter, source.chat_id, str(_sc.message_id), session_key=session_key,
                metadata=turn_ctx._status_thread_metadata,
            )
