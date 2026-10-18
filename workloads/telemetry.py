"""Structured workload telemetry and usage accounting.

Every record is one JSON object per line on stdout. stdout is the only transport:
the worker makes no network calls for telemetry, so a slow or missing collector
can never stall a simulation step or a checkpoint commit.

Field names follow OpenTelemetry semantic conventions where one exists, so a
log collector (Fluent Bit, ADOT or an OpenTelemetry Collector filelog receiver)
can map records to OTLP resource attributes without code changes. Attributes
specific to this platform use the ``robot_sim.`` namespace.

The ``usage`` record emitted when a worker exits is the input for cost
analysis: it carries wall-clock and GPU seconds per attempt, joined to the
node, capacity type (spot/on-demand), experiment and cost centre labels.
Prices are deliberately not embedded here; see scripts/usage_report.py.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, UTC
import json
import os
import sys
import time
from typing import Callable, Mapping, TextIO

SCHEMA = "robot-sim.telemetry/v1"

# Environment variable -> attribute name. Values come from the Helm chart
# (static labels) and the Kubernetes downward API (pod/node identity).
_RESOURCE_ENV = (
    ("service.name", "OTEL_SERVICE_NAME"),
    ("service.version", "GIT_REVISION"),
    ("deployment.environment.name", "PLATFORM_ENVIRONMENT"),
    ("container.image.name", "IMAGE_NAME"),
    ("container.image.digest", "IMAGE_DIGEST"),
    ("k8s.namespace.name", "POD_NAMESPACE"),
    ("k8s.pod.name", "POD_NAME"),
    ("k8s.pod.uid", "POD_UID"),
    ("k8s.node.name", "NODE_NAME"),
    ("robot_sim.project", "PLATFORM_PROJECT"),
    ("robot_sim.job_id", "JOB_ID"),
    ("robot_sim.shard", "JOB_COMPLETION_INDEX"),
    ("robot_sim.experiment", "EXPERIMENT"),
    ("robot_sim.cost_center", "COST_CENTER"),
    ("robot_sim.capacity_type", "CAPACITY_TYPE"),
)

COUNTERS = (
    "steps",
    "checkpoints",
    "checkpoint_bytes",
    "checkpoint_seconds",
    "barrier_wait_seconds",
)


def resource_attributes(env: Mapping[str, str] | None = None) -> dict:
    env = os.environ if env is None else env
    attributes = {name: env[var] for name, var in _RESOURCE_ENV if env.get(var)}
    attributes.setdefault("service.name", "robot-sim-workload")
    return attributes


def gpu_count(env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    try:
        return max(0, int(env.get("GPU_COUNT", "0")))
    except ValueError:
        return 0


class Telemetry:
    """Emit structured events and accumulate per-attempt usage counters."""

    def __init__(
        self,
        workload: str,
        *,
        stream: TextIO | None = None,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.workload = workload
        self.resource = resource_attributes(env)
        self.gpus = gpu_count(env)
        self._stream = stream
        self._clock = clock
        self._started = clock()
        self.counters: dict[str, float] = {name: 0 for name in COUNTERS}

    def event(self, name: str, **fields) -> dict:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "schema": SCHEMA,
            "event": name,
            "workload": self.workload,
            **fields,
            "resource": self.resource,
        }
        stream = self._stream or sys.stdout
        stream.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
        stream.flush()
        return record

    def add(self, counter: str, amount: float = 1) -> None:
        if counter not in self.counters:
            raise KeyError(f"unknown telemetry counter: {counter}")
        self.counters[counter] += amount

    @contextmanager
    def timed(self, counter: str):
        start = self._clock()
        try:
            yield
        finally:
            self.add(counter, self._clock() - start)

    def wall_seconds(self) -> float:
        return max(0.0, self._clock() - self._started)

    def snapshot(self) -> dict:
        """Cumulative usage for this attempt, small enough for checkpoint metadata."""
        wall = self.wall_seconds()
        return {
            "wall_seconds": round(wall, 3),
            "gpu_count": self.gpus,
            "gpu_seconds": round(wall * self.gpus, 3),
            **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in self.counters.items()},
        }

    def usage(self, outcome: str, **fields) -> dict:
        """Final per-attempt usage record; the unit of cost attribution."""
        return self.event("usage", outcome=outcome, usage=self.snapshot(), **fields)
