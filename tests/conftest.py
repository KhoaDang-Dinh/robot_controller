"""Shared fixtures: an in-process AWS emulator seeded like the dev platform."""
import os

import boto3
import pytest
from moto import mock_aws

REGION = "us-east-1"


@pytest.fixture()
def platform(monkeypatch):
    """SSM parameters, research bucket and control table, as Terraform creates them."""
    for key in [k for k in os.environ if k.startswith("AWS_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        session = boto3.session.Session(region_name=REGION)
        session.client("s3").create_bucket(Bucket="research")
        session.client("dynamodb").create_table(
            TableName="control", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                  {"AttributeName": "SK", "AttributeType": "S"}])
        ssm = session.client("ssm")
        for name, value in (("storage/research-bucket", "research"), ("state/control-table", "control"),
                            ("runtime/desired-state", "running")):
            ssm.put_parameter(Name=f"/robot-sim/dev/{name}", Value=value, Type="String", Overwrite=True)
        yield session


def meta(session, shard):
    item = session.resource("dynamodb").Table("control").get_item(
        Key={"PK": f"JOB#{shard}", "SK": "META"}, ConsistentRead=True).get("Item", {})
    return item


def seed_external(session, shard, token=1, status="PROVISIONING"):
    session.resource("dynamodb").Table("control").put_item(Item={
        "PK": f"JOB#{shard}", "SK": "META", "status": status, "fencing_token": token, "resume_required": False})
