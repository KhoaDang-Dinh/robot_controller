# Self-hosted runner and scoped dev deployment

robot_controller uses the externally supplied EC2 runner with its administrator instance
role as the source identity. infra_robot_sim creates a separate
robot-sim-dev-RobotControllerDeploy role. The app workflow must assume it before checkout
or deployment. Failed assumption or unexpected caller identity stops the job.

## Setup order

1. Register a trusted runner with robot_controller using labels
   self-hosted, linux, x64, robot-sim-admin. If using the same EC2 host for two repository
   runners, use separate runner installations/services and work directories; do not run
   two listeners from one runner registration.
2. Apply infra/environments/dev/access using the existing access backend/state.
   Existing runner_role_arn is trusted by default. Set robot_controller_runner_role_arn
   when the app uses a different externally supplied runner IAM role.
3. Set only AWS_REGION in robot_controller's GitHub environment dev.
   Before checkout, the runner uses its source identity to read the exact SSM parameter
   /robot-sim/dev/access/robot-controller-deploy-role-arn.
   It validates the returned role name, account and partition against its caller identity,
   then assumes that role. DEV_DEPLOY_ROLE is no longer required.
   Missing/inaccessible parameters or unexpected ARNs fail the workflow; there is no
   guessed role or admin deployment fallback. Apply the access root first to publish it.
   This read-only discovery plus STS role assumption is the only bootstrap use of the
   runner source identity. Application operations run under the scoped dev role.
4. Apply dev/base, then start/reapply dev/eks-runtime. Runtime reads that role ARN from
   SSM and creates its EKS access entry plus namespace-scoped AmazonEKSEditPolicy
   association. The updated runtime-start workflow creates robot-hello and robot-platform.
   For an existing cluster, run its namespace bootstrap step with the infra identity.
5. Merge the reviewed app changes into dev and dispatch
   Dev smoke through scoped deployment role from dev. AWS CLI, Python 3 and kubectl
   must be installed on the runner. No static AWS access keys or OIDC provider are required.

## Permission boundary

The dev deployment role has ssm:GetParameter for the Kubernetes contract only and
eks:DescribeCluster for the dev cluster only. EKS grants edit access in robot-hello and
robot-platform, not cluster-admin. The role cannot create namespaces or administer
Argo CD's namespace, IAM, VPC, EKS, node groups or Terraform state. Workload Argo changes
are delivered through reviewed GitOps configuration rather than direct Argo administration.
Namespace edit access includes workload/service-account/Secret operations: both namespaces
are trusted development application scopes, not mutually isolated tenants.

The deployment identity is distinct from pod identities. S3 checkpoint permissions belong
to the workload IAM role, not this deployment role. This smoke requires no S3 access.

configure-aws-credentials exchanges the runner identity through STS, then the workflow
verifies the assumed-role ARN. Metadata credential fallback is disabled for subsequent
SDK/CLI calls. No continue-on-error or admin fallback is permitted.
Validation remains on hosted runners without AWS credentials. Dev deployment is manual,
restricted to the dev ref, and uses the dev GitHub environment.

An administrator-backed host is still privileged: role chaining is an operational
least-privilege convention, not a sandbox against malicious code that can reach instance
metadata or control the host. Use only reviewed code on this runner. The EC2 role trust
identifies the runner, not a GitHub repository/ref; configure environment approvals and
runner restrictions accordingly. These GitHub protection settings are not created here.

## Validation status

Local application tests and workflow syntax validation pass. Terraform/EKS role creation,
STS assumption and live Kubernetes authorization require an infra apply and smoke run.
