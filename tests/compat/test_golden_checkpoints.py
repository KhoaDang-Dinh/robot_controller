"""Checkpoints written by earlier releases must still resume (or be refused clearly).

tests/compat/golden/<format>/ holds the exact DynamoDB META item and S3 objects
that a released worker wrote, recorded by running that release against moto:

  resume-smoke-v1          PR #2 workloads/resume_smoke.py (external token, 4 steps, SUSPENDED)
  indexed-resume-smoke-v2  PR #2 workloads/indexed_resume_smoke.py (shard 1 of 2, 5 steps, COMPLETED)

When a change alters the checkpoint layout or a plug-in state_version, add a new
golden directory recorded from the last release instead of editing these.
"""
import json
from pathlib import Path

import pytest

from robot_controller.core.experiment import parse
from robot_controller.core.runner import COMPLETED, INCOMPATIBLE, Runner, RuntimeEnv
from tests.conftest import meta

pytestmark = pytest.mark.compat
GOLDEN = Path(__file__).parent / "golden"
DIGEST = "sha256:" + "c" * 64


def load_golden(session, name):
    folder = GOLDEN / name
    session.client("dynamodb").put_item(TableName="control", Item=json.loads((folder / "meta.json").read_text()))
    s3 = session.client("s3")
    for key, body in json.loads((folder / "objects.json").read_text()).items():
        s3.put_object(Bucket="research", Key=key, Body=body.encode())


def smoke(target, shards=1, policy="noop"):
    return parse(f"""schema_version: 1
name: compat
adapter: {{name: smoke, params: {{step_seconds: 0}}}}
policy: {{name: {policy}}}
shards: {shards}
target_steps: {target}
limits: {{max_gpus: 2}}
""")


def run(session, exp, job, token=None, index=0):
    rt = RuntimeEnv(region="us-east-1", job_id=job, image_digest=DIGEST, fencing_token=token, shard_index=index)
    r = Runner(exp, rt, session=session, install_signal_handlers=False)
    return r, r.run()


def test_resume_smoke_v1_checkpoint_resumes_after_fencing(platform):
    load_golden(platform, "resume-smoke-v1")
    before = meta(platform, "legacy-resume")
    assert before["status"] == "SUSPENDED" and int(before["checkpoint_seq"]) == 4
    platform.resource("dynamodb").Table("control").update_item(
        Key={"PK": "JOB#legacy-resume", "SK": "META"}, UpdateExpression="SET fencing_token = :t",
        ExpressionAttributeValues={":t": 2})
    runner, outcome = run(platform, smoke(target=7), "legacy-resume", token=2)
    assert outcome == COMPLETED
    assert runner.steps == 7 and runner.sequence == 5  # resumed at step 4 / seq 4, one final commit
    after = meta(platform, "legacy-resume")
    assert after["status"] == "COMPLETED" and int(after["checkpoint_seq"]) == 5


def test_indexed_v2_shard_resumes_with_a_new_claim(platform):
    load_golden(platform, "indexed-resume-smoke-v2")
    runner, outcome = run(platform, smoke(target=8, shards=2), "legacy-indexed", index=1)
    assert outcome == COMPLETED and runner.steps == 8
    assert int(meta(platform, "legacy-indexed-shard-00001")["fencing_token"]) == 2


def test_indexed_v2_shard_count_cannot_change(platform):
    load_golden(platform, "indexed-resume-smoke-v2")
    _, outcome = run(platform, smoke(target=8, shards=3), "legacy-indexed", index=1)
    assert outcome == INCOMPATIBLE
    assert int(meta(platform, "legacy-indexed-shard-00001")["fencing_token"]) == 1  # untouched


def test_stateful_policy_refuses_legacy_checkpoint_without_policy_state(platform):
    # PR #2 checkpoints carry no policy state. A learning policy must not silently
    # start from scratch inside an old run; it needs a new job id.
    load_golden(platform, "indexed-resume-smoke-v2")
    _, outcome = run(platform, smoke(target=8, shards=2, policy="epsilon_greedy"), "legacy-indexed", index=1)
    assert outcome == INCOMPATIBLE
    assert meta(platform, "legacy-indexed-shard-00001")["status"] == "FAILED"
