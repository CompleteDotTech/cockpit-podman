#!/usr/bin/env python3
"""Verify and install the protected lifecycle bootstrap packet.

The default action is a read-only verification.  ``--install`` requires root,
installs into a temporary directory below the destination, sets mode 0644 and
root ownership, verifies the result, and then atomically replaces the packet
files.  It never calls Podman or systemd.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any


MANIFEST_SHA256 = "a7d4c0ac5e3f0a0040ee00ca485b394be6836f8ff170970824cb82f1c183c286"
SOURCE_LEDGER_SHA256 = "b58d2c2b95f46efc3cbd4fdf3b2c62a0024190c091d668e0a6f754d553ee4f81"
EXPECTED_FILES = {"lifecycle-bootstrap.json", "manifest.json", "source-inventory.json",
                  "source-display.json", "source-ownership.json", "source-decisions.json"}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def verify(source: Path, expected_manifest: str = MANIFEST_SHA256) -> list[str]:
    manifest_path = source / "manifest.json"
    manifest = load(manifest_path)
    actual_manifest = sha256(manifest_path)
    if actual_manifest != expected_manifest:
        raise ValueError("bootstrap manifest digest does not match the reviewed packet")
    if manifest.get("schema") != "cockpit-podman-lifecycle-manifest/v1":
        raise ValueError("unsupported lifecycle manifest")
    listed = manifest.get("source_files")
    names = {item.get("path") for item in listed or [] if isinstance(item, dict)}
    if names != EXPECTED_FILES - {"manifest.json"}:
        raise ValueError("manifest does not enumerate the protected packet files")
    if manifest.get("record_count") != 24 or manifest.get("counts") != {
        "completed_one_shot": 3,
        "retired": 20,
        "infrastructure_only": 0,
        "stopped": 1,
    }:
        raise ValueError("manifest does not contain the protected 24-row contract")
    if manifest.get("source_ledger_sha256") != SOURCE_LEDGER_SHA256:
        raise ValueError("manifest source ledger digest is not the reviewed capture")
    for item in listed:
        path = item.get("path")
        if not isinstance(path, str) or path not in names or Path(path).name != path:
            raise ValueError("manifest contains an unsafe path")
        candidate = source / path
        if sha256(candidate) != item.get("sha256"):
            raise ValueError(f"source digest mismatch: {path}")
    packet = load(source / "lifecycle-bootstrap.json")
    if packet.get("schema") != "cockpit-podman-lifecycle-bootstrap/v1" or packet.get("record_count") != 24:
        raise ValueError("bootstrap packet is not the protected 24-row packet")
    if len(packet.get("records", [])) != 24:
        raise ValueError("bootstrap packet row count is invalid")
    decisions = load(source / "source-decisions.json")
    if decisions.get("source_ledger", {}).get("sha256") != SOURCE_LEDGER_SHA256:
        raise ValueError("source decision ledger digest is not the reviewed capture")
    return sorted(names | {"manifest.json"})


def verify_installed(destination: Path, names: list[str]) -> None:
    for name in names:
        path = destination / name
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o644:
            raise ValueError(f"installed file mode is not 0644: {name}")
        if os.geteuid() == 0 and path.stat().st_uid != 0:
            raise ValueError(f"installed file is not root-owned: {name}")


def install(source: Path, destination: Path, names: list[str]) -> None:
    if os.geteuid() != 0:
        raise PermissionError("--install requires root for protected ownership")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.", dir=destination.parent) as temporary:
        staging = Path(temporary)
        staging.mkdir(exist_ok=True)
        for name in names:
            staged = staging / name
            shutil.copyfile(source / name, staged)
            os.chmod(staged, 0o644)
            os.chown(staged, 0, 0)
        verify_installed(staging, names)
        destination.mkdir(mode=0o755, exist_ok=True)
        for name in names:
            os.replace(staging / name, destination / name)
    verify_installed(destination, names)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).parent)
    parser.add_argument("--destination", type=Path, default=Path("/etc/cockpit-podman/lifecycle-bootstrap"))
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="verify only; this is the default")
    action.add_argument("--install", action="store_true", help="install after verification; requires root")
    args = parser.parse_args()
    names = verify(args.source)
    if args.install:
        install(args.source, args.destination, names)
        print(f"installed {len(names)} verified lifecycle bootstrap files in {args.destination}")
    else:
        print(f"verified {len(names)} lifecycle bootstrap files; no host changes made")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
