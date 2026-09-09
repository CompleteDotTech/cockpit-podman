# Lifecycle bootstrap packet

This directory is a protected, source-backed bootstrap for the 24 preserved
non-running containers in the Train baseline.  It contains the packet, four
sanitized source-evidence artifacts, and `manifest.json` with SHA-256 bindings.
The packet is deliberately static: a current Podman inventory must still
contain the same full ID in the same owner context and report a non-running
state before Cockpit displays its disposition.

Verify a source tree without changing a host:

```text
python3 packaging/lifecycle-bootstrap/install.py --check
```

The generic Cockpit package install leaves this site policy untouched. Apply
the verified JSON files to `/etc/cockpit-podman/lifecycle-bootstrap/` only
through the explicit target or installer after reviewing the Train host and
ownership evidence:

```text
sudo make install-lifecycle-bootstrap \
  LIFECYCLE_BOOTSTRAP_DESTINATION=/etc/cockpit-podman/lifecycle-bootstrap

# Equivalent direct command:
sudo python3 packaging/lifecycle-bootstrap/install.py --install \
  --destination /etc/cockpit-podman/lifecycle-bootstrap
```

The installer never calls Podman or systemd.  Rebuild the packet only from a
fresh private capture, display snapshot, and ownership evidence using
`build.py`; the builder rejects missing identities, host/context mismatches,
running rows, unsupported source classes, and count changes.
