#!/usr/bin/env python3
"""Regression tests for the source-backed lifecycle record projection."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lifecycle_dispositions", ROOT / "src" / "lifecycle-dispositions.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


HOST = "train.home.complete.tech"


def row(index, context, uid, source_class):
    identifier = f"{index:064x}"
    return {
        "host": HOST,
        "context": context,
        "uid": uid,
        "id": identifier,
        "lifecycle": {
            "running": False,
            "state": "exited",
            "disposition": {
                "class": source_class,
                "reason": f"reason for {source_class}",
                "evidence": "ops/fleet-recovery-20260907.md",
            },
        },
    }


ledger = {
    "schema": MODULE.LEDGER_SCHEMA,
    "host": HOST,
    "observed_at": "2026-09-09T01:32:15+00:00",
    "rows": [
        row(1, "rootful", 0, "completed_one_shot"),
        row(2, "rootful", 0, "retained_test_fixture"),
        row(3, "rootful", 0, "infrastructure_only"),
        row(4, "rootless", 1000, "retained_unknown_exited"),
    ],
}

projected = MODULE.project(ledger)
assert projected["schema"] == MODULE.SCHEMA
assert projected["record_count"] == 4
assert [record["disposition"] for record in projected["records"]] == [
    "completed_one_shot", "retired", "infrastructure_only", "stopped",
]
assert projected["records"][-1]["provenance"]["source_class"] == "retained_unknown_exited"

try:
    MODULE.project({**ledger, "rows": [{**ledger["rows"][0], "host": "other-host"}]})
except MODULE.RecordError:
    pass
else:
    raise AssertionError("host mismatch must fail closed")

missing_evidence = {**ledger, "rows": [{
    **ledger["rows"][0],
    "lifecycle": {
        **ledger["rows"][0]["lifecycle"],
        "disposition": {"class": "completed_one_shot", "reason": "known"},
    },
}]}
try:
    MODULE.project(missing_evidence)
except MODULE.RecordError:
    pass
else:
    raise AssertionError("missing provenance must fail closed")

print("lifecycle-disposition fixtures: PASS")
