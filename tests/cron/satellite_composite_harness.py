"""Delivery checks shared by the three satellite adapter construction paths."""

import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

from gateway.config import Platform, PlatformConfig
from gateway.profile_routing import ProfileRoute

WA_ROUTED = "207417493201128"
WA_OTHER = "000111"
DISCORD_OWN = "own-channel"
DISCORD_ROUTED = "primary-channel"


def satellite_routes():
    return [
        ProfileRoute(name="jarvis-wa", platform="whatsapp", profile="jarvis", chat_id=WA_ROUTED),
        ProfileRoute(
            name="jarvis-dc", platform="discord", profile="jarvis", chat_id=DISCORD_ROUTED,
        ),
    ]


def recording_adapter():
    adapter = MagicMock()
    adapter.sent = []

    async def send(chat_id, content, metadata=None):
        adapter.sent.append(str(chat_id))
        return {"success": True, "message_id": "m1"}

    adapter.send = send
    return adapter


def deliver(adapters, target):
    from cron.scheduler import _deliver_result

    loop = MagicMock()
    loop.is_running.return_value = True

    def fake_run_coro(coro, _loop):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future

    standalone = []

    async def _fake_send(platform, pconfig, chat_id, text, **kwargs):
        standalone.append(str(chat_id))
        return {"success": False, "error": "credential is not set"}

    config = MagicMock()
    config.platforms = {Platform.DISCORD: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda _platform: None
    job = {"id": "job-sat", "name": "brief", "deliver": target}
    with patch("gateway.config.load_gateway_config", return_value=config), \
         patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}), \
         patch("tools.send_message_tool._send_to_platform", _fake_send), \
         patch("asyncio.run_coroutine_threadsafe", side_effect=fake_run_coro):
        error = _deliver_result(job, "hello", adapters=adapters, loop=loop)
    return error, standalone


def _clear(*adapters):
    for adapter in adapters:
        adapter.sent.clear()


def assert_routed_whatsapp(adapters, primary_wa, own_discord, primary_discord):
    _clear(primary_wa, own_discord, primary_discord)
    error, standalone = deliver(adapters, f"whatsapp:{WA_ROUTED}")
    assert error is None, error
    assert primary_wa.sent == [WA_ROUTED]
    assert own_discord.sent == []
    assert primary_discord.sent == []
    assert standalone == []


def assert_unrouted_whatsapp(adapters, primary_wa, own_discord, primary_discord):
    _clear(primary_wa, own_discord, primary_discord)
    error, standalone = deliver(adapters, f"whatsapp:{WA_OTHER}")
    assert error is not None and "not configured/enabled" in error
    assert primary_wa.sent == []
    assert own_discord.sent == []
    assert primary_discord.sent == []
    assert standalone == []


def assert_own_discord(adapters, primary_wa, own_discord, primary_discord):
    _clear(primary_wa, own_discord, primary_discord)
    error, standalone = deliver(adapters, f"discord:{DISCORD_OWN}")
    assert error is None, error
    assert own_discord.sent == [DISCORD_OWN]
    assert primary_discord.sent == []
    assert primary_wa.sent == []
    assert standalone == []


def assert_zero_own(adapters, primary_wa, primary_discord):
    from cron.scheduler_preflight import SharedRouteAdapters

    assert isinstance(adapters, SharedRouteAdapters)
    assert adapters.get(Platform.WHATSAPP) is None
    assert adapters.get(Platform.WHATSAPP, {"chat_id": WA_OTHER}) is None
    _clear(primary_wa, primary_discord)
    error, standalone = deliver(adapters, f"whatsapp:{WA_ROUTED}")
    assert error is None, error
    assert primary_wa.sent == [WA_ROUTED]
    assert primary_discord.sent == []
    assert standalone == []
    _clear(primary_wa, primary_discord)
    error, standalone = deliver(adapters, f"whatsapp:{WA_OTHER}")
    assert error is not None and "not configured/enabled" in error
    assert primary_wa.sent == [] and standalone == []
    _clear(primary_wa, primary_discord)
    error, standalone = deliver(adapters, f"discord:{DISCORD_OWN}")
    assert primary_discord.sent == []
    assert error is not None
    assert standalone == [DISCORD_OWN]
