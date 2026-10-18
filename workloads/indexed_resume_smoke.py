#!/usr/bin/env python3
"""Indexed, checkpointed simulation worker with optional restartable barriers.

The Isaac Sim adapter is not present: the loop advances a smoke-test counter.
Production adapter must checkpoint simulator state, scene/asset references, RNG
state and any policy/optimizer state required for deterministic recovery.
"""
import json
import os
import signal
import time

import boto3
from botocore.exceptions import ClientError

from checkpoint import CheckpointCommitRejected, CheckpointStore
from sync_barrier import BarrierStopped, publish_arrival, wait_for_barrier
from telemetry import Telemetry


def shard_id(job_id: str, index: int) -> str:
    if not job_id or len(job_id) > 40 or not all(c.isalnum() or c == "-" for c in job_id):
        raise ValueError("invalid job id")
    if index < 0 or index > 99999:
        raise ValueError("invalid shard index")
    return f"{job_id}-shard-{index:05d}"


def claim_shard(table, shard: str, shard_count: int | None = None) -> int:
    """Fence older Pods, and prohibit reusing a job ID with another shard count."""
    values = {":one": 1, ":running": "RUNNING", ":false": False}
    update = "ADD fencing_token :one SET #status = :running, resume_required = :false"
    options = {}
    if shard_count is not None:
        update += ", expected_shards = :total"
        values[":total"] = shard_count
        options["ConditionExpression"] = (
            "attribute_not_exists(expected_shards) OR expected_shards = :total"
        )
    response = table.update_item(
        Key={"PK": f"JOB#{shard}", "SK": "META"},
        UpdateExpression=update,
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=values,
        ReturnValues="UPDATED_NEW",
        **options,
    )
    return int(response["Attributes"]["fencing_token"])


def complete_shard(table, shard: str, token: int):
    try:
        table.update_item(
            Key={"PK": f"JOB#{shard}", "SK": "META"},
            UpdateExpression="SET #status = :done, resume_required = :false",
            ConditionExpression="fencing_token = :token",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":token": token, ":done": "COMPLETED", ":false": False
            },
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise CheckpointCommitRejected("newer shard worker owns the lease") from error
        raise


def run():
    region = os.environ["AWS_REGION"]
    project = os.environ.get("PLATFORM_PROJECT", "robot-sim")
    environment = os.environ.get("PLATFORM_ENVIRONMENT", "dev")
    parent_job_id = os.environ["JOB_ID"]
    index = int(os.environ["JOB_COMPLETION_INDEX"])
    shard_count = int(os.environ.get("SHARD_COUNT", "1"))
    shard = shard_id(parent_job_id, index)
    target = int(os.environ.get("TARGET_STEPS", "120"))
    step_seconds = float(os.environ.get("STEP_SECONDS", "1"))
    interval = int(os.environ.get("CHECKPOINT_INTERVAL_SECONDS", "600"))
    sync_enabled = os.environ.get("SYNC_ENABLED", "false").lower() == "true"
    sync_steps = int(os.environ.get("SYNC_EVERY_STEPS", "10"))
    sync_poll = float(os.environ.get("SYNC_POLL_SECONDS", "5"))
    sync_timeout = float(os.environ.get("SYNC_TIMEOUT_SECONDS", "1800"))
    digest = os.environ["IMAGE_DIGEST"]
    revision = os.environ.get("GIT_REVISION", "unknown")
    if target <= 0 or interval <= 0 or step_seconds < 0:
        raise ValueError("target, interval and step seconds must be valid")
    if shard_count < 1 or not (0 <= index < shard_count):
        raise ValueError("invalid shard count or index")
    if sync_enabled and (sync_steps <= 0 or sync_poll <= 0 or sync_timeout <= 0):
        raise ValueError("invalid synchronization settings")

    telemetry = Telemetry("indexed-resume-smoke-v2")
    prefix = f"/{project}/{environment}"
    ssm = boto3.client("ssm", region_name=region)

    def parameter(suffix):
        return ssm.get_parameter(Name=f"{prefix}/{suffix}")["Parameter"]["Value"]

    if parameter("runtime/desired-state") != "running":
        raise RuntimeError("runtime must be running before starting a new shard")

    ddb = boto3.resource("dynamodb", region_name=region)
    table_name = parameter("state/control-table")
    table = ddb.Table(table_name)
    store = CheckpointStore(
        s3_client=boto3.client("s3", region_name=region),
        dynamodb_resource=ddb,
        bucket=parameter("storage/research-bucket"),
        table=table_name,
    )

    token = claim_shard(table, shard, shard_count)
    manifest = store.committed_manifest(shard)
    restored = store.restore_bytes(shard) if manifest else {}
    progress = int(json.loads(restored.get("progress.json", b'{"steps":0}'))["steps"])
    sequence = manifest.sequence if manifest else 0
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    telemetry.event("shard_resumed", job_id=parent_job_id, shard=index, steps=progress, sequence=sequence, fencing_token=token, synchronized=sync_enabled)

    last_checkpoint = time.monotonic()

    def commit(*, suspend=False):
        nonlocal sequence, last_checkpoint
        next_seq = sequence + 1
        payload = json.dumps({"steps": progress}).encode()
        with telemetry.timed("checkpoint_seconds"):
            store.commit_bytes(
                job_id=shard, sequence=next_seq, fencing_token=token,
                payloads={"progress.json": payload},
                image_digest=digest, git_revision=revision,
                workload_version="indexed-resume-smoke-v2",
                metadata={"parent_job_id": parent_job_id, "shard": index,
                          "sync_every_steps": sync_steps if sync_enabled else 0,
                          "usage": telemetry.snapshot()},
                suspend=suspend,
            )
        sequence = next_seq
        telemetry.add("checkpoints")
        telemetry.add("checkpoint_bytes", len(payload))
        last_checkpoint = time.monotonic()
        telemetry.event("checkpoint", shard=index, steps=progress, sequence=sequence, suspended=suspend)

    def should_stop():
        return stopping or parameter("runtime/desired-state") != "running"

    def barrier():
        """Release a round only when all shards have committed that round."""
        round_id = progress // sync_steps
        publish_arrival(table, shard, token, sequence, round_id)
        telemetry.event("barrier_arrived", shard=index, round=round_id)
        with telemetry.timed("barrier_wait_seconds"):
            wait_for_barrier(
                table, parent_job=parent_job_id, shards=shard_count,
                round_id=round_id, own_index=index, token=token,
                timeout_seconds=sync_timeout, poll_seconds=sync_poll,
                should_stop=should_stop,
            )
        telemetry.event("barrier_released", shard=index, round=round_id)

    try:
        # If an interrupted worker previously committed the boundary step but
        # died at the barrier, the replacement must re-enter that same round.
        if sync_enabled and progress > 0 and progress % sync_steps == 0:
            try:
                barrier()
            except BarrierStopped:
                stopping = True

        while progress < target and not stopping:
            if should_stop():
                stopping = True
                break
            time.sleep(step_seconds)
            progress += 1
            telemetry.add("steps")
            # Isaac Sim adapter: advance simulator and append episode samples.
            stopping = should_stop()
            boundary = sync_enabled and progress % sync_steps == 0
            due = time.monotonic() - last_checkpoint >= interval
            if boundary or due or stopping:
                commit(suspend=stopping)
            if boundary and not stopping:
                try:
                    barrier()
                except BarrierStopped:
                    stopping = True

        if stopping:
            # Even if the last progress update was checkpointed as RUNNING,
            # transition the shard to SUSPENDED for safe shutdown.
            commit(suspend=True)
            telemetry.event("suspended", shard=index, steps=progress)
            telemetry.usage("suspended", shard=index, steps=progress)
            # An aborted Indexed Job needs a replacement Job to resume.
            raise SystemExit(1)

        # Publish a final durable checkpoint, even after a round checkpoint.
        commit()
        complete_shard(table, shard, token)
        telemetry.event("completed", shard=index, steps=progress)
        telemetry.usage("completed", shard=index, steps=progress)
    except CheckpointCommitRejected:
        telemetry.event("fenced", shard=index)
        telemetry.usage("fenced", shard=index, steps=progress)
        raise SystemExit(1) from None


if __name__ == "__main__":
    run()
