from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from gateway.platforms.base import SendResult

if TYPE_CHECKING:
    from plugins.platforms.discord.status_card import CardState, StatusCardFrame


logger = logging.getLogger("plugins.platforms.discord.adapter")


class DiscordStatusCardsMixin:
    def _extra_flag(self, key: str) -> bool:
        value = (self.config.extra or {}).get(key, False)
        return value if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "on"}

    def native_task_cards_enabled(self) -> bool:
        return self._extra_flag("native_task_cards")

    def gateway_run_controls_enabled(self) -> bool:
        return self.native_task_cards_enabled() or self._extra_flag("reaction_controls")

    def reaction_controls_enabled(self) -> bool:
        return self._extra_flag("reaction_controls")

    def _context_menus_enabled(self) -> bool:
        return self._extra_flag("context_menus")

    def set_gateway_controls(self, controls: Any) -> None:
        self._gateway_controls = controls

    @staticmethod
    def _hermes_run(metadata: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        run = (metadata or {}).get("hermes_run")
        if not isinstance(run, dict):
            return None
        if not run.get("session_key") or not run.get("nonce"):
            return None
        try:
            generation = int(run.get("generation"))
        except (TypeError, ValueError):
            return None
        return {
            "session_key": str(run["session_key"]),
            "generation": generation,
            "nonce": str(run["nonce"]),
        }

    @staticmethod
    def _status_card_key(chat_id: str, metadata: Optional[dict[str, Any]]) -> tuple[str, str]:
        thread_key = str((metadata or {}).get("thread_id") or "")
        return str(chat_id), thread_key

    @staticmethod
    def _status_card_channel_id(chat_id: str, metadata: Optional[dict[str, Any]]) -> str:
        return str((metadata or {}).get("thread_id") or chat_id)

    @staticmethod
    def _status_card_error_result(exc: BaseException) -> SendResult:
        from plugins.platforms.discord.status_card import status_card_error_result
        return status_card_error_result(exc)

    @staticmethod
    def _status_card_permanent_failure(result: SendResult) -> bool:
        error = str(result.error or "").strip().lower()
        return not result.success and not result.retryable and error not in {
            "status card stopped", "status card closed",
        }

    async def _edit_status_card_frame(self, card: CardState, frame: StatusCardFrame) -> SendResult:
        from plugins.platforms.discord.adapter import SendResult, StatusCardView
        from plugins.platforms.discord.status_card import retry_message_edit
        try:
            view = StatusCardView(
                frame.tasks, nonce=card.nonce, title=frame.title, state=frame.state,
                elapsed_s=frame.elapsed_s, iteration=frame.iteration,
                max_iterations=frame.max_iterations, history_nonce=frame.history_nonce,
            )
            await retry_message_edit(self._resolve_channel, card, view=view)
            return SendResult(success=True, message_id=str(card.message.id))
        except Exception as exc:
            logger.error("[Discord] Status-card transport failed", exc_info=True)
            return self._status_card_error_result(exc)

    async def _compare_pop_status_card(self, card: CardState) -> bool:
        key = (card.chat_id, card.thread_key)
        if self._status_cards.get(key) is not card:
            return False
        self._status_cards.pop(key, None)
        if self._status_card_keys_by_nonce.get(card.nonce) == key:
            self._status_card_keys_by_nonce.pop(card.nonce, None)
        return True

    async def _stop_status_card_refresh(self, card: CardState) -> None:
        from plugins.platforms.discord.adapter import asyncio
        task = card.refresh_task
        card.refresh_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _refresh_status_card(self, card: CardState) -> None:
        from plugins.platforms.discord.adapter import asyncio, time
        try:
            while True:
                await asyncio.sleep(self._status_card_refresh_interval)
                outcome = self._ended_status_nonces.get(card.nonce)
                if outcome is not None:
                    await self._terminalize_card_once(
                        card, outcome=outcome, metadata=card.delivery_metadata,
                    )
                    return
                async with card.lock:
                    key = (card.chat_id, card.thread_key)
                    if (
                        card.terminal_completed
                        or card.state not in {"running", "waiting-approval"}
                        or self._status_cards.get(key) is not card
                        or card.coalescer is None
                    ):
                        return
                    card.elapsed_s = max(card.elapsed_s, time.time() - card.started_at)
                    result = await card.coalescer.submit(card.frame())
                if self._status_card_permanent_failure(result):
                    await self._disable_status_card_after_permanent_failure(
                        card, send_fallback=True, result=result,
                    )
                    return
        except asyncio.CancelledError:
            raise

    def _start_status_card_refresh(self, card: CardState) -> None:
        from plugins.platforms.discord.adapter import asyncio
        if card.refresh_task is None or card.refresh_task.done():
            card.refresh_task = asyncio.create_task(self._refresh_status_card(card))

    async def _persist_status_cards(self) -> None:
        from plugins.platforms.discord.adapter import asyncio, atomic_json_write
        snapshot = list(self._status_card_persisted.values())
        async with self._status_card_persistence_lock:
            path = self._status_card_persistence_path
            await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(atomic_json_write, path, snapshot, indent=None)

    def _read_status_card_history_entries(self) -> list[dict[str, Any]]:
        from plugins.platforms.discord.adapter import json
        try:
            data = json.loads(self._status_card_history_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []

    async def _load_status_card_history(self) -> None:
        from plugins.platforms.discord.adapter import asyncio, prune_status_card_history
        if self._status_card_history_loaded:
            return
        async with self._status_card_history_lock:
            if self._status_card_history_loaded:
                return
            entries = await asyncio.to_thread(self._read_status_card_history_entries)
            entries = prune_status_card_history(entries)
            self._status_card_history = {
                str(entry["nonce"]): entry for entry in entries if entry.get("nonce")
            }
            self._status_card_history_loaded = True

    async def _record_status_card_history(self, card: CardState, finished_at: float) -> Optional[str]:
        from plugins.platforms.discord.adapter import asyncio, atomic_json_write, prune_status_card_history, secrets, status_card_history_rows
        if len(card.tasks) <= 8:
            return None
        await self._load_status_card_history()
        nonce = card.history_nonce or secrets.token_hex(16)
        card.history_nonce = nonce
        rows, rows_omitted = status_card_history_rows(
            card.tasks, card.state,
            earlier_rows_omitted=card.delivery_metadata.get("status_card_rows_omitted", 0),
        )
        entry = {
            "nonce": nonce,
            "owner_user_id": str(card.delivery_metadata.get("owner_user_id") or ""),
            "allowed_user_ids": sorted(
                str(value) for value in (self._allowed_user_ids or set()) if str(value).strip()
            ),
            "allowed_role_ids": sorted(
                (value for value in (self._allowed_role_ids or set()) if str(value).strip()),
                key=str,
            ),
            "channel_id": card.channel_id,
            "message_id": str(card.message.id),
            "title": card.title,
            "state": card.state,
            "elapsed": card.elapsed_s,
            "finished_at": finished_at,
            "rows": rows,
            "rows_omitted": rows_omitted,
        }
        async with self._status_card_history_lock:
            previous = self._status_card_history.get(nonce)
            self._status_card_history[nonce] = entry
            snapshot = prune_status_card_history(list(self._status_card_history.values()), now=finished_at)
            self._status_card_history = {item["nonce"]: item for item in snapshot}
            try:
                path = self._status_card_history_path
                await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(atomic_json_write, path, snapshot, indent=None)
            except Exception:
                if previous is None:
                    self._status_card_history.pop(nonce, None)
                else:
                    self._status_card_history[nonce] = previous
                logger.error("[Discord] Failed to persist status-card history", exc_info=True)
                return None
        return nonce

    async def _remove_status_card_persistence(self, card: CardState) -> None:
        persisted = self._status_card_persisted.pop(card.nonce, None)
        try:
            await self._persist_status_cards()
        except Exception:
            if persisted is not None:
                self._status_card_persisted[card.nonce] = persisted
            raise

    async def _disable_status_card_after_permanent_failure(
        self, card: CardState, *, send_fallback: bool, result: Optional[SendResult] = None,
    ) -> None:

        async with card.lock:
            if card.terminal_completed or card.terminal_attempted or card.terminalizing:
                return
            card.terminalizing = True
            try:
                logger.warning(
                    "[Discord] Disabling status card %s after permanent lifecycle failure: %s",
                    card.nonce, getattr(result, "error", None) or "unknown failure",
                )
                await self._stop_status_card_refresh(card)
                if card.coalescer is not None:
                    await card.coalescer.close()
                if not await self._compare_pop_status_card(card):
                    card.terminal_completed = True
                    return
                await self._remove_status_card_persistence(card)
                if not send_fallback or card.fallback_sent:
                    card.fallback_sent = True
                    card.terminal_completed = True
                    return
                card.fallback_sent = True
                text = card.fallback_text.strip()
                if not text:
                    last_task = card.tasks[-1] if card.tasks else {}
                    title = str(last_task.get("title") or last_task.get("id") or "run")
                    text = f"-# {title} · progress card unavailable"
                try:
                    await self.send(card.chat_id, text, metadata=dict(card.delivery_metadata))
                except Exception:
                    logger.error("[Discord] Status-card text fallback failed", exc_info=True)
                card.terminal_completed = True
            finally:
                card.terminalizing = False

    async def _handle_status_card_coalescer_failure(
        self, card: CardState, result: SendResult,
    ) -> None:
        if not self._status_card_permanent_failure(result):
            return
        await self._disable_status_card_after_permanent_failure(
            card, send_fallback=True, result=result,
        )

    async def _terminalize_card_once(
        self, card: CardState, *, outcome: str, metadata: Optional[dict[str, Any]],
    ) -> bool:
        from plugins.platforms.discord.adapter import asyncio, time
        async with card.lock:
            if card.terminal_attempted or card.terminal_completed:
                return card.terminal_completed
            card.terminal_attempted = True
            card.terminalizing = True
            try:
                await self._stop_status_card_refresh(card)
                if card.coalescer is not None:
                    await card.coalescer.close()
                from plugins.platforms.discord.status_card import (
                    archived_thread_error, preserved_terminal_outcome, retry_message_edit,
                )
                card.state = preserved_terminal_outcome(card.state, outcome)
                self._record_ended_status_nonce(card.nonce, card.state)
                card.elapsed_s = max(card.elapsed_s, time.time() - card.started_at)
                finished_at = time.time()
                history_nonce = await self._record_status_card_history(card, finished_at)
                frame = card.frame()
                frame.history_nonce = history_nonce
                terminal_succeeded = False
                for attempt in range(3):
                    result = await self._edit_status_card_frame(card, frame)
                    if result.success:
                        terminal_succeeded = True
                        break
                    if archived_thread_error(getattr(result, "raw_response", None)):
                        break
                    logger.warning(
                        "[Discord] Status-card terminal edit attempt %d/3 failed for %s: %s",
                        attempt + 1, card.nonce, result.error or "unknown failure",
                    )
                    if attempt < 2:
                        await asyncio.sleep(0.25 * (2 ** attempt))
                notice_succeeded = False
                controls_removed = False
                if not terminal_succeeded:
                    try:
                        await retry_message_edit(self._resolve_channel, card, view=None)
                        controls_removed = True
                    except Exception as exc:
                        logger.error(
                            "[Discord] Status-card control removal failed for %s: %s",
                            card.nonce, exc, exc_info=True,
                        )
                    notice_metadata = dict(card.delivery_metadata)
                    notice_metadata.update(metadata or {})
                    if card.thread_key:
                        notice_metadata["thread_id"] = card.thread_key
                    try:
                        notice = await self.send(
                            card.chat_id, f"-# run {card.state} · status card could not be updated",
                            metadata=notice_metadata,
                        )
                        notice_succeeded = bool(notice.success)
                    except Exception:
                        logger.error("[Discord] Status-card terminal notice failed", exc_info=True)
                completed = terminal_succeeded or controls_removed or notice_succeeded
                if completed:
                    await self._remove_status_card_persistence(card)
                    card.terminal_completed = True
                return completed
            finally:
                if outcome == "interrupted" and not card.terminal_completed:
                    card.terminal_attempted = False
                else:
                    await self._compare_pop_status_card(card)
                card.terminalizing = False

    def _read_status_card_entries(self) -> list[dict[str, Any]]:
        from plugins.platforms.discord.adapter import json
        try:
            data = json.loads(self._status_card_persistence_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []

    def _status_card_run_is_current(self, session_key: str, generation: int) -> bool:

        run_key = (str(session_key), int(generation))
        if run_key in self._ended_status_runs:
            return False
        checker = getattr(self._gateway_controls, "is_current", None)
        if not callable(checker):
            return True
        try:
            return bool(checker(*run_key))
        except Exception:
            logger.error("[Discord] Status-card current-run check failed", exc_info=True)
            return True

    async def _reconcile_persisted_status_card(self, entry: dict[str, Any]) -> None:
        from plugins.platforms.discord.adapter import StatusCardView, time
        nonce = str(entry.get("nonce") or "")
        channel_id = str(entry.get("channel_id") or "")
        message_id = str(entry.get("message_id") or "")
        if not channel_id or not message_id:
            self._status_card_persisted.pop(nonce, None)
            return
        try:
            channel = await self._resolve_channel(channel_id)
            if channel is None:
                raise RuntimeError(f"Channel {channel_id} not found")
            message = channel.get_partial_message(int(message_id))
            view = StatusCardView(
                [], nonce=None, title="Hermes run", state="interrupted-restart",
                elapsed_s=max(0.0, time.time() - float(entry.get("started_at") or time.time())),
                iteration=0, max_iterations=0,
            )
            await message.edit(view=view)
            self._status_card_persisted.pop(nonce, None)
        except Exception as exc:
            try:
                attempts = max(0, int(entry.get("reconcile_attempts") or 0)) + 1
            except (TypeError, ValueError):
                attempts = 1
            if attempts >= 3:
                self._status_card_persisted.pop(nonce, None)
                logger.error(
                    "[Discord] Dropping status card %s/%s after 3 reconciliation failures: %s",
                    channel_id, message_id, exc, exc_info=True,
                )
            else:
                retained = dict(entry)
                retained["reconcile_attempts"] = attempts
                self._status_card_persisted[nonce] = retained
                logger.error(
                    "[Discord] Status-card reconciliation attempt %d/3 failed for %s/%s: %s",
                    attempts, channel_id, message_id, exc, exc_info=True,
                )

    async def _sweep_orphaned_status_cards(self, ended_nonce: str) -> None:
        for card in list(self._status_cards.values()):
            outcome = self._ended_status_nonces.get(card.nonce)
            if outcome is None:
                continue
            await self._terminalize_card_once(
                card, outcome=outcome, metadata=card.delivery_metadata,
            )
        live_nonces = set(self._status_card_keys_by_nonce)
        changed = False
        for nonce, entry in list(self._status_card_persisted.items()):
            if nonce in live_nonces or nonce in self._ended_status_nonces:
                continue
            session_key = str(entry.get("session_key") or "")
            try:
                generation = int(entry.get("generation"))
            except (TypeError, ValueError):
                continue
            if self._status_card_run_is_current(session_key, generation):
                continue
            await self._reconcile_persisted_status_card(entry)
            changed = True
        if changed:
            await self._persist_status_cards()

    async def _reconcile_status_cards(self) -> None:
        from plugins.platforms.discord.adapter import asyncio
        entries = await asyncio.to_thread(self._read_status_card_entries)
        self._status_card_persisted.update({
            str(entry.get("nonce")): dict(entry)
            for entry in entries
            if entry.get("nonce")
        })
        for entry in entries:
            nonce = str(entry.get("nonce") or "")
            if nonce in self._status_card_keys_by_nonce:
                continue
            await self._reconcile_persisted_status_card(entry)
        await self._persist_status_cards()

    def native_task_card_destination_supported(
        self, chat_id: str, *, reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        return self._hermes_run(metadata) is not None

    async def send_native_task_card_progress(
        self, chat_id: str, tasks: list[dict[str, str]], *, title: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
        fallback_text: Optional[str] = None,
    ) -> SendResult:
        from plugins.platforms.discord.adapter import CardState, SendResult, StatusCardCoalescer, StatusCardView, time
        if not self.native_task_cards_enabled():
            return SendResult(success=False, error="Discord native task cards disabled")
        run = self._hermes_run(metadata)
        if run is None or not tasks:
            return SendResult(success=False, error="Missing Discord status-card run or tasks")
        key = self._status_card_key(chat_id, metadata)
        card = self._status_cards.get(key)
        if card is not None and card.nonce != run["nonce"]:
            superseded_closed = await self._terminalize_card_once(
                card, outcome="interrupted", metadata=card.delivery_metadata,
            )
            if not superseded_closed:
                logger.warning(
                    "[Discord] Superseded status card %s could not be terminalized; restart reconciliation will retry",
                    card.nonce,
                )
                return SendResult(success=False, error="superseded card not terminalized")
            card = None
        if card is None:
            try:
                channel_id = self._status_card_channel_id(chat_id, metadata)
                channel = await self._resolve_channel(channel_id)
                if channel is None:
                    return SendResult(success=False, error=f"Channel {channel_id} not found")
                view = StatusCardView(
                    tasks, nonce=run["nonce"], title=title or "Hermes run", state="running",
                    elapsed_s=0.0, iteration=0, max_iterations=0,
                )
                from plugins.platforms.discord.status_card import retry_channel_send
                message = await retry_channel_send(channel, view)
            except Exception as exc:
                logger.error("[Discord] Status-card transport failed", exc_info=True)
                return self._status_card_error_result(exc)
            card = CardState(
                nonce=run["nonce"], generation=run["generation"],
                session_key=run["session_key"], chat_id=key[0], thread_key=key[1],
                channel_id=channel_id, message=message, tasks=[dict(task) for task in tasks],
                state="running", started_at=time.time(), title=title or "Hermes run",
                fallback_text=str(fallback_text or ""), delivery_metadata=dict(metadata or {}),
            )
            card.coalescer = StatusCardCoalescer(
                lambda frame: self._edit_status_card_frame(card, frame),
                on_failure=lambda result: self._handle_status_card_coalescer_failure(card, result),
            )
            self._status_cards[key] = card
            self._status_card_keys_by_nonce[card.nonce] = key
            if self.reaction_controls_enabled():
                self._reaction_registry.register_run_message(
                    str(message.id), session_key=run["session_key"],
                    generation=run["generation"], nonce=run["nonce"],
                )
            self._status_card_persisted[card.nonce] = {
                "channel_id": card.channel_id,
                "message_id": str(card.message.id),
                "nonce": card.nonce,
                "session_key": card.session_key,
                "generation": card.generation,
                "started_at": card.started_at,
                "reconcile_attempts": 0,
            }
            await self._persist_status_cards()
            self._start_status_card_refresh(card)
            return SendResult(success=True, message_id=str(message.id))
        card.tasks = [dict(task) for task in tasks]
        card.title = title or card.title
        card.fallback_text = str(fallback_text or card.fallback_text)
        card.delivery_metadata = dict(metadata or card.delivery_metadata)
        if card.state != "waiting-approval":
            card.state = "running"
        result = await card.coalescer.submit(card.frame())
        if self._status_card_permanent_failure(result):
            await self._disable_status_card_after_permanent_failure(
                card, send_fallback=False, result=result,
            )
        return result

    async def stop_native_task_card_progress(
        self, chat_id: str, *, reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        run = self._hermes_run(metadata)
        if run is None:
            return
        key = self._status_card_keys_by_nonce.get(run["nonce"])
        card = self._status_cards.get(key) if key is not None else None
        if card is None or card.generation != run["generation"] or card.session_key != run["session_key"]:
            return
        if card.coalescer is not None:
            result = await card.coalescer.flush()
            if self._status_card_permanent_failure(result):
                await self._disable_status_card_after_permanent_failure(
                    card, send_fallback=False, result=result,
                )

    async def update_native_task_card_activity(
        self, chat_id: str, *, elapsed_s: float, iteration: int, max_iterations: int,
        reply_to: Optional[str] = None, metadata: Optional[dict[str, Any]] = None,
    ) -> bool:
        run = self._hermes_run(metadata)
        if run is None:
            return False
        key = self._status_card_keys_by_nonce.get(run["nonce"])
        card = self._status_cards.get(key) if key is not None else None
        if card is None or card.generation != run["generation"] or card.session_key != run["session_key"]:
            return False
        card.elapsed_s = max(0.0, float(elapsed_s))
        card.iteration = max(0, int(iteration))
        card.max_iterations = max(0, int(max_iterations))
        result = await card.coalescer.submit(card.frame())
        if self._status_card_permanent_failure(result):
            await self._disable_status_card_after_permanent_failure(
                card, send_fallback=False, result=result,
            )
        return bool(result.success)

    @staticmethod
    def _log_status_card_run_mismatch(card: CardState, run: dict[str, Any]) -> None:

        if (card.session_key, card.generation) != (run["session_key"], run["generation"]):
            logger.info(
                "[Discord] Status-card run mismatch nonce=%s card=(%s, %s) run=(%s, %s)",
                card.nonce, card.session_key, card.generation, run["session_key"], run["generation"],
            )

    async def finalize_native_task_card(
        self, chat_id: str, *, outcome: str, reply_to: Optional[str],
        metadata: Optional[dict[str, Any]],
    ) -> bool:
        run = self._hermes_run(metadata)
        if run is None:
            return False
        key = self._status_card_keys_by_nonce.get(run["nonce"])
        card = self._status_cards.get(key) if key is not None else None
        if card is None:
            return False
        self._log_status_card_run_mismatch(card, run)
        return await self._terminalize_card_once(card, outcome=outcome, metadata=metadata)

    def _record_ended_status_nonce(self, nonce: str, outcome: str) -> None:
        self._ended_status_nonces[nonce] = outcome
        self._ended_status_nonces.move_to_end(nonce)
        while len(self._ended_status_nonces) > 200:
            self._ended_status_nonces.popitem(last=False)

    async def on_turn_end(
        self, chat_id: str, *, metadata: Optional[dict[str, Any]], outcome: str,
    ) -> None:
        run = self._hermes_run(metadata)
        if run is None:
            return
        self._record_ended_status_nonce(run["nonce"], outcome)
        self._reaction_registry.clear_run(run["session_key"], run["generation"])
        ended_key = (run["session_key"], run["generation"])
        self._ended_status_runs[ended_key] = None
        self._ended_status_runs.move_to_end(ended_key)
        while len(self._ended_status_runs) > 200:
            self._ended_status_runs.popitem(last=False)
        card_key = self._status_card_key(chat_id, metadata)
        for status_key in list(self._status_message_ids):
            if status_key[:3] == (card_key[0], card_key[1], run["generation"]):
                self._status_message_ids.pop(status_key, None)
        key = self._status_card_keys_by_nonce.get(run["nonce"])
        card = self._status_cards.get(key) if key is not None else None
        if card is not None:
            self._log_status_card_run_mismatch(card, run)
            await self._terminalize_card_once(card, outcome=outcome, metadata=metadata)
        await self._sweep_orphaned_status_cards(run["nonce"])

    async def _set_status_card_approval(
        self, metadata: Optional[dict[str, Any]], waiting: bool,
    ) -> None:
        run = self._hermes_run(metadata)
        if run is None:
            return
        key = self._status_card_keys_by_nonce.get(run["nonce"])
        card = self._status_cards.get(key) if key is not None else None
        if card is None or card.generation != run["generation"] or card.session_key != run["session_key"]:
            return
        card.state = "waiting-approval" if waiting else "running"
        result = await card.coalescer.submit(card.frame())
        if self._status_card_permanent_failure(result):
            await self._disable_status_card_after_permanent_failure(
                card, send_fallback=True, result=result,
            )

    async def send_or_update_status(
        self, chat_id: str, status_key: str, content: str, *,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        from plugins.platforms.discord.adapter import SendResult
        if not self.native_task_cards_enabled():
            return await self.send(chat_id, content, metadata=metadata)
        run = self._hermes_run(metadata)
        if run is None:
            return await self.send(chat_id, content, metadata=metadata)
        if (run["session_key"], run["generation"]) in self._ended_status_runs:
            return SendResult(success=False, error="Discord status run already ended")
        thread_key = str((metadata or {}).get("thread_id") or "")
        key = (str(chat_id), thread_key, run["generation"], str(status_key))
        cached_id = self._status_message_ids.get(key)
        if cached_id is not None:
            result = await self.edit_message(
                self._status_card_channel_id(chat_id, metadata), cached_id, content,
                finalize=False, metadata=metadata,
            )
            if result.success:
                return result
            self._status_message_ids.pop(key, None)
        result = await self.send(chat_id, content, metadata=metadata)
        if result.success and result.message_id:
            self._status_message_ids[key] = str(result.message_id)
        return result

    async def on_final_message(
        self, chat_id: str, message_id: str, *, metadata: Optional[dict[str, Any]],
    ) -> None:
        run = self._hermes_run(metadata)
        if run is None:
            return
        self._last_final_messages[run["session_key"]] = str(message_id)
        self._last_final_messages.move_to_end(run["session_key"])
        while len(self._last_final_messages) > 200:
            self._last_final_messages.popitem(last=False)

    async def handle_status_card_stop(self, interaction: Any, nonce: str) -> None:
        from plugins.platforms.discord.adapter import _component_check_auth, _unauthorized
        key = self._status_card_keys_by_nonce.get(nonce)
        card = self._status_cards.get(key) if key is not None else None
        if card is None or card.nonce != nonce:
            await interaction.followup.send("nothing running", ephemeral=True)
            return
        authorized = _component_check_auth(
            interaction, self._allowed_user_ids, self._allowed_role_ids,
            live_auth=self._component_live_auth,
        )
        if not authorized:
            await interaction.followup.send(_unauthorized(), ephemeral=True)
            return
        controls = self._gateway_controls
        if controls is None:
            if not self._gateway_controls_warning_emitted:
                logger.warning("[Discord] Status-card controls are unavailable; gateway controls were not injected")
                self._gateway_controls_warning_emitted = True
            await interaction.followup.send("nothing running", ephemeral=True)
            return
        stopped = await controls.stop_if_current(card.session_key, card.generation)
        await interaction.followup.send("stopping" if stopped else "nothing running", ephemeral=True)

    async def handle_status_card_show_all(self, interaction: Any, nonce: str) -> None:
        from plugins.platforms.discord.adapter import _component_check_auth, _unauthorized, build_status_card_history_markdown, discord, io, prune_status_card_history, status_card_history_filename
        await self._load_status_card_history()
        entry = self._status_card_history.get(nonce)
        if entry is None or not prune_status_card_history([entry]):
            await interaction.followup.send("Detailed history expired.", ephemeral=True)
            return
        user = getattr(interaction, "user", None)
        current_allowed_users = set(self._allowed_user_ids or set())
        current_allowed_roles = set(self._allowed_role_ids or set())
        user_id = getattr(user, "id", None)
        current_authorized = bool(current_allowed_users or current_allowed_roles) and _component_check_auth(
            interaction, current_allowed_users, current_allowed_roles,
            live_auth=self._component_live_auth,
        )
        owner_user_id = str(entry.get("owner_user_id") or "")
        owner_authorized = bool(owner_user_id and str(user_id) == owner_user_id and current_authorized)
        authorized = current_authorized or owner_authorized
        if not authorized:
            await interaction.followup.send(_unauthorized(), ephemeral=True)
            return
        data = build_status_card_history_markdown(entry)
        file = discord.File(
            io.BytesIO(data), filename=status_card_history_filename(entry["finished_at"]),
        )
        await interaction.followup.send(file=file, ephemeral=True)
