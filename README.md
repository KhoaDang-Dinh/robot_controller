# robot_controller

Kubernetes workload repository for the robot thesis. See [static architecture, dynamic
behavior and branches](docs/BASELINE.md). Start with a CPU-only Job: no custom image,
AWS credentials, GPU, service, ingress or persistent volume.

## Smallest run

Prerequisites: Python 3.10+, kubectl, a reachable Kubernetes cluster with a Ready Linux
worker, permission to manage Jobs/read Pod logs in robot-hello, and node access to pull
busybox or an approved mirror. Local kind/minikube works; EKS uses the existing infra
runtime runbook. This script does not provision a cluster.

```bash
kubectl config get-contexts
# Replace YOUR_CONTEXT explicitly.
kubectl --context YOUR_CONTEXT create namespace robot-hello --dry-run=client -o yaml | kubectl --context YOUR_CONTEXT apply -f -
python3 scripts/hello_k8s.py --context YOUR_CONTEXT
```

Expected: `HELLO_ROBOT_K8S` and `Kubernetes Hello World: PASSED`.
The script captures logs, checks completion, diagnoses failure and deletes only its unique
Job. The dedicated namespace remains. Finished Jobs have a 10-minute TTL if the client
is disconnected. If creation times out, inspect Jobs in the same namespace/context;
the server might have accepted creation even though the client timed out.

Add `--image REGISTRY/IMAGE@sha256:DIGEST` to use an approved immutable image.
It must supply sh/printf and support UID 10001. The default busybox version tag is a
convenient development default, not an immutable release pin.

Capture terminal output and both Git SHAs as acceptance evidence. Deleting this Job
**does not stop EKS billing**: run infra_robot_sim's existing Stop dev EKS runtime workflow
after cloud acceptance, including failed acceptance. No Argo Application is installed for
this manual one-shot Job, avoiding a second owner recreating it.

Next: Isaac startup -> UR10 simulated motion -> telemetry -> checkpoint recovery ->
multi-worker recovery. This test proves only scheduling, image pull and execution.

## Infrastructure discovery through SSM

See [Terraform-managed SSM contract](docs/SSM_CONTRACT.md) for the shared interface.
