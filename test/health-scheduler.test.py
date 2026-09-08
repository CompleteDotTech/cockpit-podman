#!/usr/bin/env python3
# SPDX-License-Identifier: LGPL-2.1-or-later

"""Focused safety tests for the read-only scheduler collector."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "health-scheduler.py"
SPEC = importlib.util.spec_from_file_location("health_scheduler", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def main() -> None:
    overflow = MODULE.run_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * (8 * 1024 * 1024 + 1))"],
        5,
    )
    assert overflow.returncode == 75
    assert overflow.stdout == ""
    assert overflow.timed_out is False

    aggregate_overflow = MODULE.run_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * (5 * 1024 * 1024)); sys.stderr.write('y' * (5 * 1024 * 1024))"],
        5,
    )
    assert aggregate_overflow.returncode == 75
    assert aggregate_overflow.stdout == ""
    assert aggregate_overflow.stderr == "output exceeded collector limit"

    timed_out = MODULE.run_bounded([sys.executable, "-c", "import time; time.sleep(5)"], 0.05)
    assert timed_out.returncode == 124
    assert timed_out.timed_out is True

    valid_schedule, valid_errors = MODULE._timer_schedule({
        "OnUnitActiveSec": "30s",
        "AccuracySec": "1s",
        "NextElapseUSecRealtime": "Mon 2026-09-08 12:01:30 EDT",
    })
    assert valid_schedule["effective_interval_seconds"] == 30
    assert valid_schedule["next_trigger"]
    assert "missing-next-trigger" not in valid_errors

    missing_next, missing_errors = MODULE._timer_schedule({
        "OnUnitActiveSec": "30s",
        "AccuracySec": "1s",
    })
    assert missing_next["next_trigger"] is None
    assert "missing-next-trigger" in missing_errors

    # Keep the subprocess path explicit so descriptor cleanup is exercised even
    # when the child exits nonzero.
    failed = MODULE.run_bounded([sys.executable, "-c", "raise SystemExit(3)"], 5)
    assert failed.returncode == 3

    # An active timer that is elapsed rather than waiting cannot prove that a
    # future health check is scheduled.
    container_id = "a" * 64
    ref = MODULE.ContainerRef(uid=0, container_id=container_id, name="fixture", source_index=0)
    original_run_bounded = MODULE.run_bounded

    def fake_systemctl(argv, _timeout):
        if "list-units" in argv:
            return MODULE.CommandResult(list(argv), 0,
                                        "podman-healthcheck@fixture.timer loaded active waiting -\n", "")
        if "podman-healthcheck@fixture.timer" in argv:
            return MODULE.CommandResult(
                list(argv), 0,
                "Id=podman-healthcheck@fixture.timer\n"
                "ActiveState=active\nSubState=elapsed\n"
                "Unit=podman-healthcheck@fixture.service\n"
                "OnUnitActiveSec=30s\nAccuracySec=1s\n"
                "NextElapseUSecRealtime=Mon 2026-09-08 12:01:30 EDT\n\n", "")
        return MODULE.CommandResult(
            list(argv), 0,
            "Id=podman-healthcheck@fixture.service\n"
            "ActiveState=active\nSubState=running\n"
            "ExecStart=/usr/bin/podman healthcheck run fixture\n\n", "")

    MODULE.run_bounded = fake_systemctl
    try:
        coverage = MODULE.collect_coverage([ref], uid=0, manager="system", systemctl_bin="fixture-systemctl")
    finally:
        MODULE.run_bounded = original_run_bounded
    record = coverage["containers"][ref.key]
    assert record["active_coverage_count"] == 0
    assert any(item["reason"] == "timer-not-waiting" for item in coverage["unattributed_health_timers"])

    print("health-scheduler safety tests: PASS")


if __name__ == "__main__":
    main()
