#!/usr/bin/env python3
"""Read and project the protected lifecycle operator-policy packet.

The packet records explicit maintainer decisions against a private Podman
capture, a Cockpit display snapshot, and ownership evidence for the same full
IDs.  This process is read-only: it never invokes Podman, starts a container,
or changes a host.  Every file in the packet is hash-checked before any
record is emitted.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import socket
import stat
import sys
from pathlib import Path
from typing import Any


SCHEMA = "cockpit-podman-lifecycle/v1"
PACKET_SCHEMA = "cockpit-podman-lifecycle-bootstrap/v1"
MANIFEST_SCHEMA = "cockpit-podman-lifecycle-manifest/v1"
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,252}$")
MACHINE_ID = re.compile(r"^[0-9a-f]{32}\n?$", re.IGNORECASE)
TEXT_MAX = 240
MACHINE_ID_PATH = Path("/etc/machine-id")
MACHINE_ID_MAX_BYTES = 128
CONTEXTS = {"rootful", "rootless"}
DISPOSITIONS = {"completed_one_shot", "retired", "infrastructure_only", "stopped"}
POLICY_PROVENANCE_KIND = "operator-policy"
POLICY_SOURCE = "source-decisions.json"
POLICY_EVIDENCE_REF = "worktrees/misc-source/train.home.complete.tech/ops/fleet-recovery-20260907.md"
EXPECTED_RECORD_COUNT = 24
EXPECTED_COUNTS = {
    "completed_one_shot": 3,
    "retired": 20,
    "infrastructure_only": 0,
    "stopped": 1,
}
# This is deliberately pinned in code.  A caller cannot replace both the
# packet and its manifest without also changing the reviewed collector.
BOOTSTRAP_MANIFEST_SHA256 = "a7d4c0ac5e3f0a0040ee00ca485b394be6836f8ff170970824cb82f1c183c286"
SOURCE_LEDGER_SHA256 = "b58d2c2b95f46efc3cbd4fdf3b2c62a0024190c091d668e0a6f754d553ee4f81"
DEFAULT_PACKET = "/etc/cockpit-podman/lifecycle-bootstrap/lifecycle-bootstrap.json"


class RecordError(ValueError):
    """Raised when source data cannot safely produce lifecycle records."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RecordError(f"cannot read bootstrap file: {path.name}") from exc


def safe_text(value: Any, field: str, limit: int = TEXT_MAX) -> str:
    if not isinstance(value, str):
        raise RecordError(f"{field} is not text")
    value = value.strip()
    if not value or len(value) > limit or re.search(r"[\x00-\x1f\x7f]", value):
        raise RecordError(f"{field} is empty or unsafe")
    if re.search(r"https?://", value, flags=re.IGNORECASE):
        raise RecordError(f"{field} contains a URL")
    return value


def full_id(value: Any, field: str = "id") -> str:
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


def authoritative_host() -> str:
    """Return the host identity supplied by the operating system.

    An empty hostname or localhost placeholder is insufficient for a
    cross-host lifecycle join.  There is no fallback to a packet value or
    unchecked transport field.  The host may be unqualified when that is the
    authoritative name supplied by the backend.
    """

    host = socket.getfqdn().strip().lower()
    if not host or host in {"localhost", "localhost.localdomain"} or not HOSTNAME.fullmatch(host):
        raise RecordError("authoritative system hostname is unavailable")
    return safe_text(host, "authoritative host", 253)


def machine_id_digest() -> str:
    """Hash the protected, bounded OS machine identity.

    The manifest binds the packet to the host that produced the capture.  A
    caller supplied hostname or transport alias cannot replace this check.
    The file is opened without following symlinks, must be a root-owned
    regular file that is not writable by group or other users, and is read in
    a bounded loop before its canonical machine-id format is checked.
    """

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RecordError("secure machine identity open is unavailable")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | nofollow
    try:
        descriptor = os.open(MACHINE_ID_PATH, flags)
    except OSError as exc:
        raise RecordError("authoritative machine identity is unavailable") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or \
                (metadata.st_mode & 0o022) or not (metadata.st_mode & stat.S_IRUSR) or \
                metadata.st_size <= 0 or metadata.st_size > MACHINE_ID_MAX_BYTES:
            raise RecordError("authoritative machine identity file is not protected")
        content = bytearray()
        while len(content) <= MACHINE_ID_MAX_BYTES:
            chunk = os.read(descriptor, MACHINE_ID_MAX_BYTES + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MACHINE_ID_MAX_BYTES:
            raise RecordError("authoritative machine identity is too large")
    except OSError as exc:
        raise RecordError("authoritative machine identity cannot be read") from exc
    finally:
        os.close(descriptor)

    try:
        value = bytes(content).decode("ascii")
    except UnicodeDecodeError as exc:
        raise RecordError("authoritative machine identity is not ASCII") from exc
    if not MACHINE_ID.fullmatch(value):
        raise RecordError("authoritative machine identity has invalid format")
    # The captured binding is over the validated 32-hex machine ID.  The
    # optional file newline is formatting and is deliberately excluded.
    machine_id = value[:-1] if value.endswith("\n") else value
    return hashlib.sha256(machine_id.encode("ascii")).hexdigest()


def load_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordError(f"invalid bootstrap JSON: {path.name}") from exc


def source_key(row: dict[str, Any]) -> tuple[str, int, str]:
    context = row.get("context")
    uid = row.get("uid")
    identifier = full_id(row.get("id"))
    if context not in CONTEXTS or isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise RecordError("source identity is invalid")
    return context, uid, identifier


def verify_source_record(row: Any, host: str, expected: tuple[str, int, str], require_nonrunning: bool = False) -> tuple[str, int, str]:
    if not isinstance(row, dict):
        raise RecordError("source row is not an object")
    key = source_key(row)
    if key != expected or row.get("host") != host or (require_nonrunning and row.get("running") is not False):
        raise RecordError("source row identity or running state mismatch")
    return key


def verify_bootstrap(packet_path: Path, expected_manifest: str = BOOTSTRAP_MANIFEST_SHA256) -> tuple[dict[str, Any], str]:
    """Verify the packet, all source files, and every source row binding."""

    packet = load_json(packet_path)
    if not isinstance(packet, dict) or packet.get("schema") != PACKET_SCHEMA:
        raise RecordError("unsupported lifecycle bootstrap schema")
    packet_dir = packet_path.parent
    manifest_name = packet.get("source_manifest")
    if manifest_name != "manifest.json":
        raise RecordError("bootstrap manifest name is not canonical")
    manifest_path = packet_dir / manifest_name
    manifest = load_json(manifest_path)
    manifest_digest = file_digest(manifest_path)
    if expected_manifest and manifest_digest != expected_manifest:
        raise RecordError("bootstrap manifest digest does not match reviewed packet")
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise RecordError("unsupported lifecycle manifest schema")
    if manifest.get("packet") != "lifecycle-bootstrap.json":
        raise RecordError("manifest packet name is not canonical")
    if manifest.get("host") != packet.get("host") or manifest.get("observed_at") != packet.get("observed_at"):
        raise RecordError("packet and manifest identity differ")
    if manifest.get("record_count") != EXPECTED_RECORD_COUNT or manifest.get("counts") != EXPECTED_COUNTS:
        raise RecordError("manifest record contract is not the protected 24-row contract")
    if manifest.get("source_ledger_sha256") != SOURCE_LEDGER_SHA256:
        raise RecordError("manifest source ledger digest is not the reviewed capture")
    host_binding = manifest.get("host_binding")
    if not isinstance(host_binding, dict) or \
            host_binding.get("hostname") != packet.get("host") or \
            host_binding.get("fqdn") != packet.get("host") or \
            not isinstance(host_binding.get("machine_id_sha256"), str) or \
            not HEX64.fullmatch(host_binding["machine_id_sha256"]):
        raise RecordError("manifest host binding is not the reviewed host context")
    if machine_id_digest() != host_binding["machine_id_sha256"]:
        raise RecordError("manifest host binding does not match this machine")

    listed = manifest.get("source_files")
    if not isinstance(listed, list):
        raise RecordError("manifest source files are missing")
    expected_files = {"lifecycle-bootstrap.json", "source-inventory.json", "source-display.json",
                      "source-ownership.json", "source-decisions.json"}
    actual_files = {entry.get("path") for entry in listed if isinstance(entry, dict)}
    if actual_files != expected_files or len(listed) != len(expected_files):
        raise RecordError("manifest source file set is not canonical")
    for entry in listed:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or "/" in entry["path"] or "\\" in entry["path"]:
            raise RecordError("manifest contains an unsafe path")
        source_path = packet_dir / entry["path"]
        if file_digest(source_path) != entry.get("sha256"):
            raise RecordError(f"source digest mismatch: {entry['path']}")
        if entry["path"] == "lifecycle-bootstrap.json" and source_path != packet_path:
            raise RecordError("packet path does not match manifest")

    host = safe_text(packet.get("host"), "host", 253).lower()
    observed = observed_at(packet.get("observed_at"))
    records = packet.get("records")
    if not isinstance(records, list) or packet.get("record_count") != len(records):
        raise RecordError("bootstrap record count is invalid")
    if len(records) != EXPECTED_RECORD_COUNT or packet.get("counts") != EXPECTED_COUNTS:
        raise RecordError("bootstrap is not the protected 24-row packet")

    sources: dict[str, dict[tuple[str, int, str], dict[str, Any]]] = {}
    expected_schemas = {
        "source-inventory.json": "cockpit-podman-lifecycle-source-inventory/v1",
        "source-display.json": "cockpit-podman-lifecycle-source-display/v1",
        "source-ownership.json": "cockpit-podman-lifecycle-source-ownership/v1",
        "source-decisions.json": "cockpit-podman-lifecycle-source-decisions/v1",
    }
    for name, schema in expected_schemas.items():
        source = load_json(packet_dir / name)
        if not isinstance(source, dict) or source.get("schema") != schema or source.get("host") != host or source.get("observed_at") != observed:
            raise RecordError(f"invalid source artifact: {name}")
        rows = source.get("records")
        if not isinstance(rows, list) or len(rows) != EXPECTED_RECORD_COUNT:
            raise RecordError(f"invalid source row count: {name}")
        by_key: dict[tuple[str, int, str], dict[str, Any]] = {}
        for row in rows:
            key = verify_source_record(row, host, source_key(row), name in {"source-inventory.json", "source-display.json"})
            if key in by_key:
                raise RecordError(f"duplicate source identity: {name}")
            by_key[key] = row
        sources[name] = by_key

    source_ledger = load_json(packet_dir / "source-decisions.json").get("source_ledger")
    if not isinstance(source_ledger, dict) or source_ledger.get("artifact") != "coverage-ledger-8d2c216-0131.json" or \
            source_ledger.get("sha256") != SOURCE_LEDGER_SHA256:
        raise RecordError("source ledger evidence digest is not pinned")

    seen: set[tuple[str, int, str]] = set()
    actual_counts = {name: 0 for name in EXPECTED_COUNTS}
    for record in records:
        if not isinstance(record, dict):
            raise RecordError("bootstrap record is not an object")
        key = source_key(record)
        if key in seen:
            raise RecordError("duplicate lifecycle identity")
        seen.add(key)
        verify_source_record(record, host, key, True)
        disposition = record.get("disposition")
        if disposition not in DISPOSITIONS:
            raise RecordError("unsupported canonical disposition")
        actual_counts[disposition] += 1
        reason = safe_text(record.get("reason"), "lifecycle reason")
        provenance = record.get("provenance")
        if not isinstance(provenance, dict) or provenance.get("kind") != POLICY_PROVENANCE_KIND or \
                provenance.get("source") != POLICY_SOURCE:
            raise RecordError("lifecycle record provenance is not operator-policy-bound")
        if provenance.get("source_ref") != POLICY_EVIDENCE_REF:
            raise RecordError("lifecycle record evidence is not the reviewed policy source")
        if provenance.get("policy_sha256") != SOURCE_LEDGER_SHA256 or \
                not isinstance(provenance.get("owner_verified"), bool):
            raise RecordError("lifecycle record policy evidence is invalid")

        inventory = sources["source-inventory.json"][key]
        display = sources["source-display.json"][key]
        ownership = sources["source-ownership.json"][key]
        decision = sources["source-decisions.json"][key]
        if record.get("name") != inventory.get("name") or record.get("name") != display.get("name"):
            raise RecordError("source names do not agree")
        if record.get("decision_sha256") != digest(decision):
            raise RecordError("lifecycle record decision digest mismatch")
        if decision.get("inventory_row_sha256") != digest(inventory) or \
                decision.get("display_row_sha256") != digest(display) or \
                decision.get("ownership_row_sha256") != digest(ownership):
            raise RecordError("source row digest binding mismatch")
        if decision.get("disposition") != disposition or decision.get("reason") != reason or \
                decision.get("owner_verified") is not ownership.get("owner_verified") or \
                provenance.get("owner_verified") is not ownership.get("owner_verified"):
            raise RecordError("source decision does not agree with packet")
        if display.get("running") is not False or inventory.get("running") is not False:
            raise RecordError("source evidence is running")
        safe_text(decision.get("source_class"), "source class", 96)
        if decision.get("source_ref") != provenance.get("source_ref") or \
                decision.get("source_ref") != POLICY_EVIDENCE_REF:
            raise RecordError("source decision evidence does not agree with policy")

    if actual_counts != EXPECTED_COUNTS or len(seen) != EXPECTED_RECORD_COUNT:
        raise RecordError("source packet counts do not match protected contract")
    return {"schema": PACKET_SCHEMA, "host": host, "observed_at": observed, "records": records}, manifest_digest


def stdin_identity() -> tuple[int | None, set[str] | None, str | None]:
    payload = sys.stdin.read()
    if not payload.strip():
        return None, None, None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RecordError("identity input is not JSON") from exc
    if not isinstance(data, dict) or set(data) - {"uid", "ids", "host"}:
        raise RecordError("identity input has unsupported fields")
    uid = data.get("uid")
    if isinstance(uid, bool) or not isinstance(uid, int) or uid < 0:
        raise RecordError("identity UID is invalid")
    ids = data.get("ids")
    if not isinstance(ids, list):
        raise RecordError("identity IDs are not a list")
    result = {full_id(value, "identity ID") for value in ids}
    host = data.get("host")
    if host is not None:
        if not isinstance(host, str) or not HOSTNAME.fullmatch(host.strip()) or \
                host.strip().lower() in {"localhost", "localhost.localdomain"}:
            raise RecordError("identity host is invalid")
        host = host.strip().lower()
    return uid, result, host


def project(packet: dict[str, Any], requested_uid: int, requested_ids: set[str] | None = None,
            authoritative: str | None = None, runtime_uid: int | None = None,
            manifest_digest: str | None = None, projected_at: str | None = None) -> dict[str, Any]:
    """Project verified packet rows for one authoritative owner context."""

    if authoritative is None:
        authoritative = authoritative_host()
    if packet.get("host") != authoritative.lower():
        raise RecordError("bootstrap host does not match authoritative system host")
    if runtime_uid is None:
        runtime_uid = os.getuid()
    if requested_uid != runtime_uid:
        raise RecordError("requested owner UID does not match process owner")
    if manifest_digest is None:
        manifest_digest = BOOTSTRAP_MANIFEST_SHA256
    projected_at = observed_at(projected_at or dt.datetime.now(dt.timezone.utc).isoformat())
    records = []
    for row in packet["records"]:
        if row["uid"] != requested_uid:
            continue
        if requested_ids is not None and row["id"] not in requested_ids:
            continue
        provenance = dict(row["provenance"])
        provenance["manifest_sha256"] = manifest_digest
        provenance["decision_sha256"] = row["decision_sha256"]
        records.append({
            "host": row["host"],
            "context": row["context"],
            "uid": row["uid"],
            "id": row["id"],
            "disposition": row["disposition"],
            "reason": row["reason"],
            "provenance": provenance,
        })
    records.sort(key=lambda record: (record["context"], record["uid"], record["id"]))
    return {
        "schema": SCHEMA,
        "host": packet["host"],
        "observed_at": packet["observed_at"],
        "projected_at": projected_at,
        "manifest_sha256": manifest_digest,
        "record_count": len(records),
        "records": records,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_PACKET, help="verified lifecycle bootstrap packet")
    parser.add_argument("--uid", type=int, required=True, help="one Podman owner UID")
    args = parser.parse_args(argv)
    try:
        requested_uid, requested_ids, requested_host = stdin_identity()
        if requested_uid is not None and args.uid != requested_uid:
            raise RecordError("UID argument does not match identity input")
        if requested_uid is None:
            requested_uid = args.uid
        packet, manifest_digest = verify_bootstrap(Path(args.input))
        host = authoritative_host()
        if requested_host is not None and requested_host != host:
            raise RecordError("identity host does not match authoritative system host")
        output = project(packet, requested_uid, requested_ids, host, os.getuid(), manifest_digest)
    except (OSError, RecordError) as exc:
        print(f"lifecycle disposition collection failed: {exc}", file=sys.stderr)
        return 2
    json.dump(output, sys.stdout, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
