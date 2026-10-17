# Terraform -> SSM -> Kubernetes contract

Terraform owns resource discovery; robot_controller reads it. SSM is a configuration
handoff, not a replacement for Terraform state, a job queue, readiness monitoring or etcd.

| Parameter | Terraform owner | Lifetime |
|---|---|---|
| /robot-sim/dev/contracts/base/v1 | dev/base | Retained with persistent base |
| /robot-sim/dev/contracts/kubernetes/v1 | dev/eks-runtime | Removed on runtime destroy |

Both are Standard String JSON parameters containing resource identifiers only. No tokens,
private keys or credentials belong here. Terraform updates them on apply (no ignore_changes).
The /v1 path and schema_version define the schema, distinct from SSM's value Version.
Breaking schema changes require a new path/version. Consumers reject unknown schemas.

## Run order

1. Apply the existing dev/base root with its existing S3 backend/state. This adds base/v1.
2. Apply the existing dev/eks-runtime root using the normal runtime start workflow. This
   publishes kubernetes/v1 referencing the actual cluster and shared resources.
3. From a trusted runner/shell with the scoped role, in robot_controller:

```bash
python3 scripts/hello_ssm.py --region ap-southeast-1
```

Only region (and optional project/environment) is supplied. The script reads SSM, validates
schema/account/region/environment and checks live EKS status/name/ARN/endpoint. It uses a
temporary kubeconfig without changing the operator's default context, applies the dedicated
namespace plus non-secret robot-platform-contract ConfigMap, and runs the existing Job.
The Job verifies completion/log output and deletes itself. Namespace/ConfigMap remain.
The ConfigMap is a snapshot, refreshed on the next invocation, not continuously synchronized.
Argo CD must not separately own these smoke resources. The Hello World itself needs no
AWS permissions and does not use the controller IAM role published in the contract.

## Access and failure behavior

The runner needs ssm:GetParameter for the exact kubernetes/v1 parameter,
eks:DescribeCluster, STS identity access, and existing EKS access/RBAC for namespace,
ConfigMap, Job and Pod-log operations. The existing RuntimeOperator policy includes the
SSM/EKS read calls, but IAM permission alone does not grant Kubernetes access: an EKS
access entry/RBAC grant is still required. This change does not create a new access grant.
Use existing trusted deployment identity/access setup; do not run with credentials from
an untrusted PR. Kubernetes pods do not inherit these runner credentials.

Missing parameter: apply base then runtime in the correct account/region. Stale contract:
reapply infra and retry. Endpoint mismatch or non-ACTIVE cluster: fail before Kubernetes
writes. ACTIVE is only control-plane readiness; image pull, node/CNI availability and Job
execution are checked by the actual smoke test. A concurrent shutdown may still fail the
run; coordinate with runtime lifecycle operations. This is not a distributed lock.

Legacy individual SSM parameters remain compatible. They currently ignore value drift;
the runtime contract has a precondition comparing those shared identifiers with base/v1
so drift fails visibly instead of publishing inconsistent configuration. Reconcile any
legacy drift in the base root before retrying. Runtime desired-state remains workflow-owned.

After acceptance, run Stop dev EKS runtime to stop paid runtime resources. Terraform removes
kubernetes/v1; base/v1 stays. A partial/failed destroy may leave a parameter, so consumers
always verify the live cluster instead of trusting presence. No new AWS resources were
applied by this code change. Live acceptance requires configured AWS/EKS access.
