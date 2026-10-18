"""``robot-run``: run or validate experiments.

    robot-run run [--experiment FILE]      # default: $EXPERIMENT_FILE
    robot-run validate experiments/*.yaml  # schema + plugin + param checks, no AWS
    robot-run plugins                      # list registered adapters and policies
"""
from __future__ import annotations

import argparse
import os
import sys

from robot_controller.adapters import ADAPTERS
from robot_controller.core import experiment as experiments
from robot_controller.policies import POLICIES


def validate(paths: list[str]) -> int:
    failures = 0
    for path in paths:
        try:
            exp = experiments.load(path)
            # Instantiating catches unknown plugins and bad params before any GPU is booked.
            for shard in range(exp.shards):
                ADAPTERS.create(exp.adapter.name, exp.adapter.params, seed=exp.seed_for(shard)).close()
                POLICIES.create(exp.policy.name, exp.policy.params, seed=exp.seed_for(shard))
            print(f"ok   {path}: {exp.name} ({exp.adapter.name}/{exp.policy.name}, {exp.shards} shard(s)) "
                  f"sha256={exp.config_sha256[:16]}")
        except (experiments.ExperimentError, KeyError, ValueError, OSError) as error:
            failures += 1
            print(f"FAIL {path}: {error}", file=sys.stderr)
    return 1 if failures else 0


def run(path: str | None) -> int:
    from robot_controller.core.runner import Runner, RuntimeEnv, apply_overrides, exit_code

    path = path or os.environ.get("EXPERIMENT_FILE")
    if not path:
        print("no experiment: pass --experiment or set EXPERIMENT_FILE", file=sys.stderr)
        return 2
    exp = apply_overrides(experiments.load(path))
    runtime = RuntimeEnv.from_env()
    outcome = Runner(exp, runtime).run()
    return exit_code(outcome, runtime.mode)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="robot-run", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="run one shard of an experiment")
    run_parser.add_argument("--experiment", help="experiment YAML (default: $EXPERIMENT_FILE)")
    validate_parser = sub.add_parser("validate", help="validate experiment files")
    validate_parser.add_argument("paths", nargs="+")
    sub.add_parser("plugins", help="list adapters and policies")
    args = parser.parse_args(argv)

    if args.command == "validate":
        return validate(args.paths)
    if args.command == "plugins":
        print("adapters:", ", ".join(ADAPTERS.names()))
        print("policies:", ", ".join(POLICIES.names()))
        return 0
    return run(args.experiment)


if __name__ == "__main__":
    sys.exit(main())
