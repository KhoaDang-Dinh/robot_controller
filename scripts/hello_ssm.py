"""Discover the Terraform SSM contract, verify live EKS, and run Hello World."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

IMAGES = Path(__file__).resolve().parents[1]/"config/images.json"


def expected_registry():
    """Shared GHCR namespace this repository builds into (config/images.json)."""
    return json.loads(IMAGES.read_text())["registry"]


def validate_contract(contract, region, project, environment, account, cluster,
                      registry=None):
    if contract.get("schema_version") != 1:
        raise ValueError("Unsupported platform contract schema")
    for field, expected in (("region", region), ("project", project),
                            ("environment", environment), ("account_id", account)):
        if contract.get(field) != expected:
            raise ValueError(f"Contract {field} mismatch")
    for field in ("cluster_name", "cluster_arn", "cluster_endpoint", "research_bucket",
                  "control_table", "event_queue_url", "controller_role_arn", "hello_namespace"):
        if not isinstance(contract.get(field), str) or not contract[field]:
            raise ValueError(f"Missing contract field: {field}")
    if cluster.get("status") != "ACTIVE":
        raise ValueError("EKS control plane is not ACTIVE")
    for source, target in (("cluster_name", "name"), ("cluster_arn", "arn"),
                           ("cluster_endpoint", "endpoint")):
        if contract[source] != cluster.get(target):
            raise ValueError(f"Stale or incorrect cluster contract: {source}")
    # Both repositories must push to and pull from the same GHCR namespace.
    registry = registry or expected_registry()
    if contract.get("container_registry") != registry:
        raise ValueError(
            "Contract container_registry does not match config/images.json "
            f"({contract.get('container_registry')!r} != {registry!r}); "
            "reapply infra eks-runtime or fix the registry in one repository")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--project", default="robot-sim")
    parser.add_argument("--environment", default="dev")
    args = parser.parse_args()

    def aws(*command):
        return subprocess.check_output(
            ["aws", "--region", args.region, "--no-cli-pager", *command],
            text=True, timeout=90)

    parameter = f"/{args.project}/{args.environment}/contracts/kubernetes/v1"
    result = json.loads(aws("ssm", "get-parameter", "--name", parameter, "--output", "json"))
    contract = json.loads(result["Parameter"]["Value"])
    identity = json.loads(aws("sts", "get-caller-identity", "--output", "json"))
    # Check identity before using the discovered cluster name.
    if contract.get("account_id") != identity["Account"] or contract.get("region") != args.region:
        raise ValueError("Contract account/region mismatch")
    cluster = json.loads(aws("eks", "describe-cluster", "--name", contract["cluster_name"],
                             "--output", "json"))["cluster"]
    validate_contract(contract, args.region, args.project, args.environment, identity["Account"], cluster)
    print(f"Using {parameter}, SSM version {result['Parameter']['Version']}", flush=True)
    with tempfile.TemporaryDirectory() as temp:
        kubeconfig = str(Path(temp)/"kubeconfig")
        aws("eks", "update-kubeconfig", "--name", contract["cluster_name"],
            "--kubeconfig", kubeconfig, "--alias", "robot-hello-ssm")
        env = {**os.environ, "KUBECONFIG": kubeconfig}
        kubectl = ["kubectl", "--context", "robot-hello-ssm", "--request-timeout=20s"]
        # Infra bootstrap creates the namespace; the app role has namespace-only access.
        # Non-secret resource discovery is materialized for future workloads.
        config = {"apiVersion": "v1", "kind": "ConfigMap",
                  "metadata": {"name": "robot-platform-contract", "namespace": contract["hello_namespace"]},
                  "data": {"contract.json": json.dumps(contract),
                           "ssm-parameter": parameter, "ssm-version": str(result["Parameter"]["Version"])}}
        subprocess.run(kubectl + ["apply", "-f", "-"], input=json.dumps(config),
                       text=True, env=env, check=True, timeout=60)
        subprocess.run([sys.executable, str(Path(__file__).with_name("hello_k8s.py")),
                        "--context", "robot-hello-ssm", "--namespace", contract["hello_namespace"]],
                       env=env, check=True)


if __name__ == "__main__":
    main()

