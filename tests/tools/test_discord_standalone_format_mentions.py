import asyncio
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


class _FakeAllowedMentions:
    def __init__(self, *, everyone=True, roles=True, users=True, replied_user=True):
        self.everyone = everyone
        self.roles = roles
        self.users = users
        self.replied_user = replied_user

    def to_dict(self):
        parse = [name for name, flag in (
            ("everyone", self.everyone), ("roles", self.roles), ("users", self.users),
        ) if flag is True]
        return {"parse": parse, "replied_user": self.replied_user}


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=2, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )
    discord_mod.opus = SimpleNamespace(is_loaded=lambda: True)
    discord_mod.AllowedMentions = _FakeAllowedMentions

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules["discord"] = discord_mod
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from gateway.config import Platform  # noqa: E402
from plugins.platforms.discord.adapter import (  # noqa: E402
    _build_allowed_mentions, _remember_channel_is_forum, _standalone_send,
)
from tools.send_message_tool import _send_to_platform  # noqa: E402


def _resp(status, json_data=None, text_data=None):
    r = AsyncMock()
    r.status = status
    body = json.dumps(json_data or {}).encode() if json_data is not None else (text_data or "").encode()
    r.json = AsyncMock(return_value=json_data or {})
    r.text = AsyncMock(return_value=text_data or "")
    r.content = MagicMock()
    r.content.read = AsyncMock(side_effect=[body, b"", b""])
    r.get_encoding = MagicMock(return_value="utf-8")
    return r


def _session_with(responses):
    calls = []
    idx = [0]

    def _post(url, **kwargs):
        calls.append((url, kwargs.get("json"), kwargs.get("data")))
        r = responses[idx[0]] if idx[0] < len(responses) else responses[-1]
        idx[0] += 1
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=r)
        ctx.__aexit__ = AsyncMock(return_value=False)
        return ctx

    session = MagicMock()
    session.post = MagicMock(side_effect=_post)
    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx, calls


def _pconfig(extra=None):
    return SimpleNamespace(token="bot-token", extra=extra or {})


def _payload_json(form_data):
    for field in getattr(form_data, "_fields", []):
        try:
            type_opts = field[0]
            value = field[2]
        except (IndexError, TypeError):
            continue
        if type_opts.get("name") == "payload_json":
            return json.loads(value)
    return None


def _default_mentions():
    return _build_allowed_mentions({}).to_dict()


class TestStandaloneSendMentionsDefaultChannel:

    def test_channel_send_carries_allowed_mentions(self):
        chat_id = "111222"
        _remember_channel_is_forum(chat_id, False)
        session_ctx, calls = _session_with([_resp(200, {"id": "m1"})])
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(_standalone_send(_pconfig(), chat_id, "hello"))
        assert result["success"] is True
        assert calls[0][1] == {"content": "hello", "allowed_mentions": _default_mentions()}

    def test_thread_reply_carries_allowed_mentions(self):
        session_ctx, calls = _session_with([_resp(200, {"id": "m2"})])
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(
                _standalone_send(_pconfig(), "111222", "hi there", thread_id="555")
            )
        assert result["success"] is True
        assert calls[0][0] == "https://discord.com/api/v10/channels/555/messages"
        assert calls[0][1] == {"content": "hi there", "allowed_mentions": _default_mentions()}

    def test_mentions_respect_config_override(self):
        chat_id = "111333"
        _remember_channel_is_forum(chat_id, False)
        extra = {"allow_mentions": {"everyone": True}}
        session_ctx, calls = _session_with([_resp(200, {"id": "m3"})])
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(_standalone_send(_pconfig(extra), chat_id, "ping"))
        assert result["success"] is True
        expected = _build_allowed_mentions(extra).to_dict()
        assert "everyone" in expected["parse"]
        assert calls[0][1]["allowed_mentions"] == expected

    def test_missing_media_caption_fallback_carries_allowed_mentions(self):
        chat_id = "111444"
        _remember_channel_is_forum(chat_id, False)
        session_ctx, calls = _session_with([_resp(200, {"id": "m4"})])
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(), chat_id, "",
                    media_files=[("/does/not/exist.png", False)],
                    caption="look at this",
                )
            )
        assert result["success"] is True
        assert calls[-1][1] == {"content": "look at this", "allowed_mentions": _default_mentions()}


class TestStandaloneSendMentionsMediaUpload:

    def test_media_caption_payload_json_carries_allowed_mentions(self, tmp_path):
        chat_id = "111555"
        _remember_channel_is_forum(chat_id, False)
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG")
        session_ctx, calls = _session_with([_resp(200, {"id": "m5"})])
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(), chat_id, "", media_files=[(str(img), False)], caption="a photo",
                )
            )
        assert result["success"] is True
        payload = _payload_json(calls[-1][2])
        assert payload == {"content": "a photo", "allowed_mentions": _default_mentions()}


class TestStandaloneSendMentionsForum:

    def test_forum_without_media_carries_allowed_mentions(self):
        thread_data = {"id": "t1", "message": {"id": "sm1"}}
        session_ctx, calls = _session_with([_resp(200, thread_data)])
        with patch("aiohttp.ClientSession", return_value=session_ctx), \
             patch("gateway.channel_directory.lookup_channel_type", return_value="forum"):
            result = asyncio.run(_standalone_send(_pconfig(), "forum_ch", "Hello forum"))
        assert result["success"] is True
        _url, body, _data = calls[0]
        assert body["message"] == {"content": "Hello forum", "allowed_mentions": _default_mentions()}

    def test_forum_with_media_payload_json_carries_allowed_mentions(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG")
        thread_data = {"id": "t2", "message": {"id": "sm2"}}
        session_ctx, calls = _session_with([_resp(200, thread_data)])
        with patch("aiohttp.ClientSession", return_value=session_ctx), \
             patch("gateway.channel_directory.lookup_channel_type", return_value="forum"):
            result = asyncio.run(
                _standalone_send(
                    _pconfig(), "forum_ch", "starter text", media_files=[(str(img), False)],
                )
            )
        assert result["success"] is True
        payload = _payload_json(calls[0][2])
        assert payload["message"]["content"] == "starter text"
        assert payload["message"]["allowed_mentions"] == _default_mentions()


def _discover_discord_plugin():
    from hermes_cli.plugins import discover_plugins
    discover_plugins()


class TestSendToPlatformFormatsBeforeChunking:

    def test_short_message_formatted_in_single_post(self):
        _discover_discord_plugin()
        chat_id = "222111"
        _remember_channel_is_forum(chat_id, False)
        content = "#### Heading\n\n---\n\n| A | B |\n|---|---|\n| 1 | 2 |"
        session_ctx, calls = _session_with([_resp(200, {"id": "p1"})])
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(
                _send_to_platform(Platform.DISCORD, _pconfig(), chat_id, content)
            )
        assert result["success"] is True
        assert len(calls) == 1
        sent = calls[0][1]["content"]
        assert "####" not in sent
        assert sent.startswith("### Heading")
        assert "---" not in sent
        assert "|---" not in sent
        assert calls[0][1]["allowed_mentions"] == _default_mentions()

    def test_long_message_chunks_on_formatted_length(self):
        _discover_discord_plugin()
        chat_id = "222222"
        _remember_channel_is_forum(chat_id, False)
        filler = "x" * 2400
        content = f"#### Heading\n{filler}\n\n---\n\nTail."
        session_ctx, calls = _session_with(
            [_resp(200, {"id": "c1"}), _resp(200, {"id": "c2"}), _resp(200, {"id": "c3"})]
        )
        with patch("aiohttp.ClientSession", return_value=session_ctx):
            result = asyncio.run(
                _send_to_platform(Platform.DISCORD, _pconfig(), chat_id, content)
            )
        assert result["success"] is True
        assert len(calls) >= 2
        full_text = "".join(call[1]["content"] for call in calls)
        assert "####" not in full_text
        assert "\n---\n" not in full_text
        assert full_text.startswith("### ")
        assert "Heading" in full_text
        assert "Tail." in full_text
        for call in calls:
            assert call[1]["allowed_mentions"] == _default_mentions()
