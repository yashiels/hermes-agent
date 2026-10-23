"""Restart-safe queue drain uses the same per-platform satellite adapter view as the ticker."""

from contextlib import contextmanager
from types import SimpleNamespace

from gateway.config import Platform

from tests.cron.satellite_composite_harness import (
    assert_own_discord,
    assert_routed_whatsapp,
    assert_unrouted_whatsapp,
    assert_zero_own,
    recording_adapter,
    satellite_routes,
)


def _drain_views(monkeypatch, profile_map, primary):
    import gateway.run as gateway_run
    from cron import scheduler

    captured = []

    @contextmanager
    def fake_scope(_home):
        yield

    monkeypatch.setattr(gateway_run, "_profile_runtime_scope", fake_scope)
    monkeypatch.setattr(
        gateway_run, "_handoff_watch_scopes",
        lambda _runner: [(None, None), ("jarvis", "/profiles/jarvis")],
    )
    monkeypatch.setattr(
        "cron.scheduler_preflight._primary_profile_routes_for_current_home",
        lambda: satellite_routes(),
    )
    monkeypatch.setattr(scheduler, "drain_delivery_queue", lambda adapters, _loop: captured.append(adapters))
    runner = SimpleNamespace(adapters=primary, _profile_adapters=profile_map)
    gateway_run._drain_restart_safe_cron_deliveries(primary, object(), runner)
    assert len(captured) == 2
    return captured[0], captured[1]


def test_drain_satellite_borrows_only_routed_unowned_platforms(monkeypatch):
    """Gateway queue drain: routed whatsapp leaves through primary; own discord does not."""
    own_discord = recording_adapter()
    primary_discord = recording_adapter()
    primary_wa = recording_adapter()
    primary = {Platform.DISCORD: primary_discord, Platform.WHATSAPP: primary_wa}
    primary_view, jarvis_view = _drain_views(
        monkeypatch, {"jarvis": {Platform.DISCORD: own_discord}}, primary,
    )
    assert primary_view is primary
    assert_routed_whatsapp(jarvis_view, primary_wa, own_discord, primary_discord)
    assert_unrouted_whatsapp(jarvis_view, primary_wa, own_discord, primary_discord)
    assert_own_discord(jarvis_view, primary_wa, own_discord, primary_discord)

    _primary_view, empty_view = _drain_views(monkeypatch, {"jarvis": {}}, primary)
    assert_zero_own(empty_view, primary_wa, primary_discord)
