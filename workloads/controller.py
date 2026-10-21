#!/usr/bin/env python3
import json
import os
import signal
import time

import boto3
from telemetry import Telemetry

telemetry = Telemetry("controller")


REGION = os.environ["AWS_REGION"]
PROJECT = os.environ.get("PLATFORM_PROJECT", "robot-sim")
ENVIRONMENT = os.environ.get("PLATFORM_ENVIRONMENT", "dev")
PREFIX = f"/{PROJECT}/{ENVIRONMENT}"

ssm = boto3.client("ssm", region_name=REGION)
ddb = boto3.resource("dynamodb", region_name=REGION)
sqs = boto3.client("sqs", region_name=REGION)

stopping = False


def parameter(suffix):
    return ssm.get_parameter(Name=f"{PREFIX}/{suffix}")["Parameter"]["Value"]


TABLE = ddb.Table(parameter("state/control-table"))
QUEUE_URL = parameter("events/queue-url")


def set_cluster_status(status):
    TABLE.update_item(
        Key={"PK": f"CLUSTER#{ENVIRONMENT}", "SK": "META"},
        UpdateExpression="SET #s = :s, updated_at = :now",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": status, ":now": int(time.time())},
    )


def set_kill_switch(disabled):
    TABLE.update_item(
        Key={"PK": "CONFIG#KILL_SWITCH", "SK": "META"},
        UpdateExpression="SET disabled = :disabled",
        ExpressionAttributeValues={":disabled": disabled},
    )


def handle(message):
    body = json.loads(message["Body"])
    source = body.get("source")
    detail_type = body.get("detail-type")
    detail = body.get("detail", {})

    if source == "robot-sim.runtime" and detail_type == "Runtime Shutdown Requested":
        set_kill_switch(True)
        set_cluster_status("DRAINING")
        print(json.dumps({"event": "runtime_shutdown_requested"}), flush=True)
        return

    if source == "aws.ec2" and detail_type in {
        "EC2 Spot Instance Interruption Warning",
        "EC2 Instance Rebalance Recommendation",
    }:
        print(json.dumps({
            "event": "capacity_interruption",
            "detail_type": detail_type,
            "detail": detail,
        }), flush=True)
        # Workload-specific checkpoint/reschedule handling is deliberately
        # separated from message delivery. The durable checkpoint protocol
        # prevents a stale attempt from committing after a replacement starts.
        return

    print(json.dumps({"event": "ignored", "source": source, "detail_type": detail_type}), flush=True)


def request_stop(*_):
    global stopping
    stopping = True


signal.signal(signal.SIGTERM, request_stop)
signal.signal(signal.SIGINT, request_stop)

set_cluster_status("READY")
telemetry.event("controller_ready")

while not stopping:
    response = sqs.receive_message(
        QueueUrl=QUEUE_URL,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=20,
        VisibilityTimeout=120,
    )
    for message in response.get("Messages", []):
        try:
            handle(message)
        except Exception as exc:
            print(json.dumps({"event": "message_failed", "error": repr(exc)}), flush=True)
            continue
        sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=message["ReceiptHandle"])

set_cluster_status("STOPPING")
telemetry.usage("stopped")

