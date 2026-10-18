#!/usr/bin/env python3
"""Summarise workload usage records into per-experiment GPU time and estimated cost.

Input is the JSON-lines telemetry the workers write to stdout (for example
``kubectl logs -l app.kubernetes.io/part-of=robot-sim --prefix=false`` or a log
export from S3/CloudWatch). Only ``"event": "usage"`` records are used; other
lines, including non-JSON output, are ignored.

Prices are never assumed. Pass ``--rates`` with a JSON file of USD per GPU-hour
keyed by capacity type, e.g. ``{"spot": 0.0, "on-demand": 0.0}`` filled in from
your own AWS pricing; without it the report shows usage only. Authoritative
spend comes from AWS Cost Explorer (see infra_robot_sim scripts/cost_report.py);
this report attributes that spend to experiments, shards and wasted attempts.

    python3 scripts/usage_report.py logs.jsonl --rates rates.json --format markdown
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
from typing import Iterable

GROUP_KEYS = ("robot_sim.experiment", "robot_sim.job_id", "robot_sim.capacity_type")
WASTED = {"fenced"}


def usage_records(lines: Iterable[str]) -> list[dict]:
    records = []
    for line in lines:
        line = line.strip()
        start = line.find("{")
        if start < 0:
            continue
        try:
            record = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("event") == "usage" and isinstance(record.get("usage"), dict):
            records.append(record)
    return records


def summarise(records: list[dict], rates: dict[str, float] | None = None) -> list[dict]:
    groups: dict[tuple, dict] = defaultdict(lambda: {
        "attempts": 0, "wasted_attempts": 0, "wall_seconds": 0.0, "gpu_seconds": 0.0,
        "wasted_gpu_seconds": 0.0, "steps": 0, "checkpoints": 0, "checkpoint_seconds": 0.0,
        "barrier_wait_seconds": 0.0,
    })
    for record in records:
        resource = record.get("resource", {})
        key = tuple(str(resource.get(k, "unknown")) for k in GROUP_KEYS)
        usage = record["usage"]
        group = groups[key]
        group["attempts"] += 1
        gpu_seconds = float(usage.get("gpu_seconds", 0))
        if record.get("outcome") in WASTED:
            group["wasted_attempts"] += 1
            group["wasted_gpu_seconds"] += gpu_seconds
        group["gpu_seconds"] += gpu_seconds
        for field in ("wall_seconds", "steps", "checkpoints", "checkpoint_seconds", "barrier_wait_seconds"):
            group[field] += usage.get(field, 0)

    rows = []
    for (experiment, job_id, capacity), group in sorted(groups.items()):
        gpu_hours = group["gpu_seconds"] / 3600
        row = {
            "experiment": experiment,
            "job_id": job_id,
            "capacity_type": capacity,
            **{k: round(v, 3) if isinstance(v, float) else v for k, v in group.items()},
            "gpu_hours": round(gpu_hours, 4),
            "seconds_per_step": round(group["wall_seconds"] / group["steps"], 3) if group["steps"] else None,
        }
        if rates is not None:
            rate = rates.get(capacity)
            row["estimated_usd"] = None if rate is None else round(gpu_hours * float(rate), 4)
        rows.append(row)
    return rows


def markdown(rows: list[dict]) -> str:
    if not rows:
        return "No usage records found.\n"
    columns = ["experiment", "job_id", "capacity_type", "attempts", "wasted_attempts",
               "gpu_hours", "steps", "seconds_per_step", "checkpoint_seconds", "barrier_wait_seconds"]
    if "estimated_usd" in rows[0]:
        columns.append("estimated_usd")
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for row in rows:
        lines.append("| " + " | ".join("" if row.get(c) is None else str(row.get(c)) for c in columns) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="*", help="JSON-lines log files (default: stdin)")
    parser.add_argument("--rates", type=Path, help="JSON: USD per GPU-hour keyed by capacity type")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args(argv)

    lines: list[str] = []
    if args.logs:
        for name in args.logs:
            lines.extend(Path(name).read_text().splitlines())
    else:
        lines = sys.stdin.read().splitlines()
    rates = json.loads(args.rates.read_text()) if args.rates else None
    rows = summarise(usage_records(lines), rates)
    sys.stdout.write(json.dumps(rows, indent=2) + "\n" if args.format == "json" else markdown(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
