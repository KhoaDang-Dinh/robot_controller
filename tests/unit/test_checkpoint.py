import boto3
import pytest
from moto import mock_aws

from robot_controller.core.checkpoint import CheckpointCommitRejected, CheckpointCorrupt, CheckpointStore


@pytest.fixture()
def checkpoint_env():
    with mock_aws():
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
        table.put_item(
            Item={
                "PK": "JOB#job-1",
                "SK": "META",
                "status": "RUNNING",
                "fencing_token": 7,
            }
        )

        yield CheckpointStore(
            s3_client=s3,
            dynamodb_resource=ddb,
            bucket="robot-checkpoints",
            table="robot-control",
        ), table, s3


def test_committed_checkpoint_round_trip(checkpoint_env):
    store, table, _ = checkpoint_env

    store.commit_bytes(
        job_id="job-1",
        sequence=1,
        fencing_token=7,
        payloads={
            "robot-state.json": b'{"joint": 1.25}',
            "simulation.bin": b"sim-state",
        },
        image_digest="sha256:" + "a" * 64,
        git_revision="abc123",
        workload_version="isaac-smoke-v1",
        suspend=True,
    )

    item = table.get_item(Key={"PK": "JOB#job-1", "SK": "META"})["Item"]
    assert item["checkpoint_seq"] == 1
    assert item["status"] == "SUSPENDED"
    assert item["resume_required"] is True

    restored = store.restore_bytes("job-1")
    assert restored["robot-state.json"] == b'{"joint": 1.25}'
    assert restored["simulation.bin"] == b"sim-state"


def test_stale_worker_cannot_commit(checkpoint_env):
    store, _, _ = checkpoint_env

    with pytest.raises(CheckpointCommitRejected):
        store.commit_bytes(
            job_id="job-1",
            sequence=1,
            fencing_token=6,
            payloads={"state.bin": b"stale"},
            image_digest="sha256:" + "b" * 64,
            git_revision="old",
            workload_version="v1",
        )


def test_old_sequence_cannot_replace_newer_checkpoint(checkpoint_env):
    store, _, _ = checkpoint_env

    store.commit_bytes(
        job_id="job-1",
        sequence=2,
        fencing_token=7,
        payloads={"state.bin": b"new"},
        image_digest="sha256:" + "c" * 64,
        git_revision="new",
        workload_version="v1",
    )

    with pytest.raises(CheckpointCommitRejected):
        store.commit_bytes(
            job_id="job-1",
            sequence=1,
            fencing_token=7,
            payloads={"state.bin": b"old"},
            image_digest="sha256:" + "c" * 64,
            git_revision="old",
            workload_version="v1",
        )


def test_corrupt_payload_is_rejected(checkpoint_env):
    store, _, s3 = checkpoint_env

    manifest = store.commit_bytes(
        job_id="job-1",
        sequence=1,
        fencing_token=7,
        payloads={"state.bin": b"good"},
        image_digest="sha256:" + "d" * 64,
        git_revision="abc",
        workload_version="v1",
    )
    key = manifest.files[0].key
    s3.put_object(Bucket="robot-checkpoints", Key=key, Body=b"corrupt")

    with pytest.raises(CheckpointCorrupt):
        store.restore_bytes("job-1")


def test_fenced_attempt_cannot_overwrite_committed_checkpoint(checkpoint_env):
    store, table, s3 = checkpoint_env
    original = store.commit_bytes(
        job_id="job-1", sequence=1, fencing_token=7,
        payloads={"state.bin": b"safe"}, image_digest="sha256:" + "a" * 64,
        git_revision="orig", workload_version="v1",
    )
    # A restarted worker obtains token 8 and publishes sequence 2.
    table.update_item(
        Key={"PK": "JOB#job-1", "SK": "META"},
        UpdateExpression="SET fencing_token = :token",
        ExpressionAttributeValues={":token": 8},
    )
    store.commit_bytes(
        job_id="job-1", sequence=2, fencing_token=8,
        payloads={"state.bin": b"new"}, image_digest="sha256:" + "b" * 64,
        git_revision="new", workload_version="v1",
    )
    assert original.files[0].key != store.committed_manifest("job-1").files[0].key
    with pytest.raises(CheckpointCommitRejected):
        store.commit_bytes(
            job_id="job-1", sequence=2, fencing_token=7,
            payloads={"state.bin": b"stale"}, image_digest="sha256:" + "a" * 64,
            git_revision="old", workload_version="v1",
        )
    assert store.restore_bytes("job-1")["state.bin"] == b"new"


def test_legacy_checkpoint_prefix_can_still_restore(checkpoint_env):
    store, table, s3 = checkpoint_env
    # Compatibility with pre-multi-pod manifest pointers in existing S3.
    old_prefix = store.prefix("job-1", 1)
    old_key = f"{old_prefix}/data/state.bin"
    body = b"legacy"
    from hashlib import sha256
    from json import dumps
    s3.put_object(Bucket="robot-checkpoints", Key=old_key, Body=body)
    manifest = {
        "schema_version": 1, "job_id": "job-1", "sequence": 1,
        "fencing_token": 7, "created_at": "2026-10-17T00:00:00Z",
        "image_digest": "sha256:" + "a"*64, "git_revision": "old",
        "workload_version": "v1", "metadata": {}, "state": "COMPLETE",
        "files": [{"key": old_key, "size": len(body), "sha256": sha256(body).hexdigest()}],
    }
    manifest_body = dumps(manifest).encode()
    manifest_key = f"{old_prefix}/manifest.json"
    s3.put_object(Bucket="robot-checkpoints", Key=manifest_key, Body=manifest_body)
    table.update_item(
        Key={"PK": "JOB#job-1", "SK": "META"},
        UpdateExpression="SET checkpoint_manifest_key = :key, checkpoint_manifest_sha256 = :hash",
        ExpressionAttributeValues={":key": manifest_key, ":hash": sha256(manifest_body).hexdigest()},
    )
    assert store.restore_bytes("job-1")["state.bin"] == b"legacy"
