# Controller cutover, release and rollback

## Ownership and merge order

robot_controller owns all three images in the shared config/images.json, application
code, both charts, workload Argo definitions, promotion and workload recovery tests.
Infra retains AWS lifecycle tests, IAM, EKS/add-ons and destroy/recreate orchestration.
Existing static/dynamic/security tests, multi-pod recovery and cost reporting stay in place.

1. Apply infra's access root first to create RobotControllerDeploy and publish
   /robot-sim/dev/access/robot-controller-deploy-role-arn. Apply the runtime access entry
   before testing the app role. Review Terraform plans; neither PR deploys AWS.
2. Merge the app change into dev before switching Argo's source. Grant this repository
   Actions write access to the existing robot-controller GHCR package. Keep the same
   lowercase GHCR namespace and existing package pull credentials.
3. Run Build and publish workload images on dev. Hosted CI, dynamic recovery, security
   checks and image Trivy scans must pass before publication. Retain the digest and SBOM.
   Builds use hosted runners without AWS credentials; cloud actions use the supplied EC2.
4. Merge the coordinated infra change. Start runtime bootstraps the existing root name
   robot-sim-root from this repository's k8s/argocd/applications. The existing child name
   robot-controller now reads this repository's chart. Review Argo's diff before sync:
   no unexpected resource deletion or duplicate owner. Application definitions can be
   selected with application_revision; the child normally tracks dev for GitOps promotion.
5. Dispatch Promote controller digest to dev GitOps from dev. Review and merge its
   values-dev PR. The chart remains disabled until a tested digest is promoted.
6. Run Dev smoke through scoped deployment role, then infra recovery acceptance with
   the matching tested worker digest and application revision. Capture both repo SHAs,
   chart/config revision, digest and run ID. Stop the runtime after testing or failure.

## Runner contract

Supply an EC2 runner with labels self-hosted, linux, x64, robot-sim-admin and register it
for this repository. Configure environment dev and AWS_REGION. Install AWS CLI, Python3,
kubectl and gh. The runner's source role may be admin; workflows read the one SSM role
parameter and validate its exact account/name before assuming RobotControllerDeploy.
They verify the STS identity and disable metadata fallback before checkout/application code.
No AWS role ARN is copied into app variables. Only trusted dev code runs on this host:
scoped credentials do not isolate code from the administrator-backed EC2 instance.

Infra grants SSM contract read, EKS DescribeCluster and edit access only in robot-hello
and robot-platform. Infra creates namespaces; the app must not request cluster-wide rights.
Promotion uses GitHub contents/PR permissions, separate from its scoped AWS identity.
Argo reads the public application repo over HTTPS; if it becomes private, provision a
read-only repository credential before switching the root source.

## Rollback and checkpoint compatibility

Roll back controller behavior by reverting its values-dev digest/config PR and reviewing
Argo's diff. Use only a previously scanned immutable digest. Record the exact source/config
revision; a mutable dev reference alone is not a release record.

This repository migration does not change checkpoint schemas or payloads. Current workers
read schema-1 checkpoints using legacy or fence-specific key layouts. Never assume an older
image can read a newer payload/layout: validate restore with the candidate rollback image
before resuming an existing experiment. The controller does not serialize Isaac state.

A future checkpoint migration must preserve the original objects, validate restored payloads,
write a new versioned artifact and only then conditionally advance the committed pointer.
Stop/fence active writers before controlled recovery. No automatic down-migration is provided.
Run unit/dynamic and kind e2e gates for the release; real EKS VM-loss acceptance stays in infra.

## Telemetry and cost

The controller uses the same structured telemetry module as workers and emits ready/usage
records with image and Kubernetes identity. See OBSERVABILITY_COST.md for workload usage
accounting and infra's COST_OBSERVABILITY.md for billing reports and budget foundations.
This migration does not provision a collector or dashboard, or claim estimates are actual bills.
