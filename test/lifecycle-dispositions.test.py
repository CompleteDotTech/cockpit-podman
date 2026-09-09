#!/usr/bin/env python3
"""Regression tests for the protected 24-row lifecycle bootstrap."""

import importlib.util
import hashlib
import io
import json
import os
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lifecycle_dispositions", ROOT / "src" / "lifecycle-dispositions.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)
BUILD_SPEC = importlib.util.spec_from_file_location(
    "lifecycle_bootstrap_build", ROOT / "packaging" / "lifecycle-bootstrap" / "build.py")
BUILD = importlib.util.module_from_spec(BUILD_SPEC)
assert BUILD_SPEC.loader is not None
BUILD_SPEC.loader.exec_module(BUILD)


HOST = "completetrain-b550-gaming-x-v2"
MACHINE_ID_SHA256 = "7c577a84da00e3b2683cbb782268e70733a3edd59b26794a5ccbd0367e5a2829"
PACKET_PATH = ROOT / "packaging" / "lifecycle-bootstrap" / "lifecycle-bootstrap.json"

# The production reader must hash the validated machine-id value, not its
# optional file newline, while still rejecting symlinked or unprotected files.
if os.geteuid() == 0:
    original_machine_id_path = MODULE.MACHINE_ID_PATH
    with tempfile.TemporaryDirectory() as temporary:
        machine_path = Path(temporary) / "machine-id"
        machine_value = "a" * 32
        machine_path.write_text(machine_value + "\n", encoding="ascii")
        machine_path.chmod(0o444)
        assert machine_path.stat().st_uid == 0
        MODULE.MACHINE_ID_PATH = machine_path
        try:
            assert MODULE.machine_id_digest() == hashlib.sha256(machine_value.encode("ascii")).hexdigest()
            symlink_path = Path(temporary) / "machine-id-link"
            symlink_path.symlink_to(machine_path)
            MODULE.MACHINE_ID_PATH = symlink_path
            try:
                MODULE.machine_id_digest()
            except MODULE.RecordError:
                pass
            else:
                raise AssertionError("symlinked machine identity must fail closed")
        finally:
            MODULE.MACHINE_ID_PATH = original_machine_id_path

original_machine_id_digest = MODULE.machine_id_digest
MODULE.machine_id_digest = lambda: MACHINE_ID_SHA256
try:
    packet, manifest_digest = MODULE.verify_bootstrap(PACKET_PATH)
finally:
    MODULE.machine_id_digest = original_machine_id_digest
assert manifest_digest == MODULE.BOOTSTRAP_MANIFEST_SHA256
assert len(packet["records"]) == 24
assert {record["id"] for record in packet["records"]} == {
    record["id"] for record in json.loads(PACKET_PATH.read_text())["records"]
}
assert {record["disposition"] for record in packet["records"]} == {"completed_one_shot", "retired", "stopped"}
assert {record["provenance"]["kind"] for record in packet["records"]} == {"operator-policy"}
assert {record["provenance"]["source"] for record in packet["records"]} == {"source-decisions.json"}
assert {record["provenance"]["source_ref"] for record in packet["records"]} == {BUILD.SUPPORTED_EVIDENCE_REF}
assert {record["provenance"]["policy_sha256"] for record in packet["records"]} == {MODULE.SOURCE_LEDGER_SHA256}
assert sum(record["provenance"]["owner_verified"] is True for record in packet["records"]) == 21
assert sum(record["provenance"]["owner_verified"] is False for record in packet["records"]) == 3
assert MODULE.project(packet, 0, authoritative=HOST, runtime_uid=0, manifest_digest=manifest_digest,
                      projected_at="2026-09-09T04:00:00+00:00")["projected_at"] == "2026-09-09T04:00:00+00:00"

rootful = MODULE.project(packet, 0, authoritative=HOST, runtime_uid=0, manifest_digest=manifest_digest)
rootless = MODULE.project(packet, 1000, authoritative=HOST, runtime_uid=1000, manifest_digest=manifest_digest)
assert rootful["record_count"] == 22
assert rootless["record_count"] == 2
assert [record["disposition"] for record in rootless["records"]] == ["stopped", "completed_one_shot"]
assert sum(record["disposition"] == "retired" for record in rootful["records"]) == 20
assert sum(record["disposition"] == "completed_one_shot" for record in rootful["records"]) == 2
assert all(record["provenance"]["manifest_sha256"] == manifest_digest for record in rootful["records"] + rootless["records"])

try:
    MODULE.project(packet, 0, authoritative="other.example.test", runtime_uid=0, manifest_digest=manifest_digest)
except MODULE.RecordError:
    pass
else:
    raise AssertionError("host mismatch must fail closed")

try:
    MODULE.project(packet, 0, authoritative=HOST, runtime_uid=1000, manifest_digest=manifest_digest)
except MODULE.RecordError:
    pass
else:
    raise AssertionError("owner context mismatch must fail closed")

original_stdin = MODULE.sys.stdin
MODULE.sys.stdin = io.StringIO('{"uid": 0, "containers": []}')
try:
    try:
        MODULE.stdin_identity()
    except MODULE.RecordError:
        pass
    else:
        raise AssertionError("generic identity metadata must fail closed")
finally:
    MODULE.sys.stdin = original_stdin

MODULE.sys.stdin = io.StringIO(json.dumps({"uid": 0, "ids": [], "host": HOST}))
try:
    assert MODULE.stdin_identity() == (0, set(), HOST)
finally:
    MODULE.sys.stdin = original_stdin


def assert_owner_rejected(row):
    try:
        BUILD.owner_from_capture(row)
    except ValueError:
        return
    raise AssertionError("unsupported owner evidence must fail closed")


assert_owner_rejected({"source_owner": {"kind": "fabricated-owner", "labels": {}}})
assert_owner_rejected({"source_owner": {
    "kind": "compose",
    "compose_project": "project",
    "compose_service": "service",
    "labels": {"com.docker.compose.project": "other", "com.docker.compose.service": "service"},
}})
unknown_owner = BUILD.owner_from_capture({"source_owner": {"kind": "unknown", "labels": {}}})
assert unknown_owner["kind"] == "unknown"
assert unknown_owner["verified"] is False
compose_owner = BUILD.owner_from_capture({"source_owner": {
    "kind": "compose",
    "compose_project": "project",
    "compose_service": "service",
    "labels": {"com.docker.compose.project": "project", "com.docker.compose.service": "service"},
}})
assert compose_owner["verified"] is True

with tempfile.TemporaryDirectory() as temporary:
    copy = Path(temporary) / "packet"
    shutil.copytree(PACKET_PATH.parent, copy)
    source_path = copy / "source-inventory.json"
    source = json.loads(source_path.read_text())
    source["records"][0]["running"] = True
    source_path.write_text(json.dumps(source, sort_keys=True, separators=(",", ":")) + "\n")
    MODULE.machine_id_digest = lambda: MACHINE_ID_SHA256
    try:
        MODULE.verify_bootstrap(copy / "lifecycle-bootstrap.json", expected_manifest=manifest_digest)
    except MODULE.RecordError:
        pass
    else:
        raise AssertionError("tampered source evidence must fail digest verification")
    finally:
        MODULE.machine_id_digest = original_machine_id_digest

print("lifecycle-disposition 24-row bootstrap fixtures: PASS")
