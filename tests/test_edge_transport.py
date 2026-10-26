import json

import pytest

from edge.transport import EventOutbox, build_event, mqtt_topic


def test_event_envelope_and_routing():
    event = build_event("jetson-tx2-001", "ur10-lab-01", "model.inference_failed",
                        "ERROR", "OOM", run_id="run001", data={"free_mb": 200})
    assert event["schema_version"] == "1.0"
    assert event["run_id"] == "run001"
    assert event["event_id"]
    assert mqtt_topic("ur10-lab-01", "jetson-tx2-001", "robot_sim_dev_robot_events") == (
        "$aws/rules/robot_sim_dev_robot_events/robots/ur10-lab-01/devices/jetson-tx2-001/events")


def test_spool_retains_until_broker_ack_and_replays_on_retry(tmp_path):
    box = EventOutbox(tmp_path, "robot1", "dev1", "rule1")
    box.enqueue(build_event("dev1", "robot1", "ros.node_failed", "ERROR", "offline"))
    assert len(box.pending()) == 1
    def offline(topic, payload):
        raise TimeoutError("broker unavailable")
    with pytest.raises(TimeoutError):
        box.drain(offline)
    assert len(box.pending()) == 1
    messages = []
    assert box.drain(lambda topic, payload: messages.append((topic, json.loads(payload)))) == 1
    assert messages[0][1]["event_type"] == "ros.node_failed"
    assert not box.pending()


def test_invalid_ids_size_quota_and_device_mismatch(tmp_path):
    with pytest.raises(ValueError):
        build_event("bad/+", "robot", "ros.node_failed", "ERROR", "bad")
    with pytest.raises(ValueError):
        build_event("dev1", "robot1", "ros.node_failed", "ERROR", "oversized", data={"a": "x" * 20000})
    box = EventOutbox(tmp_path, "robot1", "dev1", "rule1", max_events=1)
    with pytest.raises(ValueError):
        box.enqueue(build_event("dev2", "robot1", "ros.node_failed", "ERROR", "not me"))
    box.enqueue(build_event("dev1", "robot1", "ros.node_failed", "ERROR", "first"))
    with pytest.raises(OSError):
        box.enqueue(build_event("dev1", "robot1", "ros.node_failed", "ERROR", "second"))
