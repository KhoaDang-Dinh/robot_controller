#!/usr/bin/env python3
"""Fail if any tracked file references a container registry other than the shared one.

config/images.json names the single GHCR namespace used by robot_controller and
infra_robot_sim. Both repositories run this same check in CI, and the runtime
smoke test compares the value against the Terraform-published SSM contract, so
a drift in either repository fails before an image is pushed or pulled.

Rules:
  * every ghcr.io reference must be ``<registry>/<image>`` with a known image;
  * GHCR names must be lowercase, so ``${{ github.repository_owner }}`` (which
    preserves the owner's mixed case) must not be used to build image names;
  * the robot-sim chart's allowedRegistry must equal the registry.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
GHCR = re.compile(r"ghcr\.io/[^\s\"'`@:,)}\\]*")
DYNAMIC_OWNER = re.compile(r"ghcr\.io/\$\{\{\s*github\.repository_owner\s*\}\}")
SKIP_SUFFIXES = {".png", ".jpg", ".lock"}
# Mentions of the bare host (docker login ghcr.io, --docker-server=ghcr.io) are fine.
BARE_HOST = {"ghcr.io", "ghcr.io/"}


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z"], text=True)
        names = [n for n in out.split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        names = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]
    return [root / n for n in names]


def check(root: Path = ROOT) -> list[str]:
    config = json.loads((root / "config/images.json").read_text())
    registry = config["registry"].rstrip("/")
    images = set(config["images"])
    problems: list[str] = []
    if registry != registry.lower():
        problems.append(f"config/images.json: registry must be lowercase: {registry}")

    for path in tracked_files(root):
        if path.suffix in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(root)
        # The checker and its own fixtures intentionally contain bad references.
        if rel == Path("scripts/check_registry.py") or rel.parts[0] == "tests":
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if DYNAMIC_OWNER.search(line):
                problems.append(f"{rel}:{number}: use the lowercase registry from config/images.json, "
                                "not github.repository_owner (GHCR rejects uppercase names)")
                continue
            for match in GHCR.findall(line):
                if match in BARE_HOST:
                    continue
                if match == registry:
                    continue
                if not match.startswith(registry + "/"):
                    problems.append(f"{rel}:{number}: {match} is outside shared registry {registry}")
                    continue
                name = match[len(registry) + 1:].split("/")[0]
                if name and "$" not in name and name not in images:
                    problems.append(f"{rel}:{number}: image {name!r} is not listed in config/images.json")

    values = root / "charts/robot-sim/values.yaml"
    if values.exists():
        allowed = re.search(r"^\s*allowedRegistry:\s*(\S+)", values.read_text(), re.M)
        if not allowed or allowed.group(1).rstrip("/") != registry:
            problems.append(f"{values.relative_to(root)}: image.allowedRegistry must be {registry}")
    return problems


def main() -> int:
    problems = check()
    if problems:
        print("Container registry drift:")
        for problem in problems:
            print(f" - {problem}")
        return 1
    registry = json.loads((ROOT / "config/images.json").read_text())["registry"]
    print(f"OK: every image reference uses {registry}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
