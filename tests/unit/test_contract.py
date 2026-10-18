import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("ssm", Path(__file__).resolve().parents[2]/"scripts/hello_ssm.py")
ssm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssm)

class ContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = dict(schema_version=1, project="robot-sim", environment="dev",
                             region="ap-southeast-1", account_id="123456789012",
                             cluster_name="lab", cluster_arn="arn:cluster", cluster_endpoint="https://new",
                             research_bucket="bucket", control_table="table", event_queue_url="queue",
                             controller_role_arn="arn:role", hello_namespace="robot-hello",
                             container_registry="ghcr.io/khoadang-dinh")
        self.cluster = dict(status="ACTIVE", name="lab", arn="arn:cluster", endpoint="https://new")
    def check(self):
        ssm.validate_contract(self.contract, "ap-southeast-1", "robot-sim", "dev", "123456789012", self.cluster)
    def test_valid(self):
        self.check()
    def test_reject_schema_account_environment_region(self):
        original = copy.deepcopy(self.contract)
        for field in ("schema_version", "account_id", "environment", "region"):
            with self.subTest(field=field):
                self.contract = {**original, field: "wrong"}
                with self.assertRaises(ValueError): self.check()
    def test_reject_stale_endpoint(self):
        self.cluster["endpoint"] = "https://replacement"
        with self.assertRaises(ValueError): self.check()
    def test_reject_deleting(self):
        self.cluster["status"] = "DELETING"
        with self.assertRaises(ValueError): self.check()
    def test_reject_missing_resource(self):
        del self.contract["research_bucket"]
        with self.assertRaises(ValueError): self.check()
    def test_registry_matches_repository_config(self):
        self.assertEqual(ssm.expected_registry(), "ghcr.io/khoadang-dinh")
    def test_reject_registry_mismatch(self):
        for value in ("ghcr.io/KhoaDang-Dinh", "ghcr.io/someone-else", None):
            with self.subTest(value=value):
                self.contract["container_registry"] = value
                with self.assertRaises(ValueError): self.check()
