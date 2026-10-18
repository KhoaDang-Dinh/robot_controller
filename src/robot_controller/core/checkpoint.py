from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, UTC
from hashlib import sha256
from json import dumps, loads
from pathlib import Path
from typing import Mapping

from botocore.exceptions import ClientError


class CheckpointCommitRejected(RuntimeError):
    """The checkpoint was produced by a stale worker or lost a commit race."""


class CheckpointCorrupt(RuntimeError):
    """A committed checkpoint failed manifest or payload verification."""


class CheckpointIncompatible(RuntimeError):
    """A valid checkpoint was written by a different experiment, adapter or policy
    (or an unsupported state version) and cannot be resumed by this code."""


@dataclass(frozen=True)
class CheckpointFile:
    key: str
    size: int
    sha256: str


@dataclass(frozen=True)
class CheckpointManifest:
    schema_version: int
    job_id: str
    sequence: int
    fencing_token: int
    created_at: str
    image_digest: str
    git_revision: str
    workload_version: str
    files: list[CheckpointFile]
    metadata: dict


class CheckpointStore:
    """Durable checkpoint protocol.

    S3 payloads and a COMPLETE manifest are written first. Only after every
    object exists does DynamoDB atomically advance the job's committed
    checkpoint pointer. Recovery trusts DynamoDB, never a partially uploaded
    S3 prefix.
    """

    def __init__(self, *, s3_client, dynamodb_resource, bucket: str, table: str):
        self.s3 = s3_client
        self.table = dynamodb_resource.Table(table)
        self.bucket = bucket

    @staticmethod
    def prefix(job_id: str, sequence: int, fencing_token: int | None = None) -> str:
        # Fence-specific object keys prevent a delayed stale worker from
        # overwriting committed S3 objects belonging to another attempt.
        if fencing_token is None:
            return f"jobs/{job_id}/checkpoints/{sequence:06d}"
        return f"jobs/{job_id}/attempts/{fencing_token}/checkpoints/{sequence:06d}"

    def commit_bytes(
        self,
        *,
        job_id: str,
        sequence: int,
        fencing_token: int,
        payloads: Mapping[str, bytes],
        image_digest: str,
        git_revision: str,
        workload_version: str,
        metadata: dict | None = None,
        suspend: bool = False,
    ) -> CheckpointManifest:
        if sequence < 1:
            raise ValueError("checkpoint sequence must be >= 1")
        if not payloads:
            raise ValueError("checkpoint must contain at least one payload")

        prefix = self.prefix(job_id, sequence, fencing_token)
        files: list[CheckpointFile] = []

        for relative_name, body in sorted(payloads.items()):
            if relative_name.startswith("/") or ".." in Path(relative_name).parts:
                raise ValueError(f"unsafe checkpoint path: {relative_name}")
            digest = sha256(body).hexdigest()
            key = f"{prefix}/data/{relative_name}"
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)
            files.append(CheckpointFile(key=key, size=len(body), sha256=digest))

        manifest = CheckpointManifest(
            schema_version=1,
            job_id=job_id,
            sequence=sequence,
            fencing_token=fencing_token,
            created_at=datetime.now(UTC).isoformat(),
            image_digest=image_digest,
            git_revision=git_revision,
            workload_version=workload_version,
            files=files,
            metadata=metadata or {},
        )
        manifest_body = dumps(
            {
                **asdict(manifest),
                "state": "COMPLETE",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        manifest_sha = sha256(manifest_body).hexdigest()
        manifest_key = f"{prefix}/manifest.json"
        self.s3.put_object(
            Bucket=self.bucket,
            Key=manifest_key,
            Body=manifest_body,
            ContentType="application/json",
        )

        values = {
            ":token": fencing_token,
            ":seq": sequence,
            ":uri": f"s3://{self.bucket}/{prefix}/",
            ":manifest_key": manifest_key,
            ":manifest_sha": manifest_sha,
            ":resume": suspend,
        }
        update = (
            "SET checkpoint_seq = :seq, checkpoint_uri = :uri, "
            "checkpoint_manifest_key = :manifest_key, "
            "checkpoint_manifest_sha256 = :manifest_sha, "
            "resume_required = :resume"
        )
        names = None
        if suspend:
            update += ", #status = :status"
            values[":status"] = "SUSPENDED"
            names = {"#status": "status"}

        try:
            kwargs = dict(
                Key={"PK": f"JOB#{job_id}", "SK": "META"},
                UpdateExpression=update,
                ConditionExpression=(
                    "fencing_token = :token AND "
                    "(attribute_not_exists(checkpoint_seq) OR checkpoint_seq < :seq)"
                ),
                ExpressionAttributeValues=values,
            )
            if names:
                kwargs["ExpressionAttributeNames"] = names
            self.table.update_item(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise CheckpointCommitRejected(
                    f"checkpoint {sequence} rejected for JOB#{job_id}"
                ) from exc
            raise

        return manifest

    def committed_manifest(self, job_id: str) -> CheckpointManifest | None:
        response = self.table.get_item(
            Key={"PK": f"JOB#{job_id}", "SK": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item or "checkpoint_manifest_key" not in item:
            return None

        body = self.s3.get_object(
            Bucket=self.bucket,
            Key=item["checkpoint_manifest_key"],
        )["Body"].read()
        actual_manifest_sha = sha256(body).hexdigest()
        if actual_manifest_sha != item["checkpoint_manifest_sha256"]:
            raise CheckpointCorrupt("checkpoint manifest hash does not match DynamoDB")

        raw = loads(body)
        if raw.get("state") != "COMPLETE":
            raise CheckpointCorrupt("checkpoint manifest is not COMPLETE")

        files = [CheckpointFile(**f) for f in raw.pop("files")]
        raw.pop("state", None)
        return CheckpointManifest(files=files, **raw)

    def restore_bytes(self, job_id: str) -> dict[str, bytes]:
        manifest = self.committed_manifest(job_id)
        if manifest is None:
            return {}

        new_prefix = f"{self.prefix(job_id, manifest.sequence, manifest.fencing_token)}/data/"
        old_prefix = f"{self.prefix(job_id, manifest.sequence)}/data/"
        if all(f.key.startswith(new_prefix) for f in manifest.files):
            prefix = new_prefix
        elif all(f.key.startswith(old_prefix) for f in manifest.files):
            # Read checkpoints produced before fence-specific S3 keys existed.
            prefix = old_prefix
        else:
            raise CheckpointCorrupt("checkpoint files use an invalid or mixed prefix")
        restored: dict[str, bytes] = {}
        for entry in manifest.files:
            body = self.s3.get_object(Bucket=self.bucket, Key=entry.key)["Body"].read()
            if len(body) != entry.size or sha256(body).hexdigest() != entry.sha256:
                raise CheckpointCorrupt(f"payload verification failed: {entry.key}")
            restored[entry.key.removeprefix(prefix)] = body
        return restored
