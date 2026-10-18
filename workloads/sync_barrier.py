"""Durable, restartable logical-step barrier for Indexed simulation Jobs.

This is *not* gradient all-reduce. Every participant must have GPU capacity:
a barrier with fewer scheduled Pods than total shards will deadlock.
"""
import time

from botocore.exceptions import ClientError
from checkpoint import CheckpointCommitRejected


class BarrierStopped(Exception):
    """Operator requested stop while the worker was waiting at the barrier."""


class BarrierTimeout(RuntimeError):
    """A shard was not recovered in the configured wait window."""


def publish_arrival(table, shard: str, token: int, checkpoint_seq: int, round_id: int):
    """Only a shard owner with a committed checkpoint may announce arrival."""
    try:
        table.update_item(
            Key={"PK": f"JOB#{shard}", "SK": "META"},
            UpdateExpression="SET barrier_round = :round",
            ConditionExpression=(
                "fencing_token = :token AND checkpoint_seq = :seq AND "
                "(attribute_not_exists(barrier_round) OR barrier_round <= :round)"
            ),
            ExpressionAttributeValues={
                ":token": token, ":seq": checkpoint_seq, ":round": round_id,
            },
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise CheckpointCommitRejected("stale or uncommitted barrier participant") from error
        raise


def all_arrived(table, parent_job: str, shards: int, round_id: int, own_index: int, token: int) -> bool:
    """Consistent reads; reject an old Pod that lost the fencing token."""
    pending = False
    for index in range(shards):
        shard = f"{parent_job}-shard-{index:05d}"
        item = table.get_item(
            Key={"PK": f"JOB#{shard}", "SK": "META"}, ConsistentRead=True,
        ).get("Item", {})
        if index == own_index and item.get("fencing_token") != token:
            raise CheckpointCommitRejected("replaced by a newer Pod")
        if item.get("barrier_round", 0) < round_id or "checkpoint_manifest_key" not in item:
            pending = True
    return not pending


def wait_for_barrier(
    table, *, parent_job: str, shards: int, round_id: int,
    own_index: int, token: int, timeout_seconds: float,
    poll_seconds: float, should_stop,
):
    if timeout_seconds <= 0 or poll_seconds <= 0:
        raise ValueError("barrier timeout and poll period must be positive")
    deadline = time.monotonic() + timeout_seconds
    while True:
        if should_stop():
            raise BarrierStopped()
        if all_arrived(table, parent_job, shards, round_id, own_index, token):
            return
        if time.monotonic() >= deadline:
            raise BarrierTimeout(f"round {round_id} timed out waiting for a recovered shard")
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
