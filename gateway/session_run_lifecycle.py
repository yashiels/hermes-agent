import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass
class AdmissionRun:
    adapter: Any = None
    event: Any = None
    session_key: str = ''
    home: Any = None
    claimed: bool = False
    delivery_cancelled: bool = False


def _runs(authority):
    runs = getattr(authority, 'card_runs', None)
    if runs is None:
        runs = authority.card_runs = {}
    return runs


def bind_delivery_owner(authority, admission_id, event, waiter):
    run = _runs(authority).setdefault(admission_id, AdmissionRun())
    event._hermes_admission_run = run
    waiter.add_done_callback(lambda _: _discard_unexecuted_run(authority, admission_id, run))
    if run.event is not None:
        copy_run_context(run.event, event)
    return run


def cancel_delivery_owner(run, event):
    run.delivery_cancelled = True
    if run.event is not None:
        copy_run_context(run.event, event)


def _discard_unexecuted_run(authority, admission_id, run):
    if run.event is None and _runs(authority).get(admission_id) is run:
        _runs(authority).pop(admission_id)


def release_delivery_owner(authority, admission_id, run):
    if not run.delivery_cancelled:
        _discard_unexecuted_run(authority, admission_id, run)


def copy_run_context(source, target):
    token = getattr(source, '_hermes_run', None)
    if not isinstance(token, dict):
        return
    target._hermes_run = dict(token)
    target._hermes_turn_outcome = getattr(source, '_hermes_turn_outcome', 'failed')
    run = getattr(source, '_hermes_admission_run', None)
    if isinstance(run, AdmissionRun):
        target._hermes_admission_run = run


def capture_run(authority, admission_id, event, home):
    token = getattr(event, '_hermes_run', None)
    if not isinstance(token, dict):
        return
    adapter = authority.runner._adapter_for_source(event.source)
    if adapter is None:
        return
    run = _runs(authority).setdefault(admission_id, AdmissionRun())
    run.adapter, run.event, run.session_key, run.home = adapter, event, token['session_key'], home
    if run.delivery_cancelled:
        event._hermes_turn_outcome = 'interrupted'
    event._hermes_admission_run = run
    adopter = getattr(authority, 'marker_adopters', {}).get(admission_id)
    if adopter is not None:
        copy_run_context(event, adopter)


def claim_run(event):
    run = getattr(event, '_hermes_admission_run', None)
    if not isinstance(run, AdmissionRun):
        return True
    if run.claimed:
        return False
    run.claimed = True
    return True


async def finish_delivery(adapter, event, session_key, attempted, succeeded):
    if not isinstance(getattr(event, '_hermes_run', None), dict):
        return
    from gateway.platforms.base import _thread_metadata_for_event
    task = adapter._start_card_hook_task(
        event, session_key, asyncio.Event(), _thread_metadata_for_event(event), attempted, succeeded,
    )
    await asyncio.shield(task)


async def settle_run(authority, admission_id, *, cancelled=False):
    run = _runs(authority).pop(admission_id, None)
    if run is None or run.event is None:
        return
    adopter = getattr(authority, 'marker_adopters', {}).get(admission_id)
    if adopter is not None:
        copy_run_context(run.event, adopter)
        if not cancelled:
            return
    from gateway.session_ingress import _execution_scope
    with await _execution_scope(authority, run.home):
        await finish_delivery(run.adapter, run.event, run.session_key, False, False)


async def finish_execution(authority, admission_id, event, home, *, handed, cancelled):
    capture_run(authority, admission_id, event, home)
    try:
        if cancelled:
            await settle_run(authority, admission_id, cancelled=True)
    finally:
        if not handed:
            from gateway.session_ingress import _release_turn_marker
            await _release_turn_marker(authority, event)
