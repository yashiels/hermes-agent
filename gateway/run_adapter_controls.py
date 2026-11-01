from __future__ import annotations

import inspect
import logging
from typing import Any, Optional

logger = logging.getLogger("gateway.run")


class _GatewayControls:
    __slots__ = ("_runner",)

    def __init__(self, runner: Any):
        self._runner = runner

    async def stop_if_current(self, session_key: str, generation: int) -> bool:
        return await self._runner._gateway_stop_if_current(session_key, generation)

    def is_current(self, session_key: str, generation: int) -> bool:
        return self._runner._is_session_run_current(session_key, generation)

    async def retry_if_last(self, session_key: str, final_message_id: str) -> bool:
        return await self._runner._gateway_retry_if_last(session_key, final_message_id)


class GatewayAdapterControlsMixin:
    async def _gateway_stop_if_current(self, session_key: str, generation: int) -> bool:
        state = self._peek_session_state(session_key)
        if (
            state is None
            or state.turn.agent is None
            or int(state.persistent.run_generation or 0) != int(generation)
            or state.turn.event is None
        ):
            return False
        from gateway.run import _INTERRUPT_REASON_STOP
        await self._interrupt_and_clear_session(
            session_key, state.turn.event.source,
            interrupt_reason=_INTERRUPT_REASON_STOP,
            invalidation_reason="native_stop_control",
        )
        return True


    async def _gateway_retry_if_last(self, session_key: str, final_message_id: str) -> bool:
        last_messages = getattr(self, "_last_final_message_ids", None)
        if (
            self._is_session_running(session_key)
            or not last_messages
            or str(last_messages.get(session_key) or "") != str(final_message_id)
        ):
            return False
        source = self._get_cached_session_source(session_key)
        adapter = self._delivery_adapter_for(source) if source is not None else None
        if source is None or adapter is None:
            return False
        from gateway.platforms.event import MessageEvent, MessageType
        event = MessageEvent(
            text="/retry", message_type=MessageType.COMMAND, source=source,
            user_id=source.user_id, user_name=source.user_name,
            message_id=str(final_message_id),
        )
        await adapter.handle_message(event)
        return event._gateway_accepted


    async def _record_final_message(
        self, adapter: Any, chat_id: str, message_id: str, *, session_key: str,
        metadata: Optional[dict[str, Any]],
    ) -> None:
        capability = getattr(adapter, "gateway_run_controls_enabled", None)
        if not callable(capability):
            return
        try:
            if capability() is not True:
                return
        except Exception:
            logger.warning("Adapter run-control capability check failed", exc_info=True)
            return
        last_messages = getattr(self, "_last_final_message_ids", None)
        if last_messages is None:
            from collections import OrderedDict
            last_messages = self._last_final_message_ids = OrderedDict()
        last_messages[session_key] = str(message_id)
        last_messages.move_to_end(session_key)
        while len(last_messages) > 200:
            last_messages.popitem(last=False)
        hook = getattr(adapter, "on_final_message", None)
        if not callable(hook):
            return
        try:
            result = hook(chat_id, str(message_id), metadata=metadata)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.warning("Final-message hook failed", exc_info=True)
