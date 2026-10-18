#!/usr/bin/env bash
# Install a pinned Trivy binary and verify its checksum before use.
#
# Why not aquasecurity/trivy-action? Its tags (and trivy v0.69.4 / setup-trivy
# 0.2.x) were hijacked in the March 2026 supply-chain compromise
# (CVE-2026-33634). Installing one known release by exact version and SHA-256
# keeps a moved tag or a replaced asset from running in CI.
# To upgrade: pick a release at least two weeks old, copy the Linux-64bit line
# from trivy_<version>_checksums.txt, and update both values below together.
set -euo pipefail

TRIVY_VERSION="0.75.0"
TRIVY_SHA256="c6e65abddb348e25f10549df887045629cf28cc72453cd1c63acb717316b3f3f"
DEST="${1:-${RUNNER_TEMP:-/tmp}/trivy-bin}"

archive="trivy_${TRIVY_VERSION}_Linux-64bit.tar.gz"
url="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/${archive}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl -sSfL --retry 3 -o "$work/$archive" "$url"
echo "${TRIVY_SHA256}  $work/$archive" | sha256sum --check --strict -
mkdir -p "$DEST"
tar -xzf "$work/$archive" -C "$DEST" trivy
"$DEST/trivy" --version
if [ -n "${GITHUB_PATH:-}" ]; then
  echo "$DEST" >> "$GITHUB_PATH"
fi
