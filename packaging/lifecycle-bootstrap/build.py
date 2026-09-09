#!/usr/bin/env python3
"""Build the checked-in lifecycle bootstrap from private source captures.

The source capture, display snapshot, and ownership ledger are inputs to this
tool.  The generated packet contains only the 24 non-running rows and safe
ownership metadata.  The source file digests and per-row digests let the
runtime verifier reject a packet whose evidence was changed independently.

This tool is run by the maintainer before a packet is committed.  It never
connects to Podman and never changes a host.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_ROWS = 24
EXPECTED_COUNTS = {
    "completed_one_shot": 3,
    "retired": 20,
    "infrastructure_only": 0,
    "stopped": 1,
}
SUPPORTED_OWNER_KINDS = {"compose", "unknown"}
POLICY_PROVENANCE_KIND = "operator-policy"
POLICY_SOURCE = "source-decisions.json"
SUPPORTED_EVIDENCE_REF = "worktrees/misc-source/train.home.complete.tech/ops/fleet-recovery-20260907.md"
SOURCE_CLASSES = {
    "completed_one_shot": "completed_one_shot",
    "retained_build_helper": "retired",
    "retained_created_fixture": "retired",
    "retained_host_service_replacement": "retired",
    "retained_replaced_service": "retired",
    "retained_service": "retired",
    "retained_stopped_object": "retired",
    "retained_test_fixture": "retired",
    "retained_validation_fixture": "retired",
    "retained_unknown_exited": "stopped",
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def text(value: Any, field: str, limit: int = 240) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} is not text")
    value = value.strip()
    if not value or len(value) > limit or re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError(f"{field} is empty or unsafe")
    return value


def full_id(value: Any, field: str = "id") -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ValueError(f"{field} is not a full container ID")
    return value


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def owner_from_capture(row: dict[str, Any]) -> dict[str, Any]:
    source = row.get("source_owner")
    if not isinstance(source, dict):
        raise ValueError(f"missing source owner for {row.get('id')}")
    kind = text(source.get("kind"), "owner kind", 64)
    if kind not in SUPPORTED_OWNER_KINDS:
        raise ValueError(f"unsupported owner kind: {kind}")
    project = source.get("compose_project")
    service = source.get("compose_service")
    unit = source.get("unit")
    for value, field in ((project, "owner project"), (service, "owner service"), (unit, "owner unit")):
        if value is not None:
            text(value, field, 240)
    labels = source.get("labels")
    if not isinstance(labels, dict):
        raise ValueError("owner labels are not an object")
    if kind == "compose":
        if not isinstance(project, str) or not project or not isinstance(service, str) or not service:
            raise ValueError("compose owner lacks project or service")
        if labels.get("com.docker.compose.project") != project or \
                labels.get("com.docker.compose.service") != service:
            raise ValueError("compose owner labels do not verify project and service")
        verified = True
    else:
        if project is not None or service is not None or unit is not None or labels:
            raise ValueError("unknown owner carries unsupported ownership claims")
        # The capture verifies that ownership is unknown.  It does not verify
        # a concrete owner, so lifecycle decisions remain explicit policy.
        verified = False
    return {
        "kind": kind,
        "project": project,
        "service": service,
        "unit": unit,
        "verified": bool(verified),
        "kind_known": kind != "unknown",
    }


def build(args: argparse.Namespace) -> None:
    ledger = load(args.ledger)
    capture = load(args.capture)
    display = load(args.display)
    if ledger.get("schema") != "podman-health-coverage-ledger.v1":
        raise ValueError("unsupported ledger schema")
    if capture.get("schema") != "podman-health-coverage-capture.v1":
        raise ValueError("unsupported capture schema")
    if not isinstance(display, dict) or not isinstance(display.get("rows"), list):
        raise ValueError("unsupported display schema")
    host = text(ledger.get("host"), "host", 253)
    if not HOST_RE.fullmatch(host) or host != capture.get("host"):
        raise ValueError("source hosts do not agree")
    if ledger.get("observed_at") != capture.get("observed_at"):
        raise ValueError("source timestamps do not agree")

    capture_rows: dict[tuple[str, int, str], dict[str, Any]] = {}
    for context in capture.get("contexts", []):
        if not isinstance(context, dict):
            continue
        for row in context.get("containers", []):
            if not isinstance(row, dict):
                continue
            identifier = full_id(row.get("id"))
            uid = row.get("uid")
            scope = row.get("context")
            if not isinstance(uid, int) or isinstance(uid, bool) or scope not in {"rootful", "rootless"}:
                raise ValueError("capture identity is invalid")
            key = (scope, uid, identifier)
            if key in capture_rows:
                raise ValueError("duplicate capture identity")
            capture_rows[key] = row

    display_rows: dict[str, dict[str, Any]] = {}
    for row in display["rows"]:
        if not isinstance(row, dict):
            raise ValueError("display row is not an object")
        identifier = full_id(row.get("id"))
        if identifier in display_rows:
            raise ValueError("duplicate display identity")
        display_rows[identifier] = row

    source_rows: list[dict[str, Any]] = []
    display_source_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    packet_rows: list[dict[str, Any]] = []
    for row in ledger.get("rows", []):
        lifecycle = row.get("lifecycle")
        if not isinstance(lifecycle, dict) or lifecycle.get("running") is not False:
            continue
        identifier = full_id(row.get("id"))
        context = row.get("context")
        uid = row.get("uid")
        key = (context, uid, identifier)
        capture_row = capture_rows.get(key)
        display_row = display_rows.get(identifier)
        if capture_row is None or display_row is None:
            raise ValueError(f"missing source evidence for {identifier}")
        if row.get("host") != host or capture_row.get("host") != host:
            raise ValueError(f"host mismatch for {identifier}")
        if capture_row.get("running") is not False:
            raise ValueError(f"capture says row is running: {identifier}")
        display_uid = display_row.get("uid")
        expected_display_uid = "0" if uid == 0 else "user"
        display_lifecycle = display_row.get("lifecycle")
        if isinstance(display_lifecycle, list) and len(display_lifecycle) == 1:
            display_lifecycle = display_lifecycle[0]
        if display_uid != expected_display_uid or display_lifecycle not in {"Exited", "Created", "exited", "created"}:
            raise ValueError(f"display lifecycle mismatch for {identifier}")
        disposition = lifecycle.get("disposition")
        if not isinstance(disposition, dict):
            raise ValueError(f"missing source decision for {identifier}")
        source_class = text(disposition.get("class"), "source class", 96)
        canonical_disposition = SOURCE_CLASSES.get(source_class)
        if canonical_disposition is None:
            raise ValueError(f"unsupported source class: {source_class}")
        reason = text(disposition.get("reason"), "source reason")
        evidence = text(disposition.get("evidence"), "source evidence")
        if evidence != SUPPORTED_EVIDENCE_REF:
            raise ValueError(f"unsupported source evidence for {identifier}")
        owner = owner_from_capture(capture_row)
        inventory_record = {
            "host": host,
            "context": context,
            "uid": uid,
            "id": identifier,
            "name": text(capture_row.get("name"), "inventory name"),
            "running": False,
            "state": text(capture_row.get("lifecycle"), "inventory state", 64),
            "exit_code": capture_row.get("exit_code"),
            "image_id": full_id(capture_row.get("image_id"), "image id"),
            "image_name": text(capture_row.get("image_name"), "image name"),
            "owner": owner,
        }
        display_record = {
            "host": host,
            "context": context,
            "uid": uid,
            "id": identifier,
            "name": text(display_row.get("name"), "display name"),
            "running": False,
            "lifecycle": text(display_lifecycle, "display lifecycle", 64),
            "health": text(display_row.get("health"), "display health", 64),
            "reason": text(display_row.get("reason"), "display reason"),
        }
        ownership_record = {
            "host": host,
            "context": context,
            "uid": uid,
            "id": identifier,
            "name": inventory_record["name"],
            "owner": owner,
            "owner_verified": owner["verified"],
        }
        source_rows.append(inventory_record)
        display_source_rows.append(display_record)
        decision = {
            "host": host,
            "context": context,
            "uid": uid,
            "id": identifier,
            "source_class": source_class,
            "disposition": canonical_disposition,
            "reason": reason,
            "source_ref": evidence,
            "source_ledger_row_sha256": digest(row),
            "inventory_row_sha256": digest(inventory_record),
            "display_row_sha256": digest(display_record),
            "ownership_row_sha256": digest(ownership_record),
            "owner_verified": owner["verified"],
        }
        decision_rows.append(decision)
        packet_rows.append({
            "host": host,
            "context": context,
            "uid": uid,
            "id": identifier,
            "name": inventory_record["name"],
            "running": False,
            "disposition": canonical_disposition,
            "reason": reason,
            "decision_sha256": digest(decision),
            "provenance": {
                "kind": POLICY_PROVENANCE_KIND,
                "source": POLICY_SOURCE,
                "source_ref": evidence,
                "policy_sha256": file_digest(args.ledger),
                "owner_verified": owner["verified"],
            },
        })

    source_rows.sort(key=lambda row: (row["context"], row["uid"], row["id"]))
    decision_rows.sort(key=lambda row: (row["context"], row["uid"], row["id"]))
    packet_rows.sort(key=lambda row: (row["context"], row["uid"], row["id"]))
    counts = {name: sum(row["disposition"] == name for row in packet_rows) for name in EXPECTED_COUNTS}
    if len(packet_rows) != EXPECTED_ROWS or counts != EXPECTED_COUNTS:
        raise ValueError(f"unexpected bootstrap rows: {len(packet_rows)} {counts}")

    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    source_payloads = {
        "source-inventory.json": {
            "schema": "cockpit-podman-lifecycle-source-inventory/v1",
            "host": host,
            "observed_at": ledger["observed_at"],
            "records": source_rows,
        },
        "source-display.json": {
            "schema": "cockpit-podman-lifecycle-source-display/v1",
            "host": host,
            "observed_at": ledger["observed_at"],
            "records": sorted(display_source_rows, key=lambda row: (row["context"], row["uid"], row["id"])),
        },
        "source-ownership.json": {
            "schema": "cockpit-podman-lifecycle-source-ownership/v1",
            "host": host,
            "observed_at": ledger["observed_at"],
            "records": [
                {
                    "host": row["host"],
                    "context": row["context"],
                    "uid": row["uid"],
                    "id": row["id"],
                    "name": row["name"],
                    "owner": row["owner"],
                    "owner_verified": row["owner"]["verified"],
                }
                for row in source_rows
            ],
        },
        "source-decisions.json": {
            "schema": "cockpit-podman-lifecycle-source-decisions/v1",
            "host": host,
            "observed_at": ledger["observed_at"],
            "source_ledger": {
                "artifact": "coverage-ledger-8d2c216-0131.json",
                "sha256": file_digest(args.ledger),
                "observed_at": ledger["observed_at"],
            },
            "records": decision_rows,
        },
    }
    for name, payload in source_payloads.items():
        (out / name).write_bytes(canonical(payload) + b"\n")

    packet = {
        "schema": "cockpit-podman-lifecycle-bootstrap/v1",
        "host": host,
        "observed_at": ledger["observed_at"],
        "record_count": EXPECTED_ROWS,
        "counts": counts,
        "source_manifest": "manifest.json",
        "records": packet_rows,
    }
    (out / "lifecycle-bootstrap.json").write_bytes(canonical(packet) + b"\n")

    files = []
    for name in sorted(["lifecycle-bootstrap.json", *source_payloads]):
        files.append({"path": name, "sha256": file_digest(out / name), "mode": "0644", "owner": "root"})
    manifest = {
        "schema": "cockpit-podman-lifecycle-manifest/v1",
        "packet": "lifecycle-bootstrap.json",
        "host": host,
        "observed_at": ledger["observed_at"],
        "record_count": EXPECTED_ROWS,
        "counts": counts,
        "source_ledger_sha256": file_digest(args.ledger),
        "source_files": files,
        "owner_policy": {"owner": "root", "mode": "0644", "group_writable": False, "other_writable": False},
        "generator": "packaging/lifecycle-bootstrap/build.py",
    }
    (out / "manifest.json").write_bytes(canonical(manifest) + b"\n")
    print(f"manifest_sha256={file_digest(out / 'manifest.json')}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--display", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
