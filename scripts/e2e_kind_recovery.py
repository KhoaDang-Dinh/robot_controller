#!/usr/bin/env python3
"""Dynamic end-to-end test of the resume-smoke image on a real Kubernetes cluster.

Runs in CI against a throwaway kind cluster and a moto server on the runner
host (an AWS API emulator), so it needs no AWS account and creates no paid
resources. It drives the same lifecycle as infra_robot_sim's EKS
destroy/recreate/resume test, but replaces the cluster-level teardown with a
Pod replacement:

  1. seed SSM/DynamoDB/S3, start generation 1 with fencing token 1;
  2. wait for >= 2 committed checkpoints, then request a graceful stop;
  3. require SUSPENDED and record committed progress;
  4. fence (token 2), start generation 2 from the same template;
  5. require committed progress beyond step 3; reject a stale token-1 Pod;
  6. write all Pod logs to disk for the usage/cost report.

The Job manifest is rendered from k8s/tests/resume-smoke-job.yaml.tmpl, the
same file EKS uses, with only the image reference and emulator endpoint changed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import string
import subprocess
import sys
import time

import boto3

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "k8s/tests/resume-smoke-job.yaml.tmpl"
NAMESPACE = "robot-platform"
PREFIX = "/robot-sim/dev"
REGION = "ap-southeast-1"  # the template pins the dev region


def sh(*args, input_text=None, check=True):
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")
    return result.stdout


def render(generation: int, token: int, job_id: str, image: str, endpoint: str) -> str:
    text = string.Template(TEMPLATE.read_text()).substitute(
        JOB_ID=job_id, RUNTIME_GENERATION=generation, FENCING_TOKEN=token,
        IMAGE_DIGEST="sha256:" + "0" * 64, GIT_REVISION=os.environ.get("GITHUB_SHA", "local"))
    output = sh("kubectl", "create", "--dry-run=client", "-o", "json", "-f", "-", input_text=text)
    # kubectl versions may print one List or consecutive JSON objects for YAML documents.
    items = []
    decoder = json.JSONDecoder()
    remaining = output.lstrip()
    while remaining:
        document, end = decoder.raw_decode(remaining)
        items.extend(document["items"] if document.get("kind") == "List" else [document])
        remaining = remaining[end:].lstrip()
    for item in items:
        if item["kind"] != "Job":
            continue
        pod = item["spec"]["template"]["spec"]
        pod.pop("imagePullSecrets", None)
        container = pod["containers"][0]
        container["image"] = image          # locally built, loaded into kind
        container["imagePullPolicy"] = "Never"
        container["env"] += [
            {"name": "AWS_ENDPOINT_URL", "value": endpoint},
            {"name": "AWS_ACCESS_KEY_ID", "value": "testing"},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": "testing"},
            {"name": "CHECKPOINT_INTERVAL_SECONDS", "value": "2"},
        ]
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": items})


class Emulator:
    def __init__(self, endpoint: str):
        session = boto3.session.Session(aws_access_key_id="testing", aws_secret_access_key="testing",  # noqa: S106 - emulator dummy credentials
                                        region_name=REGION)
        self.ssm = session.client("ssm", endpoint_url=endpoint)
        self.s3 = session.client("s3", endpoint_url=endpoint)
        self.ddb = session.client("dynamodb", endpoint_url=endpoint)

    def seed(self, job_id: str):
        self.s3.create_bucket(Bucket="research", CreateBucketConfiguration={"LocationConstraint": REGION})
        self.ddb.create_table(
            TableName="control", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        self.desired("running")
        for name, value in (("storage/research-bucket", "research"), ("state/control-table", "control")):
            self.ssm.put_parameter(Name=f"{PREFIX}/{name}", Value=value, Type="String", Overwrite=True)
        self.ddb.put_item(TableName="control", Item={
            "PK": {"S": f"JOB#{job_id}"}, "SK": {"S": "META"}, "status": {"S": "PROVISIONING"},
            "fencing_token": {"N": "1"}, "resume_required": {"BOOL": False}})

    def desired(self, state: str):
        self.ssm.put_parameter(Name=f"{PREFIX}/runtime/desired-state", Value=state, Type="String", Overwrite=True)

    def meta(self, job_id: str) -> dict:
        item = self.ddb.get_item(TableName="control", ConsistentRead=True,
                                 Key={"PK": {"S": f"JOB#{job_id}"}, "SK": {"S": "META"}}).get("Item", {})
        return {k: next(iter(v.values())) for k, v in item.items()}

    def progress(self, job_id: str) -> int:
        key = self.meta(job_id)["checkpoint_manifest_key"]
        manifest = json.loads(self.s3.get_object(Bucket="research", Key=key)["Body"].read())
        return int(manifest["metadata"]["progress"])

    def fence(self, job_id: str, token: int):
        self.ddb.update_item(
            TableName="control", Key={"PK": {"S": f"JOB#{job_id}"}, "SK": {"S": "META"}},
            UpdateExpression="SET fencing_token = :t, #s = :r, resume_required = :true",
            ConditionExpression="#s = :suspended",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":t": {"N": str(token)}, ":r": {"S": "RESTORING"},
                                       ":true": {"BOOL": True}, ":suspended": {"S": "SUSPENDED"}})


def wait_for(description: str, predicate, timeout: float = 180, poll: float = 2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(poll)
    raise TimeoutError(f"timed out waiting for {description}")


def job_status(name: str) -> dict:
    out = sh("kubectl", "-n", NAMESPACE, "get", "job", name, "-o", "json", check=False)
    return json.loads(out).get("status", {}) if out else {}


def collect_logs(name: str, log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    text = sh("kubectl", "-n", NAMESPACE, "logs", f"job/{name}", "--all-containers", check=False)
    (log_dir / f"{name}.jsonl").write_text(text)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True, help="locally built image loaded into kind")
    parser.add_argument("--host-endpoint", required=True, help="emulator URL reachable from this runner")
    parser.add_argument("--pod-endpoint", required=True, help="emulator URL reachable from Pods")
    parser.add_argument("--logs", type=Path, default=Path("e2e-logs"))
    args = parser.parse_args()

    job_id = f"e2e-{os.environ.get('GITHUB_RUN_ID', int(time.time()))}"
    aws = Emulator(args.host_endpoint)
    aws.seed(job_id)
    sh("kubectl", "create", "namespace", NAMESPACE)

    print("generation 1: start and commit checkpoints", flush=True)
    sh("kubectl", "apply", "-f", "-", input_text=render(1, 1, job_id, args.image, args.pod_endpoint))
    wait_for("two committed checkpoints", lambda: int(aws.meta(job_id).get("checkpoint_seq", 0)) >= 2)

    print("generation 1: graceful stop -> SUSPENDED", flush=True)
    aws.desired("stopping")
    wait_for("generation 1 completion", lambda: job_status("resume-smoke-1").get("succeeded"))
    meta = aws.meta(job_id)
    if meta["status"] != "SUSPENDED":
        raise AssertionError(f"expected SUSPENDED, got {meta['status']}")
    base_seq, base_progress = int(meta["checkpoint_seq"]), aws.progress(job_id)
    collect_logs("resume-smoke-1", args.logs)

    print(f"generation 2: fence and resume from seq={base_seq} progress={base_progress}", flush=True)
    aws.fence(job_id, 2)
    aws.desired("running")
    sh("kubectl", "apply", "-f", "-", input_text=render(2, 2, job_id, args.image, args.pod_endpoint))
    wait_for("progress beyond the saved checkpoint",
             lambda: int(aws.meta(job_id).get("checkpoint_seq", 0)) > base_seq and aws.progress(job_id) > base_progress)
    resumed = collect_logs("resume-smoke-2", args.logs)
    first = next(json.loads(line) for line in resumed.splitlines() if '"event":"resumed"' in line)
    if first["progress"] != base_progress:
        raise AssertionError(f"generation 2 resumed at {first['progress']}, expected {base_progress}")

    print("stale generation 1 token must not start", flush=True)
    sh("kubectl", "apply", "-f", "-", input_text=render(3, 1, job_id, args.image, args.pod_endpoint))
    wait_for("stale Pod failure", lambda: job_status("resume-smoke-3").get("failed"))
    stale = collect_logs("resume-smoke-3", args.logs)
    if "ConditionalCheckFailed" not in stale:
        raise AssertionError("stale Pod failed for an unexpected reason:\n" + stale)

    aws.desired("stopping")
    wait_for("generation 2 completion", lambda: job_status("resume-smoke-2").get("succeeded"))
    collect_logs("resume-smoke-2", args.logs)
    final = aws.progress(job_id)
    print(f"PASS: seq {base_seq} -> {aws.meta(job_id)['checkpoint_seq']}; progress {base_progress} -> {final}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

