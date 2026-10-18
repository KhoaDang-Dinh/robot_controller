"""Load and validate experiment files (YAML).

An experiment file says *what* to run: which simulator adapter and policy, their
parameters, seeds, shard layout, checkpoint cadence and resource limits. It never
says *how* recovery works; that is the runner's job.

Validation is strict: unknown keys are errors, so a typo cannot silently fall back
to a default and waste a GPU run. ``config_sha256`` is the SHA-256 of the file's
exact bytes. The Helm chart labels Pods with the same hash, and the runner refuses
to resume a checkpoint written under a different hash unless the experiment sets
``resume.allow_config_change: true``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Any

import yaml

SCHEMA_VERSION = 1
LABEL_VALUE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,38}[a-z0-9])?$")
PLUGIN_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")


class ExperimentError(ValueError):
    """The experiment file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class Plugin:
    name: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Sync:
    every_steps: int
    poll_seconds: float = 5.0
    timeout_seconds: float = 1800.0


@dataclass(frozen=True)
class Limits:
    max_gpus: int
    gpus_per_shard: int = 1
    active_deadline_seconds: int = 0


@dataclass(frozen=True)
class Experiment:
    name: str
    adapter: Plugin
    policy: Plugin
    limits: Limits
    description: str = ""
    seeds: tuple[int, ...] = (0,)
    shards: int = 1
    parallelism: int = 1
    target_steps: int = 0  # 0 = run until stopped
    checkpoint_interval_seconds: int = 600
    sync: Sync | None = None
    allow_config_change: bool = False
    config_sha256: str = ""
    source: str = ""

    def seed_for(self, shard: int) -> int:
        if not 0 <= shard < self.shards:
            raise ExperimentError(f"shard {shard} outside 0..{self.shards - 1}")
        if len(self.seeds) == 1:
            return self.seeds[0] + shard
        return self.seeds[shard]

    def identity(self) -> dict[str, str]:
        return {"name": self.name, "config_sha256": self.config_sha256}


def _take(raw: dict, key: str, kind, default=..., where: str = "experiment"):
    if key not in raw:
        if default is ...:
            raise ExperimentError(f"{where}.{key} is required")
        return default
    value = raw.pop(key)
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if isinstance(value, bool) and bool not in kinds:
        raise ExperimentError(f"{where}.{key} must be {kinds[0].__name__}, not a boolean")
    if not isinstance(value, kinds):
        raise ExperimentError(f"{where}.{key} must be {' or '.join(k.__name__ for k in kinds)}")
    return value


def _no_extra(raw: dict, where: str):
    if raw:
        raise ExperimentError(f"unknown key(s) in {where}: {', '.join(sorted(raw))}")


def _plugin(raw: Any, where: str) -> Plugin:
    if not isinstance(raw, dict):
        raise ExperimentError(f"{where} must be a mapping with name and params")
    raw = dict(raw)
    name = _take(raw, "name", str, where=where)
    if not PLUGIN_NAME.match(name):
        raise ExperimentError(f"{where}.name must be a lowercase identifier")
    params = _take(raw, "params", dict, {}, where)
    _no_extra(raw, where)
    return Plugin(name, dict(params))


def parse(text: str, source: str = "<string>") -> Experiment:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ExperimentError(f"{source}: invalid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise ExperimentError(f"{source}: top level must be a mapping")
    raw = dict(raw)
    version = _take(raw, "schema_version", int)
    if version != SCHEMA_VERSION:
        raise ExperimentError(f"unsupported schema_version {version}; this runner reads {SCHEMA_VERSION}")
    name = _take(raw, "name", str)
    if not LABEL_VALUE.match(name):
        raise ExperimentError("name must be lowercase letters, digits and hyphens, at most 40 characters")
    description = _take(raw, "description", str, "")
    adapter = _plugin(raw.pop("adapter", None), "adapter")
    policy = _plugin(raw.pop("policy", None), "policy")
    shards = _take(raw, "shards", int, 1)
    parallelism = _take(raw, "parallelism", int, 1)
    if not 1 <= shards <= 1000:
        raise ExperimentError("shards must be between 1 and 1000")
    if not 1 <= parallelism <= shards:
        raise ExperimentError("parallelism must be between 1 and shards")
    seeds = _take(raw, "seeds", list, [0])
    if not seeds or not all(isinstance(s, int) and not isinstance(s, bool) for s in seeds):
        raise ExperimentError("seeds must be a non-empty list of integers")
    if len(seeds) not in (1, shards):
        raise ExperimentError("seeds must have one entry (shard i uses seed+i) or exactly one per shard")
    target = _take(raw, "target_steps", int, 0)
    if target < 0:
        raise ExperimentError("target_steps must be >= 0 (0 runs until stopped)")
    interval = _take(raw, "checkpoint_interval_seconds", int, 600)
    if interval < 1:
        raise ExperimentError("checkpoint_interval_seconds must be >= 1")

    sync = None
    sync_raw = raw.pop("sync", None)
    if sync_raw is not None:
        if not isinstance(sync_raw, dict):
            raise ExperimentError("sync must be a mapping or null")
        sync_raw = dict(sync_raw)
        sync = Sync(
            every_steps=_take(sync_raw, "every_steps", int, where="sync"),
            poll_seconds=float(_take(sync_raw, "poll_seconds", (int, float), 5.0, "sync")),
            timeout_seconds=float(_take(sync_raw, "timeout_seconds", (int, float), 1800.0, "sync")),
        )
        _no_extra(sync_raw, "sync")
        if sync.every_steps < 1 or sync.poll_seconds <= 0 or sync.timeout_seconds <= 0:
            raise ExperimentError("sync every_steps, poll_seconds and timeout_seconds must be positive")
        if parallelism != shards:
            raise ExperimentError("sync requires parallelism == shards (every shard must be running)")

    limits_raw = raw.pop("limits", None)
    if not isinstance(limits_raw, dict):
        raise ExperimentError("limits is required (max_gpus, gpus_per_shard, active_deadline_seconds)")
    limits_raw = dict(limits_raw)
    limits = Limits(
        max_gpus=_take(limits_raw, "max_gpus", int, where="limits"),
        gpus_per_shard=_take(limits_raw, "gpus_per_shard", int, 1, "limits"),
        active_deadline_seconds=_take(limits_raw, "active_deadline_seconds", int, 0, "limits"),
    )
    _no_extra(limits_raw, "limits")
    if limits.gpus_per_shard < 0 or limits.max_gpus < 0 or limits.active_deadline_seconds < 0:
        raise ExperimentError("limits must be >= 0")
    if parallelism * limits.gpus_per_shard > limits.max_gpus:
        raise ExperimentError(
            f"parallelism x gpus_per_shard = {parallelism * limits.gpus_per_shard} exceeds limits.max_gpus")

    resume_raw = raw.pop("resume", {}) or {}
    if not isinstance(resume_raw, dict):
        raise ExperimentError("resume must be a mapping")
    resume_raw = dict(resume_raw)
    allow_change = _take(resume_raw, "allow_config_change", bool, False, "resume")
    _no_extra(resume_raw, "resume")
    _no_extra(raw, "experiment")

    return Experiment(
        name=name, description=description, adapter=adapter, policy=policy, limits=limits,
        seeds=tuple(seeds), shards=shards, parallelism=parallelism, target_steps=target,
        checkpoint_interval_seconds=interval, sync=sync, allow_config_change=allow_change,
        config_sha256=hashlib.sha256(text.encode()).hexdigest(), source=source,
    )


def load(path: str | Path) -> Experiment:
    path = Path(path)
    return parse(path.read_text(), str(path))
