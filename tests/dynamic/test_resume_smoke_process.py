"""Dynamic test: run the real worker process against a local AWS emulator.

Unlike the unit tests, nothing is mocked inside the process: the worker starts
as a subprocess, talks to a moto server over HTTP through boto3's standard
AWS_ENDPOINT_URL override, handles signals, commits checkpoints and exits.
It proves resume-after-replacement and stale-worker fencing end to end.
The same scenario runs inside Kubernetes (kind) in .github/workflows/e2e.yml.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

import boto3
import pytest

moto_server = pytest.importorskip("moto.server")

pytestmark = pytest.mark.dynamic

ROOT = Path(__file__).resolve().parents[2]
WORKER = ROOT / "workloads/resume_smoke.py"
REGION = "us-east-1"
JOB = "dyn-test"
DIGEST = "sha256:" + "d" * 64


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def aws():
    port = free_port()
    server = moto_server.ThreadedMotoServer(ip_address="127.0.0.1", port=port)
    server.start()
    # moto keeps one in-process backend; start every scenario from empty state.
    urllib.request.urlopen(urllib.request.Request(  # noqa: S310 - fixed local URL
        f"http://127.0.0.1:{port}/moto-api/reset", method="POST"), timeout=10).close()
    env = {
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{port}",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
    }
    session = boto3.session.Session(aws_access_key_id="testing", aws_secret_access_key="testing",
                                    region_name=REGION)
    clients = {name: session.client(name, endpoint_url=env["AWS_ENDPOINT_URL"])
               for name in ("ssm", "s3", "dynamodb")}
    clients["s3"].create_bucket(Bucket="research")
    clients["dynamodb"].create_table(
        TableName="control",
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                              {"AttributeName": "SK", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST")
    for name, value in (("storage/research-bucket", "research"), ("state/control-table", "control"),
                        ("runtime/desired-state", "running")):
        clients["ssm"].put_parameter(Name=f"/robot-sim/dev/{name}", Value=value, Type="String", Overwrite=True)
    clients["dynamodb"].put_item(TableName="control", Item={
        "PK": {"S": f"JOB#{JOB}"}, "SK": {"S": "META"}, "status": {"S": "PROVISIONING"},
        "fencing_token": {"N": "1"}, "resume_required": {"BOOL": False}})
    yield env, clients
    server.stop()


def meta(clients):
    item = clients["dynamodb"].get_item(TableName="control", ConsistentRead=True,
                                        Key={"PK": {"S": f"JOB#{JOB}"}, "SK": {"S": "META"}})["Item"]
    return {k: next(iter(v.values())) for k, v in item.items()}


def start(env, token, max_steps):
    child_env = {
        **{k: v for k, v in os.environ.items() if not k.startswith("AWS_")},
        **env,
        "JOB_ID": JOB, "FENCING_TOKEN": str(token), "IMAGE_DIGEST": DIGEST,
        "CHECKPOINT_INTERVAL_SECONDS": "1", "MAX_STEPS": str(max_steps),
        "GIT_REVISION": "dynamic-test", "COST_CENTER": "thesis", "CAPACITY_TYPE": "spot",
    }
    return subprocess.Popen([sys.executable, str(WORKER)], env=child_env, cwd=WORKER.parent,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def events(stdout):
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def finish(process, timeout=60):
    out, err = process.communicate(timeout=timeout)
    return process.returncode, events(out), err


def test_worker_resumes_from_committed_checkpoint_after_replacement(aws):
    env, clients = aws
    code, first, err = finish(start(env, token=1, max_steps=3))
    assert code == 0, err
    assert meta(clients)["status"] == "SUSPENDED"
    assert first[-1]["event"] == "usage" and first[-1]["usage"]["steps"] == 3
    saved = int(meta(clients)["checkpoint_seq"])

    # Replacement attempt: the operator fences the old attempt (token 2).
    clients["dynamodb"].update_item(
        TableName="control", Key={"PK": {"S": f"JOB#{JOB}"}, "SK": {"S": "META"}},
        UpdateExpression="SET fencing_token = :t, #s = :r",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":t": {"N": "2"}, ":r": {"S": "RESTORING"}})
    code, second, err = finish(start(env, token=2, max_steps=6))
    assert code == 0, err
    resumed = next(e for e in second if e["event"] == "resumed")
    assert resumed["progress"] == 3 and resumed["sequence"] == saved
    assert int(meta(clients)["checkpoint_seq"]) > saved
    assert second[-1]["usage"]["steps"] == 3  # only new work is billed to this attempt


def test_stale_worker_is_fenced_while_running(aws):
    env, clients = aws
    old = start(env, token=1, max_steps=60)
    deadline = time.monotonic() + 30
    while "checkpoint_seq" not in meta(clients):
        assert time.monotonic() < deadline, "first checkpoint never committed"
        assert old.poll() is None, old.communicate()[1]
        time.sleep(0.2)
    # A replacement claims the job; the still-running old Pod must stop writing.
    clients["dynamodb"].update_item(
        TableName="control", Key={"PK": {"S": f"JOB#{JOB}"}, "SK": {"S": "META"}},
        UpdateExpression="SET fencing_token = :t", ExpressionAttributeValues={":t": {"N": "2"}})
    code, records, err = finish(old)
    assert code == 0, err
    assert [r["event"] for r in records][-2:] == ["fenced", "usage"]
    assert records[-1]["outcome"] == "fenced"


def test_stale_worker_cannot_start(aws):
    env, clients = aws
    clients["dynamodb"].update_item(
        TableName="control", Key={"PK": {"S": f"JOB#{JOB}"}, "SK": {"S": "META"}},
        UpdateExpression="SET fencing_token = :t", ExpressionAttributeValues={":t": {"N": "2"}})
    code, records, err = finish(start(env, token=1, max_steps=3))
    assert code != 0
    assert "ConditionalCheckFailed" in err
    assert "checkpoint_seq" not in meta(clients)


def test_suspends_on_sigterm(aws):
    env, clients = aws
    worker = start(env, token=1, max_steps=0)
    deadline = time.monotonic() + 30
    while not meta(clients).get("status") == "RUNNING":
        assert time.monotonic() < deadline
        time.sleep(0.2)
    worker.terminate()
    code, records, err = finish(worker)
    assert code == 0, err
    assert meta(clients)["status"] == "SUSPENDED"
    assert records[-1]["event"] == "usage"
