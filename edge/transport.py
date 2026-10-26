"""Versioned, bounded MQTT events with an offline, disk-backed QoS1 outbox.

Only operational events go through this outbox. High-frequency sensor data,
complete logs, and ROS bags must be stored locally and uploaded to S3.
MQTT PUBACK != downstream SQS/database commit; consumers deduplicate event_id.
"""
import json
import os
from pathlib import Path
import re
from datetime import datetime, timezone
from uuid import uuid4

SCHEMA_VERSION = "1.0"
MAX_EVENT_BYTES = 16 * 1024
VALID_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
VALID_EVENT = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
SEVERITIES = {"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"}


def identifier(value):
    if not isinstance(value, str) or not VALID_ID.fullmatch(value):
        raise ValueError("identifier must be 1-64 URL/topic-safe characters")
    return value


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_event(device_id, robot_id, event_type, severity, message, *,
                source="robot_edge", run_id=None, model_version=None, data=None):
    device_id, robot_id = identifier(device_id), identifier(robot_id)
    if severity not in SEVERITIES or not VALID_EVENT.fullmatch(event_type):
        raise ValueError("invalid severity or event_type")
    if not isinstance(message, str) or not message or len(message) > 2048:
        raise ValueError("message must be 1-2048 characters")
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": uuid4().hex,
        "timestamp": utc_now(),
        "device_id": device_id,
        "robot_id": robot_id,
        "severity": severity,
        "event_type": event_type,
        "source": identifier(source),
        "message": message,
    }
    if run_id is not None:
        event["run_id"] = identifier(run_id)
    if model_version is not None:
        event["model_version"] = str(model_version)
    if data is not None:
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        event["data"] = data
    encoded = json.dumps(event, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_EVENT_BYTES:
        raise ValueError("operational event exceeds 16 KiB; upload details to S3")
    return event


def mqtt_topic(robot_id, device_id, rule_name):
    identifier(robot_id)
    identifier(device_id)
    identifier(rule_name)
    return "$aws/rules/{}/robots/{}/devices/{}/events".format(rule_name, robot_id, device_id)


class EventOutbox:
    """One file per event; only remove after QoS1 publish acknowledgement.

    A bounded spool refuses new events rather than silently losing critical logs.
    Operators must monitor disk usage and failures; a safety stop is always local.
    """

    def __init__(self, directory, robot_id, device_id, rule_name, max_events=10000):
        self.directory = Path(directory)
        self.robot_id = identifier(robot_id)
        self.device_id = identifier(device_id)
        self.rule_name = identifier(rule_name)
        if not 1 <= max_events <= 1000000:
            raise ValueError("invalid max_events")
        self.max_events = max_events
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def pending(self):
        return sorted(self.directory.glob("*.json"))

    def enqueue(self, event):
        if event.get("device_id") != self.device_id or event.get("robot_id") != self.robot_id:
            raise ValueError("event identity mismatch")
        if event.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("event schema mismatch")
        payload = json.dumps(event, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(payload) > MAX_EVENT_BYTES:
            raise ValueError("event exceeds maximum MQTT size")
        if len(self.pending()) >= self.max_events:
            raise OSError("event spool quota exceeded")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        filename = "{}-{}.json".format(stamp, identifier(event["event_id"]))
        target = self.directory / filename
        temporary = self.directory / (filename + ".tmp")
        try:
            with open(temporary, "xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            dir_fd = os.open(str(self.directory), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target

    def drain(self, publish, limit=100):
        """publish(topic, bytes) must raise unless the broker acknowledged QoS1."""
        topic = mqtt_topic(self.robot_id, self.device_id, self.rule_name)
        count = 0
        for path in self.pending()[:limit]:
            payload = path.read_bytes()
            event = json.loads(payload)
            if event["device_id"] != self.device_id or event["robot_id"] != self.robot_id:
                raise ValueError("unexpected device identity in spool")
            publish(topic, payload)
            path.unlink()  # replay after a crash may cause a duplicate event_id
            count += 1
        return count
