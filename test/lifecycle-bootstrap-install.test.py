#!/usr/bin/env python3
"""Check that generic package installation preserves site lifecycle policy."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
SPEC = ROOT / "packaging" / "cockpit-podman.spec.in"


def recipe(makefile: str, target: str) -> str:
    match = re.search(rf"(?m)^{re.escape(target)}:.*\n((?:\t.*\n)*)", makefile)
    if not match:
        raise AssertionError(f"missing Make target: {target}")
    return match.group(1)


def main() -> int:
    makefile = MAKEFILE.read_text(encoding="utf-8")
    generic_install = recipe(makefile, "install")
    explicit_install = recipe(makefile, "install-lifecycle-bootstrap")
    if "lifecycle-bootstrap" in generic_install:
        raise AssertionError("generic install still writes the site lifecycle policy")
    if "packaging/lifecycle-bootstrap/install.py --install" not in explicit_install:
        raise AssertionError("site policy lacks an explicit installer target")
    if "lifecycle-bootstrap" in SPEC.read_text(encoding="utf-8"):
        raise AssertionError("RPM file list still owns the site lifecycle policy")

    with tempfile.TemporaryDirectory(prefix="cockpit-podman-install-check-") as temporary:
        root = Path(temporary)
        stage = root / "stage"
        (root / "dist").mkdir()
        (root / "dist" / "index.js").write_text("bundle\n", encoding="utf-8")
        (root / "po").mkdir()
        (root / "po" / "LINGUAS").write_text("\n", encoding="utf-8")
        (root / "Makefile").write_text("\n", encoding="utf-8")
        (root / "pkg" / "lib").mkdir(parents=True)
        (root / "pkg" / "lib" / "cockpit-po-plugin.js").write_text("\n", encoding="utf-8")
        (root / "package.json").write_text('{"name":"podman"}\n', encoding="utf-8")
        (root / "build.js").write_text("\n", encoding="utf-8")
        (root / "runtime-npm-modules.txt").write_text("\n", encoding="utf-8")
        (root / "meta.xml").write_text("<component/>\n", encoding="utf-8")
        policy = stage / "etc" / "cockpit-podman" / "lifecycle-bootstrap"
        policy.mkdir(parents=True)
        sentinel = policy / "manifest.json"
        sentinel.write_text("existing site policy\n", encoding="utf-8")

        fake_bin = root / "bin"
        fake_bin.mkdir()
        msgfmt = fake_bin / "msgfmt"
        msgfmt.write_text(
            "#!/bin/sh\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  if [ \"$1\" = \"-o\" ]; then shift; : > \"$1\"; fi\n"
            "  shift\n"
            "done\n",
            encoding="utf-8",
        )
        msgfmt.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
        command = [
            "make", "-f", str(MAKEFILE), "-C", str(root), "install",
            "PACKAGE_NAME=podman", "DIST_TEST=runtime-npm-modules.txt",
            "COCKPIT_REPO_STAMP=pkg/lib/cockpit-po-plugin.js",
            "APPSTREAMFILE=meta.xml", f"DESTDIR={stage}", "PREFIX=/usr",
        ]
        result = subprocess.run(command, env=environment, text=True, capture_output=True)
        if result.returncode != 0:
            raise AssertionError(f"generic install failed:\n{result.stdout}\n{result.stderr}")
        if sentinel.read_text(encoding="utf-8") != "existing site policy\n":
            raise AssertionError("generic install replaced the existing site policy")
        if sorted(path.name for path in policy.iterdir()) != ["manifest.json"]:
            raise AssertionError("generic install added site lifecycle policy files")

    print("lifecycle-bootstrap install boundary: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
