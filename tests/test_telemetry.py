import io
import json

import pytest

from telemetry import SCHEMA, Telemetry, resource_attributes


def test_resource_attributes_follow_semantic_conventions():
    env = {"GIT_REVISION": "abc", "POD_NAME": "p-0", "NODE_NAME": "ip-10-0-0-1",
           "JOB_ID": "trial", "CAPACITY_TYPE": "spot", "COST_CENTER": "thesis",
           "IMAGE_DIGEST": "sha256:" + "a" * 64, "UNRELATED": "ignored"}
    attributes = resource_attributes(env)
    assert attributes["service.name"] == "robot-sim-workload"
    assert attributes["service.version"] == "abc"
    assert attributes["k8s.pod.name"] == "p-0"
    assert attributes["k8s.node.name"] == "ip-10-0-0-1"
    assert attributes["robot_sim.capacity_type"] == "spot"
    assert "UNRELATED" not in json.dumps(attributes)


def test_event_is_one_json_line_with_resource():
    out = io.StringIO()
    telemetry = Telemetry("w", stream=out, env={"JOB_ID": "j"})
    telemetry.event("checkpoint", sequence=3)
    (line,) = out.getvalue().splitlines()
    record = json.loads(line)
    assert record["schema"] == SCHEMA
    assert record["event"] == "checkpoint"
    assert record["sequence"] == 3
    assert record["resource"]["robot_sim.job_id"] == "j"


def test_usage_accumulates_gpu_seconds_and_counters():
    now = [100.0]
    out = io.StringIO()
    telemetry = Telemetry("w", stream=out, env={"GPU_COUNT": "2"}, clock=lambda: now[0])
    telemetry.add("steps", 5)
    with telemetry.timed("checkpoint_seconds"):
        now[0] += 1.5
    now[0] += 8.5
    record = telemetry.usage("completed")
    assert record["outcome"] == "completed"
    assert record["usage"]["wall_seconds"] == 10.0
    assert record["usage"]["gpu_seconds"] == 20.0
    assert record["usage"]["steps"] == 5
    assert record["usage"]["checkpoint_seconds"] == 1.5


def test_invalid_gpu_count_and_unknown_counter():
    telemetry = Telemetry("w", stream=io.StringIO(), env={"GPU_COUNT": "lots"})
    assert telemetry.gpus == 0
    with pytest.raises(KeyError):
        telemetry.add("nope")
