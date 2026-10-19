import json
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from agent.i18n import t
from gateway.platforms.base import SendResult
from gateway.whatsapp_identity import expand_whatsapp_aliases, to_whatsapp_jid
from tools import approval, clarify_gateway, slash_confirm

_POLL_QUESTION_LIMIT = 255
_catalog_t = t

logger = logging.getLogger(__name__)


def _clarify_poll_question(question):
    text = str(question or "")
    suffix = "\n" + _catalog_t("gateway.clarify.skip_hint")
    return text.removesuffix(suffix).strip()


def _slash_confirm_question(title, message):
    full = f"*{title}*\n\n{message}"
    if len(full) <= _POLL_QUESTION_LIMIT:
        return full, None
    short = f"*{title}*"
    if len(short) > _POLL_QUESTION_LIMIT:
        short = short[:_POLL_QUESTION_LIMIT]
    return short, full


def _mapped_choice(label_to_choice, selected):
    if isinstance(selected, list) and len(selected) == 1:
        return label_to_choice.get(selected[0])
    return None


def _approval_reply_key(choice, count):
    if not count:
        return "platform.whatsapp.approval_expired"
    if choice == "deny":
        return "platform.whatsapp.denied"
    return "platform.whatsapp.approved"


def _binding(kind, session_key, chat_id, options, state):
    return PollBinding(kind, session_key, chat_id, options, to_whatsapp_jid(chat_id), time.time(), state)


@dataclass
class ClarifyState:
    clarify_id: str
    multi: bool
    selection: list[str] = field(default_factory=list)


@dataclass
class ApprovalState:
    request_id: str
    label_to_choice: dict[str, str]


@dataclass
class SlashConfirmState:
    confirm_id: str
    label_to_choice: dict[str, str]


@dataclass
class ChoiceState:
    title: str
    choices: list[dict]
    page: int
    per_page: int
    label_to_value: dict[str, str]
    on_choice_selected: Callable[[str, str], Awaitable[str | None]]


@dataclass
class PollBinding:
    kind: Literal["clarify", "approval", "slash_confirm", "choice"]
    session_key: str
    chat_id: str
    options: list[str]
    expected_voter: str
    created_at: float
    state: ClarifyState | ApprovalState | SlashConfirmState | ChoiceState


class PollClarifyMixin:
    def _init_poll_bindings(self):
        self._poll_bindings: OrderedDict[str, PollBinding] = OrderedDict()
        self._retired_poll_ids: OrderedDict[str, None] = OrderedDict()

    def _retire_poll(self, poll_id):
        self._poll_bindings.pop(poll_id, None)
        self._retired_poll_ids[poll_id] = None
        while len(self._retired_poll_ids) > 512:
            self._retired_poll_ids.popitem(last=False)

    def _bind_poll(self, poll_id, binding):
        self._poll_bindings[poll_id] = binding
        while len(self._poll_bindings) > 256:
            self._retire_poll(next(iter(self._poll_bindings)))

    async def _consume_poll_update(self, data):
        if data.get("nativeType") != "pollUpdateMessage":
            return False
        update = (data.get("nativeMetadata") or {}).get("pollUpdate") or {}
        poll_id = update.get("pollId")
        bindings = getattr(self, "_poll_bindings", {})
        binding = bindings.get(poll_id)
        if binding and time.time() - binding.created_at >= 86400:
            self._retire_poll(poll_id)
            binding = None
        if binding is None:
            retired = poll_id in getattr(self, "_retired_poll_ids", {})
            if retired:
                logger.debug("[whatsapp] Ignored retired poll update")
            return retired
        voter = data.get("senderId", "")
        authorised = (
            not data.get("isGroup")
            and not str(data.get("chatId", "")).endswith("@g.us")
            and bool(expand_whatsapp_aliases(voter, self._session_path)
                     & expand_whatsapp_aliases(binding.expected_voter, self._session_path))
        )
        if not authorised:
            logger.info("[whatsapp] Rejected poll voter …%s for peer …%s", str(voter).split("@")[0][-4:], binding.expected_voter.split("@")[0][-4:])
            return True
        handlers = {
            "clarify": (ClarifyState, self._handle_clarify_vote),
            "approval": (ApprovalState, self._handle_approval_vote),
            "slash_confirm": (SlashConfirmState, self._handle_slash_confirm_vote),
        }
        dispatch = handlers.get(binding.kind)
        if dispatch is None or not isinstance(binding.state, dispatch[0]):
            logger.debug("[whatsapp] Ignored unsupported poll kind or state mismatch: %s", binding.kind)
            return True
        await dispatch[1](poll_id, binding, update.get("selectedOptions"))
        return True

    async def _handle_clarify_vote(self, poll_id, binding, selected):
        if not isinstance(selected, list):
            return
        state = binding.state
        with clarify_gateway._lock:
            entry = clarify_gateway._entries.get(state.clarify_id)
            pending = entry is not None and not entry.event.is_set()
        if not pending:
            self._retire_poll(poll_id)
            return
        selection = [label for label in binding.options if label in selected]
        if selection == state.selection:
            return
        state.selection = selection
        done = binding.options[-2] if state.multi else None
        other = binding.options[-1]
        if not selection or (state.multi and done not in selection):
            return
        if other in selection:
            if clarify_gateway.mark_awaiting_text_if_pending(state.clarify_id):
                await self.send(binding.chat_id, t("platform.whatsapp.clarify_type_answer"))
            else:
                self._retire_poll(poll_id)
            return
        labels = [label for label in selection if label not in {done, other}]
        response = (json.dumps(labels, ensure_ascii=False) if labels else clarify_gateway.SKIPPED) if state.multi else labels[0]
        clarify_gateway.resolve_gateway_clarify(state.clarify_id, response)
        self._retire_poll(poll_id)

    async def _handle_approval_vote(self, poll_id, binding, selected):
        choice = _mapped_choice(binding.state.label_to_choice, selected)
        if choice is None:
            return
        count = approval.resolve_gateway_approval(binding.session_key, choice, request_id=binding.state.request_id)
        self._retire_poll(poll_id)
        await self.send(binding.chat_id, t(_approval_reply_key(choice, count)))

    async def _handle_slash_confirm_vote(self, poll_id, binding, selected):
        choice = _mapped_choice(binding.state.label_to_choice, selected)
        if choice is None:
            return
        state = binding.state
        self._retire_poll(poll_id)
        text = await slash_confirm.resolve(binding.session_key, state.confirm_id, choice)
        if text:
            await self.send(binding.chat_id, text)

    async def _send_exec_approval_prompt(self, prompt):
        if str(prompt.chat_id).endswith("@g.us") or not prompt.request_id:
            return SendResult(success=False)
        sent = await self.send(prompt.chat_id, prompt.text, metadata=prompt.metadata)
        if not sent.success:
            return sent
        options = [label for label, _choice, _style in prompt.actions]
        result = await self.send_poll(prompt.chat_id, t("platform.whatsapp.approval_poll_question"), options, selectable_count=1)
        if not result.success or not result.message_id:
            return SendResult(success=False, error=result.error)
        self._bind_poll(result.message_id, _binding(
            "approval", prompt.session_key, prompt.chat_id, options,
            ApprovalState(prompt.request_id, {label: choice for label, choice, _style in prompt.actions}),
        ))
        return sent

    async def send_slash_confirm(self, chat_id, title, message, session_key, confirm_id, metadata=None):
        if str(chat_id).endswith("@g.us"):
            return SendResult(success=False)
        once, always, cancel = (
            t("platform.whatsapp.slash_confirm_once"),
            t("platform.whatsapp.slash_confirm_always"),
            t("platform.whatsapp.slash_confirm_cancel"),
        )
        question, preamble = _slash_confirm_question(title, message)
        if preamble is not None:
            sent = await self.send(chat_id, preamble, metadata=metadata)
            if not sent.success:
                return sent
        options = [once, always, cancel]
        result = await self.send_poll(chat_id, question, options, selectable_count=1)
        if not result.success or not result.message_id:
            return SendResult(success=False, error=result.error)
        self._bind_poll(result.message_id, _binding(
            "slash_confirm", session_key, chat_id, options,
            SlashConfirmState(confirm_id, {once: "once", always: "always", cancel: "cancel"}),
        ))
        return result

    async def send_clarify(self, chat_id, question, choices, clarify_id, session_key, metadata=None):
        clean = [str(choice).strip() for choice in choices or [] if str(choice).strip()]
        if str(chat_id).endswith("@g.us"):
            if 2 <= len(clean) <= 12:
                result = await self.send_poll(chat_id, _clarify_poll_question(question), clean, selectable_count=1)
                if result.success:
                    return result
        else:
            clean = list(dict.fromkeys(clean))
            with clarify_gateway._lock:
                multi = bool(getattr(clarify_gateway._entries.get(clarify_id), "multi_select", False))
            done, other = t("platform.whatsapp.clarify_done"), t("platform.whatsapp.clarify_other")
            if clean and len(clean) <= (10 if multi else 11) and not {done, other}.intersection(clean):
                options = clean + ([done, other] if multi else [other])
                result = await self.send_poll(chat_id, _clarify_poll_question(question), options, selectable_count=len(options) if multi else 1)
                if result.success and result.message_id:
                    self._bind_poll(result.message_id, PollBinding("clarify", session_key, chat_id, options, to_whatsapp_jid(chat_id), time.time(), ClarifyState(clarify_id, multi)))
                    return result
        return await super().send_clarify(chat_id=chat_id, question=question, choices=choices, clarify_id=clarify_id, session_key=session_key, metadata=metadata)

    async def retire_clarify_card(self, clarify_id, notice):
        for poll_id, binding in list(self._poll_bindings.items()):
            if isinstance(binding.state, ClarifyState) and binding.state.clarify_id == clarify_id:
                self._retire_poll(poll_id)
