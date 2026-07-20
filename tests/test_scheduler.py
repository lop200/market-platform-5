from __future__ import annotations

from app.core.scheduler import start_scheduler, stop_scheduler


def test_start_scheduler_registers_daily_job():
    scheduler = start_scheduler()
    try:
        job = scheduler.get_job("self_audit_daily")
        assert job is not None
    finally:
        stop_scheduler()


def test_start_scheduler_is_idempotent():
    first = start_scheduler()
    second = start_scheduler()
    try:
        assert first is second
    finally:
        stop_scheduler()


def test_stop_scheduler_without_start_is_a_noop():
    stop_scheduler()  # should not raise even if nothing was started
