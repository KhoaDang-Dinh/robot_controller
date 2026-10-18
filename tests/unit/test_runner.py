import json

import pytest

from robot_controller.core.experiment import parse
from robot_controller.core.runner import (
    COMPLETED, FENCED, INCOMPATIBLE, NOT_RUNNING, SUSPENDED, Runner, RuntimeEnv, exit_code,
)
from tests.conftest import meta, seed_external

DIGEST = "sha256:" + "b" * 64


def experiment(policy="noop", target=5, shards=1, extra="", params=""):
    return parse(f"""schema_version: 1
name: unit
adapter: {{name: smoke, params: {{step_seconds: 0}}}}
policy: {{name: {policy}{params}}}
shards: {shards}
target_steps: {target}
checkpoint_interval_seconds: 3600
limits: {{max_gpus: 1}}
{extra}""")


def runner(session, exp, token=1, job="job-1", index=0):
    rt = RuntimeEnv(region="us-east-1", job_id=job, image_digest=DIGEST, fencing_token=token, shard_index=index)
    return Runner(exp, rt, session=session, install_signal_handlers=False)


def manifest(session, shard):
    key = meta(session, shard)["checkpoint_manifest_key"]
    return json.loads(session.client("s3").get_object(Bucket="research", Key=key)["Body"].read())


def test_external_run_completes_and_writes_identity_and_results(platform):
    seed_external(platform, "job-1")
    assert runner(platform, experiment(target=5)).run() == COMPLETED
    item = meta(platform, "job-1")
    assert item["status"] == "COMPLETED"
    m = manifest(platform, "job-1")
    assert m["metadata"]["progress"] == 5  # read by infra's recovery-test workflow
    assert m["metadata"]["experiment"]["adapter"] == {"name": "smoke", "state_version": 1}
    assert m["workload_version"] == "smoke@1/noop@1"
    result = json.loads(platform.client("s3").get_object(
        Bucket="research", Key="results/unit/job-1/shard-00000/attempt-000001.json")["Body"].read())
    assert result["outcome"] == "completed" and result["steps"] == 5


def test_resume_continues_from_committed_steps(platform):
    seed_external(platform, "job-1")
    runner(platform, experiment(policy="epsilon_greedy", target=5)).run()
    second = runner(platform, experiment(policy="epsilon_greedy", target=9))
    # target_steps differs, so the file differs: resuming needs the explicit opt-in.
    assert second.run() == INCOMPATIBLE
    allowed = experiment(policy="epsilon_greedy", target=9, extra="resume: {allow_config_change: true}\n")
    r = runner(platform, allowed)
    assert r.run() == COMPLETED and r.steps == 9
    assert set(r.metrics) == {"mean_reward", "epsilon"}


def test_resume_refuses_different_policy(platform):
    seed_external(platform, "job-1")
    runner(platform, experiment(target=3)).run()
    other = experiment(policy="epsilon_greedy", target=3, extra="resume: {allow_config_change: true}\n")
    assert runner(platform, other).run() == INCOMPATIBLE


def test_runtime_overrides_do_not_change_identity(platform):
    from robot_controller.core.runner import apply_overrides
    base = experiment(target=0)
    tuned = apply_overrides(base, {"TARGET_STEPS": "4", "CHECKPOINT_INTERVAL_SECONDS": "2"})
    assert (tuned.target_steps, tuned.checkpoint_interval_seconds) == (4, 2)
    assert tuned.config_sha256 == base.config_sha256
    assert apply_overrides(base, {"MAX_STEPS": "3"}).target_steps == 3  # PR #2 name


@pytest.mark.parametrize("mode_token, expected_exit", [(1, 0), (None, 1)])
def test_graceful_stop_suspends(platform, mode_token, expected_exit):
    seed_external(platform, "job-1")
    r = runner(platform, experiment(target=0), token=mode_token)
    calls = {"n": 0}

    def stop_after_three():
        calls["n"] += 1
        return calls["n"] > 3
    r.should_stop = stop_after_three
    outcome = r.run()
    shard = "job-1" if mode_token else "job-1-shard-00000"
    assert outcome == SUSPENDED and meta(platform, shard)["status"] == "SUSPENDED"
    assert exit_code(outcome, r.rt.mode) == expected_exit


def test_stale_token_cannot_start_and_running_worker_is_fenced(platform):
    seed_external(platform, "job-1", token=2)
    assert runner(platform, experiment(), token=1).run() == FENCED
    assert "checkpoint_seq" not in meta(platform, "job-1")

    r = runner(platform, experiment(target=0), token=2)
    table = platform.resource("dynamodb").Table("control")
    original_step = r.adapter.step

    def step_then_replace(action):
        if r.steps == 2:  # a replacement Pod claims the job mid-run
            table.update_item(Key={"PK": "JOB#job-1", "SK": "META"}, UpdateExpression="SET fencing_token = :t",
                              ExpressionAttributeValues={":t": 3})
            r.stopping = True
        return original_step(action)
    r.adapter.step = step_then_replace
    assert r.run() == FENCED
    assert exit_code(FENCED, "external") == 0 and exit_code(FENCED, "claim") == 1


def test_claim_mode_fences_previous_pods_and_refuses_when_not_running(platform):
    exp = experiment(target=2, shards=2, extra="parallelism: 1\n")
    assert runner(platform, exp, token=None, index=1).run() == COMPLETED
    assert int(meta(platform, "job-1-shard-00001")["fencing_token"]) == 1
    assert runner(platform, exp, token=None, index=1).run() == COMPLETED
    assert int(meta(platform, "job-1-shard-00001")["fencing_token"]) == 2
    platform.client("ssm").put_parameter(Name="/robot-sim/dev/runtime/desired-state", Value="stopping",
                                         Type="String", Overwrite=True)
    assert runner(platform, exp, token=None, index=0).run() == NOT_RUNNING


def test_external_lease_requires_single_shard(platform):
    with pytest.raises(ValueError, match="single-shard"):
        runner(platform, experiment(shards=2, extra="parallelism: 1\n"), token=1)
