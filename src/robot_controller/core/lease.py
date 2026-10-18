"""Who owns a shard right now: fencing tokens in the DynamoDB control table.

Two ways to get a lease, both carried over unchanged from the PR #2 workers:

* claim (Indexed Jobs from the robot-sim chart): the Pod atomically increments
  the shard's fencing_token. Any older Pod for that shard is fenced out
  immediately: its next checkpoint or barrier write is rejected.
* external (infra's destroy/recreate/resume test): an operator bumps the token
  and passes it in FENCING_TOKEN; the Pod only activates the lease if the
  stored token still matches.
"""
from __future__ import annotations

from botocore.exceptions import ClientError

from robot_controller.core.checkpoint import CheckpointCommitRejected, CheckpointIncompatible


def shard_id(job_id: str, index: int) -> str:
    if not job_id or len(job_id) > 40 or not all(c.isalnum() or c == "-" for c in job_id):
        raise ValueError("invalid job id")
    if index < 0 or index > 99999:
        raise ValueError("invalid shard index")
    return f"{job_id}-shard-{index:05d}"


def _key(shard: str) -> dict:
    return {"PK": f"JOB#{shard}", "SK": "META"}


def _conditional(call, message: str):
    try:
        return call()
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise CheckpointCommitRejected(f"{message} (ConditionalCheckFailedException)") from error
        raise


def claim_shard(table, shard: str, shard_count: int | None = None) -> int:
    """Fence older Pods, and prohibit reusing a job ID with another shard count."""
    values = {":one": 1, ":running": "RUNNING", ":false": False}
    update = "ADD fencing_token :one SET #status = :running, resume_required = :false"
    options = {}
    if shard_count is not None:
        update += ", expected_shards = :total"
        values[":total"] = shard_count
        options["ConditionExpression"] = "attribute_not_exists(expected_shards) OR expected_shards = :total"
    try:
        response = table.update_item(
            Key=_key(shard), UpdateExpression=update, ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values, ReturnValues="UPDATED_NEW", **options,
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise CheckpointIncompatible(
                f"job id reused with a different shard count (expected {shard_count})") from error
        raise
    return int(response["Attributes"]["fencing_token"])


def activate_external(table, shard: str, token: int) -> None:
    """Start an operator-assigned attempt; fails if a newer token owns the shard."""
    _conditional(lambda: table.update_item(
        Key=_key(shard),
        UpdateExpression="SET #s = :running, resume_required = :false",
        ConditionExpression="fencing_token = :token",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":running": "RUNNING", ":false": False, ":token": token},
    ), "fencing token is stale")


def set_status(table, shard: str, token: int, status: str) -> None:
    """Terminal status update, only while this attempt still owns the shard."""
    _conditional(lambda: table.update_item(
        Key=_key(shard),
        UpdateExpression="SET #status = :status, resume_required = :false",
        ConditionExpression="fencing_token = :token",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":token": token, ":status": status, ":false": False},
    ), "newer shard worker owns the lease")


# Backwards-compatible name used by the PR #2 indexed worker and its tests.
def complete_shard(table, shard: str, token: int) -> None:
    set_status(table, shard, token, "COMPLETED")
