"""Shard leases and the restartable barrier (ported from the PR #2 indexed worker tests)."""
import boto3
import pytest
from moto import mock_aws

from robot_controller.core import lease as module
from robot_controller.core.barrier import BarrierStopped, all_arrived, publish_arrival, wait_for_barrier
from robot_controller.core.checkpoint import CheckpointStore


def test_shard_indices_are_stable():
    assert module.shard_id("trial-001", 0) == "trial-001-shard-00000"
    assert module.shard_id("trial-001", 19) == "trial-001-shard-00019"
    with pytest.raises(ValueError):
        module.shard_id("", 0)
    with pytest.raises(ValueError):
        module.shard_id("trial", -1)


@mock_aws
def test_claim_increments_fence_and_owns_separate_shard_rows():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName="robot-control",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    first = module.shard_id("trial", 0)
    second = module.shard_id("trial", 1)
    assert module.claim_shard(table, first) == 1
    assert module.claim_shard(table, second) == 1
    assert module.claim_shard(table, first) == 2
    module.complete_shard(table, first, 2)
    item = table.get_item(Key={"PK": f"JOB#{first}", "SK": "META"})["Item"]
    assert item["status"] == "COMPLETED"
    with pytest.raises(module.CheckpointCommitRejected):
        module.complete_shard(table, first, 1)


@mock_aws
def test_spot_vm_loss_replacement_rejoins_without_restarting_survivor():
    """Survivor waits; retry of shard 1 loads its committed state and joins."""

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="robot-checkpoints")
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName="robot-control",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    store = CheckpointStore(
        s3_client=s3, dynamodb_resource=ddb,
        bucket="robot-checkpoints", table="robot-control",
    )
    shards = [module.shard_id("trial", i) for i in range(2)]
    tokens = [module.claim_shard(table, shard, 2) for shard in shards]
    store.commit_bytes(
        job_id=shards[0], sequence=1, fencing_token=tokens[0],
        payloads={"progress.json": b'{"steps":10}'},
        image_digest="sha256:" + "a"*64, git_revision="test", workload_version="v1",
    )
    publish_arrival(table, shards[0], tokens[0], 1, 1)
    assert not all_arrived(table, "trial", 2, 1, 0, tokens[0])
    # Shard 1's VM disappeared after checkpointing step 8.
    store.commit_bytes(
        job_id=shards[1], sequence=1, fencing_token=tokens[1],
        payloads={"progress.json": b'{"steps":8}'},
        image_digest="sha256:" + "a"*64, git_revision="test", workload_version="v1",
    )
    replacement_token = module.claim_shard(table, shards[1], 2)
    assert replacement_token == 2
    assert store.restore_bytes(shards[1])["progress.json"] == b'{"steps":8}'
    with pytest.raises(module.CheckpointCommitRejected):
        publish_arrival(table, shards[1], tokens[1], 1, 1)
    store.commit_bytes(
        job_id=shards[1], sequence=2, fencing_token=replacement_token,
        payloads={"progress.json": b'{"steps":10}'},
        image_digest="sha256:" + "a"*64, git_revision="test", workload_version="v1",
    )
    publish_arrival(table, shards[1], replacement_token, 2, 1)
    assert all_arrived(table, "trial", 2, 1, 0, tokens[0])
    wait_for_barrier(
        table, parent_job="trial", shards=2, round_id=1,
        own_index=0, token=tokens[0], timeout_seconds=1,
        poll_seconds=0.01, should_stop=lambda: False,
    )


@mock_aws
def test_barrier_stops_on_operator_shutdown():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName="robot-control",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    with pytest.raises(BarrierStopped):
        wait_for_barrier(
            table, parent_job="trial", shards=2, round_id=1,
            own_index=0, token=1, timeout_seconds=1,
            poll_seconds=.01, should_stop=lambda: True,
        )


@mock_aws
def test_prevents_shard_count_change_between_restarts():
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    table = ddb.create_table(
        TableName="robot-control",
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    shard = module.shard_id("trial", 0)
    module.claim_shard(table, shard, 2)
    with pytest.raises(Exception):
        module.claim_shard(table, shard, 3)
