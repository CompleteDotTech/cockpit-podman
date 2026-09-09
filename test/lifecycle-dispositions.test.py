#!/usr/bin/env python3
"""Regression tests for the protected 24-row lifecycle bootstrap."""

import importlib.util
import io
import json
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("lifecycle_dispositions", ROOT / "src" / "lifecycle-dispositions.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


HOST = "train.home.complete.tech"
PACKET_PATH = ROOT / "packaging" / "lifecycle-bootstrap" / "lifecycle-bootstrap.json"
packet, manifest_digest = MODULE.verify_bootstrap(PACKET_PATH)
assert manifest_digest == MODULE.BOOTSTRAP_MANIFEST_SHA256
assert len(packet["records"]) == 24
assert {record["id"] for record in packet["records"]} == {
    record["id"] for record in json.loads(PACKET_PATH.read_text())["records"]
}
assert {record["disposition"] for record in packet["records"]} == {"completed_one_shot", "retired", "stopped"}
assert {record["provenance"]["source"] for record in packet["records"]} == {"source-decisions.json"}
assert all(record["provenance"]["owner_verified"] is True for record in packet["records"])

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

with tempfile.TemporaryDirectory() as temporary:
    copy = Path(temporary) / "packet"
    shutil.copytree(PACKET_PATH.parent, copy)
    source_path = copy / "source-inventory.json"
    source = json.loads(source_path.read_text())
    source["records"][0]["running"] = True
    source_path.write_text(json.dumps(source, sort_keys=True, separators=(",", ":")) + "\n")
    try:
        MODULE.verify_bootstrap(copy / "lifecycle-bootstrap.json", expected_manifest=manifest_digest)
    except MODULE.RecordError:
        pass
    else:
        raise AssertionError("tampered source evidence must fail digest verification")

print("lifecycle-disposition 24-row bootstrap fixtures: PASS")
