"""The one run loop: lease -> restore -> step -> checkpoint -> finish.

Every experiment goes through this loop. Adapters and policies plug in; they never
re-implement fencing, checkpoint cadence, SIGTERM/Spot handling, the shard barrier,
results or telemetry. Two PR #2 workers (resume_smoke.py and indexed_resume_smoke.py)
are now this module running the ``smoke`` adapter.

Checkpoint layout (fence-scoped prefix, see core/checkpoint.py):
    runner.json        {"steps", "seed"}
    adapter/<file>     SimAdapter.save_state()
    policy/<file>      Policy.save_state()
Manifest metadata carries ``progress`` (= steps; read by infra's recovery test),
the experiment identity (name, config_sha256, adapter/policy names and state
versions), the latest metrics and cumulative usage.

Two kinds of policy plug in:
* per-step policies (``act`` + ``learn``): the runner steps the adapter once per step;
* vectorized learners (``train_step(adapter)``, e.g. rsl_rl PPO on Isaac Lab): one
  runner "step" is one training iteration over thousands of GPU environments.

Results: when an attempt ends, a summary is written to
``s3://<research bucket>/results/<experiment>/<job id>/shard-<i>/attempt-<token>.json``,
and any ``artifacts()`` the adapter or policy provide (exported ONNX policy,
deployment spec) under ``.../attempt-<token>/``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
import signal
import time
from typing import Mapping

import boto3

from robot_controller.adapters import ADAPTERS
from robot_controller.core import lease
from robot_controller.core.barrier import BarrierStopped, publish_arrival, wait_for_barrier
from robot_controller.core.checkpoint import CheckpointCommitRejected, CheckpointIncompatible, CheckpointStore
from robot_controller.core.experiment import Experiment
from robot_controller.core.telemetry import Telemetry
from robot_controller.policies import POLICIES

COMPLETED, SUSPENDED, FENCED, INCOMPATIBLE, NOT_RUNNING = (
    "completed", "suspended", "fenced", "incompatible", "not_running")

# Exit codes. External-lease Pods (single Job, backoffLimit 0) exit 0 when they
# stop cleanly. Claimed shards of an Indexed Job exit 1 when they stop early, so
# the Job is not marked complete and a later release can resume them.
EXIT_CODES = {
    "external": {COMPLETED: 0, SUSPENDED: 0, FENCED: 0, INCOMPATIBLE: 3, NOT_RUNNING: 4},
    "claim": {COMPLETED: 0, SUSPENDED: 1, FENCED: 1, INCOMPATIBLE: 3, NOT_RUNNING: 4},
}


@dataclass(frozen=True)
class RuntimeEnv:
    """Per-Pod settings from the environment (chart, Job template or downward API)."""
    region: str
    job_id: str
    image_digest: str
    project: str = "robot-sim"
    environment: str = "dev"
    shard_index: int = 0
    fencing_token: int | None = None  # set = external lease; unset = claim
    git_revision: str = "unknown"

    @property
    def mode(self) -> str:
        return "claim" if self.fencing_token is None else "external"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> RuntimeEnv:
        env = os.environ if env is None else env
        token = env.get("FENCING_TOKEN")
        return cls(
            region=env["AWS_REGION"], job_id=env["JOB_ID"], image_digest=env["IMAGE_DIGEST"],
            project=env.get("PLATFORM_PROJECT", "robot-sim"), environment=env.get("PLATFORM_ENVIRONMENT", "dev"),
            shard_index=int(env.get("JOB_COMPLETION_INDEX", "0")),
            fencing_token=int(token) if token else None, git_revision=env.get("GIT_REVISION", "unknown"),
        )


def apply_overrides(experiment: Experiment, env: Mapping[str, str] | None = None) -> Experiment:
    """Operational knobs that may differ per run without changing the experiment's identity."""
    env = os.environ if env is None else env
    changes = {}
    if env.get("CHECKPOINT_INTERVAL_SECONDS"):
        changes["checkpoint_interval_seconds"] = int(env["CHECKPOINT_INTERVAL_SECONDS"])
    target = env.get("TARGET_STEPS") or env.get("MAX_STEPS")  # MAX_STEPS: PR #2 name
    if target:
        changes["target_steps"] = int(target)
    return replace(experiment, **changes) if changes else experiment


def identity(experiment: Experiment, adapter, policy) -> dict:
    return {
        **experiment.identity(),
        "adapter": {"name": adapter.name, "state_version": adapter.state_version},
        "policy": {"name": policy.name, "state_version": policy.state_version},
    }


def split_checkpoint(files: dict[str, bytes], metadata: dict, experiment: Experiment, adapter, policy):
    """Return (steps, adapter_files, adapter_version, policy_files, policy_version) or raise."""
    stored = metadata.get("experiment")
    if stored is None:
        # Written by the PR #2 smoke workers: a bare step counter, no identity.
        if adapter.name != "smoke":
            raise CheckpointIncompatible("legacy smoke checkpoint cannot be resumed by a non-smoke adapter")
        doc = json.loads(files.get("progress.json", b"{}"))
        steps = int(doc.get("steps", doc.get("progress", 0)))
        return steps, files, 0, {}, 0
    for kind, plugin in (("adapter", adapter), ("policy", policy)):
        if stored[kind]["name"] != plugin.name:
            raise CheckpointIncompatible(
                f"checkpoint {kind} is {stored[kind]['name']!r}, experiment uses {plugin.name!r}")
        if stored[kind]["state_version"] > plugin.state_version:
            raise CheckpointIncompatible(
                f"checkpoint {kind} state_version {stored[kind]['state_version']} is newer than this code "
                f"({plugin.state_version}); roll forward instead of back")
    if stored["config_sha256"] != experiment.config_sha256 and not experiment.allow_config_change:
        raise CheckpointIncompatible(
            "experiment file changed since this checkpoint (config_sha256 differs); "
            "restore the original file or set resume.allow_config_change: true")
    runner_state = json.loads(files["runner.json"])
    pick = lambda prefix: {k[len(prefix):]: v for k, v in files.items() if k.startswith(prefix)}  # noqa: E731
    return (int(runner_state["steps"]), pick("adapter/"), stored["adapter"]["state_version"],
            pick("policy/"), stored["policy"]["state_version"])


class Runner:
    def __init__(self, experiment: Experiment, runtime: RuntimeEnv, *, session=None,
                 telemetry: Telemetry | None = None, install_signal_handlers: bool = True):
        self.experiment = experiment
        self.rt = runtime
        if runtime.mode == "external" and experiment.shards != 1:
            raise ValueError("an external fencing token can only drive a single-shard experiment")
        self.index = runtime.shard_index
        self.seed = experiment.seed_for(self.index)
        self.shard = runtime.job_id if runtime.mode == "external" else lease.shard_id(runtime.job_id, self.index)
        self.adapter = ADAPTERS.create(experiment.adapter.name, experiment.adapter.params, seed=self.seed)
        self.policy = POLICIES.create(experiment.policy.name, experiment.policy.params, seed=self.seed)
        self.identity = identity(experiment, self.adapter, self.policy)
        env = dict(os.environ)
        env.setdefault("EXPERIMENT", experiment.name)
        env.update(CONFIG_SHA256=experiment.config_sha256, ADAPTER=self.adapter.name, POLICY=self.policy.name)
        env.setdefault("GPU_COUNT", str(experiment.limits.gpus_per_shard))
        self.telemetry = telemetry or Telemetry(f"{self.adapter.name}/{self.policy.name}", env=env)

        session = session or boto3.session.Session(region_name=runtime.region)
        self.ssm = session.client("ssm")
        self.s3 = session.client("s3")
        ddb = session.resource("dynamodb")
        prefix = f"/{runtime.project}/{runtime.environment}"
        self._parameter = lambda name: self.ssm.get_parameter(Name=f"{prefix}/{name}")["Parameter"]["Value"]
        self.bucket = self._parameter("storage/research-bucket")
        table_name = self._parameter("state/control-table")
        self.table = ddb.Table(table_name)
        self.store = CheckpointStore(s3_client=self.s3, dynamodb_resource=ddb, bucket=self.bucket, table=table_name)

        self.stopping = False
        if install_signal_handlers:
            signal.signal(signal.SIGTERM, self._stop)
            signal.signal(signal.SIGINT, self._stop)
        self.token: int | None = runtime.fencing_token
        self.steps = 0
        self.sequence = 0
        self.metrics: dict[str, float] = {}
        self._last_commit = (0, None)  # (steps, suspended) of the latest commit

    def _stop(self, *_):
        self.stopping = True

    def should_stop(self) -> bool:
        return self.stopping or self._parameter("runtime/desired-state") != "running"

    # -- checkpoint / results -------------------------------------------------
    def commit(self, *, suspend: bool = False) -> None:
        files = {"runner.json": json.dumps({"steps": self.steps, "seed": self.seed}).encode()}
        files.update({f"adapter/{k}": v for k, v in self.adapter.save_state().items()})
        files.update({f"policy/{k}": v for k, v in self.policy.save_state().items()})
        sequence = self.sequence + 1
        with self.telemetry.timed("checkpoint_seconds"):
            self.store.commit_bytes(
                job_id=self.shard, sequence=sequence, fencing_token=self.token, payloads=files,
                image_digest=self.rt.image_digest, git_revision=self.rt.git_revision,
                workload_version=f"{self.adapter.name}@{self.adapter.state_version}/"
                                 f"{self.policy.name}@{self.policy.state_version}",
                metadata={"progress": self.steps, "steps": self.steps, "experiment": self.identity,
                          "shard": self.index, "parent_job_id": self.rt.job_id, "metrics": self.metrics,
                          "usage": self.telemetry.snapshot()},
                suspend=suspend,
            )
        self.sequence = sequence
        self._last_commit = (self.steps, suspend)
        self.telemetry.add("checkpoints")
        self.telemetry.add("checkpoint_bytes", sum(len(v) for v in files.values()))
        self.telemetry.event("checkpoint", sequence=sequence, progress=self.steps, suspend=suspend,
                             shard=self.index, metrics=self.metrics)

    def upload_artifacts(self, prefix: str) -> list[str]:
        """Exported models and deployment specs from plug-ins that provide ``artifacts()``."""
        keys = []
        for plugin in (self.adapter, self.policy):
            produce = getattr(plugin, "artifacts", None)
            if not callable(produce):
                continue
            try:
                files = produce()
            except Exception as error:  # noqa: BLE001 - an export failure must not lose the run
                self.telemetry.event("artifact_export_failed", plugin=plugin.name, error=repr(error)[:300])
                continue
            for name, body in sorted(files.items()):
                if name.startswith("/") or ".." in name.split("/"):
                    raise ValueError(f"unsafe artifact name {name!r}")
                key = f"{prefix}/{name}"
                self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)
                keys.append(key)
        return keys

    def write_results(self, outcome: str, **extra) -> None:
        stem = (f"results/{self.experiment.name}/{self.rt.job_id}/shard-{self.index:05d}/"
                f"attempt-{(self.token or 0):06d}")
        key = f"{stem}.json"
        if outcome in (COMPLETED, SUSPENDED):
            try:
                extra["artifacts"] = self.upload_artifacts(stem)
            except Exception as error:  # noqa: BLE001
                self.telemetry.event("artifact_upload_failed", error=type(error).__name__)
        body = {"outcome": outcome, "experiment": self.identity, "job_id": self.rt.job_id, "shard": self.index,
                "seed": self.seed, "fencing_token": self.token, "steps": self.steps, "sequence": self.sequence,
                "metrics": self.metrics, "usage": self.telemetry.snapshot(), "image_digest": self.rt.image_digest,
                "git_revision": self.rt.git_revision,
                "finished_at": datetime.now(timezone.utc).isoformat(), **extra}  # noqa: UP017
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(body, indent=2).encode(),
                               ContentType="application/json")
            self.telemetry.event("results_written", key=key)
        except Exception as error:  # noqa: BLE001 - checkpoints stay authoritative
            self.telemetry.event("results_write_failed", key=key, error=type(error).__name__)

    # -- lifecycle -------------------------------------------------------------
    def restore(self) -> None:
        manifest = self.store.committed_manifest(self.shard)
        if manifest is None:
            self.adapter.reset(self.seed)
            return
        files = self.store.restore_bytes(self.shard)
        steps, adapter_files, adapter_v, policy_files, policy_v = split_checkpoint(
            files, manifest.metadata, self.experiment, self.adapter, self.policy)
        self.adapter.load_state(adapter_files, adapter_v)
        self.policy.load_state(policy_files, policy_v)
        self.steps, self.sequence = steps, manifest.sequence
        self.metrics = dict(manifest.metadata.get("metrics", {}))

    def barrier(self) -> None:
        sync = self.experiment.sync
        round_id = self.steps // sync.every_steps
        publish_arrival(self.table, self.shard, self.token, self.sequence, round_id)
        self.telemetry.event("barrier_arrived", shard=self.index, round=round_id)
        with self.telemetry.timed("barrier_wait_seconds"):
            wait_for_barrier(self.table, parent_job=self.rt.job_id, shards=self.experiment.shards,
                             round_id=round_id, own_index=self.index, token=self.token,
                             timeout_seconds=sync.timeout_seconds, poll_seconds=sync.poll_seconds,
                             should_stop=self.should_stop)
        self.telemetry.event("barrier_released", shard=self.index, round=round_id)

    def run(self) -> str:
        try:
            return self._run()
        finally:
            self.adapter.close()

    def _finish(self, outcome: str, **fields) -> str:
        self.telemetry.event(outcome, progress=self.steps, shard=self.index, **fields)
        self.write_results(outcome, **fields)
        self.telemetry.usage(outcome, progress=self.steps, sequence=self.sequence, shard=self.index)
        return outcome

    def _run(self) -> str:
        exp = self.experiment
        if self._parameter("runtime/desired-state") != "running":
            self.telemetry.event(NOT_RUNNING, shard=self.index)
            return NOT_RUNNING
        try:
            try:
                if self.rt.mode == "claim":
                    self.token = lease.claim_shard(self.table, self.shard, exp.shards)
                self.restore()
            except CheckpointIncompatible as error:
                if self.rt.mode == "claim" and self.token is not None:
                    lease.set_status(self.table, self.shard, self.token, "FAILED")
                self.telemetry.event("incompatible_checkpoint", reason=str(error), shard=self.index)
                return INCOMPATIBLE
            if self.rt.mode == "external":
                lease.activate_external(self.table, self.shard, self.token)
        except CheckpointCommitRejected as error:
            self.telemetry.event("fenced_at_start", reason=str(error), shard=self.index)
            return FENCED

        self.telemetry.event("resumed", progress=self.steps, sequence=self.sequence, fencing_token=self.token,
                             shard=self.index, seed=self.seed, experiment=exp.name,
                             adapter=self.adapter.name, policy=self.policy.name)
        last_checkpoint = time.monotonic()
        sync_every = exp.sync.every_steps if exp.sync else 0
        vectorized = callable(getattr(self.policy, "train_step", None))
        observation = None if vectorized else self.adapter.observation()
        try:
            # A replacement that resumes exactly on a barrier boundary re-enters that round.
            if sync_every and self.steps > 0 and self.steps % sync_every == 0:
                try:
                    self.barrier()
                except BarrierStopped:
                    self.stopping = True

            while not self.stopping and (exp.target_steps == 0 or self.steps < exp.target_steps):
                if self.should_stop():
                    self.stopping = True
                    break
                if vectorized:
                    self.metrics.update(self.policy.train_step(self.adapter))
                else:
                    action = self.policy.act(observation)
                    result = self.adapter.step(action)
                    self.metrics.update(self.policy.learn(observation, action, result))
                    observation = result.observation
                self.steps += 1
                self.telemetry.add("steps")
                self.stopping = self.should_stop()
                boundary = bool(sync_every) and self.steps % sync_every == 0
                if boundary or self.stopping or time.monotonic() - last_checkpoint >= exp.checkpoint_interval_seconds:
                    self.commit(suspend=self.stopping)
                    last_checkpoint = time.monotonic()
                if boundary and not self.stopping:
                    try:
                        self.barrier()
                    except BarrierStopped:
                        self.stopping = True

            if self.stopping:
                if self._last_commit != (self.steps, True):
                    self.commit(suspend=True)
                return self._finish(SUSPENDED)
            self.commit()
            lease.set_status(self.table, self.shard, self.token, "COMPLETED")
            return self._finish(COMPLETED)
        except CheckpointCommitRejected:
            # A newer attempt owns this shard; never write again.
            return self._finish(FENCED)


def exit_code(outcome: str, mode: str) -> int:
    return EXIT_CODES[mode][outcome]
