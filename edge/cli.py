"""Minimal edge adapter CLI: durable events, QoS1 replay, direct S3 artifacts.

Never call these synchronous operations from a robot servo/control loop.
An integrator can run flush periodically through systemd/Greengrass.
"""
import argparse
import json

from .artifacts import upload_artifact
from .credentials import temporary_credentials
from .transport import EventOutbox, build_event, identifier


def _publisher(args):
    import paho.mqtt.client as mqtt

    client = mqtt.Client(client_id=args.device_id, protocol=mqtt.MQTTv311)
    client.tls_set(ca_certs=args.ca, certfile=args.cert, keyfile=args.key)
    client.connect(args.mqtt_endpoint, port=8883, keepalive=60)
    client.loop_start()

    def publish(topic, payload):
        info = client.publish(topic, payload, qos=1, retain=False)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise RuntimeError("MQTT publish failed: {}".format(info.rc))
        info.wait_for_publish(timeout=15)
        if not info.is_published():
            raise TimeoutError("no QoS1 PUBACK")

    return client, publish


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--robot-id", required=True)
    parser.add_argument("--spool", default="/var/lib/robot-edge/events")
    parser.add_argument("--rule", default="robot_sim_dev_robot_events")
    sub = parser.add_subparsers(dest="action", required=True)

    event = sub.add_parser("event", help="Queue operational event without network I/O")
    event.add_argument("--type", required=True)
    event.add_argument("--severity", choices=["DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"], default="INFO")
    event.add_argument("--message", required=True)
    event.add_argument("--source", default="robot_edge")
    event.add_argument("--run-id")
    event.add_argument("--model-version")
    event.add_argument("--data", default="{}")

    flush = sub.add_parser("flush", help="Publish durable events with QoS1 acknowledgements")
    flush.add_argument("--mqtt-endpoint", required=True)
    flush.add_argument("--ca", required=True)
    flush.add_argument("--cert", required=True)
    flush.add_argument("--key", required=True)
    flush.add_argument("--limit", type=int, default=100)

    upload = sub.add_parser("upload", help="Upload closed raw files directly to S3")
    upload.add_argument("--bucket", required=True)
    upload.add_argument("--region", required=True)
    upload.add_argument("--file", required=True)
    upload.add_argument("--kind", choices=["rosbag", "lidar", "camera", "logs", "simulation"], required=True)
    upload.add_argument("--run-id", required=True)
    upload.add_argument("--credentials-endpoint")
    upload.add_argument("--role-alias")
    upload.add_argument("--ca")
    upload.add_argument("--cert")
    upload.add_argument("--key")

    args = parser.parse_args(argv)
    identifier(args.device_id)
    identifier(args.robot_id)
    if args.action == "event":
        data = json.loads(args.data)
        outbox = EventOutbox(args.spool, args.robot_id, args.device_id, args.rule)
        outbox.enqueue(build_event(
            args.device_id, args.robot_id, args.type, args.severity,
            args.message, source=args.source, run_id=args.run_id,
            model_version=args.model_version, data=data))
        return 0
    if args.action == "flush":
        if not 1 <= args.limit <= 10000:
            parser.error("--limit must be 1..10000")
        outbox = EventOutbox(args.spool, args.robot_id, args.device_id, args.rule)
        client, publish = _publisher(args)
        try:
            print(json.dumps({"published": outbox.drain(publish, limit=args.limit)}))
        finally:
            client.loop_stop()
            client.disconnect()
        return 0
    import boto3

    kwargs = {}
    if args.credentials_endpoint:
        if not all((args.role_alias, args.ca, args.cert, args.key)):
            parser.error("IoT credentials require --role-alias, --ca, --cert and --key")
        kwargs = temporary_credentials(args.credentials_endpoint, args.role_alias,
                                       args.device_id, args.cert, args.key, args.ca)
    client = boto3.client("s3", region_name=args.region, **kwargs)
    print(json.dumps(upload_artifact(
        client, args.bucket, args.file, kind=args.kind,
        device_id=args.device_id, robot_id=args.robot_id, run_id=args.run_id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
