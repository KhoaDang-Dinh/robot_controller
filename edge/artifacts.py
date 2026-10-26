"""Upload complete, closed files directly to S3; manifest is the commit marker.

The same payload and run ID resolve to the same object key on retry.
S3 ObjectCreated on *.manifest.json is the trigger for downstream indexing.
"""
import hashlib
import json
from pathlib import Path
from datetime import datetime, timezone

from .transport import identifier, SCHEMA_VERSION

ALLOWED_KINDS = {"rosbag", "lidar", "camera", "logs", "simulation"}


def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload_artifact(s3, bucket, path, *, kind, device_id, robot_id, run_id):
    """Return manifest after HEAD verification; retain local file on all failures.

    Call only on finalized files (not an actively written ROS bag/log).
    The S3 client must have PutObject and GetObject scoped to this device prefix.
    """
    device_id = identifier(device_id)
    robot_id = identifier(robot_id)
    run_id = identifier(run_id)
    if kind not in ALLOWED_KINDS:
        raise ValueError("unknown artifact kind")
    file_path = Path(path)
    if not file_path.is_file() or file_path.is_symlink():
        raise ValueError("artifact must be a finalized regular file")
    if not file_path.name or file_path.name in (".", ".."):
        raise ValueError("invalid file name")
    size = file_path.stat().st_size
    if size == 0:
        raise ValueError("empty artifact")
    sha = checksum(file_path)
    # Use a fixed basename to avoid user-supplied path traversal and leakage of local filenames.
    key = "raw/devices/{}/{}/runs/{}/{}/payload".format(device_id, kind, run_id, sha)
    s3.upload_file(
        str(file_path), bucket, key,
        ExtraArgs={"Metadata": {"sha256": sha, "robot-id": robot_id, "run-id": run_id}},
    )
    head = s3.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != size or head.get("Metadata", {}).get("sha256") != sha:
        raise RuntimeError("S3 size/checksum metadata verification failed")
    # Size+metadata verification checks our upload bookkeeping, not an independent
    # server-computed payload digest. For stronger guarantees use S3 checksum APIs.
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "artifact_id": sha,
        "artifact_type": kind,
        "device_id": device_id,
        "robot_id": robot_id,
        "run_id": run_id,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "format": file_path.suffix.lower().lstrip(".") or "binary",
        "size_bytes": size,
        "sha256": sha,
        "bucket": bucket,
        "key": key,
    }
    s3.put_object(
        Bucket=bucket, Key=key + ".manifest.json",
        Body=json.dumps(manifest, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
    )
    return manifest
