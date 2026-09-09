#!/usr/bin/env python3
"""Project the sanitized Train coverage ledger into Cockpit lifecycle records.

The coverage ledger is the source of owner intent.  This helper only reads it;
it never inspects, starts, stops, or changes a container.  The browser joins
the records on the current host, owner context, UID, and full container ID.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA = "cockpit-podman-lifecycle/v1"
LEDGER_SCHEMA = "podman-health-coverage-ledger.v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TEXT_MAX = 240
CONTEXTS = {"rootful", "rootless"}

# These classes are owner statements from fleet-recovery-20260907.md.  They
# are deliberately mapped to one small public vocabulary; the source class is
# retained in provenance for auditability.
RETIRED_CLASSES = {
    "retained_build_helper",
    "retained_created_fixture",
    "retained_host_service_replacement",
    "retained_replaced_service",
    "retained_service",
    "retained_stopped_object",
    "retained_test_fixture",
    "retained_validation_fixture",
    "retired",
    "retired_model",
    "retired_service",
}


class RecordError(ValueError):
    """Raised when source data cannot safely produce lifecycle records."""


def safe_text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RecordError(f"{field} is not text")
    value = value.strip()
    if not value or len(value) > TEXT_MAX or re.search(r"[\x00-\x1f\x7f]", value):
        raise RecordError(f"{field} is empty or unsafe")
    if re.search(r"https?://", value, flags=re.IGNORECASE):
        raise RecordError(f"{field} contains a URL")
    return value


def full_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise RecordError(f"{field} is not a full container ID")
    return value


def observed_at(value: Any) -> str:
    text = safe_text(value, "observed_at")
    try:
        dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecordError("observed_at is not an ISO timestamp") from exc
    return text


def canonical_disposition(source_class: Any) -> str | None:
    if not isinstance(source_class, str):
        raise RecordError("lifecycle disposition class is not text")
    normalized = source_class.strip().lower().replace("-", "_")
    if normalized == "active_workload":
        return None
    if normalized == "completed_one_shot":
        return "completed_one_shot"
    if normalized in RETIRED_CLASSES:
        return "retired"
    if normalized in {"infrastructure", "infrastructure_only", "infra", "infra_only"}:
        return "infrastructure_only"
    # An unidentified retained object is intentionally visible as stopped.
    # It has no owner evidence for a stronger retired claim.
    if normalized in {"retained_unknown_exited", "stopped"}:
        return "stopped"
    raise RecordError(f"unsupported lifecycle class: {normalized}")


def stdin_identity() -> tuple[int | None, set[str] | None]:
    payload = sys.stdin.read()
    if not payload.strip():
        return None, None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RecordError("identity input is not JSON") from exc
    if not isinstance(data, dict):
        raise RecordError("identity input is not an object")

    uid = data.get("uid")
    if uid is not None and (isinstance(uid, bool) or not isinstance(uid, int) or uid < 0):
        raise RecordError("identity UID is invalid")
    ids = data.get("ids")
    if ids is None and isinstance(data.get("containers"), list):
        ids = [row.get("id") for row in data["containers"] if isinstance(row, dict)]
    if ids is None:
        return uid, None
    if not isinstance(ids, list):
        raise RecordError("identity IDs are not a list")
    result = {full_id(value, "identity ID") for value in ids}
    return uid, result


def project(ledger: Any, requested_uid: int | None = None,
            requested_ids: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(ledger, dict) or ledger.get("schema") != LEDGER_SCHEMA:
        raise RecordError("unsupported coverage ledger schema")
    host = safe_text(ledger.get("host"), "host")
    observed = observed_at(ledger.get("observed_at"))
    rows = ledger.get("rows")
    if not isinstance(rows, list):
        raise RecordError("ledger rows are not a list")

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RecordError("ledger row is not an object")
        row_host = safe_text(row.get("host"), "row host")
        if row_host != host:
            raise RecordError("row host does not match ledger host")
        context = row.get("context")
        if context not in CONTEXTS:
            raise RecordError("row context is invalid")
        uid = row.get("uid")
        if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
            raise RecordError("row UID is invalid")
        if requested_uid is not None and uid != requested_uid:
            continue
        identifier = full_id(row.get("id"), "row ID")
        if requested_ids is not None and identifier not in requested_ids:
            continue
        key = (context, uid, identifier)
        if key in seen:
            raise RecordError("duplicate lifecycle identity")
        seen.add(key)

        lifecycle = row.get("lifecycle")
        if not isinstance(lifecycle, dict):
            raise RecordError("row lifecycle is not an object")
        disposition = lifecycle.get("disposition")
        if not isinstance(disposition, dict):
            raise RecordError("row lifecycle disposition is not an object")
        canonical = canonical_disposition(disposition.get("class"))
        if canonical is None:
            continue
        if lifecycle.get("running") is not False:
            raise RecordError("non-active lifecycle record is still running")
        reason = safe_text(disposition.get("reason"), "lifecycle reason")
        evidence = safe_text(disposition.get("evidence"), "lifecycle evidence")
        records.append({
            "host": host,
            "context": context,
            "uid": uid,
            "id": identifier,
            "disposition": canonical,
            "reason": reason,
            "provenance": {
                "kind": "source",
                "source": evidence,
                "source_class": safe_text(disposition.get("class"), "source class"),
            },
        })

    records.sort(key=lambda record: (record["context"], record["uid"], record["id"]))
    return {
        "schema": SCHEMA,
        "host": host,
        "observed_at": observed,
        "record_count": len(records),
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="/etc/cockpit-podman/lifecycle-ledger.json",
                        help="sanitized podman-health-coverage-ledger.v1 JSON")
    parser.add_argument("--uid", type=int, help="limit output to one Podman owner UID")
    args = parser.parse_args(argv)
    try:
        requested_uid, requested_ids = stdin_identity()
        if args.uid is not None and requested_uid is not None and args.uid != requested_uid:
            raise RecordError("UID argument does not match identity input")
        requested_uid = args.uid if args.uid is not None else requested_uid
        with Path(args.input).open(encoding="utf-8") as stream:
            ledger = json.load(stream)
        output = project(ledger, requested_uid, requested_ids)
    except (OSError, json.JSONDecodeError, RecordError) as exc:
        print(f"lifecycle disposition collection failed: {exc}", file=sys.stderr)
        return 2
    json.dump(output, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
