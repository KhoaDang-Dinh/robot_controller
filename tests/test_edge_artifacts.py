import hashlib
import json
from unittest.mock import Mock

import pytest

from edge.artifacts import upload_artifact


def test_upload_commits_manifest_after_head_verified(tmp_path):
    path = tmp_path / "test.mcap"
    path.write_bytes(b"recording")
    digest = hashlib.sha256(b"recording").hexdigest()
    s3 = Mock()
    s3.head_object.return_value = {"ContentLength": 9, "Metadata": {"sha256": digest}}
    result = upload_artifact(s3, "bucket", path, kind="rosbag", device_id="dev1",
                             robot_id="robot1", run_id="run001")
    assert result["sha256"] == digest
    assert result["key"].startswith("raw/devices/dev1/rosbag/runs/run001/")
    assert s3.method_calls[0][0] == "upload_file"
    assert s3.method_calls[1][0] == "head_object"
    assert s3.method_calls[2][0] == "put_object"
    assert json.loads(s3.put_object.call_args.kwargs["Body"])["artifact_type"] == "rosbag"


def test_failed_head_does_not_commit_manifest_or_delete_local_file(tmp_path):
    path = tmp_path / "test.mcap"
    path.write_bytes(b"recording")
    s3 = Mock()
    s3.head_object.return_value = {"ContentLength": 0, "Metadata": {}}
    with pytest.raises(RuntimeError):
        upload_artifact(s3, "bucket", path, kind="rosbag", device_id="dev1",
                        robot_id="robot1", run_id="run001")
    s3.put_object.assert_not_called()
    assert path.exists()


def test_reject_unknown_kind_and_empty_file(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"")
    with pytest.raises(ValueError):
        upload_artifact(Mock(), "bucket", path, kind="rosbag", device_id="dev1",
                        robot_id="robot1", run_id="run001")
    path.write_bytes(b"data")
    with pytest.raises(ValueError):
        upload_artifact(Mock(), "bucket", path, kind="firmware", device_id="dev1",
                        robot_id="robot1", run_id="run001")
