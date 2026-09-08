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

    print("health-scheduler safety tests: PASS")


if __name__ == "__main__":
    main()
