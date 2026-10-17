"""Run one CPU Job in an explicitly selected Kubernetes context."""
import argparse
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="robot-hello")
    parser.add_argument("--image", help="Approved image, preferably pinned by digest")
    args = parser.parse_args()
    base = ["kubectl", "--context", args.context, "--namespace", args.namespace,
            "--request-timeout=20s"]

    def call(*command, data=None):
        return subprocess.check_output(base + list(command), input=data, text=True, timeout=180)

    manifest = json.loads((Path(__file__).resolve().parents[1]/"k8s/hello/job.json").read_text())
    if args.image:
        manifest["spec"]["template"]["spec"]["containers"][0]["image"] = args.image
    call("get", "namespace", args.namespace)
    created = json.loads(call("create", "-f", "-", "-o", "json", data=json.dumps(manifest)))
    name = created["metadata"]["name"]
    print(f"Created {args.context}/{args.namespace}/{name}", flush=True)
    try:
        call("wait", "--for=condition=complete", f"job/{name}", "--timeout=150s")
        logs = call("logs", f"job/{name}")
        print(logs, end="")
        if "HELLO_ROBOT_K8S" not in logs.splitlines():
            raise RuntimeError("Completed Job did not emit expected marker")
        print("Kubernetes Hello World: PASSED")
    except Exception:
        for command in [("describe", "job", name), ("logs", f"job/{name}")]:
            try:
                print(call(*command))
            except subprocess.SubprocessError as error:
                print(f"Diagnostic unavailable: {error}")
        raise
    finally:
        call("delete", "job", name, "--wait=false", "--ignore-not-found=true")


if __name__ == "__main__":
    main()
