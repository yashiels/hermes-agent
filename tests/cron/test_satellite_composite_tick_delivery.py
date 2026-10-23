"""A satellite that owns some adapters still delivers cron output for a platform it does not own.

The multiplex ticker's adapter view is per platform: the satellite's own adapter for a
platform it runs, and the primary adapter only when an exact enabled route maps the target.
"""

import threading
from unittest.mock import patch

from gateway.config import Platform

from tests.cron.satellite_composite_harness import (
    assert_own_discord,
    assert_routed_whatsapp,
    assert_unrouted_whatsapp,
    assert_zero_own,
    recording_adapter,
    satellite_routes,
)


def _tick_views(tmp_path, monkeypatch, profile_adapters, primary, label):
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_constants import get_hermes_home

    primary_home = tmp_path / label
    jarvis_home = primary_home / "profiles" / "jarvis"
    primary_home.mkdir(parents=True)
    jarvis_home.mkdir(parents=True)
    captured = {}
    stop = threading.Event()

    def _tick(*_args, **kwargs):
        captured[str(get_hermes_home())] = kwargs.get("adapters")
        if str(jarvis_home) in captured and str(primary_home) in captured:
            stop.set()
        return 0

    monkeypatch.setattr(
        "cron.scheduler_preflight._primary_profile_routes_for_current_home",
        lambda: satellite_routes(),
    )
    with patch("cron.scheduler.tick", side_effect=_tick):
        thread = threading.Thread(
            target=InProcessCronScheduler().start,
            args=(stop,),
            kwargs={
                "interval": 60,
                "profile_homes": [("default", str(primary_home)), ("jarvis", str(jarvis_home))],
                "profile_adapters": profile_adapters,
                "adapters": primary,
                "default_profile": "default",
            },
            daemon=True,
        )
        thread.start()
        assert stop.wait(10), "multiplex tick did not capture both profiles"
        thread.join(timeout=5)
    assert not thread.is_alive()
    return captured[str(primary_home)], captured[str(jarvis_home)], primary


def test_tick_satellite_borrows_only_routed_unowned_platforms(tmp_path, monkeypatch):
    """Scheduled tick: own discord stays on the satellite; routed whatsapp uses primary."""
    own_discord = recording_adapter()
    primary_discord = recording_adapter()
    primary_wa = recording_adapter()
    primary = {Platform.DISCORD: primary_discord, Platform.WHATSAPP: primary_wa}
    default_view, jarvis_view, primary_map = _tick_views(
        tmp_path, monkeypatch,
        {"jarvis": {Platform.DISCORD: own_discord}}, primary, "mixed",
    )
    assert default_view is primary_map
    assert_routed_whatsapp(jarvis_view, primary_wa, own_discord, primary_discord)
    assert_unrouted_whatsapp(jarvis_view, primary_wa, own_discord, primary_discord)
    assert_own_discord(jarvis_view, primary_wa, own_discord, primary_discord)

    _default_view, empty_view, _primary_map = _tick_views(
        tmp_path, monkeypatch, {"jarvis": {}}, primary, "empty",
    )
    assert_zero_own(empty_view, primary_wa, primary_discord)
