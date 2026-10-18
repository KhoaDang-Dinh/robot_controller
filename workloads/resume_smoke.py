#!/usr/bin/env python3
"""Single-stream checkpoint/resume smoke worker.

Advances a counter, commits fenced checkpoints, and suspends cleanly when the
runtime desired-state leaves ``running`` or the Pod receives SIGTERM.
"""
import json
import os
import signal
import time

import boto3

from checkpoint import CheckpointCommitRejected, CheckpointStore
from telemetry import Telemetry


REGION = os.environ["AWS_REGION"]
PROJECT = os.environ.get("PLATFORM_PROJECT", "robot-sim")
ENVIRONMENT = os.environ.get("PLATFORM_ENVIRONMENT", "dev")
JOB_ID = os.environ["JOB_ID"]
FENCING_TOKEN = int(os.environ["FENCING_TOKEN"])
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL_SECONDS", "5"))
IMAGE_DIGEST = os.environ["IMAGE_DIGEST"]
GIT_REVISION = os.environ.get("GIT_REVISION", "unknown")
MAX_STEPS = int(os.environ.get("MAX_STEPS", "0"))  # 0 = run until suspended
PREFIX = f"/{PROJECT}/{ENVIRONMENT}"

telemetry = Telemetry("resume-smoke-v1")
ssm = boto3.client("ssm", region_name=REGION)


def parameter(name):
    return ssm.get_parameter(Name=f"{PREFIX}/{name}")["Parameter"]["Value"]


bucket = parameter("storage/research-bucket")
table_name = parameter("state/control-table")
ddb = boto3.resource("dynamodb", region_name=REGION)
table = ddb.Table(table_name)
store = CheckpointStore(
    s3_client=boto3.client("s3", region_name=REGION),
    dynamodb_resource=ddb,
    bucket=bucket,
    table=table_name,
)

stopping = False


def stop(*_):
    global stopping
    stopping = True


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)

manifest = store.committed_manifest(JOB_ID)
sequence = manifest.sequence if manifest else 0
restored = store.restore_bytes(JOB_ID) if manifest else {}
progress = json.loads(restored.get("progress.json", b'{"progress":0}'))["progress"]

table.update_item(
    Key={"PK": f"JOB#{JOB_ID}", "SK": "META"},
    UpdateExpression="SET #s = :running, resume_required = :false",
    ConditionExpression="fencing_token = :token",
    ExpressionAttributeNames={"#s": "status"},
    ExpressionAttributeValues={
        ":running": "RUNNING",
        ":false": False,
        ":token": FENCING_TOKEN,
    },
)

last_checkpoint = time.monotonic()
telemetry.event("resumed", progress=progress, sequence=sequence, fencing_token=FENCING_TOKEN)
outcome = "suspended"

while True:
    progress += 1
    telemetry.add("steps")
    time.sleep(1)

    desired = parameter("runtime/desired-state")
    reached_limit = MAX_STEPS > 0 and progress >= MAX_STEPS
    should_suspend = stopping or desired != "running" or reached_limit
    checkpoint_due = time.monotonic() - last_checkpoint >= CHECKPOINT_INTERVAL

    if checkpoint_due or should_suspend:
        sequence += 1
        payload = json.dumps({"progress": progress}).encode()
        try:
            with telemetry.timed("checkpoint_seconds"):
                store.commit_bytes(
                    job_id=JOB_ID,
                    sequence=sequence,
                    fencing_token=FENCING_TOKEN,
                    payloads={"progress.json": payload},
                    image_digest=IMAGE_DIGEST,
                    git_revision=GIT_REVISION,
                    workload_version="resume-smoke-v1",
                    metadata={"progress": progress, "usage": telemetry.snapshot()},
                    suspend=should_suspend,
                )
        except CheckpointCommitRejected:
            # A newer attempt owns the job. This process must not keep writing.
            telemetry.event("fenced", progress=progress)
            telemetry.usage("fenced", progress=progress)
            raise SystemExit(0) from None
        telemetry.add("checkpoints")
        telemetry.add("checkpoint_bytes", len(payload))
        telemetry.event("checkpoint", sequence=sequence, progress=progress, suspend=should_suspend)
        last_checkpoint = time.monotonic()

    if should_suspend:
        telemetry.event("suspended", progress=progress)
        break

telemetry.usage(outcome, progress=progress, sequence=sequence)
