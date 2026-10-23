"""Manual cron runs build the same per-platform satellite adapter view as the ticker."""

from pathlib import Path
from types import SimpleNamespace
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

_JOB = {
    "id": "job-run-1", "name": "manual run", "prompt": "hi",
    "schedule": {"kind": "cron", "expr": "0 9 * * *"},
}


def _manual_view(own, primary, *, shared):
    from tools.cronjob_tools import _execute_job_now

    def adapters_for(profile):
        if profile == "jarvis" and own:
            return own
        return primary

    runner = SimpleNamespace(
        adapters=primary,
        _gateway_loop=object(),
        _profile_adapters={"jarvis": own},
        _adapters_for_profile=adapters_for,
        _is_shared_bot_satellite=lambda profile: shared and profile == "jarvis",
    )
    completed = {"id": "job-run-1", "last_status": "ok", "last_error": None}
    with patch(
        "tools.cronjob_tools.claim_job_for_fire",
        return_value={**_JOB, "fire_claim": {"by": "manual-owner"}},
    ), patch("gateway.run._gateway_runner_ref", return_value=runner), \
         patch("hermes_constants.get_hermes_home", return_value=Path("/srv/hermes/profiles/jarvis")), \
         patch(
             "cron.scheduler_preflight._primary_profile_routes_for_current_home",
             return_value=satellite_routes(),
         ), patch("cron.scheduler.run_one_job", return_value=True) as run_job, \
         patch("tools.cronjob_tools.get_job", return_value=completed):
        result = _execute_job_now(dict(_JOB))
    assert result["success"] is True
    return run_job.call_args.kwargs["adapters"]


def test_manual_run_satellite_borrows_only_routed_unowned_platforms():
    """cronjob action=run: routed whatsapp uses primary; the satellite's discord bot stays its own."""
    own_discord = recording_adapter()
    primary_discord = recording_adapter()
    primary_wa = recording_adapter()
    primary = {Platform.DISCORD: primary_discord, Platform.WHATSAPP: primary_wa}
    view = _manual_view({Platform.DISCORD: own_discord}, primary, shared=True)
    assert_routed_whatsapp(view, primary_wa, own_discord, primary_discord)
    assert_unrouted_whatsapp(view, primary_wa, own_discord, primary_discord)
    assert_own_discord(view, primary_wa, own_discord, primary_discord)

    empty = _manual_view({}, primary, shared=True)
    assert_zero_own(empty, primary_wa, primary_discord)
